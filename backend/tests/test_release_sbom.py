from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from AI.model_registry import MODELS

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/generate_release_sbom.py"


def test_every_bundled_ai_model_declares_a_commercially_permissive_license():
    permissive = {"Apache-2.0", "MIT"}

    assert {model.key: model.license for model in MODELS} == {
        "asr": "Apache-2.0",
        "aligner": "Apache-2.0",
        "ctc_ru": "Apache-2.0",
        "ctc_uk": "Apache-2.0",
        "roformer": "MIT",
        "vocalparse": "Apache-2.0",
    }
    assert all(model.license in permissive for model in MODELS)


def test_release_sbom_emits_cyclonedx_components_for_every_runtime_layer():
    spec = importlib.util.spec_from_file_location("release_sbom_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    component = module._component(
        "backend", {"name": "Example", "version": "1.2.3", "license": "MIT"}
    )
    assert component["bom-ref"] == "pkg:pypi/Example@1.2.3"
    assert component["licenses"] == [{"license": {"name": "MIT"}}]

    version = (module.ROOT / "VERSION").read_text(encoding="utf-8").strip()
    native = module._native_components(version)
    names = {item["name"] for item in native}
    assert {
        "Electron",
        "FFmpeg",
        "KaraokeWasapi",
        "KaraokeAsioBridge",
        "KeyboardLighting",
        "Music-Source-Separation-Training",
    } <= names
    assert all(item.get("licenses") for item in native)
    model_components = {
        item["name"]: item for item in native
        if item["type"] == "machine-learning-model"
    }
    assert {
        name: component["licenses"][0]["license"]["name"]
        for name, component in model_components.items()
    } == {
        **{model.key: model.license for model in MODELS},
        "fcpe": "MIT",
    }
    assert model_components["fcpe"]["version"] == "torchfcpe-0.0.4"
    assert model_components["fcpe"]["externalReferences"] == [{
        "type": "license",
        "url": "https://github.com/CNChTu/FCPE/blob/main/LICENSE",
    }]
    msst = next(
        item for item in native
        if item["name"] == "Music-Source-Separation-Training"
    )
    assert msst["licenses"] == [{"license": {"name": "MIT"}}]
    assert msst["externalReferences"] == [{
        "type": "license",
        "url": (
            "https://github.com/ZFTurbo/Music-Source-Separation-Training/"
            f"blob/{msst['version']}/LICENSE"
        ),
    }]


def test_generated_release_sbom_is_valid_cyclonedx_without_unknown_licenses(
    monkeypatch, tmp_path
):
    spec = importlib.util.spec_from_file_location("release_sbom_generate_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sbom_dir = tmp_path / "generated/sbom"
    sbom_dir.mkdir(parents=True)
    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/requirements-lock.in").write_text(
        "backend-package==1\n", encoding="utf-8"
    )
    for ecosystem in ("backend", "frontend", "cloudflare"):
        (sbom_dir / f"{ecosystem}.json").write_text(
            json.dumps(
                {
                    "packages": [
                        {"name": f"{ecosystem}-package", "version": "1", "license": "MIT"}
                    ]
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "SBOM_DIR", sbom_dir)
    monkeypatch.setattr(module, "OUTPUT", sbom_dir / "release.cdx.json")
    monkeypatch.setattr(module, "_native_components", lambda _version: [])
    assert module.main() == 0
    document = json.loads(module.OUTPUT.read_text(encoding="utf-8"))
    assert document["bomFormat"] == "CycloneDX"
    assert document["specVersion"] == "1.6"
    assert document["metadata"]["component"]["name"] == "A&D Voice"
    assert len(document["components"]) == 3
    assert all(
        license_entry["license"]["name"] != "UNKNOWN"
        for component in document["components"]
        for license_entry in component["licenses"]
    )


def test_release_sbom_filters_dirty_venv_packages_through_reviewed_backend_lock():
    spec = importlib.util.spec_from_file_location("release_sbom_filter_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    packages = [
        {"name": "kept_package", "version": "1", "license": "MIT"},
        {"name": "stale-package", "version": "9", "license": "MIT"},
    ]

    assert module._filter_backend_packages(
        packages,
        "# reviewed runtime\nkept-package==1\n",
    ) == [packages[0]]
