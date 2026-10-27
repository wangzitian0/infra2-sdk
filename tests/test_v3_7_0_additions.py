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
    announce_approval_wait,
    identifier_kind,
    identity_from_body,
    print_approval_notice,
    read_deployed_release,
    render_sender_request,
    validate_sender_request,
)
from infra2_sdk.manifests import _render, assert_environment
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
        _FieldSource,
    )

    manifest = EnvironmentManifest(
        source="demo",
        fields=(
            EnvironmentField(
                field="db_url",
                env="DB_URL",
                source=_FieldSource.RUNTIME,
                required=True,
                empty_ok=False,
            ),
        ),
    )
    manifest_file = tmp_path / "env.manifest.yaml"
    manifest_file.write_text(_render(manifest), encoding="utf-8")

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


def test_openpanel_observability_engine(tmp_path: Path) -> None:
    from infra2_sdk.observability import (
        build_openpanel_payload,
        format_openpanel_markdown,
        load_openpanel_funnel_presets,
        post_openpanel_json,
        resolve_openpanel_api_key,
        resolve_openpanel_api_url,
    )

    assert resolve_openpanel_api_url(None, env={}) == "https://openpanel.zitian.party/api"
    assert (
        resolve_openpanel_api_url(None, env={"OPENPANEL_API_URL": "https://env.example/api"})
        == "https://env.example/api"
    )
    assert (
        resolve_openpanel_api_url("http://localhost:3000/api", env={})
        == "http://localhost:3000/api"
    )
    assert resolve_openpanel_api_url("http://[::1]:3000/api", env={}) == "http://[::1]:3000/api"
    with pytest.raises(SystemExit, match="non-HTTPS"):
        resolve_openpanel_api_url("http://remote.example.com/api", env={})

    assert resolve_openpanel_api_key({"OPENPANEL_API_KEY": "  sec-123  "}) == "sec-123"
    with pytest.raises(SystemExit, match="OPENPANEL_API_KEY is not set"):
        resolve_openpanel_api_key({})

    contract_file = tmp_path / "openpanel_analytics.json"
    contract_file.write_text(
        json.dumps(
            {
                "funnels": [
                    {
                        "name": "Upload -> Report",
                        "description": "Core activation funnel",
                        "steps": ["upload_started", "upload_succeeded", "report_generated"],
                    },
                    {
                        "name": "Upload -> Review -> Report (North Star)",
                        "description": "Full core loop funnel",
                        "steps": [
                            "upload_started",
                            "upload_succeeded",
                            "review_approved",
                            "report_generated",
                        ],
                    },
                    "not-a-mapping",
                    {"name": "invalid", "steps": "not-a-list"},
                    {"name": "too-short", "steps": ["only_one"]},
                    {"preset": "custom", "steps": ["step_a", "step_b"]},
                    {"name": "unrecognized", "steps": ["step_x", "step_y"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    presets = load_openpanel_funnel_presets(contract_file)
    assert presets["activation"] == ("upload_started", "upload_succeeded", "report_generated")
    assert presets["core-funnel"] == (
        "upload_started",
        "upload_succeeded",
        "review_approved",
        "report_generated",
    )
    assert presets["custom"] == ("step_a", "step_b")

    bad_contract = tmp_path / "bad.json"
    bad_contract.write_text(json.dumps({"funnels": "invalid"}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing 'funnels' list"):
        load_openpanel_funnel_presets(bad_contract)

    ev_payload = build_openpanel_payload(
        command="events", limit=25, env="staging", event="upload_started"
    )
    assert ev_payload == {
        "limit": 25,
        "filters": [{"name": "environment", "operator": "is", "value": ["staging"]}],
        "event": "upload_started",
    }
    fn_payload = build_openpanel_payload(command="funnel", limit=10, steps=["s1", "s2"])
    assert fn_payload == {"limit": 10, "steps": ["s1", "s2"]}
    assert build_openpanel_payload(command="noop", limit=5) == {"limit": 5}

    md_funnel = format_openpanel_markdown(
        {
            "steps": [
                {"event": "s1", "count": 10},
                {"event": "s2", "count": 5},
                "ignore-me",
            ]
        },
        command="funnel",
        env="production",
    )
    assert "### OpenPanel Funnel Summary (production)" in md_funnel
    assert "| 2 | `s2` | 5 | 50.0% |" in md_funnel
    assert "(no funnel steps returned)" in format_openpanel_markdown(
        {"steps": []}, command="funnel"
    )

    md_events = format_openpanel_markdown(
        {"events": [{"id": 1}, {"id": 2}]}, command="events", env="staging"
    )
    assert "Total returned: 2" in md_events

    class _FakeResp:
        def __init__(self, body: str) -> None:
            self._body = body

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def read(self) -> bytes:
            return self._body.encode("utf-8")

    seen_req: dict[str, Any] = {}

    def fake_opener(req: Any, timeout: float = 30.0) -> _FakeResp:
        seen_req["url"] = req.full_url
        seen_req["secret"] = req.headers.get("Openpanel-client-secret")
        seen_req["timeout"] = timeout
        return _FakeResp('{"ok": true}')

    res = post_openpanel_json(
        "https://openpanel.zitian.party/api/events",
        "secret-1",
        {"limit": 1},
        timeout=12.0,
        opener=fake_opener,
    )
    assert res == {"ok": True}
    assert seen_req["secret"] == "secret-1"
    assert seen_req["timeout"] == 12.0

    with pytest.raises(ValueError, match="not a JSON object"):
        post_openpanel_json(
            "https://openpanel.zitian.party/api/events",
            "secret-1",
            {"limit": 1},
            opener=lambda req, timeout=30.0: _FakeResp("[1, 2, 3]"),
        )


def test_ci_edge_cases_and_inspect_compose_reexport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from infra2_sdk.ci import (
        CoverageArtifactMissing,
        check_no_regression,
        defanged_steps,
        inspect_compose,
        load_baseline,
        load_workflow,
        parse_github_time,
        read_coverage_summary,
        read_workflow,
        workflow_read_error,
        write_github_output,
    )
    from infra2_sdk.rules.compose import inspect_compose as rules_inspect_compose

    assert inspect_compose is rules_inspect_compose

    missing = tmp_path / "missing.yml"
    assert read_workflow(missing) == ({}, "")
    assert workflow_read_error(missing) is None
    assert load_workflow(missing) == {}

    blank = tmp_path / "blank.yml"
    blank.write_text("   \n", encoding="utf-8")
    assert workflow_read_error(blank) is None

    bad_yaml = tmp_path / "bad.yml"
    bad_yaml.write_text("jobs: [\n", encoding="utf-8")
    assert read_workflow(bad_yaml)[0] == {}
    assert workflow_read_error(bad_yaml) is not None

    list_yaml = tmp_path / "list.yml"
    list_yaml.write_text("- a\n- b\n", encoding="utf-8")
    assert read_workflow(list_yaml)[0] == {}
    assert workflow_read_error(list_yaml) == "top level is list, not a mapping"

    bad_utf8 = tmp_path / "bad_utf8.yml"
    bad_utf8.write_bytes(b"\xff\xfe")
    assert read_workflow(bad_utf8) == ({}, "")
    assert workflow_read_error(bad_utf8) is not None

    wf_src = (
        "name: test\n"
        "jobs:\n"
        "  lint:\n"
        "    continue-on-error: true\n"
        "    steps:\n"
        "      - run: |\n"
        "          echo hi\n"
        "\n"
        "      - name: allowed\n"
        "        continue-on-error: true # gate-exempt: advisory\n"
        "      - name: flagged\n"
        "        continue-on-error: true\n"
    )
    wf_file = tmp_path / "wf.yml"
    wf_file.write_text(wf_src, encoding="utf-8")
    wf_parsed, wf_raw = read_workflow(wf_file)
    assert defanged_steps(wf_parsed, "missing", "") == []
    assert defanged_steps(wf_parsed, "lint", wf_raw) == ["<job>", "flagged"]

    bad_xml = tmp_path / "bad.xml"
    bad_xml.write_text("<coverage lines-valid='bad'/>", encoding="utf-8")
    with pytest.raises(CoverageArtifactMissing):
        read_coverage_summary(bad_xml)

    assert load_baseline(tmp_path / "no_baseline.json") is None
    ok, msg = check_no_regression({"line_rate": 0.9, "lines_covered": 9, "lines_valid": 10}, None)
    assert ok and "no committed baseline yet" in msg

    assert parse_github_time(None) is None
    assert parse_github_time("  ") is None
    assert parse_github_time("2026-10-01T12:00:00").tzinfo is not None

    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    write_github_output({"a": "1"})
    out_file = tmp_path / "gh_out.txt"
    with pytest.raises(ValueError, match="invalid GitHub output key"):
        write_github_output({"bad-key": "1"}, output_path=out_file)
    write_github_output({"multi": "line1\nline2"}, output_path=out_file)
    assert "multi<<FINANCE_REPORT_OUTPUT_" in out_file.read_text(encoding="utf-8")


def test_observability_github_and_trail_branches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import base64
    from urllib.error import HTTPError, URLError

    from infra2_sdk.observability import (
        CheckVerdict,
        ConsecutiveObservationState,
        GitHubIssues,
        ListingFailed,
        PeerReadError,
        ScheduleError,
        Trail,
        WriteFailed,
        evaluate_consecutive_hysteresis,
        evaluate_peer_liveness,
        github_getter,
        issue_trail_mode,
        largest_gap,
        load_trail,
        parse_bound_cap_hours,
        reconcile_issue_trail,
    )

    state = ConsecutiveObservationState(active=True, bad_streak=2, last_alert_at=10.0)
    assert (
        evaluate_consecutive_hysteresis(
            state=state,
            is_bad_now=True,
            now=25.0,
            failure_threshold=2,
            recovery_threshold=2,
            renotify_seconds=10,
        )
        == "fire"
    )
    assert (
        evaluate_consecutive_hysteresis(
            state=state,
            is_bad_now=False,
            now=26.0,
            failure_threshold=2,
            recovery_threshold=2,
            renotify_seconds=10,
        )
        == "none"
    )

    class _Resp:
        def __init__(self, body: str) -> None:
            self._body = body

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def read(self) -> bytes:
            return self._body.encode("utf-8")

    get_ok = github_getter(token="tok", opener=lambda req, timeout=20.0: _Resp('{"ok": 1}'))
    assert get_ok("/repos/x/y") == {"ok": 1}

    def raise_http(req: Any, timeout: float = 20.0) -> Any:
        raise HTTPError(req.full_url, 502, "Bad Gateway", None, None)  # type: ignore[arg-type]

    with pytest.raises(PeerReadError, match="HTTP 502"):
        github_getter(opener=raise_http)("/repos/x/y")

    def raise_url(req: Any, timeout: float = 20.0) -> Any:
        raise URLError("dns failure")

    with pytest.raises(PeerReadError, match="dns failure"):
        github_getter(opener=raise_url)("/repos/x/y")

    with pytest.raises(ScheduleError, match="no cron"):
        largest_gap([])
    with pytest.raises(ScheduleError, match="not 5"):
        largest_gap(["* * * *"])
    with pytest.raises(ScheduleError, match="not daily-periodic"):
        largest_gap(["0 0 1 * *"])
    with pytest.raises(ScheduleError, match="not a number"):
        largest_gap(["bad * * * *"])
    with pytest.raises(ScheduleError, match="at least 1"):
        largest_gap(["*/0 * * * *"])
    with pytest.raises(ScheduleError, match="outside"):
        largest_gap(["70 * * * *"])
    assert largest_gap(["0 */6 * * *", "30 1-2 * * *"]).total_seconds() > 0

    assert parse_bound_cap_hours("") is None
    with pytest.raises(ValueError, match="bound cap"):
        parse_bound_cap_hours("-1")

    now = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    wf_path = "/repos/wangzitian0/truealpha/actions/workflows/scheduler-liveness.yml"
    disabled = evaluate_peer_liveness(lambda p: {"state": "disabled"}, now=now)
    assert disabled.status == "DISABLED" and not disabled.ok

    unverifiable = evaluate_peer_liveness(lambda p: "not-a-dict", now=now)
    assert unverifiable.status == "UNVERIFIABLE"

    def never_ran_getter(path: str) -> object:
        if path == wf_path:
            return {"state": "active", "path": ".github/workflows/s.yml"}
        if path == "/repos/wangzitian0/truealpha":
            return {"default_branch": "main"}
        if "contents/" in path:
            content = base64.b64encode(b"on:\n  schedule:\n    - cron: '0 * * * *'\n").decode()
            return {"encoding": "base64", "content": content}
        if "/runs" in path:
            return {"workflow_runs": []}
        if "/commits?" in path:
            return [{"commit": {"committer": {"date": "2026-10-20T00:00:00Z"}}}]
        return {}

    assert evaluate_peer_liveness(never_ran_getter, now=now).status == "NEVER"

    trail_file = tmp_path / "trail.jsonl"
    trail_file.write_text(
        "\nnot-json\n"
        + json.dumps({"source": "", "checks": []})
        + "\n"
        + json.dumps(
            {
                "source": "out-of-band-watchdog",
                "checks": [{"name": "c1", "ok": True}],
                "complete": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    loaded = load_trail(trail_file)
    assert "iac-runner-health" in loaded.failing

    assert issue_trail_mode({"WATCHDOG_DRY_RUN": "1"}) == "off"
    assert issue_trail_mode({"GITHUB_EVENT_NAME": "push"}) == "off"
    assert (
        issue_trail_mode(
            {"GITHUB_EVENT_NAME": "schedule", "INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS": "2"}
        )
        == "open-only"
    )
    assert (
        issue_trail_mode(
            {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/feat"}
        )
        == "open-only"
    )

    calls: list[tuple[str, str]] = []

    def gh_opener(req: Any, timeout: float = 20.0) -> _Resp:
        calls.append((req.get_method(), req.full_url))
        if req.get_method() == "GET":
            return _Resp(
                json.dumps(
                    [
                        {
                            "number": 1,
                            "title": "ops-checks watchdog is red: c1",
                            "pull_request": {},
                        },
                        {
                            "number": 2,
                            "title": "ops-checks watchdog is red: c2",
                            "body": "b",
                            "user": {"login": "bot"},
                        },
                    ]
                )
            )
        return _Resp(json.dumps({"number": 99}))

    gh = GitHubIssues("wangzitian0/infra2", "tok", opener=gh_opener)
    listed = gh.open_issues(with_body=True)
    assert listed == [
        {"number": 2, "title": "ops-checks watchdog is red: c2", "body": "b", "author": "bot"}
    ]
    assert gh.create("t", "b", ["incident"]) == 99
    gh.comment(2, "c")
    gh.update_body(2, "u")
    gh.close(2)

    bad_gh = GitHubIssues("wangzitian0/infra2", "tok", opener=raise_url)
    with pytest.raises(ListingFailed):
        bad_gh.open_issues()
    with pytest.raises(WriteFailed):
        bad_gh.create("t", "b", [])

    assert reconcile_issue_trail(gh, Trail(), mode="invalid") == 1
    assert reconcile_issue_trail(gh, Trail(), mode="off") == 0
    assert reconcile_issue_trail(bad_gh, Trail(), mode="full") == 0

    trail_run = Trail(
        failing={"c2": CheckVerdict(name="c2", ok=False, detail="down", severity="P1")},
        green={"c_green"},
        complete=True,
    )

    class _FakeIssueApi:
        def open_issues(self) -> list[dict]:
            return [
                {"number": 2, "title": "ops-checks watchdog is red: c2"},
                {"number": 3, "title": "ops-checks watchdog is red: c_green"},
                {"number": 4, "title": "ops-checks watchdog is red: c_retired"},
                {"number": 5, "title": "unrelated issue"},
            ]

        def create(self, title: str, body: str, labels: Any) -> int:
            return 10

        def comment(self, number: int, body: str) -> None:
            pass

        def close(self, number: int) -> None:
            pass

    assert reconcile_issue_trail(_FakeIssueApi(), trail_run, mode="full", run_url="https://x") == 0
    assert reconcile_issue_trail(_FakeIssueApi(), trail_run, mode="open-only") == 0


def test_issue154_function_placement_ssot_invariants() -> None:
    import base64

    from infra2_sdk._wire import _is_loopback_host, parse_iso_utc
    from infra2_sdk.ci import (
        ScheduleError as CiScheduleError,
    )
    from infra2_sdk.ci import (
        crons_in as ci_crons_in,
    )
    from infra2_sdk.ci import (
        largest_gap as ci_largest_gap,
    )
    from infra2_sdk.deploy import identifier_kind as deploy_identifier_kind
    from infra2_sdk.manifests import assert_environment as manifests_assert_environment
    from infra2_sdk.observability import (
        ScheduleError as ObsScheduleError,
    )
    from infra2_sdk.observability import (
        crons_in as obs_crons_in,
    )
    from infra2_sdk.observability import (
        largest_gap as obs_largest_gap,
    )
    from infra2_sdk.refs import identifier_kind as refs_identifier_kind
    from infra2_sdk.runtime.config_schema import assert_environment as config_assert_environment
    from infra2_sdk.transport import (
        _decode_github_contents,
        _extract_workflow_runs,
        _urlopen_json,
    )

    assert config_assert_environment.__module__ == "infra2_sdk.runtime.config_schema"
    assert manifests_assert_environment is config_assert_environment

    assert refs_identifier_kind.__module__ == "infra2_sdk.refs"
    assert deploy_identifier_kind is refs_identifier_kind

    assert ci_crons_in.__module__ == "infra2_sdk.ci"
    assert ci_largest_gap.__module__ == "infra2_sdk.ci"
    assert CiScheduleError.__module__ == "infra2_sdk.ci"
    assert obs_crons_in is ci_crons_in
    assert obs_largest_gap is ci_largest_gap
    assert ObsScheduleError is CiScheduleError

    assert parse_iso_utc("2026-10-27T01:02:03Z") == datetime(2026, 10, 27, 1, 2, 3, tzinfo=UTC)
    assert parse_iso_utc("2026-10-27T01:02:03", allow_naive=True) == datetime(
        2026, 10, 27, 1, 2, 3, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="expected non-empty ISO 8601 string"):
        parse_iso_utc("")
    with pytest.raises(ValueError, match="timestamp must have a time zone"):
        parse_iso_utc("2026-10-27T01:02:03")

    assert _is_loopback_host("localhost")
    assert _is_loopback_host("[::1]")
    assert not _is_loopback_host("example.com")

    assert _extract_workflow_runs({"workflow_runs": [{"id": 1}]}) == [{"id": 1}]
    with pytest.raises(TypeError, match="must be an object"):
        _extract_workflow_runs([])
    with pytest.raises(ValueError, match="must contain a run list"):
        _extract_workflow_runs({"workflow_runs": "bad"})

    encoded = base64.b64encode(b'{"a":1}').decode("ascii")
    assert _decode_github_contents({"encoding": "base64", "content": encoded}) == b'{"a":1}'
    with pytest.raises(TypeError, match="must contain a JSON object"):
        _decode_github_contents("bad")
    with pytest.raises(KeyError, match="did not return file content"):
        _decode_github_contents({"encoding": "utf-8", "content": encoded}, require_encoding=True)
    assert callable(_urlopen_json)
