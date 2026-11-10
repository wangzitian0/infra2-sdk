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
from infra2_sdk.deploy import (
    PRODUCTION_REQUEST_MAX_AGE_SECONDS,
    DeployOperation,
    DeployRequest,
    DeployType,
    dispatch_and_wait,
    poll_until_healthy,
    verify_production_evidence,
)
from infra2_sdk.manifests import (
    HOOK_SLOTS,
    HookRunResult,
    HookSlotSpec,
    ManifestSpec,
    RuntimeHooksManifest,
    load_hooks_manifest,
    run_hook_slot,
)
from infra2_sdk.refs import (
    ReleaseError,
    ResolvedRef,
    classify_ref,
    next_patch_tag,
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
    # Deploy
    "DeployOperation",
    "DeployRequest",
    "DeployType",
    "PRODUCTION_REQUEST_MAX_AGE_SECONDS",
    "verify_production_evidence",
    # Deploy health polling
    "poll_until_healthy",
    # Deploy dispatch
    "dispatch_and_wait",
    # Manifests
    "HOOK_SLOTS",
    "HookRunResult",
    "HookSlotSpec",
    "ManifestSpec",
    "RuntimeHooksManifest",
    "load_hooks_manifest",
    "run_hook_slot",
    # Refs
    "ResolvedRef",
    "classify_ref",
    "next_patch_tag",
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
