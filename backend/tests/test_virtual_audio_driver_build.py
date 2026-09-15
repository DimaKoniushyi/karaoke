from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_virtual_audio_driver_build_is_pinned_reproducible_and_branded():
    script = (ROOT / "scripts/prepare-virtual-audio.ps1").read_text(encoding="utf-8")

    assert "97429c5623590d52f001249460daf43e6749d777" in script
    assert "microsoft-simpleaudiosample.patch" in script
    assert "ROOT\\ADVoiceVirtualAudio" in script
    assert "A&D Voice Virtual Microphone Feed" in script
    assert "A&D Voice Virtual Microphone" in script
    assert "WindowsKernelModeDriver10.0" in script
    assert "Main.vcxproj" in script
    assert "Microsoft.Windows.WDK.x64" in script
    assert "Microsoft.Windows.SDK.CPP.x64" in script
    assert "Inf2Cat.exe" in script


def test_virtual_audio_dependencies_are_not_reexpanded_on_every_build():
    script = (ROOT / "scripts/prepare-virtual-audio.ps1").read_text(
        encoding="utf-8"
    )

    assert ".advoice-extracted" in script
    assert "Test-Path -LiteralPath $marker" in script


def test_virtual_audio_package_retains_the_microsoft_sample_license():
    license_path = (
        ROOT / "backend/engines/virtual_audio/LICENSE-MS-PL.txt"
    )
    script = (ROOT / "scripts/prepare-virtual-audio.ps1").read_text(
        encoding="utf-8"
    )

    assert license_path.is_file()
    assert "Microsoft Public License (MS-PL)" in license_path.read_text(
        encoding="utf-8"
    )
    assert "LICENSE-MS-PL.txt" in script


def test_virtual_audio_bridge_test_is_registered_with_ctest():
    cmake = (ROOT / "backend/engines/asio/CMakeLists.txt").read_text(
        encoding="utf-8"
    )

    assert "enable_testing()" in cmake
    assert "add_test(NAME virtual-audio-bridge" in cmake


def test_installer_build_knows_about_the_virtual_audio_driver_package():
    installer = (ROOT / "scripts/build-installer.ps1").read_text(encoding="utf-8")

    assert "ADVoiceVirtualAudio" in installer
    assert "prepare-virtual-audio.ps1" in installer


def test_installer_elevates_and_installs_the_packaged_virtual_microphone_driver():
    installer = (ROOT / "scripts/karaoke-studio.iss").read_text(encoding="utf-8")

    assert "PrivilegesRequired=admin" in installer
    assert "ADVoiceVirtualAudio.inf" in installer
    assert "pnputil.exe" in installer
    assert "/add-driver" in installer
    assert "/install" in installer
