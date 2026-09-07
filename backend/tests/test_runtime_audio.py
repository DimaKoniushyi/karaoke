from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import config
from app.services import audio_service, monitor_worker, recording_service
from tests._shared import patch_attrs


def test_runtime_executable_prefers_packaged_sibling(monkeypatch, tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    executable, ffmpeg = backend / 'KaraokeBackend.exe', backend / 'ffmpeg.exe'
    executable.touch()
    ffmpeg.touch()
    monkeypatch.setattr(config.sys, "executable", str(executable))

    assert config.resolve_runtime_executable("ffmpeg") == str(ffmpeg)


def test_auto_input_does_not_promote_unreliable_wdm(monkeypatch):
    devices = [
        {"name": "USB Microphone", "hostapi": 0, "max_input_channels": 1},
        {"name": "USB Microphone", "hostapi": 1, "max_input_channels": 1},
    ]
    monkeypatch.setattr(audio_service.sd, "query_devices", lambda *_args, **_kwargs: devices)
    patch_attrs(monkeypatch, audio_service, _host_api_name=lambda device: 'MME' if device['hostapi'] == 0 else 'Windows WDM-KS')

    assert audio_service._low_latency_equivalent(0, "input") == 0


def test_duplex_output_uses_same_host_api(monkeypatch):
    devices = [
        {
            "name": "USB Microphone",
            "hostapi": 0,
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 44_100,
        },
        {
            "name": "USB Speakers",
            "hostapi": 1,
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 44_100,
        },
        {
            "name": "USB Speakers",
            "hostapi": 0,
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 44_100,
        },
    ]
    monkeypatch.setattr(audio_service.sd, "query_devices", lambda *_args, **_kwargs: devices)
    patch_attrs(monkeypatch, audio_service, _low_latency_equivalent=lambda *_args, **_kwargs: 1, _host_api_name=lambda device: 'Windows WASAPI' if device['hostapi'] == 1 else 'MME')

    assert audio_service._matching_output_for_input(0, None) == 2


def test_wasapi_has_no_host_neutral_fallback():
    candidate = monitor_worker._stream_candidate(
        {
            "sample_rate": 48_000,
            "output_channels": 2,
            "input_device_id": 1,
            "output_device_id": 2,
            "blocksize": 64,
            "wasapi_mode": "shared",
        }
    )

    assert (
        candidate["blocksize"] == 64
        and candidate["latency"] == 64 / 48_000
        and "extra_settings" in candidate
        and Path(config.FFMPEG_EXE).name.casefold() in {"ffmpeg", "ffmpeg.exe"}
    )


def test_recording_keeps_selected_microphone_monitor_and_buffer(monkeypatch):
    patch_attrs(monkeypatch, recording_service, _AUDIO_BACKEND_AVAILABLE=True, _capture_blocksize=lambda *_args: 64)
    factory = Mock()
    patch_attrs(monkeypatch, recording_service, RecordingSession=factory, _sessions={})
    patch_attrs(monkeypatch, recording_service.uuid, uuid4=lambda: SimpleNamespace(hex="session"))

    recording_service.start_recording('song', device_id=7, output_device_id=9, sample_rate=44_100, blocksize=64, monitoring_enabled=True)

    (_session_id, _song_id, input_id, output_id, rate, _channels, _gain, monitoring,
     _offset, _rate, frames, *_rest, latency) = factory.call_args.args
    assert (input_id, output_id, rate, frames, monitoring, latency) == (7, 9, 44100, 64, True, 64 / 44100)
