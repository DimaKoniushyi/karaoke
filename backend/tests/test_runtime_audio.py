from pathlib import Path

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
    candidates = monitor_worker._stream_candidates(
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
        candidates[0]["blocksize"] == 64
        and candidates[0]["latency"] == 64 / 48_000
        and "extra_settings" in candidates[0]
        and len(candidates) == 1
        and Path(config.FFMPEG_EXE).name.casefold() in {"ffmpeg", "ffmpeg.exe"}
    )


def test_recording_keeps_selected_microphone_monitor_and_buffer(monkeypatch):
    patch_attrs(monkeypatch, recording_service.sd, query_devices=lambda *_args, **_kwargs: {'default_samplerate': 48000})
    attempts = recording_service._capture_attempts(7, 9, 44_100, 64, True)

    assert attempts == [(7, 9, 44100, 64, True, 64 / 44100)]
