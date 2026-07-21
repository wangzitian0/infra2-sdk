import json
from dataclasses import replace

import pytest

from infra2_sdk.deploy import (
    DeployEvidence,
    DeployOperation,
    DeployRequest,
    DeployState,
    DeployStatus,
    DeployType,
    ProductionEvidencePolicy,
    RunEvidenceExpectation,
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


# --- Production evidence policy (infra2-sdk#8) ------------------------------------


def expectation(**overrides) -> "RunEvidenceExpectation":
    values = {
        "workflow_path": ".github/workflows/ci-required.yml",
        "event": "push",
        "display_title_template": "Release Images {version_ref}",
    }
    values.update(overrides)
    return RunEvidenceExpectation(**values)


def policy(**overrides) -> "ProductionEvidencePolicy":
    values = {
        "service": "truealpha/app",
        "source": expectation(),
        "staging": expectation(
            workflow_path=".github/workflows/deploy-release.yml",
            event="workflow_dispatch",
            display_title_template="Deploy staging {version_ref}",
        ),
        "review_base_ref": "main",
    }
    values.update(overrides)
    return ProductionEvidencePolicy(**values)


def test_policy_json_round_trip() -> None:
    # Apps check this contract into their own repo as plain JSON: serialize ->
    # json -> deserialize must reproduce an identical value.
    original = policy()
    raw = json.loads(json.dumps(original.to_dict()))
    assert ProductionEvidencePolicy.from_dict(raw) == original


def test_expectation_renders_the_version_ref() -> None:
    assert (
        expectation().expected_display_title("v0.0.6") == "Release Images v0.0.6"
    )


def test_expectation_rejects_a_non_workflow_path() -> None:
    with pytest.raises(ValueError, match="workflow_path"):
        expectation(workflow_path="tools/deploy.sh")


def test_expectation_rejects_an_unknown_event() -> None:
    with pytest.raises(ValueError, match="event must be one of"):
        expectation(event="repository_dispatch")


def test_expectation_rejects_placeholders_other_than_version_ref() -> None:
    # Only the literal {version_ref} substitution — never arbitrary/unverifiable
    # placeholders an app-side self-test couldn't grep for.
    with pytest.raises(ValueError, match="version_ref.*placeholder only"):
        expectation(display_title_template="Deploy {deploy_type} {version_ref}")


def test_policy_rejects_a_malformed_service_key() -> None:
    with pytest.raises(ValueError, match="project/service"):
        policy(service="TrueAlpha")


def test_policy_rejects_an_unknown_contract_version() -> None:
    with pytest.raises(ValueError, match="evidence policy contract_version"):
        ProductionEvidencePolicy.from_dict(
            {**policy().to_dict(), "contract_version": 2}
        )


def test_policy_from_dict_requires_nested_objects() -> None:
    raw = policy().to_dict()
    raw["staging"] = "deploy-release.yml"
    with pytest.raises(ValueError, match="staging must be an object"):
        ProductionEvidencePolicy.from_dict(raw)
