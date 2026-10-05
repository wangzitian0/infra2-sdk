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
    validate_wire_shape,
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


# --- validate_wire_shape: exact-field-set completeness (infra2#597 audit) ---------


def test_validate_wire_shape_accepts_the_real_wire_dict() -> None:
    raw = request().to_dict()
    validate_wire_shape(raw)
    assert DeployRequest.from_dict(raw) == request()


@pytest.mark.parametrize(
    "mutator,message",
    [
        (
            lambda r: (r.update({"unexpected_field": "oops"}), r)[1],
            "request fields must exactly match",
        ),
        (
            lambda r: (r.pop("source_sha"), r)[1],
            "request fields must exactly match",
        ),
        (
            lambda r: (r["evidence"].update({"unexpected_field": "oops"}), r)[1],
            "evidence fields must exactly match",
        ),
        (
            lambda r: (r["evidence"].pop("source_run_id"), r)[1],
            "evidence fields must exactly match",
        ),
        (
            lambda r: (r.update({"evidence": "not-an-object"}), r)[1],
            "evidence must be an object",
        ),
        (
            lambda r: "not-an-object",
            "deploy request must be an object",
        ),
    ],
)
def test_validate_wire_shape_rejects_malformed_input(mutator, message) -> None:
    raw = request().to_dict()
    target = mutator(raw)
    with pytest.raises(ValueError, match=message):
        validate_wire_shape(target)  # type: ignore[arg-type]


def test_validate_wire_shape_field_sets_track_the_dataclasses_not_a_hardcoded_copy() -> None:
    """Regression guard for the exact drift this function replaces: the expected
    field sets are derived from DeployRequest/DeployEvidence's own dataclass
    fields, not a hand-copied literal that could silently go stale."""
    from dataclasses import fields

    raw = request().to_dict()
    assert set(raw) == {f.name for f in fields(DeployRequest)}
    assert set(raw["evidence"]) == {f.name for f in fields(DeployEvidence)}


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
    assert expectation().expected_display_title("v0.0.6") == "Release Images v0.0.6"


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
        ProductionEvidencePolicy.from_dict({**policy().to_dict(), "contract_version": 2})


def test_policy_from_dict_requires_nested_objects() -> None:
    raw = policy().to_dict()
    raw["staging"] = "deploy-release.yml"
    with pytest.raises(ValueError, match="staging must be an object"):
        ProductionEvidencePolicy.from_dict(raw)


def test_expectation_round_trips_require_head_sha_false() -> None:
    # A staging deploy dispatched on a branch (head_sha = branch tip, not the tag
    # commit) declares this explicitly — never a silent verifier-side skip.
    branch_dispatched = expectation(require_head_sha=False)
    raw = json.loads(json.dumps(branch_dispatched.to_dict()))
    assert RunEvidenceExpectation.from_dict(raw) == branch_dispatched
    assert RunEvidenceExpectation.from_dict(raw).require_head_sha is False


def test_expectation_defaults_require_head_sha_true() -> None:
    raw = expectation().to_dict()
    del raw["require_head_sha"]
    assert RunEvidenceExpectation.from_dict(raw).require_head_sha is True


def test_expectation_rejects_a_non_boolean_require_head_sha() -> None:
    raw = expectation().to_dict()
    raw["require_head_sha"] = "false"
    with pytest.raises(ValueError, match="require_head_sha must be a boolean"):
        RunEvidenceExpectation.from_dict(raw)


def test_build_deploy_request_constructs_valid_request() -> None:
    from infra2_sdk.deploy import DeployOperation, DeployType, build_deploy_request

    req = build_deploy_request(
        service="finance_report/app",
        deploy_type=DeployType.STAGING,
        version_ref="v1.2.3",
        source_repository="wangzitian0/finance_report",
        source_sha="a" * 40,
        source_run_url="https://github.com/wangzitian0/finance_report/actions/runs/1",
    )
    assert req.service == "finance_report/app"
    assert req.deploy_type == DeployType.STAGING
    assert req.operation == DeployOperation.DEPLOY
    assert req.version_ref == "v1.2.3"
    assert req.source_repository == "wangzitian0/finance_report"
    assert req.source_sha == "a" * 40
    assert (
        req.evidence.source_run_url
        == "https://github.com/wangzitian0/finance_report/actions/runs/1"
    )
    assert req.request_id.startswith("req-")


def sample_policy() -> ProductionEvidencePolicy:
    return ProductionEvidencePolicy(
        service="finance_report/app",
        source=RunEvidenceExpectation(
            workflow_path=".github/workflows/deploy.yml",
            event="push",
            display_title_template="Release Images {version_ref}",
            require_head_sha=True,
        ),
        staging=RunEvidenceExpectation(
            workflow_path=".github/workflows/deploy.yml",
            event="workflow_dispatch",
            display_title_template="Deploy Staging {version_ref}",
            require_head_sha=False,
        ),
        review_base_ref="main",
    )


def test_canonical_json_produces_deterministic_sorted_output() -> None:
    from infra2_sdk.deploy import canonical_json

    req = request()
    json_str = canonical_json(req)
    assert json_str.endswith("\n")
    assert json.loads(json_str) == req.to_dict()


def prod_request(**overrides) -> DeployRequest:
    return request(
        deploy_type=DeployType.PRODUCTION,
        evidence=DeployEvidence(
            source_run_url="https://github.com/wangzitian0/finance_report/actions/runs/100",
            source_run_id="100",
            staging_run_url="https://github.com/wangzitian0/finance_report/actions/runs/101",
            reviewed_change_url="https://github.com/wangzitian0/finance_report/pull/10",
        ),
        **overrides,
    )


def test_fetch_production_evidence_policy_success() -> None:
    import base64

    from infra2_sdk.deploy import fetch_production_evidence_policy

    policy = sample_policy()
    encoded = base64.b64encode(json.dumps(policy.to_dict()).encode("utf-8")).decode("utf-8")
    req = prod_request()

    def fake_fetch(path: str) -> dict:
        return {"content": encoded}

    fetched = fetch_production_evidence_policy(req, fetch_json=fake_fetch)
    assert fetched.service == "finance_report/app"
    assert fetched.review_base_ref == "main"


def test_fetch_production_evidence_policy_missing_contract_raises() -> None:
    from infra2_sdk.deploy import fetch_production_evidence_policy

    req = prod_request()

    def fake_fetch(path: str) -> dict:
        raise ValueError("HTTP 404")

    with pytest.raises(ValueError, match="has no Production evidence contract"):
        fetch_production_evidence_policy(req, fetch_json=fake_fetch)


def test_fetch_production_evidence_policy_service_mismatch_raises() -> None:
    import base64

    from infra2_sdk.deploy import fetch_production_evidence_policy

    policy = sample_policy()
    raw = policy.to_dict()
    raw["service"] = "other/service"
    encoded = base64.b64encode(json.dumps(raw).encode("utf-8")).decode("utf-8")
    req = prod_request()

    with pytest.raises(
        ValueError, match="declares service 'other/service', not 'finance_report/app'"
    ):
        fetch_production_evidence_policy(req, fetch_json=lambda p: {"content": encoded})


def test_verify_production_evidence_success() -> None:
    from infra2_sdk.deploy import verify_production_evidence

    req = request(
        deploy_type=DeployType.PRODUCTION,
        evidence=DeployEvidence(
            source_run_url="https://github.com/wangzitian0/finance_report/actions/runs/100",
            source_run_id="100",
            staging_run_url="https://github.com/wangzitian0/finance_report/actions/runs/101",
            reviewed_change_url="https://github.com/wangzitian0/finance_report/pull/10",
        ),
    )
    source_run = {
        "repository": {"full_name": "wangzitian0/finance_report"},
        "html_url": "https://github.com/wangzitian0/finance_report/actions/runs/100",
        "status": "completed",
        "conclusion": "success",
        "head_sha": SHA,
        "path": ".github/workflows/deploy.yml",
        "event": "push",
        "display_title": "Release Images v1.2.3",
    }
    staging_run = {
        "repository": {"full_name": "wangzitian0/finance_report"},
        "html_url": "https://github.com/wangzitian0/finance_report/actions/runs/101",
        "status": "completed",
        "conclusion": "success",
        "head_sha": "c" * 40,
        "path": ".github/workflows/deploy.yml",
        "event": "workflow_dispatch",
        "display_title": "Deploy Staging v1.2.3",
    }
    pull = {
        "base": {"repo": {"full_name": "wangzitian0/finance_report"}, "ref": "main"},
        "html_url": "https://github.com/wangzitian0/finance_report/pull/10",
        "state": "closed",
        "merged_at": "2026-10-05T00:00:00Z",
        "merge_commit_sha": SHA,
        "user": {"login": "author"},
    }
    reviews = [{"user": {"login": "reviewer"}, "state": "APPROVED"}]

    responses = {
        "/repos/wangzitian0/finance_report/actions/runs/100": source_run,
        "/repos/wangzitian0/finance_report/actions/runs/101": staging_run,
        "/repos/wangzitian0/finance_report/pulls/10": pull,
        "/repos/wangzitian0/finance_report/pulls/10/reviews": reviews,
    }

    verify_production_evidence(req, policy=sample_policy(), fetch_json=responses.__getitem__)


def test_verify_production_evidence_rejected_when_changes_requested() -> None:
    from infra2_sdk.deploy import verify_production_evidence

    req = request(
        deploy_type=DeployType.PRODUCTION,
        evidence=DeployEvidence(
            source_run_url="https://github.com/wangzitian0/finance_report/actions/runs/100",
            source_run_id="100",
            staging_run_url="https://github.com/wangzitian0/finance_report/actions/runs/101",
            reviewed_change_url="https://github.com/wangzitian0/finance_report/pull/10",
        ),
    )
    responses = {
        "/repos/wangzitian0/finance_report/actions/runs/100": {
            "repository": {"full_name": "wangzitian0/finance_report"},
            "html_url": "https://github.com/wangzitian0/finance_report/actions/runs/100",
            "status": "completed",
            "conclusion": "success",
            "head_sha": SHA,
            "path": ".github/workflows/deploy.yml",
            "event": "push",
            "display_title": "Release Images v1.2.3",
        },
        "/repos/wangzitian0/finance_report/actions/runs/101": {
            "repository": {"full_name": "wangzitian0/finance_report"},
            "html_url": "https://github.com/wangzitian0/finance_report/actions/runs/101",
            "status": "completed",
            "conclusion": "success",
            "head_sha": "c" * 40,
            "path": ".github/workflows/deploy.yml",
            "event": "workflow_dispatch",
            "display_title": "Deploy Staging v1.2.3",
        },
        "/repos/wangzitian0/finance_report/pulls/10": {
            "base": {"repo": {"full_name": "wangzitian0/finance_report"}, "ref": "main"},
            "html_url": "https://github.com/wangzitian0/finance_report/pull/10",
            "state": "closed",
            "merged_at": "2026-10-05T00:00:00Z",
            "merge_commit_sha": SHA,
            "user": {"login": "author"},
        },
        "/repos/wangzitian0/finance_report/pulls/10/reviews": [
            {"user": {"login": "reviewer"}, "state": "CHANGES_REQUESTED"}
        ],
    }

    with pytest.raises(ValueError, match="pending CHANGES_REQUESTED"):
        verify_production_evidence(req, policy=sample_policy(), fetch_json=responses.__getitem__)


def test_derive_release_evidence_staging_and_prod() -> None:
    from infra2_sdk.deploy import derive_release_evidence

    responses = {
        "/repos/wangzitian0/finance_report/actions/runs?event=push&branch=v1.2.3": {
            "workflow_runs": [
                {
                    "id": 100,
                    "html_url": "https://github.com/wangzitian0/finance_report/actions/runs/100",
                    "head_sha": SHA,
                    "conclusion": "success",
                    "path": ".github/workflows/deploy.yml",
                    "display_title": "Release Images v1.2.3",
                }
            ]
        },
        "/repos/wangzitian0/finance_report/actions/runs?event=workflow_dispatch": {
            "workflow_runs": [
                {
                    "id": 101,
                    "conclusion": "success",
                    "path": ".github/workflows/deploy.yml",
                    "display_title": "Deploy Staging v1.2.3",
                }
            ]
        },
        f"/repos/wangzitian0/finance_report/commits/{SHA}/pulls": [
            {
                "number": 10,
                "merged_at": "2026-10-05T00:00:00Z",
                "merge_commit_sha": SHA,
                "base": {"ref": "main"},
            }
        ],
    }

    ev_staging = derive_release_evidence(
        repository="wangzitian0/finance_report",
        version_ref="v1.2.3",
        deploy_type=DeployType.STAGING,
        tag_sha=SHA,
        policy=sample_policy(),
        fetch_json=responses.__getitem__,
    )
    assert ev_staging.source_run_id == "100"
    assert (
        ev_staging.source_run_url
        == "https://github.com/wangzitian0/finance_report/actions/runs/100"
    )
    assert ev_staging.staging_run_url == ""
    assert ev_staging.reviewed_change_url == ""

    ev_prod = derive_release_evidence(
        repository="wangzitian0/finance_report",
        version_ref="v1.2.3",
        deploy_type=DeployType.PRODUCTION,
        tag_sha=SHA,
        policy=sample_policy(),
        fetch_json=responses.__getitem__,
    )
    assert ev_prod.source_run_id == "100"
    assert (
        ev_prod.staging_run_url == "https://github.com/wangzitian0/finance_report/actions/runs/101"
    )
    assert ev_prod.reviewed_change_url == "https://github.com/wangzitian0/finance_report/pull/10"


def test_deploy_cli_build_request(tmp_path) -> None:
    from infra2_sdk.deploy import main as deploy_main

    out_file = tmp_path / "request.json"
    rc = deploy_main(
        [
            "build-request",
            "--service",
            "finance_report/app",
            "--source-repo",
            "wangzitian0/finance_report",
            "--source-sha",
            SHA,
            "--version-ref",
            "v1.2.3",
            "--source-run-url",
            "https://github.com/wangzitian0/finance_report/actions/runs/100",
            "--source-run-id",
            "100",
            "--output",
            str(out_file),
        ]
    )
    assert rc == 0
    loaded = json.loads(out_file.read_text(encoding="utf-8"))
    assert loaded["service"] == "finance_report/app"
    assert loaded["evidence"]["source_run_id"] == "100"
