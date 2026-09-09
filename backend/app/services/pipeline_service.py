
import contextlib
import gc
import io
import logging
import math
import os
import re
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import soundfile as sf
from sqlalchemy.orm import Session

import config
import models
from AI.artifacts import recover_orphaned_backups
from AI.audio_pipeline_v2 import AudioPipelineV2, validate_audio_artifacts
from AI.cache import StageCache
from AI.notes import NOTE_DECODER_VERSION
from AI.pitch_post import PITCH_STABILIZER_VERSION
from AI.runtime import RuntimePlan, configure_runtime, format_runtime_plan
from AI.version import AI_BUILD_ID
from app import repositories
from app.services import (
    ai_bridge,
    app_settings_service,
    cache_service,
    kar_dataset_service,
    kfn_dataset_service,
    metadata_enrichment_service,
    model_install_service,
    pipeline_job_runner,
    revision_cache,
    song_service,
    storage_budget_service,
)
from app.services._metadata import first_audio_tag
from app.services.db_utils import commit
from app.utils.json_files import read_json, write_json
from database import SessionLocal

_first_audio_tag = first_audio_tag

logger = logging.getLogger(__name__)

_STEP_RE = re.compile(r"(?P<step>\d+(?:\.\d+)?)\s*/\s*13")
_NOISY_PROGRESS_RE = re.compile(
    r"(?:warning|traceback|token_id|generation[_ ](?:config|flags)|transformers_verbosity|deprecated|onnxruntime|cudaexecutionprovider)",
    re.IGNORECASE,
)

# песни, которые прямо сейчас обрабатываются (song_id -> Thread) — чтобы не
# запускать одну и ту же песню повторно, пока предыдущий запуск не завершился
_active_jobs: dict[str, threading.Thread] = {}
_active_jobs_lock = threading.RLock()
_processing_condition = threading.Condition(threading.RLock())
_processing_queue: list[str] = []
_processing_active: set[str] = set()
_cancelled_jobs: set[str] = set()
_accepting_jobs = True
_progress_runtime: dict[str, dict] = {}
_progress_runtime_lock = threading.RLock()


def _configure_ai_runtime() -> RuntimePlan:
    config.configure_ai_resource_environment(force=True)
    settings = app_settings_service.read_settings()
    configured_device, override = str(settings['compute_mode']), os.getenv('KARAOKE_AI_RUNTIME_OVERRIDE', '').strip().lower()
    device = override if override in {'auto', 'cuda', 'cpu'} else configured_device
    configured_threads = int(settings['thread_count'])
    thread_count = configured_threads
    try:
        import psutil

        physical = int(psutil.cpu_count(logical=False) or os.cpu_count() or 2)
        reserve = 2 if physical >= 6 else 1
        thread_count = min(configured_threads, max(1, physical - reserve))
        process = psutil.Process()
        if os.name == "nt":
            process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            process.nice(max(int(process.nice()), 5))
    except (ImportError, OSError, ValueError, TypeError):
        pass
    os.environ["SONGAPP_DEVICE"] = device
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"): os.environ[name] = str(thread_count)
    with contextlib.suppress(ImportError, RuntimeError):
        import torch

        torch.set_num_threads(thread_count)
    logger.info(
        "AI background resources: configured_threads=%s effective_threads=%s priority=background",
        configured_threads, thread_count,
    )
    return configure_runtime(device, force=True)


@contextlib.contextmanager
def _limit_background_native_threads() -> Iterator[None]:
    """Apply the configured background limit to already-loaded BLAS pools."""
    limit = max(1, int(os.environ.get("OMP_NUM_THREADS", "1")))
    try:
        from threadpoolctl import threadpool_limits
    except ImportError:
        yield
        return
    with threadpool_limits(limits=limit):
        yield

def _apply_source_metadata(song: models.Song) -> None:
    try:
        from mutagen import File as MutagenFile

        tags = MutagenFile(song.source_path, easy=True)
    except Exception:
        tags = None

    if not song.genre:
        song.genre = first_audio_tag(
            tags, "genre") if tags is not None else None


# The expensive AI stages receive a larger share of the indicator.  This makes
# progress meaningful instead of pretending that thirteen very different jobs
# each take the same amount of time.
_STEP_PLAN = {
    1.0: (0.0, 3.0, 3),
    2.0: (3.0, 7.0, 12),
    3.0: (7.0, 28.0, 120),
    3.5: (28.0, 33.0, 20),
    4.0: (33.0, 39.0, 18),
    5.0: (39.0, 58.0, 90),
    6.0: (58.0, 65.0, 16),
    7.0: (65.0, 69.0, 14),
    8.0: (69.0, 79.0, 70),
    9.0: (79.0, 89.0, 70),
    9.5: (89.0, 92.0, 12),
    10.0: (92.0, 95.0, 10),
    11.0: (95.0, 97.0, 8),
    11.5: (97.0, 98.0, 8),
    12.0: (98.0, 99.0, 7),
    13.0: (99.0, 99.7, 5),
}

# AI Core reports semantic stages instead of the historical N/13 counter.
# Each entry describes the next observable boundary, a conservative duration
# and text intended for a person rather than an internal engine log.
_AI_STAGE_PLAN = {
    "decode": (10.0, 5, "Подготавливаем аудио"),
    "separate": (42.0, 70, "Отделяем голос исполнителя от музыки"),
    "vocal": (48.0, 10, "Очищаем голос и переводим его в моно"),
    "analysis": (70.0, 18, "Определяем темп, тональность и мелодию голоса"),
    "transcribe": (84.0, 30, "Распознаём текст вокала"),
    "align": (94.0, 20, "Синхронизируем слова с голосом"),
    "notes": (98.0, 8, "Строим вокальные ноты"),
    "validate": (99.7, 3, "Проверяем результат"),
    "complete": (100.0, 1, "Завершаем обработку"),
    "karaoke_parse": (6.0, 3, "Читаем слова и ноты караоке-файла"),
    "karaoke_audio": (34.0, 45, "Ищем и проверяем оригинальную песню"),
    "karaoke_media": (97.0, 90, "Подготавливаем обложку и клип"),
}


class ProcessingCancelled(RuntimeError):
    pass


class _ProgressCapture(io.TextIOBase):

    def __init__(self, song_id: str, log_path: Path):
        self._song_id = song_id
        self._lock = threading.RLock()
        self._closed = False
        self._log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115

    def write(self, text: str) -> int:
        with self._lock:
            if self._closed: raise ValueError("I/O operation on closed progress capture")
            if _is_cancelled(self._song_id): raise ProcessingCancelled("Processing cancelled by user")
            tagged = "".join(
                f"[song:{self._song_id}] {line}\n" for line in text.splitlines() if line.strip()
            )
            self._log_file.write(tagged)
            self._log_file.flush()
            if match := _STEP_RE.search(text):
                step = match.group("step")
                _set_runtime_step(self._song_id, float(step), text)
                _update_progress(
                    self._song_id,
                    step_label=f"{step}/13",
                    percent=_percent_from_step(step),
                )
            elif text.strip(): _set_runtime_detail(self._song_id, text)
            return len(text)

    def flush(self) -> None:
        with self._lock:
            if not self._closed: self._log_file.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed: return
            self._closed = True
            self._log_file.close()


def _percent_from_step(step_str: str) -> float:
    try:
        step = float(step_str)
        return _STEP_PLAN.get(step, (min(99.0, step / 13.0 * 100.0), 100.0, 1))[0]
    except ValueError:
        return 0.0


def _set_runtime_step(song_id: str, step: float, log_line: str) -> None:
    detail = log_line.strip().splitlines()[0]
    detail = re.sub(r"^\d+(?:\.\d+)?\s*/\s*13\s*", "", detail).strip(" .—-")
    with _progress_runtime_lock:
        now = time.monotonic()
        runtime = _progress_runtime.setdefault(
            song_id,
            {
                "started_at": now,
                "step": 0.0,
                "step_started_at": now,
                "completed_step_seconds": {},
            },
        )
        previous_step = float(runtime.get("step", 0.0))

        if step <= previous_step:
            runtime["detail"] = detail[:120]
            return

        if previous_step in _STEP_PLAN:
            completed = runtime.setdefault("completed_step_seconds", {})
            completed[previous_step] = max(
                0.0, now - runtime.get("step_started_at", now))
        runtime.update(
            {"step": step, "step_started_at": now, "detail": detail[:120]})


def _set_runtime_detail(song_id: str, log_text: str) -> None:
    detail = log_text.strip().splitlines()[-1].strip()
    if not detail or len(detail) > 160 or _NOISY_PROGRESS_RE.search(detail): return
    with _progress_runtime_lock:
        if runtime := _progress_runtime.get(song_id):
            if "direct_percent" in runtime: return
            runtime["detail"] = detail


def _begin_runtime_progress(song_id: str) -> None:
    now = time.monotonic()
    with _progress_runtime_lock:
        _progress_runtime[song_id] = {
            "started_at": now,
            "step": 0.0,
            "step_started_at": now,
            "completed_step_seconds": {},
            "completed_stage_seconds": {},
            "detail": "Подготавливаем обработку песни",
        }


def _end_runtime_progress(song_id: str) -> None:
    with _progress_runtime_lock: _progress_runtime.pop(song_id, None)


def _runtime_speed_factor(completed_steps: dict) -> float:
    expected_total, actual_total = 0.0, 0.0
    for completed_step, seconds in completed_steps.items():
        plan = _STEP_PLAN.get(float(completed_step))
        if plan is None: continue
        expected_total += plan[2]
        actual_total += float(seconds)
    return 1.0 if expected_total < 10.0 or actual_total <= 0 else min(3.0, max(0.25, actual_total / expected_total))


def _remaining_seconds(step: float, expected: float, elapsed: float, speed_factor: float) -> int:
    remaining = max(0.0, expected * speed_factor - elapsed)
    remaining += sum(
        seconds * speed_factor
        for plan_step, (_, _, seconds) in _STEP_PLAN.items()
        if plan_step > step
    )
    return max(1, int(round(remaining)))


def get_processing_telemetry(song_id: str) -> dict:
    with _progress_runtime_lock: runtime = dict(_progress_runtime.get(song_id, {}))
    if not runtime: return {}

    if "direct_percent" in runtime:
        now = time.monotonic()
        stage = str(runtime.get("stage") or "")
        base = float(runtime.get("direct_percent", 0.0))
        next_percent, expected, _label = _AI_STAGE_PLAN.get(
            stage, (min(99.7, base + 1.0), 10,
                    runtime.get("detail") or "Обрабатываем песню")
        )
        elapsed = max(0.0, now - float(runtime.get("stage_started_at", now)))
        # GPU separation, CPU lyrics and short validation stages do not share
        # one speed factor. Applying the duration of one completed stage to all
        # later stages made ETA jump from seconds to minutes and back again.
        scale = max(1.0, expected)
        fraction = 1.0 - math.exp(-2.0 * elapsed / scale)
        percent = base + (next_percent - base) * fraction
        percent = min(next_percent - 0.1,
                      percent) if next_percent > base else base
        stage_names = list(_AI_STAGE_PLAN)
        try:
            stage_index = stage_names.index(stage)
        except ValueError:
            stage_index = len(stage_names) - 1
        future_seconds = sum(
            _AI_STAGE_PLAN[name][1] for name in stage_names[stage_index + 1:]
        )
        overdue = elapsed > max(15.0, expected * 1.25)
        remaining = max(0.0, expected - elapsed) + future_seconds
        eta_seconds = None if overdue else max(1, int(round(remaining)))
        total_elapsed = max(0.0, now - float(runtime.get("started_at", now)))
        estimated_finish = (
            datetime.now().astimezone() + timedelta(seconds=eta_seconds)
            if eta_seconds is not None else None
        )
        return {
            "step": float(runtime.get("step", 0.0)),
            "progress_percent": round(min(99.7, percent), 1),
            "progress_detail": runtime.get("detail"),
            "eta_seconds": eta_seconds,
            "stage": stage,
            "stage_elapsed_seconds": int(round(elapsed)),
            "total_elapsed_seconds": int(round(total_elapsed)),
            "estimated_finish_at": (
                estimated_finish.isoformat(timespec="seconds")
                if estimated_finish is not None else None
            ),
            "semantic": True,
        }

    now, step = time.monotonic(), float(runtime.get('step', 0.0))
    if step <= 0:
        return {
            "step": 0.0,
            "progress_percent": 0.5,
            "progress_detail": runtime.get("detail"),
            "eta_seconds": None,
        }
    base, end, expected = _STEP_PLAN.get(step, (0.0, 1.0, 10))
    elapsed = max(0.0, now - runtime.get("step_started_at", now))
    fraction, speed_factor = min(0.94, elapsed / max(1, expected)), _runtime_speed_factor(runtime.get('completed_step_seconds', {}))
    return {
        "step": step,
        "progress_percent": round(base + (end - base) * fraction, 1),
        "progress_detail": runtime.get("detail"),
        "eta_seconds": _remaining_seconds(step, expected, elapsed, speed_factor),
    }


def _progress_heartbeat(song_id: str, stop_event: threading.Event) -> None:
    while not stop_event.wait(1.0):
        try:
            if telemetry := get_processing_telemetry(song_id):
                step = telemetry["step"]
                detail = telemetry.get("progress_detail") or "Обработка AI"
                label = detail if telemetry.get(
                    "semantic") else f"{step:g}/13 · {detail}"
                _update_progress(
                    song_id,
                    step_label=label,
                    percent=telemetry["progress_percent"],
                )
                if telemetry.get("semantic"):
                    should_log = False
                    with _progress_runtime_lock:
                        if runtime := _progress_runtime.get(song_id):
                            now = time.monotonic()
                            last = float(runtime.get("last_stage_log_at", 0.0))
                            if now - last >= 15.0:
                                runtime["last_stage_log_at"] = now
                                should_log = True
                    if should_log:
                        logger.info(
                            "AI stage active: song_id=%s stage=%s elapsed_sec=%s "
                            "total_elapsed_sec=%s eta_sec=%s estimated_finish=%s",
                            song_id, telemetry.get("stage"),
                            telemetry.get("stage_elapsed_seconds"),
                            telemetry.get("total_elapsed_seconds"),
                            telemetry.get("eta_seconds"),
                            telemetry.get("estimated_finish_at"),
                        )
        except Exception:  # A transient SQLite error must not kill telemetry forever.
            logger.warning(
                "Could not persist pipeline heartbeat for song %s", song_id, exc_info=True
            )


@contextlib.contextmanager
def _song_session(song_id: str) -> Iterator[tuple[Session, models.Song | None]]:
    db = SessionLocal()
    try:
        yield db, repositories.get_song(db, song_id)
    finally:
        db.close()


def _update_progress(
    song_id: str,
    step_label: str | None = None,
    percent: float | None = None,
    status: models.SongStatus | None = None,
    error_message: str | None = None,
) -> None:
    with _song_session(song_id) as (db, song):
        if song is None: return
        if step_label is not None: song.progress_step = step_label
        if percent is not None: song.progress_percent = percent
        if status is not None:
            # Log-only, not enforced: this funnel runs deep inside job
            # recovery/error handling, where refusing the write could leave a
            # song stuck in a non-terminal status forever -- worse than an
            # occasional transition this table doesn't yet recognize.
            current_status = getattr(song, "status", None)
            try:
                song_service.validate_status_transition(current_status, status)
            except song_service.InvalidStatusTransition:
                logger.warning(
                    "Unexpected song status transition: song_id=%s %s -> %s",
                    song_id, current_status, status,
                )
            song.status = status
        if error_message is not None: song.error_message = error_message
        commit(db)


def is_processing(song_id: str) -> bool:
    with _active_jobs_lock:
        thread = _active_jobs.get(song_id)
        return thread is not None and thread.is_alive()


def has_active_jobs() -> bool:
    with _active_jobs_lock: return any(thread.is_alive() for thread in _active_jobs.values())


def cancel_all_active_processing() -> int:
    """Cooperatively cancel every song currently processing.

    Used on app shutdown so a job gets a chance to reach a clean CANCELLED
    state (and release its AI resources) instead of being killed mid-write
    a moment later when Electron force-terminates the backend process.
    """
    with _active_jobs_lock:
        song_ids = [song_id for song_id, thread in _active_jobs.items() if thread.is_alive()]
    for song_id in song_ids: cancel_processing(song_id)
    return len(song_ids)


def start_accepting_jobs() -> None:
    global _accepting_jobs
    with _active_jobs_lock:
        _accepting_jobs = True


def stop_accepting_jobs() -> None:
    global _accepting_jobs
    with _active_jobs_lock:
        _accepting_jobs = False


def shutdown_active_processing(timeout: float = 15.0) -> dict[str, object]:
    """Stop admission, cancel active jobs and wait for their finalizers.

    The wait is bounded so a third-party AI call cannot deadlock application
    shutdown. Any thread left after the deadline remains visible in the result
    and in logs instead of being silently treated as successfully stopped.
    """
    stop_accepting_jobs()
    with _active_jobs_lock:
        active = {
            song_id: thread
            for song_id, thread in _active_jobs.items()
            if thread.is_alive()
        }
    for song_id in active:
        try:
            cancel_processing(song_id)
        except Exception:
            logger.exception("Could not request cancellation for %s", song_id)

    deadline = time.monotonic() + max(0.0, timeout)
    for thread in active.values():
        if thread is threading.current_thread():
            continue
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    lingering = [song_id for song_id, thread in active.items() if thread.is_alive()]
    if lingering:
        logger.error("AI jobs exceeded shutdown deadline: %s", ", ".join(lingering))
    return {
        "requested": len(active),
        "finished": len(active) - len(lingering),
        "lingering": lingering,
    }


def _release_active_job(song_id: str) -> None:
    with _active_jobs_lock:
        if _active_jobs.get(song_id) is threading.current_thread(): _active_jobs.pop(song_id, None)


def _job_entrypoint(song_id: str, target, storage_reservation=None) -> None:
    try:
        target(song_id)
    finally:
        cancelled = _is_cancelled(song_id)
        if cancelled:
            try:
                _update_progress(
                    song_id,
                    status=models.SongStatus.CANCELLED,
                    step_label="Cancelled",
                    error_message=None,
                )
            except Exception:
                logger.exception(
                    "Could not persist terminal cancellation for %s", song_id)
            with contextlib.suppress(Exception):
                ai_bridge.release_ai_resources()
        with _active_jobs_lock: _cancelled_jobs.discard(song_id)
        _release_active_job(song_id)
        # gc.collect() walks every Python object and holds the GIL for the
        # duration -- on a machine actively recording, that blocks the
        # realtime capture callback (also plain Python) right when a
        # background song just finished processing, a real source of
        # overflow/glitches. CPython's normal generational GC still runs on
        # its own; this only skips the extra, non-essential explicit sweep.
        from app.services import recording_service

        if not recording_service.has_live_capture():
            gc.collect()
            torch = sys.modules.get("torch")
            with contextlib.suppress(AttributeError, RuntimeError):
                if torch is not None and torch.cuda.is_available(): torch.cuda.empty_cache()
        if storage_reservation is not None:
            storage_reservation.release()


def _start_background_job(song_id: str, target, *, storage_reservation=None) -> bool:
    with _active_jobs_lock:
        if not _accepting_jobs:
            if storage_reservation is not None: storage_reservation.release()
            return False
        _cancelled_jobs.discard(song_id)
        if is_processing(song_id):
            if storage_reservation is not None: storage_reservation.release()
            return False
        thread = threading.Thread(
            target=_job_entrypoint,
            args=(song_id, target, storage_reservation),
            daemon=True,
        )
        _active_jobs[song_id] = thread
        try:
            thread.start()
        except Exception:
            _active_jobs.pop(song_id, None)
            if storage_reservation is not None: storage_reservation.release()
            raise
        return True


def start_processing(song_id: str, processing_mode: str = "auto") -> bool:
    paths = _load_job_paths(song_id)
    reservation = None
    if paths is not None:
        source_path, out_dir = paths
        reservation = storage_budget_service.reserve(
            "song_processing",
            out_dir,
            storage_budget_service.processing_bytes(Path(source_path)),
        )
    return _start_background_job(
        song_id,
        lambda current_song_id: _run_job(current_song_id, processing_mode),
        storage_reservation=reservation,
    )


def start_reprocessing(song_id: str) -> bool:
    paths = _load_job_paths(song_id)
    reservation = None
    if paths is not None:
        _source_path, out_dir = paths
        vocals = out_dir / "vocals.flac"
        reservation = storage_budget_service.reserve(
            "song_reprocessing",
            out_dir,
            storage_budget_service.processing_bytes(vocals, reuse_vocals=True),
        )
    return _start_background_job(
        song_id, _run_reprocessing, storage_reservation=reservation
    )


def cancel_processing(song_id: str) -> bool:
    with _active_jobs_lock:
        if not is_processing(song_id): return False
        _cancelled_jobs.add(song_id)
    with _processing_condition:
        _processing_condition.notify_all()
    _update_progress(
        song_id, status=models.SongStatus.CANCELLING, step_label="Cancelling")
    return True


def _is_cancelled(song_id: str) -> bool:
    with _active_jobs_lock: return song_id in _cancelled_jobs


def _load_job_paths(song_id: str) -> tuple[str, Path] | None:
    with _song_session(song_id) as (_db, song):
        if song is None: return None
        stored_output = getattr(song, "output_dir", None)
        out_dir = (
            song_service.resolve_output_dir(song)
            if isinstance(stored_output, (str, os.PathLike)) and str(stored_output)
            else config.SONG_OUTPUT_DIR / song.slug
        )
        return song.source_path, out_dir


def _load_ai_inputs(song_id: str, out_dir: Path) -> tuple[Path | None, float | None, str | None]:
    with _song_session(song_id) as (_db, song):
        if song is None: return None, None, None

        candidate_lyrics_path = out_dir / config.TRUSTED_LYRICS_FILENAME
        lyrics_path: Path | None = (
            candidate_lyrics_path
            if candidate_lyrics_path.is_file()
            and candidate_lyrics_path.read_text(encoding="utf-8-sig", errors="ignore").strip()
            else None
        )
        if lyrics_path is None:
            lyrics_payload = _read_optional_generated_json(out_dir / "lyricsSync.json", {})
            if isinstance(lyrics_payload, dict) and lyrics_payload.get("edited") and str(lyrics_payload.get("text") or "").strip():
                cached_lyrics = config.CACHE_DIR / "trusted-lyrics" / f"{song.id}.txt"
                cached_lyrics.parent.mkdir(parents=True, exist_ok=True)
                cached_lyrics.write_text(str(lyrics_payload["text"]).strip() + "\n", encoding="utf-8")
                lyrics_path = cached_lyrics

        tempo_value = getattr(song, "tempo_override", None)
        key_value = getattr(song, "key_override", None)
        tempo_edited = bool(
            getattr(song, "tempo_user_edited", tempo_value is not None))
        key_edited = bool(
            getattr(song, "key_user_edited", key_value is not None))

        bpm_override = float(
            tempo_value) if tempo_edited and tempo_value is not None else None
        key_override = str(key_value).strip(
        ) if key_edited and key_value else None
        return lyrics_path, bpm_override, key_override


def _load_searchable_title(song_id: str) -> str | None:
    with _song_session(song_id) as (_db, song):
        if song is None: return None
        artist = str(getattr(song, "artist", "") or "").strip()
        title = str(getattr(song, "title", "") or "").strip()
        if artist and title: return f"{artist} - {title}"
        return (title or artist or "").strip() or None


def _start_progress_heartbeat(song_id: str) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_progress_heartbeat,
        args=(song_id, stop_event),
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _create_progress_capture(song_id: str, out_dir: Path) -> _ProgressCapture:
    del out_dir
    config.APP_LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _ProgressCapture(song_id, config.APP_LOG_DIR / "application.log")


def _stop_progress_heartbeat(
    stop_event: threading.Event | None,
    thread: threading.Thread | None,
) -> None:
    if stop_event is None: return
    stop_event.set()
    if thread is not None and thread is not threading.current_thread(): thread.join(timeout=2.0)


def _format_processing_error(exc: BaseException) -> str:
    error_type, message = type(exc).__name__, str(exc).strip()
    return f"{error_type}: {message}" if message else error_type


def _write_pipeline_error(song_id: str, capture: _ProgressCapture | None, exc: Exception) -> None:
    logger.error("Song processing failed: song_id=%s error=%s",
                 song_id, _format_processing_error(exc), exc_info=exc)
    if capture is None: return
    with contextlib.suppress(OSError, ValueError):
        capture.write(
            f"\n[backend] ОШИБКА: {_format_processing_error(exc)}\n{traceback.format_exc()}\n"
        )


def _write_stage_reports(capture: _ProgressCapture, reports) -> None:
    for report in reports:
        capture.write(
            f"[AI] stage {report.stage} completed in {report.elapsed_sec:.2f}s "
            f"with {report.engine}\n"
        )


def _create_ai_progress_callback(
    song_id: str, capture: _ProgressCapture
) -> Callable[[str, float, str], None]:
    def on_ai_progress(stage: str, percent: float, detail: str) -> None:
        if _is_cancelled(song_id): raise ProcessingCancelled("Processing cancelled by user")
        bounded_percent, friendly = max(0.0, min(99.7, float(percent))), _AI_STAGE_PLAN.get(stage, (0, 0, 'Обрабатываем песню'))[2]
        finished_stage = None
        finished_elapsed = None
        started_stage = False
        with _progress_runtime_lock:
            if (runtime := _progress_runtime.get(song_id)) is not None:
                now = time.monotonic()
                bounded_percent = max(
                    bounded_percent, float(runtime.get("direct_percent", 0.0))
                )
                previous_stage = runtime.get("stage")
                if previous_stage and previous_stage != stage:
                    completed = runtime.setdefault(
                        "completed_stage_seconds", {})
                    completed[previous_stage] = max(
                        0.0, now - float(runtime.get("stage_started_at", now))
                    )
                    finished_stage = previous_stage
                    finished_elapsed = completed[previous_stage]
                if previous_stage != stage:
                    runtime["stage_started_at"] = now
                    runtime["last_stage_log_at"] = now
                    started_stage = True
                runtime["stage"] = stage
                runtime["direct_percent"] = bounded_percent
                runtime["detail"] = friendly
        _update_progress(song_id, step_label=friendly, percent=bounded_percent)
        capture.write(f"[AI] {bounded_percent:5.1f}% {stage} · {detail}\n")
        if finished_stage is not None:
            logger.info(
                "AI stage finished: song_id=%s stage=%s elapsed_sec=%.1f finished_at=%s",
                song_id, finished_stage, finished_elapsed,
                datetime.now().astimezone().isoformat(timespec="seconds"),
            )
        if started_stage:
            expected = _AI_STAGE_PLAN.get(stage, (0, 10, friendly))[1]
            estimated_finish = datetime.now().astimezone() + timedelta(seconds=expected)
            logger.info(
                "AI stage started: song_id=%s stage=%s expected_sec=%s estimated_finish=%s detail=%s",
                song_id, stage, expected,
                estimated_finish.isoformat(timespec="seconds"), friendly,
            )

    return on_ai_progress


def _ensure_cover_extracted(source_path: str, out_dir: Path) -> None:
    if any((out_dir / f"cover{ext}").is_file() for ext in (".jpg", ".png", ".webp")): return
    with contextlib.suppress(Exception): song_service.extract_embedded_cover(Path(source_path), out_dir)


def _acquire_processing_slot(song_id: str) -> bool:
    # A strict single global owner meant a purely CPU-bound stage of one
    # song (lyric search, librosa music analysis) could never run while any
    # other song held the one slot for its GPU stage, even for a different
    # user's unrelated song. Waiters still queue up and are admitted in
    # FIFO order, but now up to max_concurrent_jobs songs can hold a slot at
    # once, bounded so they don't outrun available GPU memory/compute.
    with _processing_condition:
        if song_id not in _processing_queue:
            _processing_queue.append(song_id)
        try:
            limit = ai_bridge.max_concurrent_jobs()
            while len(_processing_active) >= limit or _processing_queue[0] != song_id:
                if _is_cancelled(song_id):
                    _processing_queue.remove(song_id)
                    _processing_condition.notify_all()
                    return False
                _processing_condition.wait(timeout=0.2)
            _processing_queue.pop(0)
            _processing_active.add(song_id)
            return True
        except BaseException:
            # song_id was appended to the shared _processing_queue above
            # before anything here could fail (max_concurrent_jobs(),
            # _is_cancelled(), or the wait itself). Without this, one
            # transient failure left it stranded at the head of the queue
            # forever -- every future call for every OTHER song loops
            # forever on `_processing_queue[0] != song_id`, since nothing
            # else would ever remove the stranded entry.
            if song_id in _processing_queue:
                _processing_queue.remove(song_id)
            _processing_condition.notify_all()
            raise


def _restore_process_priority() -> None:
    # _configure_ai_runtime() lowers THIS WHOLE PROCESS's priority class
    # (there is no separate AI worker process -- pipeline jobs run as plain
    # threading.Thread inside the same backend process that also owns
    # recording/PortAudio/the API), and nothing previously ever restored it.
    # Once processing was lowered once, every realtime audio callback for
    # the rest of the app's lifetime -- including songs sung long after --
    # kept running at BELOW_NORMAL/niced priority too.
    try:
        import psutil

        process = psutil.Process()
        if os.name == "nt":
            process.nice(psutil.NORMAL_PRIORITY_CLASS)
        else:
            process.nice(0)
    except (ImportError, OSError, ValueError, TypeError):
        pass


def _release_processing_slot(song_id: str) -> None:
    with _processing_condition:
        _processing_active.discard(song_id)
        idle = not _processing_active and not _processing_queue
        _processing_condition.notify_all()
    if idle:
        _restore_process_priority()


def _invoke_ai_pipeline(
    song_id: str,
    source_path: str,
    out_dir: Path,
    lyrics_path: Path | None,
    artist: str | None,
    title: str | None,
    genre: str | None,
    bpm_override: float | None,
    key_override: str | None,
    processing_mode: str,
    capture: _ProgressCapture,
    *,
    reuse_vocals: bool,
):
    language = None if lyrics_path is not None else config.DEFAULT_LANGUAGE
    progress = _create_ai_progress_callback(song_id, capture)
    cancelled = lambda: _is_cancelled(song_id)  # noqa: E731
    if reuse_vocals:
        return ai_bridge.reprocess_song(
            out_dir, artist=artist, title=title, language=language,
            progress=progress, cancelled=cancelled,
        )
    return ai_bridge.process_song(
        source_path, out_dir, lyrics_path=lyrics_path,
        artist=artist, title=title, genre=genre, bpm_override=bpm_override,
        key_override=key_override, processing_mode=processing_mode,
        language=language, progress=progress, cancelled=cancelled,
    )


def _complete_audio_media(
    song_id: str,
    out_dir: Path,
    *,
    artist: str | None,
    title: str | None,
    prepared: dict[str, object] | None = None,
) -> None:
    if not artist or not title:
        raise ValueError("Для загрузки клипа нужны точные исполнитель и название")
    if _is_cancelled(song_id):
        raise ProcessingCancelled("Processing cancelled by user")
    metadata_path = out_dir / "metadata.json"
    metadata = _read_optional_generated_json(metadata_path, {})
    expected_duration = None
    if isinstance(metadata, dict):
        try:
            expected_duration = float(metadata.get("duration") or 0) or None
        except (TypeError, ValueError):
            expected_duration = None
    _update_progress(
        song_id,
        step_label="Загружаем и проверяем клип",
        percent=98.5,
    )
    media = prepared or metadata_enrichment_service.prepare_training_media(
        title, artist, out_dir, expected_duration=expected_duration
    )
    if media.get("cover_status") != "ready" or not (out_dir / "cover.jpg").is_file():
        raise ValueError("Не удалось загрузить и проверить обложку песни")
    video_ready = media.get("video_status") == "ready" and (
        metadata_enrichment_service.video_file_is_ready(
            out_dir / metadata_enrichment_service.LOCAL_VIDEO_NAME,
            expected_duration=expected_duration,
            allow_extended_duration=bool(media.get("video_id")),
        )
    )
    if not video_ready:
        # No valid clip is a supported result. The dedicated media process has
        # already finished at this point, so this cannot publish a partial
        # download while it is still in progress.
        (out_dir / metadata_enrichment_service.LOCAL_VIDEO_NAME).unlink(missing_ok=True)
        (out_dir / metadata_enrichment_service.LOCAL_VIDEO_SOURCE_NAME).unlink(missing_ok=True)
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["media"] = {
        "cover_status": "ready",
        "video_status": "ready" if video_ready else "fallback",
        "video_id": media.get("video_id") if video_ready else None,
    }
    warnings = media.get("warnings")
    metadata["warnings"] = list(warnings) if isinstance(warnings, list) else []
    files = set(metadata.get("files", [])) if isinstance(metadata.get("files"), list) else set()
    files.add("cover.jpg")
    if video_ready:
        files.update({"clip.mp4", "clip.source.json"})
    else:
        files.difference_update({"clip.mp4", "clip.source.json"})
    metadata["files"] = sorted(files)
    write_json(metadata_path, metadata)


def _audio_artifacts_waiting_for_media(out_dir: Path) -> bool:
    """Recognize an AI-complete job that failed only in optional clip media."""
    metadata = _read_optional_generated_json(out_dir / "metadata.json", {})
    media = metadata.get("media", {}) if isinstance(metadata, dict) else {}
    if not isinstance(media, dict) or media.get("video_status") != "pending":
        return False
    try:
        validate_audio_artifacts(out_dir)
    except (OSError, ValueError, TypeError):
        return False
    return True


def _finalize_processed_job(
    song_id: str,
    out_dir: Path,
    *,
    retain_source: bool = False,
) -> None:
    try:
        if not _is_cancelled(song_id):
            with song_service.library_write_lock():
                _finalize_success(song_id, out_dir, retain_source=retain_source)
            metadata_enrichment_service.enqueue(song_id)
    except Exception as exc:  # noqa: BLE001 - finalization is a worker boundary
        _update_progress(
            song_id,
            status=models.SongStatus.ERROR,
            error_message=f"Could not finalize processing results: {_format_processing_error(exc)}",
        )


def _source_unavailable_for_full_process(song_id: str) -> bool:
    with _song_session(song_id) as (_db, song):
        return song is not None and song_service.original_source_retired(song)


def _reject_full_process_if_source_retired(song_id: str, *, reuse_vocals: bool) -> bool:
    if reuse_vocals or not _source_unavailable_for_full_process(song_id): return False
    _update_progress(
        song_id,
        status=models.SongStatus.ERROR,
        error_message="Исходный файл песни был удалён после обработки — полная "
        "повторная обработка недоступна. Используйте переобработку мелодии или "
        "загрузите песню заново.",
    )
    return True


def _log_processing_started(song_id: str, processing_mode: str, reuse_vocals: bool) -> None:
    logger.info("Song processing started: song_id=%s mode=%s reuse_vocals=%s", song_id, processing_mode, reuse_vocals)


def _log_processing_finished(song_id: str, started_at: float) -> None:
    logger.info("Song processing finished: song_id=%s elapsed_sec=%.1f", song_id, time.monotonic() - started_at)


def _run_symbolic_job(
    song_id: str,
    source_path: str,
    out_dir: Path,
    *,
    reuse_existing_audio: bool = False,
) -> None:
    pipeline_job_runner.run_symbolic(
        sys.modules[__name__], song_id, source_path, out_dir,
        reuse_existing_audio=reuse_existing_audio,
    )


def _load_original_filename(song_id: str) -> str:
    with _song_session(song_id) as (_db, song):
        return str(getattr(song, "original_filename", "") or Path(song.source_path).name) if song else "song"


def _load_song_identity(song_id: str) -> tuple[str | None, str | None]:
    with _song_session(song_id) as (_db, song):
        if song is None:
            return None, None
        artist = str(getattr(song, "artist", "") or "").strip() or None
        title = str(getattr(song, "title", "") or "").strip() or None
        return artist, title


def _load_song_genre(song_id: str) -> str | None:
    with _song_session(song_id) as (_db, song):
        return (
            str(getattr(song, "genre", "") or "").strip() or None
            if song is not None else None
        )


def _run_job(song_id: str, processing_mode: str = "auto", *, reuse_vocals: bool = False) -> None:
    pipeline_job_runner.run(
        sys.modules[__name__],
        song_id,
        processing_mode,
        reuse_vocals=reuse_vocals,
    )


def _clear_generated_results(out_dir: Path) -> None:
    cache = StageCache(out_dir / ".ai-cache")
    cache.invalidate("pitch", "derivation")
    for pattern in ("*.json", "*.mid", "*.midi"):
        for path in out_dir.glob(pattern):
            if path.name in {"lyricsSync.json", "metadata.json"}: continue
            with contextlib.suppress(OSError): path.unlink(missing_ok=True)


def _run_reprocessing(song_id: str) -> None:
    with _song_session(song_id) as (_db, song):
        if song is None: return
        out_dir = song_service.resolve_output_dir(song)
    # A song's output_dir may live under a *historical* library root (the user
    # changed the storage location after the song was created) rather than the
    # current one — resolve_output_dir already trusts every root in
    # SONG_LIBRARY_ROOTS for reads, so this ownership check must trust the same
    # set, not just the current SONG_OUTPUT_DIR, or reprocessing would wrongly
    # reject perfectly readable/playable historical-root songs.
    target_dir = out_dir.resolve()
    if target_dir.parent not in song_service.trusted_library_roots():
        _update_progress(
            song_id,
            status=models.SongStatus.ERROR,
            error_message="Недопустимый путь к результатам песни",
        )
        return
    with song_service.song_content_lock(song_id):
        _clear_generated_results(out_dir)
    _run_job(song_id, reuse_vocals=True)


def _read_optional_generated_json(path: Path, default):
    try:
        return read_json(path, default=default)
    except (OSError, ValueError, TypeError):
        return default


def _apply_generated_metadata(song: models.Song, out_dir: Path) -> None:
    music = _read_optional_generated_json(out_dir / "lyricsSync.json", {})
    generated = _read_optional_generated_json(out_dir / "metadata.json", {})
    if (
        not getattr(song, "genre", None)
        and isinstance(generated, dict)
        and str(generated.get("genre") or "").strip()
    ):
        song.genre = str(generated["genre"]).strip()
    if isinstance(music, dict):
        key_user_edited = getattr(
            song, "key_user_edited", getattr(
                song, "key_override", None) is not None
        )
        tempo_user_edited = getattr(
            song, "tempo_user_edited", getattr(
                song, "tempo_override", None) is not None
        )
        if not key_user_edited and music.get("key"): song.key_override = music["key"]
        detected_bpm = music.get("bpm")
        if detected_bpm is not None and not tempo_user_edited: song.tempo_override = int(round(float(detected_bpm)))

    words = music.get("words", []) if isinstance(music, dict) else []
    if not isinstance(words, list): return
    try:
        midi = []
        for word in words:
            notes = word.get("notes", []) if isinstance(word, dict) else []
            if not isinstance(notes, list): continue
            for note in notes:
                if isinstance(note, dict) and (value := note.get("note")) is not None:
                    midi.append(int(value))
    except (TypeError, ValueError):
        return
    if not midi: return
    if getattr(song, "note_range_min", None) is None: song.note_range_min = min(midi)
    if getattr(song, "note_range_max", None) is None: song.note_range_max = max(midi)


def _persist_confirmed_identity(song: models.Song, out_dir: Path) -> None:
    path = out_dir / "lyricsSync.json"
    payload = _read_optional_generated_json(path, {})
    if not isinstance(payload, dict):
        raise ValueError("lyricsSync.json must contain an object")
    payload["title"] = str(song.title).strip()
    payload["artist"] = str(song.artist or "").strip()
    write_json(path, payload)


_MIN_ARTIFACT_DURATION_SEC = 0.02


def _validate_artifact_audio(path: Path) -> None:
    """Reject a processed output that exists but is empty/corrupt/undecodable.

    A crash mid-write, a full disk, or a broken AI/ffmpeg step can all leave
    an instrumental.flac/vocals.flac that passes a bare is_file() check but
    has no real audio in it -- catch that here, before the song is marked
    DONE, rather than leaving the user to discover it on first playback.
    """
    try:
        info = sf.info(str(path))
    except Exception as exc:
        raise ValueError(f"{path.name} is not a valid audio file") from exc
    if info.frames <= 0 or info.duration <= _MIN_ARTIFACT_DURATION_SEC:
        raise ValueError(f"{path.name} has no usable audio duration")


def _finalize_success(song_id: str, out_dir: Path, *, retain_source: bool = False) -> None:
    retired_source: Path | None = None
    for name in ("instrumental.flac", "vocals.flac"):
        _validate_artifact_audio(out_dir / name)
    with _song_session(song_id) as (db, song):
        if song is None: return
        song.output_dir = str(out_dir)
        _apply_source_metadata(song)
        _persist_confirmed_identity(song, out_dir)
        _apply_generated_metadata(song, out_dir)
        instrumental = (out_dir / "instrumental.flac").resolve()
        if (out_dir / metadata_enrichment_service.LOCAL_VIDEO_NAME).is_file():
            song.video_url = metadata_enrichment_service.LOCAL_VIDEO_URL
        source_value = getattr(song, "source_path", None)
        if source_value and instrumental.is_file():
            source = song_service.resolve_source_path(song).resolve()
            if source.parent == out_dir.resolve() and source.name.startswith("source."):
                song.source_path = str(instrumental)
                if not retain_source:
                    retired_source = source
        song.status = models.SongStatus.DONE
        song.optimized = False
        song.progress_percent = 100.0
        song.progress_step = "13/13"
        song.error_message = None
        commit(db)
        revision_cache.invalidate(song)

    if retired_source is not None:
        with contextlib.suppress(OSError): retired_source.unlink(missing_ok=True)
    with contextlib.suppress(Exception): cache_service.optimize_song_files(song_id)
