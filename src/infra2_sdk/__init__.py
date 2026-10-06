"""Stable contracts shared by infra2 and application repositories."""

from importlib.metadata import PackageNotFoundError, version
from typing import Any

try:
    __version__ = version("infra2-sdk")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.0.0"

from infra2_sdk import (
    capacity,
    ci,
    delivery,
    deploy,
    deploy_health,
    dispatch,
    manifests,
    refs,
    release,
    routing,
    rules,
    runtime,
    secrets,
    snapshot,
    transport,
)
from infra2_sdk.capacity import (
    CapacityLimit,
    CapacityReading,
    OnePasswordCapacityReport,
    cloudflare_readings,
    onepassword_capacity,
)
from infra2_sdk.ci import (
    load_delivery_stages,
    validate_gate,
    validate_inventory,
    validate_manifest_offline,
)
from infra2_sdk.delivery import (
    BudgetStatus,
    DisagreementKind,
    FailureDomain,
    PipelineStage,
    StageResult,
    StageStatus,
    make_stage_result,
    validate_stage_result,
)
from infra2_sdk.deploy import (
    CONTRACT_VERSION,
    DeployEvidence,
    DeployOperation,
    DeployRequest,
    DeployState,
    DeployStatus,
    DeployType,
    ProductionEvidencePolicy,
    build_deploy_request,
    canonical_json,
    derive_release_evidence,
    fetch_production_evidence_policy,
    verify_production_evidence,
)
from infra2_sdk.deploy_health import (
    HealthCheckResult,
    poll_until_healthy,
)
from infra2_sdk.dispatch import (
    ReceiverRun,
    dispatch_and_wait,
)
from infra2_sdk.manifests import (
    ManifestSpec,
)
from infra2_sdk.refs import (
    CommandRunner,
    ResolvedRef,
    classify_ref,
    ls_remote_rows,
    redact_repo,
    resolve_image_ref,
    resolve_pr,
    resolve_to_sha,
)
from infra2_sdk.release import (
    ReleaseError,
    ReleaseIdentity,
    resolve_release_identity,
    verify_runtime_identity,
)
from infra2_sdk.routing import (
    CANARY_SLOT,
    DEFAULT_BASE_DOMAIN,
    LEGACY_CANARY_PR,
    AppRoutePreference,
    DokployDomainSpec,
    RouteEndpoint,
    resolve_app_hostname,
    resolve_dokploy_domains,
    resolve_service_url,
)
from infra2_sdk.secrets import (
    EnvBackend,
    OnePasswordBackend,
    SecretsBackend,
    SecretsResolver,
    TokenStatus,
    VaultKvBackend,
    vault_token_status,
)
from infra2_sdk.snapshot import (
    AnonymizedSnapshotManifest,
    ResidualScanProof,
    ResidualScanStatus,
    SnapshotArtifact,
    verify_snapshot_artifact,
)
from infra2_sdk.transport import (
    HttpResponse,
    HttpTransport,
    urllib_transport,
)


def __getattr__(name: str) -> Any:
    if name == "images":
        import importlib

        return importlib.import_module("infra2_sdk.images")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Top-level version
    "__version__",
    # Submodules
    "capacity",
    "ci",
    "delivery",
    "deploy",
    "deploy_health",
    "dispatch",
    "images",
    "manifests",
    "refs",
    "release",
    "routing",
    "rules",
    "runtime",
    "secrets",
    "snapshot",
    "transport",
    # Capacity
    "CapacityLimit",
    "CapacityReading",
    "OnePasswordCapacityReport",
    "cloudflare_readings",
    "onepassword_capacity",
    # CI
    "load_delivery_stages",
    "validate_gate",
    "validate_inventory",
    "validate_manifest_offline",
    # Delivery
    "BudgetStatus",
    "DisagreementKind",
    "FailureDomain",
    "PipelineStage",
    "StageResult",
    "StageStatus",
    "make_stage_result",
    "validate_stage_result",
    # Deploy
    "CONTRACT_VERSION",
    "DeployEvidence",
    "DeployOperation",
    "DeployRequest",
    "DeployState",
    "DeployStatus",
    "DeployType",
    "ProductionEvidencePolicy",
    "build_deploy_request",
    "canonical_json",
    "derive_release_evidence",
    "fetch_production_evidence_policy",
    "verify_production_evidence",
    # Deploy Health
    "HealthCheckResult",
    "poll_until_healthy",
    # Dispatch
    "ReceiverRun",
    "dispatch_and_wait",
    # Manifests
    "ManifestSpec",
    # Refs
    "CommandRunner",
    "ResolvedRef",
    "classify_ref",
    "ls_remote_rows",
    "redact_repo",
    "resolve_image_ref",
    "resolve_pr",
    "resolve_to_sha",
    # Release
    "ReleaseError",
    "ReleaseIdentity",
    "resolve_release_identity",
    "verify_runtime_identity",
    # Routing
    "AppRoutePreference",
    "CANARY_SLOT",
    "DEFAULT_BASE_DOMAIN",
    "DokployDomainSpec",
    "LEGACY_CANARY_PR",
    "RouteEndpoint",
    "resolve_app_hostname",
    "resolve_dokploy_domains",
    "resolve_service_url",
    # Secrets
    "EnvBackend",
    "OnePasswordBackend",
    "SecretsBackend",
    "SecretsResolver",
    "TokenStatus",
    "VaultKvBackend",
    "vault_token_status",
    # Snapshot
    "AnonymizedSnapshotManifest",
    "ResidualScanProof",
    "ResidualScanStatus",
    "SnapshotArtifact",
    "verify_snapshot_artifact",
    # Transport
    "HttpResponse",
    "HttpTransport",
    "urllib_transport",
]
