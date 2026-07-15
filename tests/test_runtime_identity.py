import pytest

from infra2_sdk.runtime.environment import EnvironmentTier
from infra2_sdk.runtime.identity import RuntimeIdentity, configuration_fingerprint

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
CONFIG = "c" * 64


def identity(**changes) -> RuntimeIdentity:
    values = {
        "service_name": "api",
        "service_version": "1.2.3",
        "environment": EnvironmentTier.STAGING,
        "commit_sha": SHA,
        "image_digest": DIGEST,
        "configuration_sha256": CONFIG,
        "release_id": "release-1",
        "instance_id": "pod-1",
        "provenance_uri": "https://example.test/provenance",
        "sbom_uri": "oci://registry.example.test/api@" + DIGEST,
    }
    values.update(changes)
    return RuntimeIdentity(**values)


def test_runtime_identity_round_trip_and_otel_attributes() -> None:
    original = identity()
    original.validate_protected()
    assert RuntimeIdentity.from_dict(original.to_dict()) == original
    attributes = original.to_otel_resource_attributes()
    assert attributes["service.name"] == "api"
    assert attributes["container.image.id"] == DIGEST
    assert attributes["infra2.sbom.uri"].startswith("oci://")
    assert identity(environment="staging").environment is EnvironmentTier.STAGING
    assert original.json_schema()["properties"]["image_digest"]["pattern"].startswith("^")


def test_protected_identity_requires_immutable_coordinates() -> None:
    with pytest.raises(ValueError, match="image_digest"):
        identity(image_digest="").validate_protected()
    local = identity(environment=EnvironmentTier.LOCAL_DEV, commit_sha="unknown", image_digest="")
    local.validate_protected()


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"commit_sha": "short"}, "commit_sha"),
        ({"image_digest": "sha256:bad"}, "image_digest"),
        ({"configuration_sha256": "bad"}, "configuration_sha256"),
        ({"provenance_uri": "file:///tmp/provenance"}, "provenance_uri"),
    ],
)
def test_identity_rejects_invalid_coordinates(changes, message) -> None:
    with pytest.raises(ValueError, match=message):
        identity(**changes)


def test_configuration_fingerprint_is_order_independent_and_framed() -> None:
    first = configuration_fingerprint({"a": "bc", "ab": b"c"})
    second = configuration_fingerprint({"ab": b"c", "a": "bc"})
    assert first == second
    assert first != configuration_fingerprint({"a": "b", "cab": "c"})
    with pytest.raises(TypeError, match="str or bytes"):
        configuration_fingerprint({"bad": 1})  # type: ignore[dict-item]
