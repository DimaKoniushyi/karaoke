from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
SBOM_DIR = ROOT / "generated/sbom"
OUTPUT = SBOM_DIR / "release.cdx.json"


def _package_identity(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _backend_lock_versions(lock_text: str) -> dict[str, str]:
    versions: dict[str, str] = {}
    for raw_line in lock_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, version = line.partition("==")
        if not separator or not name.strip() or not version.strip():
            raise ValueError(f"Invalid reviewed backend lock entry: {raw_line!r}")
        versions[_package_identity(name.strip())] = version.strip()
    return versions


def _filter_backend_packages(
    packages: list[dict[str, str]], lock_text: str
) -> list[dict[str, str]]:
    """Keep only exact packages declared by the reviewed reproducible lock."""
    expected = _backend_lock_versions(lock_text)
    selected = [
        package
        for package in packages
        if expected.get(_package_identity(package["name"])) == package["version"]
    ]
    found = {_package_identity(package["name"]) for package in selected}
    if missing := sorted(expected.keys() - found):
        raise ValueError(
            "Backend SBOM input does not match requirements-lock.in; missing exact "
            f"packages: {', '.join(missing)}"
        )
    return selected


def _component(ecosystem: str, package: dict[str, str]) -> dict[str, object]:
    name, version, license_name = package["name"], package["version"], package["license"]
    namespace = "pypi" if ecosystem == "backend" else "npm"
    purl_name = quote(name, safe="@/")
    return {
        "type": "library",
        "bom-ref": f"pkg:{namespace}/{purl_name}@{quote(version, safe='.+-')}",
        "name": name,
        "version": version,
        "licenses": [{"license": {"name": license_name}}],
        "properties": [{"name": "advoice:ecosystem", "value": ecosystem}],
    }


def _native_components(version: str) -> list[dict[str, object]]:
    installer = (ROOT / "scripts/install-msst-engine.bat").read_text(encoding="utf-8")
    commit_match = re.search(r'^set "COMMIT=([0-9a-f]{40})"', installer, re.MULTILINE)
    msst_version = commit_match.group(1) if commit_match else "unknown"
    package_lock = json.loads((ROOT / "front/package-lock.json").read_text(encoding="utf-8"))
    electron_version = package_lock["packages"]["node_modules/electron"]["version"]
    ffmpeg_version = "unknown"
    try:
        sys.path.insert(0, str(ROOT / "backend"))
        import config

        output = subprocess.check_output(
            [config.FFMPEG_EXE, "-version"], text=True, stderr=subprocess.STDOUT, timeout=10
        ).splitlines()[0]
        match = re.search(r"ffmpeg version\s+([^\s]+)", output)
        if match:
            ffmpeg_version = match.group(1)
    except (OSError, subprocess.SubprocessError, ImportError):
        pass
    components = [
        {"type": "framework", "name": "Electron", "version": electron_version, "licenses": [{"license": {"name": "MIT"}}]},
        {"type": "application", "name": "FFmpeg", "version": ffmpeg_version, "licenses": [{"license": {"name": "LicenseRef-FFmpeg-Build"}}]},
        {"type": "framework", "name": "KaraokeWasapi", "version": version, "licenses": [{"license": {"name": "LicenseRef-A&D-Voice"}}]},
        {"type": "framework", "name": "KaraokeAsioBridge", "version": version, "licenses": [{"license": {"name": "LicenseRef-A&D-Voice"}}]},
        {"type": "framework", "name": "KeyboardLighting", "version": version, "licenses": [{"license": {"name": "LicenseRef-A&D-Voice"}}]},
        {"type": "library", "name": "hidapi", "version": "d3013f0", "licenses": [{"license": {"name": "BSD-3-Clause"}}]},
        {"type": "library", "name": "wooting-rgb-sdk", "version": "1.8.0", "licenses": [{"license": {"name": "MPL-2.0"}}]},
        {
            "type": "application",
            "name": "Music-Source-Separation-Training",
            "version": msst_version,
            "licenses": [{"license": {"name": "MIT"}}],
            "externalReferences": [{
                "type": "license",
                "url": (
                    "https://github.com/ZFTurbo/Music-Source-Separation-Training/"
                    f"blob/{msst_version}/LICENSE"
                ),
            }],
        },
        {
            "type": "machine-learning-model",
            "name": "fcpe",
            "version": "torchfcpe-0.0.4",
            "licenses": [{"license": {"name": "MIT"}}],
            "externalReferences": [{
                "type": "license",
                "url": "https://github.com/CNChTu/FCPE/blob/main/LICENSE",
            }],
        },
    ]
    try:
        from AI.model_registry import MODELS

        for model in MODELS:
            component = {
                "type": "machine-learning-model",
                "name": model.key,
                "version": model.revision,
                "licenses": [{"license": {"name": model.license}}],
                "externalReferences": [{
                    "type": "license",
                    "url": model.license_url,
                }],
            }
            if model.sha256:
                component["hashes"] = [{"alg": "SHA-256", "content": model.sha256}]
            components.append(component)
    except ImportError:
        pass
    return components


def main() -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    components: list[dict[str, object]] = []
    unknown = []
    for ecosystem in ("backend", "frontend", "cloudflare"):
        path = SBOM_DIR / f"{ecosystem}.json"
        if not path.is_file():
            raise FileNotFoundError(f"SBOM input is missing: {path}")
        source = json.loads(path.read_text(encoding="utf-8"))
        packages = source["packages"]
        if ecosystem == "backend":
            lock_text = (ROOT / "backend/requirements-lock.in").read_text(
                encoding="utf-8"
            )
            packages = _filter_backend_packages(packages, lock_text)
        for package in packages:
            if not package.get("license") or package["license"] == "UNKNOWN":
                unknown.append(f"{ecosystem}:{package['name']}@{package['version']}")
            components.append(_component(ecosystem, package))
    if unknown:
        print("Release SBOM contains packages without a declared license:", file=sys.stderr)
        for package in unknown:
            print(f"- {package}", file=sys.stderr)
        return 1

    components.extend(_native_components(version))
    components.sort(key=lambda item: (str(item["name"]).casefold(), str(item["version"])))
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(UTC).isoformat(),
            "component": {
                "type": "application",
                "bom-ref": f"pkg:generic/a-and-d-voice@{version}",
                "name": "A&D Voice",
                "version": version,
                "licenses": [{"license": {"name": "LicenseRef-A&D-Voice"}}],
            },
        },
        "components": components,
    }
    SBOM_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote CycloneDX SBOM with {len(components)} components to {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
