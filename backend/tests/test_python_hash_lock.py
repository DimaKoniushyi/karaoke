from __future__ import annotations

import base64
import csv
import hashlib
import subprocess
import sys
import zipfile
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "backend" / "requirements-lock.in"
LOCK = ROOT / "backend" / "requirements-lock.txt"


def test_torch_only_ai_runtime_does_not_bundle_tensorflow_frameworks():
    pins = {
        line.split("==", 1)[0].strip().casefold().replace("_", "-")
        for line in INPUT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert pins.isdisjoint({"tensorflow", "tf-keras", "keras"})


def test_current_pitch_pipeline_does_not_install_legacy_praat_runtime():
    pins = {
        line.split("==", 1)[0].strip().casefold().replace("_", "-")
        for line in INPUT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "praat-parselmouth" not in pins


def test_torch_only_runtime_does_not_keep_orphaned_tensorflow_dependencies():
    pins = {
        line.split("==", 1)[0].strip().casefold().replace("_", "-")
        for line in INPUT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert pins.isdisjoint(
        {
            "astunparse",
            "gast",
            "google-pasta",
            "h5py",
            "libclang",
            "ml-dtypes",
            "namex",
            "opt-einsum",
            "optree",
            "termcolor",
            "wrapt",
        }
    )


def _logical_requirements() -> list[str]:
    result, current = [], ""
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or (line.startswith("--") and not current):
            continue
        current += (" " if current else "") + line.removesuffix("\\").strip()
        if not line.endswith("\\"):
            result.append(current)
            current = ""
    assert not current
    return result


def test_every_reviewed_pin_has_published_sha256_hashes():
    pins = [line.strip() for line in INPUT.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    locked = _logical_requirements()
    assert len(locked) == len(pins)
    for pin, requirement in zip(pins, locked, strict=True):
        assert requirement.startswith(f"{pin} ")
        hashes = requirement.count("--hash=sha256:")
        assert hashes >= 1
        for digest in requirement.split("--hash=sha256:")[1:]:
            assert len(digest.split()[0]) == 64


def _wheel(path: Path) -> None:
    files = {
        "lockprobe/__init__.py": b"VALUE = 1\n",
        "lockprobe-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: lockprobe\nVersion: 1.0\n",
        "lockprobe-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    rows = []
    for name, payload in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
        rows.append((name, f"sha256={digest}", str(len(payload))))
    record = StringIO()
    writer = csv.writer(record, lineterminator="\n")
    writer.writerows([*rows, ("lockprobe-1.0.dist-info/RECORD", "", "")])
    files["lockprobe-1.0.dist-info/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)


def test_pip_require_hashes_accepts_verified_wheel_and_blocks_mutation(tmp_path):
    wheel = tmp_path / "lockprobe-1.0-py3-none-any.whl"
    _wheel(wheel)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"lockprobe @ {wheel.as_uri()} --hash=sha256:{digest}\n", encoding="utf-8")
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "--require-hashes"]
    accepted = subprocess.run([*command, "--target", str(tmp_path / "ok"), "-r", str(requirements)], capture_output=True, text=True, check=False)
    assert accepted.returncode == 0, accepted.stderr
    wheel.write_bytes(wheel.read_bytes() + b"tampered")
    rejected = subprocess.run([*command, "--target", str(tmp_path / "bad"), "-r", str(requirements)], capture_output=True, text=True, check=False)
    assert rejected.returncode != 0
    assert "THESE PACKAGES DO NOT MATCH THE HASHES" in rejected.stderr
