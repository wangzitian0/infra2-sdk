"""Deterministic, side-effect-free resolution of the SDK environment vocabulary."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class RuntimeEnvKey(StrEnum):
    """Canonical environment variables owned by the portable runtime contract."""

    ENVIRONMENT = "ENVIRONMENT"
    SERVICE_NAME = "OTEL_SERVICE_NAME"
    SERVICE_VERSION = "SERVICE_VERSION"
    GIT_COMMIT_SHA = "GIT_COMMIT_SHA"
    IMAGE_DIGEST = "IMAGE_DIGEST"
    CONFIGURATION_SHA256 = "CONFIGURATION_SHA256"
    RELEASE_ID = "RELEASE_ID"
    INSTANCE_ID = "INSTANCE_ID"
    DATABASE_URL = "DATABASE_URL"
    DATABASE_CONNECT_TIMEOUT_SECONDS = "DATABASE_CONNECT_TIMEOUT_SECONDS"
    OBJECT_STORAGE_PROTOCOL = "OBJECT_STORAGE_PROTOCOL"
    S3_BUCKET = "S3_BUCKET"
    S3_ENDPOINT_URL = "AWS_ENDPOINT_URL_S3"
    S3_REGION = "AWS_REGION"
    AWS_ACCESS_KEY_ID = "AWS_ACCESS_KEY_ID"
    AWS_SECRET_ACCESS_KEY = "AWS_SECRET_ACCESS_KEY"
    AWS_SESSION_TOKEN = "AWS_SESSION_TOKEN"
    S3_CONNECT_TIMEOUT_SECONDS = "S3_CONNECT_TIMEOUT_SECONDS"
    S3_READ_TIMEOUT_SECONDS = "S3_READ_TIMEOUT_SECONDS"
    S3_ADDRESSING_STYLE = "S3_ADDRESSING_STYLE"
    OTEL_EXPORTER_OTLP_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
    OTEL_RESOURCE_ATTRIBUTES = "OTEL_RESOURCE_ATTRIBUTES"
    OTEL_SDK_DISABLED = "OTEL_SDK_DISABLED"
    OTEL_METRIC_EXPORT_INTERVAL = "OTEL_METRIC_EXPORT_INTERVAL"
    HTTP_TIMEOUT_SECONDS = "HTTP_TIMEOUT_SECONDS"
    HTTP_CONNECT_TIMEOUT_SECONDS = "HTTP_CONNECT_TIMEOUT_SECONDS"
    HTTP_MAX_CONNECTIONS = "HTTP_MAX_CONNECTIONS"
    HTTP_MAX_KEEPALIVE_CONNECTIONS = "HTTP_MAX_KEEPALIVE_CONNECTIONS"
    HTTP_CONNECT_RETRIES = "HTTP_CONNECT_RETRIES"
    HTTP_USER_AGENT = "HTTP_USER_AGENT"
    HTTP_FOLLOW_REDIRECTS = "HTTP_FOLLOW_REDIRECTS"


RUNTIME_ENV_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class RuntimeEnvSpec:
    key: RuntimeEnvKey
    aliases: tuple[str, ...] = ()
    sensitive: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.key.value,
            "aliases": list(self.aliases),
            "sensitive": self.sensitive,
        }


RUNTIME_ENV_SPECS = (
    RuntimeEnvSpec(RuntimeEnvKey.ENVIRONMENT, ("ENV", "APP_ENV")),
    RuntimeEnvSpec(RuntimeEnvKey.SERVICE_NAME, ("SERVICE_NAME",)),
    RuntimeEnvSpec(RuntimeEnvKey.SERVICE_VERSION, ("IMAGE_TAG",)),
    RuntimeEnvSpec(RuntimeEnvKey.GIT_COMMIT_SHA),
    RuntimeEnvSpec(RuntimeEnvKey.IMAGE_DIGEST),
    RuntimeEnvSpec(RuntimeEnvKey.CONFIGURATION_SHA256),
    RuntimeEnvSpec(RuntimeEnvKey.RELEASE_ID),
    RuntimeEnvSpec(RuntimeEnvKey.INSTANCE_ID),
    RuntimeEnvSpec(RuntimeEnvKey.DATABASE_URL, sensitive=True),
    RuntimeEnvSpec(RuntimeEnvKey.DATABASE_CONNECT_TIMEOUT_SECONDS),
    RuntimeEnvSpec(RuntimeEnvKey.OBJECT_STORAGE_PROTOCOL, ("OBJECT_STORAGE_DRIVER",)),
    RuntimeEnvSpec(RuntimeEnvKey.S3_BUCKET),
    RuntimeEnvSpec(RuntimeEnvKey.S3_ENDPOINT_URL, ("S3_ENDPOINT_URL", "S3_ENDPOINT")),
    RuntimeEnvSpec(RuntimeEnvKey.S3_REGION, ("AWS_DEFAULT_REGION", "S3_REGION")),
    RuntimeEnvSpec(RuntimeEnvKey.AWS_ACCESS_KEY_ID, ("S3_ACCESS_KEY",), sensitive=True),
    RuntimeEnvSpec(RuntimeEnvKey.AWS_SECRET_ACCESS_KEY, ("S3_SECRET_KEY",), sensitive=True),
    RuntimeEnvSpec(RuntimeEnvKey.AWS_SESSION_TOKEN, ("S3_SESSION_TOKEN",), sensitive=True),
    RuntimeEnvSpec(RuntimeEnvKey.S3_CONNECT_TIMEOUT_SECONDS),
    RuntimeEnvSpec(RuntimeEnvKey.S3_READ_TIMEOUT_SECONDS),
    RuntimeEnvSpec(RuntimeEnvKey.S3_ADDRESSING_STYLE),
    RuntimeEnvSpec(RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT),
    RuntimeEnvSpec(RuntimeEnvKey.OTEL_RESOURCE_ATTRIBUTES),
    RuntimeEnvSpec(RuntimeEnvKey.OTEL_SDK_DISABLED),
    RuntimeEnvSpec(RuntimeEnvKey.OTEL_METRIC_EXPORT_INTERVAL),
    RuntimeEnvSpec(RuntimeEnvKey.HTTP_TIMEOUT_SECONDS),
    RuntimeEnvSpec(RuntimeEnvKey.HTTP_CONNECT_TIMEOUT_SECONDS),
    RuntimeEnvSpec(RuntimeEnvKey.HTTP_MAX_CONNECTIONS),
    RuntimeEnvSpec(RuntimeEnvKey.HTTP_MAX_KEEPALIVE_CONNECTIONS),
    RuntimeEnvSpec(RuntimeEnvKey.HTTP_CONNECT_RETRIES),
    RuntimeEnvSpec(RuntimeEnvKey.HTTP_USER_AGENT),
    RuntimeEnvSpec(RuntimeEnvKey.HTTP_FOLLOW_REDIRECTS),
)

_SPECS_BY_KEY = {spec.key: spec for spec in RUNTIME_ENV_SPECS}


def runtime_env_spec(key: str | RuntimeEnvKey) -> RuntimeEnvSpec:
    return _SPECS_BY_KEY[RuntimeEnvKey(key)]


def runtime_env_contract() -> dict[str, object]:
    """Return the stable, serializable vocabulary consumed by platform canaries."""

    return {
        "contract_version": RUNTIME_ENV_CONTRACT_VERSION,
        "variables": [spec.to_dict() for spec in RUNTIME_ENV_SPECS],
    }


def resolve_runtime_env(
    environ: Mapping[str, str] | None,
    key: str | RuntimeEnvKey,
    *,
    default: str | None = None,
    required: bool = False,
) -> ResolvedEnvValue:
    """Resolve a registered key using its single-source aliases and sensitivity."""

    spec = runtime_env_spec(key)
    return resolve_env(
        environ,
        spec.key,
        aliases=spec.aliases,
        default=default,
        required=required,
        sensitive=spec.sensitive,
    )


class EnvironmentConflictError(ValueError):
    """Raised when canonical and compatibility names disagree."""


@dataclass(frozen=True)
class ResolvedEnvValue:
    value: str | None
    source: str | None


def resolve_env(
    environ: Mapping[str, str] | None,
    key: str | RuntimeEnvKey,
    *,
    aliases: tuple[str, ...] = (),
    default: str | None = None,
    required: bool = False,
    sensitive: bool = False,
) -> ResolvedEnvValue:
    """Resolve one variable and reject ambiguous aliases without logging values."""

    values = os.environ if environ is None else environ
    canonical = str(key)
    candidates = (canonical, *aliases)
    present: list[tuple[str, str]] = []
    for name in candidates:
        raw = values.get(name)
        if isinstance(raw, str) and raw.strip():
            present.append((name, raw if sensitive else raw.strip()))

    distinct = {value for _, value in present}
    if len(distinct) > 1:
        names = ", ".join(name for name, _ in present)
        label = "sensitive environment variables" if sensitive else "environment variables"
        raise EnvironmentConflictError(f"conflicting {label} for {canonical}: {names}")

    if present:
        by_name = dict(present)
        for name in candidates:
            if name in by_name:
                return ResolvedEnvValue(by_name[name], name)
    if required and default is None:
        raise ValueError(f"{canonical} is required")
    return ResolvedEnvValue(default, None)


def env_int(
    environ: Mapping[str, str] | None,
    key: str | RuntimeEnvKey,
    *,
    default: int,
    aliases: tuple[str, ...] = (),
) -> int:
    resolved = resolve_env(environ, key, aliases=aliases)
    if resolved.value is None:
        return default
    try:
        return int(resolved.value)
    except ValueError:
        raise ValueError(f"{key} must be an integer") from None


def env_float(
    environ: Mapping[str, str] | None,
    key: str | RuntimeEnvKey,
    *,
    default: float,
    aliases: tuple[str, ...] = (),
) -> float:
    resolved = resolve_env(environ, key, aliases=aliases)
    if resolved.value is None:
        return default
    try:
        value = float(resolved.value)
    except ValueError:
        raise ValueError(f"{key} must be a floating-point number") from None
    if not math.isfinite(value):
        raise ValueError(f"{key} must be finite")
    return value


def env_bool(
    environ: Mapping[str, str] | None,
    key: str | RuntimeEnvKey,
    *,
    default: bool,
    aliases: tuple[str, ...] = (),
) -> bool:
    resolved = resolve_env(environ, key, aliases=aliases)
    if resolved.value is None:
        return default
    normalized = resolved.value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be a boolean")
