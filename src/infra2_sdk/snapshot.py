"""Versioned wire contract for an anonymized production snapshot.

This module validates manifest shape and binds a manifest to exact artifact
bytes. It deliberately does not authorize the producer or perform snapshot,
storage, host, Vault, or deployment operations. A consumer such as infra2
must independently authorize the declared producer run before side effects.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from infra2_sdk._wire import parse_contract_version, require_contract_version

SNAPSHOT_MANIFEST_VERSION = 1

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
_SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_SCHEMA_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_UTC_RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)


class ResidualScanStatus(StrEnum):
    PASSED = "passed"


class SnapshotArtifactFormat(StrEnum):
    POSTGRESQL_CUSTOM = "postgresql-custom"


@dataclass(frozen=True, kw_only=True)
class SnapshotProducer:
    repository: str
    source_sha: str
    run_id: str
    run_url: str

    def __post_init__(self) -> None:
        if not _REPOSITORY_RE.fullmatch(self.repository):
            raise ValueError("producer repository must have the form owner/repository")
        if not _SHA40_RE.fullmatch(self.source_sha):
            raise ValueError("producer source_sha must be a lowercase 40-hex commit sha")
        if not _RUN_ID_RE.fullmatch(self.run_id):
            raise ValueError("producer run_id must be a positive decimal GitHub Actions run id")
        expected_url = f"https://github.com/{self.repository}/actions/runs/{self.run_id}"
        if self.run_url != expected_url:
            raise ValueError(
                "producer run_url must identify producer run_id in producer repository"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "source_sha": self.source_sha,
            "run_id": self.run_id,
            "run_url": self.run_url,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SnapshotProducer:
        _require_exact_fields(
            raw,
            {"repository", "source_sha", "run_id", "run_url"},
            description="producer",
        )
        return cls(
            repository=_string(raw, "repository"),
            source_sha=_string(raw, "source_sha"),
            run_id=_string(raw, "run_id"),
            run_url=_string(raw, "run_url"),
        )


@dataclass(frozen=True, kw_only=True)
class ResidualScanProof:
    status: ResidualScanStatus
    classified_columns: int
    tables_scanned: int
    residuals_found: int

    def __post_init__(self) -> None:
        if not isinstance(self.status, ResidualScanStatus):
            raise TypeError("residual scan status must be a ResidualScanStatus")
        _require_positive_int(self.classified_columns, description="classified_columns")
        _require_positive_int(self.tables_scanned, description="tables_scanned")
        if type(self.residuals_found) is not int or self.residuals_found != 0:
            raise ValueError("residuals_found must be zero")

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "classified_columns": self.classified_columns,
            "tables_scanned": self.tables_scanned,
            "residuals_found": self.residuals_found,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ResidualScanProof:
        _require_exact_fields(
            raw,
            {"status", "classified_columns", "tables_scanned", "residuals_found"},
            description="residual_scan",
        )
        try:
            status = ResidualScanStatus(_string(raw, "status"))
        except ValueError as exc:
            raise ValueError("residual scan status must be passed") from exc
        return cls(
            status=status,
            classified_columns=_integer(raw, "classified_columns"),
            tables_scanned=_integer(raw, "tables_scanned"),
            residuals_found=_integer(raw, "residuals_found"),
        )


@dataclass(frozen=True, kw_only=True)
class SnapshotArtifact:
    format: SnapshotArtifactFormat
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.format, SnapshotArtifactFormat):
            raise TypeError("artifact format must be a SnapshotArtifactFormat")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("artifact sha256 must be a lowercase 64-hex digest")
        _require_positive_int(self.size_bytes, description="artifact size_bytes")

    def to_dict(self) -> dict[str, object]:
        return {
            "format": self.format.value,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SnapshotArtifact:
        _require_exact_fields(
            raw,
            {"format", "sha256", "size_bytes"},
            description="artifact",
        )
        try:
            artifact_format = SnapshotArtifactFormat(_string(raw, "format"))
        except ValueError as exc:
            raise ValueError("unsupported snapshot artifact format") from exc
        return cls(
            format=artifact_format,
            sha256=_string(raw, "sha256"),
            size_bytes=_integer(raw, "size_bytes"),
        )


@dataclass(frozen=True, kw_only=True)
class AnonymizedSnapshotManifest:
    snapshot_id: str
    source_environment: str
    source_schema_revision: str
    anonymizer_sha: str
    generated_at: str
    producer: SnapshotProducer
    residual_scan: ResidualScanProof
    artifact: SnapshotArtifact
    contract_version: int = SNAPSHOT_MANIFEST_VERSION

    def __post_init__(self) -> None:
        require_contract_version(
            self.contract_version,
            SNAPSHOT_MANIFEST_VERSION,
            description="snapshot manifest version",
        )
        if not _SNAPSHOT_ID_RE.fullmatch(self.snapshot_id):
            raise ValueError("snapshot_id must be 8-128 URL-safe characters")
        if self.source_environment != "production":
            raise ValueError("source_environment must be production")
        if not _SCHEMA_REVISION_RE.fullmatch(self.source_schema_revision):
            raise ValueError("source_schema_revision is invalid")
        if not _SHA40_RE.fullmatch(self.anonymizer_sha):
            raise ValueError("anonymizer_sha must be a lowercase 40-hex commit sha")
        _require_utc_rfc3339(self.generated_at)
        if not isinstance(self.producer, SnapshotProducer):
            raise TypeError("producer must be a SnapshotProducer")
        if not isinstance(self.residual_scan, ResidualScanProof):
            raise TypeError("residual_scan must be a ResidualScanProof")
        if not isinstance(self.artifact, SnapshotArtifact):
            raise TypeError("artifact must be a SnapshotArtifact")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "snapshot_id": self.snapshot_id,
            "source_environment": self.source_environment,
            "source_schema_revision": self.source_schema_revision,
            "anonymizer_sha": self.anonymizer_sha,
            "generated_at": self.generated_at,
            "producer": self.producer.to_dict(),
            "residual_scan": self.residual_scan.to_dict(),
            "artifact": self.artifact.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> AnonymizedSnapshotManifest:
        _require_exact_fields(
            raw,
            {
                "contract_version",
                "snapshot_id",
                "source_environment",
                "source_schema_revision",
                "anonymizer_sha",
                "generated_at",
                "producer",
                "residual_scan",
                "artifact",
            },
            description="manifest",
        )
        contract_version = parse_contract_version(
            raw,
            SNAPSHOT_MANIFEST_VERSION,
            description="snapshot manifest version",
        )
        return cls(
            contract_version=contract_version,
            snapshot_id=_string(raw, "snapshot_id"),
            source_environment=_string(raw, "source_environment"),
            source_schema_revision=_string(raw, "source_schema_revision"),
            anonymizer_sha=_string(raw, "anonymizer_sha"),
            generated_at=_string(raw, "generated_at"),
            producer=SnapshotProducer.from_dict(_mapping(raw, "producer")),
            residual_scan=ResidualScanProof.from_dict(_mapping(raw, "residual_scan")),
            artifact=SnapshotArtifact.from_dict(_mapping(raw, "artifact")),
        )

    @staticmethod
    def json_schema() -> dict[str, Any]:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Anonymized production snapshot manifest",
            "type": "object",
            "additionalProperties": False,
            "required": [
                "contract_version",
                "snapshot_id",
                "source_environment",
                "source_schema_revision",
                "anonymizer_sha",
                "generated_at",
                "producer",
                "residual_scan",
                "artifact",
            ],
            "properties": {
                "contract_version": {"const": SNAPSHOT_MANIFEST_VERSION},
                "snapshot_id": {"type": "string", "pattern": _SNAPSHOT_ID_RE.pattern},
                "source_environment": {"const": "production"},
                "source_schema_revision": {
                    "type": "string",
                    "pattern": _SCHEMA_REVISION_RE.pattern,
                },
                "anonymizer_sha": {"type": "string", "pattern": _SHA40_RE.pattern},
                "generated_at": {"type": "string", "format": "date-time"},
                "producer": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["repository", "source_sha", "run_id", "run_url"],
                    "properties": {
                        "repository": {"type": "string", "minLength": 1},
                        "source_sha": {"type": "string", "pattern": _SHA40_RE.pattern},
                        "run_id": {"type": "string", "pattern": _RUN_ID_RE.pattern},
                        "run_url": {"type": "string", "format": "uri"},
                    },
                },
                "residual_scan": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "status",
                        "classified_columns",
                        "tables_scanned",
                        "residuals_found",
                    ],
                    "properties": {
                        "status": {"const": "passed"},
                        "classified_columns": {"type": "integer", "minimum": 1},
                        "tables_scanned": {"type": "integer", "minimum": 1},
                        "residuals_found": {"const": 0},
                    },
                },
                "artifact": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["format", "sha256", "size_bytes"],
                    "properties": {
                        "format": {"const": "postgresql-custom"},
                        "sha256": {"type": "string", "pattern": _SHA256_RE.pattern},
                        "size_bytes": {"type": "integer", "minimum": 1},
                    },
                },
            },
        }


def verify_snapshot_artifact(
    manifest: AnonymizedSnapshotManifest,
    path: str | Path,
) -> None:
    """Fail closed unless ``path`` is the exact artifact bound by ``manifest``."""
    artifact_path = Path(path)
    try:
        size = artifact_path.stat().st_size
    except OSError as exc:
        raise ValueError("snapshot artifact is unavailable") from exc
    if size != manifest.artifact.size_bytes:
        raise ValueError("snapshot artifact size mismatch")

    digest = hashlib.sha256()
    try:
        with artifact_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValueError("snapshot artifact is unavailable") from exc
    if digest.hexdigest() != manifest.artifact.sha256:
        raise ValueError("snapshot artifact sha256 mismatch")


def _require_exact_fields(raw: Mapping[str, Any], expected: set[str], *, description: str) -> None:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{description} must be an object")
    if set(raw) != expected:
        raise ValueError(f"{description} fields must exactly match v1")


def _string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _integer(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    if type(value) is not int:
        raise ValueError(f"{key} must be an integer")
    return value


def _mapping(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object")
    return value


def _require_positive_int(value: object, *, description: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{description} must be a positive integer")


def _require_utc_rfc3339(value: str) -> None:
    if not isinstance(value, str) or not _UTC_RFC3339_RE.fullmatch(value):
        raise ValueError("generated_at must be a UTC RFC3339 timestamp ending in Z")
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError("generated_at must be a UTC RFC3339 timestamp ending in Z") from exc


__all__ = [
    "AnonymizedSnapshotManifest",
    "ResidualScanProof",
    "ResidualScanStatus",
    "SNAPSHOT_MANIFEST_VERSION",
    "SnapshotArtifact",
    "SnapshotArtifactFormat",
    "SnapshotProducer",
    "verify_snapshot_artifact",
]
