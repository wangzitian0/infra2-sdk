import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError

import httpx
import pytest

from infra2_sdk.deploy import (
    PRODUCTION_DEPLOY_BUDGET_SECONDS,
    PRODUCTION_REQUEST_MAX_AGE_SECONDS,
    DeployEvidence,
    DeployOperation,
    DeployRequest,
    DeployType,
    ProductionEvidencePolicy,
    _default_github_json_fetcher,
    _HealthCheckResult,
    _RunEvidenceExpectation,
    _validate_wire_shape,
    _verify_reviewed_pull,
    _verify_run,
    default_http_get,
    parse_sender_deadline,
    poll_until_healthy,
    production_request_accept_until,
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


# --- _validate_wire_shape: exact-field-set completeness (infra2#597 audit) ---------


def test_validate_wire_shape_accepts_the_real_wire_dict() -> None:
    raw = request().to_dict()
    _validate_wire_shape(raw)
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
        _validate_wire_shape(target)  # type: ignore[arg-type]


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


def test_production_request_max_age_seconds_is_twenty_four_minutes() -> None:
    import infra2_sdk

    assert type(PRODUCTION_REQUEST_MAX_AGE_SECONDS) is int
    assert PRODUCTION_REQUEST_MAX_AGE_SECONDS == 1440
    assert infra2_sdk.PRODUCTION_REQUEST_MAX_AGE_SECONDS == 1440


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


# --- Production evidence policy (infra2-sdk#8) ------------------------------------


def expectation(**overrides) -> "_RunEvidenceExpectation":
    values = {
        "workflow_path": ".github/workflows/ci-required.yml",
        "event": "push",
        "display_title_template": "Release Images {version_ref}",
    }
    values.update(overrides)
    return _RunEvidenceExpectation(**values)


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
    assert _RunEvidenceExpectation.from_dict(raw) == branch_dispatched
    assert _RunEvidenceExpectation.from_dict(raw).require_head_sha is False


def test_expectation_defaults_require_head_sha_true() -> None:
    raw = expectation().to_dict()
    del raw["require_head_sha"]
    assert _RunEvidenceExpectation.from_dict(raw).require_head_sha is True


def test_expectation_rejects_a_non_boolean_require_head_sha() -> None:
    raw = expectation().to_dict()
    raw["require_head_sha"] = "false"
    with pytest.raises(ValueError, match="require_head_sha must be a boolean"):
        _RunEvidenceExpectation.from_dict(raw)


def sample_policy() -> ProductionEvidencePolicy:
    return ProductionEvidencePolicy(
        service="finance_report/app",
        source=_RunEvidenceExpectation(
            workflow_path=".github/workflows/deploy.yml",
            event="push",
            display_title_template="Release Images {version_ref}",
            require_head_sha=True,
        ),
        staging=_RunEvidenceExpectation(
            workflow_path=".github/workflows/deploy.yml",
            event="workflow_dispatch",
            display_title_template="Deploy Staging {version_ref}",
            require_head_sha=False,
        ),
        review_base_ref="main",
    )


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

    from infra2_sdk.deploy import _fetch_production_evidence_policy

    policy = sample_policy()
    encoded = base64.b64encode(json.dumps(policy.to_dict()).encode("utf-8")).decode("utf-8")
    req = prod_request()

    def fake_fetch(path: str) -> dict:
        return {"content": encoded}

    fetched = _fetch_production_evidence_policy(req, fetch_json=fake_fetch)
    assert fetched.service == "finance_report/app"
    assert fetched.review_base_ref == "main"


def test_fetch_production_evidence_policy_missing_contract_raises() -> None:
    from infra2_sdk.deploy import _fetch_production_evidence_policy

    req = prod_request()

    def fake_fetch(path: str) -> dict:
        raise ValueError("HTTP 404")

    with pytest.raises(ValueError, match="has no Production evidence contract"):
        _fetch_production_evidence_policy(req, fetch_json=fake_fetch)


def test_fetch_production_evidence_policy_service_mismatch_raises() -> None:
    import base64

    from infra2_sdk.deploy import _fetch_production_evidence_policy

    policy = sample_policy()
    raw = policy.to_dict()
    raw["service"] = "other/service"
    encoded = base64.b64encode(json.dumps(raw).encode("utf-8")).decode("utf-8")
    req = prod_request()

    with pytest.raises(
        ValueError, match="declares service 'other/service', not 'finance_report/app'"
    ):
        _fetch_production_evidence_policy(req, fetch_json=lambda p: {"content": encoded})


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

    assert (
        verify_production_evidence(req, policy=sample_policy(), fetch_json=responses.__getitem__)
        is None
    )

    # Negative mutation: failing staging conclusion must raise ValueError
    failing_staging = dict(staging_run, conclusion="failure")
    bad_responses = dict(
        responses,
        **{"/repos/wangzitian0/finance_report/actions/runs/101": failing_staging},
    )
    with pytest.raises(ValueError, match="staging run must be completed successfully"):
        verify_production_evidence(
            req, policy=sample_policy(), fetch_json=bad_responses.__getitem__
        )


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


# --- poll_until_healthy tests ---


def _health_responses(*pairs: tuple[int, str]):
    it = iter(pairs)

    def http_get(url: str) -> tuple[int, str]:
        return next(it)

    return http_get


def test_succeeds_immediately_on_a_plain_200() -> None:
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_health_responses((200, '{"status": "healthy"}')),
        sleep=lambda _: None,
    )
    assert result == _HealthCheckResult(attempts=1, status_code=200, body='{"status": "healthy"}')


def test_retries_through_connection_failures_then_succeeds() -> None:
    sleeps: list[float] = []
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_health_responses(
            (0, "connection refused"), (0, "timeout"), (200, '{"status": "healthy"}')
        ),
        sleep=sleeps.append,
        max_attempts=5,
        interval_seconds=2.0,
    )
    assert result.attempts == 3
    assert sleeps == [2.0, 2.0]


def test_retries_through_non_200_status_then_succeeds() -> None:
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_health_responses((404, "not found"), (200, '{"status": "healthy"}')),
        sleep=lambda _: None,
        max_attempts=5,
    )
    assert result.attempts == 2


def test_raises_after_exhausting_max_attempts() -> None:
    with pytest.raises(RuntimeError, match="did not become healthy after 2 attempts"):
        poll_until_healthy(
            "https://example.test/api/health",
            http_get=_health_responses((0, "unreachable"), (503, "")),
            sleep=lambda _: None,
            max_attempts=2,
        )


def test_require_status_rejects_a_200_with_the_wrong_status_field() -> None:
    with pytest.raises(RuntimeError, match="did not become healthy"):
        poll_until_healthy(
            "https://example.test/api/health",
            http_get=_health_responses((200, '{"status": "degraded"}')),
            require_status="healthy",
            sleep=lambda _: None,
            max_attempts=1,
        )


def test_require_status_accepts_a_different_apps_own_convention() -> None:
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_health_responses((200, '{"status": "ok"}')),
        require_status="ok",
        sleep=lambda _: None,
    )
    assert result.status_code == 200


def test_require_status_none_accepts_any_200_body() -> None:
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_health_responses((200, "not even json")),
        sleep=lambda _: None,
    )
    assert result.body == "not even json"


def test_expected_version_matches_by_exact_git_sha() -> None:
    body = json.dumps({"status": "healthy", "git_sha": "abc1234"})
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_health_responses((200, body)),
        expected_version="abc1234",
        sleep=lambda _: None,
    )
    assert result.attempts == 1


def test_expected_version_matches_by_short_sha_prefix_either_direction() -> None:
    long_sha = json.dumps(
        {"status": "healthy", "git_sha": "abc1234def5678901234567890123456789abcd"}
    )
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_health_responses((200, long_sha)),
        expected_version="abc1234",
        sleep=lambda _: None,
    )
    assert result.attempts == 1

    short_sha = json.dumps({"status": "healthy", "git_sha": "abc1234"})
    result2 = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_health_responses((200, short_sha)),
        expected_version="abc1234def5678901234567890123456789abcd",
        sleep=lambda _: None,
    )
    assert result2.attempts == 1


def test_expected_version_falls_back_to_the_version_key() -> None:
    body = json.dumps({"status": "healthy", "version": "v1.2.3"})
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_health_responses((200, body)),
        expected_version="v1.2.3",
        sleep=lambda _: None,
    )
    assert result.attempts == 1


def test_version_mismatch_retries_then_succeeds_once_matching() -> None:
    old = json.dumps({"status": "healthy", "git_sha": "old0000"})
    new = json.dumps({"status": "healthy", "git_sha": "new1111"})
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_health_responses((200, old), (200, new)),
        expected_version="new1111",
        sleep=lambda _: None,
        max_attempts=5,
    )
    assert result.attempts == 2


def test_version_mismatch_fails_early_once_the_same_wrong_sha_is_stable() -> None:
    stuck = json.dumps({"status": "healthy", "git_sha": "old0000"})
    with pytest.raises(RuntimeError, match="still reporting version 'old0000'"):
        poll_until_healthy(
            "https://example.test/api/health",
            http_get=_health_responses((200, stuck), (200, stuck), (200, stuck)),
            expected_version="new1111",
            max_version_mismatch_attempts=2,
            sleep=lambda _: None,
            max_attempts=10,
        )


def test_version_mismatch_streak_resets_when_the_reported_version_changes() -> None:
    a = json.dumps({"status": "healthy", "git_sha": "aaaa"})
    b = json.dumps({"status": "healthy", "git_sha": "bbbb"})
    target = json.dumps({"status": "healthy", "git_sha": "cccc"})
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_health_responses((200, a), (200, b), (200, target)),
        expected_version="cccc",
        max_version_mismatch_attempts=2,
        sleep=lambda _: None,
        max_attempts=10,
    )
    assert result.attempts == 3


def test_parse_sender_deadline_accepts_iso_utc_and_rejects_malformed_values() -> None:
    assert parse_sender_deadline({"sender_deadline": "2026-10-23T11:01:31Z"}) == datetime(
        2026, 10, 23, 11, 1, 31, tzinfo=UTC
    )
    assert parse_sender_deadline({"sender_deadline": "2026-10-23T19:01:31+08:00"}) == datetime(
        2026, 10, 23, 11, 1, 31, tzinfo=UTC
    )
    assert parse_sender_deadline({}) is None
    for bad in (
        {"sender_deadline": 123},
        {"sender_deadline": ""},
        {"sender_deadline": "2026-10-23 11:01:31"},
        {"sender_deadline": "invalid"},
    ):
        with pytest.raises(ValueError, match="sender_deadline"):
            parse_sender_deadline(bad)  # type: ignore[arg-type]


def test_production_request_accept_until_matches_the_deadline_and_budget() -> None:
    now = datetime(2026, 10, 23, 11, 1, 31, tzinfo=UTC)
    one_hour = now + timedelta(seconds=3600)
    assert production_request_accept_until(now, one_hour) == one_hour - timedelta(
        seconds=PRODUCTION_DEPLOY_BUDGET_SECONDS
    )
    assert production_request_accept_until(now, None) == now + timedelta(
        seconds=PRODUCTION_REQUEST_MAX_AGE_SECONDS
    )
    with pytest.raises(ValueError, match="creation time must have a time zone"):
        production_request_accept_until(now.replace(tzinfo=None), one_hour)
    with pytest.raises(ValueError, match="sender deadline must have a time zone"):
        production_request_accept_until(now, one_hour.replace(tzinfo=None))


def test_fetch_production_evidence_policy_raises_on_malformed_policy_file() -> None:
    from infra2_sdk.deploy import _fetch_production_evidence_policy

    def fetch_json(url: str):
        if "contents" in url:
            return {"content": base64.b64encode(b"invalid-json{").decode("utf-8")}
        return {}

    with pytest.raises(ValueError, match="not valid JSON"):
        _fetch_production_evidence_policy(prod_request(), fetch_json=fetch_json)


# --- Coverage hardening tests for issue #76 ---


def test_run_evidence_expectation_validation_edge_cases() -> None:
    with pytest.raises(ValueError, match="display_title_template is required"):
        _RunEvidenceExpectation(
            workflow_path=".github/workflows/deploy.yml",
            event="push",
            display_title_template="   ",
        )


def test_production_evidence_policy_validation_edge_cases() -> None:
    with pytest.raises(ValueError, match="review_base_ref is required"):
        ProductionEvidencePolicy(
            service="finance_report/app",
            source=_RunEvidenceExpectation(
                workflow_path=".github/workflows/deploy.yml",
                event="push",
                display_title_template="Deploy {version_ref}",
            ),
            staging=_RunEvidenceExpectation(
                workflow_path=".github/workflows/deploy.yml",
                event="push",
                display_title_template="Deploy {version_ref}",
            ),
            review_base_ref="   ",
        )

    with pytest.raises(ValueError, match="source must be an object"):
        ProductionEvidencePolicy.from_dict(
            {
                "contract_version": 1,
                "service": "finance_report/app",
                "source": "not-an-object",
                "staging": {},
                "review_base_ref": "main",
            }
        )

    with pytest.raises(ValueError, match="staging must be an object"):
        ProductionEvidencePolicy.from_dict(
            {
                "contract_version": 1,
                "service": "finance_report/app",
                "source": {},
                "staging": "not-an-object",
                "review_base_ref": "main",
            }
        )


def test_default_github_json_fetcher(monkeypatch) -> None:
    # 1. Success case
    class FakeResponse:
        def __init__(self, data: bytes):
            self._data = data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return self._data

    monkeypatch.setattr(
        "infra2_sdk.deploy.urlopen",
        lambda req, timeout=10.0: FakeResponse(b'{"key": "value"}'),
    )

    fetcher = _default_github_json_fetcher(token="test-token")
    result = fetcher("/test/path")
    assert result == {"key": "value"}

    # 2. HTTPError
    def raise_http_error(req, timeout=10.0):
        raise HTTPError("url", 404, "Not Found", {}, None)

    monkeypatch.setattr("infra2_sdk.deploy.urlopen", raise_http_error)
    with pytest.raises(ValueError, match="HTTP 404"):
        fetcher("/test/path")

    # 3. URLError
    def raise_url_error(req, timeout=10.0):
        raise URLError("Connection refused")

    monkeypatch.setattr("infra2_sdk.deploy.urlopen", raise_url_error)
    with pytest.raises(ValueError, match="URLError"):
        fetcher("/test/path")

    # 4. JSONDecodeError
    monkeypatch.setattr(
        "infra2_sdk.deploy.urlopen",
        lambda req, timeout=10.0: FakeResponse(b"not-valid-json"),
    )
    with pytest.raises(ValueError, match="JSONDecodeError"):
        fetcher("/test/path")


def test_default_http_get_network_error(monkeypatch) -> None:
    class FakeHttpx:
        HTTPError = httpx.HTTPError

        @staticmethod
        def get(url, timeout=None, follow_redirects=True):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("infra2_sdk.deploy._require_httpx", lambda: FakeHttpx)
    status, body = default_http_get(timeout=1.0)("http://localhost:1234/health")
    assert status == 0
    assert "connection refused" in body


def test_verify_workflow_run_and_reviewed_pull_edge_cases() -> None:
    # _verify_workflow_run mismatched fields
    valid_run = {
        "repository": {"full_name": "wangzitian0/finance_report"},
        "html_url": "https://github.com/wangzitian0/finance_report/actions/runs/1",
        "status": "completed",
        "conclusion": "success",
        "head_sha": SHA,
        "path": ".github/workflows/deploy.yml",
        "event": "push",
        "display_title": "Deploy v1.0.0",
    }

    with pytest.raises(ValueError, match="html_url does not match"):
        _verify_run(
            dict(valid_run, html_url="https://other.com"),
            repository="wangzitian0/finance_report",
            url="https://github.com/wangzitian0/finance_report/actions/runs/1",
            sha=SHA,
            workflow_path=".github/workflows/deploy.yml",
            event="push",
            display_title="Deploy v1.0.0",
            label="source run",
        )

    with pytest.raises(ValueError, match="head_sha does not match"):
        _verify_run(
            dict(valid_run, head_sha="b" * 40),
            repository="wangzitian0/finance_report",
            url="https://github.com/wangzitian0/finance_report/actions/runs/1",
            sha=SHA,
            workflow_path=".github/workflows/deploy.yml",
            event="push",
            display_title="Deploy v1.0.0",
            label="source run",
        )

    with pytest.raises(ValueError, match="workflow is not approved"):
        _verify_run(
            dict(valid_run, path=".github/workflows/other.yml"),
            repository="wangzitian0/finance_report",
            url="https://github.com/wangzitian0/finance_report/actions/runs/1",
            sha=SHA,
            workflow_path=".github/workflows/deploy.yml",
            event="push",
            display_title="Deploy v1.0.0",
            label="source run",
        )

    with pytest.raises(ValueError, match="event does not match"):
        _verify_run(
            dict(valid_run, event="pull_request"),
            repository="wangzitian0/finance_report",
            url="https://github.com/wangzitian0/finance_report/actions/runs/1",
            sha=SHA,
            workflow_path=".github/workflows/deploy.yml",
            event="push",
            display_title="Deploy v1.0.0",
            label="source run",
        )

    with pytest.raises(ValueError, match="title does not match"):
        _verify_run(
            dict(valid_run, display_title="Other title"),
            repository="wangzitian0/finance_report",
            url="https://github.com/wangzitian0/finance_report/actions/runs/1",
            sha=SHA,
            workflow_path=".github/workflows/deploy.yml",
            event="push",
            display_title="Deploy v1.0.0",
            label="source run",
        )

    # _verify_reviewed_pull mismatched fields
    valid_pull = {
        "base": {"repo": {"full_name": "wangzitian0/finance_report"}, "ref": "main"},
        "html_url": "https://github.com/wangzitian0/finance_report/pull/1",
        "state": "closed",
        "merged_at": "2026-09-20T00:00:00Z",
        "merge_commit_sha": SHA,
    }

    with pytest.raises(ValueError, match="html_url does not match"):
        _verify_reviewed_pull(
            dict(valid_pull, html_url="https://other.com"),
            repository="wangzitian0/finance_report",
            url="https://github.com/wangzitian0/finance_report/pull/1",
            sha=SHA,
            base_ref="main",
        )

    with pytest.raises(ValueError, match="must be merged"):
        _verify_reviewed_pull(
            dict(valid_pull, state="open", merged_at=None),
            repository="wangzitian0/finance_report",
            url="https://github.com/wangzitian0/finance_report/pull/1",
            sha=SHA,
            base_ref="main",
        )

    with pytest.raises(ValueError, match="base branch is not approved"):
        _verify_reviewed_pull(
            dict(
                valid_pull,
                base={"repo": {"full_name": "wangzitian0/finance_report"}, "ref": "release"},
            ),
            repository="wangzitian0/finance_report",
            url="https://github.com/wangzitian0/finance_report/pull/1",
            sha=SHA,
            base_ref="main",
        )

    with pytest.raises(ValueError, match="merge_commit_sha does not match"):
        _verify_reviewed_pull(
            dict(valid_pull, merge_commit_sha="b" * 40),
            repository="wangzitian0/finance_report",
            url="https://github.com/wangzitian0/finance_report/pull/1",
            sha=SHA,
            base_ref="main",
        )


def test_version_prefix_matches() -> None:
    from infra2_sdk.deploy import _version_prefix_matches

    assert _version_prefix_matches("", "v1.0") is False
    assert _version_prefix_matches("v1.0.0", "v1.0") is True


def test_validate_evidence_urls_is_public_and_accepts_canonical_urls() -> None:
    from infra2_sdk import deploy
    from infra2_sdk.deploy import validate_evidence_urls

    assert "validate_evidence_urls" in deploy.__all__
    validate_evidence_urls(request())
    validate_evidence_urls(request(), expected_repository="wangzitian0/finance_report")


def test_validate_evidence_urls_rejects_a_foreign_repository_and_a_wrong_resource() -> None:
    from infra2_sdk.deploy import validate_evidence_urls

    with pytest.raises(ValueError, match="expected repository"):
        validate_evidence_urls(request(), expected_repository="evil/repo")
    foreign = request(
        evidence=DeployEvidence(source_run_url="https://github.com/evil/repo/actions/runs/1")
    )
    with pytest.raises(ValueError, match="source_run_url"):
        validate_evidence_urls(foreign)
    wrong_resource = request(
        evidence=DeployEvidence(
            source_run_url="https://github.com/wangzitian0/finance_report/pull/1"
        )
    )
    with pytest.raises(ValueError, match="source_run_url"):
        validate_evidence_urls(wrong_resource)


SENDER_DEADLINE = "2026-10-23T12:01:31Z"
