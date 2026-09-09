"""Управление песнями + запуск AI-обработки."""

import asyncio
import base64
import tempfile
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

import config
import models
import schemas
from app.api.dependencies import SongDependency
from app.api.errors import http_error
from app.services import (
    ai_bridge,
    kar_dataset_service,
    kfn_dataset_service,
    metadata_enrichment_service,
    pipeline_service,
    recording_service,
    song_artifacts,
    song_editor_service,
    song_package_service,
    song_lyrics,
    song_service,
    storage_budget_service,
)
from app.services.db_utils import commit, commit_refresh
from app.services.storage_budget_service import InsufficientStorageError
from app.utils.files import read_text_tail
from app.utils.json_files import read_json, write_json
from app.utils.uploads import save_upload_limited
from database import get_db

router = APIRouter(prefix="/songs", tags=["songs"])

_COVER_MIME_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def _upload_size(file: UploadFile, default: int) -> int:
    # UploadFile.size is 0 until Starlette has parsed the multipart body, then
    # holds the real (possibly genuinely 0-byte) size -- `or default` treated
    # that real 0 as falsy and silently substituted `default` (often ~2GiB)
    # for a storage_budget_service.reserve() call sized for a trivial upload,
    # which could then fail with 507 for lack of ~2GiB free disk space.
    size = getattr(file, "size", None)
    return int(size) if size is not None else default


def _processing_status(
    song: models.Song,
    *,
    telemetry: dict[str, object] | None = None,
) -> schemas.ProcessingStatusOut:
    telemetry = telemetry or {}
    progress_detail, live_percent, eta_seconds = telemetry.get('progress_detail'), telemetry.get('progress_percent'), telemetry.get('eta_seconds')
    stage = telemetry.get("stage")
    stage_elapsed = telemetry.get("stage_elapsed_seconds")
    total_elapsed = telemetry.get("total_elapsed_seconds")
    estimated_finish = telemetry.get("estimated_finish_at")
    return schemas.ProcessingStatusOut(
        song_id=song.id,
        status=song.status,
        progress_step=song.progress_step,
        progress_percent=(
            float(live_percent) if isinstance(live_percent, int | float) else song.progress_percent
        ),
        progress_detail=progress_detail if isinstance(progress_detail, str) else None,
        eta_seconds=eta_seconds if isinstance(eta_seconds, int) else None,
        stage=stage if isinstance(stage, str) else None,
        stage_elapsed_seconds=(
            stage_elapsed if isinstance(stage_elapsed, int) else None
        ),
        total_elapsed_seconds=(
            total_elapsed if isinstance(total_elapsed, int) else None
        ),
        estimated_finish_at=(
            estimated_finish if isinstance(estimated_finish, str) else None
        ),
        error_message=song.error_message,
    )


def _queue_song_job(
    db: Session,
    song: models.Song,
    start_job: Callable[[str], bool],
    **queued_values: object,
) -> None:
    previous = {field: getattr(song, field) for field in queued_values}
    requested_status = queued_values.get("status")
    if isinstance(requested_status, models.SongStatus):
        with http_error(song_service.InvalidStatusTransition, 409):
            song_service.validate_status_transition(song.status, requested_status)

    def restore_previous_state() -> None:
        # start_job(False) can mean another request won the race and already
        # advanced this row. Refresh first and never overwrite that newer state.
        db.refresh(song)
        if isinstance(requested_status, models.SongStatus) and song.status != requested_status:
            return
        for field, value in previous.items(): setattr(song, field, value)
        commit(db)

    for field, value in queued_values.items(): setattr(song, field, value)
    try:
        commit(db)
    except Exception:
        for field, value in previous.items(): setattr(song, field, value)
        raise

    try:
        started = start_job(song.id)
    except Exception:
        restore_previous_state()
        raise
    if started: return
    restore_previous_state()
    raise HTTPException(status_code=409, detail="Обработка уже запущена")


def _touch_song(db: Session, song: models.Song, *, refresh: bool) -> None:
    song.updated_at = datetime.now(UTC)
    db.add(song)
    if refresh:
        commit_refresh(db, song)
    else:
        commit(db)


def _song_error_status(detail: str) -> int: return 404 if detail == 'Song not found' else 409


@router.post("", response_model=schemas.SongOut, status_code=201)
async def add_song(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    artist: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    config.UPLOAD_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    filename = file.filename or "song"
    suffix = Path(filename).suffix.casefold() or ".tmp"
    with tempfile.NamedTemporaryFile(
        prefix=".song-upload-",
        suffix=suffix,
        dir=config.UPLOAD_TEMP_DIR,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    upload_reservation = None
    try:
        upload_size = _upload_size(file, config.MAX_AUDIO_UPLOAD_BYTES)
        upload_reservation = storage_budget_service.reserve(
            "song_upload",
            config.UPLOAD_TEMP_DIR,
            min(upload_size, config.MAX_AUDIO_UPLOAD_BYTES),
        )
        await save_upload_limited(
            file,
            temporary_path,
            limit=config.MAX_AUDIO_UPLOAD_BYTES,
            chunk_size=config.UPLOAD_CHUNK_SIZE,
            too_large_message="Audio file is too large",
        )
        resolved_title, resolved_artist = title or "", artist
        if suffix in config.ALLOWED_KARAOKE_EXTENSIONS:
            if suffix in kar_dataset_service.SUPPORTED_KARAOKE_MIDI_SUFFIXES:
                document = kar_dataset_service.parse_kar(
                    temporary_path,
                    original_filename=filename,
                )
                detected_artist, detected_title = document.artist, document.title
            else:
                detected_artist, detected_title = kfn_dataset_service.inspect_kfn_identity(
                    temporary_path,
                    original_filename=filename,
                )
            resolved_title = resolved_title or detected_title
            if not (resolved_artist or "").strip():
                resolved_artist = detected_artist
        # create_song_from_path validates the upload through a blocking
        # ffmpeg subprocess (see AI/audio.py:run_ffmpeg). Running it inline
        # in this async handler would stall FastAPI's single event loop --
        # and every other concurrent request/websocket with it -- for as
        # long as ffmpeg takes, so it goes through the same threadpool
        # pattern already used for kar/kfn preparation above.
        return await run_in_threadpool(
            song_service.create_song_from_path,
            db,
            resolved_title,
            filename,
            temporary_path,
            resolved_artist,
        )
    except HTTPException:
        raise
    except song_service.SongAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if upload_reservation is not None: upload_reservation.release()
        temporary_path.unlink(missing_ok=True)


@router.post("/identity", response_model=schemas.SongIdentityOut)
async def inspect_song_identity(file: UploadFile = File(...)):
    config.UPLOAD_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "song").suffix or ".tmp"
    with tempfile.NamedTemporaryFile(
        prefix=".song-identity-",
        suffix=suffix,
        dir=config.UPLOAD_TEMP_DIR,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    upload_reservation = None
    try:
        upload_size = _upload_size(file, config.MAX_AUDIO_UPLOAD_BYTES)
        upload_reservation = storage_budget_service.reserve(
            "song_identity_upload",
            config.UPLOAD_TEMP_DIR,
            min(upload_size, config.MAX_AUDIO_UPLOAD_BYTES),
        )
        await save_upload_limited(
            file,
            temporary_path,
            limit=config.MAX_AUDIO_UPLOAD_BYTES,
            chunk_size=config.UPLOAD_CHUNK_SIZE,
            too_large_message="Audio file is too large",
        )
        filename = file.filename or "song"
        suffix = Path(filename).suffix.casefold()
        artist: str | None
        title: str
        if suffix in kar_dataset_service.SUPPORTED_KARAOKE_MIDI_SUFFIXES:
            document = kar_dataset_service.parse_kar(
                temporary_path,
                original_filename=filename,
            )
            artist, title = document.artist, document.title
        elif suffix == ".kfn":
            artist, title = kfn_dataset_service.inspect_kfn_identity(
                temporary_path,
                original_filename=filename,
            )
        else:
            artist, title = song_service._read_source_identity(
                temporary_path,
                filename,
                "",
            )
        cover = (
            None
            if suffix in config.ALLOWED_KARAOKE_EXTENSIONS
            else song_service.read_embedded_cover(temporary_path)
        )
        cover_data_url = None
        if cover is not None:
            payload, extension = cover
            mime = _COVER_MIME_TYPES[extension]
            cover_data_url = f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"
        return schemas.SongIdentityOut(
            title=title,
            artist=artist,
            cover_data_url=cover_data_url,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if upload_reservation is not None: upload_reservation.release()
        temporary_path.unlink(missing_ok=True)


@router.post("/training/kar", response_model=schemas.KarDatasetBatchOut)
async def prepare_kar_training_dataset(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(
            status_code=400, detail="Выберите хотя бы один файл .kar, .mid или .kfn"
        )
    if len(files) > 250:
        raise HTTPException(status_code=400, detail="За один раз можно подготовить не более 250 файлов")
    semaphore = asyncio.Semaphore(3)

    async def prepare_one(upload: UploadFile):
        filename = Path(upload.filename or "song.kar").name
        suffix = Path(filename).suffix.casefold()
        if suffix not in {".kar", ".mid", ".kfn"}:
            await upload.close()
            return {
                "filename": filename,
                "status": "error",
                "error": "Поддерживаются только файлы .kar, .mid и .kfn",
            }
        config.UPLOAD_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".kar-training-",
            suffix=suffix,
            dir=config.UPLOAD_TEMP_DIR,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            async with semaphore:
                await save_upload_limited(
                    upload,
                    temporary_path,
                    limit=(
                        kar_dataset_service.MAX_KAR_BYTES
                        if suffix in {".kar", ".mid"}
                        else kfn_dataset_service.MAX_KFN_BYTES
                    ),
                    chunk_size=256 * 1024,
                    too_large_message=(
                        f"Файл {suffix} превышает допустимый размер 8 МБ"
                        if suffix in {".kar", ".mid"}
                        else "Файл .kfn превышает допустимый размер 256 МБ"
                    ),
                )
                prepare = (
                    kar_dataset_service.prepare_kar_file
                    if suffix in {".kar", ".mid"}
                    else kfn_dataset_service.prepare_kfn_file
                )
                prepared = await run_in_threadpool(
                    prepare, temporary_path, original_filename=filename
                )
                return {"filename": filename, **prepared}
        except (kar_dataset_service.MidiSkipped, kfn_dataset_service.KfnSkipped) as exc:
            return {"filename": filename, "status": "skipped", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - one bad file must not cancel the batch
            return {"filename": filename, "status": "error", "error": str(exc)}
        finally:
            temporary_path.unlink(missing_ok=True)
            await upload.close()

    results = await asyncio.gather(*(prepare_one(upload) for upload in files))
    return {
        "output_root": str(kar_dataset_service.DATASET_DIR.resolve()),
        "items": results,
    }


@router.get("", response_model=list[schemas.SongOut])
def get_songs(db: Session = Depends(get_db)):
    return song_service.list_songs(db)


@router.get("/{song_id}", response_model=schemas.SongOut)
def get_song(song: SongDependency):
    return song


@router.get("/{song_id}/revision")
def get_song_revision(song_id: str, db: Session = Depends(get_db)):
    try:
        revision = song_package_service.content_revision_for_song(db, song_id)
    except (OSError, ValueError) as exc:
        detail = str(exc)
        raise HTTPException(
            status_code=_song_error_status(detail), detail=f"Could not fingerprint song: {detail}"
        ) from exc
    return {"song_id": song_id, "revision": revision}


@router.post("/revisions", response_model=schemas.SongRevisionsOut)
def get_song_revisions(payload: schemas.SongRevisionsRequest, db: Session = Depends(get_db)):
    results = song_package_service.content_revisions_for_songs(db, payload.song_ids)
    return {
        "revisions": [
            {"song_id": song_id, "revision": revision, "error": error}
            for song_id, revision, error in results
        ]
    }


@router.post("/revision/resolve", response_model=schemas.SongRevisionResolveOut)
def resolve_song_revision(
    payload: schemas.SongRevisionResolveRequest, db: Session = Depends(get_db)
):
    return {
        "song_id": song_package_service.find_song_id_by_content_revision(db, payload.revision)
    }


@router.get("/{song_id}/package")
def export_song_package(
    song_id: str, background_tasks: BackgroundTasks, expected_revision: str | None = None,
    db: Session = Depends(get_db),
):
    try:
        package_path, slug = song_package_service.build_package_for_song(
            db, song_id, expected_revision=expected_revision,
        )
    except InsufficientStorageError as exc:
        raise HTTPException(status_code=507, detail=exc.payload()) from exc
    except (OSError, ValueError) as exc:
        detail = str(exc)
        raise HTTPException(
            status_code=_song_error_status(detail), detail=f"Could not package song: {detail}"
        ) from exc
    background_tasks.add_task(package_path.unlink, missing_ok=True)
    return FileResponse(
        package_path,
        media_type="application/zip",
        filename=f"{slug}.karaoke.zip",
        background=background_tasks,
    )


@router.post("/package/import", response_model=schemas.SongOut, status_code=201)
async def import_song_package(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    expected_revision: str | None = None,
):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="karaoke-import-",
        suffix=".zip",
        dir=config.CACHE_DIR,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    upload_reservation = None
    try:
        upload_size = _upload_size(file, song_package_service.MAX_PACKAGE_BYTES)
        upload_reservation = storage_budget_service.reserve(
            "song_package_upload",
            config.CACHE_DIR,
            min(upload_size, song_package_service.MAX_PACKAGE_BYTES),
        )
        await save_upload_limited(
            file,
            temporary_path,
            limit=song_package_service.MAX_PACKAGE_BYTES,
            chunk_size=1024 * 1024,
            too_large_message="Song package is too large",
        )
        upload_reservation.release()
        upload_reservation = None
        # import_package runs up to 3 blocking ffprobe subprocesses
        # (_validate_archive_audio); same event-loop-stall concern as
        # create_song_from_path above.
        return await run_in_threadpool(
            song_package_service.import_package, db, temporary_path, expected_revision=expected_revision
        )
    except HTTPException:
        raise
    except InsufficientStorageError as exc:
        raise HTTPException(status_code=507, detail=exc.payload()) from exc
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise HTTPException(
            status_code=400, detail=f"Could not import song package: {exc}"
        ) from exc
    finally:
        if upload_reservation is not None: upload_reservation.release()
        temporary_path.unlink(missing_ok=True)


@router.patch("/{song_id}", response_model=schemas.SongOut)
def patch_song(song: SongDependency, patch: schemas.SongUpdate, db: Session = Depends(get_db)):
    with http_error(ValueError, 422): return song_service.update_song(db, song, patch)


@router.delete("/{song_id}", status_code=204)
def remove_song(song: SongDependency, db: Session = Depends(get_db)):
    if pipeline_service.is_processing(song.id):
        raise HTTPException(
            status_code=409, detail="Песня сейчас обрабатывается, дождитесь завершения"
        )
    recording_service.close_sessions_for_song(song.id)
    try:
        song_service.delete_song(db, song)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=f"Could not delete song files: {exc}") from exc


@router.post("/{song_id}/process", response_model=schemas.ProcessingStatusOut, status_code=202)
def process_song(
    song: SongDependency,
    db: Session = Depends(get_db),
    request: schemas.ProcessingRequest | None = None,
):
    if recording_service.has_active_recording(song.id): raise HTTPException(status_code=409, detail="Нельзя обрабатывать песню во время записи")
    if pipeline_service.is_processing(song.id): raise HTTPException(status_code=409, detail="Обработка уже запущена")
    if song_service.original_source_retired(song):
        raise HTTPException(
            status_code=409,
            detail="Исходный файл песни был удалён после обработки — доступна только "
            "переобработка мелодии (/reprocess). Полная повторная обработка требует "
            "заново загрузить оригинальный файл.",
        )

    _queue_song_job(
        db,
        song,
        lambda song_id: pipeline_service.start_processing(
            song_id, request.mode if request is not None else "auto"
        ),
        status=models.SongStatus.QUEUED,
        error_message=None,
    )
    db.refresh(song)
    return _processing_status(song)


@router.post("/{song_id}/reprocess", response_model=schemas.ProcessingStatusOut, status_code=202)
def reprocess_melody(song: SongDependency, db: Session = Depends(get_db)):
    """Clear prior generated files and rebuild the vocal melody from vocals.flac."""
    if recording_service.has_active_recording(song.id): raise HTTPException(status_code=409, detail="Нельзя переобрабатывать песню во время записи")
    if not song.output_dir or not song_service.is_done(song): raise HTTPException(status_code=409, detail="Сначала завершите полную обработку песни")
    if pipeline_service.is_processing(song.id): raise HTTPException(status_code=409, detail="Обработка уже запущена")
    _queue_song_job(
        db,
        song,
        pipeline_service.start_reprocessing,
        status=models.SongStatus.QUEUED,
        error_message=None,
        progress_percent=0.0,
        progress_step="0/13",
    )
    db.refresh(song)
    return _processing_status(song)


@router.get("/{song_id}/status", response_model=schemas.ProcessingStatusOut)
def get_status(song: SongDependency):
    telemetry = pipeline_service.get_processing_telemetry(song.id)
    return _processing_status(song, telemetry=telemetry)


@router.post("/{song_id}/cancel", response_model=schemas.ProcessingStatusOut)
def cancel_processing(song: SongDependency, db: Session = Depends(get_db)):
    if not pipeline_service.cancel_processing(song.id): raise HTTPException(status_code=409, detail="Song is not being processed")
    db.refresh(song)
    return _processing_status(song)


@router.get("/{song_id}/log")
def get_processing_log(song: SongDependency):
    log_path = config.APP_LOG_DIR / "application.log"
    marker = f"[song:{song.id}]"
    lines = [] if not log_path.exists() else [line for line in read_text_tail(log_path) if marker in line]
    return {"lines": lines}


@router.get("/{song_id}/cover")
def get_song_cover(song: SongDependency):
    with http_error(ValueError, 409):
        output_dir = song_service.resolve_output_dir(song)
        for extension, media_type in _COVER_MIME_TYPES.items():
            path = output_dir / f"cover{extension}"
            if path.is_file(): return FileResponse(path, media_type=media_type)

        source = song_service.resolve_source_path(song)
    recovered = song_service.extract_embedded_cover(source, output_dir) if source.is_file() else None
    if recovered is not None:
        media_type = _COVER_MIME_TYPES[recovered.suffix.lower()]
        return FileResponse(recovered, media_type=media_type)
    raise HTTPException(status_code=404, detail="Song cover not found")


@router.get("/{song_id}/audio/{track}")
def get_audio_track(track: str, song: SongDependency):
    if track not in {"instrumental", "vocals", "song", "diagnostic"}: raise HTTPException(status_code=404, detail="Unknown audio track")
    with http_error(ValueError, 409):
        output_dir = song_service.resolve_output_dir(song)
        candidate = song_service.resolve_source_path(song) if track == "song" else song_artifacts.resolve_audio_artifact(output_dir, track)
    # During KAR/MID/KFN processing the database source still points at the
    # symbolic file. WaveSurfer cannot decode MIDI/KFN, but the preparation
    # job writes the matched recording to original.flac well before the rest
    # of the pipeline is complete. Expose that real audio as soon as it exists
    # so the processing modal renders the song's waveform instead of its
    # synthetic fallback shape.
    if track == "song" and (
        candidate is None or candidate.suffix.lower() not in config.ALLOWED_AUDIO_EXTENSIONS
    ):
        original = output_dir / "original.flac"
        if original.is_file(): candidate = original
    if candidate is not None and candidate.is_file() and candidate.suffix.lower() in config.ALLOWED_AUDIO_EXTENSIONS:
        media_type = {".flac": "audio/flac", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".aac": "audio/aac"}.get(
            candidate.suffix.lower(), "application/octet-stream"
        )
        return FileResponse(
            candidate,
            media_type=media_type,
            filename=candidate.name,
            content_disposition_type="inline",
        )
    raise HTTPException(status_code=404, detail="Audio track is not available")


@router.get("/{song_id}/video")
def get_song_video(song: SongDependency):
    candidate = metadata_enrichment_service.resolve_local_video(song)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Song video is not available")
    return FileResponse(
        candidate,
        media_type="video/mp4",
        filename=candidate.name,
        content_disposition_type="inline",
    )


@router.get("/{song_id}/editor", response_model=schemas.SongEditorOut)
def get_song_editor(song: SongDependency):
    if not song_service.is_done(song): raise HTTPException(status_code=409, detail="Песня ещё не обработана")
    with http_error(ValueError, 409):
        lyrics_sync, backup_exists = song_editor_service.load_editor(
            song_service.resolve_output_dir(song)
        )
    return schemas.SongEditorOut(lyrics_sync=lyrics_sync, ai_backup_exists=backup_exists)


@router.put("/{song_id}/editor", response_model=schemas.SongEditorOut)
def save_song_editor(
    payload: schemas.SongEditorUpdate, song: SongDependency, db: Session = Depends(get_db)
):
    if not song_service.is_done(song): raise HTTPException(status_code=409, detail="Песня ещё не обработана")
    with http_error(ValueError, 400), song_service.song_content_lock(song.id), song_service.library_write_lock():
        lyrics_sync = song_editor_service.save_editor(
            song_service.resolve_output_dir(song), payload.notes,
            payload.word_texts, payload.word_bounds,
        )
        song_package_service.invalidate_content_revision(song)
    _touch_song(db, song, refresh=True)
    return schemas.SongEditorOut(lyrics_sync=lyrics_sync, ai_backup_exists=True)


@router.post("/{song_id}/editor/reset", response_model=schemas.SongEditorOut)
def reset_song_editor(song: SongDependency, db: Session = Depends(get_db)):
    if not song_service.is_done(song): raise HTTPException(status_code=409, detail="Песня ещё не обработана")
    with http_error(ValueError, 409), song_service.song_content_lock(song.id), song_service.library_write_lock():
        lyrics_sync = song_editor_service.reset_editor(song_service.resolve_output_dir(song))
        song_package_service.invalidate_content_revision(song)
    _touch_song(db, song, refresh=False)
    return schemas.SongEditorOut(lyrics_sync=lyrics_sync, ai_backup_exists=False)


@router.get("/{song_id}/result", response_model=schemas.SongResultOut)
def get_result(song: SongDependency, response: Response):
    if not song_service.is_done(song) or not song.output_dir: raise HTTPException(status_code=409, detail="Песня ещё не обработана")

    with http_error(ValueError, 409):
        out_dir = song_service.resolve_output_dir(song)
        with song_service.song_content_lock(song.id):
            ai_bridge.recover_generated_artifacts(out_dir)
            lyrics_sync = ai_bridge.get_karaoke_lyrics(out_dir)
    if not isinstance(lyrics_sync, dict): lyrics_sync = {}
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return schemas.SongResultOut(
        song=schemas.SongOut.model_validate(song),
        lyrics_sync=lyrics_sync,
    )


@router.put("/{song_id}/lyrics")
def update_lyrics(body: schemas.LyricsUpdate, song: SongDependency, db: Session = Depends(get_db)):
    if not song_service.is_done(song) or not song.output_dir: raise HTTPException(status_code=409, detail="Song has not been processed yet")

    try:
        out_dir = song_service.resolve_output_dir(song)
        lyrics_path = out_dir / "lyricsSync.json"
        with song_service.song_content_lock(song.id), song_service.library_write_lock():
            reconcile_lyric_words = ai_bridge.reconcile_lyric_words
            lyrics = reconcile_lyric_words([line.model_dump() for line in body.lyrics])
            current: dict[str, Any] = read_json(lyrics_path, default={}) or {}
            trusted_text = "\n".join(str(line.get("text") or "").strip() for line in lyrics).strip()
            previous = current.get("words", []) if isinstance(current, dict) else []
            words = []
            for source in (word for line in lyrics for word in line.get("words", [])):
                word = dict(source)
                word["text"] = str(word.pop("word", word.get("text", ""))).strip()
                word["notes"] = []
                words.append(word)
            song_lyrics.carry_forward_word_notes(previous, words)
            updated = {**current, "text": trusted_text, "words": words, "edited": True}
            write_json(lyrics_path, ai_bridge.validate_lyrics_document(updated))
            song_package_service.invalidate_content_revision(song)
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Could not save lyrics: {exc}") from exc
    _touch_song(db, song, refresh=False)
    return {"status": "saved"}
