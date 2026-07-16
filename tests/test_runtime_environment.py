import pytest

from infra2_sdk.runtime.environment import (
    APP_OWNED_TIERS,
    PLATFORM_OWNED_TIERS,
    EnvironmentTier,
    RuntimeEnvironment,
    environment_from_env,
    resolve_environment_tier,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("development", EnvironmentTier.LOCAL_DEV),
        ("local-ci", EnvironmentTier.LOCAL_TEST),
        ("local_test", EnvironmentTier.LOCAL_TEST),
        ("github_ci", EnvironmentTier.GITHUB_CI),
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


@pytest.mark.parametrize(
    "name",
    ["branch-main", "branch-feature_x", "pr-42", "commit-1ab32d5", "tag-v1-2-3"],
)
def test_deploy_v2_preview_alias_preserves_name_and_resolves_tier(name) -> None:
    runtime = environment_from_env({"ENVIRONMENT": name})
    assert runtime == RuntimeEnvironment(name=name, tier=EnvironmentTier.PREVIEW)


def test_environment_from_env_is_transparent_and_conflict_safe() -> None:
    assert environment_from_env({}) == RuntimeEnvironment("local_dev", EnvironmentTier.LOCAL_DEV)
    assert environment_from_env({"ENV": "staging"}).tier is EnvironmentTier.STAGING
    assert environment_from_env({"APP_ENV": "prod"}).tier is EnvironmentTier.PRODUCTION
    with pytest.raises(ValueError, match="conflicting environment variables"):
        environment_from_env({"ENVIRONMENT": "staging", "ENV": "production"})


def test_process_environment_is_read_only_when_requested(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert environment_from_env().tier is EnvironmentTier.STAGING
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert environment_from_env().tier is EnvironmentTier.PRODUCTION


@pytest.mark.parametrize("name", ["pr-0", "pr-latest", "commit-bad", "tag-latest", "branch-"])
def test_malformed_deploy_v2_preview_aliases_fail_closed(name) -> None:
    with pytest.raises(ValueError, match="unknown environment"):
        environment_from_env({"ENVIRONMENT": name})
