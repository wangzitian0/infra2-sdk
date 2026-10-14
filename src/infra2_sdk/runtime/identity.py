"""Runtime and software-supply-chain identity bound to standard coordinates."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from infra2_sdk._wire import (
    JSON_SCHEMA_DIALECT,
    parse_string,
)
from infra2_sdk._wire import (
    canonical_sha256 as _wire_canonical_sha256,
)
from infra2_sdk.runtime.environment import (
    EnvironmentTier,
    RuntimeEnvKey,
    _resolve_service_identity,
    normalize_deployment_environment,
    resolve_environment_tier,
    resolve_runtime_env,
)

_SHA40_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_OCI_DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class RuntimeIdentity:
    """Identity emitted by a running artifact and compared by deployment canaries."""

    service_name: str
    service_version: str
    environment: EnvironmentTier
    commit_sha: str
    deployment_environment: str = field(default="", kw_only=True)
    image_digest: str = ""
    configuration_sha256: str = ""
    release_id: str = ""
    instance_id: str = ""
    provenance_uri: str = ""
    sbom_uri: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", resolve_environment_tier(self.environment))
        display = normalize_deployment_environment(self.deployment_environment, self.environment)
        object.__setattr__(self, "deployment_environment", display)
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

    def validate_deployed(self) -> None:
        """Require commit identity in every platform tier and full identity when protected."""

        if (
            self.environment
            in {
                EnvironmentTier.PREVIEW,
                EnvironmentTier.STAGING,
                EnvironmentTier.PRODUCTION,
            }
            and self.commit_sha == "unknown"
        ):
            raise ValueError("deployed runtime identity is missing commit_sha")
        self.validate_protected()

    def to_dict(self) -> dict[str, str]:
        data = asdict(self)
        data["environment"] = self.environment.value
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RuntimeIdentity:
        return cls(
            service_name=parse_string(raw, "service_name"),
            service_version=parse_string(raw, "service_version"),
            environment=resolve_environment_tier(parse_string(raw, "environment")),
            commit_sha=parse_string(raw, "commit_sha"),
            deployment_environment=parse_string(raw, "deployment_environment", required=False),
            image_digest=parse_string(raw, "image_digest", required=False),
            configuration_sha256=parse_string(raw, "configuration_sha256", required=False),
            release_id=parse_string(raw, "release_id", required=False),
            instance_id=parse_string(raw, "instance_id", required=False),
            provenance_uri=parse_string(raw, "provenance_uri", required=False),
            sbom_uri=parse_string(raw, "sbom_uri", required=False),
        )

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        strict: bool = False,
    ) -> RuntimeIdentity:
        """Build identity from deploy_v2-derived results or equivalent standalone env.

        The service name is ``OTEL_SERVICE_NAME``, then ``service.name`` in
        ``OTEL_RESOURCE_ATTRIBUTES``. A missing name raises ``ValueError``.
        """

        resolved = _resolve_service_identity(environ, strict=strict)
        if resolved.service_name is None:
            raise ValueError(f"{RuntimeEnvKey.SERVICE_NAME} is required")
        identity = cls(
            service_name=resolved.service_name,
            service_version=resolved.service_version,
            environment=resolved.environment.tier,
            deployment_environment=resolved.deployment_environment or resolved.environment.name,
            commit_sha=resolve_runtime_env(environ, RuntimeEnvKey.GIT_COMMIT_SHA).value
            or "unknown",
            image_digest=resolve_runtime_env(environ, RuntimeEnvKey.IMAGE_DIGEST, default="").value
            or "",
            configuration_sha256=resolve_runtime_env(
                environ,
                RuntimeEnvKey.CONFIGURATION_SHA256,
                default="",
            ).value
            or "",
            release_id=resolve_runtime_env(environ, RuntimeEnvKey.RELEASE_ID, default="").value
            or "",
            instance_id=resolved.instance_id,
        )
        if strict:
            identity.validate_deployed()
        return identity

    def to_standard_otel_resource_attributes(self) -> dict[str, str]:
        """Return only provider-neutral OpenTelemetry semantic coordinates."""

        attributes = {
            "service.name": self.service_name,
            "service.version": self.service_version,
            "deployment.environment.name": self.deployment_environment,
            "vcs.ref.head.revision": self.commit_sha,
        }
        optional = {
            "service.instance.id": self.instance_id,
            "container.image.id": self.image_digest,
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
                "deployment_environment": {"type": "string"},
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


def canonical_sha256(payload: Any) -> str:
    """sha256 of a JSON payload in canonical form: sorted keys, compact separators, ASCII
    escaping (the same canonical JSON the DeployRequest renderer and TrueAlpha's contracts
    use, so the estate has one hash for release manifests, decision records and
    configuration fingerprints — whatever a writer's JSON library defaults to).
    """
    return _wire_canonical_sha256(payload)
