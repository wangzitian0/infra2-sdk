"""Tests for infra2_sdk.dispatch — ported from finance_report's own
tools/tests/test_app_deploy_request.py coverage of its (now-retired) local
dispatch_and_wait, adapted to the sdk's DeployRequest-object signature."""

from __future__ import annotations

import pytest

from infra2_sdk.deploy import DeployEvidence, DeployOperation, DeployRequest, DeployType
from infra2_sdk.dispatch import INFRA_REPOSITORY, dispatch_and_wait

SHA = "a" * 40


def _request() -> DeployRequest:
    return DeployRequest(
        request_id="finance-report-run-12345678",
        operation=DeployOperation.DEPLOY,
        service="finance_report/app",
        deploy_type=DeployType.STAGING,
        version_ref="v2.3.4",
        source_repository="wangzitian0/finance_report",
        source_sha=SHA,
        evidence=DeployEvidence(
            source_run_url="https://github.com/wangzitian0/finance_report/actions/runs/12345678",
            source_run_id="12345678",
        ),
    )


def test_dispatch_and_wait_correlates_the_watermarked_run_and_verifies_logs() -> None:
    request = _request()
    calls: list[tuple[str, str, object]] = []
    run_lists = iter(
        [
            {"workflow_runs": [{"id": 100}]},
            {
                "workflow_runs": [
                    {
                        "id": 101,
                        "status": "in_progress",
                        "conclusion": None,
                        "html_url": f"https://github.com/{INFRA_REPOSITORY}/actions/runs/101",
                    },
                    {"id": 100},
                ]
            },
            {
                "workflow_runs": [
                    {
                        "id": 101,
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": f"https://github.com/{INFRA_REPOSITORY}/actions/runs/101",
                    },
                    {"id": 100},
                ]
            },
        ]
    )

    def api(method: str, path: str, body: object = None) -> object:
        calls.append((method, path, body))
        if method == "GET":
            return next(run_lists)
        return None

    result = dispatch_and_wait(
        request,
        api=api,
        fetch_logs=lambda run_id: b'plan {"request_id": "finance-report-run-12345678"}',
        sleep=lambda _: None,
        max_attempts=3,
    )

    assert result.run_id == 101
    assert result.url.endswith("/101")
    dispatch = next(call for call in calls if call[0] == "POST")
    assert dispatch[2] == {
        "event_type": "app-deploy-request",
        "client_payload": request.to_dict(),
    }


def test_dispatch_and_wait_raises_when_more_than_one_run_appears_after_watermark() -> None:
    ambiguous_runs = {
        "workflow_runs": [
            {"id": 103, "status": "queued"},
            {"id": 102, "status": "queued"},
            {"id": 100, "status": "completed"},
        ]
    }
    responses = iter([{"workflow_runs": [{"id": 100}]}, ambiguous_runs])
    with pytest.raises(RuntimeError, match="ambiguous"):
        dispatch_and_wait(
            _request(),
            api=lambda method, path, body=None: (next(responses) if method == "GET" else None),
            fetch_logs=lambda run_id: b"",
            sleep=lambda _: None,
            max_attempts=1,
        )


def test_dispatch_and_wait_raises_when_logs_do_not_contain_the_request_id() -> None:
    responses = iter(
        [
            {"workflow_runs": [{"id": 100}]},
            {
                "workflow_runs": [
                    {
                        "id": 101,
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": f"https://github.com/{INFRA_REPOSITORY}/actions/runs/101",
                    }
                ]
            },
        ]
    )
    with pytest.raises(RuntimeError, match="request_id"):
        dispatch_and_wait(
            _request(),
            api=lambda method, path, body=None: (next(responses) if method == "GET" else None),
            fetch_logs=lambda run_id: b"another request",
            sleep=lambda _: None,
            max_attempts=1,
        )


def test_dispatch_and_wait_times_out_when_no_run_ever_appears() -> None:
    responses = iter(
        [
            {"workflow_runs": [{"id": 100}]},
            {"workflow_runs": [{"id": 100}]},
            {"workflow_runs": [{"id": 100}]},
        ]
    )
    sleeps: list[float] = []
    with pytest.raises(RuntimeError, match="timed out"):
        dispatch_and_wait(
            _request(),
            api=lambda method, path, body=None: (next(responses) if method == "GET" else None),
            fetch_logs=lambda run_id: b"",
            sleep=sleeps.append,
            poll_interval=0.25,
            max_attempts=2,
        )
    assert sleeps == [0.25]


def _run_with_outcome(conclusion: str, url: object):
    responses = iter(
        [
            {"workflow_runs": [{"id": 100}]},
            {
                "workflow_runs": [
                    {
                        "id": 101,
                        "status": "completed",
                        "conclusion": conclusion,
                        "html_url": url,
                    }
                ]
            },
        ]
    )
    request = _request()
    return dispatch_and_wait(
        request,
        api=lambda method, path, body=None: (next(responses) if method == "GET" else None),
        fetch_logs=lambda run_id: request.request_id.encode(),
        sleep=lambda _: None,
        max_attempts=1,
    )


def test_dispatch_and_wait_raises_on_a_non_success_conclusion() -> None:
    with pytest.raises(RuntimeError, match="concluded 'failure'"):
        _run_with_outcome("failure", f"https://github.com/{INFRA_REPOSITORY}/actions/runs/101")


def test_dispatch_and_wait_raises_on_a_non_canonical_url() -> None:
    with pytest.raises(RuntimeError, match="has no canonical URL"):
        _run_with_outcome("success", "https://example.com/actions/runs/101")


def test_workflow_runs_rejects_malformed_payloads() -> None:
    from infra2_sdk.dispatch import _workflow_runs

    for payload in ([], {}, {"workflow_runs": ["not-a-run"]}):
        with pytest.raises(RuntimeError, match="workflow-runs response"):
            _workflow_runs(payload)


def test_run_id_rejects_non_positive_integers() -> None:
    from infra2_sdk.dispatch import _run_id

    for run_id in (True, 0, "101"):
        with pytest.raises(RuntimeError, match="positive integer"):
            _run_id({"id": run_id})
