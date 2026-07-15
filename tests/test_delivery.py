import pytest

from infra2_sdk.delivery import (
    BudgetStatus,
    DisagreementKind,
    FailureDomain,
    PipelineEnvironment,
    PipelineStage,
    StageResult,
    StageStatus,
    acceleration_allowed,
    classify_budget,
    detect_disagreement,
    make_stage_result,
    validate_stage_result,
)


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


def test_skip_acceleration_is_never_allowed_in_production() -> None:
    result = make_stage_result(
        source="app",
        environment="production",
        stage=PipelineStage.FULL_UT,
        target="release",
        status="skip",
        skipped_reason="covered by immutable release evidence",
    )
    assert acceleration_allowed(result) is False


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
        "environment": PipelineEnvironment.STAGING,
        "stage": PipelineStage.DEPLOY_SMOKE,
        "target": "finance_report/app",
        "status": StageStatus.PASS,
        "duration_ms": 1,
        "deadline_ms": 2,
    }
    values.update(changes)
    with pytest.raises(ValueError, match=message):
        validate_stage_result(StageResult(**values))


def test_skip_requires_a_reason() -> None:
    with pytest.raises(ValueError, match="skipped_reason"):
        make_stage_result(
            source="infra2",
            environment="pr",
            stage="full-ut",
            target="change",
            status="skip",
        )


def test_acceleration_allows_reasoned_non_prod_skip() -> None:
    result = make_stage_result(
        source="app",
        environment="pr",
        stage="full-ut",
        target="change",
        status="skip",
        suppressed_reason="unaffected",
    )
    assert acceleration_allowed(result) is True
    assert (
        acceleration_allowed(
            make_stage_result(
                source="app",
                environment="pr",
                stage="full-ut",
                target="change",
                status="pass",
            )
        )
        is False
    )


def test_disagreement_classification() -> None:
    def result(stage: str, status: str, domain: str = "none"):
        return make_stage_result(
            source="infra2",
            environment="staging",
            stage=stage,
            target="app",
            status=status,
            failure_domain=domain,
        )

    route_failure = result("deploy-status", "fail", "traefik-public-route")
    healthy_watchdog = result("watchdog", "pass")
    assert detect_disagreement([route_failure, healthy_watchdog]) == (
        DisagreementKind.INTERNAL_HEALTH_PUBLIC_ROUTE
    )

    stale = result("deploy-status", "fail", "heartbeat-stale")
    canary = result("route-canary", "pass")
    assert detect_disagreement([stale, canary]) == DisagreementKind.HEARTBEAT_PUBLIC_ROUTE

    worker = result("deploy-status", "fail", "dokploy-worker-or-deployment-record")
    assert detect_disagreement([worker, healthy_watchdog]) == DisagreementKind.FALLBACK_PUBLIC_ROUTE
    assert detect_disagreement([healthy_watchdog]) == DisagreementKind.NONE
