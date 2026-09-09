import json
import subprocess
from pathlib import Path

import pytest

from tests._shared import assert_contains, assert_excludes, project_text

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("script", "required"),
    [
        (
            "start-dev.bat",
            (
                'if /i "%~1"=="--prepare-only" set "PREPARE_ONLY=1"',
                "Development dependencies are ready",
            ),
        ),
        (
            "scripts/install-ai-models.bat",
            (
                'set "DML_SETUP=%ROOT%\\scripts\\prepare-fcpe-directml-pilot.bat"',
                ":optional_accelerators",
                'call "%DML_SETUP%"',
            ),
        ),
    ],
)
def test_recovery_script_contracts(script, required):
    assert_contains(project_text(script, encoding="utf-8-sig"), *required)


def test_development_launch_uses_browser_compatible_isolated_ports():
    script = project_text("start-dev.bat", encoding="utf-8-sig")
    assert_contains(
        script,
        'set "KARAOKE_BACKEND_URL=http://127.0.0.1:18000"',
        'set "VITE_API_BASE_URL=http://127.0.0.1:18000"',
        'set "SONGAPP_API_TOKEN=advoice-local-development"',
        'set "VITE_API_TOKEN=advoice-local-development"',
        '"$p=18000,5173;',
        "Get-CimInstance Win32_Process",
        "'node.exe','electron.exe','python.exe','pythonw.exe'",
        "Start-Process -FilePath taskkill.exe",
        "-ArgumentList '/PID',([string]$target),'/T','/F'",
    )
    assert script.index("call :stop_dev_processes") < script.index("call :start_job front")


def test_prepare_only_does_not_stop_running_development_instance():
    script = project_text("start-dev.bat", encoding="utf-8-sig")
    cleanup_guard = script.index('if "%PREPARE_ONLY%"=="0" (')
    cleanup_call = script.index("call :stop_dev_processes")
    assert cleanup_guard < cleanup_call
    assert "*> $null" not in script
    assert "\\.vscode\\extensions\\" in script


def test_fresh_clone_bootstraps_the_pinned_node_before_frontend_jobs():
    script = project_text("start-dev.bat", encoding="utf-8-sig")
    bootstrap = project_text("scripts/ensure-node.ps1", encoding="utf-8-sig")
    package = json.loads(project_text("front/package.json", encoding="utf-8-sig"))
    pinned = project_text("front/.nvmrc", encoding="utf-8-sig").strip()

    assert pinned == "22.18.0"
    assert package["engines"]["node"] == ">=22.18.0 <23 || >=24.11.0"
    assert_contains(
        script,
        'set "NODE_ENV_FILE=%TEMP%\\advoice-node-',
        'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\\ensure-node.ps1" -Root "%ROOT%."',
        'for /f "usebackq delims=" %%P in ("%NODE_ENV_FILE%") do set "PATH=%%P;%PATH%"',
    )
    assert script.index("ensure-node.ps1") < script.index("call :start_job front")
    assert_contains(
        bootstrap,
        "nvm.exe",
        "nvm install",
        "nvm use",
        "nvm root",
        "Split-Path -Parent $nvm.Source",
        '"v$RequiredVersion\\node.exe"',
        "nodejs.org/dist/v$RequiredVersion",
        "SHASUMS256.txt",
        "Get-FileHash",
        "Expand-Archive",
        "npm.cmd",
    )


def test_fresh_clone_bootstraps_native_build_tools_and_ffmpeg():
    script = project_text("start-dev.bat", encoding="utf-8-sig")
    tools = project_text("scripts/ensure-dev-tools.ps1", encoding="utf-8-sig")
    assert_contains(
        script,
        'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\\ensure-dev-tools.ps1" -Root "%ROOT%."',
        'set "FFMPEG_ENV_FILE=%TEMP%\\advoice-ffmpeg-',
        'for /f "usebackq delims=" %%P in ("%FFMPEG_ENV_FILE%") do set "PATH=%%P;%PATH%"',
    )
    assert script.index("ensure-dev-tools.ps1") < script.index("call :start_job front")
    assert_contains(
        tools,
        "vswhere.exe",
        "Microsoft.VisualStudio.Workload.VCTools",
        "Microsoft.VisualStudio.Component.VC.CMake.Project",
        "https://aka.ms/vs/17/release/vs_BuildTools.exe",
        "ffmpeg-master-latest-win64-gpl.zip",
        "Get-FileHash",
        "ffmpeg.exe",
    )


def test_fresh_clone_compiles_native_audio_before_development_launch():
    script = project_text("start-dev.bat", encoding="utf-8-sig")
    wrapper = project_text("scripts/prepare-native-audio.bat", encoding="utf-8-sig")
    builder = project_text("scripts/prepare-native-audio.ps1", encoding="utf-8-sig")

    assert_contains(
        script,
        'set "ASIO=%ROOT%scripts\\prepare-native-audio.bat"',
        'call :start_job asio "%ASIO%" "%ROOT%"',
    )
    assert_contains(
        wrapper,
        'call "%ROOT%\\scripts\\install-asio-sdk.bat" "%ROOT%"',
        'prepare-native-audio.ps1',
    )
    # start-dev's ROOT ends in a backslash. Forwarding that value through a
    # quoted nested cmd invocation can leave a literal quote in PowerShell's
    # -Root argument (D:\\repo\\"), which GetFullPath rejects. The wrapper
    # must derive its canonical root from its own stable location instead.
    assert 'if "%~1"' not in wrapper
    assert_contains(wrapper, 'for %%I in ("%~dp0..") do set "ROOT=%%~fI"')
    assert_contains(
        builder,
        ".Trim().Trim('\"')",
        'backend\\engines\\asio',
        'generated\\build\\asio',
        'KaraokeAsioBridge.exe',
        'KaraokeWasapi.dll',
        'cmake.exe',
        'ninja.exe',
    )


def test_parallel_job_status_does_not_override_windows_resource_compiler():
    script = project_text("start-dev.bat", encoding="utf-8-sig")

    # RC is a standard compiler environment variable consumed by CMake/MSVC.
    # Reusing it for the worker's .rc result file makes CMake try to execute
    # that text file as rc.exe on a clean machine.
    assert 'set "RC=%~6"' not in script
    assert_contains(
        script,
        'set "JOB_RESULT=%~6"',
        '>"%JOB_RESULT%" echo 0',
        '>"%JOB_RESULT%" echo !E!',
        '>"%JOB_RESULT%" echo 1',
    )


def test_build_installer_prepares_gitignored_downloads_before_build():
    script = project_text("build-installer.bat", encoding="utf-8-sig")
    assert_contains(script, 'set "KARAOKE_PREPARE_DIRECTML=1"', 'call "%~dp0start-dev.bat" --prepare-only')
    assert script.index("--prepare-only") < script.index("build-installer.ps1")


def test_ai_install_restores_msst_before_fast_path():
    script = project_text("scripts/install-ai-models.bat", encoding="utf-8-sig")
    assert_contains(
        script,
        'set "MSST_SETUP=%ROOT%\\scripts\\install-msst-engine.bat"',
        'call "%MSST_SETUP%" "%ROOT%" || goto :fail',
    )
    assert script.index('call "%MSST_SETUP%"') < script.index("rem FAST PATH")


def test_msst_installer_recovery_contract():
    script = project_text("scripts/install-msst-engine.bat", encoding="utf-8-sig")
    # The engine clone is pinned to an exact commit (not refs/heads/main) so a
    # fresh bootstrap can't silently fetch code patch-msst-engine.ps1 wasn't
    # verified against -- both the git fetch and the ZIP fallback must target
    # that same pinned COMMIT variable, not a moving branch.
    assert_contains(
        script,
        "ZFTurbo/Music-Source-Separation-Training.git",
        'set "COMMIT=',
        "Music-Source-Separation-Training/archive/%COMMIT%.zip",
        "fetch --quiet --depth 1 origin \"%COMMIT%\"",
        "inference.py",
        "utils\\model_utils.py",
        "models\\bs_roformer\\mel_band_roformer.py",
        "config_vocals_mel_band_roformer_kj.yaml",
    )


def test_msst_installer_tolerates_a_preexisting_downloads_engines_directory():
    # Native `mkdir` fails (errorlevel 1) if the target already exists, unlike
    # `mkdir -p`. downloads\engines is shared with the ASIO engine and other
    # downloads, so it's almost always already there by the time MSST
    # installs -- an unguarded `mkdir "%DL%\engines" ... || goto :fail` jumps
    # straight to the generic failure message on every single run, before
    # ever attempting a download, with the real reason hidden by `>nul 2>&1`.
    script = project_text("scripts/install-msst-engine.bat", encoding="utf-8-sig")
    assert_contains(script, 'if not exist "%DL%\\engines" mkdir "%DL%\\engines"')


def test_msst_patch_applies_regardless_of_the_patch_scripts_own_line_endings(tmp_path):
    # patch-msst-engine.ps1's here-string literals (the old/new code blocks)
    # preserve whatever line endings the .ps1 file itself was checked out
    # with, while the target model_utils.py is normalized to LF before
    # matching. A CRLF checkout of the *patch script* (git core.autocrlf=true
    # is a common Windows default, and this repo has no .gitattributes
    # forcing LF) used to make every multi-line here-string match fail,
    # throwing "Unsupported MSST window implementation" against an unmodified
    # upstream file -- not a real incompatibility.
    engine = tmp_path / "engine"
    (engine / "utils").mkdir(parents=True)
    source = (
        'step = chunk_size // num_overlap\n'
        'step = chunk_size // num_overlap\n'
        '                    if mode == "generic":\n'
        '                        window = windowing_array.clone() # using clone() fixes the clicks at chunk edges when using batch_size=1\n'
        '                        if i - step == 0:  # First audio chunk, no fadein\n'
        '                            window[:fade_size] = 1\n'
        '                        elif i >= mix.shape[1]:  # Last audio chunk, no fadeout\n'
        '                            window[-fade_size:] = 1\n'
        '\n'
        '                    for j, (start, seg_len) in enumerate(batch_locations):\n'
        '                        if mode == "generic":\n'
    )
    real_script = (PROJECT_ROOT / "scripts" / "patch-msst-engine.ps1").read_text(encoding="utf-8")
    variants = {"LF (as committed)": real_script, "CRLF (autocrlf=true checkout)": real_script.replace("\n", "\r\n")}
    for label, script_text in variants.items():
        (engine / "utils" / "model_utils.py").write_text(source, encoding="utf-8", newline="")
        script_path = tmp_path / f"patch-{hash(label)}.ps1"
        script_path.write_text(script_text, encoding="utf-8", newline="")
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path), "-Engine", str(engine)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"{label}: {result.stderr}"
        patched = (engine / "utils" / "model_utils.py").read_text(encoding="utf-8")
        assert patched.count("step = max(1, int(chunk_size / float(num_overlap)))") == 2, label
        assert "window[max(0, seg_len - fade_size):seg_len] = 1" in patched, label


def test_directml_optional_assets_run_before_cached_fast_path():
    script = project_text("scripts/install-ai-models.bat", encoding="utf-8-sig")
    cached = script.index("AI Core is ready. [cached]")
    assert script.rfind("call :optional_accelerators", 0, cached) != -1


def test_scene_video_remains_optional_for_git_clean_build():
    package = json.loads(project_text("front/package.json", encoding="utf-8-sig"))
    assert all(
        item.get("from") != "../downloads/media/videoplayback.webm"
        for item in package["build"]["extraResources"]
    )
    assert_contains(
        project_text("scripts/build-installer.ps1", encoding="utf-8-sig"),
        "Optional karaoke scene video is absent; building without it.",
        "Optional karaoke scene video copied into application resources.",
    )


def test_ai_cached_quick_check_uses_public_nagisa_runtime():
    script = project_text("scripts/install-ai-models.bat", encoding="utf-8-sig")
    quick_line = next(line for line in script.splitlines() if "mods=('qwen_asr','nagisa'" in line)
    assert_excludes(quick_line, "'prepro'", "'nagisa_utils'")
    assert_contains(
        script,
        "Qwen/Nagisa runtime",
        "from qwen_asr import Qwen3ASRModel,Qwen3ForcedAligner;import nagisa",
    )


def test_backend_packaging_bundles_nagisa_native_modules_and_smokes_qwen():
    builder = project_text("scripts/build-installer.ps1", encoding="utf-8-sig")
    assert_contains(
        builder,
        'foreach ($moduleName in @("prepro", "nagisa_utils"))',
        "from importlib.metadata import files",
        "files('nagisa')",
        '$args += @("--add-binary", "$($nagisaNative[$moduleName]);.")',
    )
    assert_excludes(builder, "Required Nagisa native module")
    assert_contains(
        project_text("scripts/smoke-packaged-backend.ps1", encoding="utf-8-sig"),
        'ArgumentList "--verify-qwen-runtime"',
    )
    runner = project_text("backend/run.py", encoding="utf-8-sig")
    assert_contains(runner, 'nagisa.tagging("テスト")')
    assert_excludes(runner, 'importlib.import_module("prepro")')


def test_audio_monitor_is_an_internal_backend_mode_not_a_second_python_executable():
    builder = project_text("scripts/build-installer.ps1", encoding="utf-8-sig")
    runner = project_text("backend/run.py", encoding="utf-8-sig")
    assert_excludes(builder, '"--name","KaraokeAudioMonitor"', "audio-monitor-runtime")
    assert_contains(builder, '"--hidden-import","app.services.monitor_worker"')
    assert_contains(runner, '"--audio-monitor"', "monitor_worker.main()")


def test_backend_packaging_does_not_bundle_or_smoke_legacy_parselmouth():
    build = project_text("scripts/build-installer.ps1", encoding="utf-8-sig")
    runner = project_text("backend/run.py", encoding="utf-8-sig")

    assert_excludes(build, '"--collect-all","parselmouth"', "parselmouth-psola")
    assert_excludes(runner, "import parselmouth", "parselmouth.PRAAT_VERSION")


def test_torch_only_backend_package_explicitly_excludes_other_ml_frameworks():
    build = project_text("scripts/build-installer.ps1", encoding="utf-8-sig")

    for module in ("tensorflow", "tf_keras", "keras", "jax", "flax"):
        assert f'"--exclude-module","{module}"' in build
