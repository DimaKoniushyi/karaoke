from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from pathlib import Path

import soundfile as sf

import config
import models


@dataclass
class _Lifecycle:
    capture: object | None = None
    heartbeat_stop: object | None = None
    heartbeat_thread: object | None = None
    slot_acquired: bool = False
    succeeded: bool = False
    media_process: object | None = None


def _begin(service, state: _Lifecycle, song_id: str, out_dir: Path) -> None:
    state.slot_acquired = service._acquire_processing_slot(song_id)
    if not state.slot_acquired:
        return
    service._begin_runtime_progress(song_id)
    state.heartbeat_stop, state.heartbeat_thread = service._start_progress_heartbeat(
        song_id
    )
    state.capture = service._create_progress_capture(song_id, out_dir)


def _mark_cancelled(service, song_id: str) -> None:
    service._update_progress(
        song_id,
        status=models.SongStatus.CANCELLED,
        step_label="Отменено",
    )


def _handle_failure(service, song_id: str, capture, exc: Exception) -> None:
    if service._is_cancelled(song_id):
        _mark_cancelled(service, song_id)
        return
    service._write_pipeline_error(song_id, capture, exc)
    service._update_progress(
        song_id,
        status=models.SongStatus.ERROR,
        error_message=service._format_processing_error(exc),
    )


def _cleanup_common(service, state: _Lifecycle, song_id: str) -> bool:
    try:
        if state.capture is not None:
            state.capture.close()
        service._stop_progress_heartbeat(
            state.heartbeat_stop,
            state.heartbeat_thread,
        )
        service._end_runtime_progress(song_id)
        return True
    except Exception:
        service.logger.exception(
            "Song processing cleanup failed: song_id=%s", song_id
        )
        return False


def _finalize(service, state: _Lifecycle, song_id: str, out_dir: Path) -> None:
    try:
        service._finalize_processed_job(song_id, out_dir, retain_source=True)
    finally:
        if state.slot_acquired:
            service._release_processing_slot(song_id)


def _prepare_symbolic(
    service,
    song_id: str,
    source_path: str,
    out_dir: Path,
    state: _Lifecycle,
    *,
    reuse_existing_audio: bool,
) -> None:
    progress = service._create_ai_progress_callback(song_id, state.capture)
    service._update_progress(song_id, step_label="Проверка AI-моделей", percent=1.0)
    service.model_install_service.ensure_ready_sync(
        cancelled=lambda: service._is_cancelled(song_id)
    )
    service._configure_ai_runtime()
    source = Path(source_path)
    prepare = (
        service.kfn_dataset_service.prepare_kfn_file
        if source.suffix.casefold() == ".kfn"
        else service.kar_dataset_service.prepare_kar_file
    )
    artist, title = service._load_song_identity(song_id)
    result = prepare(
        source,
        original_filename=service._load_original_filename(song_id),
        title_override=title,
        artist_override=artist,
        output_root=out_dir.parent,
        target_dir=out_dir,
        progress=progress,
        cancelled=lambda: service._is_cancelled(song_id),
        reuse_existing_audio=reuse_existing_audio,
    )
    if result.get("status") != "ready" or result.get("stems_status") != "ready":
        details = "; ".join(str(item) for item in result.get("warnings", []) if item)
        raise ValueError(details or "Не удалось получить оригинал, голос и минусовку")


def run_symbolic(
    service,
    song_id: str,
    source_path: str,
    out_dir: Path,
    *,
    reuse_existing_audio: bool = False,
) -> None:
    state = _Lifecycle()
    started_at = time.monotonic()
    try:
        _begin(service, state, song_id, out_dir)
        if not state.slot_acquired:
            return
        service._log_processing_started(song_id, "karaoke-file", False)
        service._update_progress(
            song_id,
            status=models.SongStatus.PROCESSING,
            percent=0.0,
            step_label="Подготавливаем karaoke-файл",
        )
        _prepare_symbolic(
            service,
            song_id,
            source_path,
            out_dir,
            state,
            reuse_existing_audio=reuse_existing_audio,
        )
        state.succeeded = True
    except service.ProcessingCancelled:
        _mark_cancelled(service, song_id)
        return
    except Exception as exc:
        _handle_failure(service, song_id, state.capture, exc)
        return
    finally:
        cleaned = _cleanup_common(service, state, song_id)
        if state.slot_acquired and (not state.succeeded or not cleaned):
            service._release_processing_slot(song_id)
    _finalize(service, state, song_id, out_dir)
    service._log_processing_finished(song_id, started_at)


def _start_media(service, song_id: str, source_path: str, out_dir: Path, artist, title):
    if not artist or not title:
        return None
    source_duration = None
    with contextlib.suppress(RuntimeError, OSError, ValueError):
        source_duration = float(sf.info(source_path).duration) or None
    return service.metadata_enrichment_service.start_training_media_process(
        title,
        artist,
        out_dir,
        expected_duration=source_duration,
    )


def _run_audio_pipeline(
    service,
    state: _Lifecycle,
    song_id: str,
    source_path: str,
    out_dir: Path,
    inputs: tuple,
    processing_mode: str,
    reuse_vocals: bool,
):
    artist, title, genre, lyrics_path, bpm_override, key_override = inputs
    if not reuse_vocals and service._audio_artifacts_waiting_for_media(out_dir):
        service._update_progress(
            song_id,
            step_label="Повторно загружаем и проверяем клип",
            percent=98.0,
        )
        state.capture.write("[backend] Valid AI artifacts found; retrying media only\n")
        return None
    service._ensure_cover_extracted(source_path, out_dir)
    runtime_plan = service._configure_ai_runtime()
    service._update_progress(song_id, step_label="Проверка AI-моделей", percent=1.0)
    service.model_install_service.ensure_ready_sync(
        cancelled=lambda: service._is_cancelled(song_id)
    )
    state.capture.write(
        f"[backend] AI build={service.AI_BUILD_ID} "
        f"pipeline={service.AudioPipelineV2.VERSION} "
        f"decoder={service.NOTE_DECODER_VERSION} "
        f"pitch={service.PITCH_STABILIZER_VERSION}\n"
    )
    state.capture.write(f"[backend] AI module={Path(service.__file__).resolve()}\n")
    for line in service.format_runtime_plan(runtime_plan):
        state.capture.write(f"[backend] AI runtime: {line}\n")
    with service._limit_background_native_threads():
        return service._invoke_ai_pipeline(
            song_id,
            source_path,
            out_dir,
            lyrics_path,
            artist,
            title,
            genre,
            bpm_override,
            key_override,
            processing_mode,
            state.capture,
            reuse_vocals=reuse_vocals,
        )


def _finish_audio_media(service, state, song_id, out_dir, artist, title) -> None:
    prepared = (
        state.media_process.result(cancelled=lambda: service._is_cancelled(song_id))
        if state.media_process
        else None
    )
    service._complete_audio_media(
        song_id,
        out_dir,
        artist=artist,
        title=title,
        prepared=prepared,
    )


def _report_result(service, state: _Lifecycle, song_id: str, result) -> None:
    warnings = getattr(result, "warnings", ())
    for warning in warnings if isinstance(warnings, (list, tuple)) else ():
        service.logger.warning(
            "Song processing warning: song_id=%s warning=%s", song_id, warning
        )
    reports = getattr(result, "reports", ())
    service._write_stage_reports(
        state.capture,
        reports if isinstance(reports, (list, tuple)) else (),
    )


def _cleanup_audio(service, state: _Lifecycle, song_id: str, lyrics_path) -> bool:
    try:
        if state.capture is not None:
            state.capture.close()
        if (
            lyrics_path is not None
            and lyrics_path.parent == config.CACHE_DIR / "trusted-lyrics"
        ):
            lyrics_path.unlink(missing_ok=True)
        service._stop_progress_heartbeat(
            state.heartbeat_stop,
            state.heartbeat_thread,
        )
        service._end_runtime_progress(song_id)
        if state.media_process is not None:
            state.media_process.close()
        return True
    except Exception:
        service.logger.exception(
            "Song processing cleanup failed: song_id=%s", song_id
        )
        return False


def run_audio(
    service,
    song_id: str,
    source_path: str,
    out_dir: Path,
    *,
    processing_mode: str,
    reuse_vocals: bool,
) -> None:
    artist, title = service._load_song_identity(song_id)
    genre = service._load_song_genre(song_id)
    lyrics_path, bpm_override, key_override = service._load_ai_inputs(song_id, out_dir)
    inputs = (artist, title, genre, lyrics_path, bpm_override, key_override)
    state, started_at = _Lifecycle(), time.monotonic()
    try:
        _begin(service, state, song_id, out_dir)
        if not state.slot_acquired:
            return
        service._log_processing_started(song_id, processing_mode, reuse_vocals)
        service._update_progress(
            song_id,
            status=models.SongStatus.PROCESSING,
            percent=0.0,
            step_label="0/13",
        )
        if not reuse_vocals:
            state.media_process = _start_media(
                service, song_id, source_path, out_dir, artist, title
            )
        result = _run_audio_pipeline(
            service,
            state,
            song_id,
            source_path,
            out_dir,
            inputs,
            processing_mode,
            reuse_vocals,
        )
        if not reuse_vocals:
            _finish_audio_media(service, state, song_id, out_dir, artist, title)
        _report_result(service, state, song_id, result)
        state.succeeded = True
    except service.ProcessingCancelled:
        _mark_cancelled(service, song_id)
        return
    except Exception as exc:
        _handle_failure(service, song_id, state.capture, exc)
        return
    finally:
        cleaned = _cleanup_audio(service, state, song_id, lyrics_path)
        if state.slot_acquired and (not state.succeeded or not cleaned):
            service._release_processing_slot(song_id)
    _finalize(service, state, song_id, out_dir)
    service._log_processing_finished(song_id, started_at)


def run(
    service,
    song_id: str,
    processing_mode: str = "auto",
    *,
    reuse_vocals: bool = False,
) -> None:
    paths = service._load_job_paths(song_id)
    if paths is None or service._is_cancelled(song_id):
        return
    if service._reject_full_process_if_source_retired(
        song_id, reuse_vocals=reuse_vocals
    ):
        return
    source_path, out_dir = paths
    service.recover_orphaned_backups(out_dir)
    if Path(source_path).suffix.casefold() in config.ALLOWED_KARAOKE_EXTENSIONS:
        if reuse_vocals:
            service._run_symbolic_job(
                song_id,
                source_path,
                out_dir,
                reuse_existing_audio=True,
            )
        else:
            service._run_symbolic_job(song_id, source_path, out_dir)
        return
    run_audio(
        service,
        song_id,
        source_path,
        out_dir,
        processing_mode=processing_mode,
        reuse_vocals=reuse_vocals,
    )
