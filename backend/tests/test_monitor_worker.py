import contextlib
import json
import runpy
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from app.services import monitor_worker
from tests._shared import patch_attrs


def options(): return {'sample_rate': 48000, 'output_channels': 2, 'input_device_id': 1, 'output_device_id': 2, 'blocksize': 64, 'gain': 2, 'wasapi_exclusive': False}


def configure_argv(monkeypatch, config=None): patch_attrs(monkeypatch, sys, argv=['monitor_worker', '--config', json.dumps(config or options())])


def test_emit_and_stop_update_process_contract(monkeypatch, capsys):
    monitor_worker._emit({"event": "started"})
    assert json.loads(capsys.readouterr().out) == {"event": "started"}
    monkeypatch.setattr(monitor_worker, "_running", True)
    monitor_worker._stop(0, None)
    assert monitor_worker._running is False


def test_monitor_worker_limits_python_gil_timeslice_for_realtime_callback(monkeypatch):
    configured = Mock()
    monkeypatch.setattr(monitor_worker.sys, "setswitchinterval", configured)

    monitor_worker._configure_realtime_python()

    configured.assert_called_once_with(0.001)


def test_selected_buffer_is_not_raised_or_replaced():
    candidate = monitor_worker._stream_candidate(options())
    assert candidate["blocksize"] == 64
    assert candidate["latency"] == 64 / 48000
    assert "extra_settings" in candidate


def test_low_rate_keeps_selected_buffer():
    candidate = monitor_worker._stream_candidate({**options(), "sample_rate": 16000})
    assert candidate["blocksize"] == 64
    assert candidate["samplerate"] == 16000


@pytest.mark.parametrize("mode", ["shared", "input-exclusive", "exclusive"])
def test_auto_buffer_is_rejected_instead_of_changing_configuration(mode):
    with pytest.raises(ValueError, match="fixed positive"):
        monitor_worker._stream_candidate({**options(), "wasapi_mode": mode, "blocksize": 0})


def test_variable_callback_frames_and_glitches_are_reported(monkeypatch):
    monkeypatch.setattr(monitor_worker, "_live_params", {"reverb": 0, "echo": 0, "delay": 0, "octave": 0, "noise_suppression": 0})
    stats = {}
    callback = monitor_worker._audio_callback(0.5, 48000, stats)
    for frames in (48, 128, 480, 96):
        samples = np.full((frames, 1), 0.1, dtype=np.float32)
        output = np.empty((frames, 2), dtype=np.float32)
        callback(samples, output, frames, None, "underflow" if frames == 480 else None)
        assert np.isfinite(output).all()
        assert np.allclose(output[:, 0], output[:, 1])
    assert {key: stats[key] for key in ("callback_frames", "callback_count", "glitch_count")} == {"callback_frames": 96, "callback_count": 4, "glitch_count": 1}
    assert stats["dsp_compute_ms"] >= 0


def test_real_latency_is_measured_from_driver_adc_and_dac_timestamps(monkeypatch):
    monkeypatch.setattr(monitor_worker, "_live_params", {"reverb": 0, "echo": 0, "delay": 0, "octave": 0, "noise_suppression": 0})
    callback = monitor_worker._audio_callback(1.0, 48000, {})
    samples = np.full((64, 1), 0.1, dtype=np.float32)
    output = np.empty((64, 2), dtype=np.float32)

    # A real host API timestamp pair: the block was captured 12ms before "now"
    # and is scheduled to play 3ms after "now" -- a 15ms round trip.
    timing = SimpleNamespace(inputBufferAdcTime=100.0, currentTime=100.012, outputBufferDacTime=100.015)
    callback(samples, output, 64, timing, None)
    assert monitor_worker._level["real_latency_ms"] == pytest.approx(15.0, abs=0.001)


def test_real_latency_is_unavailable_without_both_driver_timestamps(monkeypatch):
    monkeypatch.setattr(monitor_worker, "_live_params", {"reverb": 0, "echo": 0, "delay": 0, "octave": 0, "noise_suppression": 0})
    callback = monitor_worker._audio_callback(1.0, 48000, {})
    samples = np.full((64, 1), 0.1, dtype=np.float32)
    output = np.empty((64, 2), dtype=np.float32)

    callback(samples, output, 64, None, None)
    assert monitor_worker._level["real_latency_ms"] is None

    # Some host APIs report only one of the two timestamps -- still unusable.
    callback(samples, output, 64, SimpleNamespace(inputBufferAdcTime=5.0, outputBufferDacTime=0.0), None)
    assert monitor_worker._level["real_latency_ms"] is None


def test_each_mode_uses_only_its_selected_configuration():
    for mode in ("plain", "shared"):
        for block in (64, 128, 256, 512):
            candidate = monitor_worker._stream_candidate({**options(), "wasapi_mode": mode, "blocksize": block})
            assert candidate["blocksize"] == block
            assert candidate["_mode"] == mode
            assert candidate.get("_engine") != "wasapi-split"


def test_main_reports_a_hard_error_when_the_only_candidate_fails(monkeypatch, capsys):
    configure_argv(monkeypatch, options())
    monkeypatch.setattr(monitor_worker, "_running", False)
    always_fails = Mock()
    always_fails.start.side_effect = RuntimeError("no supported configuration")
    monkeypatch.setattr(monitor_worker.sd, "Stream", Mock(return_value=always_fails))

    assert monitor_worker.main() == 1
    assert monitor_worker.sd.Stream.call_count == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {
        "event": "error", "message": "no supported configuration"
    }


def test_exclusive_request_rejected_without_opening_hardware(monkeypatch, capsys):
    configure_argv(monkeypatch, {**options(), "wasapi_mode": "exclusive"})
    monkeypatch.setattr(monitor_worker, "_running", False)
    monkeypatch.setattr(monitor_worker.sd, "InputStream", Mock(side_effect=RuntimeError("separate endpoints rejected")), raising=False)
    monkeypatch.setattr(monitor_worker.sd, "Stream", Mock())
    assert monitor_worker.main() == 1
    monitor_worker.sd.InputStream.assert_not_called()
    monitor_worker.sd.Stream.assert_not_called()
    assert json.loads(capsys.readouterr().out) == {"event": "error", "message": "Unsupported WASAPI mode"}


def test_input_exclusive_request_rejected_without_switching_engine(monkeypatch, capsys):
    configure_argv(monkeypatch, {**options(), "wasapi_mode": "input-exclusive", "blocksize": 128})
    monkeypatch.setattr(monitor_worker, "_running", True)
    input_endpoint = Mock(latency=.006)
    def start_input():
        monitor_worker.sd.InputStream.call_args.kwargs["callback"](np.zeros((64, 1)), 64, None, None)
    input_endpoint.start.side_effect = start_input
    monkeypatch.setattr(monitor_worker.sd, "InputStream", Mock(return_value=input_endpoint), raising=False)
    monkeypatch.setattr(monitor_worker.sd, "OutputStream", Mock(return_value=Mock(latency=.006)), raising=False)
    monkeypatch.setattr(monitor_worker.sd, "Stream", Mock())
    assert monitor_worker.main() == 1
    monitor_worker.sd.Stream.assert_not_called()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events == [{"event": "error", "message": "Unsupported WASAPI mode"}]
    monitor_worker.sd.InputStream.assert_not_called()
    monitor_worker.sd.OutputStream.assert_not_called()


def test_read_live_updates_applies_json_lines_and_ignores_bad_input(monkeypatch):
    monkeypatch.setattr(monitor_worker, "_live_params", {"reverb": 0.0, "echo": 0.0, "delay": 0.0})
    lines = iter(["not-json\n", "\n", '{"reverb": 0.4}\n', '{"echo": 0.2, "delay": 0.1}\n'])
    monkeypatch.setattr(sys, "stdin", lines)
    monitor_worker._read_live_updates()
    assert monitor_worker._live_params == {"reverb": 0.4, "echo": 0.2, "delay": 0.1}


def test_audio_callback_reads_current_live_effect_parameters(monkeypatch):
    monkeypatch.setattr(monitor_worker, "_live_params", {"reverb": 0.3, "echo": 0.4, "delay": 0.5, "noise_suppression": 0.35})
    captured = {}

    class FakeChain:
        def __init__(self, sample_rate): pass

        def process(self, samples, reverb, echo, delay):
            captured["params"] = (reverb, echo, delay)
            return samples

    monkeypatch.setattr(monitor_worker, "MonitorEffectsChain", FakeChain)
    callback, indata, outdata = monitor_worker._audio_callback(1.0, sample_rate=48000), np.zeros((4, 1), dtype=np.float32), np.zeros((4, 2), dtype=np.float32)
    callback(indata, outdata, 4, None, None)
    assert captured["params"] == (0.3, 0.4, 0.5)


def test_audio_callback_primes_dsp_before_realtime_audio_without_reporting_fake_samples(monkeypatch):
    """Cold DSP allocation must happen before the first device callback.

    A cold effects chain delayed the first render phase by almost one complete
    WASAPI quantum.  Priming must stay completely internal: no callback
    counters and, especially, no silent packets sent to a room relay.
    """
    monkeypatch.setattr(
        monitor_worker,
        "_live_params",
        {
            "volume": 1.0,
            "reverb": 0.4,
            "echo": 0.3,
            "delay": 0.2,
            "octave": 0.0,
            "noise_suppression": 0.35,
            "dry_monitor": 0.0,
        },
    )
    calls = {"quality": 0, "pitch": 0, "effects": 0, "relay": 0}

    class FakeQuality:
        def __init__(self, _sample_rate, _channels): pass
        def process(self, samples, _gain, _suppression):
            calls["quality"] += 1
            return samples

    class FakePitch:
        def __init__(self, _sample_rate): pass
        def process(self, samples, _octave):
            calls["pitch"] += 1
            return samples

    class FakeEffects:
        def __init__(self, _sample_rate): pass
        def process(self, samples, _reverb, _echo, _delay):
            calls["effects"] += 1
            return samples

    monkeypatch.setattr(monitor_worker, "StudioMicrophoneProcessor", FakeQuality)
    monkeypatch.setattr(monitor_worker, "RealtimePitchShifter", FakePitch)
    monkeypatch.setattr(monitor_worker, "MonitorEffectsChain", FakeEffects)
    relay = SimpleNamespace(push=lambda *_args: calls.__setitem__("relay", calls["relay"] + 1))
    stats = {}

    monitor_worker._audio_callback(1.0, 48_000, stats, relay)

    assert calls == {"quality": 8, "pitch": 8, "effects": 8, "relay": 0}
    assert stats == {}


def test_audio_callback_reports_pitch_latency_only_for_the_locally_heard_path(monkeypatch):
    stats = {}
    monkeypatch.setattr(
        monitor_worker,
        "_live_params",
        {"volume": 1, "reverb": 0, "echo": 0, "delay": 0,
         "octave": -0.5, "noise_suppression": 0, "dry_monitor": 0},
    )
    callback = monitor_worker._audio_callback(1.0, 48_000, stats)
    callback(
        np.zeros((64, 1), dtype=np.float32),
        np.empty((64, 2), dtype=np.float32),
        64, None, None,
    )
    assert stats["effect_latency_ms"] == pytest.approx(6.0, abs=0.07)

    monitor_worker._live_params = {**monitor_worker._live_params, "dry_monitor": 1}
    callback(
        np.zeros((64, 1), dtype=np.float32),
        np.empty((64, 2), dtype=np.float32),
        64, None, None,
    )
    assert stats["effect_latency_ms"] == 0.0


def test_native_raw_bypass_clears_stale_effect_latency_without_a_dsp_callback(monkeypatch):
    reports = []
    stream = Mock()

    def stop_after_one_pump():
        monkeypatch.setattr(monitor_worker, "_running", False)

    stream.pump.side_effect = stop_after_one_pump
    clock = iter((0.0, 0.2, 0.2))
    monkeypatch.setattr(monitor_worker.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(monitor_worker, "_running", True)
    monkeypatch.setattr(
        monitor_worker, "_native_stream_target",
        {"stream": stream, "raw_eligible": True},
    )
    monkeypatch.setattr(monitor_worker, "_live_params", {"dry_monitor": 1.0})
    monkeypatch.setattr(monitor_worker, "_queue_report", reports.append)

    monitor_worker._pump_reports(
        stream, "wasapi-native-shared", threading.Event(),
        {"effect_latency_ms": 6.0, "dsp_compute_ms": 0.2},
    )

    assert reports[0]["effect_latency_ms"] == 0.0
    assert reports[0]["dsp_compute_ms"] is None


def test_main_seeds_live_params_from_config_and_starts_reader_thread(monkeypatch, capsys):
    configure_argv(monkeypatch, {**options(), "reverb": 0.2, "echo": 0.5, "delay": 0.7})
    patch_attrs(monkeypatch, monitor_worker, _running=False, _live_params={'reverb': 0.0, 'echo': 0.0, 'delay': 0.0, 'noise_suppression': 0.35})
    thread_started, real_thread_init = threading.Event(), monitor_worker.threading.Thread.__init__

    def watched_init(self, *args, **kwargs):
        if kwargs.get("target") is monitor_worker._read_live_updates:
            thread_started.set()
            assert kwargs.get("daemon") is True
        real_thread_init(self, *args, **kwargs)

    monkeypatch.setattr(monitor_worker.threading.Thread, "__init__", watched_init)
    stream = Mock()
    monkeypatch.setattr(monitor_worker.sd, "Stream", Mock(return_value=stream))

    assert (monitor_worker.main() == 0) and (monitor_worker._live_params == {'reverb': 0.2, 'echo': 0.5, 'delay': 0.7, 'noise_suppression': 0.35, 'octave': 0.0, 'volume': 2.0, 'dry_monitor': 0.0, 'local_monitoring_enabled': 1.0}) and (thread_started.is_set())
    capsys.readouterr()


def test_audio_callback_pushes_dry_and_wet_to_the_relay_when_configured(monkeypatch):
    monkeypatch.setattr(monitor_worker, "_live_params", {"reverb": 0, "echo": 0, "delay": 0, "octave": 0, "noise_suppression": 0})
    pushed = []
    relay = SimpleNamespace(push=lambda stream_id, sample_rate, samples: pushed.append((stream_id, sample_rate, len(samples))))
    callback = monitor_worker._audio_callback(1.0, 48000, {}, relay)
    callback(np.zeros((32, 1), dtype=np.float32), np.empty((32, 2), dtype=np.float32), 32, None, None)
    assert pushed == [
        (monitor_worker.STREAM_CAPTURE, 48000, 32),
        (monitor_worker.STREAM_DRY, 48000, 32),
        (monitor_worker.STREAM_WET, 48000, 32),
    ]


def test_audio_callback_never_touches_a_relay_when_none_is_configured(monkeypatch):
    monkeypatch.setattr(monitor_worker, "_live_params", {"reverb": 0, "echo": 0, "delay": 0, "octave": 0, "noise_suppression": 0})
    callback = monitor_worker._audio_callback(1.0, 48000, {})
    # Must not raise even though relay defaults to None -- this is the
    # regression case: plain solo monitoring is unaffected by the relay code.
    callback(np.zeros((32, 1), dtype=np.float32), np.empty((32, 2), dtype=np.float32), 32, None, None)


def _spy_on_quality_process(monkeypatch):
    calls = []
    original = monitor_worker.StudioMicrophoneProcessor.process

    def spy(self, *args, **kwargs):
        calls.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(monitor_worker.StudioMicrophoneProcessor, "process", spy)
    return calls


def test_dry_monitor_skips_the_whole_dsp_chain_without_a_relay(monkeypatch):
    # Nothing local needs the processed signal (dry_monitor bypasses it) and
    # no relay/room peer is listening either -- the native WASAPI engine
    # already skips this whole callback for that case (see raw_active in
    # monitor.cpp); this is the fallback PortAudio engine's own version of
    # the same optimization, so it must not compute a chain it then discards.
    monkeypatch.setattr(
        monitor_worker, "_live_params",
        {"reverb": 0, "echo": 0, "delay": 0, "octave": 0, "noise_suppression": 0, "dry_monitor": 1.0, "volume": 2.0},
    )
    calls = _spy_on_quality_process(monkeypatch)
    callback = monitor_worker._audio_callback(1.0, 48000, {})
    samples = np.full((32, 1), 0.5, dtype=np.float32)
    output = np.empty((32, 2), dtype=np.float32)
    callback(samples, output, 32, None, None)
    assert calls == []
    assert np.allclose(output[:, 0], np.clip(samples[:, 0] * 2.0, -1.0, 1.0))
    assert np.allclose(output[:, 1], output[:, 0])


def test_dry_monitor_still_runs_the_dsp_chain_when_a_relay_is_configured(monkeypatch):
    # A room peer via the relay must keep hearing the normally processed
    # audio even while the singer's own local output is the raw bypass.
    monkeypatch.setattr(
        monitor_worker, "_live_params",
        {"reverb": 0, "echo": 0, "delay": 0, "octave": 0, "noise_suppression": 0, "dry_monitor": 1.0, "volume": 1.0},
    )
    calls = _spy_on_quality_process(monkeypatch)
    pushed = []
    relay = SimpleNamespace(push=lambda stream_id, sample_rate, samples: pushed.append(stream_id))
    callback = monitor_worker._audio_callback(1.0, 48000, {}, relay)
    callback(np.zeros((32, 1), dtype=np.float32), np.empty((32, 2), dtype=np.float32), 32, None, None)
    # Eight silent warm-up blocks prime the room DSP, then the first real
    # block is processed once. Warm-up itself must never reach the relay.
    assert calls == [True] * 9
    assert pushed == [
        monitor_worker.STREAM_CAPTURE,
        monitor_worker.STREAM_DRY,
        monitor_worker.STREAM_WET,
    ]


def test_room_capture_and_relay_continue_while_local_monitor_output_is_muted(monkeypatch):
    monkeypatch.setattr(
        monitor_worker,
        "_live_params",
        {
            "volume": 1.0, "reverb": 0, "echo": 0, "delay": 0,
            "octave": 0, "noise_suppression": 0,
            "local_monitoring_enabled": 0.0,
        },
    )
    pushed = []
    relay = SimpleNamespace(push=lambda stream_id, _sample_rate, samples: pushed.append((stream_id, samples.copy())))
    callback = monitor_worker._audio_callback(1.0, 48_000, {}, relay)
    samples = np.full((32, 1), 0.25, dtype=np.float32)
    output = np.full((32, 2), 123.0, dtype=np.float32)

    callback(samples, output, 32, None, None)

    assert np.count_nonzero(output) == 0
    assert [stream_id for stream_id, _samples in pushed] == [
        monitor_worker.STREAM_CAPTURE,
        monitor_worker.STREAM_DRY,
        monitor_worker.STREAM_WET,
    ]
    assert np.any(pushed[1][1])


def test_main_constructs_a_relay_link_only_when_a_relay_port_is_configured(monkeypatch, capsys):
    configure_argv(monkeypatch, {**options(), "audio_relay_port": 54321})
    monkeypatch.setattr(monitor_worker, "_running", False)
    stream = Mock()
    monkeypatch.setattr(monitor_worker.sd, "Stream", Mock(return_value=stream))
    constructed = {}
    class FakeRelayLink:
        def __init__(self, port, sample_rate):
            constructed["port"], constructed["sample_rate"] = port, sample_rate
        def close(self):
            constructed["closed"] = True
    monkeypatch.setattr(monitor_worker, "RelayLink", FakeRelayLink)

    assert monitor_worker.main() == 0
    assert constructed == {"port": 54321, "sample_rate": 48000, "closed": True}
    capsys.readouterr()


def test_main_skips_the_relay_link_when_no_relay_port_is_configured(monkeypatch, capsys):
    configure_argv(monkeypatch)
    monkeypatch.setattr(monitor_worker, "_running", False)
    stream = Mock()
    monkeypatch.setattr(monitor_worker.sd, "Stream", Mock(return_value=stream))
    monkeypatch.setattr(monitor_worker, "RelayLink", Mock(side_effect=AssertionError("must not construct a relay without a port")))

    assert monitor_worker.main() == 0
    capsys.readouterr()


def test_main_starts_and_stops_first_candidate(monkeypatch, capsys):
    configure_argv(monkeypatch)
    monkeypatch.setattr(monitor_worker, "_running", False)
    stream = Mock()
    monkeypatch.setattr(monitor_worker.sd, "Stream", Mock(return_value=stream))

    assert monitor_worker.main() == 0
    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    assert events == ["stage", "stage", "stage", "started"]
    stream.start.assert_called_once_with()
    stream.abort.assert_called_once_with()
    stream.close.assert_called_once_with()


def test_main_does_not_retry_after_driver_rejection(monkeypatch, capsys):
    configure_argv(monkeypatch)
    monkeypatch.setattr(monitor_worker, "_running", False)
    failed = Mock()
    failed.start.side_effect = RuntimeError("selected settings rejected")
    monkeypatch.setattr(monitor_worker.sd, "Stream", Mock(return_value=failed))
    assert monitor_worker.main() == 1
    monitor_worker.sd.Stream.assert_called_once()
    assert monitor_worker.sd.Stream.call_args.kwargs["blocksize"] == 64
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"event": "error", "message": "selected settings rejected"}
    failed.abort.assert_called_once()
    failed.close.assert_called_once()


def test_main_reports_device_error_and_closes_even_if_abort_fails(monkeypatch, capsys):
    configure_argv(monkeypatch)
    monkeypatch.setattr(monitor_worker, "_running", True)
    broken = Mock()
    broken.start.side_effect = RuntimeError("device busy")
    broken.abort.side_effect = RuntimeError("abort failed")
    monkeypatch.setattr(monitor_worker.sd, "Stream", Mock(return_value=broken))
    assert monitor_worker.main() == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["message"] == "device busy"
    broken.close.assert_called_once()


def test_callback_updates_levels_without_restarting_after_glitches(monkeypatch, capsys):
    configure_argv(monkeypatch)
    monkeypatch.setattr(monitor_worker, "_running", False)
    stream = Mock()

    def start():
        callback, input_data, output = monitor_worker.sd.Stream.call_args.kwargs['callback'], np.array([[0.6], [-0.6]], dtype=np.float32), np.empty((2, 2), dtype=np.float32)
        callback(input_data, output, 2, None, "glitch")
        callback(input_data, output, 2, None, "glitch")
        callback(input_data, output, 2, None, "glitch")
        callback(input_data, output, 2, None, "glitch")
        assert (np.max(np.abs(output)) <= 0.985) and (np.allclose(output[:, 0], output[:, 1]))

    stream.start.side_effect = start
    monkeypatch.setattr(monitor_worker.sd, "Stream", Mock(return_value=stream))
    assert (monitor_worker.main() == 0) and (monitor_worker._level['rms_db'] > -20) and (monitor_worker._level['clipping'] is False) and (monitor_worker._level['silent'] is False)
    capsys.readouterr()


def test_sustained_glitches_are_counted_without_restart_or_reconfiguration(monkeypatch):
    stats = {}
    callback = monitor_worker._audio_callback(1.0, sample_rate=48000, statistics=stats)
    input_data, output = np.zeros((128, 1), dtype=np.float32), np.empty((128, 2), dtype=np.float32)
    for _ in range(100):
        callback(input_data, output, 128, None, "underflow")
    assert stats["glitch_count"] == 100
    assert stats["callback_frames"] == 128


def test_compute_time_measures_callback_work_not_buffer_size(monkeypatch):
    clock = iter((10.0, 10.00025))
    monkeypatch.setattr(monitor_worker.time, "perf_counter", lambda: next(clock))
    stats = {}
    callback = monitor_worker._audio_callback(1, 44100, stats)
    callback(np.zeros((128, 1), np.float32), np.zeros((128, 2), np.float32), 128, None, None)
    assert stats["dsp_compute_ms"] == .25


def test_main_emits_level_while_running(monkeypatch, capsys):
    configure_argv(monkeypatch)
    monkeypatch.setattr(monitor_worker, "_running", True)

    class Event:
        def is_set(self): return False

        def clear(self): pass

        def set(self): pass

        def wait(self, _timeout):
            monitor_worker._running = False
            return False

    patch_attrs(monkeypatch, monitor_worker.threading, Event=Event, Thread=Mock())
    # Thread is mocked out above (same as the live-update reader), so the
    # separate reporter thread never actually drains this -- it stays off
    # the realtime pump loop deliberately, see _queue_report's docstring.
    monkeypatch.setattr(monitor_worker, "_report_queue", monitor_worker.queue.Queue(maxsize=1))
    stream = Mock()
    monkeypatch.setattr(monitor_worker.sd, "Stream", Mock(return_value=stream))
    assert monitor_worker.main() == 0
    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    assert events == ["stage", "stage", "stage", "started"]
    assert monitor_worker._report_queue.get_nowait()["event"] == "level"


def test_main_finalizer_suppresses_stream_cleanup_failure(monkeypatch, capsys):
    configure_argv(monkeypatch)
    monkeypatch.setattr(monitor_worker, "_running", True)
    stream = Mock()
    stream.start.side_effect = KeyboardInterrupt()
    stream.close.side_effect = RuntimeError("cleanup failed")
    monkeypatch.setattr(monitor_worker.sd, "Stream", Mock(return_value=stream))
    with contextlib.suppress(KeyboardInterrupt): monitor_worker.main()
    capsys.readouterr()


def test_script_entrypoint_registers_signals_and_exits(monkeypatch, tmp_path, capsys):
    configure_argv(monkeypatch)
    stream = Mock()

    def create_stream(**kwargs):
        def start():
            kwargs["callback"].__globals__["_running"] = False

        stream.start.side_effect = start
        return stream

    monkeypatch.setattr(monitor_worker.sd, "Stream", create_stream)
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", monitor_worker.sd)
    register = Mock()
    monkeypatch.setattr(monitor_worker.signal, "signal", register)
    with pytest.raises(SystemExit) as stopped: runpy.run_path(monitor_worker.__file__, run_name="__main__")
    assert (stopped.value.code == 0) and (register.call_count >= 1)
    capsys.readouterr()
