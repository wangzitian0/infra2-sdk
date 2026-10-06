from typing import get_type_hints

import pytest

from infra2_sdk.runtime.environment import (
    APP_OWNED_TIERS,
    PLATFORM_OWNED_TIERS,
    EnvironmentConflictError,
    EnvironmentTier,
    RuntimeEnvironment,
    RuntimeEnvKey,
    env_bool,
    env_float,
    env_int,
    environment_from_env,
    resolve_env,
    resolve_environment_tier,
    runtime_env_contract,
    strict_environment_from_env,
    to_environment_tier,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("development", EnvironmentTier.LOCAL_DEV),
        ("dev", EnvironmentTier.LOCAL_DEV),
        ("local", EnvironmentTier.LOCAL_DEV),
        ("local-ci", EnvironmentTier.LOCAL_TEST),
        ("local_test", EnvironmentTier.LOCAL_TEST),
        ("github_ci", EnvironmentTier.GITHUB_CI),
        ("preview", EnvironmentTier.PREVIEW),
        ("pr", EnvironmentTier.PREVIEW),
        ("canary", EnvironmentTier.PREVIEW),
        ("preview/branch", EnvironmentTier.PREVIEW),
        ("staging", EnvironmentTier.STAGING),
        ("prod", EnvironmentTier.PRODUCTION),
        ("production", EnvironmentTier.PRODUCTION),
        (EnvironmentTier.PRODUCTION, EnvironmentTier.PRODUCTION),
    ],
)
def test_environment_aliases(value, expected) -> None:
    assert to_environment_tier(value) is expected
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
    from infra2_sdk.deploy import DeployType
    from infra2_sdk.runtime.environment import to_environment_tier

    # DeployType mapping
    assert to_environment_tier(DeployType.STAGING) is EnvironmentTier.STAGING
    assert to_environment_tier(DeployType.PRODUCTION) is EnvironmentTier.PRODUCTION
    assert to_environment_tier(DeployType.CANARY) is EnvironmentTier.PREVIEW
    assert to_environment_tier(DeployType.PREVIEW_BRANCH) is EnvironmentTier.PREVIEW
    assert to_environment_tier(DeployType.PREVIEW_PR) is EnvironmentTier.PREVIEW
    assert to_environment_tier(DeployType.PREVIEW_COMMIT) is EnvironmentTier.PREVIEW
    assert to_environment_tier(DeployType.PREVIEW_TAG) is EnvironmentTier.PREVIEW

    # Errors
    with pytest.raises(TypeError, match="cannot convert"):
        to_environment_tier(123)
    with pytest.raises(ValueError, match="unknown environment"):
        to_environment_tier("unknown_val")


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


def test_the_canary_slot_is_a_preview_tier_and_routing_shares_the_definition() -> None:
    """#54: the SDK names the canary slot (``routing.CANARY_SLOT``) and infra2 issues it as that
    slot's ``deployment.environment.name``; resolving it must give the preview tier, not
    ``unknown environment`` — and routing must re-export the one definition, not a copy."""
    from infra2_sdk import routing
    from infra2_sdk.runtime import environment

    assert routing.CANARY_SLOT is environment.CANARY_SLOT == "canary-preview"
    assert to_environment_tier(environment.CANARY_SLOT) is EnvironmentTier.PREVIEW
    assert to_environment_tier(environment.CANARY_SLOT.upper()) is EnvironmentTier.PREVIEW
    normalize = environment.normalize_deployment_environment
    assert normalize(environment.CANARY_SLOT, EnvironmentTier.PREVIEW) == "canary-preview"
    with pytest.raises(ValueError, match="disagrees"):
        normalize(environment.CANARY_SLOT, EnvironmentTier.STAGING)


# --- Runtime Environment Variable Registry & Resolution ----------------------


def test_canonical_alias_and_default_resolution_are_explicit() -> None:
    assert resolve_env({"ENVIRONMENT": "staging"}, RuntimeEnvKey.ENVIRONMENT).value == "staging"
    alias = resolve_env(
        {"ENV": "pr-42"},
        RuntimeEnvKey.ENVIRONMENT,
        aliases=("ENV", "APP_ENV"),
    )
    assert (alias.value, alias.source) == ("pr-42", "ENV")
    default = resolve_env({}, RuntimeEnvKey.ENVIRONMENT, default="local_dev")
    assert (default.value, default.source) == ("local_dev", None)


def test_equal_aliases_are_allowed_but_conflicts_fail_closed() -> None:
    resolved = resolve_env(
        {"ENVIRONMENT": "staging", "ENV": "staging"},
        RuntimeEnvKey.ENVIRONMENT,
        aliases=("ENV",),
    )
    assert resolved.source == "ENVIRONMENT"

    with pytest.raises(EnvironmentConflictError, match="ENVIRONMENT.*ENV"):
        resolve_env(
            {"ENVIRONMENT": "staging", "ENV": "production"},
            RuntimeEnvKey.ENVIRONMENT,
            aliases=("ENV",),
        )


def test_required_and_secret_errors_never_expose_values() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        resolve_env({}, RuntimeEnvKey.DATABASE_URL, required=True)

    secret = "do-not-leak-this-secret"
    with pytest.raises(EnvironmentConflictError) as captured:
        resolve_env(
            {"AWS_SECRET_ACCESS_KEY": secret, "S3_SECRET_KEY": "different-secret"},
            RuntimeEnvKey.AWS_SECRET_ACCESS_KEY,
            aliases=("S3_SECRET_KEY",),
            sensitive=True,
        )
    assert secret not in str(captured.value)

    spaced = "  value with intentional spaces  "
    resolved = resolve_env(
        {"AWS_SECRET_ACCESS_KEY": spaced},
        RuntimeEnvKey.AWS_SECRET_ACCESS_KEY,
        sensitive=True,
    )
    assert resolved.value == spaced


def test_runtime_env_registry_is_versioned_unique_and_platform_neutral() -> None:
    contract = runtime_env_contract()
    assert contract["contract_version"] == 1
    variables = contract["variables"]
    names = [item["name"] for item in variables]
    aliases = [alias for item in variables for alias in item["aliases"]]
    assert len(names) == len(set(names))
    assert not set(names) & set(aliases)
    assert len(aliases) == len(set(aliases))
    serialized = str(contract).upper()
    for forbidden in ("INFRA2", "IAC_REF", "VAULT", "DOKPLOY"):
        assert forbidden not in serialized


def test_typed_env_helpers_accept_registry_keys_in_the_public_contract() -> None:
    for helper in (env_int, env_float, env_bool):
        assert get_type_hints(helper)["key"] == str | RuntimeEnvKey
