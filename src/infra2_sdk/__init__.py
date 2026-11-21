"""Stable contracts shared by infra2 and application repositories."""

from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("infra2-sdk")
except _PackageNotFoundError:  # pragma: no cover
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
    secrets,
    transport,
)
from infra2_sdk.capacity import CapacityReading, cloudflare_readings
from infra2_sdk.ci import load_delivery_stages, validate_inventory, validate_manifest_offline
from infra2_sdk.deploy import (
    PRODUCTION_REQUEST_MAX_AGE_SECONDS,
    DeployOperation,
    DeployRequest,
    DeployType,
    poll_until_healthy,
    verify_production_evidence,
)
from infra2_sdk.manifests import (
    CELL_POLICIES,
    CURRENT_HOOKS_SCHEMA_VERSION,
    POLICY_VERSION,
    STAGE_HOOK_PLANS,
    CellOverrides,
    CellPolicy,
    CheckRow,
    HookRunResult,
    ManifestSpec,
    OverrideReason,
    RuntimeHooksManifest,
    StageHookPlan,
    format_check_grid,
    load_hooks_manifest,
    resolve_check_cells,
    run_cell,
    stage_hook_plan,
)
from infra2_sdk.refs import (
    ReleaseError,
    ResolvedRef,
    classify_ref,
    next_patch_tag,
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
from infra2_sdk.stage_record import StageRecord
from infra2_sdk.transport import HttpTransport, urllib_transport

__all__ = [
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
    "CapacityReading",
    "cloudflare_readings",
    "load_delivery_stages",
    "validate_inventory",
    "validate_manifest_offline",
    "DeployOperation",
    "DeployRequest",
    "DeployType",
    "PRODUCTION_REQUEST_MAX_AGE_SECONDS",
    "verify_production_evidence",
    "poll_until_healthy",
    "CELL_POLICIES",
    "CURRENT_HOOKS_SCHEMA_VERSION",
    "POLICY_VERSION",
    "STAGE_HOOK_PLANS",
    "CellOverrides",
    "CellPolicy",
    "CheckRow",
    "HookRunResult",
    "ManifestSpec",
    "OverrideReason",
    "RuntimeHooksManifest",
    "StageHookPlan",
    "StageRecord",
    "format_check_grid",
    "load_hooks_manifest",
    "resolve_check_cells",
    "run_cell",
    "stage_hook_plan",
    "ResolvedRef",
    "classify_ref",
    "next_patch_tag",
    "resolve_image_ref",
    "resolve_pr",
    "resolve_to_sha",
    "ReleaseError",
    "CANARY_SLOT",
    "PreviewStackCoordinates",
    "derive_preview_coordinates",
    "resolve_app_hostname",
    "resolve_service_url",
    "OnePasswordBackend",
    "SecretsResolver",
    "VaultKvBackend",
    "vault_token_status",
    "HttpTransport",
    "urllib_transport",
    "fastapi",
]
