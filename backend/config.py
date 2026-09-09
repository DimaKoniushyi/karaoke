"""
Центральная конфигурация backend'а.

Всё, что завязано на пути к папкам/файлам, дефолты пайплайна и настройки
БД — собрано здесь в одном месте, чтобы не расползалось по роутерам и
сервисам. Все пути считаются от расположения этого файла (корень backend/),
поэтому программу можно установить/скопировать в любую директорию на диске
пользователя — ничего не захардкожено абсолютным путём.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from AI.model_registry import MODELS, model_path


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default


def _env_int(name: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
    """Read a bounded integer environment variable with an actionable error."""
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum or (maximum is not None and value > maximum):
        range_text = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise ValueError(f"{name} must be in range {range_text}, got {value}")
    return value


def _unique_csv(name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    """Read a comma-separated setting, trim it and preserve unique order."""
    raw = os.environ.get(name, ",".join(defaults))
    return tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


# --------------------------------------------------------------------
# Базовые пути
# --------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
IS_FROZEN = bool(getattr(sys, "frozen", False))
# PyInstaller places bundled read-only files in _MEIPASS.
RUNTIME_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
PROJECT_ROOT = BASE_DIR.parent


def resolve_runtime_executable(name: str) -> str:
    """Resolve packaged siblings deterministically before consulting the host PATH."""
    if name.lower().endswith('.exe'):
        names = [name]
    elif IS_FROZEN or os.name == 'nt':
        names = [f'{name}.exe', name]
    else:
        names = [name, f'{name}.exe']
    roots = (Path(sys.executable).resolve().parent, RUNTIME_DIR, BASE_DIR)
    for root in roots:
        for executable in names:
            candidate = root / executable
            if candidate.is_file(): return str(candidate)
    return shutil.which(name) or name


FFMPEG_EXE = resolve_runtime_executable("ffmpeg")


def _install_root() -> Path:
    """Resolve the writable installation root for packaged builds."""
    configured = os.environ.get("SONGAPP_INSTALL_ROOT")
    if configured: return Path(configured).expanduser().resolve()
    if IS_FROZEN:
        executable = Path(sys.executable).resolve()
        # Installed layout: <app>/resources/backend/KaraokeBackend.exe
        if executable.parent.name.casefold() == "backend" and executable.parent.parent.name.casefold() == "resources": return executable.parent.parent.parent
        return executable.parent
    return PROJECT_ROOT


def _per_user_fallback_root() -> Path:
    """Per-user directory that never requires admin rights to write to.

    Used only when the install root itself turns out to be read-only for the
    current user (e.g. an administrator pointed the installer at
    ``C:\\Program Files\\...``); this app's own generated data -- DB, song
    library, downloaded AI models -- must never require elevation just to run.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "A&D Voice"


def _writable_install_root() -> Path:
    """``_install_root()``, falling back to a per-user directory if unwritable."""
    root = _install_root()
    if not IS_FROZEN: return root
    probe = root / ".write-test"
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.touch()
        probe.unlink()
        return root
    except OSError:
        return _per_user_fallback_root()


def _default_data_dir() -> Path:
    return PROJECT_ROOT / 'data' if not IS_FROZEN else _writable_install_root() / 'data' / 'backend'


DATA_DIR = _env_path("SONGAPP_DATA_DIR", _default_data_dir())
DB_PATH = DATA_DIR / "app.db"
PATH_SETTINGS_FILE = DATA_DIR / "path-settings.json"


def _looks_like_previous_dev_checkout(path: Path, default: Path) -> bool:
    """Detect a saved project-relative path from a checkout on another drive.

    Development settings are persistent, so moving ``D:\\Git\\karaoke`` to
    another drive could otherwise keep writing songs into the old checkout.
    Deliberately selected external folders are preserved; only paths whose tail
    exactly matches this checkout's project-relative default are remapped.
    """
    if IS_FROZEN: return False
    try:
        relative = default.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return False
    tail, parts = tuple(part.casefold() for part in relative.parts), path.resolve().parts
    if not tail or len(parts) <= len(tail): return False
    if tuple(part.casefold() for part in parts[-len(tail) :]) != tail: return False
    old_root, current_root = Path(*parts[:-len(tail)]).resolve(), PROJECT_ROOT.resolve()
    return old_root != current_root and old_root.name.casefold() == current_root.name.casefold()


def _saved_path(name: str, default: Path) -> Path:
    """Read a user-selected path, ignoring stale paths from an old dev checkout."""
    try:
        raw = json.loads(PATH_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        raw = {}
    value = raw.get(name) if isinstance(raw, dict) else None
    if isinstance(value, str) and value.strip():
        selected = Path(value).expanduser().resolve()
        if not _looks_like_previous_dev_checkout(selected, default): return selected
    return default


AI_DIR = _env_path("SONGAPP_AI_DIR", RUNTIME_DIR / "AI")
DOWNLOADS_DIR = _env_path(
    "SONGAPP_DOWNLOADS_DIR",
    _writable_install_root() / "data" / "downloads" if IS_FROZEN else PROJECT_ROOT / "downloads",
)


# Packaged models stay under the selected installation root together with the app.
def _default_models_dir() -> Path: return DOWNLOADS_DIR / 'models' if not IS_FROZEN else _writable_install_root() / 'data' / 'models'


_DEFAULT_MODELS_DIR = _default_models_dir()
MODELS_DIR = _env_path("SONGAPP_MODELS_DIR", _saved_path("ai_folder", _DEFAULT_MODELS_DIR))
EXTERNAL_ENGINES_DIR = _env_path(
    "SONGAPP_ENGINES_DIR", RUNTIME_DIR / "engines" if IS_FROZEN else DOWNLOADS_DIR / "engines"
)
RECORDINGS_DIRNAME = "recordings"  # подпапка внутри Song/<slug>/ для записей пользователя
LOGS_DIRNAME = "logs"  # подпапка внутри Song/<slug>/ для логов обработки
TRUSTED_LYRICS_FILENAME = "trusted_lyrics.txt"

_DEFAULT_LOG_DIR = DATA_DIR / "generated" / "logs" if IS_FROZEN else PROJECT_ROOT / "generated" / "logs"
APP_LOG_DIR = _env_path("SONGAPP_LOG_DIR", _DEFAULT_LOG_DIR)

# Source uploads and generated artefacts live in one owned library. The original
# upload is replaced by the pipeline's normalized song.mp3 after processing, so
# the application never keeps a second full-song copy indefinitely.
_DEFAULT_SONGS_DIR = DATA_DIR / "karaoke_songs" if IS_FROZEN else PROJECT_ROOT / "karaoke_songs"
SONG_OUTPUT_DIR = _env_path(
    "SONGAPP_SONG_OUTPUT_DIR", _saved_path("songs_folder", _DEFAULT_SONGS_DIR)
)


def _saved_song_library_roots(current: Path) -> set[Path]:
    try:
        raw = json.loads(PATH_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        raw = {}
    values, roots = raw.get('song_library_roots', []) if isinstance(raw, dict) else [], {current.resolve()}
    if isinstance(values, list): roots.update(Path(value).expanduser().resolve() for value in values if isinstance(value, str) and value.strip())
    return roots


SONG_LIBRARY_ROOTS = _saved_song_library_roots(SONG_OUTPUT_DIR)
_DEFAULT_CACHE_DIR = DATA_DIR / "generated" / "temp" if IS_FROZEN else PROJECT_ROOT / "generated" / "temp"
CACHE_DIR = _env_path("SONGAPP_CACHE_DIR", _saved_path("cache_folder", _DEFAULT_CACHE_DIR))
UPLOAD_TEMP_DIR = CACHE_DIR / "uploads"


def configure_runtime_cache_environment() -> None:
    """Keep transient Python/AI artefacts inside the user-selected cache."""
    root = CACHE_DIR / "ai-runtime"
    paths = {
        "TEMP": root / "temp",
        "TMP": root / "temp",
        "HF_HOME": root / "huggingface",
        "HUGGINGFACE_HUB_CACHE": root / "huggingface" / "hub",
        "TORCH_HOME": root / "torch",
        "XDG_CACHE_HOME": root,
        "NUMBA_CACHE_DIR": root / "numba",
        "KERAS_HOME": root / "keras",
        "MPLCONFIGDIR": root / "matplotlib",
        "CUDA_CACHE_PATH": root / "cuda",
        "TRITON_CACHE_DIR": root / "triton",
        "TORCHINDUCTOR_CACHE_DIR": root / "torchinductor",
    }
    for name, path in paths.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    tempfile.tempdir = str(paths["TEMP"])


def apply_storage_paths(
    *,
    songs_folder: str | None = None,
    ai_folder: str | None = None,
    cache_folder: str | None = None,
    song_library_roots: list[str] | None = None,
) -> None:
    """Apply persisted user storage paths to the running backend process."""
    global SONG_OUTPUT_DIR, SONG_LIBRARY_ROOTS, MODELS_DIR, CACHE_DIR, UPLOAD_TEMP_DIR

    if songs_folder is not None:
        previous = SONG_OUTPUT_DIR.resolve()
        SONG_OUTPUT_DIR = Path(songs_folder).expanduser().resolve()
        SONG_LIBRARY_ROOTS.update({previous, SONG_OUTPUT_DIR.resolve()})
    if song_library_roots is not None:
        SONG_LIBRARY_ROOTS.update(
            Path(value).expanduser().resolve()
            for value in song_library_roots
            if isinstance(value, str) and value.strip()
        )
        SONG_LIBRARY_ROOTS.add(SONG_OUTPUT_DIR.resolve())
    if ai_folder is not None: MODELS_DIR = Path(ai_folder).expanduser().resolve()
    if cache_folder is not None: CACHE_DIR = Path(cache_folder).expanduser().resolve()
    UPLOAD_TEMP_DIR = CACHE_DIR / "uploads"
    ensure_directories()
    configure_runtime_cache_environment()
    configure_ai_resource_environment(force=True)


def configure_ai_resource_environment(*, force: bool = False) -> None:
    """Point the AI core at resources declared by the backend model registry."""
    # Every production inference engine in this project is PyTorch-based.
    # Transformers otherwise auto-detects the installed TensorFlow/Flax
    # packages and initializes those runtimes inside each spawned alignment
    # worker, adding seconds of startup and hundreds of MB of needless memory.
    os.environ.setdefault("USE_TORCH", "1")
    os.environ.setdefault("USE_TF", "0")
    os.environ.setdefault("USE_FLAX", "0")
    # HF_HOME/HUGGINGFACE_HUB_CACHE are configured centrally above. Keeping
    # the removed legacy variable makes current Transformers emit a warning
    # in every spawned inference worker.
    os.environ.pop("TRANSFORMERS_CACHE", None)
    msst = EXTERNAL_ENGINES_DIR / "msst"
    msst_config = msst / "configs" / "KimberleyJensen" / "config_vocals_mel_band_roformer_kj.yaml"

    def set_resource(name: str, path: Path, *, directory: bool = False) -> None:
        configured = os.environ.get(name)
        configured_path = Path(configured).expanduser() if configured else None
        configured_exists = (
            configured_path.is_dir()
            if configured_path and directory
            else configured_path.is_file()
            if configured_path
            else False
        )
        if (force or not configured_exists) and (path.is_dir() if directory else path.is_file()): os.environ[name] = str(path)

    # A model directory existing is not enough: interrupted/resumable installs can
    # leave config/index files without one of the weight shards. Never point the
    # AI core at an incomplete local snapshot.
    from AI.install_models import is_valid

    for model in MODELS:
        path = model_path(MODELS_DIR, model)
        # Production registry entries carry relative_path and can be validated
        # deeply. Lightweight test/dynamic resource descriptors keep the legacy
        # existence semantics.
        valid = (
            is_valid(MODELS_DIR, model)
            if hasattr(model, "relative_path")
            else (path.is_dir() if model.kind in {"snapshot", "bundle"} else path.is_file())
        )
        if valid:
            set_resource(model.env_var, path, directory=model.kind in {"snapshot", "bundle"})
        elif force:
            configured = os.environ.get(model.env_var)
            if configured and Path(configured).expanduser().resolve() == path.resolve(): os.environ.pop(model.env_var, None)

    os.environ.setdefault("KARAOKE_AI_REQUIRE_CTC", "0")
    set_resource("MSST_ENGINE_DIR", msst, directory=True)
    if msst_config.is_file(): set_resource("MSST_CONFIG", msst_config)


# --------------------------------------------------------------------
# База данных
# --------------------------------------------------------------------

DATABASE_URL = f"sqlite:///{DB_PATH}"

# --------------------------------------------------------------------
# AI-пайплайн
# --------------------------------------------------------------------

DEFAULT_LANGUAGE = None  # автоопределение

ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".ogg"}
ALLOWED_KARAOKE_EXTENSIONS = {".kar", ".mid", ".kfn"}
ALLOWED_SONG_UPLOAD_EXTENSIONS = ALLOWED_AUDIO_EXTENSIONS | ALLOWED_KARAOKE_EXTENSIONS
MAX_AUDIO_UPLOAD_BYTES = _env_int("SONGAPP_MAX_AUDIO_UPLOAD_BYTES", 2 * 1024**3, minimum=1)
UPLOAD_CHUNK_SIZE = _env_int("SONGAPP_UPLOAD_CHUNK_SIZE", 1024 * 1024, minimum=4096)
MAX_SONG_DURATION_SECONDS = _env_int("SONGAPP_MAX_SONG_DURATION_SECONDS", 20 * 60, minimum=1)
MAX_RECORDING_DURATION_SECONDS = _env_int("SONGAPP_MAX_RECORDING_DURATION_SECONDS", 15 * 60, minimum=1)

# --------------------------------------------------------------------
# Запись голоса
# --------------------------------------------------------------------

RECORDING_SAMPLE_RATE = 44100
RECORDING_CHANNELS = 1

# --------------------------------------------------------------------
# Сервер
# --------------------------------------------------------------------

HOST = os.environ.get("SONGAPP_HOST", "127.0.0.1")  # локальный десктоп-бекенд — наружу не торчит
PORT = _env_int("SONGAPP_PORT", 8000, minimum=1, maximum=65535)
DEFAULT_UI_ORIGINS = (
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)
CORS_ORIGINS = _unique_csv("SONGAPP_CORS_ORIGINS", DEFAULT_UI_ORIGINS)


def ensure_directories() -> None:
    """Создаёт все рабочие директории при первом запуске, если их ещё нет."""
    for path in (SONG_OUTPUT_DIR, UPLOAD_TEMP_DIR, CACHE_DIR, DATA_DIR, APP_LOG_DIR): path.mkdir(parents=True, exist_ok=True)


ensure_directories()
configure_runtime_cache_environment()
