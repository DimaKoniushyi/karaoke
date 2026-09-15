import ctypes as ct
from pathlib import Path
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
    def open_stream(input_name, output_name, _mirror_name, blocksize, _gain, input_exclusive, info, _error, _size):
        assert (input_name, output_name, blocksize) == ("Chosen microphone", "Chosen speakers", 64)
        for name, value in {"sample_rate": 44100, "output_sample_rate": 48000, "blocksize": 64,
                            "input_period": 441, "output_period": 144,
                            "input_latency_ms": 10, "output_latency_ms": 3,
                            "input_exclusive": input_exclusive}.items():
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


def test_native_period_probe_does_not_open_a_working_audio_stream(monkeypatch):
    library = SimpleNamespace(wm_open=Mock())

    def probe_stream(
        input_name, output_name, _mirror_name, blocksize, _gain,
        input_exclusive, info, _error, _size,
    ):
        assert (input_name, output_name, blocksize, input_exclusive) == (
            "Chosen microphone", "Chosen speakers", 64, 0,
        )
        for name, value in {
            "sample_rate": 48_000,
            "output_sample_rate": 48_000,
            "blocksize": 64,
            "input_period": 288,
            "output_period": 480,
            "input_min_period": 288,
            "output_min_period": 480,
        }.items():
            setattr(info._obj, name, value)
        return 1

    library.wm_probe = Mock(side_effect=probe_stream)
    monkeypatch.setattr(native_wasapi, "load_library", lambda: library)

    info = native_wasapi.NativeWasapiStream.probe(options())

    assert info["minimum_period_latency_ms"] == pytest.approx(16.0)
    library.wm_probe.assert_called_once()
    library.wm_open.assert_not_called()


def test_native_stream_distinguishes_driver_minimum_from_an_engine_period_locked_by_another_app(dll):
    stream = native_wasapi.NativeWasapiStream(options(), {})
    try:
        stream.info.input_min_period = 96
        stream.info.output_min_period = 48
        stream.info.input_period_locked = 1
        stream.info.output_period_locked = 0

        info = stream.diagnostics()

        assert info["input_min_period_frames"] == 96
        assert info["output_min_period_frames"] == 48
        assert info["input_period_locked"] is True
        assert info["output_period_locked"] is False
        assert info["minimum_period_latency_ms"] == pytest.approx(96 / 44.1 + 48 / 48)
        assert info["negotiated_period_latency_ms"] == pytest.approx(441 / 44.1 + 144 / 48)
        assert info["latency_limit"] == "engine-period-locked"
    finally:
        stream.close()


def test_native_stream_classifies_exactly_16ms_shared_period_as_capable(dll):
    stream = native_wasapi.NativeWasapiStream(options(), {})
    try:
        # 8 ms capture + 8 ms render at their respective sample rates.
        stream.info.input_min_period = 352
        stream.info.output_min_period = 384
        info = stream.diagnostics()

        assert info["minimum_period_latency_ms"] == pytest.approx(352 / 44.1 + 384 / 48)
        assert info["minimum_period_latency_ms"] <= 16.0
        assert info["latency_limit"] == "shared-low-latency-capable"
    finally:
        stream.close()


def test_native_stream_reports_whether_raw_mode_actually_engaged(monkeypatch):
    library = SimpleNamespace(
        wm_close=Mock(), wm_start=Mock(return_value=1), wm_pump=Mock(return_value=1), wm_set_raw=Mock()
    )

    def open_stream(_input_name, _output_name, _mirror_name, _blocksize, _gain, _exclusive, info, _error, _size):
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
    assert dll.wm_open.call_args.args[4] == pytest.approx(2.5)

    stream.set_raw(True)
    dll.wm_set_raw.assert_called_once_with(42, 1)
    stream.set_raw(False)
    dll.wm_set_raw.assert_called_with(42, 0)
    stream.close()

    dll.wm_set_raw.reset_mock()
    stream.set_raw(True)  # No handle after close(): must not call into a freed engine.
    dll.wm_set_raw.assert_not_called()


def test_native_stream_opens_an_optional_virtual_microphone_feed_without_replacing_headphones(dll):
    native_wasapi.NativeWasapiStream(
        {**options(), "virtual_output_device_name": "A&D Voice Virtual Microphone Feed"},
        {},
    ).close()

    assert dll.wm_open.call_args.args[:4] == (
        "Chosen microphone",
        "Chosen speakers",
        "A&D Voice Virtual Microphone Feed",
        64,
    )


def test_native_stream_requests_exclusive_capture_without_making_render_exclusive(dll):
    stream = native_wasapi.NativeWasapiStream(
        {**options(), "input_exclusive": True}, {}
    )

    assert dll.wm_open.call_args.args[5] == 1
    diagnostics = stream.diagnostics()
    assert diagnostics["input_exclusive"] is True
    assert diagnostics["output_exclusive"] is False
    stream.close()


def test_native_stream_reports_automatic_capture_render_phase_alignment(dll):
    stream = native_wasapi.NativeWasapiStream(
        {**options(), "input_exclusive": True}, {}
    )
    try:
        stream.info.output_period = 480
        stream.info.output_sample_rate = 48_000

        assert stream.diagnostics()["capture_start_phase_delay_ms"] == pytest.approx(4.0)
    finally:
        stream.close()


def test_native_engine_uses_exclusive_mode_only_for_requested_capture_endpoint():
    source = (
        native_wasapi.library_path().parents[3]
        / "backend/engines/wasapi/monitor.cpp"
    ).read_text(encoding="utf-8")

    assert "AUDCLNT_SHAREMODE_EXCLUSIVE" in source
    assert "input.open(enumerator.Get(), eCapture" in source
    assert "output.open(enumerator.Get(), eRender" in source
    assert "input_exclusive" in source


def test_native_shared_engine_primes_only_the_requested_small_queue_reserve():
    source = (
        native_wasapi.library_path().parents[3]
        / "backend/engines/wasapi/monitor.cpp"
    ).read_text(encoding="utf-8")

    assert "queue->prime_silence();" in source
    assert "queue->reset(!input.exclusive);" in source


def test_native_endpoint_lookup_accepts_portaudio_decorated_windows_names():
    source = (
        native_wasapi.library_path().parents[3]
        / "backend/engines/wasapi/monitor.cpp"
    ).read_text(encoding="utf-8")

    assert "PKEY_Device_DeviceDesc" in source
    assert "endpoint_name_matches" in source
    assert "description" in source[source.index("endpoint_name_matches") : source.index("struct Endpoint")]


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
        stream.stats.resample_ratio = 0.33341
        stream.pump()
        assert stats["stream_latency_ms"] == 22.669
        assert stats["queue_ms"] == 10
        assert stats["queue_dropped_frames"] == 3
        assert stats["resample_ratio"] == pytest.approx(0.33341)
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


def test_callback_failure_with_no_message_still_reports_something(dll):
    # str(error) is "" for an exception raised with no message (a bare
    # `raise ValueError()` or a failed `assert`) -- pump()'s
    # `if callback_error:` check must not treat that empty string as "no
    # callback error happened" and fall back to the generic device-error
    # text, losing the fact that a real callback exception occurred at all.
    stats = {}
    stream = native_wasapi.NativeWasapiStream(options(), stats)
    stream.start(Mock(side_effect=ValueError()))
    samples = (ct.c_float * 4)()
    assert stream.callback(samples, samples, 4) == 0
    assert stats["callback_error"]
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


def test_native_pump_failure_surfaces_the_real_dsp_callback_error(dll):
    # monitor.cpp only ever reports back a fixed "Microphone processing
    # callback failed" string for a DSP-callback failure -- process() (see
    # test_callback_failure_stops_native_output_instead_of_replaying_old_block
    # above) already captured the real Python exception into statistics
    # first, and pump() must prefer that over the generic device-error text
    # a caller would otherwise see instead of the actual reason.
    stats = {}
    stream = native_wasapi.NativeWasapiStream(options(), stats)
    stream.error.value = b"Microphone processing callback failed"
    stats["callback_error"] = "gate: division by zero"
    dll.wm_pump.return_value = 0
    with pytest.raises(RuntimeError, match="division by zero"):
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
    assert "output.open(enumerator.Get(), eRender, output_name, blocksize, initialize, false)" in source


def test_native_shared_compares_raw_and_non_raw_candidates_before_selecting():
    source = (native_wasapi.library_path().parents[3] / "backend/engines/wasapi/monitor.cpp").read_text(encoding="utf-8")
    capture = source[source.index("if (flow == eCapture)"):source.index("} else {", source.index("if (flow == eCapture)"))]
    render = source[source.index("} else {", source.index("if (flow == eCapture)")):source.index("if (!client) throw")]

    # Both modes must be probed before selection: some drivers reject RAW,
    # while some consumer endpoints advertise a shorter normal shared period.
    assert capture.count("static_cast<AUDCLNT_STREAMOPTIONS>(0)") == 3
    assert render.count("static_cast<AUDCLNT_STREAMOPTIONS>(0)") >= 5
    assert "if (!client)\n                try_candidate" not in capture
    assert "if (!client)\n                try_candidate" not in render


def test_native_shared_validates_initialization_for_each_candidate_before_selecting_it():
    source = (native_wasapi.library_path().parents[3] / "backend/engines/wasapi/monitor.cpp").read_text(encoding="utf-8")
    candidate = source[source.index("std::stable_sort"):source.index("if (!client) throw")]

    # GetSharedModeEnginePeriod can succeed for a category/options pair that
    # InitializeSharedAudioStream later rejects.  Selection must only retain a
    # candidate after the real shared stream has initialized successfully.
    initialized = candidate.index("InitializeSharedAudioStream")
    selected = candidate.index("select(candidate)")
    assert initialized < selected


def test_native_shared_queries_all_candidates_before_initializing_shortest_first():
    source = (native_wasapi.library_path().parents[3] / "backend/engines/wasapi/monitor.cpp").read_text(encoding="utf-8")
    categories = source.index("if (flow == eCapture)")
    initialized = source.index("InitializeSharedAudioStream")

    # Initializing the first category while the rest are still being queried
    # can lock the shared engine to that first period, causing Windows to
    # reject a later, genuinely shorter candidate as PERIODICITY_LOCKED.
    assert categories < initialized
    assert "std::stable_sort" in source[categories:initialized]


def test_native_shared_falls_back_to_current_period_when_an_existing_app_locks_engine():
    source = (native_wasapi.library_path().parents[3] / "backend/engines/wasapi/monitor.cpp").read_text(encoding="utf-8")

    assert "AUDCLNT_E_ENGINE_PERIODICITY_LOCKED" in source
    assert "GetCurrentSharedModeEnginePeriod" in source
    current = source.index("GetCurrentSharedModeEnginePeriod")
    current_initialize = source.index("InitializeSharedAudioStream", current)
    assert current < current_initialize
    assert "output.open(enumerator.Get(), eRender, output_name, blocksize, initialize, false)" in source


def test_native_render_probes_game_chat_for_low_latency_without_ducking_other_audio():
    source = (native_wasapi.library_path().parents[3] / "backend/engines/wasapi/monitor.cpp").read_text(encoding="utf-8")
    render_branch = source[source.index("} else {"):source.index("if (!client) throw")]
    assert "try_candidate(AudioCategory_GameChat, AUDCLNT_STREAMOPTIONS_RAW);" in render_branch


def test_native_render_prefers_realtime_non_ducking_category_on_equal_period():
    source = (native_wasapi.library_path().parents[3] / "backend/engines/wasapi/monitor.cpp").read_text(encoding="utf-8")
    render_branch = source[source.index("} else {"):source.index("if (!client) throw")]

    # stable_sort preserves probe order when period and RAW mode tie. Voice
    # monitoring is a realtime chat stream, so GameChat must win that tie; it
    # receives the communications latency policy without attenuating music.
    assert render_branch.index(
        "try_candidate(AudioCategory_GameChat, AUDCLNT_STREAMOPTIONS_RAW);"
    ) < render_branch.index("try_candidate(AudioCategory_Media, AUDCLNT_STREAMOPTIONS_RAW);")
    assert render_branch.index(
        "try_candidate(AudioCategory_GameChat, static_cast<AUDCLNT_STREAMOPTIONS>(0));"
    ) < render_branch.index(
        "try_candidate(AudioCategory_Media, static_cast<AUDCLNT_STREAMOPTIONS>(0));"
    )


def test_native_capture_prefers_communications_policy_on_equal_period():
    source = (native_wasapi.library_path().parents[3] / "backend/engines/wasapi/monitor.cpp").read_text(encoding="utf-8")
    start = source.index("if (flow == eCapture)")
    capture_branch = source[start:source.index("} else {", start)]

    # Equal engine periods do not imply equal endpoint policy. Monitoring is a
    # bidirectional realtime-voice scenario, for which Windows documents the
    # Communications category as selecting the appropriate device mode and
    # latency parameters.
    assert capture_branch.index(
        "try_candidate(AudioCategory_Communications, AUDCLNT_STREAMOPTIONS_RAW);"
    ) < capture_branch.index("try_candidate(AudioCategory_Other, AUDCLNT_STREAMOPTIONS_RAW);")
    assert capture_branch.index(
        "try_candidate(AudioCategory_Communications, static_cast<AUDCLNT_STREAMOPTIONS>(0));"
    ) < capture_branch.index(
        "try_candidate(AudioCategory_Other, static_cast<AUDCLNT_STREAMOPTIONS>(0));"
    )


def test_native_render_probes_neutral_and_realtime_categories_before_accepting_legacy_period():
    source = (native_wasapi.library_path().parents[3] / "backend/engines/wasapi/monitor.cpp").read_text(encoding="utf-8")
    render_branch = source[source.index("} else {"):source.index("if (!client) throw")]

    # OEM drivers map categories to different signal-processing modes and
    # periods.  Realtek commonly keeps Media on a legacy period while Other
    # or Communications exposes its RTC period; candidate selection already
    # chooses by measured duration, so omitting these categories silently
    # strands the endpoint on the slower graph.
    assert "try_candidate(AudioCategory_Other, AUDCLNT_STREAMOPTIONS_RAW);" in render_branch
    assert "try_candidate(AudioCategory_Communications, AUDCLNT_STREAMOPTIONS_RAW);" in render_branch
    assert "try_candidate(AudioCategory_Other, static_cast<AUDCLNT_STREAMOPTIONS>(0));" in render_branch
    assert "try_candidate(AudioCategory_Communications, static_cast<AUDCLNT_STREAMOPTIONS>(0));" in render_branch


def test_native_wasapi_pump_requests_critical_pro_audio_mmcss_priority():
    source = (native_wasapi.library_path().parents[3] / "backend/engines/wasapi/monitor.cpp").read_text(encoding="utf-8")
    registered = source.index('AvSetMmThreadCharacteristicsW(L"Pro Audio"')
    critical = source.index("AvSetMmThreadPriority(scheduling, AVRT_PRIORITY_CRITICAL)")
    capture_start = source.index("input.start()", registered)
    assert registered < critical < capture_start


def test_native_wasapi_starts_render_clock_before_capture_delivery():
    source = (native_wasapi.library_path().parents[3] / "backend/engines/wasapi/monitor.cpp").read_text(encoding="utf-8")
    start = source[source.index("void start(Process callback)"):source.index("void pump(uint32_t timeout)")]

    # Starting shared render only after the first complete microphone packet
    # makes that packet wait through an additional render-engine quantum.
    # A continuously running empty render clock emits silence until capture is
    # ready, then accepts the first real packet on the next writable quantum.
    assert start.index("output.start();") < start.index("input.start();")


def test_native_wasapi_has_a_bounded_capture_phase_probe_for_hardware_diagnostics():
    """A silent probe can scan endpoint phase while normal startup stays unchanged."""
    source = (native_wasapi.library_path().parents[3] / "backend/engines/wasapi/monitor.cpp").read_text(encoding="utf-8")

    assert "ADVOICE_WASAPI_CAPTURE_START_DELAY_US" in source
    assert "capture_start_delay_us" in source
    assert "std::min<uint32_t>(requested, 20000)" in source


def test_native_wasapi_aligns_capture_to_an_observed_render_clock_boundary():
    source = (native_wasapi.library_path().parents[3] / "backend/engines/wasapi/monitor.cpp").read_text(encoding="utf-8")
    start = source[source.index("void start(Process callback)"):source.index("void pump(uint32_t timeout)")]

    render_start = start.index("output.start();")
    observed_boundary = start.index("WaitForSingleObject(output.event.value", render_start)
    capture_start = start.index("input.start();", observed_boundary)
    assert render_start < observed_boundary < capture_start


def test_native_wasapi_drift_target_is_the_requested_low_latency_block_not_a_full_device_period():
    source = (native_wasapi.library_path().parents[3] / "backend/engines/wasapi/monitor.cpp").read_text(encoding="utf-8")
    assert "std::min<size_t>(blocksize" in source
    assert "capacity, ratio, safety_frames, calibration_output_frames" in source
    assert "drift_calibration_output_frames(" in source


def test_native_wasapi_phase_reserve_converts_render_period_to_capture_rate():
    source = (native_wasapi.library_path().parents[3] / "backend/engines/wasapi/monitor.cpp").read_text(encoding="utf-8")
    queue_target = source[source.index("monitor_queue_target("):source.index(");", source.index("monitor_queue_target("))]

    # A 48-frame/16-kHz capture period and 480-frame/48-kHz render period are
    # 3 ms and 10 ms, not 48 vs 480 frames on one clock. The render quantum is
    # 160 frames in the capture timeline before gcd/phase calculations.
    assert "render_period_at_capture_rate" in queue_target
    assert "render_period_at_capture_rate = UINT32(std::ceil(output.period * ratio))" in source


def test_native_render_prefers_rtc_latency_policy_without_ducking_music():
    source = (Path(__file__).parents[1] / "engines/wasapi/monitor.cpp").read_text(encoding="utf-8")
    render_branch = source[source.index("} else {", source.index("if (flow == eCapture)")):
                           source.index("// Query every option first")]

    # Communications is Windows' real-time voice policy. When its advertised
    # period ties GameChat, it must get first refusal; the session explicitly
    # opts out of ducking so this cannot attenuate karaoke/radio playback.
    assert render_branch.index("AudioCategory_Communications") < render_branch.index("AudioCategory_GameChat")
    assert "SetDuckingPreference(TRUE)" in source


def test_native_wasapi_render_target_never_underfills_one_shared_output_period():
    source = (Path(__file__).parents[1] / "engines/wasapi/monitor.cpp").read_text(encoding="utf-8")
    call = source[source.index("shared_audio::render_padding_target("):]
    call = call[:call.index(");")]
    assert "output.period" in call


def test_native_wasapi_adjusts_clock_drift_only_after_capture_is_drained_on_render_event():
    source = (Path(__file__).parents[1] / "engines/wasapi/monitor.cpp").read_text(encoding="utf-8")
    assert "const bool output_wakeup =" in source
    adjustment = "const bool adjust_drift = shared_audio::should_adjust_drift("
    assert adjustment in source
    assert source.index(adjustment) > source.index(
        'check(capture->GetNextPacketSize(&available), "GetNextPacketSize");',
        source.index("for (unsigned packet = 0;"),
    )
    assert source.count("render_ready(false);") >= 2
    # Capture is handle zero, so WaitForMultipleObjects may keep returning it
    # while the lower-priority render event is also signalled. Hardware clock
    # sampling must therefore not be gated by output_wakeup/adjust_drift.
    assert "observe_output_clock(output_starved);" in source
    assert "if (adjust_drift) observe_output_clock(output_starved);" not in source
    assert "queue->nudge(elapsed_frames, output_starved ? elapsed_frames : 0);" in source


def test_native_drift_uses_correlated_device_positions_not_packet_arrival_counts():
    source = (Path(__file__).parents[1] / "engines/wasapi/monitor.cpp").read_text(encoding="utf-8")

    # Both endpoints expose a device position correlated to Windows' common
    # QPC timebase. Production opts out of scheduler/queue-based pitch
    # steering and applies only the rate measured from those hardware clocks.
    assert "&captured_position, &captured_qpc" in source
    assert "device_clock_rate->observe_capture(captured_position, captured_qpc);" in source
    assert "device_clock_rate->observe_output(position, qpc);" in source
    assert "queue->prefer_device_clock();" in source
    assert "queue->set_device_clock_rate_ratio(device_clock_rate->rate_ratio());" in source


def test_virtual_microphone_mirror_never_preempts_physical_monitoring():
    source = (Path(__file__).parents[1] / "engines/wasapi/monitor.cpp").read_text(encoding="utf-8")
    pump_start = source.index("void pump(uint32_t timeout)")
    pump = source[pump_start:source.index("void observe_output_clock", pump_start)]

    # The optional virtual endpoint is not part of what the singer hears. It
    # must be serviced once, only after capture and the final physical-render
    # submission, rather than adding COM calls ahead of every local packet.
    assert pump.count("mirror_ready();") == 1
    assert pump.index("mirror_ready();") > pump.index(
        "const bool output_starved = render_ready(adjust_drift, submitted_since_wait);"
    )


def test_exclusive_capture_does_not_retain_a_capture_period_in_the_user_queue():
    source = (
        native_wasapi.library_path().parents[3]
        / "backend/engines/wasapi/monitor.cpp"
    ).read_text(encoding="utf-8")

    assert "monitor_queue_target(" in source
    assert "input.exclusive ? input.period : blocksize" not in source


def test_native_underrun_counts_only_when_render_was_not_serviced_in_this_pump():
    source = (
        native_wasapi.library_path().parents[3]
        / "backend/engines/wasapi/monitor.cpp"
    ).read_text(encoding="utf-8")

    assert "written_frames > written_before_wait_service" in source
    assert "render_starved(" in source
    assert "if (fully_starved) ++stats.underruns;" in source
    assert "if (padding < target && !count) ++stats.underruns;" not in source


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
