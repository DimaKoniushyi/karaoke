from pathlib import Path

from scripts.backend import audit_architecture


def test_production_architecture_budget_is_enforced():
    failures = []
    for path in audit_architecture.python_files():
        errors, _warnings = audit_architecture.audit_file(path)
        failures.extend(errors)
    assert failures == []


def test_text_alignment_engine_stays_within_its_architecture_budget():
    errors, _warnings = audit_architecture.audit_file(
        audit_architecture.ROOT / "AI" / "engines" / "text.py"
    )

    assert errors == []


def test_songs_router_stays_within_its_architecture_budget():
    errors, _warnings = audit_architecture.audit_file(
        audit_architecture.ROOT / "app" / "routers" / "songs.py"
    )

    assert errors == []


def test_song_package_service_stays_within_its_architecture_budget():
    errors, _warnings = audit_architecture.audit_file(
        audit_architecture.ROOT / "app" / "services" / "song_package_service.py"
    )

    assert errors == []


def test_kfn_dataset_service_stays_within_its_architecture_budget():
    errors, _warnings = audit_architecture.audit_file(
        audit_architecture.ROOT / "app" / "services" / "kfn_dataset_service.py"
    )

    assert errors == []


def test_monitor_worker_stays_within_its_architecture_budget():
    errors, _warnings = audit_architecture.audit_file(
        audit_architecture.ROOT / "app" / "services" / "monitor_worker.py"
    )

    assert errors == []


def test_pipeline_service_stays_within_its_architecture_budget():
    errors, _warnings = audit_architecture.audit_file(
        audit_architecture.ROOT / "app" / "services" / "pipeline_service.py"
    )

    assert errors == []


def test_metadata_enrichment_service_stays_within_its_architecture_budget():
    errors, _warnings = audit_architecture.audit_file(
        audit_architecture.ROOT
        / "app"
        / "services"
        / "metadata_enrichment_service.py"
    )

    assert errors == []


def test_new_module_and_function_use_default_budgets(tmp_path: Path):
    module = tmp_path / "oversized.py"
    body = "\n".join(["def oversized():", *["    value = 1"] * 81, "    return value"])
    module.write_text(body, encoding="utf-8")

    original_root = audit_architecture.ROOT
    audit_architecture.ROOT = tmp_path
    try:
        errors, _warnings = audit_architecture.audit_file(module)
    finally:
        audit_architecture.ROOT = original_root

    assert any("function 'oversized'" in error and "budget 80" in error for error in errors)


def test_named_hotspot_budget_does_not_apply_to_a_copy(tmp_path: Path):
    module = tmp_path / "text.py"
    module.write_text(
        "\n".join(["def align_timed_lines():", *["    value = 1"] * 81]),
        encoding="utf-8",
    )

    original_root = audit_architecture.ROOT
    audit_architecture.ROOT = tmp_path
    try:
        errors, _warnings = audit_architecture.audit_file(module)
    finally:
        audit_architecture.ROOT = original_root

    assert any("budget 80" in error for error in errors)
