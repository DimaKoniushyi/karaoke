from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from app.services import monitor_worker, native_wasapi
from tools import probe_wasapi


def test_probe_cli_accepts_the_production_low_latency_buffer():
    source = Path(probe_wasapi.__file__).read_text(encoding="utf-8")
    assert "choices=(16, 32, 64, 128, 256, 512, 1024, 2048)" in source
    assert "1024, 2048), default=16" in source


def test_native_probe_uses_actual_rate_mutes_dsp_and_closes(monkeypatch):
    stats, callbacks = {}, []
    stream = SimpleNamespace(info=SimpleNamespace(sample_rate=44100), close=Mock(),
                             diagnostics=lambda: {"sample_rate": 44100})
    def create(options, statistics):
        assert options == {"input_device_name": "input", "output_device_name": "output", "blocksize": 64}
        stats["target"] = statistics
        return stream
    stream.start = callbacks.append
    def pump():
        target = stats["target"]
        target.update(rendered_frames=target.get("rendered_frames", 0) + 64,
                      program_residence_ms=.5, stream_latency_ms=30, queue_ms=0)
        output = np.ones((64, 1))
        callbacks[0](output.copy(), output, 64, None, None)
        assert np.all(output == 0)
    stream.pump = pump
    monkeypatch.setattr(native_wasapi, "NativeWasapiStream", create)
    monkeypatch.setattr(probe_wasapi.sd, "query_devices", lambda index: {"name": "input" if index == 1 else "output"})
    times = iter([0, .1, .3, .4, .5, 2])
    monkeypatch.setattr(probe_wasapi.time, "monotonic", lambda: next(times))
    factory = Mock(return_value=Mock())
    monkeypatch.setattr(monitor_worker, "_audio_callback", factory)
    result = probe_wasapi.probe_native({"input": 1, "output": 2, "blocksize": 64, "duration": 1, "dsp": True})
    factory.assert_called_once_with(2.0, 44100, stats["target"])
    assert result["timings"]["program_residence_ms"]["median"] == .5
    assert result["round_trip_latency_ms"] is None
    stream.close.assert_called_once()


def test_native_probe_closes_if_dsp_initialization_fails(monkeypatch):
    stream = SimpleNamespace(info=SimpleNamespace(sample_rate=44100), close=Mock())
    monkeypatch.setattr(native_wasapi, "NativeWasapiStream", lambda *_: stream)
    monkeypatch.setattr(probe_wasapi.sd, "query_devices", lambda _: {"name": "device"})
    monkeypatch.setattr(monitor_worker, "_audio_callback", Mock(side_effect=RuntimeError("DSP unavailable")))
    with pytest.raises(RuntimeError, match="DSP unavailable"):
        probe_wasapi.probe_native({"input": 1, "output": 2, "blocksize": 64, "duration": 1, "dsp": True})
    stream.close.assert_called_once()


def test_native_probe_applies_and_restores_the_requested_effect_stress_profile(monkeypatch):
    original = dict(monitor_worker._live_params)
    observed = {}
    target_stats = {}
    stream = SimpleNamespace(
        info=SimpleNamespace(sample_rate=48_000),
        diagnostics=lambda: {"sample_rate": 48_000},
        close=Mock(),
    )

    def start(_callback):
        observed.update(monitor_worker._live_params)

    def pump():
        statistics = target_stats["target"]
        statistics["rendered_frames"] = statistics.get("rendered_frames", 0) + 64

    stream.start, stream.pump = start, pump
    monkeypatch.setattr(
        native_wasapi, "NativeWasapiStream",
        lambda _options, statistics: target_stats.update(target=statistics) or stream,
    )
    monkeypatch.setattr(probe_wasapi.sd, "query_devices", lambda index: {"name": f"device-{index}"})
    monkeypatch.setattr(monitor_worker, "_audio_callback", Mock(return_value=lambda *_args: None))
    times = iter([0, .1, .3, .4, .5, 2])
    monkeypatch.setattr(probe_wasapi.time, "monotonic", lambda: next(times))
    effects = {
        "volume": 2.0, "reverb": 1.0, "echo": 1.0, "delay": 1.0,
        "noise_suppression": 1.0, "octave": -0.5,
    }

    probe_wasapi.probe_native({
        "input": 1, "output": 2, "blocksize": 64, "duration": 1,
        "dsp": True, "effects": effects,
    })

    assert all(observed[name] == value for name, value in effects.items())
    assert monitor_worker._live_params == original
    stream.close.assert_called_once()


def test_native_probe_exercises_silent_cpp_raw_bypass(monkeypatch):
    statistics = {}
    stream = SimpleNamespace(
        info=SimpleNamespace(sample_rate=16_000),
        diagnostics=lambda: {"sample_rate": 16_000},
        start=Mock(), set_raw=Mock(), close=Mock(),
    )

    def create(options, target):
        assert options["gain"] == 0.0
        statistics["target"] = target
        return stream

    def pump():
        target = statistics["target"]
        target["rendered_frames"] = target.get("rendered_frames", 0) + 48

    stream.pump = pump
    monkeypatch.setattr(native_wasapi, "NativeWasapiStream", create)
    monkeypatch.setattr(probe_wasapi.sd, "query_devices", lambda index: {"name": f"device-{index}"})
    times = iter([0, .1, .3, .4, .5, 2])
    monkeypatch.setattr(probe_wasapi.time, "monotonic", lambda: next(times))

    probe_wasapi.probe_native({
        "input": 1, "output": 2, "blocksize": 16, "duration": 1,
        "dsp": False, "raw": True,
    })

    stream.set_raw.assert_called_once_with(True)
    stream.close.assert_called_once()


def test_native_probe_can_measure_the_production_hybrid_transport(monkeypatch):
    observed = {}

    def create(options, _statistics):
        observed.update(options)
        raise RuntimeError("probe stopped")

    monkeypatch.setattr(native_wasapi, "NativeWasapiStream", create)
    monkeypatch.setattr(probe_wasapi.sd, "query_devices", lambda index: {"name": f"device-{index}"})
    with pytest.raises(RuntimeError, match="probe stopped"):
        probe_wasapi.probe_native({
            "input": 1, "output": 2, "blocksize": 16, "duration": 1,
            "dsp": False, "input_exclusive": True,
        })

    assert observed["input_exclusive"] is True


def test_plain_host_probe_does_not_attach_wasapi_stream_settings(monkeypatch):
    observed = {}
    stream = SimpleNamespace(
        latency=(0.001, 0.001),
        start=Mock(), abort=Mock(), close=Mock(),
    )

    def create(**options):
        observed.update(options)
        return stream

    monkeypatch.setattr(probe_wasapi.sd, "Stream", create)
    monkeypatch.setattr(
        probe_wasapi.sd, "WasapiSettings",
        Mock(side_effect=AssertionError("plain/WDM-KS must not receive WASAPI settings")),
    )
    monkeypatch.setattr(probe_wasapi, "host_buffer_frames", lambda _stream: {})
    monkeypatch.setattr(probe_wasapi.time, "sleep", lambda _duration: None)

    result = probe_wasapi.probe({
        "input": 1, "output": 2, "rate": 48_000, "mode": "plain",
        "kind": "duplex", "blocksize": 16, "latency": 16 / 48_000,
        "duration": 0, "dsp": False,
    })

    assert "extra_settings" not in observed
    assert result["reported_latency_seconds"] == (0.001, 0.001)
    stream.close.assert_called_once()


def test_split_probe_runs_requested_effects_and_restores_live_settings(monkeypatch):
    from app.services import monitor_worker

    original = monitor_worker._live_params
    observed = {}
    monkeypatch.setattr(monitor_worker, "_live_params", {**original, "octave": 0.0})

    def make_callback(*_args):
        def callback(_source, _output, _frames, _clocks, _status):
            observed["octave"] = monitor_worker._live_params["octave"]
        return callback

    class FakeStream:
        latency = (0.003, 0.003)
        input = output = object()
        def __init__(self, _sd, _candidate, callback, _stats, _failed):
            self.callback = callback
        def start(self):
            self.callback(np.zeros((128, 1), dtype=np.float32),
                          np.zeros((128, 2), dtype=np.float32), 128, None, None)
        def abort(self):
            pass
        def close(self):
            pass

    monkeypatch.setattr(monitor_worker, "_audio_callback", make_callback)
    monkeypatch.setattr(probe_wasapi, "WasapiMonitorStream", FakeStream)
    monkeypatch.setattr(probe_wasapi.sd, "WasapiSettings", lambda **_kwargs: object())
    monkeypatch.setattr(probe_wasapi, "host_buffer_frames", lambda _stream: {})
    monkeypatch.setattr(probe_wasapi.time, "sleep", lambda _duration: None)
    probe_wasapi.probe({"kind": "split", "mode": "exclusive", "input": 1,
                       "output": 2, "rate": 48_000, "blocksize": 128,
                       "latency": 128 / 48_000, "duration": 0, "dsp": True,
                       "effects": {"octave": -0.5}})
    assert observed["octave"] == -0.5
    assert monitor_worker._live_params["octave"] == 0.0
