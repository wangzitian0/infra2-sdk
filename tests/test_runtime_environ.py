import pytest

from infra2_sdk.runtime.environ import (
    EnvironmentConflictError,
    RuntimeEnvKey,
    resolve_env,
    runtime_env_contract,
)


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
