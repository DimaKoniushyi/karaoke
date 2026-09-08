"""Запись голоса пользователя."""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

import models
import schemas
from app import repositories
from app.api.dependencies import DatabaseSession, RecordingDependency, SongDependency
from app.api.errors import http_error
from app.services import audio_service, pipeline_service, recording_service
from app.services.audio_runtime import serialized
from database import get_db

router = APIRouter(prefix="/recording", tags=["recording"])


def _change_session_state(session_id: str, action, status: str) -> dict[str, str]:
    with http_error(KeyError, 404): action(session_id)
    return {"status": status}


def _restore_monitoring(db: Session) -> None:
    try:
        # Always clears any room-relay session's sticky relay_needed=True --
        # this runs on every recording stop (and start failure), so ordinary
        # monitoring afterward never keeps a room-only relay open by mistake.
        audio_service.configure_monitoring(audio_service.get_settings(db), relay_needed=False)
    except RuntimeError:
        audio_service.stop_monitoring()


def _configure_room_relay_monitor(settings) -> bool:
    """Keeps the shared Python monitor running for a room-mode recording
    when the frontend is using its audio relay (body.voice_relay) instead
    of its own local Web Audio DSP graph.

    Unlike the JS-graph room path below, the monitor here is now the source
    of BOTH local self-monitoring in the singer's headphones AND the audio
    relayed to the browser for the WebRTC peer (see app/routers/
    audio_relay.py) -- stopping it would silence what the room hears, not
    just local playback. No second hardware output path is opened: the
    monitor already owns the one real output device, exactly as it does
    outside of room mode.
    """
    if not settings.monitoring_enabled:
        audio_service.stop_monitoring()
        return False
    try:
        audio_service.configure_monitoring(settings, relay_needed=True)
    except RuntimeError:
        audio_service.stop_monitoring()
        return False
    return True


def _configure_recording_monitor(settings, body: schemas.RecordingStartRequest) -> bool:
    if not settings.monitoring_enabled:
        audio_service.stop_monitoring()
        return True

    overrides = audio_service.settings_snapshot(
        settings,
        monitoring_enabled=True,
        volume=body.microphone_volume,
        reverb=body.reverb,
        echo=body.echo,
        delay=body.delay,
        octave=body.octave,
    )
    audio_service.configure_monitoring(overrides)
    return True


@router.get("/settings", response_model=schemas.AudioSettingsOut)
def get_recording_settings(db: Session = Depends(get_db)):
    return audio_service.get_settings(db)


@router.post("/room/prepare-voice-relay")
@serialized
def prepare_room_voice_relay(db: DatabaseSession):
    """Opens the Python monitor's audio relay for the room to connect to,
    ahead of the room actually recording anything.

    Without this, the relay only ever opened as a side effect of /start
    receiving voice_relay=true -- but the frontend only ever sent that once
    it already knew the relay worked (OnlineVoiceMesh.usingRelay), which was
    only ever set by successfully connecting to a relay that first requires
    this same voice_relay=true to exist. On a cold room join, that self-
    referential requirement meant the relay could never open at all, and the
    room silently fell back to the local JS DSP graph every time. The
    frontend now calls this explicitly before attempting to connect (see
    onlineVoiceMesh.js's tryRelay), breaking the cycle.
    """
    settings = audio_service.get_settings(db)
    return {"relay_available": _configure_room_relay_monitor(settings)}


@router.post("/room/release-voice-relay")
@serialized
def release_room_voice_relay(db: DatabaseSession):
    """Counterpart to prepare_room_voice_relay: releases the relay (falls
    back to ordinary, non-relay monitoring) once the room's local voice
    capture stops, whether or not a recording ever actually used it.
    """
    _restore_monitoring(db)
    return {"status": "released"}


@router.post("/start", response_model=schemas.RecordingStartOut)
@serialized
def start_recording(body: schemas.RecordingStartRequest, db: DatabaseSession):
    song = repositories.get_song(db, body.song_id)
    if song is None: raise HTTPException(status_code=404, detail="Песня не найдена")
    if pipeline_service.is_processing(song.id): raise HTTPException(status_code=409, detail="Нельзя начать запись во время обработки песни")
    if recording_service.has_live_capture():
        raise HTTPException(status_code=409, detail="A microphone recording is already active")
    settings = audio_service.get_settings(db)
    try:
        if body.room_mode and body.voice_relay:
            keep_native_monitor = _configure_room_relay_monitor(settings)
        elif body.room_mode:
            # Fallback path: the browser's own local Web Audio graph owns
            # live self-monitoring for the room here (voice_relay is false --
            # the frontend could not use the Python monitor's relay, e.g. an
            # ASIO driver or an unavailable/failed relay connection). Opening
            # a second PortAudio output path here makes consumer USB/WASAPI
            # devices buffer twice and creates the delayed doubled voice
            # users hear.
            audio_service.stop_monitoring()
            keep_native_monitor = False
        else:
            keep_native_monitor = _configure_recording_monitor(settings, body)
        devices = audio_service.device_snapshot()
        input_device_id = audio_service.preferred_input_device(
            settings.input_device_id,
            settings.audio_driver,
            settings.asio_driver_name,
            devices,
            device_name=getattr(settings, "input_device_name", None),
        )
        session_id = recording_service.start_recording(
            song_id=song.id,
            device_id=input_device_id,
            output_device_id=audio_service.preferred_output_device(
                input_device_id,
                settings.audio_driver,
                settings.output_device_id,
                settings.asio_driver_name,
                devices,
                device_name=getattr(settings, "output_device_name", None),
            ),
            sample_rate=audio_service.preferred_sample_rate(
                input_device_id,
                settings.audio_driver,
                devices,
            ),
            gain=body.microphone_volume,
            monitoring_enabled=(
                settings.monitoring_enabled and not keep_native_monitor and not body.room_mode
            ),
            playback_offset_sec=body.position_sec,
            playback_rate=body.playback_rate,
            blocksize=settings.buffer_size,
            music_gain=body.music_volume,
            effects={
                "reverb": body.reverb,
                "echo": body.echo,
                "delay": body.delay,
                "octave": body.octave,
            },
            noise_suppression=(
                0.35
                if getattr(settings, "noise_suppression", None) is None
                else settings.noise_suppression
            ),
            monitor_owner=(
                "room-relay" if body.room_mode and body.voice_relay
                else "room" if body.room_mode
                else "native-monitor" if keep_native_monitor
                else "recording"
            ),
            monitor_mode=None if body.room_mode or keep_native_monitor else audio_service.recording_monitor_mode(input_device_id),
        )
    except RuntimeError as exc:
        _restore_monitoring(db)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return schemas.RecordingStartOut(recording_session_id=session_id, message="Запись начата")


@router.post("/pause")
def pause_recording(session_id: str):
    return _change_session_state(session_id, recording_service.pause_recording, "paused")


@router.post("/resume")
def resume_recording(session_id: str):
    return _change_session_state(session_id, recording_service.resume_recording, "recording")


@router.post("/sync")
def sync_recording(session_id: str, position_sec: float, playback_rate: float = 1):
    # Unlike /recording/start and /player/{id}/seek, these arrive as bare
    # query params (not a validated Pydantic body), so nothing enforced their
    # bounds -- an arbitrary negative/out-of-range position_sec written here
    # persists permanently in recordings.playback_segments_json.
    if not (0 <= position_sec and 0.5 <= playback_rate <= 1.5):
        raise HTTPException(
            status_code=422,
            detail="position_sec must be >= 0 and playback_rate must be between 0.5 and 1.5",
        )
    with http_error(KeyError, 404), http_error(RuntimeError, 409):
        recording_service.sync_recording(session_id, position_sec, playback_rate)
    return {"status": "synchronized"}


@router.patch("/controls")
def update_recording_controls(session_id: str, body: schemas.RecordingControlsRequest):
    with http_error(KeyError, 404):
        recording_service.update_recording_controls(
            session_id,
            music_gain=body.music_volume,
            gain=body.microphone_volume,
            effects={
                "reverb": body.reverb,
                "echo": body.echo,
                "delay": body.delay,
                "octave": body.octave,
            },
        )
    return {"status": "updated"}


@router.post("/stop", response_model=schemas.RecordingOut)
def stop_recording(session_id: str, db: Session = Depends(get_db)):
    # Monitoring must be restored no matter how this ends — success, a mapped
    # error below, or an unmapped one (e.g. a writer/stream failure surfaces as
    # RuntimeError) — otherwise the user is left without mic monitoring until
    # they notice and start a new recording to trigger another restore.
    #
    # restored tracks whether on_capture_released already did it: the
    # input/output device is released well before stop_recording() returns
    # (WAV write and the ffmpeg performance mix happen after), so restoring
    # there instead of only in this finally means the singer isn't left
    # without self-monitoring for however long that extra work takes on top.
    restored = False

    def restore_once() -> None:
        nonlocal restored
        _restore_monitoring(db)
        restored = True

    try:
        try:
            recording = recording_service.stop_recording(session_id, on_capture_released=restore_once)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=500, detail=f"Could not save recording: {exc}") from exc
    finally:
        if not restored:
            _restore_monitoring(db)
    return recording


@router.get("/by-song/{song_id}", response_model=list[schemas.RecordingOut])
def list_recordings_for_song(song: SongDependency, db: Session = Depends(get_db)):
    return repositories.list_recordings_for_song(db, song.id)


@router.get("/library", response_model=list[schemas.RecordedSongOut])
def list_recording_library(db: Session = Depends(get_db)):
    rows = repositories.list_recording_library(db)
    return [
        {**schemas.RecordingOut.model_validate(recording).model_dump(), "song_title": title}
        for recording, title in rows
    ]


@router.get("/{recording_id}", response_model=schemas.RecordingOut)
def get_recording(recording: RecordingDependency):
    return recording


def _wav_file_response(recording: models.Recording) -> FileResponse:
    path = recording_service.resolve_recording_path(recording)
    if not path.is_file(): raise HTTPException(status_code=404, detail="Файл записи не найден")
    return FileResponse(path, media_type="audio/wav", filename=recording.filename)


@router.get("/{recording_id}/file")
def get_recording_file(recording: RecordingDependency):
    return _wav_file_response(recording)


@router.get("/{recording_id}/performance")
def get_performance_file(recording: RecordingDependency):
    for mixed_path in recording_service.performance_mix_paths(recording):
        if mixed_path.is_file():
            media_type = "audio/mpeg" if mixed_path.suffix == ".mp3" else "audio/wav"
            return FileResponse(mixed_path, media_type=media_type, filename=mixed_path.name)
    return _wav_file_response(recording)


@router.post("/{recording_id}/room-audio", response_model=schemas.RecordingOut)
async def attach_room_audio(
    recording: RecordingDependency,
    db: DatabaseSession,
    file: UploadFile = File(...),
    start_playback_sec: float = Form(default=0),
    latency_compensation_sec: float = Form(default=0),
):
    song = repositories.get_song(db, recording.song_id)
    if song is None: raise HTTPException(status_code=404, detail="Песня для записи не найдена")
    try:
        await file.seek(0)
        await run_in_threadpool(
            recording_service.attach_room_audio,
            recording,
            song,
            file.file,
            start_playback_sec,
            latency_compensation_sec,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Could not add room voices: {exc}") from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not add room voices: {exc}") from exc
    finally:
        await file.close()
    return recording


@router.delete("/{recording_id}", status_code=204)
def delete_recording(recording: RecordingDependency, db: DatabaseSession):
    recording_service.delete_recording(db, recording)
