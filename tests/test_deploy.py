from dataclasses import replace

import pytest

from infra2_sdk.deploy import (
    DeployEvidence,
    DeployOperation,
    DeployRequest,
    DeployState,
    DeployStatus,
    DeployType,
)

SHA = "a" * 40


def request(**overrides) -> DeployRequest:
    values = {
        "request_id": "run-12345678",
        "operation": DeployOperation.DEPLOY,
        "service": "finance_report/app",
        "deploy_type": DeployType.STAGING,
        "version_ref": "v1.2.3",
        "source_repository": "wangzitian0/finance_report",
        "source_sha": SHA,
        "evidence": DeployEvidence(
            source_run_url="https://github.com/wangzitian0/finance_report/actions/runs/1"
        ),
    }
    values.update(overrides)
    return DeployRequest(**values)


def test_request_round_trip() -> None:
    original = request()
    assert DeployRequest.from_dict(original.to_dict()) == original


def test_production_requires_staging_and_review_evidence() -> None:
    with pytest.raises(ValueError, match="staging and reviewed-change"):
        request(deploy_type=DeployType.PRODUCTION)


def test_remove_is_limited_to_ephemeral_targets() -> None:
    with pytest.raises(ValueError, match="remove is limited"):
        request(operation=DeployOperation.REMOVE)


def test_contract_version_fails_closed() -> None:
    raw = request().to_dict()
    raw["contract_version"] = 2
    with pytest.raises(ValueError, match="unsupported contract_version"):
        DeployRequest.from_dict(raw)
    raw["contract_version"] = True
    with pytest.raises(ValueError, match="integer"):
        DeployRequest.from_dict(raw)
    with pytest.raises(ValueError, match="integer"):
        request(contract_version=True)


def test_success_status_requires_evidence() -> None:
    with pytest.raises(ValueError, match="evidence_url"):
        DeployStatus(request_id="run-12345678", state=DeployState.SUCCEEDED)

    status = DeployStatus(
        request_id="run-12345678",
        state=DeployState.SUCCEEDED,
        evidence_url="https://github.com/wangzitian0/infra2/actions/runs/2",
        deployed_version="v1.2.3",
    )
    assert DeployStatus.from_dict(status.to_dict()) == status


def test_source_sha_is_lowercase_and_full_length() -> None:
    with pytest.raises(ValueError, match="lowercase 40-hex"):
        replace(request(), source_sha="A" * 40)


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"request_id": "short"}, "request_id"),
        ({"service": "invalid"}, "project/service"),
        ({"version_ref": "  "}, "version_ref"),
        ({"source_repository": "invalid"}, "owner/repository"),
        (
            {"evidence": DeployEvidence(source_run_url="https://example.com/run")},
            "GitHub URL",
        ),
    ],
)
def test_request_validation(changes: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        request(**changes)


def test_preview_remove_is_valid() -> None:
    deploy_request = request(
        operation=DeployOperation.REMOVE,
        deploy_type=DeployType.PREVIEW_PR,
        version_ref="42",
    )
    assert deploy_request.operation == DeployOperation.REMOVE


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("evidence", [], "evidence must be an object"),
        ("contract_version", "bad", "must be an integer"),
        ("request_id", 123, "must be a string"),
        ("service", "", "is required"),
    ],
)
def test_request_deserialization_rejects_bad_types(field, value, message) -> None:
    raw = request().to_dict()
    raw[field] = value
    with pytest.raises(ValueError, match=message):
        DeployRequest.from_dict(raw)


def test_status_failure_requires_detail_and_round_trips() -> None:
    with pytest.raises(ValueError, match="requires detail"):
        DeployStatus(request_id="run-12345678", state=DeployState.FAILED)
    status = DeployStatus(
        request_id="run-12345678",
        state=DeployState.REJECTED,
        detail="unsupported service",
    )
    assert DeployStatus.from_dict(status.to_dict()) == status


def test_status_rejects_bad_contract_and_types() -> None:
    with pytest.raises(ValueError, match="unsupported contract_version"):
        DeployStatus(
            request_id="run-12345678",
            state=DeployState.ACCEPTED,
            contract_version=2,
        )
    raw = {"contract_version": "bad", "request_id": "run-12345678", "state": "accepted"}
    with pytest.raises(ValueError, match="must be an integer"):
        DeployStatus.from_dict(raw)
    raw["contract_version"] = True
    with pytest.raises(ValueError, match="must be an integer"):
        DeployStatus.from_dict(raw)
    with pytest.raises(ValueError, match="must be an integer"):
        DeployStatus(
            request_id="run-12345678",
            state=DeployState.ACCEPTED,
            contract_version=True,
        )
