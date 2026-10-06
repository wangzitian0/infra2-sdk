"""Canonical environment vocabulary shared by applications and infra2."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


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
    DATABASE_STATEMENT_TIMEOUT_SECONDS = "DATABASE_STATEMENT_TIMEOUT_SECONDS"
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
    OTEL_TRACES_SAMPLER = "OTEL_TRACES_SAMPLER"
    OTEL_TRACES_SAMPLER_ARG = "OTEL_TRACES_SAMPLER_ARG"
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
    RuntimeEnvSpec(RuntimeEnvKey.DATABASE_STATEMENT_TIMEOUT_SECONDS),
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
    RuntimeEnvSpec(RuntimeEnvKey.OTEL_TRACES_SAMPLER),
    RuntimeEnvSpec(RuntimeEnvKey.OTEL_TRACES_SAMPLER_ARG),
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


class EnvironmentTier(StrEnum):
    """A runtime tier, independent of the backend that provides its dependencies."""

    LOCAL_DEV = "local_dev"
    LOCAL_TEST = "local_test"
    GITHUB_CI = "github_ci"
    PREVIEW = "preview"
    STAGING = "staging"
    PRODUCTION = "production"


class UnknownEnvironmentPolicy(StrEnum):
    """How a consumer handles an environment name outside the shared vocabulary."""

    ERROR = "error"
    PRODUCTION = "production"


APP_OWNED_TIERS = frozenset(
    {
        EnvironmentTier.LOCAL_DEV,
        EnvironmentTier.LOCAL_TEST,
        EnvironmentTier.GITHUB_CI,
    }
)
PLATFORM_OWNED_TIERS = frozenset(
    {
        EnvironmentTier.PREVIEW,
        EnvironmentTier.STAGING,
        EnvironmentTier.PRODUCTION,
    }
)

#: The canary deployment slot, and therefore that slot's ``deployment.environment.name``.
#: Defined here, not in ``routing`` (which re-exports it), because ``routing`` imports this
#: module: one definition, and the tier vocabulary below recognises it as a preview (#54).
CANARY_SLOT = "canary-preview"

_LOCAL_DEV_ALIASES = frozenset({"dev", "development", "local", "local_dev"})
_LOCAL_TEST_ALIASES = frozenset({"test", "testing", "ci", "local_ci", "local_test"})
_PRODUCTION_ALIASES = frozenset({"prod", "production"})
_PREVIEW_ALIAS_RE = re.compile(
    r"\A(?:branch-[a-z0-9][a-z0-9_-]*|pr-[1-9][0-9]*|commit-[0-9a-f]{7,40}|tag-v[0-9]+-[0-9]+-[0-9]+)\Z"
)


@dataclass(frozen=True)
class RuntimeEnvironment:
    """A deployment display name and its portable behavioral tier."""

    name: str
    tier: EnvironmentTier


def to_environment_tier(
    value: str | EnvironmentTier | Any,
    *,
    github_actions: bool = False,
    unknown: str | UnknownEnvironmentPolicy = UnknownEnvironmentPolicy.ERROR,
) -> EnvironmentTier:
    """Map any environment representation to canonical EnvironmentTier.

    Accepts:
    - EnvironmentTier instance
    - DeployType instance or value (STAGING -> STAGING, PRODUCTION -> PRODUCTION,
      PREVIEW_* -> PREVIEW, CANARY -> PREVIEW)
    - Raw environment strings and aliases ("prod", "production", "preview",
      "local", "pr", "local_ci", etc.)
    """
    if isinstance(value, EnvironmentTier):
        return value

    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in ("pr", "canary") or cleaned.startswith("preview/"):
            return EnvironmentTier.PREVIEW

        normalized = cleaned.replace("-", "_")
        if normalized in _LOCAL_DEV_ALIASES:
            return EnvironmentTier.LOCAL_DEV
        if normalized in _LOCAL_TEST_ALIASES:
            return EnvironmentTier.GITHUB_CI if github_actions else EnvironmentTier.LOCAL_TEST
        if normalized == EnvironmentTier.GITHUB_CI.value:
            return EnvironmentTier.GITHUB_CI
        if (
            normalized == EnvironmentTier.PREVIEW.value
            or cleaned == CANARY_SLOT
            or _PREVIEW_ALIAS_RE.match(cleaned)
        ):
            return EnvironmentTier.PREVIEW
        if normalized == EnvironmentTier.STAGING.value:
            return EnvironmentTier.STAGING
        if normalized in _PRODUCTION_ALIASES:
            return EnvironmentTier.PRODUCTION

        policy = UnknownEnvironmentPolicy(unknown)
        if policy is UnknownEnvironmentPolicy.PRODUCTION:
            return EnvironmentTier.PRODUCTION
        raise ValueError(f"unknown environment: {value!r}")

    if hasattr(value, "value") and isinstance(value.value, str):
        return to_environment_tier(value.value, github_actions=github_actions, unknown=unknown)

    raise TypeError(f"cannot convert {type(value).__name__} to EnvironmentTier")


def resolve_environment_tier(
    value: str | EnvironmentTier | Any,
    *,
    github_actions: bool = False,
    unknown: str | UnknownEnvironmentPolicy = UnknownEnvironmentPolicy.ERROR,
) -> EnvironmentTier:
    """Normalize common aliases while making fail-closed behavior explicit.

    Delegates to the canonical implementation `to_environment_tier`.
    """
    if not isinstance(value, (str, EnvironmentTier)):
        raise TypeError("environment must be a string or EnvironmentTier")
    return to_environment_tier(value, github_actions=github_actions, unknown=unknown)


def environment_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    required: bool = False,
    github_actions: bool | None = None,
    unknown: str | UnknownEnvironmentPolicy = UnknownEnvironmentPolicy.ERROR,
) -> RuntimeEnvironment:
    """Resolve locally, optionally requiring an explicit deployed environment."""

    resolved = resolve_runtime_env(
        environ,
        RuntimeEnvKey.ENVIRONMENT,
        default=None if required else EnvironmentTier.LOCAL_DEV.value,
        required=required,
    )
    if resolved.value is None:
        raise ValueError("Resolved environment value must not be None")
    values = os.environ if environ is None else environ
    if github_actions is None:
        github_actions = values.get("GITHUB_ACTIONS", "").strip().lower() == "true"
    tier = to_environment_tier(
        resolved.value,
        github_actions=github_actions,
        unknown=unknown,
    )
    raw_name = resolved.value.strip().lower()
    name = raw_name if tier is EnvironmentTier.PREVIEW and raw_name != "preview" else tier.value
    return RuntimeEnvironment(name=name, tier=tier)


def strict_environment_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    github_actions: bool | None = None,
    unknown: str | UnknownEnvironmentPolicy = UnknownEnvironmentPolicy.ERROR,
) -> RuntimeEnvironment:
    """Require an explicit environment for deployed/protected runtime validation."""

    return environment_from_env(
        environ,
        required=True,
        github_actions=github_actions,
        unknown=unknown,
    )


def normalize_deployment_environment(
    value: str,
    tier: str | EnvironmentTier,
) -> str:
    """Validate a display identity without imposing a platform's preview naming scheme."""

    resolved_tier = to_environment_tier(tier)
    display = value.strip().lower()
    if not display:
        return resolved_tier.value
    if resolved_tier is EnvironmentTier.PREVIEW:
        return display
    if to_environment_tier(display) is not resolved_tier:
        raise ValueError("deployment_environment disagrees with environment tier")
    return resolved_tier.value


def to_deploy_type(
    value: str | EnvironmentTier | Any,
    *,
    preview_variant: str = "branch",
) -> Any:
    """Map an environment representation to canonical DeployType."""
    from infra2_sdk.deploy import DeployType

    if isinstance(value, DeployType):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        try:
            return DeployType(cleaned)
        except ValueError:
            pass
    tier = to_environment_tier(value)
    if tier is EnvironmentTier.STAGING:
        return DeployType.STAGING
    if tier is EnvironmentTier.PRODUCTION:
        return DeployType.PRODUCTION
    if tier is EnvironmentTier.PREVIEW:
        variant = preview_variant.lower().strip()
        if variant == "pr":
            return DeployType.PREVIEW_PR
        if variant == "commit":
            return DeployType.PREVIEW_COMMIT
        if variant == "tag":
            return DeployType.PREVIEW_TAG
        return DeployType.PREVIEW_BRANCH
    raise ValueError(f"cannot map non-deployable tier {tier} to DeployType")


__all__ = [
    "APP_OWNED_TIERS",
    "CANARY_SLOT",
    "EnvironmentConflictError",
    "EnvironmentTier",
    "PLATFORM_OWNED_TIERS",
    "RUNTIME_ENV_CONTRACT_VERSION",
    "RUNTIME_ENV_SPECS",
    "ResolvedEnvValue",
    "RuntimeEnvironment",
    "RuntimeEnvKey",
    "RuntimeEnvSpec",
    "UnknownEnvironmentPolicy",
    "env_bool",
    "env_float",
    "env_int",
    "environment_from_env",
    "normalize_deployment_environment",
    "resolve_env",
    "resolve_environment_tier",
    "resolve_runtime_env",
    "runtime_env_contract",
    "runtime_env_spec",
    "strict_environment_from_env",
    "to_deploy_type",
    "to_environment_tier",
]
