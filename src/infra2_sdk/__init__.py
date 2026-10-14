"""Stable contracts shared by infra2 and application repositories."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("infra2-sdk")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.0.0"

from infra2_sdk import (
    capacity,
    ci,
    delivery,
    deploy,
    fastapi,
    manifests,
    refs,
    routing,
    rules,
    runtime,
    scaffold,
    secrets,
    snapshot,
    transport,
)
from infra2_sdk.capacity import (
    CapacityLimit,
    CapacityReading,
    cloudflare_readings,
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
    HealthCheckResult,
    ProductionEvidencePolicy,
    ReceiverRun,
    build_deploy_request,
    canonical_json,
    derive_release_evidence,
    dispatch_and_wait,
    fetch_production_evidence_policy,
    poll_until_healthy,
    verify_production_evidence,
)
from infra2_sdk.manifests import (
    ManifestSpec,
)
from infra2_sdk.refs import (
    CommandRunner,
    ReleaseError,
    ReleaseIdentity,
    ResolvedRef,
    classify_ref,
    ls_remote_rows,
    redact_repo,
    resolve_image_ref,
    resolve_pr,
    resolve_to_sha,
)
from infra2_sdk.routing import (
    CANARY_SLOT,
    DEFAULT_BASE_DOMAIN,
    AppRoutePreference,
    RouteEndpoint,
    resolve_app_hostname,
    resolve_service_url,
)
from infra2_sdk.secrets import (
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
)
from infra2_sdk.transport import (
    HttpResponse,
    HttpTransport,
    urllib_transport,
)

__all__ = [
    # Top-level version
    "__version__",
    # Submodules
    "capacity",
    "ci",
    "delivery",
    "deploy",
    "manifests",
    "refs",
    "routing",
    "rules",
    "runtime",
    "secrets",
    "snapshot",
    "transport",
    # Capacity
    "CapacityLimit",
    "CapacityReading",
    "cloudflare_readings",
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
    # Deploy health polling
    "HealthCheckResult",
    "poll_until_healthy",
    # Deploy dispatch
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
    # Release identity
    "ReleaseError",
    "ReleaseIdentity",
    # Routing
    "AppRoutePreference",
    "CANARY_SLOT",
    "DEFAULT_BASE_DOMAIN",
    "RouteEndpoint",
    "resolve_app_hostname",
    "resolve_service_url",
    # Secrets
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
    # Transport
    "HttpResponse",
    "HttpTransport",
    "urllib_transport",
    # Frameworks & Automation
    "fastapi",
    "scaffold",
]
