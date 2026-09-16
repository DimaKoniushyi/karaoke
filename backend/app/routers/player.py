"""Karaoke synchronization, timeline and playback-state endpoints."""

import logging

from fastapi import APIRouter

import schemas
from app.api.dependencies import DatabaseSession, SongDependency
from app.services import audio_service, player_service

router = APIRouter(prefix="/player", tags=["player"])
logger = logging.getLogger(__name__)


def _set_karaoke_media_active(db: DatabaseSession, song_id: str, active: bool) -> None:
    try:
        audio_service.set_shared_media_active(db, f"karaoke:{song_id}", active)
    except RuntimeError as exc:
        # Monitoring is optional to music playback. A failed restart has
        # already released the prior exclusive worker; keep karaoke usable.
        logger.warning("Could not change microphone monitor for karaoke playback: %s", exc)


@router.get("/{song_id}/sync")
def get_sync(song: SongDependency):
    return player_service.get_sync_data(song)


@router.get("/{song_id}/timeline")
def get_timeline(song: SongDependency):
    return player_service.get_timeline(song)


@router.get("/{song_id}/position", response_model=schemas.PlaybackStateOut)
def get_position(song: SongDependency, db: DatabaseSession):
    return player_service.get_state(db, song.id)


@router.post("/{song_id}/seek", response_model=schemas.PlaybackStateOut)
def seek(song: SongDependency, body: schemas.SeekRequest, db: DatabaseSession):
    return player_service.seek(db, song.id, body.position_sec)


@router.post("/{song_id}/pause", response_model=schemas.PlaybackStateOut)
def pause(song: SongDependency, db: DatabaseSession):
    result = player_service.set_playing(db, song.id, False)
    _set_karaoke_media_active(db, song.id, False)
    return result


@router.post("/{song_id}/resume", response_model=schemas.PlaybackStateOut)
def resume(song: SongDependency, db: DatabaseSession):
    _set_karaoke_media_active(db, song.id, True)
    return player_service.set_playing(db, song.id, True)


@router.post("/{song_id}/stop", response_model=schemas.PlaybackStateOut)
def stop(song: SongDependency, db: DatabaseSession):
    result = player_service.stop(db, song.id)
    _set_karaoke_media_active(db, song.id, False)
    return result
