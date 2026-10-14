"""Canonical environment vocabulary shared by applications and infra2."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from infra2_sdk.runtime._otel_env import load_resource_attributes


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
    OTEL_EXPORTER_OTLP_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
    OTEL_RESOURCE_ATTRIBUTES = "OTEL_RESOURCE_ATTRIBUTES"
    OTEL_SDK_DISABLED = "OTEL_SDK_DISABLED"
    OTEL_METRIC_EXPORT_INTERVAL = "OTEL_METRIC_EXPORT_INTERVAL"
    OTEL_TRACES_SAMPLER = "OTEL_TRACES_SAMPLER"
    OTEL_TRACES_SAMPLER_ARG = "OTEL_TRACES_SAMPLER_ARG"


RUNTIME_ENV_CONTRACT_VERSION = 2


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
    RuntimeEnvSpec(RuntimeEnvKey.SERVICE_NAME),
    RuntimeEnvSpec(RuntimeEnvKey.SERVICE_VERSION, ("IMAGE_TAG",)),
    RuntimeEnvSpec(RuntimeEnvKey.GIT_COMMIT_SHA),
    RuntimeEnvSpec(RuntimeEnvKey.IMAGE_DIGEST),
    RuntimeEnvSpec(RuntimeEnvKey.CONFIGURATION_SHA256),
    RuntimeEnvSpec(RuntimeEnvKey.RELEASE_ID),
    RuntimeEnvSpec(RuntimeEnvKey.INSTANCE_ID),
    RuntimeEnvSpec(RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT),
    RuntimeEnvSpec(RuntimeEnvKey.OTEL_RESOURCE_ATTRIBUTES),
    RuntimeEnvSpec(RuntimeEnvKey.OTEL_SDK_DISABLED),
    RuntimeEnvSpec(RuntimeEnvKey.OTEL_METRIC_EXPORT_INTERVAL),
    RuntimeEnvSpec(RuntimeEnvKey.OTEL_TRACES_SAMPLER),
    RuntimeEnvSpec(RuntimeEnvKey.OTEL_TRACES_SAMPLER_ARG),
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


#: The resource attributes that name a service. A blank one is missing, like a blank variable.
_IDENTITY_ATTRIBUTES = ("service.name", "service.version", "service.instance.id")


@dataclass(frozen=True)
class _ServiceIdentity:
    """What one environment says about one service. ``_resolve_service_identity`` builds it.

    ``environment_is_set`` is true when ``ENVIRONMENT`` or one of its aliases is set. When it is
    false in non-strict mode, the tier is the ``local_dev`` default.
    """

    environment: RuntimeEnvironment
    environment_is_set: bool
    attributes: dict[str, str]
    deployment_environment: str | None
    service_name: str | None
    service_version: str
    instance_id: str


def _resolve_service_identity(
    environ: Mapping[str, str] | None,
    *,
    strict: bool,
    default_service_name: str | None = None,
) -> _ServiceIdentity:
    """Resolve the tier and the service coordinates of one environment.

    This function is the one reader of these facts. ``OtelSettings.from_env``,
    ``RuntimeIdentity.from_env`` and ``init_service`` all get them from here.

    - Tier: ``environment_from_env``, or ``strict_environment_from_env`` when ``strict`` is true.
    - Resource attributes: ``OTEL_RESOURCE_ATTRIBUTES``. A malformed value raises ``ValueError``
      when ``strict`` is true. Otherwise it gives a ``RuntimeWarning`` and no attributes.
      The values of ``service.name``, ``service.version`` and ``service.instance.id`` lose their
      blank padding. A blank one counts as missing, as a blank variable does, and the key is
      dropped. The next source of the chain applies.
    - Service name: ``OTEL_SERVICE_NAME``, then ``service.name`` in the attributes, then
      ``default_service_name``. The result is ``None`` when all three are missing.
    - Service version: ``SERVICE_VERSION``, then ``service.version``, then ``unknown``.
    - Instance id: ``INSTANCE_ID``, then ``service.instance.id``, then an empty string.
    """

    runtime = strict_environment_from_env(environ) if strict else environment_from_env(environ)
    environment_is_set = resolve_runtime_env(environ, RuntimeEnvKey.ENVIRONMENT).value is not None
    named = resolve_runtime_env(environ, RuntimeEnvKey.SERVICE_NAME).value
    attributes, deployment_environment = load_resource_attributes(
        resolve_runtime_env(environ, RuntimeEnvKey.OTEL_RESOURCE_ATTRIBUTES, default="").value
        or "",
        strict=strict,
        # Frames: 1 load_resource_attributes, 2 this function, 3 from_env, 4 the caller of
        # from_env. The warning names the caller of from_env.
        stacklevel=4,
    )
    for key in _IDENTITY_ATTRIBUTES:
        if key in attributes:
            if attributes[key].strip():
                attributes[key] = attributes[key].strip()
            else:
                del attributes[key]
    version = resolve_runtime_env(environ, RuntimeEnvKey.SERVICE_VERSION).value
    instance = resolve_runtime_env(environ, RuntimeEnvKey.INSTANCE_ID).value
    return _ServiceIdentity(
        environment=runtime,
        environment_is_set=environment_is_set,
        attributes=attributes,
        deployment_environment=deployment_environment,
        service_name=named or attributes.get("service.name") or default_service_name or None,
        service_version=version or attributes.get("service.version", "unknown"),
        instance_id=instance or attributes.get("service.instance.id", ""),
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
