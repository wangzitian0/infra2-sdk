"""Normalized delivery evidence shared by CI, CD, probes, and watchdogs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from infra2_sdk.runtime.environment import EnvironmentTier, to_environment_tier

__all__ = [
    "CanaryReport",
    "FailureDomain",
    "SchemaDiscrepancy",
    "SmokeCheck",
    "SmokeCheckResult",
    "StageResult",
    "StageStatus",
    "compare_enum_definitions",
    "make_stage_result",
    "validate_schema_compatibility",
]


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


_STAGE_DEADLINE_MS: dict[PipelineStage, int] = {
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
    PipelineStage.WATCHDOG: 120_000,
}


@dataclass(frozen=True)
class StageResult:
    source: str
    environment: EnvironmentTier
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
    environment: str | EnvironmentTier,
    stage: str | PipelineStage,
    target: str,
    status: str | StageStatus,
    duration_ms: int = 0,
    deadline_ms: int | None = None,
    failure_domain: str | FailureDomain = FailureDomain.NONE,
    disagreement_kind: str | DisagreementKind = DisagreementKind.NONE,
    external_dependency: bool = False,
    suppressed_reason: str = "",
    skipped_reason: str = "",
    current_stage_age_ms: int = 0,
    evidence_url: str = "",
) -> StageResult:
    if isinstance(environment, EnvironmentTier):
        env_value = environment
    else:
        env_value = to_environment_tier(environment)
    stage_value = PipelineStage(stage)
    status_value = StageStatus(status)
    domain_value = FailureDomain(failure_domain)
    resolved_deadline_ms = _STAGE_DEADLINE_MS[stage_value] if deadline_ms is None else deadline_ms

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
        disagreement_kind=DisagreementKind(disagreement_kind)
        if isinstance(disagreement_kind, str)
        else disagreement_kind,
        evidence_url=evidence_url,
    )
    _validate_stage_result(result)
    return result


def _validate_stage_result(result: StageResult) -> None:
    if not isinstance(result.environment, EnvironmentTier):
        got = type(result.environment).__name__
        raise TypeError(f"result.environment must be an EnvironmentTier, got {got}")
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


@dataclass(frozen=True)
class SmokeCheckResult:
    """Normalized outcome for a single post-deploy or canary smoke check."""

    name: str
    passed: bool
    duration_ms: int = 0
    detail: str = ""
    failure_domain: FailureDomain = FailureDomain.NONE
    evidence_url: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("name is required")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["failure_domain"] = self.failure_domain.value
        return data


@runtime_checkable
class SmokeCheck(Protocol):
    """Pure protocol for pluggable smoke checks across app and infra repos."""

    def check(self, target: Any) -> SmokeCheckResult:
        """Execute a smoke check against target and return a SmokeCheckResult."""
        ...


@dataclass(frozen=True)
class CanaryReport:
    """Aggregated smoke-check report for a canary or post-swap verification run."""

    service: str
    environment: EnvironmentTier
    version: str
    checks: tuple[SmokeCheckResult, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.service:
            raise ValueError("service is required")
        if not self.version:
            raise ValueError("version is required")

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(item.passed for item in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "service": self.service,
            "environment": self.environment.value,
            "version": self.version,
            "passed": self.passed,
            "checks": [item.to_dict() for item in self.checks],
        }


@dataclass(frozen=True)
class SchemaDiscrepancy:
    """Discrepancy between code-declared enum values and database enum values."""

    enum_name: str
    missing_in_db: tuple[str, ...] = ()
    missing_in_code: tuple[str, ...] = ()
    casing_mismatches: tuple[tuple[str, str], ...] = ()

    @property
    def is_compatible(self) -> bool:
        return not self.missing_in_db and not self.casing_mismatches

    def to_dict(self) -> dict[str, object]:
        return {
            "enum_name": self.enum_name,
            "missing_in_db": list(self.missing_in_db),
            "missing_in_code": list(self.missing_in_code),
            "casing_mismatches": [
                {"code": code_val, "db": db_val}
                for code_val, db_val in self.casing_mismatches
            ],
            "is_compatible": self.is_compatible,
        }


def compare_enum_definitions(
    code_enums: Mapping[str, Iterable[str]],
    db_enums: Mapping[str, Iterable[str]],
) -> list[SchemaDiscrepancy]:
    """Compare code enum value sets against database enum value sets without I/O.

    Detects values present in code but missing in DB, values present in DB but
    missing in code, and case-only divergences (separated from missing sets).
    """
    discrepancies: list[SchemaDiscrepancy] = []
    all_names = sorted(set(code_enums.keys()) | set(db_enums.keys()))
    for name in all_names:
        code_set = set(code_enums.get(name, ()))
        db_set = set(db_enums.get(name, ()))
        raw_missing_db = code_set - db_set
        raw_missing_code = db_set - code_set

        db_lower_map: dict[str, list[str]] = {}
        for val in sorted(raw_missing_code):
            db_lower_map.setdefault(val.lower(), []).append(val)

        casing_pairs: list[tuple[str, str]] = []
        matched_code: set[str] = set()
        matched_db: set[str] = set()
        for code_val in sorted(raw_missing_db):
            candidates = db_lower_map.get(code_val.lower(), [])
            if candidates:
                for db_val in candidates:
                    casing_pairs.append((code_val, db_val))
                    matched_db.add(db_val)
                matched_code.add(code_val)

        missing_in_db = tuple(sorted(raw_missing_db - matched_code))
        missing_in_code = tuple(sorted(raw_missing_code - matched_db))
        casing_mismatches = tuple(casing_pairs)

        if missing_in_db or missing_in_code or casing_mismatches:
            discrepancies.append(
                SchemaDiscrepancy(
                    enum_name=name,
                    missing_in_db=missing_in_db,
                    missing_in_code=missing_in_code,
                    casing_mismatches=casing_mismatches,
                )
            )
    return discrepancies


def validate_schema_compatibility(
    code_enums: Mapping[str, Iterable[str]],
    db_enums: Mapping[str, Iterable[str]],
    *,
    allow_extra_db_values: bool = True,
) -> list[SchemaDiscrepancy]:
    """Return blocking schema discrepancies between code and DB enums.

    By default, values present in DB but absent from code are allowed for
    expand-first migrations, while missing DB values or casing mismatches block.
    """
    all_diffs = compare_enum_definitions(code_enums, db_enums)
    if not allow_extra_db_values:
        return all_diffs
    return [diff for diff in all_diffs if not diff.is_compatible]

