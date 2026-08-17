from dataclasses import replace
from pathlib import Path

import pytest

from infra2_sdk.snapshot import (
    AnonymizedSnapshotManifest,
    ResidualScanProof,
    ResidualScanStatus,
    SnapshotArtifact,
    SnapshotArtifactFormat,
    SnapshotProducer,
    verify_snapshot_artifact,
)

APP_SHA = "a" * 40
ARTIFACT_SHA = "b" * 64


def manifest(**overrides) -> AnonymizedSnapshotManifest:
    values = {
        "snapshot_id": "snapshot-20260817-123456",
        "source_environment": "production",
        "source_schema_revision": "20260817_1234_add_snapshot_lane",
        "anonymizer_sha": APP_SHA,
        "generated_at": "2026-08-17T12:34:56Z",
        "producer": SnapshotProducer(
            repository="wangzitian0/finance_report",
            source_sha=APP_SHA,
            run_id="32036861255",
            run_url=("https://github.com/wangzitian0/finance_report/actions/runs/32036861255"),
        ),
        "residual_scan": ResidualScanProof(
            status=ResidualScanStatus.PASSED,
            classified_columns=441,
            tables_scanned=52,
            residuals_found=0,
        ),
        "artifact": SnapshotArtifact(
            format=SnapshotArtifactFormat.POSTGRESQL_CUSTOM,
            sha256=ARTIFACT_SHA,
            size_bytes=4096,
        ),
    }
    values.update(overrides)
    return AnonymizedSnapshotManifest(**values)


def test_manifest_round_trips_through_the_exact_v1_wire_shape() -> None:
    original = manifest()
    raw = original.to_dict()

    assert AnonymizedSnapshotManifest.from_dict(raw) == original
    assert set(raw) == {
        "contract_version",
        "snapshot_id",
        "source_environment",
        "source_schema_revision",
        "anonymizer_sha",
        "generated_at",
        "producer",
        "residual_scan",
        "artifact",
    }


@pytest.mark.parametrize("field", ["snapshot_id", "artifact", "residual_scan"])
def test_manifest_rejects_missing_or_extra_fields(field: str) -> None:
    raw = manifest().to_dict()
    del raw[field]
    with pytest.raises(ValueError, match="manifest fields must exactly match"):
        AnonymizedSnapshotManifest.from_dict(raw)

    raw = manifest().to_dict()
    raw["unexpected"] = "value"
    with pytest.raises(ValueError, match="manifest fields must exactly match"):
        AnonymizedSnapshotManifest.from_dict(raw)


def test_manifest_rejects_unsupported_or_boolean_contract_versions() -> None:
    raw = manifest().to_dict()
    raw["contract_version"] = 2
    with pytest.raises(ValueError, match="unsupported snapshot manifest version"):
        AnonymizedSnapshotManifest.from_dict(raw)

    raw["contract_version"] = True
    with pytest.raises(ValueError, match="integer"):
        AnonymizedSnapshotManifest.from_dict(raw)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"source_environment": "staging"}, "source_environment"),
        ({"source_schema_revision": ""}, "source_schema_revision"),
        ({"anonymizer_sha": "v1.2.3"}, "anonymizer_sha"),
        ({"generated_at": "2026-08-17T12:34:56+08:00"}, "UTC RFC3339"),
        ({"snapshot_id": "short"}, "snapshot_id"),
    ],
)
def test_manifest_identity_and_time_are_fail_closed(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        manifest(**changes)


def test_producer_run_is_immutable_and_self_consistent() -> None:
    with pytest.raises(ValueError, match="source_sha"):
        replace(manifest().producer, source_sha="main")
    with pytest.raises(ValueError, match="run_id"):
        replace(manifest().producer, run_id="latest")
    with pytest.raises(ValueError, match="must identify producer run"):
        replace(
            manifest().producer,
            run_url="https://github.com/wangzitian0/infra2/actions/runs/32036861255",
        )


def test_nested_wire_objects_are_closed_and_typed() -> None:
    raw = manifest().to_dict()
    raw["producer"]["extra"] = True
    with pytest.raises(ValueError, match="producer fields must exactly match"):
        AnonymizedSnapshotManifest.from_dict(raw)

    raw = manifest().to_dict()
    raw["residual_scan"] = "passed"
    with pytest.raises(ValueError, match="residual_scan must be an object"):
        AnonymizedSnapshotManifest.from_dict(raw)

    with pytest.raises(TypeError, match="producer must be"):
        manifest(producer={})
    with pytest.raises(TypeError, match="residual_scan must be"):
        manifest(residual_scan={})
    with pytest.raises(TypeError, match="artifact must be"):
        manifest(artifact={})


def test_nested_deserialization_fails_closed_on_bad_values_and_types() -> None:
    raw = manifest().to_dict()
    raw["producer"]["repository"] = "not-a-repository"
    with pytest.raises(ValueError, match="owner/repository"):
        AnonymizedSnapshotManifest.from_dict(raw)

    raw = manifest().to_dict()
    raw["residual_scan"]["status"] = "failed"
    with pytest.raises(ValueError, match="status must be passed"):
        AnonymizedSnapshotManifest.from_dict(raw)

    raw = manifest().to_dict()
    raw["residual_scan"]["classified_columns"] = True
    with pytest.raises(ValueError, match="classified_columns must be an integer"):
        AnonymizedSnapshotManifest.from_dict(raw)

    raw = manifest().to_dict()
    raw["artifact"]["format"] = "sql"
    with pytest.raises(ValueError, match="unsupported snapshot artifact format"):
        AnonymizedSnapshotManifest.from_dict(raw)

    raw = manifest().to_dict()
    raw["artifact"]["sha256"] = 123
    with pytest.raises(ValueError, match="sha256 must be a string"):
        AnonymizedSnapshotManifest.from_dict(raw)

    with pytest.raises(ValueError, match="manifest must be an object"):
        AnonymizedSnapshotManifest.from_dict("not-an-object")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"status": "failed"}, "ResidualScanStatus"),
        ({"classified_columns": 0}, "classified_columns"),
        ({"tables_scanned": 0}, "tables_scanned"),
        ({"residuals_found": 1}, "residuals_found must be zero"),
    ],
)
def test_residual_proof_can_represent_only_a_completed_clean_scan(
    changes: dict[str, object], message: str
) -> None:
    values = {
        "status": ResidualScanStatus.PASSED,
        "classified_columns": 441,
        "tables_scanned": 52,
        "residuals_found": 0,
    }
    values.update(changes)
    with pytest.raises((TypeError, ValueError), match=message):
        ResidualScanProof(**values)


def test_direct_residual_status_requires_the_enum_type() -> None:
    with pytest.raises(TypeError, match="ResidualScanStatus"):
        ResidualScanProof(
            status="passed",
            classified_columns=441,
            tables_scanned=52,
            residuals_found=0,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"format": "sql"}, "artifact format"),
        ({"sha256": "ABC"}, "sha256"),
        ({"size_bytes": 0}, "size_bytes"),
        ({"size_bytes": True}, "size_bytes"),
    ],
)
def test_artifact_identity_is_fail_closed(changes: dict[str, object], message: str) -> None:
    values = {
        "format": SnapshotArtifactFormat.POSTGRESQL_CUSTOM,
        "sha256": ARTIFACT_SHA,
        "size_bytes": 4096,
    }
    values.update(changes)
    with pytest.raises((TypeError, ValueError), match=message):
        SnapshotArtifact(**values)


def test_direct_artifact_format_requires_the_enum_type() -> None:
    with pytest.raises(TypeError, match="SnapshotArtifactFormat"):
        SnapshotArtifact(
            format="postgresql-custom",
            sha256=ARTIFACT_SHA,
            size_bytes=4096,
        )


def test_artifact_verification_binds_manifest_to_exact_bytes(tmp_path: Path) -> None:
    artifact = tmp_path / "snapshot.dump"
    artifact.write_bytes(b"verified snapshot bytes")
    import hashlib

    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    expected = replace(
        manifest(),
        artifact=SnapshotArtifact(
            format=SnapshotArtifactFormat.POSTGRESQL_CUSTOM,
            sha256=digest,
            size_bytes=artifact.stat().st_size,
        ),
    )

    verify_snapshot_artifact(expected, artifact)
    artifact.write_bytes(b"tampered snapshot byte!")
    with pytest.raises(ValueError, match="snapshot artifact sha256 mismatch"):
        verify_snapshot_artifact(expected, artifact)

    artifact.write_bytes(b"short")
    with pytest.raises(ValueError, match="snapshot artifact size mismatch"):
        verify_snapshot_artifact(expected, artifact)

    with pytest.raises(ValueError, match="snapshot artifact is unavailable"):
        verify_snapshot_artifact(expected, tmp_path / "missing.dump")


def test_artifact_verification_converts_read_failures_to_closed_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "snapshot.dump"
    artifact.write_bytes(b"verified snapshot bytes")
    import hashlib

    expected = replace(
        manifest(),
        artifact=SnapshotArtifact(
            format=SnapshotArtifactFormat.POSTGRESQL_CUSTOM,
            sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
            size_bytes=artifact.stat().st_size,
        ),
    )

    def deny_read(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", deny_read)
    with pytest.raises(ValueError, match="snapshot artifact is unavailable"):
        verify_snapshot_artifact(expected, artifact)


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-17Z",
        "2026-08-17T12:34:56",
        "2026-13-17T12:34:56Z",
        "not-a-time",
        123,
    ],
)
def test_generated_at_requires_a_real_utc_rfc3339_timestamp(value: object) -> None:
    with pytest.raises(ValueError, match="UTC RFC3339"):
        manifest(generated_at=value)


def test_json_schema_is_closed_and_requires_clean_proof() -> None:
    schema = AnonymizedSnapshotManifest.json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["contract_version"] == {"const": 1}
    proof = schema["properties"]["residual_scan"]
    assert proof["additionalProperties"] is False
    assert proof["properties"]["status"] == {"const": "passed"}
    assert proof["properties"]["residuals_found"] == {"const": 0}


def test_shape_validation_is_not_presented_as_producer_attestation() -> None:
    """The pure SDK contract must keep the trust decision with infra2."""
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "does not attest" in readme
    assert "independently authorize" in readme
