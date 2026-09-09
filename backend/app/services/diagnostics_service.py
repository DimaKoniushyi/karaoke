
from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import platform
import subprocess
import sys
from pathlib import Path

import config
from app.version import APP_VERSION

BACKEND_VERSION = APP_VERSION


def _build_id() -> str:
    for name in ("SONGAPP_BUILD_ID", "BUILD_ID", "GITHUB_SHA", "CI_COMMIT_SHA"):
        if value := os.environ.get(name, "").strip():
            return value
    candidates = (
        Path(sys.executable).resolve().parent / "build-identity.json",
        config.RUNTIME_DIR / "build-identity.json",
        config.PROJECT_ROOT / "build-identity.json",
    )
    for candidate in candidates:
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
            if document.get("version") == BACKEND_VERSION and document.get("build_id"):
                return str(document["build_id"])
        except (OSError, ValueError, TypeError):
            continue
    try:
        result = subprocess.run(
            ["git", "-C", str(config.PROJECT_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


BUILD_ID = _build_id()
_CLIENT_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}


def _ffmpeg_available() -> bool: return config.FFMPEG_EXE != 'ffmpeg'


def _package_available(distribution: str) -> bool:
    try:
        importlib.metadata.version(distribution)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def _torch_info() -> tuple[bool, bool, str | None]:
    try:
        import torch

        return True, torch.cuda.is_available(), torch.__version__
    except (ImportError, RuntimeError):
        return False, False, None


def _ai_package_available() -> bool:
    try:
        import AI  # noqa: F401
        from AI.service import AICoreService  # noqa: F401

        return True
    except ImportError:
        return False


def pipeline_health() -> dict:
    torch_available, cuda_available, _ = _torch_info()
    runtime = None
    try:
        from app.services import ai_bridge

        health = ai_bridge.get_ai_service().health()
        separator_available = bool(health.get("separation_configured"))
        runtime = health.get("runtime")
    except Exception:
        separator_available = False

    return {
        "ffmpeg_available": _ffmpeg_available(),
        "demucs_available": separator_available,
        "whisper_available": _package_available("qwen-asr"),
        "torch_available": torch_available,
        "cuda_available": cuda_available,
        "ai_dir_found": _ai_package_available(),
        "runtime": runtime,
    }


def versions() -> dict:
    _, _, torch_version = _torch_info()
    components: dict[str, str | None] = {"torch": torch_version}
    try:
        from AI.pipeline import KaraokePipeline
        from AI.version import AI_BUILD_ID

        components["ai_build"] = AI_BUILD_ID
        components["ai_pipeline"] = KaraokePipeline.VERSION
    except Exception:
        components["ai_build"] = None
        components["ai_pipeline"] = None

    try:
        result = subprocess.run(
            [config.FFMPEG_EXE, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        components["ffmpeg"] = result.stdout.splitlines()[0] if result.stdout else None
    except (OSError, subprocess.SubprocessError):
        components["ffmpeg"] = None

    for display, package in (
        ("qwen_asr", "qwen-asr"),
        ("torchfcpe", "torchfcpe"),
        ("librosa", "librosa"),
        ("soundfile", "soundfile"),
    ):
        try:
            components[display] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            components[display] = None

    return {
        "backend_version": BACKEND_VERSION,
        "build_id": BUILD_ID,
        "python_version": platform.python_version(),
        "components": components,
    }


def recent_errors(limit: int = 20) -> list[dict]:
    import models
    from database import SessionLocal

    db = SessionLocal()
    try:
        errored = (
            db.query(models.Song)
            .filter(models.Song.status == models.SongStatus.ERROR)
            .order_by(models.Song.updated_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "song_id": song.id,
                "title": song.title,
                "error_message": song.error_message,
                "updated_at": song.updated_at.isoformat() if song.updated_at else None,
            }
            for song in errored
        ]
    finally:
        db.close()


def record_client_log(entry) -> None:
    level, context = _CLIENT_LOG_LEVELS.get(str(entry.level or 'info').casefold(), logging.INFO), ' '.join(part for part in (f'user={entry.user}' if entry.user else '', f'at={entry.url}' if entry.url else '') if part)
    message = f"{context}: {entry.message}" if context else entry.message
    if entry.stack: message = f"{message}\n{entry.stack}"
    logging.getLogger(f"client.{entry.source or 'unknown'}").log(level, message)
