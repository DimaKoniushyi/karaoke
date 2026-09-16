from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from tools import acoustic_latency
from tools.acoustic_latency import detect_pulse


def test_consistent_acoustic_echoes_require_three_close_repeats():
    assert acoustic_latency.consistent_echoes([25.521, 25.375, 65.563]) == []
    assert acoustic_latency.consistent_echoes([25.5, 43.0, 65.0]) == []
    assert acoustic_latency.consistent_echoes([28.167, 41.917, 41.646]) == []
    assert acoustic_latency.consistent_echoes([28.1, 28.2, 28.0]) == [28.0, 28.1, 28.2]
    assert acoustic_latency.consistent_echoes([13.2, 13.3, 13.1, 13.2]) == [13.1, 13.2, 13.2, 13.3]


def test_extended_acoustic_probe_places_pulses_throughout_duration():
    positions = acoustic_latency.probe_positions(48_000, 10)
    assert len(positions) >= 20
    assert positions[0] == 14_400
    assert positions[-1] > 9 * 48_000


def test_detect_pulse_returns_acoustic_delay_for_a_clear_echo():
    rate = 48_000
    rng = np.random.default_rng(7)
    pulse = rng.choice([-1.0, 1.0], 256).astype(np.float32)
    capture = rng.normal(0, .002, rate // 2).astype(np.float32)
    capture[rate // 10 + 960:rate // 10 + 960 + len(pulse)] += pulse * .15
    result = detect_pulse(pulse, capture, rate, output_time=.1, capture_start_time=0)
    assert result is not None
    assert abs(result["latency_ms"] - 20.0) < .03
    assert result["correlation"] > .8


def test_detect_pulse_accepts_acoustic_polarity_inversion():
    rate = 48_000
    pulse = np.random.default_rng(7).choice([-1.0, 1.0], 256).astype(np.float32)
    capture = np.zeros(rate // 2, dtype=np.float32)
    capture[rate // 10 + 960:rate // 10 + 960 + len(pulse)] = -pulse * .15
    result = detect_pulse(pulse, capture, rate, output_time=.1, capture_start_time=0)
    assert result is not None
    assert abs(result["latency_ms"] - 20.0) < .03
    assert result["correlation"] > .8


def test_acoustic_probe_pulse_uses_twice_the_previous_level():
    pulse = acoustic_latency.make_probe_pulse()
    assert len(pulse) == 256
    assert .22 <= float(np.max(np.abs(pulse))) <= .24


def test_detect_pulse_rejects_noise_without_echo():
    rate = 48_000
    pulse = np.random.default_rng(3).choice([-1.0, 1.0], 256).astype(np.float32)
    noise = np.random.default_rng(4).normal(0, .002, rate // 2).astype(np.float32)
    assert detect_pulse(pulse, noise, rate, output_time=.1, capture_start_time=0) is None


def test_start_dev_runs_the_bounded_acoustic_check_automatically():
    source = (Path(__file__).resolve().parents[2] / "start-dev.bat").read_text(encoding="utf-8-sig")
    assert source.index("backend\\tools\\acoustic_latency.py") > source.index('if "%PREPARE_ONLY%"=="1" (')
    assert source.index("backend\\tools\\acoustic_latency.py") < source.index("Starting A^&D Voice")
    assert "fully shared WASAPI acoustic baseline" in source


def test_acoustic_probe_uses_only_fully_shared_wasapi(monkeypatch):
    class StopProbe(Exception):
        pass

    observed = {}
    monkeypatch.setattr(acoustic_latency.sd, "query_devices", lambda index: {
        "name": str(index), "default_samplerate": 48_000,
        "max_output_channels": 2,
    })

    def wasapi_settings(*, exclusive):
        return {"exclusive": exclusive}

    def stream(**options):
        observed.update(options)
        raise StopProbe

    monkeypatch.setattr(acoustic_latency.sd, "WasapiSettings", wasapi_settings)
    monkeypatch.setattr(acoustic_latency.sd, "Stream", stream)
    with pytest.raises(StopProbe):
        acoustic_latency.run_probe(1, 2)
    assert observed["extra_settings"] == ({"exclusive": False}, {"exclusive": False})


def test_acoustic_probe_can_use_wdm_ks_without_wasapi_settings(monkeypatch):
    class StopProbe(Exception):
        pass

    observed = {}
    monkeypatch.setattr(acoustic_latency.sd, "query_devices", lambda index: {
        "name": str(index), "default_samplerate": 48_000,
        "max_output_channels": 2,
    })

    def stream(**options):
        observed.update(options)
        raise StopProbe

    monkeypatch.setattr(acoustic_latency.sd, "Stream", stream)
    with pytest.raises(StopProbe):
        acoustic_latency.run_probe(50, 49, transport="wdm-ks")
    assert "extra_settings" not in observed


def test_acoustic_probe_can_request_exclusive_wasapi_on_both_endpoints(monkeypatch):
    class StopProbe(Exception):
        pass

    observed = {}
    monkeypatch.setattr(acoustic_latency.sd, "query_devices", lambda index: {
        "name": str(index), "default_samplerate": 48_000,
        "max_output_channels": 2,
    })
    monkeypatch.setattr(acoustic_latency.sd, "WasapiSettings",
                        lambda *, exclusive: {"exclusive": exclusive})

    def stream(**options):
        observed.update(options)
        raise StopProbe

    monkeypatch.setattr(acoustic_latency.sd, "Stream", stream)
    with pytest.raises(StopProbe):
        acoustic_latency.run_probe(39, 30, transport="wasapi-exclusive")
    assert observed["extra_settings"] == ({"exclusive": True}, {"exclusive": True})


def test_acoustic_probe_can_compare_exclusive_capture_with_shared_render(monkeypatch):
    class StopProbe(Exception):
        pass

    observed = {}
    monkeypatch.setattr(acoustic_latency.sd, "query_devices", lambda _index: {
        "name": "headset", "default_samplerate": 48_000,
        "max_output_channels": 2,
    })
    monkeypatch.setattr(acoustic_latency.sd, "WasapiSettings",
                        lambda *, exclusive: {"exclusive": exclusive})

    def stream(**options):
        observed.update(options)
        raise StopProbe

    monkeypatch.setattr(acoustic_latency.sd, "Stream", stream)
    with pytest.raises(StopProbe):
        acoustic_latency.run_probe(39, 30, transport="wasapi-hybrid")
    assert observed["extra_settings"] == ({"exclusive": True}, {"exclusive": False})


def test_portaudio_probe_passes_requested_blocksize_to_device(monkeypatch):
    class StopProbe(Exception):
        pass

    monkeypatch.setattr(acoustic_latency.sd, "query_devices", lambda _index: {
        "name": "headset", "default_samplerate": 48_000,
        "max_output_channels": 2,
    })
    monkeypatch.setattr(acoustic_latency.sd, "WasapiSettings",
                        lambda *, exclusive: {"exclusive": exclusive})

    def stream(**options):
        assert options["blocksize"] == 96
        raise StopProbe

    monkeypatch.setattr(acoustic_latency.sd, "Stream", stream)
    with pytest.raises(StopProbe):
        acoustic_latency.run_probe(39, 30, transport="wasapi-exclusive", blocksize=96)


def test_portaudio_exclusive_monitor_uses_two_echoes_in_the_same_capture_timeline(monkeypatch):
    rate, delay = 48_000, 960
    monkeypatch.setattr(acoustic_latency.sd, "query_devices", lambda _index: {
        "name": "headset", "default_samplerate": rate, "max_output_channels": 1,
    })

    class FakeStream:
        def __init__(self, **options):
            self.callback = options["callback"]
            self.history = np.zeros(0, dtype=np.float32)
            self.position = 0

        def __enter__(self):
            for _ in range(150):
                frames = 480
                heard = np.zeros((frames, 1), dtype=np.float32)
                for index in range(frames):
                    previous = self.position + index - delay
                    if 0 <= previous < len(self.history):
                        heard[index, 0] = self.history[previous]
                output = np.zeros_like(heard)
                timing = SimpleNamespace(inputBufferAdcTime=self.position / rate,
                                         outputBufferDacTime=self.position / rate)
                self.callback(heard, output, frames, timing, None)
                self.history = np.concatenate((self.history, output[:, 0]))
                self.position += frames
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(acoustic_latency.sd, "Stream", FakeStream)
    monkeypatch.setattr(acoustic_latency.time, "sleep", lambda _duration: None)
    result = acoustic_latency.run_portaudio_monitor_loopback(39, 30, transport="wasapi-exclusive")
    assert result["status"] == "measured"
    assert result["acoustic_monitor_ms"] == pytest.approx(20.0, abs=.03)
    assert "acoustic_roundtrip_ms" not in result["baseline"]
    assert result["baseline"]["reported_clock_roundtrip_ms"] == pytest.approx(20.0, abs=.03)


def test_portaudio_monitor_probe_can_exercise_production_effect_callback(monkeypatch):
    from app.services import monitor_worker

    class StopProbe(Exception):
        pass

    monkeypatch.setattr(acoustic_latency, "run_probe", lambda *_args, **_kwargs: {
        "status": "measured", "sample_timeline_roundtrip_ms": 20.0,
    })
    monkeypatch.setattr(acoustic_latency.sd, "query_devices", lambda _index: {
        "name": "headset", "default_samplerate": 48_000,
        "max_output_channels": 2,
    })
    observed = []

    def effects_callback(_gain, rate, _stats):
        observed.append((rate, monitor_worker._live_params["reverb"]))
        return lambda source, output, *_args: output.__setitem__(slice(None), source)

    monkeypatch.setattr(monitor_worker, "_audio_callback", effects_callback)
    monkeypatch.setattr(acoustic_latency.sd, "Stream", lambda **_opts: (_ for _ in ()).throw(StopProbe()))
    with pytest.raises(StopProbe):
        acoustic_latency.run_portaudio_monitor_loopback(
            39, 30, transport="wasapi-exclusive", blocksize=16,
            effects={"reverb": 1.0},
        )
    assert observed == [(48_000, 1.0)]


def test_native_shared_probe_measures_returned_pulses_in_the_production_sample_timeline(
        monkeypatch):
    rate = 48_000
    delay = 960
    class FakeStream:
        def __init__(self, options, stats):
            assert options["input_exclusive"] is False
            self.info = type("Info", (), {"sample_rate": rate})()
            self.callback = None
            self.emitted = np.zeros(0, dtype=np.float32)
            self.position = 0
            self.stats = stats

        def start(self, callback):
            self.callback = callback

        def pump(self):
            frames = 480
            source = np.zeros((frames, 1), dtype=np.float32)
            for index in range(frames):
                heard = self.position + index - delay
                if 0 <= heard < len(self.emitted):
                    source[index, 0] = self.emitted[heard]
            output = np.zeros_like(source)
            self.callback(source, output, frames, None, None)
            self.emitted = np.concatenate((self.emitted, output[:, 0]))
            self.position += frames
            self.stats["stream_latency_ms"] = 14.5 if self.position < rate else 19.5

        def close(self):
            pass

    fake = FakeStream
    monkeypatch.setattr("app.services.native_wasapi.NativeWasapiStream", fake)
    clock = [0.0]
    def monotonic():
        clock[0] += .01
        return clock[0]
    monkeypatch.setattr(acoustic_latency.time, "monotonic", monotonic)
    result = acoustic_latency.run_native_shared_probe("microphone", "speakers", duration=1.5)
    assert result["status"] == "measured"
    assert result["transport"] == "native-wasapi-fully-shared-test"
    assert result["acoustic_roundtrip_ms"] == 20.0
    assert result["stream_latency_ms"] == 19.5
    assert result["stream_latency_p50_ms"] == 14.5
    assert result["unreported_latency_ms"] == 5.5


def test_automatic_acoustic_probe_uses_native_shared_stream(monkeypatch, capsys):
    monkeypatch.setattr(acoustic_latency.sd, "query_hostapis", lambda: [{
        "name": "Windows WASAPI", "default_input_device": 1, "default_output_device": 2,
    }])
    monkeypatch.setattr(acoustic_latency.sd, "query_devices", lambda index: {"name": f"device-{index}"})
    observed = []
    monkeypatch.setattr(acoustic_latency, "run_native_shared_probe", lambda *args: (
        observed.append(args) or {"status": "unavailable", "reason": "test"}))
    monkeypatch.setattr(acoustic_latency, "run_probe", lambda *_: pytest.fail("PortAudio probe used"))
    monkeypatch.setattr("sys.argv", ["acoustic_latency.py"])
    acoustic_latency.main()
    assert observed == [("device-1", "device-2")]
    assert "ACOUSTIC_LATENCY_DIAGNOSTICS" in capsys.readouterr().out


def test_native_probe_reports_render_activity_when_no_acoustic_echo_is_detected(monkeypatch):
    class SilentMicStream:
        def __init__(self, _options, stats):
            self.info = type("Info", (), {"sample_rate": 48_000})()
            self.stats = stats
        def start(self, callback):
            self.callback = callback
        def pump(self):
            self.callback(np.zeros((480, 1), dtype=np.float32),
                          np.zeros((480, 1), dtype=np.float32), 480, None, None)
            self.stats["captured_frames"] = self.stats.get("captured_frames", 0) + 480
            self.stats["rendered_frames"] = self.stats.get("rendered_frames", 0) + 480
            self.stats["output_clock_lead_ms"] = 24.0
            self.stats["queue_ms"] = 1.0
        def diagnostics(self):
            return {"minimum_period_latency_ms": 13.0,
                    "negotiated_period_latency_ms": 13.0,
                    "input_period_frames": 144, "output_period_frames": 480,
                    "input_exclusive": False, "output_exclusive": False}
        def close(self):
            pass
    monkeypatch.setattr("app.services.native_wasapi.NativeWasapiStream", SilentMicStream)
    clock = [0.0]
    def monotonic():
        clock[0] += .01
        return clock[0]
    monkeypatch.setattr(acoustic_latency.time, "monotonic", monotonic)
    result = acoustic_latency.run_native_shared_probe("microphone", "speakers", duration=1.5)
    assert result["status"] == "unavailable"
    assert result["captured_frames"] > 0
    assert result["rendered_frames"] > 0
    assert result["microphone_peak"] == 0.0
    assert result["minimum_period_latency_ms"] == 13.0
    assert result["negotiated_period_latency_ms"] == 13.0
    assert result["output_clock_lead_ms"] == 24.0
    assert result["queue_ms"] == 1.0
    assert result["input_exclusive"] is False
    assert result["output_exclusive"] is False


def test_native_probe_can_compare_hybrid_capture_without_changing_shared_default(monkeypatch):
    class StopProbe(Exception):
        pass
    def stream(options, _statistics):
        assert options["input_exclusive"] is True
        raise StopProbe
    monkeypatch.setattr("app.services.native_wasapi.NativeWasapiStream", stream)
    with pytest.raises(StopProbe):
        acoustic_latency.run_native_shared_probe("microphone", "speakers", input_exclusive=True)


def test_hybrid_probe_can_sweep_driver_buffer_period_without_changing_default(monkeypatch):
    class StopProbe(Exception):
        pass

    def stream(options, _statistics):
        assert options["blocksize"] == 480
        raise StopProbe

    monkeypatch.setattr("app.services.native_wasapi.NativeWasapiStream", stream)
    with pytest.raises(StopProbe):
        acoustic_latency.run_native_shared_probe(
            "microphone", "speakers", input_exclusive=True,
            blocksize=480,
        )


def test_monitor_loopback_can_measure_exclusive_input_with_shared_output(monkeypatch):
    observed = []
    monkeypatch.setattr(acoustic_latency, "run_native_shared_probe", lambda *_args, **kwargs: (
        observed.append(kwargs.get("input_exclusive")) or
        {"status": "measured", "acoustic_roundtrip_ms": 20.0}
    ))

    class StopProbe(Exception):
        pass

    def stream(options, _statistics):
        assert options["input_exclusive"] is True
        observed.append(options["input_exclusive"])
        raise StopProbe

    monkeypatch.setattr("app.services.native_wasapi.NativeWasapiStream", stream)
    with pytest.raises(StopProbe):
        acoustic_latency.run_native_monitor_loopback(
            "microphone", "speakers", input_exclusive=True
        )
    assert observed == [True, True]


@pytest.mark.parametrize(
    ("dropped", "hardware_roundtrip", "expected"),
    [(0, 960, "measured"), (0, 1440, "measured"), (16, 960, "unavailable")],
)
def test_native_monitor_loopback_measures_a_second_acoustic_echo(
    monkeypatch, dropped, hardware_roundtrip, expected,
):
    rate = 48_000

    class FakeStream:
        def __init__(self, options, stats):
            assert options["input_exclusive"] is False
            self.info = type("Info", (), {"sample_rate": rate})()
            self.stats = stats
            self.emitted = np.zeros(0, dtype=np.float32)
            self.position = 0

        def start(self, callback):
            self.callback = callback

        def pump(self):
            frames = 480
            source = np.zeros((frames, 1), dtype=np.float32)
            for index in range(frames):
                heard = self.position + index - hardware_roundtrip
                if 0 <= heard < len(self.emitted):
                    source[index, 0] = self.emitted[heard]
            output = np.zeros_like(source)
            self.callback(source, output, frames, None, None)
            self.emitted = np.concatenate((self.emitted, output[:, 0]))
            self.position += frames
            self.stats["queue_dropped_frames"] = dropped

        def close(self):
            pass

    monkeypatch.setattr("app.services.native_wasapi.NativeWasapiStream", FakeStream)
    monkeypatch.setattr(acoustic_latency, "run_native_shared_probe", lambda *_args, **_kwargs: {
        "status": "measured", "acoustic_roundtrip_ms": 20.0,
    })
    clock = [0.0]
    def monotonic():
        clock[0] += .01
        return clock[0]
    monkeypatch.setattr(acoustic_latency.time, "monotonic", monotonic)

    result = acoustic_latency.run_native_monitor_loopback(
        "microphone", "speakers", duration=1.5
    )

    assert result["status"] == expected
    if expected == "measured":
        assert abs(result["acoustic_monitor_ms"] - hardware_roundtrip / 48) <= .03
        assert result["acoustic_device_roundtrip_ms"] == 20.0
        assert result["input_exclusive"] is False


def test_native_monitor_loopback_rejects_first_echo_without_return(monkeypatch):
    rate, delay = 48_000, 960
    direct_positions = [int(rate * second) for second in (.3, .7, 1.1)]

    class FirstEchoOnly:
        def __init__(self, _options, stats):
            self.info = type("Info", (), {"sample_rate": rate})()
            self.stats = stats
            self.position = 0
            self.emitted = np.zeros(0, dtype=np.float32)

        def start(self, callback):
            self.callback = callback

        def pump(self):
            frames = 480
            source = np.zeros((frames, 1), dtype=np.float32)
            for index in range(frames):
                heard = self.position + index - delay
                if any(start <= heard < start + 256 for start in direct_positions):
                    source[index, 0] = self.emitted[heard]
            output = np.zeros_like(source)
            self.callback(source, output, frames, None, None)
            self.emitted = np.concatenate((self.emitted, output[:, 0]))
            self.position += frames

        def close(self):
            pass

    monkeypatch.setattr("app.services.native_wasapi.NativeWasapiStream", FirstEchoOnly)
    monkeypatch.setattr(acoustic_latency, "run_native_shared_probe", lambda *_args, **_kwargs: {
        "status": "measured", "acoustic_roundtrip_ms": 20.0,
    })
    clock = [0.0]
    def monotonic():
        clock[0] += .01
        return clock[0]
    monkeypatch.setattr(acoustic_latency.time, "monotonic", monotonic)

    result = acoustic_latency.run_native_monitor_loopback(
        "microphone", "speakers", duration=1.5
    )

    assert result["status"] == "unavailable"
    assert result["reason"] == "second_echo_not_reliably_detected"


def test_native_monitor_loopback_includes_effect_processing_delay(monkeypatch):
    from app.services import monitor_worker

    rate, hardware_roundtrip, effect_delay = 48_000, 960, 288

    class FakeStream:
        def __init__(self, options, stats):
            assert options["input_exclusive"] is False
            self.info = type("Info", (), {"sample_rate": rate})()
            self.stats = stats
            self.emitted = np.zeros(0, dtype=np.float32)
            self.position = 0

        def start(self, callback):
            self.callback = callback

        def pump(self):
            frames = 480
            source = np.zeros((frames, 1), dtype=np.float32)
            for index in range(frames):
                heard = self.position + index - hardware_roundtrip
                if 0 <= heard < len(self.emitted):
                    source[index, 0] = self.emitted[heard]
            output = np.zeros_like(source)
            self.callback(source, output, frames, None, None)
            self.emitted = np.concatenate((self.emitted, output[:, 0]))
            self.position += frames

        def close(self):
            pass

    history = np.zeros(effect_delay, dtype=np.float32)
    def fake_effect_callback(_gain, _rate, _stats):
        def process(source, output, frames, _clock, _status):
            nonlocal history
            values = np.concatenate((history, source[:, 0]))
            output[:, 0] = values[:frames]
            history = values[frames:]
        return process

    monkeypatch.setattr("app.services.native_wasapi.NativeWasapiStream", FakeStream)
    monkeypatch.setattr(monitor_worker, "_audio_callback", fake_effect_callback)
    monkeypatch.setattr(acoustic_latency, "run_native_shared_probe", lambda *_args, **_kwargs: {
        "status": "measured", "acoustic_roundtrip_ms": 20.0,
    })
    clock = [0.0]
    def monotonic():
        clock[0] += .01
        return clock[0]
    monkeypatch.setattr(acoustic_latency.time, "monotonic", monotonic)

    result = acoustic_latency.run_native_monitor_loopback(
        "microphone", "speakers", duration=1.5,
        effects={"octave": -.5, "reverb": 1.0},
    )

    assert result["status"] == "measured"
    assert abs(result["acoustic_monitor_ms"] - 26.0) <= .03
    assert result["effects"] == {"octave": -.5, "reverb": 1.0}
