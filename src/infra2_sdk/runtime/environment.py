"""Canonical environment vocabulary shared by applications and infra2."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from infra2_sdk.runtime.environ import RuntimeEnvKey, resolve_runtime_env


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


def resolve_environment_tier(
    value: str | EnvironmentTier,
    *,
    github_actions: bool = False,
    unknown: str | UnknownEnvironmentPolicy = UnknownEnvironmentPolicy.ERROR,
) -> EnvironmentTier:
    """Normalize common aliases while making fail-closed behavior explicit.

    ``local_ci`` remains accepted as a migration alias for Finance Report, while
    ``local_test`` is the canonical wire value. Consumers that historically map
    unknown names to Production can opt into ``unknown="production"``.
    """

    if isinstance(value, EnvironmentTier):
        return value
    if not isinstance(value, str):
        raise TypeError("environment must be a string or EnvironmentTier")
    normalized = value.strip().lower().replace("-", "_")
    display = value.strip().lower()
    if normalized in _LOCAL_DEV_ALIASES:
        return EnvironmentTier.LOCAL_DEV
    if normalized in _LOCAL_TEST_ALIASES:
        return EnvironmentTier.GITHUB_CI if github_actions else EnvironmentTier.LOCAL_TEST
    if normalized == EnvironmentTier.GITHUB_CI.value:
        return EnvironmentTier.GITHUB_CI
    if normalized == EnvironmentTier.PREVIEW.value:
        return EnvironmentTier.PREVIEW
    if _PREVIEW_ALIAS_RE.match(display):
        return EnvironmentTier.PREVIEW
    if normalized == EnvironmentTier.STAGING.value:
        return EnvironmentTier.STAGING
    if normalized in _PRODUCTION_ALIASES:
        return EnvironmentTier.PRODUCTION
    policy = UnknownEnvironmentPolicy(unknown)
    if policy is UnknownEnvironmentPolicy.PRODUCTION:
        return EnvironmentTier.PRODUCTION
    raise ValueError(f"unknown environment: {value!r}")


def environment_from_env(environ: Mapping[str, str] | None = None) -> RuntimeEnvironment:
    """Resolve deploy_v2 and standalone environments without knowing their producer."""

    resolved = resolve_runtime_env(
        environ,
        RuntimeEnvKey.ENVIRONMENT,
        default=EnvironmentTier.LOCAL_DEV.value,
    )
    assert resolved.value is not None
    tier = resolve_environment_tier(resolved.value)
    raw_name = resolved.value.strip().lower()
    name = raw_name if tier is EnvironmentTier.PREVIEW and raw_name != "preview" else tier.value
    return RuntimeEnvironment(name=name, tier=tier)
