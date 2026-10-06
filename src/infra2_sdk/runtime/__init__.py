"""Provider-neutral runtime contracts and optional standard-protocol adapters."""

from infra2_sdk.runtime import (
    config_schema,
    dependencies,
    environ,
    environment,
    health,
    http,
    identity,
    otel,
    postgres,
    probes,
    s3,
)
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
    to_deploy_type,
    to_environment_tier,
)
from infra2_sdk.runtime.health import (
    HealthStatus,
    check_health,
    health_response,
)
from infra2_sdk.runtime.http import (
    HttpCheck,
    HttpClientSettings,
    create_http_client,
    parse_retry_after,
    probe_http,
    retryable_request,
)
from infra2_sdk.runtime.identity import (
    RuntimeIdentity,
    runtime_identity_fingerprint,
)
from infra2_sdk.runtime.otel import (
    OtelSettings,
    configure_telemetry,
    extract_trace_context,
    inject_trace_context,
    signal_endpoint,
)
from infra2_sdk.runtime.postgres import (
    PostgresCheck,
    PostgresSettings,
    normalize_postgres_dsn,
    probe_postgres,
    redact_postgres_error,
)
from infra2_sdk.runtime.probes import (
    DependencyCheck,
    DependencyStatus,
    DependencyUnavailableError,
    ProbeResult,
    assert_required_dependencies,
    run_probes,
)
from infra2_sdk.runtime.s3 import (
    S3Check,
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
    "dependencies",
    "environ",
    "environment",
    "health",
    "http",
    "identity",
    "otel",
    "postgres",
    "probes",
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
    # Environ
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
    "runtime_identity_fingerprint",
    # Config schema
    "EnvironmentField",
    "EnvironmentManifest",
    "EnvironmentValidation",
    "environment_manifest_from_model",
    "manifest_config_fingerprint",
    "settings_json_schema",
    "validate_environment",
    # Dependencies
    "Dependency",
    "DependencyKind",
    "DependencyManifest",
    # Probes
    "DependencyCheck",
    "DependencyStatus",
    "DependencyUnavailableError",
    "ProbeResult",
    "assert_required_dependencies",
    "run_probes",
    # Health
    "HealthStatus",
    "check_health",
    "health_response",
    # S3
    "S3Check",
    "S3Settings",
    "create_s3_client",
    "ensure_bucket",
    "is_not_found",
    "probe_s3",
    "read_object_bytes",
    "redact_presigned_url",
    # Postgres
    "PostgresCheck",
    "PostgresSettings",
    "normalize_postgres_dsn",
    "probe_postgres",
    "redact_postgres_error",
    # HTTP
    "HttpClientSettings",
    "HttpCheck",
    "create_http_client",
    "parse_retry_after",
    "probe_http",
    "retryable_request",
    # OTel
    "OtelSettings",
    "configure_telemetry",
    "extract_trace_context",
    "inject_trace_context",
    "signal_endpoint",
]
