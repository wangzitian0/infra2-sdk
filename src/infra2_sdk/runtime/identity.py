"""Runtime and software-supply-chain identity bound to standard coordinates."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from infra2_sdk.runtime.environment import EnvironmentTier, resolve_environment_tier

_SHA40_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_OCI_DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


@dataclass(frozen=True)
class RuntimeIdentity:
    """Identity emitted by a running artifact and compared by deployment canaries."""

    service_name: str
    service_version: str
    environment: EnvironmentTier
    commit_sha: str
    image_digest: str = ""
    configuration_sha256: str = ""
    release_id: str = ""
    instance_id: str = ""
    provenance_uri: str = ""
    sbom_uri: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", resolve_environment_tier(self.environment))
        if not self.service_name.strip() or not self.service_version.strip():
            raise ValueError("service_name and service_version are required")
        if self.commit_sha != "unknown" and not _SHA40_RE.match(self.commit_sha):
            raise ValueError("commit_sha must be 'unknown' or a lowercase 40-hex SHA")
        if self.image_digest and not _OCI_DIGEST_RE.match(self.image_digest):
            raise ValueError("image_digest must be an OCI sha256 digest")
        if self.configuration_sha256 and not _SHA256_RE.match(self.configuration_sha256):
            raise ValueError("configuration_sha256 must be lowercase 64-hex")
        for name, value in (("provenance_uri", self.provenance_uri), ("sbom_uri", self.sbom_uri)):
            if value and not value.startswith(("https://", "oci://")):
                raise ValueError(f"{name} must use https:// or oci://")

    def validate_protected(self) -> None:
        """Require immutable coordinates in Staging and Production."""

        if self.environment not in {EnvironmentTier.STAGING, EnvironmentTier.PRODUCTION}:
            return
        missing = [
            name
            for name, value in (
                ("commit_sha", self.commit_sha if self.commit_sha != "unknown" else ""),
                ("image_digest", self.image_digest),
                ("configuration_sha256", self.configuration_sha256),
                ("release_id", self.release_id),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"protected runtime identity is missing: {', '.join(missing)}")

    def to_dict(self) -> dict[str, str]:
        data = asdict(self)
        data["environment"] = self.environment.value
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RuntimeIdentity:
        return cls(
            service_name=_string(raw, "service_name"),
            service_version=_string(raw, "service_version"),
            environment=resolve_environment_tier(_string(raw, "environment")),
            commit_sha=_string(raw, "commit_sha"),
            image_digest=_string(raw, "image_digest", required=False),
            configuration_sha256=_string(raw, "configuration_sha256", required=False),
            release_id=_string(raw, "release_id", required=False),
            instance_id=_string(raw, "instance_id", required=False),
            provenance_uri=_string(raw, "provenance_uri", required=False),
            sbom_uri=_string(raw, "sbom_uri", required=False),
        )

    def to_otel_resource_attributes(self) -> dict[str, str]:
        attributes = {
            "service.name": self.service_name,
            "service.version": self.service_version,
            "deployment.environment.name": self.environment.value,
            "vcs.ref.head.revision": self.commit_sha,
        }
        optional = {
            "service.instance.id": self.instance_id,
            "container.image.id": self.image_digest,
            "infra2.configuration.sha256": self.configuration_sha256,
            "infra2.release.id": self.release_id,
            "infra2.provenance.uri": self.provenance_uri,
            "infra2.sbom.uri": self.sbom_uri,
        }
        attributes.update({key: value for key, value in optional.items() if value})
        return attributes

    @staticmethod
    def json_schema() -> dict[str, Any]:
        return {
            "$schema": JSON_SCHEMA_DIALECT,
            "title": "Runtime identity",
            "type": "object",
            "additionalProperties": False,
            "required": ["service_name", "service_version", "environment", "commit_sha"],
            "properties": {
                "service_name": {"type": "string", "minLength": 1},
                "service_version": {"type": "string", "minLength": 1},
                "environment": {"enum": [tier.value for tier in EnvironmentTier]},
                "commit_sha": {"type": "string", "pattern": "^(unknown|[0-9a-f]{40})$"},
                "image_digest": {"type": "string", "pattern": "^(|sha256:[0-9a-f]{64})$"},
                "configuration_sha256": {
                    "type": "string",
                    "pattern": "^(|[0-9a-f]{64})$",
                },
                "release_id": {"type": "string"},
                "instance_id": {"type": "string"},
                "provenance_uri": {"type": "string"},
                "sbom_uri": {"type": "string"},
            },
        }


def configuration_fingerprint(parts: Mapping[str, str | bytes]) -> str:
    """Hash named configuration inputs with unambiguous length framing."""

    digest = hashlib.sha256()
    for name in sorted(parts):
        value = parts[name]
        encoded = value.encode("utf-8") if isinstance(value, str) else value
        if not isinstance(encoded, bytes):
            raise TypeError("configuration values must be str or bytes")
        name_bytes = name.encode("utf-8")
        digest.update(len(name_bytes).to_bytes(4, "big"))
        digest.update(name_bytes)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _string(raw: Mapping[str, Any], key: str, *, required: bool = True) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{key} is required")
    return value
