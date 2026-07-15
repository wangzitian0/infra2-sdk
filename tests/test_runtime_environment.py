import pytest

from infra2_sdk.runtime.environment import (
    APP_OWNED_TIERS,
    PLATFORM_OWNED_TIERS,
    EnvironmentTier,
    resolve_environment_tier,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("development", EnvironmentTier.LOCAL_DEV),
        ("local-ci", EnvironmentTier.LOCAL_TEST),
        ("local_test", EnvironmentTier.LOCAL_TEST),
        ("preview", EnvironmentTier.PREVIEW),
        ("staging", EnvironmentTier.STAGING),
        ("prod", EnvironmentTier.PRODUCTION),
        (EnvironmentTier.PRODUCTION, EnvironmentTier.PRODUCTION),
    ],
)
def test_environment_aliases(value, expected) -> None:
    assert resolve_environment_tier(value) is expected


def test_ci_and_unknown_policies_are_explicit() -> None:
    assert resolve_environment_tier("ci", github_actions=True) is EnvironmentTier.GITHUB_CI
    assert resolve_environment_tier("future", unknown="production") is EnvironmentTier.PRODUCTION
    with pytest.raises(ValueError, match="unknown environment"):
        resolve_environment_tier("future")
    with pytest.raises(TypeError, match="environment"):
        resolve_environment_tier(1)  # type: ignore[arg-type]


def test_ownership_sets_cover_every_tier_once() -> None:
    assert APP_OWNED_TIERS.isdisjoint(PLATFORM_OWNED_TIERS)
    assert frozenset(EnvironmentTier) == APP_OWNED_TIERS | PLATFORM_OWNED_TIERS
