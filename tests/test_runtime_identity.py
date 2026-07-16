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


def test_identity_loads_deploy_v2_derived_results_without_iac_coordinates() -> None:
    original = RuntimeIdentity.from_env(
        {
            "ENVIRONMENT": "pr-42",
            "OTEL_SERVICE_NAME": "finance-api",
            "SERVICE_VERSION": "a1b2c3d",
            "GIT_COMMIT_SHA": SHA,
            "IMAGE_DIGEST": DIGEST,
            "CONFIGURATION_SHA256": CONFIG,
            "RELEASE_ID": "preview/pr/42",
        }
    )
    assert original.environment is EnvironmentTier.PREVIEW
    assert original.deployment_environment == "pr-42"
    assert original.commit_sha == SHA
    standard = original.to_standard_otel_resource_attributes()
    assert standard["deployment.environment.name"] == "pr-42"
    assert all(not key.startswith("infra2.") for key in standard)


def test_identity_rejects_display_environment_that_disagrees_with_tier() -> None:
    with pytest.raises(ValueError, match="deployment_environment"):
        identity(deployment_environment="pr-42")


def test_identity_accepts_provider_neutral_preview_display_name() -> None:
    preview = identity(
        environment=EnvironmentTier.PREVIEW,
        deployment_environment="review-slot-202",
    )
    assert preview.deployment_environment == "review-slot-202"


def test_identity_reads_standard_resource_identity_and_supports_strict_mode() -> None:
    preview = RuntimeIdentity.from_env(
        {
            "ENVIRONMENT": "preview",
            "OTEL_SERVICE_NAME": "api",
            "GIT_COMMIT_SHA": SHA,
            "OTEL_RESOURCE_ATTRIBUTES": (
                "service.version=1.2.3,deployment.environment.name=review-slot-202"
            ),
        },
        strict=True,
    )
    assert preview.service_version == "1.2.3"
    assert preview.deployment_environment == "review-slot-202"
    with pytest.raises(ValueError, match="ENVIRONMENT is required"):
        RuntimeIdentity.from_env({"OTEL_SERVICE_NAME": "api"}, strict=True)
    with pytest.raises(ValueError, match="commit_sha"):
        RuntimeIdentity.from_env(
            {"ENVIRONMENT": "preview", "OTEL_SERVICE_NAME": "api"},
            strict=True,
        )


def test_deploy_v2_must_inject_resolved_sha_not_image_ref() -> None:
    with pytest.raises(ValueError, match="commit_sha"):
        RuntimeIdentity.from_env(
            {
                "ENVIRONMENT": "staging",
                "OTEL_SERVICE_NAME": "api",
                "SERVICE_VERSION": "v1.2.3",
                "GIT_COMMIT_SHA": "v1.2.3",
            }
        )


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
