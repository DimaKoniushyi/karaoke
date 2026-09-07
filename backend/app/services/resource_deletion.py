
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

import config
from app.services.db_utils import commit
from app.utils.json_files import read_json, write_json
from app.utils.quarantine import (
    existing_unique_paths,
    purge_quarantined_paths,
    quarantine_paths,
    restore_quarantined_paths,
)

logger = logging.getLogger(__name__)
_WINDOWS_SHARING_ERRORS = {5, 32, 33}
_cleanup_lock = threading.RLock()
_cleanup_worker: threading.Thread | None = None


def _pending_cleanup_file() -> Path:
    return config.DATA_DIR / "pending-file-deletions.json"


def _is_windows_sharing_error(exc: BaseException) -> bool:
    return getattr(exc, "winerror", None) in _WINDOWS_SHARING_ERRORS


def _allowed_recovery_path(path: Path) -> bool:
    """Reject hand-edited queue entries outside application-owned storage."""
    resolved = path.resolve()
    roots = {
        config.DATA_DIR.resolve(),
        config.CACHE_DIR.resolve(),
        config.SONG_OUTPUT_DIR.resolve(),
        *(Path(root).resolve() for root in config.SONG_LIBRARY_ROOTS),
    }
    return any(root != resolved and root in resolved.parents for root in roots)


def _read_pending_cleanup() -> set[Path]:
    try:
        payload = read_json(_pending_cleanup_file(), default=[])
    except (OSError, ValueError, TypeError):
        logger.warning("Could not read pending file-deletion queue", exc_info=True)
        return set()
    if not isinstance(payload, list): return set()
    return {Path(value) for value in payload if isinstance(value, str)}


def _write_pending_cleanup(paths: set[Path]) -> None:
    queue_file = _pending_cleanup_file()
    if not paths:
        queue_file.unlink(missing_ok=True)
        return
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    write_json(queue_file, sorted(str(path) for path in paths))


def _forget_pending_path(path: Path) -> None:
    with _cleanup_lock:
        pending = _read_pending_cleanup()
        pending.discard(path)
        _write_pending_cleanup(pending)


def _cleanup_paths_with_retry(paths: tuple[Path, ...], *, timeout: float = 120.0) -> None:
    global _cleanup_worker
    remaining = set(paths)
    deadline, delay = time.monotonic() + timeout, 0.15
    completed = False
    try:
        while remaining and time.monotonic() < deadline:
            for path in tuple(remaining):
                try:
                    quarantined = quarantine_paths((path,))
                    purge_quarantined_paths(quarantined)
                except OSError as exc:
                    if not _is_windows_sharing_error(exc):
                        logger.warning("Deferred file deletion failed: %s", path, exc_info=True)
                    continue
                remaining.remove(path)
                _forget_pending_path(path)
            if remaining:
                time.sleep(delay)
                delay = min(delay * 1.5, 2.0)
        completed = not remaining
        if remaining:
            logger.warning(
                "Files remain locked and will be deleted after the next application start: %s",
                ", ".join(str(path) for path in sorted(remaining, key=str)),
            )
    finally:
        with _cleanup_lock:
            _cleanup_worker = None
            queued_after_completion = tuple(_read_pending_cleanup()) if completed else ()
        # Close the tiny race where another request queues a path while this
        # worker is finishing but still reports itself as alive.
        if queued_after_completion:
            schedule_deferred_cleanup(queued_after_completion)


def schedule_deferred_cleanup(paths: tuple[Path, ...]) -> None:
    """Persist and retry paths that Windows still has open in another thread."""
    global _cleanup_worker
    paths = tuple(dict.fromkeys(Path(path).resolve() for path in paths))
    if not paths: return
    with _cleanup_lock:
        pending = _read_pending_cleanup()
        pending.update(paths)
        _write_pending_cleanup(pending)
        if _cleanup_worker is not None and _cleanup_worker.is_alive(): return
        _cleanup_worker = threading.Thread(
            target=_cleanup_paths_with_retry,
            args=(tuple(pending),),
            name="deferred-file-cleanup",
            daemon=True,
        )
        _cleanup_worker.start()


def recover_deferred_cleanup() -> None:
    """Resume safe, application-owned deletions left by a previous process."""
    pending = tuple(path.resolve() for path in _read_pending_cleanup() if _allowed_recovery_path(path))
    if pending: schedule_deferred_cleanup(pending)


def delete_with_files(
    db: Session,
    instance: Any,
    paths: Iterable[Path],
    *,
    defer_windows_locks: bool = False,
) -> None:
    existing = existing_unique_paths(paths)
    try:
        quarantined = quarantine_paths(existing)
    except OSError as exc:
        if not defer_windows_locks or not _is_windows_sharing_error(exc): raise
        db.delete(instance)
        commit(db)
        try:
            schedule_deferred_cleanup(existing)
        except Exception:
            # The database commit is already durable. Returning an error now
            # would make the UI resurrect a song that no longer exists.
            logger.error("Could not queue locked files for deferred cleanup", exc_info=True)
        logger.info("Song record deleted while Windows still held its files")
        return
    try:
        db.delete(instance)
        commit(db)
    except Exception as exc:
        try:
            restore_quarantined_paths(quarantined)
        except Exception as restore_error:  # pragma: no cover - rare OS failure
            exc.add_note(f"Could not restore quarantined files: {restore_error}")
        raise

    try:
        purge_quarantined_paths(quarantined)
    except OSError:
        logger.warning(
            "Database record was deleted, but quarantined files could not be purged",
            exc_info=True,
        )
