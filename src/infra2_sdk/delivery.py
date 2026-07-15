"""Normalized delivery evidence shared by CI, CD, probes, and watchdogs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class PipelineEnvironment(StrEnum):
    LOCAL = "local"
    PR = "pr"
    STAGING = "staging"
    PRODUCTION = "production"


class PipelineStage(StrEnum):
    CHANGED_AFFECTED_UT = "changed-affected-ut"
    LINT_STATIC = "lint-static"
    FULL_UT = "full-ut"
    INTEGRATION = "integration"
    REGRESSION_E2E = "regression-e2e"
    IMAGE_BUILD = "image-build"
    DEPLOY_SMOKE = "deploy-smoke"
    PROVIDER_GATE = "provider-gate"
    RELEASE_INTEGRITY = "release-integrity"
    CONFIG_PREFLIGHT = "config-preflight"
    DEPLOY_START = "deploy-start"
    DEPLOY_STATUS = "deploy-status"
    ROUTE_CANARY = "route-canary"
    WATCHDOG = "watchdog"


class StageStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    WARN = "warn"
    RUNNING = "running"


class FailureDomain(StrEnum):
    NONE = "none"
    CONFIGURATION = "configuration"
    EXTERNAL_DEPENDENCY = "external-dependency"
    GITHUB_ACTIONS = "github-actions"
    IAC_RUNNER = "iac-runner"
    DOKPLOY_CONTROL_PLANE = "dokploy-control-plane"
    DOKPLOY_WORKER_OR_DEPLOYMENT_RECORD = "dokploy-worker-or-deployment-record"
    DOKPLOY_COMPOSE_SOURCE_TYPE = "dokploy-compose-source-type"
    DOCKER_RUNTIME = "docker-runtime"
    TRAEFIK_PUBLIC_ROUTE = "traefik-public-route"
    CLOUDFLARE_WORKER_HEALTH = "cloudflare-worker-health"
    HOST_REACHABILITY = "host-reachability"
    HEARTBEAT_STALE = "heartbeat-stale"
    PROBE_CLIENT_BLOCKED = "probe-client-blocked"
    ALERT_BRIDGE = "alert-bridge"
    RESOURCE = "resource"
    TIME_BUDGET = "time-budget"
    RELEASE_INTEGRITY = "release-integrity"
    UNKNOWN = "unknown"


class BudgetStatus(StrEnum):
    WITHIN_BUDGET = "within-budget"
    SOFT_BREACH = "soft-breach"
    HARD_BREACH = "hard-breach"
    NOT_APPLICABLE = "not-applicable"


class DisagreementKind(StrEnum):
    NONE = "none"
    INTERNAL_HEALTH_PUBLIC_ROUTE = "internal-health-public-route"
    HEARTBEAT_PROBE_RESULT = "heartbeat-probe-result"
    HEARTBEAT_PUBLIC_ROUTE = "heartbeat-public-route"
    CANARY_APP_READINESS = "canary-app-readiness"
    FALLBACK_PUBLIC_ROUTE = "fallback-public-route"


STAGE_DEADLINE_MS: dict[PipelineStage, int] = {
    PipelineStage.CHANGED_AFFECTED_UT: 120_000,
    PipelineStage.LINT_STATIC: 180_000,
    PipelineStage.FULL_UT: 600_000,
    PipelineStage.INTEGRATION: 600_000,
    PipelineStage.REGRESSION_E2E: 900_000,
    PipelineStage.IMAGE_BUILD: 600_000,
    PipelineStage.DEPLOY_SMOKE: 180_000,
    PipelineStage.PROVIDER_GATE: 300_000,
    PipelineStage.RELEASE_INTEGRITY: 180_000,
    PipelineStage.CONFIG_PREFLIGHT: 60_000,
    PipelineStage.DEPLOY_START: 120_000,
    PipelineStage.DEPLOY_STATUS: 1_200_000,
    PipelineStage.ROUTE_CANARY: 180_000,
    PipelineStage.WATCHDOG: 120_000,
}

PREVIEW_RELEVANT_STAGES = frozenset(
    {
        PipelineStage.REGRESSION_E2E,
        PipelineStage.IMAGE_BUILD,
        PipelineStage.DEPLOY_SMOKE,
        PipelineStage.ROUTE_CANARY,
    }
)


@dataclass(frozen=True)
class StageResult:
    source: str
    environment: PipelineEnvironment
    stage: PipelineStage
    target: str
    status: StageStatus
    duration_ms: int
    deadline_ms: int
    failure_domain: FailureDomain = FailureDomain.NONE
    external_dependency: bool = False
    suppressed_reason: str = ""
    skipped_reason: str = ""
    current_stage_age_ms: int = 0
    budget_status: BudgetStatus = BudgetStatus.NOT_APPLICABLE
    disagreement_kind: DisagreementKind = DisagreementKind.NONE
    evidence_url: str = ""

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        for key in (
            "environment",
            "stage",
            "status",
            "failure_domain",
            "budget_status",
            "disagreement_kind",
        ):
            data[key] = data[key].value
        return data


def classify_budget(
    duration_ms: int,
    *,
    deadline_ms: int,
    soft_budget_ms: int | None = None,
) -> BudgetStatus:
    if duration_ms < 0 or deadline_ms < 0:
        raise ValueError("duration_ms and deadline_ms must be non-negative")
    if deadline_ms == 0:
        return BudgetStatus.NOT_APPLICABLE
    if duration_ms > deadline_ms:
        return BudgetStatus.HARD_BREACH
    if soft_budget_ms is not None and duration_ms > soft_budget_ms:
        return BudgetStatus.SOFT_BREACH
    return BudgetStatus.WITHIN_BUDGET


def make_stage_result(
    *,
    source: str,
    environment: str | PipelineEnvironment,
    stage: str | PipelineStage,
    target: str,
    status: str | StageStatus,
    duration_ms: int = 0,
    deadline_ms: int | None = None,
    failure_domain: str | FailureDomain = FailureDomain.NONE,
    external_dependency: bool = False,
    suppressed_reason: str = "",
    skipped_reason: str = "",
    current_stage_age_ms: int = 0,
    evidence_url: str = "",
) -> StageResult:
    env_value = PipelineEnvironment(environment)
    stage_value = PipelineStage(stage)
    status_value = StageStatus(status)
    domain_value = FailureDomain(failure_domain)
    resolved_deadline_ms = STAGE_DEADLINE_MS[stage_value] if deadline_ms is None else deadline_ms

    result = StageResult(
        source=source,
        environment=env_value,
        stage=stage_value,
        target=target,
        status=status_value,
        duration_ms=duration_ms,
        deadline_ms=resolved_deadline_ms,
        failure_domain=domain_value,
        external_dependency=external_dependency,
        suppressed_reason=suppressed_reason,
        skipped_reason=skipped_reason,
        current_stage_age_ms=current_stage_age_ms,
        budget_status=classify_budget(duration_ms, deadline_ms=resolved_deadline_ms),
        evidence_url=evidence_url,
    )
    validate_stage_result(result)
    return result


def validate_stage_result(result: StageResult) -> None:
    if not result.source:
        raise ValueError("source is required")
    if not result.target:
        raise ValueError("target is required")
    if result.duration_ms < 0:
        raise ValueError("duration_ms must be non-negative")
    if result.deadline_ms < 0:
        raise ValueError("deadline_ms must be non-negative")
    if result.status == StageStatus.FAIL and result.failure_domain == FailureDomain.NONE:
        raise ValueError("failed stages must include failure_domain")
    if result.status == StageStatus.SKIP and not (
        result.skipped_reason or result.suppressed_reason
    ):
        raise ValueError("skipped stages must include skipped_reason or suppressed_reason")
    if result.current_stage_age_ms < 0:
        raise ValueError("current_stage_age_ms must be non-negative")


def acceleration_allowed(result: StageResult) -> bool:
    if result.status != StageStatus.SKIP:
        return False
    if result.environment == PipelineEnvironment.PRODUCTION:
        return False
    if not (result.skipped_reason or result.suppressed_reason):
        return False
    return result.stage in {
        PipelineStage.FULL_UT,
        PipelineStage.INTEGRATION,
        PipelineStage.REGRESSION_E2E,
        PipelineStage.IMAGE_BUILD,
        PipelineStage.PROVIDER_GATE,
    }


def detect_disagreement(results: list[StageResult]) -> DisagreementKind:
    if _has_pass(results, PipelineStage.WATCHDOG) and _has_fail(
        results, FailureDomain.TRAEFIK_PUBLIC_ROUTE
    ):
        return DisagreementKind.INTERNAL_HEALTH_PUBLIC_ROUTE
    if _has_fail(results, FailureDomain.HEARTBEAT_STALE) and _has_pass(
        results, PipelineStage.ROUTE_CANARY
    ):
        return DisagreementKind.HEARTBEAT_PUBLIC_ROUTE
    if _has_fail(results, FailureDomain.DOKPLOY_WORKER_OR_DEPLOYMENT_RECORD) and _has_pass(
        results, PipelineStage.WATCHDOG
    ):
        return DisagreementKind.FALLBACK_PUBLIC_ROUTE
    return DisagreementKind.NONE


def _has_pass(results: list[StageResult], stage: PipelineStage) -> bool:
    return any(result.stage == stage and result.status == StageStatus.PASS for result in results)


def _has_fail(results: list[StageResult], domain: FailureDomain) -> bool:
    return any(
        result.status == StageStatus.FAIL and result.failure_domain == domain for result in results
    )
