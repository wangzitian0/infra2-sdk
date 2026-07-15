import pytest

from infra2_sdk.runtime.dependencies import Dependency, DependencyKind, DependencyManifest
from infra2_sdk.runtime.environment import EnvironmentTier


def dependency(name: str = "database") -> Dependency:
    return Dependency(
        name=name,
        kind=DependencyKind.CODE_DOMINANT,
        required_in=frozenset({EnvironmentTier.STAGING, EnvironmentTier.PRODUCTION}),
        env_vars=frozenset({"DATABASE_URL"}),
        summary="Postgres",
        local_backend="postgres",
        deployed_backend="postgres",
    )


def test_dependency_manifest_round_trip_and_tier_lookup() -> None:
    original = DependencyManifest((dependency(),))
    restored = DependencyManifest.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()
    assert restored.names() == frozenset({"database"})
    assert restored.get("database").summary == "Postgres"
    assert restored.required_for("staging") == frozenset({"database"})
    assert restored.required_for("preview") == frozenset()
    assert len(restored) == 1
    schema = DependencyManifest.json_schema()
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["properties"]["contract_version"] == {"const": 1}
    normalized = Dependency(
        "cache",
        "code_dominant",
        frozenset({"staging"}),
        frozenset({"REDIS_URL"}),
    )
    assert normalized.kind is DependencyKind.CODE_DOMINANT
    assert normalized.required_in == frozenset({EnvironmentTier.STAGING})


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"name": "bad/name"}, "identifier"),
        ({"required_in": frozenset()}, "at least one"),
        ({"env_vars": frozenset()}, "environment variables"),
        ({"env_vars": frozenset({"lower"})}, "uppercase"),
    ],
)
def test_dependency_validation(changes, message) -> None:
    values = dependency().__dict__ | changes
    with pytest.raises(ValueError, match=message):
        Dependency(**values)


def test_manifest_rejects_duplicates_and_bad_wire_values() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        DependencyManifest((dependency(), dependency()))
    with pytest.raises(ValueError, match="array"):
        DependencyManifest.from_dict({"contract_version": 1, "dependencies": "bad"})
    with pytest.raises(ValueError, match="object"):
        DependencyManifest.from_dict({"contract_version": 1, "dependencies": ["bad"]})
    with pytest.raises(ValueError, match="required_in"):
        Dependency.from_dict({"name": "db", "kind": "code_dominant"})
    raw = dependency().to_dict()
    raw["env_vars"] = [1]
    with pytest.raises(ValueError, match="contain strings"):
        Dependency.from_dict(raw)
    with pytest.raises(ValueError, match="unsupported"):
        DependencyManifest((), contract_version=2)
