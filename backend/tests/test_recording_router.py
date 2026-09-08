from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

import models
import schemas
from app.routers import recording
from tests._shared import assert_http_status, patch_attrs, patch_many, raises


def audio_settings(**changes):
    values = {
        "input_device_id": 1,
        "output_device_id": 2,
        "volume": 1.0,
        "audio_driver": "auto",
        "asio_driver_name": None,
        "buffer_size": 64,
        "latency_ms": 50,
        "monitoring_enabled": False,
        "reverb": 0.1,
        "echo": 0.2,
        "delay": 0.3,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def start_body(): return schemas.RecordingStartRequest(song_id='song', position_sec=2, music_volume=0.8, microphone_volume=1.5, reverb=0.4, echo=0.5, delay=0.6)


def test_session_state_and_monitor_restore_translate_failures(monkeypatch):
    action = Mock()
    assert recording._change_session_state("session", action, "paused") == {"status": "paused"}
    action.side_effect = KeyError("missing")
    assert_http_status(404, lambda: recording._change_session_state("session", action, "paused"))

    database, current = Mock(), audio_settings()
    monkeypatch.setattr(recording.audio_service, "get_settings", Mock(return_value=current))
    configure, stop = Mock(), Mock()
    patch_attrs(monkeypatch, recording.audio_service, configure_monitoring=configure, stop_monitoring=stop)
    recording._restore_monitoring(database)
    configure.assert_called_once_with(current, relay_needed=False)
    configure.side_effect = RuntimeError("device unavailable")
    recording._restore_monitoring(database)
    stop.assert_called_once_with()


def test_recording_monitor_configuration_handles_auto_and_temporary_asio(monkeypatch):
    stop, configure = Mock(), Mock()
    patch_attrs(monkeypatch, recording.audio_service, stop_monitoring=stop, configure_monitoring=configure)
    body = start_body()
    assert (recording._configure_recording_monitor(audio_settings(), body) is True) and (recording._configure_recording_monitor(audio_settings(audio_driver='asio', monitoring_enabled=False), body) is True)

    current = audio_settings(audio_driver="asio", monitoring_enabled=True)
    original = vars(current).copy()
    assert recording._configure_recording_monitor(current, body) is True
    passed = configure.call_args.args[0]
    assert passed is not current  # a detached snapshot, never the live settings row
    assert (passed.monitoring_enabled, passed.volume, passed.reverb, passed.echo, passed.delay, passed.octave) == (
        True, body.microphone_volume, body.reverb, body.echo, body.delay, body.octave
    )
    assert vars(current) == original

    configure.side_effect = RuntimeError("ASIO failed")
    raises(RuntimeError, lambda: recording._configure_recording_monitor(current, body), match='ASIO failed')
    assert vars(current) == original


def test_windows_recording_keeps_native_shared_monitor_instead_of_portaudio_duplex(monkeypatch):
    stop, configure = Mock(), Mock()
    patch_attrs(monkeypatch, recording.audio_service, stop_monitoring=stop, configure_monitoring=configure)
    body = start_body()
    current = audio_settings(audio_driver="auto", monitoring_enabled=True)

    assert recording._configure_recording_monitor(current, body) is True
    configured = configure.call_args.args[0]
    assert configured.monitoring_enabled is True
    assert configured.volume == body.microphone_volume
    stop.assert_not_called()


def test_start_recording_builds_session_without_driver_latency_adjustment(monkeypatch):
    database, body, song, settings = Mock(), start_body(), SimpleNamespace(id='song'), audio_settings(monitoring_enabled=True)
    patch_many(monkeypatch, (recording.repositories, "get_song", Mock(return_value=song)), (recording.audio_service, "get_settings", Mock(return_value=settings)), (recording, "_configure_recording_monitor", Mock(return_value=True)))
    patch_attrs(monkeypatch, recording.audio_service, preferred_input_device=Mock(return_value=3), preferred_output_device=Mock(return_value=4), preferred_sample_rate=Mock(return_value=48000))
    start = Mock(return_value="session")
    monkeypatch.setattr(recording.audio_service, "recording_monitor_mode", Mock(return_value="shared"))
    monkeypatch.setattr(recording.recording_service, "start_recording", start)

    result = recording.start_recording(body, database)

    assert result.recording_session_id == "session"
    start.assert_called_once_with(
        song_id="song",
        device_id=3,
        output_device_id=4,
        sample_rate=48_000,
        gain=1.5,
        monitoring_enabled=False,
        playback_offset_sec=2,
        playback_rate=1,
        blocksize=64,
        music_gain=0.8,
            effects={"reverb": 0.4, "echo": 0.5, "delay": 0.6, "octave": 0},
            noise_suppression=0.35,
            monitor_owner="native-monitor",
            monitor_mode=None,
        )


def test_room_recording_does_not_open_a_second_live_monitor(monkeypatch):
    database, body, song, settings = Mock(), start_body(), SimpleNamespace(id="song"), audio_settings(monitoring_enabled=True)
    body.room_mode = True
    stop_monitoring = Mock()
    configure = Mock()
    start = Mock(return_value="session")
    patch_many(
        monkeypatch,
        (recording.repositories, "get_song", Mock(return_value=song)),
        (recording.audio_service, "get_settings", Mock(return_value=settings)),
        (recording.audio_service, "stop_monitoring", stop_monitoring),
        (recording, "_configure_recording_monitor", configure),
        (recording.recording_service, "start_recording", start),
    )
    patch_attrs(
        monkeypatch,
        recording.audio_service,
        preferred_input_device=Mock(return_value=3),
        preferred_output_device=Mock(return_value=4),
        preferred_sample_rate=Mock(return_value=44_100),
    )

    recording.start_recording(body, database)

    stop_monitoring.assert_called_once_with()
    configure.assert_not_called()
    assert start.call_args.kwargs["monitoring_enabled"] is False


def test_prepare_room_voice_relay_opens_the_relay_when_monitoring_is_on(monkeypatch):
    # Breaks the cold-start cycle: the frontend used to only ever send
    # voice_relay=true once it already knew the relay worked, but the relay
    # only ever opened as a side effect of receiving voice_relay=true.
    database, settings = Mock(), audio_settings(monitoring_enabled=True)
    configure = Mock()
    patch_many(
        monkeypatch,
        (recording.audio_service, "get_settings", Mock(return_value=settings)),
        (recording.audio_service, "configure_monitoring", configure),
    )

    assert recording.prepare_room_voice_relay(database) == {"relay_available": True}
    configure.assert_called_once_with(settings, relay_needed=True)


def test_prepare_room_voice_relay_reports_unavailable_when_monitoring_is_off(monkeypatch):
    database, settings = Mock(), audio_settings(monitoring_enabled=False)
    stop_monitoring = Mock()
    patch_many(
        monkeypatch,
        (recording.audio_service, "get_settings", Mock(return_value=settings)),
        (recording.audio_service, "stop_monitoring", stop_monitoring),
    )

    assert recording.prepare_room_voice_relay(database) == {"relay_available": False}
    stop_monitoring.assert_called_once_with()


def test_release_room_voice_relay_restores_ordinary_monitoring(monkeypatch):
    database, settings = Mock(), audio_settings(monitoring_enabled=True)
    configure = Mock()
    patch_many(
        monkeypatch,
        (recording.audio_service, "get_settings", Mock(return_value=settings)),
        (recording.audio_service, "configure_monitoring", configure),
    )

    assert recording.release_room_voice_relay(database) == {"status": "released"}
    configure.assert_called_once_with(settings, relay_needed=False)


def test_room_relay_recording_keeps_the_shared_monitor_running(monkeypatch):
    database, body, song, settings = Mock(), start_body(), SimpleNamespace(id="song"), audio_settings(monitoring_enabled=True)
    body.room_mode = True
    body.voice_relay = True
    stop_monitoring = Mock()
    configure = Mock()
    start = Mock(return_value="session")
    patch_many(
        monkeypatch,
        (recording.repositories, "get_song", Mock(return_value=song)),
        (recording.audio_service, "get_settings", Mock(return_value=settings)),
        (recording.audio_service, "stop_monitoring", stop_monitoring),
        (recording.audio_service, "configure_monitoring", configure),
        (recording.recording_service, "start_recording", start),
    )
    patch_attrs(
        monkeypatch,
        recording.audio_service,
        preferred_input_device=Mock(return_value=3),
        preferred_output_device=Mock(return_value=4),
        preferred_sample_rate=Mock(return_value=44_100),
    )

    recording.start_recording(body, database)

    configure.assert_called_once_with(settings, relay_needed=True)
    stop_monitoring.assert_not_called()
    assert start.call_args.kwargs["monitoring_enabled"] is False
    assert start.call_args.kwargs["monitor_owner"] == "room-relay"
    assert start.call_args.kwargs["monitor_mode"] is None


def test_room_relay_recording_falls_back_to_stopping_when_monitoring_is_disabled(monkeypatch):
    database, body, song, settings = Mock(), start_body(), SimpleNamespace(id="song"), audio_settings(monitoring_enabled=False)
    body.room_mode = True
    body.voice_relay = True
    stop_monitoring = Mock()
    configure = Mock()
    start = Mock(return_value="session")
    patch_many(
        monkeypatch,
        (recording.repositories, "get_song", Mock(return_value=song)),
        (recording.audio_service, "get_settings", Mock(return_value=settings)),
        (recording.audio_service, "stop_monitoring", stop_monitoring),
        (recording.audio_service, "configure_monitoring", configure),
        (recording.recording_service, "start_recording", start),
    )
    patch_attrs(
        monkeypatch,
        recording.audio_service,
        preferred_input_device=Mock(return_value=3),
        preferred_output_device=Mock(return_value=4),
        preferred_sample_rate=Mock(return_value=44_100),
    )

    recording.start_recording(body, database)

    stop_monitoring.assert_called_once_with()
    configure.assert_not_called()
    assert start.call_args.kwargs["monitor_owner"] == "room-relay"


def test_room_relay_recording_falls_back_to_stopping_when_the_monitor_fails_to_start(monkeypatch):
    database, body, song, settings = Mock(), start_body(), SimpleNamespace(id="song"), audio_settings(monitoring_enabled=True)
    body.room_mode = True
    body.voice_relay = True
    stop_monitoring = Mock()
    configure = Mock(side_effect=RuntimeError("device busy"))
    start = Mock(return_value="session")
    patch_many(
        monkeypatch,
        (recording.repositories, "get_song", Mock(return_value=song)),
        (recording.audio_service, "get_settings", Mock(return_value=settings)),
        (recording.audio_service, "stop_monitoring", stop_monitoring),
        (recording.audio_service, "configure_monitoring", configure),
        (recording.recording_service, "start_recording", start),
    )
    patch_attrs(
        monkeypatch,
        recording.audio_service,
        preferred_input_device=Mock(return_value=3),
        preferred_output_device=Mock(return_value=4),
        preferred_sample_rate=Mock(return_value=44_100),
    )

    recording.start_recording(body, database)

    configure.assert_called_once_with(settings, relay_needed=True)
    stop_monitoring.assert_called_once_with()
    assert start.call_args.kwargs["monitoring_enabled"] is False


def test_start_recording_translates_missing_song_and_audio_failure(monkeypatch):
    database, body = Mock(), start_body()
    monkeypatch.setattr(recording.repositories, "get_song", Mock(return_value=None))
    assert_http_status(404, lambda: recording.start_recording(body, database))

    recording.repositories.get_song.return_value = SimpleNamespace(id="song")
    patch_attrs(monkeypatch, recording.audio_service, get_settings=Mock(return_value=audio_settings()))
    patch_attrs(monkeypatch, recording, _configure_recording_monitor=Mock(side_effect=RuntimeError('device busy')))
    restore = Mock()
    monkeypatch.setattr(recording, "_restore_monitoring", restore)
    assert_http_status(503, lambda: recording.start_recording(body, database))
    restore.assert_called_once_with(database)


def test_recording_actions_and_stop_error_mapping(monkeypatch):
    patch_attrs(monkeypatch, recording.recording_service, pause_recording=Mock(), resume_recording=Mock(), sync_recording=Mock())
    assert (recording.pause_recording('session') == {'status': 'paused'}) and (recording.resume_recording('session') == {'status': 'recording'})
    assert recording.sync_recording("session", 3.25) == {"status": "synchronized"}
    recording.recording_service.sync_recording.assert_called_once_with("session", 3.25, 1)

    for kwargs in ({"position_sec": -0.01}, {"position_sec": 1, "playback_rate": 0.49}, {"position_sec": 1, "playback_rate": 1.51}):
        assert_http_status(422, lambda kwargs=kwargs: recording.sync_recording("session", **kwargs))

    database, saved = Mock(), object()
    stop = Mock(return_value=saved)
    monkeypatch.setattr(recording.recording_service, "stop_recording", stop)
    restore = Mock()
    monkeypatch.setattr(recording, "_restore_monitoring", restore)
    assert recording.stop_recording("session", database) is saved
    restore.assert_called_once_with(database)
    # TASK 4.2: monitoring must be restored on EVERY outcome, including a
    # RuntimeError — which is exactly what a writer/stream failure raises
    # (recording_service.stop_and_save) and was previously left uncaught here.
    for error, status in (
        (KeyError("missing"), 404),
        (ValueError("bad"), 400),
        (OSError("disk"), 500),
        (RuntimeError("writer crashed"), 500),
    ):
        restore.reset_mock()
        stop.side_effect = error
        with pytest.raises(HTTPException) as mapped: recording.stop_recording("session", database)
        assert mapped.value.status_code == status
        assert str(error) in str(mapped.value.detail)
        restore.assert_called_once_with(database)


def test_stop_recording_restores_monitoring_as_soon_as_capture_is_released(monkeypatch):
    # recording_service.stop_recording() releases the input/output device
    # (via on_capture_released) well before it returns -- WAV write and the
    # ffmpeg performance mix still run after. Monitoring must come back at
    # that earlier point, not only once the whole call (and that extra
    # work) finishes, or the singer goes without self-monitoring for however
    # long ffmpeg happens to take on top.
    database, saved, order = Mock(), object(), []
    restore = Mock(side_effect=lambda _db: order.append("restore"))
    monkeypatch.setattr(recording, "_restore_monitoring", restore)

    def fake_stop_recording(_session_id, *, on_capture_released=None):
        order.append("capture_released")
        if on_capture_released is not None:
            on_capture_released()
        order.append("ffmpeg_mix")
        return saved

    monkeypatch.setattr(recording.recording_service, "stop_recording", fake_stop_recording)

    assert recording.stop_recording("session", database) is saved
    restore.assert_called_once_with(database)
    assert order == ["capture_released", "restore", "ffmpeg_mix"]


def test_recording_controls_are_applied_to_the_named_active_session(monkeypatch):
    update = Mock()
    monkeypatch.setattr(recording.recording_service, "update_recording_controls", update)
    body = schemas.RecordingControlsRequest(
        music_volume=0.25,
        microphone_volume=1.75,
        reverb=0.4,
        echo=0.3,
        delay=0.2,
        octave=-0.5,
    )

    assert recording.update_recording_controls("session", body) == {"status": "updated"}
    update.assert_called_once_with(
        "session",
        music_gain=0.25,
        gain=1.75,
        effects={"reverb": 0.4, "echo": 0.3, "delay": 0.2, "octave": -0.5},
    )


def persisted_recording(path): return models.Recording(id='recording', song_id='song', filename='take.wav', path=str(path), duration_sec=2, sample_rate=48000, created_at=datetime(2026, 1, 1, tzinfo=UTC))


def test_recording_queries_files_and_deletion(monkeypatch, tmp_path):
    database, song, voice = Mock(), SimpleNamespace(id='song'), tmp_path / 'take.wav'
    voice.write_bytes(b"voice")
    current = persisted_recording(voice)
    patch_attrs(monkeypatch, recording.repositories, list_recordings_for_song=Mock(return_value=[current]), list_recording_library=Mock(return_value=[(current, 'Song')]))
    assert (recording.get_recording_settings(database) is not None) and (recording.list_recordings_for_song(song, database) == [current]) and (recording.list_recording_library(database)[0]['song_title'] == 'Song') and (recording.get_recording(current) is current)

    patch_attrs(monkeypatch, recording.recording_service, resolve_recording_path=Mock(return_value=voice))
    assert recording.get_recording_file(current).path == voice
    voice.unlink()
    assert_http_status(404, lambda: recording.get_recording_file(current))

    delete = Mock()
    monkeypatch.setattr(recording.recording_service, "delete_recording", delete)
    recording.delete_recording(current, database)
    delete.assert_called_once_with(database, current)


def test_performance_file_prefers_mp3_then_wav_then_voice(monkeypatch, tmp_path):
    voice = tmp_path / "take.wav"
    voice.write_bytes(b"voice")
    current, mp3, wav = persisted_recording(voice), tmp_path / 'mix.mp3', tmp_path / 'mix.wav'
    patch_attrs(monkeypatch, recording.recording_service, performance_mix_paths=Mock(return_value=(mp3, wav)), resolve_recording_path=Mock(return_value=voice))
    mp3.write_bytes(b"mix")
    assert recording.get_performance_file(current).media_type == "audio/mpeg"
    mp3.unlink()
    wav.write_bytes(b"mix")
    assert recording.get_performance_file(current).media_type == "audio/wav"
    wav.unlink()
    assert recording.get_performance_file(current).path == voice
    voice.unlink()
    assert_http_status(404, lambda: recording.get_performance_file(current))


def test_recording_start_is_blocked_while_song_is_processing(monkeypatch):
    database, body = Mock(), start_body()
    patch_many(monkeypatch, (recording.repositories, "get_song", Mock(return_value=SimpleNamespace(id="song"))), (recording.pipeline_service, "is_processing", Mock(return_value=True)))

    with pytest.raises(HTTPException) as blocked: recording.start_recording(body, database)

    assert blocked.value.status_code == 409
