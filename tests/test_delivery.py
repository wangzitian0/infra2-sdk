import pytest

from infra2_sdk.delivery import (
    BudgetStatus,
    CanaryReport,
    DisagreementKind,
    FailureDomain,
    PipelineStage,
    SchemaDiscrepancy,
    SmokeCheck,
    SmokeCheckResult,
    StageResult,
    StageStatus,
    _validate_stage_result,
    classify_budget,
    compare_enum_definitions,
    make_stage_result,
    validate_schema_compatibility,
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


def test_smoke_check_protocol_and_canary_report() -> None:
    class _HealthCheck:
        def check(self, target: str) -> SmokeCheckResult:
            return SmokeCheckResult(
                name="public-health",
                passed=(target == "ok"),
                duration_ms=12,
                detail="status=ok",
            )

    probe = _HealthCheck()
    assert isinstance(probe, SmokeCheck)
    res_ok = probe.check("ok")
    assert res_ok.passed is True
    assert res_ok.to_dict()["failure_domain"] == "none"

    report = CanaryReport(
        service="truealpha-web",
        environment=EnvironmentTier.STAGING,
        version="abc1234",
        checks=(res_ok,),
    )
    assert report.passed is True
    assert report.to_dict()["environment"] == "staging"
    assert report.to_dict()["passed"] is True

    empty_report = CanaryReport(
        service="truealpha-web",
        environment=EnvironmentTier.STAGING,
        version="abc1234",
    )
    assert empty_report.passed is False


def test_smoke_check_and_canary_report_reject_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="name"):
        SmokeCheckResult(name="", passed=True)
    with pytest.raises(ValueError, match="duration_ms"):
        SmokeCheckResult(name="health", passed=True, duration_ms=-1)
    with pytest.raises(ValueError, match="service"):
        CanaryReport(service="", environment=EnvironmentTier.STAGING, version="v1")
    with pytest.raises(ValueError, match="version"):
        CanaryReport(service="web", environment=EnvironmentTier.STAGING, version="")


def test_compare_enum_definitions_detects_additions_removals_and_casing() -> None:
    code_enums = {
        "order_status": ["PENDING", "FILLED", "CANCELLED"],
        "role_type": ["admin", "viewer"],
        "exact_match": ["a", "b"],
    }
    db_enums = {
        "order_status": ["PENDING", "filled", "LEGACY_EXPIRED"],
        "role_type": ["admin"],
        "exact_match": ["a", "b"],
    }

    diffs = compare_enum_definitions(code_enums, db_enums)
    by_name = {d.enum_name: d for d in diffs}
    assert set(by_name.keys()) == {"order_status", "role_type"}

    order_diff = by_name["order_status"]
    assert order_diff.missing_in_db == ("CANCELLED",)
    assert order_diff.missing_in_code == ("LEGACY_EXPIRED",)
    assert order_diff.casing_mismatches == (("FILLED", "filled"),)
    assert order_diff.is_compatible is False
    assert order_diff.to_dict()["casing_mismatches"] == [{"code": "FILLED", "db": "filled"}]

    role_diff = by_name["role_type"]
    assert role_diff.missing_in_db == ("viewer",)
    assert role_diff.missing_in_code == ()
    assert role_diff.casing_mismatches == ()
    assert role_diff.is_compatible is False


def test_validate_schema_compatibility_allows_expand_first_extra_db_values() -> None:
    code_enums = {"status": ["ACTIVE"]}
    db_enums = {"status": ["ACTIVE", "DEPRECATED_OLD"]}

    all_diffs = compare_enum_definitions(code_enums, db_enums)
    assert len(all_diffs) == 1
    assert all_diffs[0] == SchemaDiscrepancy(
        enum_name="status",
        missing_in_db=(),
        missing_in_code=("DEPRECATED_OLD",),
        casing_mismatches=(),
    )
    assert all_diffs[0].is_compatible is True

    assert validate_schema_compatibility(code_enums, db_enums) == []
    assert len(
        validate_schema_compatibility(code_enums, db_enums, allow_extra_db_values=False)
    ) == 1


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

