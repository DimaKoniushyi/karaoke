from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def _remove_stale_local_clip(service, song_id: str, song, output_dir: Path) -> tuple[bool, bool]:
    if song.video_url != service.LOCAL_VIDEO_URL:
        return False, False
    locked = False
    with service.song_service.song_content_lock(
        song_id
    ), service.song_service.library_write_lock():
        local_clip = service.resolve_local_video(song)
        source_file = output_dir / service.LOCAL_VIDEO_SOURCE_NAME
        if local_clip is not None and source_file.is_file():
            return False, False
        if local_clip is not None:
            try:
                local_clip.unlink(missing_ok=True)
            except OSError as exc:
                locked = True
                service.logger.info(
                    "Stale local clip is still in use for %s: %s", song_id, exc
                )
        source_file.unlink(missing_ok=True)
        song.video_url = None
        return True, locked


def _resolve_video(service, song_id: str, song, output_dir: Path) -> tuple[str | None, bool]:
    changed, stale_locked = _remove_stale_local_clip(
        service, song_id, song, output_dir
    )
    existing_id = service._youtube_id_from_url(song.video_url)
    if existing_id and song.video_url not in service._validated_video_urls:
        existing_url = song.video_url if isinstance(song.video_url, str) else ""
        quality = service._youtube_video_is_acceptable(
            existing_id, song.title, song.artist
        )
        if quality is True:
            service._validated_video_urls.add(existing_url)
        elif quality is False:
            song.video_url = None
            changed = True
    if existing_id and song.video_url:
        return existing_id, changed
    if not song.video_url and not stale_locked:
        return service._youtube_video_id(song.title, song.artist), changed
    return None, changed


def _lookup(service, song_id: str):
    db = service.SessionLocal()
    try:
        song = service.repositories.get_song(db, song_id)
        if song is None:
            return None
        genre = None
        try:
            if not song.genre:
                genre = service._itunes_genre(song.title, song.artist)
        except Exception as exc:
            service.logger.info("Genre lookup skipped for %s: %s", song_id, exc)
        output_dir = service.song_service.resolve_output_dir(song)
        video_id, changed = None, False
        try:
            video_id, changed = _resolve_video(service, song_id, song, output_dir)
        except Exception as exc:
            service.logger.info("Music video lookup skipped for %s: %s", song_id, exc)
        if genre and not song.genre:
            song.genre = genre
        if genre or changed:
            service.commit(db)
            service.revision_cache.invalidate(song)
        return output_dir, video_id
    finally:
        db.close()


def _expected_duration(output_dir: Path) -> float | None:
    try:
        payload = json.loads(
            (output_dir / "metadata.json").read_text(encoding="utf-8")
        )
        return float(payload.get("duration") or 0) or None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _publish(service, song_id: str, video_id: str, output_dir: Path) -> None:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output_dir.name}-metadata-", dir=output_dir.parent
    ) as staging_name:
        staging = Path(staging_name)
        downloaded = service._download_youtube_video(
            video_id,
            staging,
            expected_duration=_expected_duration(output_dir),
        )
        db = service.SessionLocal()
        try:
            with service.song_service.song_content_lock(
                song_id
            ), service.song_service.library_write_lock():
                current = service.repositories.get_song(db, song_id)
                if current is None:
                    return
                if downloaded:
                    target = service.song_service.resolve_output_dir(current)
                    target.mkdir(parents=True, exist_ok=True)
                    os.replace(
                        staging / service.LOCAL_VIDEO_NAME,
                        target / service.LOCAL_VIDEO_NAME,
                    )
                    os.replace(
                        staging / service.LOCAL_VIDEO_SOURCE_NAME,
                        target / service.LOCAL_VIDEO_SOURCE_NAME,
                    )
                next_url = service.LOCAL_VIDEO_URL if downloaded else None
                if current.video_url != next_url:
                    current.video_url = next_url
                    service.commit(db)
                    service.revision_cache.invalidate(current)
        finally:
            db.close()


def enrich(service, song_id: str) -> None:
    lookup = _lookup(service, song_id)
    if lookup is None:
        return
    output_dir, video_id = lookup
    if video_id:
        _publish(service, song_id, video_id, output_dir)
