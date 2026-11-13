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
    ScheduleError,
    check_no_regression,
    defanged_steps,
    largest_gap,
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
    VERSION_KEYS,
    ReleaseIdentityError,
    identity_from_body,
    read_deployed_release,
)
from infra2_sdk.manifests import _render
from infra2_sdk.observability import (
    DELAY_ALLOWANCE,
    MISSED_TICKS_TOLERATED,
    CheckVerdict,
    ConsecutiveObservationState,
    GitHubIssues,
    ListingFailed,
    PeerReadError,
    Trail,
    WriteFailed,
    bound_for,
    check_name,
    evaluate_consecutive_hysteresis,
    github_getter,
    is_recently_flapping,
    issue_trail_mode,
    load_trail,
    newest_tick,
    parse_bound_cap_hours,
    reconcile_issue_trail,
    record_verdicts,
    scrub,
    title_for,
)
from infra2_sdk.refs import (
    _MANIFEST_ACCEPT,
    _SHORT_SHA_LENGTH,
    ReleaseError,
    assert_after_on_main,
    identifier_kind,
    short_sha,
)
from infra2_sdk.runtime.config_schema import assert_environment
from infra2_sdk.secrets import (
    VaultTokenTarget,
    normalize_selector,
    policy_name,
)
from infra2_sdk.stage_record import StageRecord


def test_short_sha_and_manifest_accept() -> None:
    sha = "a" * 40
    assert _SHORT_SHA_LENGTH == 7
    assert short_sha(sha) == "aaaaaaa"
    assert "application/vnd.oci.image.manifest.v1+json" in _MANIFEST_ACCEPT
    with pytest.raises(ValueError, match="full 40-hex commit sha"):
        short_sha("not-a-sha")


def test_assert_after_on_main_success_and_failures(tmp_path: Path) -> None:
    sha = "b" * 40
    timeouts: list[float | None] = []

    def ok_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        timeouts.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(cmd, 0, stdout=sha + "\n", stderr="")

    assert assert_after_on_main(sha, tmp_path, runner=ok_runner) == sha
    assert timeouts == [30, 30]

    def timeout_rev_parse(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    with pytest.raises(ReleaseError, match="git rev-parse failed"):
        assert_after_on_main(sha, tmp_path, runner=timeout_rev_parse)

    def oserror_merge_base(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--is-ancestor" in cmd:
            raise OSError("git vanished")
        return subprocess.CompletedProcess(cmd, 0, stdout=sha + "\n", stderr="")

    with pytest.raises(ReleaseError, match="git merge-base failed"):
        assert_after_on_main(sha, tmp_path, runner=oserror_merge_base)

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
    crons = ["*/30 * * * *"]
    gap = largest_gap(crons)
    assert gap == timedelta(minutes=30)
    bound = bound_for(gap)
    assert bound == timedelta(hours=2)
    assert parse_bound_cap_hours("2") == timedelta(hours=2)

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


def test_reconcile_issue_trail_multi_actor_interleaving() -> None:
    with pytest.raises(ValueError, match="title_prefix must not be empty or whitespace"):
        title_for("check", title_prefix="")
    with pytest.raises(ValueError, match="title_prefix must not be empty or whitespace"):
        title_for("check", title_prefix="   ")
    with pytest.raises(ValueError, match="title_prefix must not be empty or whitespace"):
        check_name("title", title_prefix="")
    with pytest.raises(ValueError, match="title_prefix must not be empty or whitespace"):
        check_name("title", title_prefix="   ")

    class FakeIssueApi:
        def __init__(self) -> None:
            self.issues: list[dict[str, Any]] = []
            self.comments: list[tuple[int, str]] = []
            self.closed: list[int] = []

        def open_issues(self) -> list[dict[str, Any]]:
            return [i for i in self.issues if i["number"] not in self.closed]

        def create(self, title: str, body: str, labels: Sequence[str]) -> int:
            num = len(self.issues) + 1
            self.issues.append(
                {"number": num, "title": title, "body": body, "labels": tuple(labels)}
            )
            return num

        def comment(self, number: int, body: str) -> None:
            self.comments.append((number, body))

        def close(self, number: int) -> None:
            self.closed.append(number)

    api = FakeIssueApi()

    # Step 1: Default watchdog run opens an issue for failing "db"
    watchdog_trail = Trail(
        failing={"db": CheckVerdict(name="db", ok=False, detail="conn timeout", severity="P1")},
        complete=False,
    )
    rc1 = reconcile_issue_trail(api, watchdog_trail, mode="full")
    assert rc1 == 0
    assert len(api.issues) == 1
    assert api.issues[0]["title"] == "ops-checks watchdog is red: db"
    assert api.issues[0]["labels"] == ("incident",)
    assert "**`db` is red** in the ops-checks watchdog" in api.issues[0]["body"]

    # Step 2: Custom app-hook run opens an issue for failing "svc:scheduled_audit:staging"
    hook_prefix = "app-hook is red: "
    hook_labels = ("hook", "verification")
    hook_source = "app runtime hook"
    hook_trail = Trail(
        failing={
            "svc:scheduled_audit:staging": CheckVerdict(
                name="svc:scheduled_audit:staging", ok=False, detail="audit failed"
            )
        },
        complete=False,
    )
    rc2 = reconcile_issue_trail(
        api,
        hook_trail,
        mode="full",
        title_prefix=hook_prefix,
        labels=hook_labels,
        source=hook_source,
    )
    assert rc2 == 0
    assert len(api.issues) == 2
    assert api.issues[1]["title"] == "app-hook is red: svc:scheduled_audit:staging"
    assert api.issues[1]["labels"] == ("hook", "verification")
    expected_hook_red = "**`svc:scheduled_audit:staging` is red** in the app runtime hook"
    assert expected_hook_red in api.issues[1]["body"]
    assert "Recorded by the app runtime hook issue trail" in api.issues[1]["body"]
    assert api.closed == []

    # Step 3: Watchdog runs with complete=True and empty trail.
    # Proves default title_prefix closes retired watchdog issues while leaving app-hook open!
    watchdog_retire_trail = Trail(complete=True)
    rc3 = reconcile_issue_trail(api, watchdog_retire_trail, mode="full")
    assert rc3 == 0
    assert api.closed == [1]  # issue #1 ("ops-checks watchdog is red: db") closed
    assert [i["number"] for i in api.open_issues()] == [2]  # app-hook issue #2 remains OPEN!
    watchdog_close_comment = [c for num, c in api.comments if num == 1][-1]
    assert "**`db` no longer reports to this trail**" in watchdog_close_comment
    assert "Closed by the ops-checks watchdog issue trail" in watchdog_close_comment

    # Step 4: Vice versa: App-hook runs with complete=True and empty trail.
    # Proves custom title_prefix closes retired hook issues!
    hook_retire_trail = Trail(complete=True)
    rc4 = reconcile_issue_trail(
        api,
        hook_retire_trail,
        mode="full",
        title_prefix=hook_prefix,
        labels=hook_labels,
        source=hook_source,
    )
    assert rc4 == 0
    assert api.closed == [1, 2]  # issue #2 is now closed
    assert api.open_issues() == []
    hook_close_comment = [c for num, c in api.comments if num == 2][-1]
    assert "**`svc:scheduled_audit:staging` no longer reports to this trail**" in hook_close_comment
    assert "Closed by the app runtime hook issue trail" in hook_close_comment


_HOOK_PREFIX = "app-hook is red: "
_HOOK_KEY = "svc:smoke:staging"


class _TrailApi:
    """A fake IssueApi: `open_issues` lists the numbers that `close` has not closed."""

    def __init__(self, *titles: str) -> None:
        self.issues = {100 + i: t for i, t in enumerate(titles)}
        self.comments: list[tuple[int, str]] = []
        self.closed: list[int] = []

    def open_issues(self) -> list[dict[str, Any]]:
        return [{"number": n, "title": t} for n, t in self.issues.items() if n not in self.closed]

    def create(self, title: str, body: str, labels: Sequence[str]) -> int:
        num = 100 + len(self.issues)
        self.issues[num] = title
        self.comments.append((num, body))
        return num

    def comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))

    def close(self, number: int) -> None:
        self.closed.append(number)


def _stage_record(
    subject: str, *, ok: bool, ref: str, event: str, key: str = _HOOK_KEY
) -> StageRecord:
    return StageRecord(
        env="staging.release_validation",
        phase="post",
        subject=subject,
        ref=ref,
        event=event,
        run_url=f"https://github.com/o/r/actions/runs/{subject.rpartition('.')[2]}",
        started_at="2026-11-12T03:10:13Z",
        finished_at="2026-11-12T03:11:47Z",
        checks=(CheckVerdict(key, ok, seconds=2.0), CheckVerdict("other", True, seconds=1.0)),
    )


def test_red_body_carries_context() -> None:
    tag, main = "refs/tags/v3.1.4", "refs/heads/main"
    history = [
        _stage_record("tag:v3.1.1", ok=False, ref=main, event="schedule"),
        _stage_record("tag:v3.1.2", ok=True, ref=main, event="schedule"),
        _stage_record("tag:v3.1.3", ok=False, ref=main, event="schedule"),
        _stage_record("tag:v3.1.4", ok=False, ref=tag, event="tag"),
    ]
    red = Trail(failing={_HOOK_KEY: CheckVerdict(_HOOK_KEY, False, "boom", "high", "app")})
    api = _TrailApi()
    rc = reconcile_issue_trail(
        api,
        red,
        mode="full",
        run_url="https://github.com/o/r/actions/runs/99",
        title_prefix=_HOOK_PREFIX,
        record=history[-1],
        history=history,
        runbooks={_HOOK_KEY: "https://example.test/runbook/smoke"},
    )
    assert rc == 0
    lines = api.comments[0][1].splitlines()
    assert "- Run: https://github.com/o/r/actions/runs/99" in lines
    assert "- Env: staging.release_validation" in lines
    assert "- Subject: tag:v3.1.4" in lines
    assert "- Record: https://github.com/o/r/actions/runs/4" in lines  # not the run URL above
    # The newest three verdicts of the key, oldest first: the first record is dropped.
    assert "- Last verdicts: green tag:v3.1.2, red tag:v3.1.3, red tag:v3.1.4" in lines
    assert "- Runbook: https://example.test/runbook/smoke" in lines
    assert "- Detail: boom" in lines

    bare = _TrailApi()
    reconcile_issue_trail(bare, red, mode="full", title_prefix=_HOOK_PREFIX)
    lines = bare.comments[0][1].splitlines()
    for label in ("Env", "Subject", "Record", "Last verdicts", "Runbook"):
        assert f"- {label}: (none)" in lines, label


def test_green_record_closes_issue_for_every_event_type() -> None:
    cases = [  # (record event, record ref, GITHUB_EVENT_NAME of the run)
        ("tag", "refs/tags/v3.1.5", "push"),
        ("schedule", "refs/heads/main", "schedule"),
        ("dispatch", "refs/heads/main", "workflow_dispatch"),
    ]
    for event, ref, github_event in cases:
        record = _stage_record("tag:v3.1.5", ok=True, ref=ref, event=event)
        env = {"GITHUB_EVENT_NAME": github_event, "GITHUB_REF": ref}
        api = _TrailApi(_HOOK_PREFIX + _HOOK_KEY, _HOOK_PREFIX + "other:key")
        rc = reconcile_issue_trail(
            api,
            Trail(green={_HOOK_KEY}),
            mode=issue_trail_mode(env),
            env=env,
            title_prefix=_HOOK_PREFIX,
            record=record,
            history=[record],
        )
        assert rc == 0, event
        assert api.closed == [100], f"a green {event} record must close the issue of its key"
        assert "is green again" in api.comments[0][1], event


def test_feature_branch_record_cannot_close() -> None:
    for ref, event in [
        ("refs/heads/feat/x", "dispatch"),
        ("refs/pull/9/merge", "pull_request"),
        ("main", "dispatch"),
    ]:
        record = _stage_record("sha:" + "a" * 40, ok=True, ref=ref, event=event)
        for trail in (Trail(green={_HOOK_KEY}), Trail(complete=True)):  # green, then retired
            api = _TrailApi(_HOOK_PREFIX + _HOOK_KEY)
            rc = reconcile_issue_trail(
                api, trail, mode="full", title_prefix=_HOOK_PREFIX, record=record
            )
            assert rc == 0
            assert api.closed == [], f"a record of {ref} must not close an issue"
            assert api.comments == [], f"a record of {ref} must not comment on a closed-out issue"


def test_close_false_keeps_issues_open_and_still_opens_and_comments() -> None:
    main_record = _stage_record("tag:v3.1.6", ok=True, ref="refs/heads/main", event="dispatch")
    tag_record = _stage_record("tag:v3.1.6", ok=True, ref="refs/tags/v3.1.6", event="tag")
    # A drill is a dispatch on main: its record passes the provenance check, so only `close`
    # can stop it. Each case is a (trail, record) pair that closes an issue when `close` is True.
    cases = [
        (Trail(green={_HOOK_KEY}), None),
        (Trail(green={_HOOK_KEY}), main_record),
        (Trail(green={_HOOK_KEY}), tag_record),
        (Trail(complete=True), None),
        (Trail(complete=True), main_record),
    ]
    for trail, record in cases:
        kwargs: dict[str, Any] = {"mode": "full", "title_prefix": _HOOK_PREFIX, "record": record}
        closes = _TrailApi(_HOOK_PREFIX + _HOOK_KEY)
        assert reconcile_issue_trail(closes, trail, **kwargs) == 0
        assert closes.closed == [100], "the default close=True must close"
        keeps = _TrailApi(_HOOK_PREFIX + _HOOK_KEY)
        assert reconcile_issue_trail(keeps, trail, close=False, **kwargs) == 0
        assert keeps.closed == [], "close=False must leave the issue open"
        assert keeps.comments == [], "close=False must not post a close comment"

    red = Trail(failing={_HOOK_KEY: CheckVerdict(_HOOK_KEY, False, "boom")})
    api = _TrailApi(_HOOK_PREFIX + "other:key")
    for _ in range(2):  # the first call opens an issue, the second comments on it
        rc = reconcile_issue_trail(
            api, red, mode="full", title_prefix=_HOOK_PREFIX, record=main_record, close=False
        )
        assert rc == 0
    assert list(api.issues.values()) == [
        _HOOK_PREFIX + "other:key",
        _HOOK_PREFIX + _HOOK_KEY,
    ]
    assert [n for n, _ in api.comments] == [101, 101], "red results still open and comment"
    assert api.closed == []


def test_open_only_mode_is_removed() -> None:
    api = _TrailApi(_HOOK_PREFIX + _HOOK_KEY)
    with pytest.raises(ValueError, match="open-only.*removed"):
        reconcile_issue_trail(api, Trail(green={_HOOK_KEY}), mode="open-only")
    assert api.closed == []
    assert reconcile_issue_trail(api, Trail(), mode="unknown") == 1
    # The mode does not read the event name, the ref or the bound cap: only a dry run or an SSH
    # override turns the trail off.
    for env in (
        {},
        {"GITHUB_EVENT_NAME": "push"},
        {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/feat"},
        {"GITHUB_EVENT_NAME": "schedule", "INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS": "2"},
    ):
        assert issue_trail_mode(env) == "full", env
    assert issue_trail_mode({"WATCHDOG_DRY_RUN": "1"}) == "off"
    assert issue_trail_mode({"INFRA2_WATCHDOG_SSH_TARGETS_OVERRIDDEN": "1"}) == "off"


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


def test_v3_8_0_bare_interpreter_import_without_yaml() -> None:
    import sys

    code = (
        "import sys; sys.modules['yaml'] = None; "
        "import infra2_sdk; "
        "from infra2_sdk import ci, deploy, observability, refs, secrets; "
        "assert callable(refs.resolve_to_sha); "
        "assert callable(deploy.poll_until_healthy)"
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
    import infra2_sdk.ci as ci_mod
    from infra2_sdk.ci import (
        CoverageArtifactMissing,
        check_no_regression,
        defanged_steps,
        load_baseline,
        load_workflow,
        parse_github_time,
        read_coverage_summary,
        read_workflow,
        workflow_read_error,
        write_github_output,
    )
    from infra2_sdk.rules.compose import inspect_compose

    assert not hasattr(ci_mod, "inspect_compose")
    assert inspect_compose.__module__ == "infra2_sdk.rules.compose"

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
    from urllib.error import HTTPError, URLError

    from infra2_sdk.ci import ScheduleError, largest_gap
    from infra2_sdk.observability import (
        CheckVerdict,
        ConsecutiveObservationState,
        GitHubIssues,
        ListingFailed,
        PeerReadError,
        Trail,
        WriteFailed,
        evaluate_consecutive_hysteresis,
        github_getter,
        issue_trail_mode,
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
    assert largest_gap(["0 1 * * 1"]) == timedelta(days=7)
    assert largest_gap(["0 0 1 * *"]).days in (31, 32)
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


def test_issue154_function_placement_ssot_invariants() -> None:
    import base64

    import infra2_sdk.ci as ci_mod
    import infra2_sdk.deploy as deploy_mod
    import infra2_sdk.manifests as manifests_mod
    import infra2_sdk.observability as obs_mod
    from infra2_sdk._wire import _is_loopback_host, parse_iso_utc
    from infra2_sdk.ci import (
        ScheduleError as CiScheduleError,
    )
    from infra2_sdk.ci import (
        largest_gap as ci_largest_gap,
    )
    from infra2_sdk.refs import identifier_kind as refs_identifier_kind
    from infra2_sdk.runtime.config_schema import assert_environment as config_assert_environment
    from infra2_sdk.transport import (
        _decode_github_contents,
        _urlopen_json,
    )

    assert config_assert_environment.__module__ == "infra2_sdk.runtime.config_schema"
    assert not hasattr(manifests_mod, "assert_environment")
    assert "assert_environment" not in manifests_mod.__all__

    assert refs_identifier_kind.__module__ == "infra2_sdk.refs"
    assert not hasattr(deploy_mod, "identifier_kind")
    assert "identifier_kind" not in deploy_mod.__all__

    assert not hasattr(ci_mod, "inspect_compose")
    assert "inspect_compose" not in ci_mod.__all__

    assert ci_largest_gap.__module__ == "infra2_sdk.ci"
    assert CiScheduleError.__module__ == "infra2_sdk.ci"
    assert "ScheduleError" not in obs_mod.__all__
    assert "crons_in" not in obs_mod.__all__
    assert "crons_in" not in ci_mod.__all__
    assert "largest_gap" not in obs_mod.__all__

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

    encoded = base64.b64encode(b'{"a":1}').decode("ascii")
    assert _decode_github_contents({"encoding": "base64", "content": encoded}) == b'{"a":1}'
    with pytest.raises(TypeError, match="must contain a JSON object"):
        _decode_github_contents("bad")
    with pytest.raises(KeyError, match="did not return file content"):
        _decode_github_contents({"encoding": "utf-8", "content": encoded}, require_encoding=True)
    assert callable(_urlopen_json)
