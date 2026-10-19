"""Unit tests for infra2-sdk v3.7.0 additions."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from infra2_sdk.ci import (
    BANNED_GATE_MARKERS,
    BANNED_IN_GATE_PATTERNS,
    EPSILON,
    GATE_EXEMPT_RE,
    GATE_WALL_CLOCK_BUDGET_S,
    MAX_SINGLE_TEST_S,
    SHARD_MAX,
    SHARD_MIN,
    SOFT_WARN_S,
    SPEC_VERSION,
    TARGET_PER_SHARD_S,
    CoverageArtifactMissing,
    CoverageBaselineInvalid,
    check_no_regression,
    defanged_steps,
    load_baseline,
    load_workflow,
    parse_github_time,
    read_coverage_summary,
    read_workflow,
    workflow_read_error,
    write_baseline,
    write_github_output,
)
from infra2_sdk.deploy import (
    PRODUCTION_APPROVAL_MARGIN_SECONDS,
    VERSION_KEYS,
    DeployType,
    ReleaseIdentityError,
    Staleness,
    announce_approval_wait,
    check_deploy_freshness,
    format_freshness_failure,
    identifier_kind,
    identity_from_body,
    measure_freshness,
    print_approval_notice,
    read_deployed_release,
    render_sender_request,
    validate_sender_request,
)
from infra2_sdk.manifests import assert_environment, render
from infra2_sdk.observability import (
    DELAY_ALLOWANCE,
    MISSED_TICKS_TOLERATED,
    CheckVerdict,
    ConsecutiveObservationState,
    GitHubIssues,
    ListingFailed,
    PeerReadError,
    PeerVerdict,
    ScheduleError,
    Trail,
    WriteFailed,
    bound_for,
    check_name,
    crons_in,
    evaluate_consecutive_hysteresis,
    evaluate_peer_liveness,
    github_getter,
    is_recently_flapping,
    issue_trail_mode,
    largest_gap,
    load_trail,
    newest_tick,
    parse_bound_cap_hours,
    reconcile_issue_trail,
    record_verdicts,
    scrub,
    title_for,
)
from infra2_sdk.refs import (
    MANIFEST_ACCEPT,
    SHORT_SHA_LENGTH,
    assert_after_on_main,
    short_sha,
)
from infra2_sdk.secrets import (
    VaultTokenTarget,
    normalize_selector,
    policy_name,
)


def test_short_sha_and_manifest_accept() -> None:
    sha = "a" * 40
    assert SHORT_SHA_LENGTH == 7
    assert short_sha(sha) == "aaaaaaa"
    assert "application/vnd.oci.image.manifest.v1+json" in MANIFEST_ACCEPT
    with pytest.raises(ValueError, match="full 40-hex commit sha"):
        short_sha("not-a-sha")


def test_assert_after_on_main_success_and_failures(tmp_path: Path) -> None:
    sha = "b" * 40

    def ok_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=sha + "\n", stderr="")

    assert assert_after_on_main(sha, tmp_path, runner=ok_runner) == sha

    def missing_commit_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="missing")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with pytest.raises(SystemExit, match="cannot resolve"):
        assert_after_on_main(sha, tmp_path, runner=missing_commit_runner)

    def non_ancestor_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--is-ancestor" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=sha + "\n", stderr="")

    with pytest.raises(SystemExit, match="not reachable from"):
        assert_after_on_main(sha, tmp_path, runner=non_ancestor_runner)

    def error_ancestor_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--is-ancestor" in cmd:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: bad ref")
        return subprocess.CompletedProcess(cmd, 0, stdout=sha + "\n", stderr="")

    with pytest.raises(SystemExit, match="base ref 'origin/main' is unresolvable"):
        assert_after_on_main(sha, tmp_path, runner=error_ancestor_runner)


def test_assert_environment_passes_and_fails(tmp_path: Path) -> None:
    from infra2_sdk.runtime.config_schema import (
        EnvironmentField,
        EnvironmentManifest,
        FieldSource,
    )

    manifest = EnvironmentManifest(
        source="demo",
        fields=(
            EnvironmentField(
                field="db_url",
                env="DB_URL",
                source=FieldSource.RUNTIME,
                required=True,
                empty_ok=False,
            ),
        ),
    )
    manifest_file = tmp_path / "env.manifest.yaml"
    manifest_file.write_text(render(manifest), encoding="utf-8")

    assert_environment(
        manifest_file,
        environ={"DB_URL": "postgres://localhost/db"},
        require_injected=False,
    )

    with pytest.raises(RuntimeError, match="environment lacks 1 value"):
        assert_environment(manifest_file, environ={}, require_injected=False)

    with pytest.raises(RuntimeError, match="is not in the image"):
        assert_environment(tmp_path / "missing.yaml", environ={}, require_injected=False)


def test_approval_notice_and_sender_request(capsys: pytest.CaptureFixture[str]) -> None:
    assert PRODUCTION_APPROVAL_MARGIN_SECONDS == 120
    print_approval_notice("waiting for prod approval")
    assert "waiting for prod approval" in capsys.readouterr().err

    notices: list[str] = []

    def fake_api(method: str, path: str, body: object = None) -> object:
        return {
            "workflow_runs": [
                {
                    "display_title": "request-0001",
                    "status": "waiting",
                    "created_at": "2026-10-19T00:00:00Z",
                    "html_url": "https://github.com/org/repo/actions/runs/5",
                }
            ]
        }

    wrapped = announce_approval_wait(fake_api, "request-0001", "v1.2.3", announce=notices.append)
    wrapped("GET", "/runs")
    wrapped("GET", "/runs")
    assert len(notices) == 1
    assert "v1.2.3" in notices[0]

    req = render_sender_request(
        service="finance_report/app",
        source_repository="org/repo",
        repo_label="org/repo",
        request_id="request-0123",
        version_ref="v1.2.3",
        source_sha="a" * 40,
        source_run_url="https://github.com/org/repo/actions/runs/10",
        source_run_id="10",
        deploy_type="staging",
    )
    assert req.service == "finance_report/app"

    prod_req = validate_sender_request(
        {
            "contract_version": 1,
            "request_id": "request-0124",
            "operation": "deploy",
            "service": "finance_report/app",
            "deploy_type": "prod",
            "version_ref": "v1.2.3",
            "source_repository": "org/repo",
            "source_sha": "a" * 40,
            "evidence": {
                "source_run_url": "https://github.com/org/repo/actions/runs/10",
                "source_run_id": "10",
                "staging_run_url": "https://github.com/org/repo/actions/runs/11",
                "reviewed_change_url": "https://github.com/org/repo/pull/5",
            },
        },
        service="finance_report/app",
        source_repository="org/repo",
        repo_label="org/repo",
    )
    assert prod_req.deploy_type == DeployType.PRODUCTION


def test_deployed_release_and_freshness() -> None:
    sha = "c" * 40
    assert VERSION_KEYS == ("git_sha", "version")
    assert identifier_kind(sha) == "commit sha"
    assert identifier_kind("v1.2.3") == "release tag"
    assert identifier_kind("unknown") == "unset"
    assert identifier_kind("branch-ref") == "unrecognised"

    assert identity_from_body(json.dumps({"git_sha": sha})) == sha
    assert identity_from_body(json.dumps({"version": "v2.0.0"})) == "v2.0.0"

    with pytest.raises(ReleaseIdentityError):
        identity_from_body(json.dumps({"status": "ok"}))

    assert (
        read_deployed_release(
            "https://example.com/health",
            lambda url: (200, json.dumps({"git_sha": sha})),
        )
        == sha
    )

    with pytest.raises(ReleaseIdentityError, match="answered HTTP 502"):
        read_deployed_release("https://example.com/bad", lambda url: (502, "bad"))

    def git_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=sha + "\n", stderr="")
        if "log" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    staleness = measure_freshness("staging", sha, run=git_runner)
    assert isinstance(staleness, Staleness)
    assert staleness.unreleased_commits == 0

    stale_obj = Staleness(
        environment="staging",
        deployed_ref=sha[:7],
        unreleased_commits=4,
        oldest_unreleased_age=timedelta(days=5, hours=2),
        oldest_unreleased_subject="feat: add new feature",
    )
    msg = format_freshness_failure(stale_obj, timedelta(days=3))
    assert "staging is stale" in msg

    code = check_deploy_freshness(
        "https://example.com/health",
        environment="staging",
        max_age_days=3,
        http_get=lambda url: (200, json.dumps({"git_sha": sha})),
        run=git_runner,
    )
    assert code == 0


def test_ci_workflow_and_coverage_ratchet(tmp_path: Path) -> None:
    assert SPEC_VERSION == "1.0.0"
    assert GATE_WALL_CLOCK_BUDGET_S == 90
    assert SOFT_WARN_S < GATE_WALL_CLOCK_BUDGET_S
    assert MAX_SINGLE_TEST_S > 0
    assert TARGET_PER_SHARD_S > 0
    assert SHARD_MIN < SHARD_MAX
    assert BANNED_IN_GATE_PATTERNS
    assert BANNED_GATE_MARKERS
    assert GATE_EXEMPT_RE.search("# gate-exempt: reason")

    wf = tmp_path / "gate.yml"
    wf_text = (
        "name: Gate\non: push\njobs:\n  test:\n    steps:\n"
        "      - name: Allowed\n        continue-on-error: true # gate-exempt: ok\n"
        "      - name: Defanged\n        continue-on-error: true\n"
    )
    wf.write_text(wf_text, encoding="utf-8")
    doc, raw = read_workflow(wf)
    assert doc and raw == wf_text
    assert workflow_read_error(wf) is None
    loaded = load_workflow(wf)
    steps = defanged_steps(loaded, "test", raw)
    assert steps == ["Defanged"]

    cov_xml = tmp_path / "coverage.xml"
    cov_xml.write_text(
        '<coverage lines-valid="100" lines-covered="85" line-rate="0.85"></coverage>',
        encoding="utf-8",
    )
    summary = read_coverage_summary(cov_xml)
    assert summary["line_rate"] == 0.85

    with pytest.raises(CoverageArtifactMissing):
        read_coverage_summary(tmp_path / "nonexistent.xml")

    baseline_file = tmp_path / "baseline.json"
    write_baseline(baseline_file, summary)
    loaded_base = load_baseline(baseline_file)
    assert loaded_base is not None and loaded_base["line_rate"] == 0.85
    assert EPSILON > 0

    ok, detail = check_no_regression(summary, loaded_base)
    assert ok and ">=" in detail
    ok_fail, detail_fail = check_no_regression(
        {"lines_valid": 100, "lines_covered": 80, "line_rate": 0.80},
        loaded_base,
    )
    assert not ok_fail and "regressed" in detail_fail

    bad_base = tmp_path / "bad_base.json"
    bad_base.write_text("not-json", encoding="utf-8")
    with pytest.raises(CoverageBaselineInvalid):
        load_baseline(bad_base)

    dt = parse_github_time("2026-10-19T01:02:03Z")
    assert dt == datetime(2026, 10, 19, 1, 2, 3, tzinfo=UTC)

    gh_out = tmp_path / "gh_output.txt"
    write_github_output({"run_id": "123"}, output_path=gh_out)
    assert gh_out.read_text(encoding="utf-8") == "run_id=123\n"


def test_observability_recency_and_hysteresis() -> None:
    now = 1_000_000.0
    assert is_recently_flapping(
        event_count=9,
        last_event_at=now - 30,
        now=now,
        count_threshold=3,
        recency_window_seconds=3600,
    )
    assert not is_recently_flapping(
        event_count=3,
        last_event_at=now,
        now=now,
        count_threshold=3,
        recency_window_seconds=3600,
    )

    state = ConsecutiveObservationState()
    assert (
        evaluate_consecutive_hysteresis(
            state=state,
            is_bad_now=True,
            now=now,
            failure_threshold=2,
            recovery_threshold=2,
            renotify_seconds=600,
        )
        == "none"
    )
    assert (
        evaluate_consecutive_hysteresis(
            state=state,
            is_bad_now=True,
            now=now + 60,
            failure_threshold=2,
            recovery_threshold=2,
            renotify_seconds=600,
        )
        == "fire"
    )
    assert state.active
    assert (
        evaluate_consecutive_hysteresis(
            state=state,
            is_bad_now=False,
            now=now + 120,
            failure_threshold=2,
            recovery_threshold=2,
            renotify_seconds=600,
        )
        == "none"
    )
    assert (
        evaluate_consecutive_hysteresis(
            state=state,
            is_bad_now=False,
            now=now + 180,
            failure_threshold=2,
            recovery_threshold=2,
            renotify_seconds=600,
        )
        == "resolve"
    )
    assert not state.active


def test_observability_peer_liveness() -> None:
    import base64

    assert MISSED_TICKS_TOLERATED == 2
    assert timedelta(hours=1) == DELAY_ALLOWANCE
    wf_yaml = "on:\n  schedule:\n    - cron: '*/30 * * * *'\n"
    crons = crons_in(wf_yaml)
    assert crons == ["*/30 * * * *"]
    gap = largest_gap(crons)
    assert gap == timedelta(minutes=30)
    bound = bound_for(gap)
    assert bound == timedelta(hours=2)
    assert parse_bound_cap_hours("2") == timedelta(hours=2)

    now = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
    runs = [
        {
            "id": 1,
            "event": "schedule",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-10-19T11:45:00Z",
        }
    ]
    tick = newest_tick(runs)
    assert tick == datetime(2026, 10, 19, 11, 45, 0, tzinfo=UTC)

    def fake_get(path: str) -> object:
        if path.endswith("/scheduler-liveness.yml"):
            return {"state": "active", "path": ".github/workflows/scheduler-liveness.yml"}
        if path == "/repos/wangzitian0/truealpha":
            return {"default_branch": "main"}
        if "/contents/" in path:
            return {
                "encoding": "base64",
                "content": base64.b64encode(wf_yaml.encode("utf-8")).decode("ascii"),
            }
        if "/runs" in path:
            return {"workflow_runs": runs}
        raise PeerReadError(f"unexpected {path}")

    verdict = evaluate_peer_liveness(fake_get, now=now)
    assert isinstance(verdict, PeerVerdict)
    assert verdict.ok

    getter = github_getter("fake-token")
    assert callable(getter)
    with pytest.raises(ScheduleError):
        largest_gap([])
    with pytest.raises(PeerReadError):
        newest_tick([{"id": 2, "event": "schedule", "status": "completed", "created_at": "bad"}])


def test_observability_issue_trail(tmp_path: Path) -> None:
    assert title_for("db") == "ops-checks watchdog is red: db"
    assert check_name("ops-checks watchdog is red: db") == "db"
    assert check_name("other title") is None
    assert "***" in scrub("token secret123", {"GITHUB_TOKEN": "secret123"})

    verdicts_file = tmp_path / "verdicts.jsonl"
    record_verdicts(
        verdicts_file,
        source="out-of-band-watchdog",
        checks=[CheckVerdict(name="db", ok=False, detail="connection refused", severity="P1")],
    )
    record_verdicts(
        verdicts_file,
        source="iac-runner-health",
        checks=[CheckVerdict(name="runner", ok=True)],
    )
    trail = load_trail(verdicts_file)
    assert isinstance(trail, Trail)
    assert "db" in trail.failing
    assert trail.is_green("runner")

    class FakeIssueApi:
        def __init__(self) -> None:
            self.issues: list[dict[str, Any]] = []
            self.comments: list[tuple[int, str]] = []
            self.closed: list[int] = []

        def open_issues(self) -> list[dict[str, Any]]:
            return list(self.issues)

        def create(self, title: str, body: str, labels: Sequence[str]) -> int:
            num = len(self.issues) + 1
            self.issues.append({"number": num, "title": title})
            return num

        def comment(self, number: int, body: str) -> None:
            self.comments.append((number, body))

        def close(self, number: int) -> None:
            self.closed.append(number)

    api = FakeIssueApi()
    rc = reconcile_issue_trail(
        api,
        trail,
        mode="full",
        run_url="https://github.com/org/repo/actions/runs/1",
    )
    assert rc == 0
    assert len(api.issues) == 1

    green_trail = Trail(green={"db", "runner"}, complete=True)
    rc2 = reconcile_issue_trail(api, green_trail, mode="full")
    assert rc2 == 0
    assert api.closed == [1]

    assert issue_trail_mode({"GITHUB_EVENT_NAME": "schedule"}) == "full"
    gh_issues = GitHubIssues("org/repo", "tok")
    assert gh_issues is not None
    assert issubclass(ListingFailed, RuntimeError)
    assert issubclass(WriteFailed, RuntimeError)


def test_secrets_vault_token_target_and_policy_name() -> None:
    target = VaultTokenTarget(
        project="apps",
        service="report",
        service_dir="apps/report",
        project_dir="apps",
        dokploy_project="apps",
    )
    assert target.project == "apps"
    assert normalize_selector(None, label="project") is None
    assert normalize_selector("  apps ", label="project") == "apps"
    with pytest.raises(ValueError, match="must not include"):
        normalize_selector("bad-name", label="project")
    assert policy_name("apps", "stg", "report") == "apps-staging-report"
    assert policy_name("apps", "prod", "report") == "apps-production-report"


def test_v3_8_0_resolve_branch_to_sha() -> None:
    from infra2_sdk.refs import resolve_branch_to_sha

    sha = "a" * 40

    def fake_runner(
        args: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        check: bool = True,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, f"{sha}\trefs/heads/fix/branch-test\n", "")

    assert (
        resolve_branch_to_sha(
            "fix/branch-test",
            repo="https://github.com/org/repo.git",
            runner=fake_runner,
        )
        == sha
    )
    for bad in ("", "-flag", ".hidden", "refs/heads/main", "a..b", "a//b", "branch.lock"):
        with pytest.raises(ValueError, match="invalid clone branch"):
            resolve_branch_to_sha(
                bad,
                repo="https://github.com/org/repo.git",
                runner=fake_runner,
            )
    with pytest.raises(ValueError, match="not found"):
        resolve_branch_to_sha(
            "missing/branch",
            repo="https://github.com/org/repo.git",
            runner=fake_runner,
        )


def test_v3_8_0_run_deploy_transport_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    from infra2_sdk.deploy import ReceiverRun, run_deploy_transport_cli

    monkeypatch.delenv("INFRA2_PAT", raising=False)
    assert (
        run_deploy_transport_cli(
            [],
            dispatch=lambda *a, **kw: ReceiverRun(
                run_id=1, url="https://github.com/x/y/actions/runs/1"
            ),
            user_agent="test-agent",
        )
        == 1
    )
    assert "INFRA2_PAT is required" in capsys.readouterr().err

    monkeypatch.setenv("INFRA2_PAT", "tok")
    assert (
        run_deploy_transport_cli(
            ["--timeout", "0"],
            dispatch=lambda *a, **kw: ReceiverRun(
                run_id=1, url="https://github.com/x/y/actions/runs/1"
            ),
            user_agent="test-agent",
        )
        == 1
    )
    assert "must be positive" in capsys.readouterr().err

    assert (
        run_deploy_transport_cli(
            ["--timeout", "5", "--poll-interval", "5"],
            dispatch=lambda *a, **kw: ReceiverRun(
                run_id=1, url="https://github.com/x/y/actions/runs/1"
            ),
            user_agent="test-agent",
            stdin=io.StringIO("[]"),
        )
        == 1
    )
    assert "must be a JSON object" in capsys.readouterr().err

    outputs: dict[str, str] = {}
    assert (
        run_deploy_transport_cli(
            ["--timeout", "6", "--poll-interval", "5"],
            dispatch=lambda raw, **kw: ReceiverRun(
                run_id=101, url="https://github.com/wangzitian0/infra2/actions/runs/101"
            ),
            user_agent="test-agent",
            api_client_factory=lambda **kw: (lambda *a, **k: None, lambda run_id: b""),
            github_output=outputs.update,
            stdin=io.StringIO('{"request_id": "abc"}'),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "receiver_run_id": 101,
        "receiver_run_url": "https://github.com/wangzitian0/infra2/actions/runs/101",
    }
    assert outputs == {
        "receiver_run_id": "101",
        "receiver_run_url": "https://github.com/wangzitian0/infra2/actions/runs/101",
    }


def test_v3_8_0_bare_interpreter_import_without_yaml() -> None:
    import sys

    code = (
        "import sys; sys.modules['yaml'] = None; "
        "import infra2_sdk; "
        "from infra2_sdk import ci, deploy, observability, refs, secrets; "
        "assert callable(refs.resolve_branch_to_sha); "
        "assert callable(deploy.run_deploy_transport_cli)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
