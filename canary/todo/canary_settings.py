"""The environment contract of the Canary Todo: one class, read by app, worker, and config CLI.

`CanarySettings.from_environ()` is the one reader of the process environment in `app.py`.
No default names a host of one platform. A deployment gives each address; a local run gives
them in `compose.yaml`. An address that is not set makes its check `unconfigured`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field


def _field(default: Any, description: str, **extra: Any) -> Any:
    return Field(default=default, description=description, json_schema_extra=extra or None)


class CanarySettings(BaseModel):
    """Each field name in upper case is the environment variable name."""

    port: int = _field(8000, "HTTP port of the service.")

    # Postgres: one login for the todo store and for the Postgres check.
    database_url: str = Field(
        description="PostgreSQL URL with host, user and password.",
        json_schema_extra={"source": "runtime", "sensitive": True, "group": "postgres"},
    )

    # Redis check.
    redis_url: str = _field(
        "",
        "Redis connection URL with credentials. The Redis check is unconfigured without it.",
        source="runtime",
        sensitive=True,
        empty_ok=True,
        group="redis",
    )

    # S3 check.
    s3_endpoint: str = _field("", "S3 endpoint URL of the S3 check.", empty_ok=True, group="s3")
    s3_bucket: str = _field("", "Bucket of the S3 check.", empty_ok=True, group="s3")
    s3_access_key: str = _field(
        "", "S3 access key.", source="human", sensitive=True, empty_ok=True, group="s3"
    )
    s3_secret_key: str = _field(
        "", "S3 secret key.", source="human", sensitive=True, empty_ok=True, group="s3"
    )

    # Informational liveness checks. They never gate the verdict.
    signoz_otel_collector: str = _field(
        "", "Health URL of the SigNoz collector.", empty_ok=True, group="liveness"
    )
    openpanel_api_url: str = _field("", "Health URL of OpenPanel.", empty_ok=True, group="liveness")
    openpanel_health_url: str = _field(
        "", "Internal OpenPanel health URL for liveness checks.", empty_ok=True, group="liveness"
    )
    authentik_endpoint: str = _field(
        "", "Liveness URL of Authentik.", empty_ok=True, group="liveness"
    )

    # Capabilities injected by x-capabilities (openpanel, browser_otel) in App-peer stacks.
    openpanel_client_id: str = _field(
        "",
        "OpenPanel client ID from x-capabilities openpanel.",
        empty_ok=True,
        group="frontend_telemetry",
    )
    openpanel_script_url: str = _field(
        "",
        "OpenPanel script URL from x-capabilities openpanel.",
        empty_ok=True,
        group="frontend_telemetry",
    )
    next_public_otel_exporter_otlp_endpoint: str = _field(
        "",
        "Browser OTLP endpoint from x-capabilities browser_otel.",
        empty_ok=True,
        group="frontend_telemetry",
    )
    next_public_deployment_environment: str = _field(
        "",
        "Browser deployment environment from x-capabilities browser_otel.",
        empty_ok=True,
        group="frontend_telemetry",
    )
    next_public_git_sha: str = _field(
        "",
        "Browser Git SHA from x-capabilities browser_otel.",
        empty_ok=True,
        group="frontend_telemetry",
    )

    # Parameterized service identity and storage prefixes for multi-slot canary deployments.
    canary_service_id: str = _field(
        "platform/todo", "Logical service identifier returned by health endpoints.", group="canary"
    )
    canary_service_name: str = _field(
        "platform-todo", "Telemetry service name and logger identifier.", group="canary"
    )
    canary_domain: str = _field(
        "todo.zitian.party", "Public hostname of this canary slot.", group="canary"
    )
    redis_key_prefix: str = _field(
        "canary:probe:", "Redis key prefix isolated per canary slot.", group="redis"
    )
    s3_key_prefix: str = _field("canary/", "S3 object prefix isolated per canary slot.", group="s3")

    # Read by the SDK, not by app.py: runtime identity, telemetry and the LLM client.
    status_cache_ttl_seconds: float = _field(
        30.0,
        "Canary status probe cache TTL in seconds.",
        group="sdk",
    )
    environment: str = _field("", "Environment tier. The SDK reads it.", empty_ok=True, group="sdk")
    service_version: str = _field(
        "", "Deployed service version. The SDK reads it.", empty_ok=True, group="sdk"
    )
    configuration_sha256: str = _field(
        "", "Configuration hash of the deployment. The SDK reads it.", empty_ok=True, group="sdk"
    )
    git_commit_sha: str = _field(
        "", "Commit of the deployment. The SDK reads it.", empty_ok=True, group="sdk"
    )
    otel_exporter_otlp_endpoint: str = _field(
        "", "OTLP endpoint. Telemetry export is off without it.", empty_ok=True, group="sdk"
    )
    otel_service_name: str = _field(
        "", "Telemetry service name. The SDK reads it.", empty_ok=True, group="sdk"
    )
    otel_resource_attributes: str = _field(
        "", "Telemetry resource attributes. The SDK reads them.", empty_ok=True, group="sdk"
    )
    llm_provider: str = _field("", "LLM provider. The SDK reads it.", empty_ok=True, group="sdk")
    llm_base_url: str = _field("", "LLM base URL. The SDK reads it.", empty_ok=True, group="sdk")
    llm_small_model: str = _field(
        "", "LLM model of the todo summary. The SDK reads it.", empty_ok=True, group="sdk"
    )
    llm_api_key: str = _field(
        "",
        "LLM API key. The todo summary is unconfigured without it.",
        source="human",
        sensitive=True,
        empty_ok=True,
        group="sdk",
    )

    @classmethod
    def env_name(cls, field: str) -> str:
        if field not in cls.model_fields:
            raise KeyError(field)
        return field.upper()

    @classmethod
    def read(cls, environ: Mapping[str, str], field: str) -> str:
        """The text of one declared variable, or an empty string. A sensitive value keeps
        its edge whitespace; a blank value reads as not set."""
        raw = environ.get(cls.env_name(field), "")
        if not raw.strip():
            return ""
        extra = cls.model_fields[field].json_schema_extra
        sensitive = isinstance(extra, Mapping) and bool(extra.get("sensitive"))
        return raw if sensitive else raw.strip()

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> CanarySettings:
        """Read each declared variable. An unset `DATABASE_URL` reads as an empty string:
        startup validation refuses it, and each caller reports it at run time."""
        values: dict[str, str] = {"database_url": ""}
        for field in cls.model_fields:
            if raw := cls.read(environ, field):
                values[field] = raw
        return cls(**values)
