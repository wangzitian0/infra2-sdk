"""Canonical environment vocabulary shared by applications and infra2."""

from __future__ import annotations

from enum import StrEnum


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
    if normalized in _LOCAL_DEV_ALIASES:
        return EnvironmentTier.LOCAL_DEV
    if normalized in _LOCAL_TEST_ALIASES:
        return EnvironmentTier.GITHUB_CI if github_actions else EnvironmentTier.LOCAL_TEST
    if normalized == EnvironmentTier.GITHUB_CI:
        return EnvironmentTier.GITHUB_CI
    if normalized == EnvironmentTier.PREVIEW:
        return EnvironmentTier.PREVIEW
    if normalized == EnvironmentTier.STAGING:
        return EnvironmentTier.STAGING
    if normalized in _PRODUCTION_ALIASES:
        return EnvironmentTier.PRODUCTION
    policy = UnknownEnvironmentPolicy(unknown)
    if policy is UnknownEnvironmentPolicy.PRODUCTION:
        return EnvironmentTier.PRODUCTION
    raise ValueError(f"unknown environment: {value!r}")
