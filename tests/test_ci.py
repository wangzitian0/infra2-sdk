import pytest

from infra2_sdk.ci import load_delivery_stages, validate_gate, validate_inventory


def test_packaged_stage_vocabulary_is_available() -> None:
    stages = load_delivery_stages()
    assert stages["github_ci.merge_authority"]["order"] == 1
    assert stages["prod.release_integrity"]["order"] == 5
    expected_budgets = {
        "local.advisory": 30,
        "github_ci.merge_authority": 240,
        "preview.runtime": 300,
        "staging.release_validation": 360,
        "staging.provider_regression": 3600,
        "prod.release_integrity": 300,
        "ops.scheduled_cleanup": 3600,
    }
    for stage_id, budget in expected_budgets.items():
        entry = stages[stage_id]
        assert entry["wall_clock_budget_seconds"] == budget
        assert isinstance(entry["phase"], str) and entry["phase"]
        assert isinstance(entry["batch_policy"], str) and entry["batch_policy"]


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


# ----------------------------------------------------------------------------- PR behind window


def test_pr_behind_window_contract() -> None:
    from datetime import UTC, datetime

    from infra2_sdk.ci import PR_BEHIND_MAX_AGE_SECONDS, is_pr_behind_fresh

    assert PR_BEHIND_MAX_AGE_SECONDS == 86400
    now_iso = "2026-11-11T12:00:00Z"
    now_dt = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
    now_epoch = now_dt.timestamp()

    # Exact 24h boundary and fresher commits pass.
    assert is_pr_behind_fresh("2026-11-10T12:00:00Z", now=now_iso)
    assert is_pr_behind_fresh("2026-11-11T11:55:00Z", now=now_dt)
    assert is_pr_behind_fresh(now_epoch - 3600, now=now_epoch)

    # Older than 24h fails closed.
    assert not is_pr_behind_fresh("2026-11-10T11:59:59Z", now=now_iso)
    assert not is_pr_behind_fresh(now_epoch - 86401, now=now_epoch)

    # Missing, malformed, boolean, naive-datetime, non-positive, or negative window fails closed.
    assert not is_pr_behind_fresh(None, now=now_iso)
    assert not is_pr_behind_fresh("", now=now_iso)
    assert not is_pr_behind_fresh("not-a-timestamp", now=now_iso)
    assert not is_pr_behind_fresh(True, now=now_epoch)
    assert not is_pr_behind_fresh(0, now=now_epoch)
    assert not is_pr_behind_fresh(float("inf"), now=now_epoch)
    assert not is_pr_behind_fresh(float("nan"), now=now_epoch)
    assert not is_pr_behind_fresh(now_iso, now=now_iso, max_age_seconds=float("inf"))
    assert not is_pr_behind_fresh(datetime(2026, 11, 11, 11, 0, 0), now=now_dt)
    assert not is_pr_behind_fresh(now_iso, now="bad")
    assert not is_pr_behind_fresh(now_iso, now=now_iso, max_age_seconds=-1)
