from types import SimpleNamespace
from unittest.mock import Mock

import config
from tests._shared import patch_attrs, raises


def test_environment_path_integer_and_csv_helpers(monkeypatch, tmp_path):
    monkeypatch.delenv("TEST_PATH", raising=False)
    assert config._env_path("TEST_PATH", tmp_path) == tmp_path
    monkeypatch.setenv("TEST_PATH", str(tmp_path / "nested" / ".." / "target"))
    assert config._env_path("TEST_PATH", tmp_path) == (tmp_path / "target").resolve()

    monkeypatch.delenv("TEST_INT", raising=False)
    assert config._env_int("TEST_INT", 3, minimum=1, maximum=5) == 3
    monkeypatch.setenv("TEST_INT", "4")
    assert config._env_int("TEST_INT", 3, minimum=1, maximum=5) == 4
    for invalid in ("text", "0", "6"):
        monkeypatch.setenv("TEST_INT", invalid)
        raises(ValueError, lambda: config._env_int('TEST_INT', 3, minimum=1, maximum=5), match='TEST_INT')
    monkeypatch.setenv("TEST_INT", "0")
    raises(ValueError, lambda: config._env_int('TEST_INT', 3, minimum=1), match='>= 1')

    monkeypatch.delenv("TEST_CSV", raising=False)
    assert config._unique_csv("TEST_CSV", ("a", "b")) == ("a", "b")
    monkeypatch.setenv("TEST_CSV", " b, a, b, ,c ")
    assert config._unique_csv("TEST_CSV", ()) == ("b", "a", "c")


def test_runtime_executable_prefers_bundled_binary_then_path(monkeypatch, tmp_path):
    executable_dir, runtime_dir, base_dir = tmp_path / 'python', tmp_path / 'runtime', tmp_path / 'base'
    executable_dir.mkdir()
    runtime_dir.mkdir()
    base_dir.mkdir()
    monkeypatch.setattr(config.sys, "executable", str(executable_dir / "python.exe"))
    patch_attrs(monkeypatch, config, RUNTIME_DIR=runtime_dir, BASE_DIR=base_dir)

    bundled_name = "tool.exe" if config.os.name == "nt" else "tool"
    bundled = runtime_dir / bundled_name
    bundled.write_bytes(b"binary")
    assert config.resolve_runtime_executable("tool") == str(bundled)
    bundled.unlink()

    monkeypatch.setattr(config.shutil, "which", Mock(return_value="C:/bin/tool.exe"))
    assert config.resolve_runtime_executable("tool.exe") == "C:/bin/tool.exe"
    config.shutil.which.return_value = None
    assert config.resolve_runtime_executable("missing") == "missing"


def test_default_data_and_models_directories_cover_runtime_modes(monkeypatch, tmp_path):
    patch_attrs(monkeypatch, config, IS_FROZEN=False, PROJECT_ROOT=tmp_path / 'project', DOWNLOADS_DIR=tmp_path / 'downloads')
    assert (config._default_data_dir() == tmp_path / 'project' / 'data') and (config._default_models_dir() == tmp_path / 'downloads' / 'models')

    monkeypatch.setattr(config, "IS_FROZEN", True)
    monkeypatch.setenv("SONGAPP_INSTALL_ROOT", str(tmp_path / "installed"))
    assert (config._default_data_dir() == tmp_path / 'installed' / 'data' / 'backend') and (config._default_models_dir() == tmp_path / 'installed' / 'data' / 'models')

    monkeypatch.delenv("SONGAPP_INSTALL_ROOT")
    executable = tmp_path / "portable" / "resources" / "backend" / "KaraokeBackend.exe"
    monkeypatch.setattr(config.sys, "executable", str(executable))
    assert (config._default_data_dir() == tmp_path / 'portable' / 'data' / 'backend') and (config._default_models_dir() == tmp_path / 'portable' / 'data' / 'models')


def test_default_data_dir_falls_back_to_per_user_dir_when_install_root_is_unwritable(monkeypatch, tmp_path):
    # e.g. an administrator installed to C:\Program Files\...; a standard
    # user's process can't write there, so app data must not live under it.
    root = tmp_path / "Program Files" / "installed"
    local_appdata = tmp_path / "local-appdata"
    monkeypatch.setattr(config, "IS_FROZEN", True)
    monkeypatch.setenv("SONGAPP_INSTALL_ROOT", str(root))
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    original_mkdir = config.Path.mkdir

    def unwritable_mkdir(self, *args, **kwargs):
        if self == root:
            raise PermissionError("simulated read-only install root")
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(config.Path, "mkdir", unwritable_mkdir)

    fallback_root = local_appdata / "A&D Voice"
    assert config._default_data_dir() == fallback_root / "data" / "backend"
    assert config._default_models_dir() == fallback_root / "data" / "models"




def test_saved_storage_path_is_validated(monkeypatch, tmp_path):
    settings, default = tmp_path / 'paths.json', tmp_path / 'default'
    monkeypatch.setattr(config, "PATH_SETTINGS_FILE", settings)

    assert config._saved_path("songs_folder", default) == default
    settings.write_text("invalid", encoding="utf-8")
    assert config._saved_path("songs_folder", default) == default
    settings.write_text('[["songs_folder", "ignored"]]', encoding="utf-8")
    assert config._saved_path("songs_folder", default) == default
    settings.write_text('{"songs_folder": "   "}', encoding="utf-8")
    assert config._saved_path("songs_folder", default) == default
    selected = tmp_path / "selected"
    settings.write_text(f'{{"songs_folder": "{selected.as_posix()}"}}', encoding="utf-8")
    assert config._saved_path("songs_folder", default) == selected.resolve()



def test_saved_storage_path_from_previous_dev_checkout_uses_current_default(monkeypatch, tmp_path):
    current_root, old_root = tmp_path / 'D' / 'Git' / 'karaoke', tmp_path / 'E' / 'Git' / 'karaoke'
    current_default, settings = current_root / 'karaoke_songs', tmp_path / 'paths.json'
    settings.write_text(
        '{"songs_folder": "' + (old_root / "karaoke_songs").as_posix() + '"}',
        encoding="utf-8",
    )
    patch_attrs(monkeypatch, config, IS_FROZEN=False, PROJECT_ROOT=current_root, PATH_SETTINGS_FILE=settings)
    assert config._saved_path("songs_folder", current_default) == current_default

    external = tmp_path / "external-library"
    settings.write_text(
        '{"songs_folder": "' + external.as_posix() + '"}', encoding="utf-8"
    )
    assert config._saved_path("songs_folder", current_default) == external.resolve()

def test_apply_storage_paths_updates_only_supplied_values(monkeypatch, tmp_path):
    original_models, ensure, configure = config.MODELS_DIR, Mock(), Mock()
    patch_attrs(monkeypatch, config, ensure_directories=ensure, configure_ai_resource_environment=configure)

    config.apply_storage_paths(
        songs_folder=str(tmp_path / "songs"),
        ai_folder=str(tmp_path / "models"),
        cache_folder=str(tmp_path / "cache"),
    )
    assert ((tmp_path / 'songs').resolve() == config.SONG_OUTPUT_DIR) and ((tmp_path / 'models').resolve() == config.MODELS_DIR) and ((tmp_path / 'cache').resolve() == config.CACHE_DIR) and ((tmp_path / 'cache' / 'uploads').resolve() == config.UPLOAD_TEMP_DIR)
    ensure.assert_called_once_with()
    configure.assert_called_once_with(force=True)

    monkeypatch.setattr(config, "MODELS_DIR", original_models)


def test_runtime_caches_follow_selected_cache_folder(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    names = (
        "TEMP",
        "TMP",
        "HF_HOME",
        "HUGGINGFACE_HUB_CACHE",
        "TORCH_HOME",
        "XDG_CACHE_HOME",
        "NUMBA_CACHE_DIR",
        "KERAS_HOME",
        "MPLCONFIGDIR",
        "CUDA_CACHE_PATH",
        "TRITON_CACHE_DIR",
        "TORCHINDUCTOR_CACHE_DIR",
    )
    monkeypatch.setattr(config, "CACHE_DIR", cache)
    monkeypatch.setattr(config.tempfile, "tempdir", config.tempfile.tempdir)
    for name in names:
        monkeypatch.setenv(name, "previous")

    config.configure_runtime_cache_environment()

    root = cache / "ai-runtime"
    assert config.tempfile.gettempdir() == str(root / "temp")
    assert config.os.environ["TEMP"] == str(root / "temp")
    assert config.os.environ["TMP"] == str(root / "temp")
    assert config.os.environ["HF_HOME"] == str(root / "huggingface")
    assert config.os.environ["TORCH_HOME"] == str(root / "torch")
    assert config.os.environ["CUDA_CACHE_PATH"] == str(root / "cuda")


def test_ai_resource_environment_uses_existing_downloads(monkeypatch, tmp_path):
    models_dir, engine_dir = tmp_path / 'models', tmp_path / 'engines' / 'msst'
    config_file, checkpoint, snapshot = engine_dir / 'configs' / 'KimberleyJensen' / 'config_vocals_mel_band_roformer_kj.yaml', models_dir / 'pitch.pt', models_dir / 'asr'
    config_file.parent.mkdir(parents=True)
    config_file.write_text("model: test", encoding="utf-8")
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"model")
    snapshot.mkdir()
    resources = (
        SimpleNamespace(key="pitch", env_var="TEST_PITCH", kind="file"),
        SimpleNamespace(key="asr", env_var="TEST_ASR", kind="snapshot"),
    )
    patch_attrs(monkeypatch, config, MODELS=resources, MODELS_DIR=models_dir, EXTERNAL_ENGINES_DIR=tmp_path / 'engines', model_path=lambda _root, model: checkpoint if model.key == 'pitch' else snapshot)
    for name in (
        "TEST_PITCH",
        "TEST_ASR",
        "MSST_ENGINE_DIR",
        "MSST_CONFIG",
        "KARAOKE_AI_REQUIRE_CTC",
        "USE_TORCH",
        "USE_TF",
        "USE_FLAX",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRANSFORMERS_CACHE", str(tmp_path / "obsolete-cache"))

    config.configure_ai_resource_environment()

    assert (config.os.environ['TEST_PITCH'] == str(checkpoint)) and (config.os.environ['TEST_ASR'] == str(snapshot)) and (config.os.environ['MSST_ENGINE_DIR'] == str(engine_dir)) and (config.os.environ['MSST_CONFIG'] == str(config_file)) and (config.os.environ['KARAOKE_AI_REQUIRE_CTC'] == '0')
    assert config.os.environ["USE_TORCH"] == "1"
    assert config.os.environ["USE_TF"] == "0"
    assert config.os.environ["USE_FLAX"] == "0"
    assert "TRANSFORMERS_CACHE" not in config.os.environ

    alternate = tmp_path / "alternate.pt"
    alternate.write_bytes(b"old")
    monkeypatch.setenv("TEST_PITCH", str(alternate))
    config.configure_ai_resource_environment()
    assert config.os.environ["TEST_PITCH"] == str(alternate)
    config.configure_ai_resource_environment(force=True)
    assert config.os.environ["TEST_PITCH"] == str(checkpoint)


def test_ensure_directories_creates_every_runtime_location(monkeypatch, tmp_path):
    paths = [tmp_path / name for name in ("songs", "uploads", "cache", "data", "logs")]
    for attribute, path in zip(
        ("SONG_OUTPUT_DIR", "UPLOAD_TEMP_DIR", "CACHE_DIR", "DATA_DIR", "APP_LOG_DIR"),
        paths,
        strict=True,
    ):
        monkeypatch.setattr(config, attribute, path)
    config.ensure_directories()
    assert all(path.is_dir() for path in paths)


def test_backend_import_defers_expensive_model_validation_until_startup_worker():
    source = config.Path(config.__file__).read_text(encoding="utf-8")
    module_tail = source[source.rfind("ensure_directories()") :]

    assert "configure_runtime_cache_environment()" in module_tail
    assert "configure_ai_resource_environment()" not in module_tail
