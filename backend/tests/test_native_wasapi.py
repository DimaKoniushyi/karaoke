import ctypes as ct
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from app.services import monitor_worker, native_wasapi


def options():
    return {"input_device_name": "Chosen microphone", "output_device_name": "Chosen speakers", "blocksize": 64}


def test_native_module_does_not_import_song_pipeline():
    import subprocess
    import sys
    from pathlib import Path
    check = subprocess.run([sys.executable, "-c",
        "import sys; from app.services import native_wasapi; "
        "assert 'config' not in sys.modules; assert 'AI' not in sys.modules; "
        "assert 'AI.pipeline' not in sys.modules"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert check.returncode == 0, check.stderr


def test_packaged_library_stays_beside_worker(monkeypatch, tmp_path):
    import sys
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "KaraokeBackend.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    assert native_wasapi.library_path() == tmp_path / "KaraokeWasapi.dll"


@pytest.fixture
def dll(monkeypatch):
    library = SimpleNamespace(
        wm_close=Mock(), wm_start=Mock(return_value=1), wm_pump=Mock(return_value=1), wm_set_raw=Mock()
    )
    def open_stream(input_name, output_name, blocksize, _gain, info, _error, _size):
        assert (input_name, output_name, blocksize) == ("Chosen microphone", "Chosen speakers", 64)
        for name, value in {"sample_rate": 44100, "output_sample_rate": 48000, "blocksize": 64,
                            "input_period": 441, "output_period": 144,
                            "input_latency_ms": 10, "output_latency_ms": 3}.items():
            setattr(info._obj, name, value)
        return 42
    library.wm_open = Mock(side_effect=open_stream)
    monkeypatch.setattr(native_wasapi, "load_library", lambda: library)
    return library


def test_native_stream_reports_real_format_and_periods_without_changing_settings(dll):
    requested = options()
    stream = native_wasapi.NativeWasapiStream(requested, {})
    info = stream.diagnostics()
    assert info["sample_rate"] == 44100 and info["output_sample_rate"] == 48000
    assert info["blocksize"] == 64 and info["input_period_frames"] == 441
    assert info["latency_source"] == "wasapi-stream-report" and not info["exclusive"]
    assert requested == options()
    stream.close()
    stream.close()
    dll.wm_close.assert_called_once_with(42)


def test_native_stream_reports_whether_raw_mode_actually_engaged(monkeypatch):
    library = SimpleNamespace(
        wm_close=Mock(), wm_start=Mock(return_value=1), wm_pump=Mock(return_value=1), wm_set_raw=Mock()
    )

    def open_stream(_input_name, _output_name, _blocksize, _gain, info, _error, _size):
        # A driver that rejected AUDCLNT_STREAMOPTIONS_RAW on the input side
        # (see Endpoint::open's try_candidate fallback) but accepted it for
        # output -- both are reported independently.
        for name, value in {"sample_rate": 44100, "output_sample_rate": 48000, "blocksize": 64,
                            "input_raw": 0, "output_raw": 1}.items():
            setattr(info._obj, name, value)
        return 42

    library.wm_open = Mock(side_effect=open_stream)
    monkeypatch.setattr(native_wasapi, "load_library", lambda: library)
    stream = native_wasapi.NativeWasapiStream(options(), {})
    info = stream.diagnostics()
    assert info["input_raw"] is False and info["output_raw"] is True
    stream.close()


def test_native_stream_threads_gain_and_toggles_raw_mode(dll):
    stream = native_wasapi.NativeWasapiStream({**options(), "gain": 2.5}, {})
    dll.wm_open.assert_called_once()
    assert dll.wm_open.call_args.args[3] == pytest.approx(2.5)

    stream.set_raw(True)
    dll.wm_set_raw.assert_called_once_with(42, 1)
    stream.set_raw(False)
    dll.wm_set_raw.assert_called_with(42, 0)
    stream.close()

    dll.wm_set_raw.reset_mock()
    stream.set_raw(True)  # No handle after close(): must not call into a freed engine.
    dll.wm_set_raw.assert_not_called()


def test_native_callback_reuses_existing_dsp_and_supports_partial_engine_packets(dll):
    stats = {}
    stream = native_wasapi.NativeWasapiStream(options(), stats)
    def dsp(source, output, frames, _clock, _status):
        assert source.shape == output.shape == (frames, 1)
        output[:] = source * .5
    stream.start(dsp)
    for frames in (64, 57):
        source = (ct.c_float * frames)(*([.4] * frames))
        output = (ct.c_float * frames)()
        assert stream.callback(source, output, frames) == 1
        assert np.allclose(output, .2)
    stream.close()


@pytest.mark.parametrize("latency", [0, -1])
def test_unavailable_native_latency_is_not_reported_as_zero(dll, latency):
    stats = {}
    stream = native_wasapi.NativeWasapiStream(options(), stats)
    try:
        stream.info.input_latency_ms = stream.info.output_latency_ms = latency
        stream.stats.stream_latency_ms = latency
        stream.pump()
        assert stats["stream_latency_ms"] is None
        assert stream.diagnostics()["input_latency_ms"] is None
        assert stream.diagnostics()["output_latency_ms"] is None
    finally:
        stream.close()


def test_native_clock_and_bounded_queue_statistics_are_propagated(dll):
    stats = {}
    stream = native_wasapi.NativeWasapiStream(options(), stats)
    try:
        stream.stats.stream_latency_ms = 22.6694
        stream.stats.queued_frames = 441
        stream.stats.dropped_frames = 3
        stream.pump()
        assert stats["stream_latency_ms"] == 22.669
        assert stats["queue_ms"] == 10
        assert stats["queue_dropped_frames"] == 3
    finally:
        stream.close()


def test_program_timings_do_not_require_driver_clock_estimate(dll):
    stats = {}
    stream = native_wasapi.NativeWasapiStream(options(), stats)
    try:
        stream.stats.stream_latency_ms = -1
        stream.stats.program_residence_ms = .4567
        stream.stats.queue_residence_ms = .1004
        stream.stats.output_clock_lead_ms = -1
        stream.pump()
        assert stats["stream_latency_ms"] is None
        assert stats["program_residence_ms"] == .457
        assert stats["queue_residence_ms"] == .1
        assert stats["output_clock_lead_ms"] is None
    finally:
        stream.close()


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_invalid_stage_timings_are_unavailable(dll, value):
    stats = {}
    stream = native_wasapi.NativeWasapiStream(options(), stats)
    try:
        for name in native_wasapi.TIMING_FIELDS:
            setattr(stream.stats, name, value)
        stream.pump()
        assert all(stats[name] is None for name in native_wasapi.TIMING_FIELDS)
    finally:
        stream.close()


@pytest.mark.parametrize("version", [None, 1, 3])
def test_mismatched_native_binary_rejected_before_writing_statistics(monkeypatch, version):
    library = SimpleNamespace() if version is None else SimpleNamespace(wm_abi_version=Mock(return_value=version))
    monkeypatch.setattr(native_wasapi, "library_path", lambda: SimpleNamespace(is_file=lambda: True))
    monkeypatch.setattr(native_wasapi.ct, "CDLL", lambda _path: library)
    with pytest.raises(RuntimeError, match="rebuild"):
        native_wasapi.load_library()


def test_callback_failure_stops_native_output_instead_of_replaying_old_block(dll):
    stats = {}
    stream = native_wasapi.NativeWasapiStream(options(), stats)
    stream.start(Mock(side_effect=RuntimeError("DSP failed")))
    samples = (ct.c_float * 4)()
    assert stream.callback(samples, samples, 4) == 0
    assert stats["callback_error"] == "DSP failed"
    stream.close()


def test_start_failure_and_idempotent_cleanup(dll):
    stream = native_wasapi.NativeWasapiStream(options(), {})
    dll.wm_start.return_value = 0
    stream.error.value = b"Device invalidated"
    with pytest.raises(RuntimeError, match="Device invalidated"):
        stream.start(Mock())
    stream.abort()
    stream.close()
    dll.wm_close.assert_called_once_with(42)


def test_missing_library_is_not_silently_replaced_with_slow_duplex(monkeypatch, tmp_path):
    monkeypatch.setattr(native_wasapi, "library_path", lambda: tmp_path / "missing.dll")
    with pytest.raises(RuntimeError, match="missing"):
        native_wasapi.load_library()


def test_native_pump_failure_is_reported(dll):
    stream = native_wasapi.NativeWasapiStream(options(), {})
    stream.error.value = b"Audio device disconnected"
    dll.wm_pump.return_value = 0
    with pytest.raises(RuntimeError, match="disconnected"):
        stream.pump()
    stream.close()


def test_native_shared_candidate_does_not_switch_mode_or_buffer():
    config = {"sample_rate": 48000, "blocksize": 64, "input_device_id": 1, "output_device_id": 2,
              "output_channels": 2, "wasapi_mode": "shared", "native_shared": True}
    candidate = monitor_worker._stream_candidate(config)
    assert candidate["_engine"] == "wasapi-native-shared"
    assert candidate["_mode"] == "shared" and candidate["blocksize"] == 64
    with pytest.raises(ValueError):
        monitor_worker._stream_candidate({**config, "wasapi_mode": "exclusive"})


def test_native_shared_probes_only_categories_valid_for_capture_and_render():
    source = (native_wasapi.library_path().parents[3] / "backend/engines/wasapi/monitor.cpp").read_text(encoding="utf-8")

    probe = source.index("try_candidate")
    period_query = source.index("GetSharedModeEnginePeriod")
    shared_initialize = source.index("InitializeSharedAudioStream")
    assert probe < period_query < shared_initialize
    assert "AudioCategory_ProAudio" not in source
    assert "if (flow == eCapture)" in source
    assert "try_candidate(AudioCategory_Communications, AUDCLNT_STREAMOPTIONS_RAW)" in source
    assert "try_candidate(AudioCategory_Speech, AUDCLNT_STREAMOPTIONS_RAW)" in source
    assert "try_candidate(AudioCategory_Other, AUDCLNT_STREAMOPTIONS_RAW)" in source
    assert "try_candidate(AudioCategory_Other, static_cast<AUDCLNT_STREAMOPTIONS>(0))" in source
    assert "try_candidate(AudioCategory_Media, AUDCLNT_STREAMOPTIONS_RAW)" in source
    assert "try_candidate(AudioCategory_Movie, AUDCLNT_STREAMOPTIONS_RAW)" in source
    assert "try_candidate(AudioCategory_SoundEffects, AUDCLNT_STREAMOPTIONS_RAW)" in source
    assert "try_candidate(AudioCategory_GameEffects, AUDCLNT_STREAMOPTIONS_RAW)" in source
    assert "try_candidate(AudioCategory_Media, static_cast<AUDCLNT_STREAMOPTIONS>(0))" in source
    assert "AudioCategory_GameMedia" not in source
    assert "AUDCLNT_STREAMOPTIONS_RAW" in source
    assert "AUDCLNT_SHAREMODE_EXCLUSIVE" not in source


def test_worker_uses_native_event_pump_and_native_rate(monkeypatch, dll, capsys):
    import json
    import sys
    config = {**options(), "sample_rate": 48000, "input_device_id": 1, "output_device_id": 2,
              "output_channels": 2, "gain": 1, "wasapi_mode": "shared", "native_shared": True}
    monkeypatch.setattr(sys, "argv", ["monitor_worker", "--config", json.dumps(config)])
    monkeypatch.setattr(monitor_worker, "_running", True)
    monkeypatch.setattr(monitor_worker.threading, "Thread", Mock())
    factory = Mock(return_value=Mock())
    monkeypatch.setattr(monitor_worker, "_audio_callback", factory)
    legacy = Mock(side_effect=AssertionError("Legacy duplex must not open"))
    monkeypatch.setattr(monitor_worker.sd, "Stream", legacy)
    def pump(*args):
        monitor_worker._running = False
        return 1
    dll.wm_pump.side_effect = pump
    assert monitor_worker.main() == 0
    assert factory.call_args.args[1] == 44100
    dll.wm_pump.assert_called_once()
    dll.wm_close.assert_called_once_with(42)
    legacy.assert_not_called()
    started = next(event for event in map(json.loads, capsys.readouterr().out.splitlines()) if event["event"] == "started")
    assert started["engine"] == "wasapi-native-shared"
    assert started["input_period_frames"] == 441


def test_native_raw_mode_is_armed_only_without_a_relay(monkeypatch, dll, capsys):
    import json
    import sys
    config = {**options(), "sample_rate": 48000, "input_device_id": 1, "output_device_id": 2,
              "output_channels": 2, "gain": 1, "wasapi_mode": "shared", "native_shared": True}
    monkeypatch.setattr(sys, "argv", ["monitor_worker", "--config", json.dumps(config)])
    monkeypatch.setattr(monitor_worker, "_running", True)
    monkeypatch.setattr(monitor_worker.threading, "Thread", Mock())
    monkeypatch.setattr(monitor_worker, "_audio_callback", Mock(return_value=Mock()))
    monkeypatch.setattr(monitor_worker.sd, "Stream", Mock(side_effect=AssertionError("must not fall back")))

    def pump(*args):
        monitor_worker._running = False
        return 1
    dll.wm_pump.side_effect = pump
    assert monitor_worker.main() == 0
    assert monitor_worker._native_stream_target["raw_eligible"] is True
    assert isinstance(monitor_worker._native_stream_target["stream"], native_wasapi.NativeWasapiStream)
    capsys.readouterr()


def test_native_raw_mode_is_armed_at_startup_when_requested_by_config(monkeypatch, dll, capsys):
    # A "no effects" Settings monitoring session (or an already-on "listen to
    # raw voice" check) has nothing for the Python DSP chain to do, so it
    # should arm the native raw pass-through from the very first block --
    # not only after a live update arrives later over stdin (which this test
    # never even starts a thread for, via the mocked-out Thread below).
    import json
    import sys
    config = {**options(), "sample_rate": 48000, "input_device_id": 1, "output_device_id": 2,
              "output_channels": 2, "gain": 1, "wasapi_mode": "shared", "native_shared": True,
              "dry_monitor": 1}
    monkeypatch.setattr(sys, "argv", ["monitor_worker", "--config", json.dumps(config)])
    monkeypatch.setattr(monitor_worker, "_running", True)
    monkeypatch.setattr(monitor_worker.threading, "Thread", Mock())
    monkeypatch.setattr(monitor_worker, "_audio_callback", Mock(return_value=Mock()))
    monkeypatch.setattr(monitor_worker.sd, "Stream", Mock(side_effect=AssertionError("must not fall back")))

    def pump(*args):
        monitor_worker._running = False
        return 1
    dll.wm_pump.side_effect = pump
    assert monitor_worker.main() == 0
    dll.wm_set_raw.assert_called_once_with(42, 1)
    capsys.readouterr()


def test_native_raw_mode_reports_sanitized_level_instead_of_stale_python_values(monkeypatch, dll, capsys):
    # The native raw pass-through skips this module's own Python callback
    # entirely, which is the only place _level/dsp_compute_ms ever get
    # updated -- without this, the worker kept reporting whatever they last
    # were before raw engaged, a frozen but plausible-looking (and
    # therefore misleading) number instead of visibly unavailable.
    import json
    import sys
    config = {**options(), "sample_rate": 48000, "input_device_id": 1, "output_device_id": 2,
              "output_channels": 2, "gain": 1, "wasapi_mode": "shared", "native_shared": True,
              "dry_monitor": 1}
    monkeypatch.setattr(sys, "argv", ["monitor_worker", "--config", json.dumps(config)])
    monkeypatch.setattr(monitor_worker, "_running", True)
    monkeypatch.setattr(monitor_worker.threading, "Thread", Mock())
    monkeypatch.setattr(monitor_worker, "_audio_callback", Mock(return_value=Mock()))
    monkeypatch.setattr(monitor_worker.sd, "Stream", Mock(side_effect=AssertionError("must not fall back")))
    monkeypatch.setattr(
        monitor_worker, "_level",
        {"rms_db": -6.0, "clipping": True, "silent": False, "real_latency_ms": 12.3},
    )
    monkeypatch.setattr(monitor_worker, "_report_queue", monitor_worker.queue.Queue(maxsize=1))

    def pump(*args):
        monitor_worker.time.sleep(0.11)  # cross the 0.1s report throttle using real time
        monitor_worker._running = False
        return 1
    dll.wm_pump.side_effect = pump
    assert monitor_worker.main() == 0
    report = monitor_worker._report_queue.get_nowait()
    assert report["event"] == "level"
    assert (report["rms_db"], report["clipping"], report["silent"], report["real_latency_ms"]) == (
        -120.0, False, True, None
    )
    capsys.readouterr()


def test_native_raw_mode_is_never_armed_when_a_relay_is_attached(monkeypatch, dll, capsys):
    import json
    import sys
    config = {**options(), "sample_rate": 48000, "input_device_id": 1, "output_device_id": 2,
              "output_channels": 2, "gain": 1, "wasapi_mode": "shared", "native_shared": True,
              "audio_relay_port": 54321}
    monkeypatch.setattr(sys, "argv", ["monitor_worker", "--config", json.dumps(config)])
    monkeypatch.setattr(monitor_worker, "_running", True)
    monkeypatch.setattr(monitor_worker.threading, "Thread", Mock())
    monkeypatch.setattr(monitor_worker, "_audio_callback", Mock(return_value=Mock()))
    monkeypatch.setattr(monitor_worker.sd, "Stream", Mock(side_effect=AssertionError("must not fall back")))
    monkeypatch.setattr(monitor_worker, "RelayLink", Mock())

    def pump(*args):
        monitor_worker._running = False
        return 1
    dll.wm_pump.side_effect = pump
    assert monitor_worker.main() == 0
    # A room peer may still be listening through the relay -- native raw mode
    # must never be armed here, or that peer would go silent whenever the
    # singer flips on the local "listen to raw voice" check. The stream
    # reference itself is still kept (a live volume change must still reach
    # it), only raw_eligible is false.
    assert monitor_worker._native_stream_target["raw_eligible"] is False
    assert isinstance(monitor_worker._native_stream_target["stream"], native_wasapi.NativeWasapiStream)
    capsys.readouterr()
