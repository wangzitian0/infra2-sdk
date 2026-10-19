"""Stable contracts shared by infra2 and application repositories."""

from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("infra2-sdk")
except _PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.0.0"

from infra2_sdk import (
    capacity,
    ci,
    delivery,
    deploy,
    fastapi,
    manifests,
    observability,
    refs,
    routing,
    rules,
    runtime,
    scaffold,
    secrets,
    transport,
)
from infra2_sdk.capacity import (
    CapacityReading,
    cloudflare_readings,
)
from infra2_sdk.ci import (
    load_delivery_stages,
    validate_inventory,
    validate_manifest_offline,
)
from infra2_sdk.delivery import (
    FailureDomain,
    StageResult,
    StageStatus,
    make_stage_result,
)
from infra2_sdk.deploy import (
    PRODUCTION_REQUEST_MAX_AGE_SECONDS,
    DeployOperation,
    DeployRequest,
    DeployType,
    ReceiverRun,
    canonical_json,
    dispatch_and_wait,
    poll_until_healthy,
    run_deploy_transport_cli,
    verify_production_evidence,
)
from infra2_sdk.manifests import (
    ManifestSpec,
)
from infra2_sdk.refs import (
    CommandRunner,
    ReleaseError,
    ResolvedRef,
    classify_ref,
    ls_remote_rows,
    redact_repo,
    resolve_branch_to_sha,
    resolve_image_ref,
    resolve_pr,
    resolve_to_sha,
)
from infra2_sdk.routing import (
    CANARY_SLOT,
    PreviewStackCoordinates,
    derive_preview_coordinates,
    resolve_app_hostname,
    resolve_service_url,
)
from infra2_sdk.secrets import (
    OnePasswordBackend,
    SecretsResolver,
    VaultKvBackend,
    vault_token_status,
)
from infra2_sdk.transport import (
    HttpResponse,
    HttpTransport,
    urllib_transport,
)

__all__ = [
    # Submodules
    "capacity",
    "ci",
    "delivery",
    "deploy",
    "manifests",
    "observability",
    "refs",
    "routing",
    "rules",
    "runtime",
    "secrets",
    "transport",
    # Capacity
    "CapacityReading",
    "cloudflare_readings",
    # CI
    "load_delivery_stages",
    "validate_inventory",
    "validate_manifest_offline",
    # Delivery
    "FailureDomain",
    "StageResult",
    "StageStatus",
    "make_stage_result",
    # Deploy
    "DeployOperation",
    "DeployRequest",
    "DeployType",
    "PRODUCTION_REQUEST_MAX_AGE_SECONDS",
    "canonical_json",
    "verify_production_evidence",
    # Deploy health polling
    "poll_until_healthy",
    # Deploy dispatch
    "ReceiverRun",
    "dispatch_and_wait",
    "run_deploy_transport_cli",
    # Manifests
    "ManifestSpec",
    # Refs
    "CommandRunner",
    "ResolvedRef",
    "classify_ref",
    "ls_remote_rows",
    "redact_repo",
    "resolve_branch_to_sha",
    "resolve_image_ref",
    "resolve_pr",
    "resolve_to_sha",
    # Release errors
    "ReleaseError",
    # Routing
    "CANARY_SLOT",
    "PreviewStackCoordinates",
    "derive_preview_coordinates",
    "resolve_app_hostname",
    "resolve_service_url",
    # Secrets
    "OnePasswordBackend",
    "SecretsResolver",
    "VaultKvBackend",
    "vault_token_status",
    # Transport
    "HttpResponse",
    "HttpTransport",
    "urllib_transport",
    # Frameworks & Automation
    "fastapi",
    "scaffold",
]
