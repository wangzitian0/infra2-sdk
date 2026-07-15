import pytest

from infra2_sdk.ci import load_delivery_stages, validate_gate, validate_inventory


def test_packaged_stage_vocabulary_is_available() -> None:
    stages = load_delivery_stages()
    assert stages["github_ci.merge_authority"]["order"] == 1
    assert stages["prod.release_integrity"]["order"] == 5


def test_inventory_rejects_unknown_and_duplicate_gates() -> None:
    gates = [
        {
            "id": "finance.ci",
            "stage": "missing.stage",
            "task_category": "unit",
            "workflow": "ci.yml",
            "job": "test",
        },
        {
            "id": "finance.ci",
            "stage": "github_ci.merge_authority",
            "task_category": "unit",
            "workflow": "ci.yml",
            "job": "test",
        },
    ]
    result = validate_inventory(
        gates,
        stage_ids=set(load_delivery_stages()),
        id_prefix="finance.",
    )
    assert any("unknown stage" in error for error in result["errors"])
    assert any("duplicate gate id" in error for error in result["errors"])


def test_gate_shape_and_prefix_validation() -> None:
    errors = validate_gate(
        {"id": "Bad", "stage": "known"},
        stage_ids={"known"},
        id_prefix="infra.",
    )
    assert any("missing required field" in error for error in errors)
    assert any("must match" in error for error in errors)
    assert any("repo prefix" in error for error in errors)


def test_valid_inventory_returns_sorted_ids() -> None:
    gates = [
        {
            "id": "infra.zeta",
            "stage": "known",
            "task_category": "unit",
            "workflow": "ci.yml",
            "job": "zeta",
        },
        {
            "id": "infra.alpha",
            "stage": "known",
            "task_category": "unit",
            "workflow": "ci.yml",
            "job": "alpha",
        },
    ]
    assert validate_inventory(gates, stage_ids={"known"}, id_prefix="infra.") == {
        "errors": [],
        "ids": ["infra.alpha", "infra.zeta"],
    }


def test_invalid_stage_document_fails_closed(tmp_path) -> None:
    path = tmp_path / "stages.yaml"
    path.write_text("stages: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_delivery_stages(path)
