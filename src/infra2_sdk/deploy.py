"""Versioned wire contract between an application and the infra2 deploy receiver."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from infra2_sdk._wire import parse_contract_version, require_contract_version

CONTRACT_VERSION = 1
_REQUEST_ID_RE = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9._:-]{7,127}\Z")
_SERVICE_RE = re.compile(r"\A[a-z][a-z0-9_]*/[a-z][a-z0-9_]*\Z")
_REPOSITORY_RE = re.compile(r"\A[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+\Z")
_SHA40_RE = re.compile(r"\A[0-9a-f]{40}\Z")


class DeployOperation(StrEnum):
    DEPLOY = "deploy"
    ROLLBACK = "rollback"
    REMOVE = "remove"


class DeployType(StrEnum):
    STAGING = "staging"
    PRODUCTION = "prod"
    PREVIEW_BRANCH = "preview/branch"
    PREVIEW_PR = "preview/pr"
    PREVIEW_COMMIT = "preview/commit"
    PREVIEW_TAG = "preview/tag"
    CANARY = "canary"


class DeployState(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class DeployEvidence:
    source_run_url: str
    source_run_id: str = ""
    staging_run_url: str = ""
    reviewed_change_url: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DeployEvidence:
        return cls(
            source_run_url=_string(raw, "source_run_url"),
            source_run_id=_string(raw, "source_run_id", required=False),
            staging_run_url=_string(raw, "staging_run_url", required=False),
            reviewed_change_url=_string(raw, "reviewed_change_url", required=False),
        )


@dataclass(frozen=True)
class DeployRequest:
    request_id: str
    operation: DeployOperation
    service: str
    deploy_type: DeployType
    version_ref: str
    source_repository: str
    source_sha: str
    evidence: DeployEvidence
    contract_version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        validate_deploy_request(self)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["operation"] = self.operation.value
        data["deploy_type"] = self.deploy_type.value
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DeployRequest:
        evidence = raw.get("evidence")
        if not isinstance(evidence, Mapping):
            raise ValueError("evidence must be an object")
        contract_version = parse_contract_version(
            raw,
            CONTRACT_VERSION,
            description="contract_version",
        )
        return cls(
            contract_version=contract_version,
            request_id=_string(raw, "request_id"),
            operation=DeployOperation(_string(raw, "operation")),
            service=_string(raw, "service"),
            deploy_type=DeployType(_string(raw, "deploy_type")),
            version_ref=_string(raw, "version_ref"),
            source_repository=_string(raw, "source_repository"),
            source_sha=_string(raw, "source_sha").lower(),
            evidence=DeployEvidence.from_dict(evidence),
        )


@dataclass(frozen=True)
class DeployStatus:
    request_id: str
    state: DeployState
    detail: str = ""
    evidence_url: str = ""
    deployed_version: str = ""
    contract_version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        require_contract_version(
            self.contract_version,
            CONTRACT_VERSION,
            description="contract_version",
        )
        if not _REQUEST_ID_RE.match(self.request_id):
            raise ValueError("invalid request_id")
        if self.state == DeployState.SUCCEEDED and not self.evidence_url:
            raise ValueError("successful deploy status requires evidence_url")
        if self.state in {DeployState.FAILED, DeployState.REJECTED} and not self.detail:
            raise ValueError("failed or rejected deploy status requires detail")

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["state"] = self.state.value
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DeployStatus:
        contract_version = parse_contract_version(
            raw,
            CONTRACT_VERSION,
            description="contract_version",
        )
        return cls(
            contract_version=contract_version,
            request_id=_string(raw, "request_id"),
            state=DeployState(_string(raw, "state")),
            detail=_string(raw, "detail", required=False),
            evidence_url=_string(raw, "evidence_url", required=False),
            deployed_version=_string(raw, "deployed_version", required=False),
        )


def validate_deploy_request(request: DeployRequest) -> None:
    require_contract_version(
        request.contract_version,
        CONTRACT_VERSION,
        description="contract_version",
    )
    if not _REQUEST_ID_RE.match(request.request_id):
        raise ValueError("request_id must be 8-128 URL-safe characters")
    if not _SERVICE_RE.match(request.service):
        raise ValueError("service must have the form project/service")
    if not request.version_ref.strip():
        raise ValueError("version_ref is required")
    if not _REPOSITORY_RE.match(request.source_repository):
        raise ValueError("source_repository must have the form owner/repository")
    if not _SHA40_RE.match(request.source_sha):
        raise ValueError("source_sha must be a lowercase 40-hex commit sha")
    if not request.evidence.source_run_url.startswith("https://github.com/"):
        raise ValueError("evidence.source_run_url must be a GitHub URL")
    if request.deploy_type == DeployType.PRODUCTION and not (
        request.evidence.staging_run_url and request.evidence.reviewed_change_url
    ):
        raise ValueError("production deploys require staging and reviewed-change evidence")
    if request.operation == DeployOperation.REMOVE and request.deploy_type not in {
        DeployType.PREVIEW_BRANCH,
        DeployType.PREVIEW_PR,
        DeployType.PREVIEW_COMMIT,
        DeployType.PREVIEW_TAG,
        DeployType.CANARY,
    }:
        raise ValueError("remove is limited to preview and canary targets")


def _string(raw: Mapping[str, Any], key: str, *, required: bool = True) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{key} is required")
    return value
