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


# ----------------------------------------------------------------------------- manifests


def test_validate_manifest_offline_flags_contract_violations() -> None:
    from infra2_sdk.ci import validate_manifest_offline
    from infra2_sdk.runtime.config_schema import EnvironmentField, EnvironmentManifest

    manifest = EnvironmentManifest(
        source="sc",
        fields=(
            EnvironmentField("ok_human", "OK_HUMAN", source="human", empty_ok=True),
            EnvironmentField(
                "ok_runtime", "OK_RUNTIME", source="runtime", required=True, has_default=False
            ),
            EnvironmentField("lazy", "LAZY_KEY", source="human"),
            EnvironmentField("digest", "IMAGE_DIGEST", source="release"),
            EnvironmentField("leak", "API_TOKEN", source="code", sensitive=True),
            EnvironmentField("root", "VAULT_ROOT", source="bootstrap"),
            EnvironmentField(
                "url",
                "DATABASE_URL",
                source="runtime",
                required=True,
                has_default=False,
                composed_from="pg://{NOPE}@db",
            ),
        ),
    )
    errors = validate_manifest_offline(manifest)
    assert errors == [
        "LAZY_KEY: human value must be required, injected or empty_ok",
        "IMAGE_DIGEST: release value must be injected by the deployment",
        "API_TOKEN: a sensitive value cannot be a code default",
        "VAULT_ROOT: bootstrap values never enter an application manifest",
        "DATABASE_URL: composed_from references undeclared NOPE",
    ]
    clean = EnvironmentManifest(
        source="sc",
        fields=(
            *manifest.fields[:2],
            EnvironmentField("ua", "SEC_USER_AGENT", source="human", injected=True),
            EnvironmentField(
                "s3", "S3_ENDPOINT", source="code", composed_from="http://127.0.0.1:{env:PORT}"
            ),
            EnvironmentField(
                "db",
                "DATABASE_URL",
                source="runtime",
                provided_by="truealpha/postgres:POSTGRES_PASSWORD",
                composed_from="postgresql://postgres:{POSTGRES_PASSWORD}@db/app",
            ),
        ),
    )
    assert validate_manifest_offline(clean) == []
