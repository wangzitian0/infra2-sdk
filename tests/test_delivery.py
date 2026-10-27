import pytest

from infra2_sdk.delivery import (
    BudgetStatus,
    DisagreementKind,
    FailureDomain,
    PipelineStage,
    StageResult,
    StageStatus,
    _validate_stage_result,
    classify_budget,
    make_stage_result,
)
from infra2_sdk.runtime.environment import EnvironmentTier


def test_stage_result_serializes_enum_values() -> None:
    result = make_stage_result(
        source="infra2",
        environment="staging",
        stage="deploy-smoke",
        target="finance_report/app",
        status="pass",
        duration_ms=15,
    )
    assert result.to_dict()["environment"] == "staging"
    assert result.to_dict()["stage"] == "deploy-smoke"


def test_failure_requires_a_failure_domain() -> None:
    with pytest.raises(ValueError, match="failure_domain"):
        make_stage_result(
            source="infra2",
            environment="staging",
            stage="deploy-smoke",
            target="finance_report/app",
            status=StageStatus.FAIL,
        )


def test_budget_classification() -> None:
    assert classify_budget(11, deadline_ms=10) == BudgetStatus.HARD_BREACH
    assert classify_budget(8, deadline_ms=10, soft_budget_ms=5) == BudgetStatus.SOFT_BREACH
    assert classify_budget(3, deadline_ms=10) == BudgetStatus.WITHIN_BUDGET
    assert classify_budget(3, deadline_ms=0) == BudgetStatus.NOT_APPLICABLE
    assert FailureDomain.CONFIGURATION.value == "configuration"


@pytest.mark.parametrize("duration,deadline", [(-1, 1), (1, -1)])
def test_budget_rejects_negative_values(duration: int, deadline: int) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        classify_budget(duration, deadline_ms=deadline)


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"source": ""}, "source"),
        ({"target": ""}, "target"),
        ({"duration_ms": -1}, "duration_ms"),
        ({"deadline_ms": -1}, "deadline_ms"),
        ({"current_stage_age_ms": -1}, "current_stage_age_ms"),
    ],
)
def test_stage_validation_rejects_incomplete_evidence(changes: dict, message: str) -> None:
    values = {
        "source": "infra2",
        "environment": EnvironmentTier.STAGING,
        "stage": PipelineStage.DEPLOY_SMOKE,
        "target": "finance_report/app",
        "status": StageStatus.PASS,
        "duration_ms": 1,
        "deadline_ms": 2,
    }
    values.update(changes)
    with pytest.raises(ValueError, match=message):
        _validate_stage_result(StageResult(**values))


def test_skip_requires_a_reason() -> None:
    with pytest.raises(ValueError, match="skipped_reason"):
        make_stage_result(
            source="infra2",
            environment="pr",
            stage="full-ut",
            target="change",
            status="skip",
        )


def test_make_stage_result_with_disagreement_kind() -> None:
    res_str = make_stage_result(
        source="infra2",
        environment="staging",
        stage="deploy-status",
        target="app",
        status="pass",
        disagreement_kind="internal-health-public-route",
    )
    assert res_str.disagreement_kind is DisagreementKind.INTERNAL_HEALTH_PUBLIC_ROUTE
    assert res_str.to_dict()["disagreement_kind"] == "internal-health-public-route"

    res_enum = make_stage_result(
        source="infra2",
        environment="staging",
        stage="deploy-status",
        target="app",
        status="pass",
        disagreement_kind=DisagreementKind.INTERNAL_HEALTH_PUBLIC_ROUTE,
    )
    assert res_enum.disagreement_kind is DisagreementKind.INTERNAL_HEALTH_PUBLIC_ROUTE
    assert res_enum.to_dict()["disagreement_kind"] == "internal-health-public-route"


def test_make_stage_result_accepts_environment_tier() -> None:
    r1 = make_stage_result(
        source="infra2",
        environment=EnvironmentTier.PREVIEW,
        stage=PipelineStage.DEPLOY_SMOKE,
        target="app",
        status="pass",
    )
    assert r1.environment is EnvironmentTier.PREVIEW

    r2 = make_stage_result(
        source="infra2",
        environment=EnvironmentTier.LOCAL_DEV,
        stage=PipelineStage.DEPLOY_SMOKE,
        target="app",
        status="pass",
    )
    assert r2.environment is EnvironmentTier.LOCAL_DEV

    r3 = make_stage_result(
        source="infra2",
        environment="local_dev",
        stage=PipelineStage.DEPLOY_SMOKE,
        target="app",
        status="pass",
    )
    assert r3.environment is EnvironmentTier.LOCAL_DEV


def test_stage_result_runtime_type_hints_resolve() -> None:
    import typing

    hints = typing.get_type_hints(StageResult)
    assert hints["environment"] is EnvironmentTier


def test_validate_stage_result_rejects_non_environment_tier() -> None:
    result = StageResult(
        source="test",
        environment="staging",  # type: ignore[arg-type]
        stage=PipelineStage.DEPLOY_SMOKE,
        target="app",
        status=StageStatus.PASS,
        duration_ms=10,
        deadline_ms=1000,
        failure_domain=FailureDomain.NONE,
        external_dependency=False,
        suppressed_reason="",
        skipped_reason="",
        current_stage_age_ms=0,
        budget_status=BudgetStatus.WITHIN_BUDGET,
        disagreement_kind=DisagreementKind.NONE,
        evidence_url="",
    )
    with pytest.raises(TypeError, match="EnvironmentTier"):
        _validate_stage_result(result)


def test_delivery_module_has_zero_runtime_io_dependencies() -> None:
    import ast
    from pathlib import Path

    import infra2_sdk.delivery as delivery_mod

    source = Path(delivery_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    forbidden = {"docker", "requests", "psycopg", "psycopg2", "httpx", "urllib", "socket"}
    assert imported_roots.isdisjoint(forbidden)
