"""Provider-neutral runtime contracts and optional standard-protocol adapters."""

from typing import Any

from infra2_sdk.runtime.config_schema import (
    EnvironmentField,
    EnvironmentManifest,
    EnvironmentValidation,
    environment_manifest_from_model,
    manifest_config_fingerprint,
    settings_json_schema,
    validate_environment,
)
from infra2_sdk.runtime.dependencies import (
    Dependency,
    DependencyKind,
    DependencyManifest,
)
from infra2_sdk.runtime.environ import (
    RUNTIME_ENV_CONTRACT_VERSION,
    RUNTIME_ENV_SPECS,
    EnvironmentConflictError,
    ResolvedEnvValue,
    RuntimeEnvKey,
    RuntimeEnvSpec,
    resolve_env,
    resolve_runtime_env,
    runtime_env_contract,
    runtime_env_spec,
)
from infra2_sdk.runtime.environment import (
    APP_OWNED_TIERS,
    PLATFORM_OWNED_TIERS,
    EnvironmentTier,
    RuntimeEnvironment,
    UnknownEnvironmentPolicy,
    environment_from_env,
    normalize_deployment_environment,
    resolve_environment_tier,
    strict_environment_from_env,
)
from infra2_sdk.runtime.identity import (
    RuntimeIdentity,
    runtime_identity_fingerprint,
)
from infra2_sdk.runtime.probes import (
    DependencyStatus,
    DependencyUnavailableError,
    ProbeResult,
    assert_required_dependencies,
    run_probes,
)


def configuration_fingerprint(*args: Any, **kwargs: Any) -> str:
    """Polymorphic configuration fingerprint dispatcher for backward compatibility.

    - If 2 or more positional arguments are provided, or 'manifest' is in kwargs, or the 1st
      argument is an EnvironmentManifest, dispatches to manifest_config_fingerprint.
    - Otherwise dispatches to runtime_identity_fingerprint.
    """
    if len(args) >= 2 or "manifest" in kwargs:
        return manifest_config_fingerprint(*args, **kwargs)
    if args and isinstance(args[0], EnvironmentManifest):
        return manifest_config_fingerprint(*args, **kwargs)
    return runtime_identity_fingerprint(*args, **kwargs)


__all__ = [
    "APP_OWNED_TIERS",
    "PLATFORM_OWNED_TIERS",
    "Dependency",
    "DependencyKind",
    "DependencyManifest",
    "DependencyStatus",
    "DependencyUnavailableError",
    "EnvironmentField",
    "EnvironmentConflictError",
    "EnvironmentManifest",
    "EnvironmentTier",
    "EnvironmentValidation",
    "ProbeResult",
    "RUNTIME_ENV_CONTRACT_VERSION",
    "RUNTIME_ENV_SPECS",
    "RuntimeIdentity",
    "RuntimeEnvironment",
    "RuntimeEnvKey",
    "RuntimeEnvSpec",
    "ResolvedEnvValue",
    "UnknownEnvironmentPolicy",
    "assert_required_dependencies",
    "configuration_fingerprint",
    "environment_manifest_from_model",
    "environment_from_env",
    "manifest_config_fingerprint",
    "normalize_deployment_environment",
    "resolve_env",
    "resolve_environment_tier",
    "resolve_runtime_env",
    "runtime_env_contract",
    "runtime_env_spec",
    "runtime_identity_fingerprint",
    "run_probes",
    "settings_json_schema",
    "strict_environment_from_env",
    "validate_environment",
]
