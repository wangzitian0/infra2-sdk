import pytest

from infra2_sdk.runtime.environment import (
    APP_OWNED_TIERS,
    PLATFORM_OWNED_TIERS,
    EnvironmentTier,
    RuntimeEnvironment,
    environment_from_env,
    resolve_environment_tier,
    strict_environment_from_env,
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


def test_strict_environment_requires_explicit_configuration() -> None:
    with pytest.raises(ValueError, match="ENVIRONMENT is required"):
        strict_environment_from_env({})
    assert strict_environment_from_env({"ENVIRONMENT": "preview"}) == RuntimeEnvironment(
        "preview", EnvironmentTier.PREVIEW
    )


def test_normalize_deployment_environment() -> None:
    from infra2_sdk.runtime.environment import normalize_deployment_environment

    assert normalize_deployment_environment("", EnvironmentTier.STAGING) == "staging"
    assert (
        normalize_deployment_environment("branch-feature", EnvironmentTier.PREVIEW)
        == "branch-feature"
    )
    assert normalize_deployment_environment("staging", EnvironmentTier.STAGING) == "staging"
    with pytest.raises(ValueError, match="disagrees with environment tier"):
        normalize_deployment_environment("staging", EnvironmentTier.PRODUCTION)


def test_environment_from_env_detects_github_actions_from_supplied_mapping() -> None:
    runtime = environment_from_env({"ENVIRONMENT": "ci", "GITHUB_ACTIONS": "true"})
    assert runtime.tier is EnvironmentTier.GITHUB_CI


def test_process_environment_is_read_only_when_requested(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert environment_from_env().tier is EnvironmentTier.STAGING
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert environment_from_env().tier is EnvironmentTier.PRODUCTION


@pytest.mark.parametrize("name", ["pr-0", "pr-latest", "commit-bad", "tag-latest", "branch-"])
def test_malformed_deploy_v2_preview_aliases_fail_closed(name) -> None:
    with pytest.raises(ValueError, match="unknown environment"):
        environment_from_env({"ENVIRONMENT": name})


def test_to_environment_tier_conversions() -> None:
    from infra2_sdk.delivery import PipelineEnvironment
    from infra2_sdk.deploy import DeployType
    from infra2_sdk.runtime.environment import to_environment_tier

    # PipelineEnvironment mapping
    assert to_environment_tier(PipelineEnvironment.LOCAL) is EnvironmentTier.LOCAL_DEV
    assert to_environment_tier(PipelineEnvironment.PR) is EnvironmentTier.PREVIEW
    assert to_environment_tier(PipelineEnvironment.STAGING) is EnvironmentTier.STAGING
    assert to_environment_tier(PipelineEnvironment.PRODUCTION) is EnvironmentTier.PRODUCTION

    # DeployType mapping
    assert to_environment_tier(DeployType.STAGING) is EnvironmentTier.STAGING
    assert to_environment_tier(DeployType.PRODUCTION) is EnvironmentTier.PRODUCTION
    assert to_environment_tier(DeployType.CANARY) is EnvironmentTier.PREVIEW
    assert to_environment_tier(DeployType.PREVIEW_BRANCH) is EnvironmentTier.PREVIEW
    assert to_environment_tier(DeployType.PREVIEW_PR) is EnvironmentTier.PREVIEW
    assert to_environment_tier(DeployType.PREVIEW_COMMIT) is EnvironmentTier.PREVIEW
    assert to_environment_tier(DeployType.PREVIEW_TAG) is EnvironmentTier.PREVIEW

    # String mapping
    assert to_environment_tier("local") is EnvironmentTier.LOCAL_DEV
    assert to_environment_tier("pr") is EnvironmentTier.PREVIEW
    assert to_environment_tier("canary") is EnvironmentTier.PREVIEW
    assert to_environment_tier("preview/branch") is EnvironmentTier.PREVIEW
    assert to_environment_tier("staging") is EnvironmentTier.STAGING
    assert to_environment_tier("prod") is EnvironmentTier.PRODUCTION
    assert to_environment_tier("production") is EnvironmentTier.PRODUCTION
    assert to_environment_tier(EnvironmentTier.STAGING) is EnvironmentTier.STAGING

    # Errors
    with pytest.raises(TypeError, match="cannot convert"):
        to_environment_tier(123)
    with pytest.raises(ValueError, match="unknown environment"):
        to_environment_tier("unknown_val")


def test_to_pipeline_environment_conversions() -> None:
    from infra2_sdk.delivery import PipelineEnvironment
    from infra2_sdk.runtime.environment import to_pipeline_environment

    assert to_pipeline_environment(EnvironmentTier.LOCAL_DEV) is PipelineEnvironment.LOCAL
    assert to_pipeline_environment(EnvironmentTier.LOCAL_TEST) is PipelineEnvironment.LOCAL
    assert to_pipeline_environment(EnvironmentTier.GITHUB_CI) is PipelineEnvironment.PR
    assert to_pipeline_environment(EnvironmentTier.PREVIEW) is PipelineEnvironment.PR
    assert to_pipeline_environment(EnvironmentTier.STAGING) is PipelineEnvironment.STAGING
    assert to_pipeline_environment(EnvironmentTier.PRODUCTION) is PipelineEnvironment.PRODUCTION
    assert to_pipeline_environment(PipelineEnvironment.STAGING) is PipelineEnvironment.STAGING


def test_to_deploy_type_conversions() -> None:
    from infra2_sdk.deploy import DeployType
    from infra2_sdk.runtime.environment import to_deploy_type

    assert to_deploy_type(EnvironmentTier.STAGING) is DeployType.STAGING
    assert to_deploy_type(EnvironmentTier.PRODUCTION) is DeployType.PRODUCTION
    assert to_deploy_type(EnvironmentTier.PREVIEW) is DeployType.PREVIEW_BRANCH
    assert to_deploy_type(EnvironmentTier.PREVIEW, preview_variant="pr") is DeployType.PREVIEW_PR
    assert (
        to_deploy_type(EnvironmentTier.PREVIEW, preview_variant="commit")
        is DeployType.PREVIEW_COMMIT
    )
    assert to_deploy_type(EnvironmentTier.PREVIEW, preview_variant="tag") is DeployType.PREVIEW_TAG
    assert to_deploy_type(DeployType.STAGING) is DeployType.STAGING
    assert to_deploy_type("preview/commit") is DeployType.PREVIEW_COMMIT
    assert to_deploy_type("canary") is DeployType.CANARY
    assert to_deploy_type("prod") is DeployType.PRODUCTION
    assert to_deploy_type("staging") is DeployType.STAGING

    with pytest.raises(ValueError, match="cannot map non-deployable tier"):
        to_deploy_type(EnvironmentTier.LOCAL_DEV)
    with pytest.raises(ValueError, match="cannot map non-deployable tier"):
        to_deploy_type(EnvironmentTier.GITHUB_CI)
