"""Provider-neutral runtime contracts and optional standard-protocol adapters."""

from infra2_sdk.runtime import (
    config_schema,
    environment,
    health,
    http,
    identity,
    otel,
    postgres,
    s3,
)
from infra2_sdk.runtime.config_schema import (
    EnvironmentField,
    EnvironmentManifest,
    EnvironmentValidation,
    environment_manifest_from_model,
    manifest_config_fingerprint,
    validate_environment,
)
from infra2_sdk.runtime.environment import (
    APP_OWNED_TIERS,
    PLATFORM_OWNED_TIERS,
    RUNTIME_ENV_CONTRACT_VERSION,
    RUNTIME_ENV_SPECS,
    EnvironmentConflictError,
    EnvironmentTier,
    ResolvedEnvValue,
    RuntimeEnvironment,
    RuntimeEnvKey,
    RuntimeEnvSpec,
    UnknownEnvironmentPolicy,
    environment_from_env,
    normalize_deployment_environment,
    resolve_env,
    resolve_environment_tier,
    resolve_runtime_env,
    runtime_env_contract,
    runtime_env_spec,
    strict_environment_from_env,
    to_deploy_type,
    to_environment_tier,
)
from infra2_sdk.runtime.health import (
    Dependency,
    DependencyCheck,
    DependencyKind,
    DependencyManifest,
    DependencyStatus,
    HealthStatus,
    ProbeResult,
    check_health,
    health_response,
    run_probes,
)
from infra2_sdk.runtime.http import (
    HttpClientSettings,
    create_http_client,
    probe_http,
)
from infra2_sdk.runtime.identity import (
    RuntimeIdentity,
)
from infra2_sdk.runtime.otel import (
    OtelSettings,
    configure_telemetry,
    signal_endpoint,
)
from infra2_sdk.runtime.postgres import (
    PostgresSettings,
    normalize_postgres_dsn,
    probe_postgres,
    redact_postgres_error,
)
from infra2_sdk.runtime.s3 import (
    S3Settings,
    create_s3_client,
    ensure_bucket,
    is_not_found,
    probe_s3,
    read_object_bytes,
    redact_presigned_url,
)

__all__ = [
    # Submodules
    "config_schema",
    "environment",
    "health",
    "http",
    "identity",
    "otel",
    "postgres",
    "s3",
    # Environment
    "APP_OWNED_TIERS",
    "PLATFORM_OWNED_TIERS",
    "EnvironmentTier",
    "RuntimeEnvironment",
    "UnknownEnvironmentPolicy",
    "environment_from_env",
    "normalize_deployment_environment",
    "resolve_environment_tier",
    "strict_environment_from_env",
    "to_deploy_type",
    "to_environment_tier",
    # Environment variables
    "EnvironmentConflictError",
    "RUNTIME_ENV_CONTRACT_VERSION",
    "RUNTIME_ENV_SPECS",
    "ResolvedEnvValue",
    "RuntimeEnvKey",
    "RuntimeEnvSpec",
    "resolve_env",
    "resolve_runtime_env",
    "runtime_env_contract",
    "runtime_env_spec",
    # Identity
    "RuntimeIdentity",
    # Config schema
    "EnvironmentField",
    "EnvironmentManifest",
    "EnvironmentValidation",
    "environment_manifest_from_model",
    "manifest_config_fingerprint",
    "validate_environment",
    # Dependency declarations
    "Dependency",
    "DependencyKind",
    "DependencyManifest",
    # Probes
    "DependencyCheck",
    "DependencyStatus",
    "ProbeResult",
    "run_probes",
    # Health
    "HealthStatus",
    "check_health",
    "health_response",
    # S3
    "S3Settings",
    "create_s3_client",
    "ensure_bucket",
    "is_not_found",
    "probe_s3",
    "read_object_bytes",
    "redact_presigned_url",
    # Postgres
    "PostgresSettings",
    "normalize_postgres_dsn",
    "probe_postgres",
    "redact_postgres_error",
    # HTTP
    "HttpClientSettings",
    "create_http_client",
    "probe_http",
    # OTel
    "OtelSettings",
    "configure_telemetry",
    "signal_endpoint",
]
