"""Provider-neutral runtime contracts and optional standard-protocol adapters."""

from infra2_sdk.runtime import (
    config_schema,
    environment,
    health,
    http,
    llm,
    otel,
    postgres,
    s3,
)
from infra2_sdk.runtime.config_schema import (
    EnvironmentField,
    EnvironmentManifest,
    environment_manifest_from_model,
    manifest_config_fingerprint,
)
from infra2_sdk.runtime.environment import (
    RUNTIME_ENV_CONTRACT_VERSION,
    RUNTIME_ENV_SPECS,
    EnvironmentTier,
    RuntimeEnvironment,
    RuntimeEnvKey,
    environment_from_env,
    resolve_environment_tier,
    resolve_runtime_env,
    runtime_env_contract,
    runtime_env_spec,
    to_environment_tier,
)
from infra2_sdk.runtime.health import (
    Dependency,
    DependencyCheck,
    DependencyKind,
    DependencyManifest,
    DependencyStatus,
    ProbeResult,
    check_health,
    run_probes,
)
from infra2_sdk.runtime.http import (
    HttpClientSettings,
    create_http_client,
    probe_http,
)
from infra2_sdk.runtime.llm import (
    LlmSettings,
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
    "llm",
    "otel",
    "postgres",
    "s3",
    # Environment
    "EnvironmentTier",
    "RuntimeEnvironment",
    "environment_from_env",
    "resolve_environment_tier",
    "to_environment_tier",
    # Environment variables
    "RUNTIME_ENV_CONTRACT_VERSION",
    "RUNTIME_ENV_SPECS",
    "RuntimeEnvKey",
    "resolve_runtime_env",
    "runtime_env_contract",
    "runtime_env_spec",
    # Config schema
    "EnvironmentField",
    "EnvironmentManifest",
    "environment_manifest_from_model",
    "manifest_config_fingerprint",
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
    "check_health",
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
    # LLM
    "LlmSettings",
]
