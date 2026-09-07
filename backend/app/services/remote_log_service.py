from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from AI.utils.env import env_flag
from config import IS_FROZEN

_LOG_UPLOAD_URL = os.getenv(
    "KARAOKE_LOG_COLLECTOR_URL",
    "https://karaoke-studio-online.pro100dimka-and.workers.dev/logs",
)

# Development already has the local application.log and terminal. Remote R2
# diagnostics are reserved for installed/frozen builds to avoid needless
# Cloudflare requests while running start-dev/start-web.
_DISABLED = env_flag("KARAOKE_LOG_COLLECTOR_DISABLED") or not IS_FROZEN
try:
    _FLUSH_DELAY_SECONDS = max(
        1.0, float(os.getenv("KARAOKE_LOG_FLUSH_DELAY_SECONDS", "30"))
    )
except ValueError:
    _FLUSH_DELAY_SECONDS = 30.0
_CLIENT_USER_AGENT = "A-and-D-Voice/0.3"
_MAX_PENDING_EVENTS = 500
_IDENTITY_LOCK = threading.Lock()
_BUFFER_LOCK = threading.RLock()
_PENDING_EVENTS: list[dict[str, str]] = []
_PENDING_HARDWARE: dict[str, Any] | None = None
_FLUSH_TIMER: threading.Timer | None = None
_RETRY_DELAY_SECONDS = 60.0
_CREDENTIALS_LOCK = threading.Lock()


def _remote_policy() -> dict[str, bool]:
    try:
        from app.services.app_settings_service import read_settings

        settings = read_settings()
    except Exception:
        settings = {}
    enabled = bool(settings.get("remote_diagnostics_enabled", False))
    return {
        "enabled": enabled,
        "errors": enabled and bool(settings.get("remote_diagnostics_errors_enabled", False)),
        "hardware": enabled and bool(settings.get("remote_diagnostics_hardware_enabled", False)),
        "crashes": enabled and bool(settings.get("remote_crash_reports_enabled", False)),
    }


def diagnostics_policy_preview() -> dict[str, Any]:
    """Return the exact opt-in categories without exposing queued log content."""
    return {
        **_remote_policy(),
        "collector": _LOG_UPLOAD_URL,
        "event_levels": ["WARNING", "ERROR"],
        "includes_device_id": True,
        "includes_online_name": True,
        "max_event_characters": 16_000,
    }


def clear_pending() -> None:
    global _FLUSH_TIMER, _PENDING_HARDWARE
    with _BUFFER_LOCK:
        if _FLUSH_TIMER is not None:
            _FLUSH_TIMER.cancel()
        _FLUSH_TIMER = None
        _PENDING_EVENTS.clear()
        _PENDING_HARDWARE = None


def apply_policy() -> None:
    """Apply a persisted consent change immediately, including revocation."""
    global _FLUSH_TIMER, _PENDING_HARDWARE
    policy = _remote_policy()
    if _DISABLED or not policy["enabled"]:
        clear_pending()
        return
    with _BUFFER_LOCK:
        if not policy["errors"]:
            _PENDING_EVENTS.clear()
        if not policy["hardware"]:
            _PENDING_HARDWARE = None
        if not _PENDING_EVENTS and _PENDING_HARDWARE is None and _FLUSH_TIMER is not None:
            _FLUSH_TIMER.cancel()
            _FLUSH_TIMER = None


def _persistent_device_id() -> str:
    from config import DATA_DIR

    path = Path(DATA_DIR) / "device-id"
    with _IDENTITY_LOCK:
        try:
            value = path.read_text(encoding="utf-8").strip()
            if value: return value
        except OSError:
            pass
        value = f"pc-{uuid.uuid4().hex[:12]}"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
        except OSError:
            pass
        return value


def _credentials_path() -> Path:
    from config import DATA_DIR

    return Path(DATA_DIR) / "remote-log-credentials.json"


def _read_credentials() -> dict[str, str] | None:
    try:
        value = json.loads(_credentials_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    device_id = str(value.get("device_id") or "") if isinstance(value, dict) else ""
    token = str(value.get("upload_token") or "") if isinstance(value, dict) else ""
    if not device_id.startswith("pc-") or len(device_id) != 27 or len(token) < 32:
        return None
    return {"device_id": device_id, "upload_token": token}


def _write_credentials(credentials: dict[str, str]) -> None:
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(credentials), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _register_credentials() -> dict[str, str] | None:
    request = urllib.request.Request(
        f"{_LOG_UPLOAD_URL}/register",
        data=b"{}",
        headers={"Content-Type": "application/json", "User-Agent": _CLIENT_USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        credentials = {
            "device_id": str(payload.get("device_id") or ""),
            "upload_token": str(payload.get("upload_token") or ""),
        }
        if (
            not credentials["device_id"].startswith("pc-")
            or len(credentials["device_id"]) != 27
            or len(credentials["upload_token"]) < 32
        ):
            return None
        _write_credentials(credentials)
        return credentials
    except Exception:
        return None


def _log_credentials() -> dict[str, str] | None:
    with _CREDENTIALS_LOCK:
        return _read_credentials() or _register_credentials()


def _current_online_name() -> str:
    try:
        from app.services.app_settings_service import read_settings

        value = str(read_settings().get("online_name") or "").strip()
        if value and "\ufffd" not in value and value.isprintable(): return value[:80]
    except Exception:
        pass
    return ""


def _send(payload: dict[str, Any]) -> bool:
    credentials = _log_credentials()
    if credentials is None:
        return False
    authenticated_payload = {**payload, "device_id": credentials["device_id"]}
    try:
        request = urllib.request.Request(
            _LOG_UPLOAD_URL,
            data=json.dumps(authenticated_payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {credentials['upload_token']}",
                "Content-Type": "application/json",
                "User-Agent": _CLIENT_USER_AGENT,
            },
            method="POST",
        )
        urllib.request.urlopen(request, timeout=10).close()
        return True
    except Exception:
        return False  # Remote diagnostics must never break the application.


def delete_remote_diagnostics() -> bool:
    """Delete the authenticated installation's Cloudflare record and local token."""
    credentials = _read_credentials()
    if credentials is None:
        clear_pending()
        return True
    request = urllib.request.Request(
        f"{_LOG_UPLOAD_URL}/{credentials['device_id']}",
        headers={
            "Authorization": f"Bearer {credentials['upload_token']}",
            "User-Agent": _CLIENT_USER_AGENT,
        },
        method="DELETE",
    )
    try:
        urllib.request.urlopen(request, timeout=10).close()
    except Exception:
        return False
    clear_pending()
    _credentials_path().unlink(missing_ok=True)
    return True


def _schedule_flush_locked(delay: float = _FLUSH_DELAY_SECONDS) -> None:
    global _FLUSH_TIMER
    if _FLUSH_TIMER is not None: _FLUSH_TIMER.cancel()
    timer: threading.Timer

    def flush_if_current() -> None:
        with _BUFFER_LOCK:
            if _FLUSH_TIMER is not timer: return
        flush_pending()

    timer = threading.Timer(delay, flush_if_current)
    timer.daemon = True
    _FLUSH_TIMER = timer
    timer.start()


def _take_pending() -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    global _FLUSH_TIMER, _PENDING_HARDWARE
    with _BUFFER_LOCK:
        timer = _FLUSH_TIMER
        events, hardware = list(_PENDING_EVENTS), _PENDING_HARDWARE
        _PENDING_EVENTS.clear()
        _PENDING_HARDWARE = None
        _FLUSH_TIMER = None
        if timer is not None and timer is not threading.current_thread(): timer.cancel()
        return events, hardware


def flush_pending() -> None:
    global _PENDING_HARDWARE, _RETRY_DELAY_SECONDS
    events, hardware = _take_pending()
    policy = _remote_policy()
    if _DISABLED or not policy["enabled"]: return
    if not policy["errors"]: events = []
    if not policy["hardware"]: hardware = None
    if not events and hardware is None: return
    payload = {
        "device_id": _persistent_device_id(),
        "display_name": _current_online_name(),
        "events": events,
        **({"hardware": hardware} if hardware is not None else {}),
    }
    if _send(payload):
        _RETRY_DELAY_SECONDS = 60.0
        return
    with _BUFFER_LOCK:
        # queue_log()/queue_hardware_snapshot() calls that arrived while
        # _send() above was in flight already scheduled their own near-term
        # timer (_take_pending cleared _FLUSH_TIMER before releasing the
        # lock for the network call) -- _PENDING_EVENTS/_PENDING_HARDWARE
        # being non-empty here means that happened. Without this, the
        # unconditional _schedule_flush_locked(retry_delay) below silently
        # replaces that fresh near-term timer with the full exponential
        # backoff delay (up to an hour), even though nothing about that new
        # event has actually failed to send yet.
        newly_queued = bool(_PENDING_EVENTS) or _PENDING_HARDWARE is not None
        _PENDING_EVENTS[:0] = events
        del _PENDING_EVENTS[:-_MAX_PENDING_EVENTS]
        if hardware is not None: _PENDING_HARDWARE = hardware
        retry_delay = _RETRY_DELAY_SECONDS
        _RETRY_DELAY_SECONDS = min(3600.0, _RETRY_DELAY_SECONDS * 2.0)
        delay = min(retry_delay, _FLUSH_DELAY_SECONDS) if newly_queued else retry_delay
        _schedule_flush_locked(delay)


def queue_hardware_snapshot(hardware: dict[str, Any]) -> None:
    if _DISABLED or not _remote_policy()["hardware"] or not hardware: return
    global _PENDING_HARDWARE
    with _BUFFER_LOCK:
        _PENDING_HARDWARE = hardware
        _schedule_flush_locked()


def queue_log(level: int, message: str) -> None:
    if (
        _DISABLED
        or not _remote_policy()["errors"]
        or level < logging.WARNING
        or not message.strip()
    ):
        return
    event = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": "ERROR" if level >= logging.ERROR else "WARNING",
        "message": message.strip()[:16_000],
    }
    with _BUFFER_LOCK:
        _PENDING_EVENTS.append(event)
        del _PENDING_EVENTS[:-_MAX_PENDING_EVENTS]
        _schedule_flush_locked()


class RemoteErrorLogHandler(logging.Handler):

    def __init__(self) -> None: super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        if _DISABLED or record.levelno < logging.WARNING: return
        try:
            message = self.format(record)
        except Exception:
            return
        queue_log(record.levelno, message)


atexit.register(flush_pending)
