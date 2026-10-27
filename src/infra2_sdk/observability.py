"""Observability primitives: hysteresis, peer liveness, issue trails, and OpenPanel queries."""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from infra2_sdk.transport import _github_api_url, _github_headers

# ---------------------------------------------------------------------------
# Recency & consecutive-observation hysteresis (#531, #475)
# ---------------------------------------------------------------------------


def is_recently_flapping(
    *,
    event_count: int,
    last_event_at: float,
    now: float,
    count_threshold: int,
    recency_window_seconds: int,
) -> bool:
    """Return True when a bad signal exceeds *count_threshold* within *recency_window_seconds*."""
    if event_count <= count_threshold:
        return False
    age_seconds = max(0.0, now - last_event_at)
    return age_seconds <= recency_window_seconds


@dataclass
class ConsecutiveObservationState:
    """Persisted per-key hysteresis state for a repeatedly-polled bad/healthy signal."""

    active: bool = False
    bad_streak: int = 0
    good_streak: int = 0
    last_alert_at: float = 0.0
    context: object = field(default=None)


def evaluate_consecutive_hysteresis(
    *,
    state: ConsecutiveObservationState,
    is_bad_now: bool,
    now: float,
    failure_threshold: int,
    recovery_threshold: int,
    renotify_seconds: int,
) -> str:
    """Advance *state* by one poll and return ``"fire"``, ``"resolve"``, or ``"none"``."""
    if is_bad_now:
        state.good_streak = 0
        state.bad_streak += 1
        if not state.active:
            if state.bad_streak < max(1, failure_threshold):
                return "none"
            state.active = True
            state.last_alert_at = now
            return "fire"
        if renotify_seconds > 0 and now - state.last_alert_at >= renotify_seconds:
            state.last_alert_at = now
            return "fire"
        return "none"

    state.bad_streak = 0
    if not state.active:
        return "none"
    state.good_streak += 1
    if state.good_streak < max(1, recovery_threshold):
        return "none"
    state.active = False
    state.good_streak = 0
    state.last_alert_at = 0.0
    return "resolve"


# ---------------------------------------------------------------------------
# Scheduler peer liveness (truealpha#876)
# ---------------------------------------------------------------------------

_PEER_REPOSITORY = "wangzitian0/truealpha"
_PEER_WORKFLOW = "scheduler-liveness.yml"
_BOUND_CAP_ENV = "_".join(("INFRA2", "PEER", "LIVENESS", "BOUND", "CAP", "HOURS"))
MISSED_TICKS_TOLERATED = 2
DELAY_ALLOWANCE = timedelta(hours=1)
_SCHEDULED_PAGE = 5
_UNFILTERED_PAGE = 50
_STARTED = ("in_progress", "completed")
_STARTUP_FAILURE = "startup_failure"
_MINUTES_PER_DAY = 24 * 60
_API_TIMEOUT_SECONDS = 20.0

_OK = "OK"
_STALE = "STALE"
_DISABLED = "DISABLED"
_NEVER = "NEVER"
_INVALID = "INVALID"
_UNVERIFIABLE = "UNVERIFIABLE"

_Get = Callable[[str], object]


class PeerReadError(RuntimeError):
    """A read the verdict depends on did not answer."""


class ScheduleError(ValueError):
    """The workflow's schedule is not one this module can measure."""


@dataclass(frozen=True)
class PeerVerdict:
    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status == _OK


def _urlopen_json(
    url: str,
    *,
    headers: Mapping[str, str],
    method: str = "GET",
    payload: Mapping[str, object] | None = None,
    timeout: float = _API_TIMEOUT_SECONDS,
    opener: Callable[..., object] = urlopen,
) -> object:
    request = Request(  # noqa: S310
        url,
        headers=dict(headers),
        data=None if payload is None else json.dumps(dict(payload)).encode("utf-8"),
        method=method,
    )
    with opener(request, timeout=timeout) as response:  # type: ignore[attr-defined]
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else None


def github_getter(
    token: str = "", *, timeout: float = _API_TIMEOUT_SECONDS, opener=urlopen
) -> _Get:
    """Return a reader for the public GitHub REST API; the token is optional."""
    headers = _github_headers(
        token=token,
        user_agent="infra2-scheduler-peer-liveness/1.0",
    )

    def get(path: str) -> object:
        try:
            return _urlopen_json(
                _github_api_url(path), headers=headers, timeout=timeout, opener=opener
            )
        except HTTPError as exc:
            raise PeerReadError(f"GET {path} answered HTTP {exc.code}") from exc
        except (OSError, URLError, ValueError) as exc:
            raise PeerReadError(f"GET {path} failed: {exc}") from exc

    return get


_CRON_LINE = re.compile(
    r"""^[ \t]*-?[ \t]*cron:[ \t]*(?P<quote>["']?)(?P<expr>[^"'#\n]+?)(?P=quote)[ \t]*(?:#.*)?$""",
    re.MULTILINE,
)
_ELEMENT = re.compile(r"^(?P<span>\*|\d+(?:-\d+)?)(?:/(?P<step>\d+))?$")


def crons_in(workflow_text: str) -> list[str]:
    """Return the ``cron:`` values in a workflow file, in order."""
    return [match.group("expr").strip() for match in _CRON_LINE.finditer(workflow_text)]


def _field(text: str, name: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for element in text.split(","):
        match = _ELEMENT.match(element)
        if not match:
            raise ScheduleError(f"{name} {element!r} is not a number, range or step")
        span, step_text = match.group("span"), match.group("step")
        step = int(step_text) if step_text is not None else 1
        if step < 1:
            raise ScheduleError(f"{name} step in {element!r} must be at least 1")
        if span == "*":
            first, last = low, high
        elif "-" in span:
            first, last = (int(part) for part in span.split("-", 1))
        else:
            first = int(span)
            last = high if step_text is not None else first
        if not low <= first <= last <= high:
            raise ScheduleError(f"{name} {element!r} is outside {low}-{high}")
        values.update(range(first, last + 1, step))
    return values


def largest_gap(crons: Iterable[str]) -> timedelta:
    """Return the largest gap between consecutive fire times of daily-periodic crons."""
    fires: set[int] = set()
    listed = list(crons)
    if not listed:
        raise ScheduleError("the workflow has no cron")
    for expression in listed:
        parts = expression.split()
        if len(parts) != 5:
            raise ScheduleError(f"cron {expression!r} has {len(parts)} fields, not 5")
        minute, hour, day, month, weekday = parts
        if (day, month, weekday) != ("*", "*", "*"):
            raise ScheduleError(
                f"cron {expression!r} is not daily-periodic; teach "
                "infra2_sdk.observability.largest_gap to measure it"
            )
        minutes = _field(minute, "minute", 0, 59)
        hours = _field(hour, "hour", 0, 23)
        fires.update(h * 60 + m for h in hours for m in minutes)
    ordered = sorted(fires)
    gaps = [later - earlier for earlier, later in zip(ordered, ordered[1:], strict=False)]
    gaps.append(ordered[0] + _MINUTES_PER_DAY - ordered[-1])
    return timedelta(minutes=max(gaps))


def bound_for(gap: timedelta) -> timedelta:
    """Return the silence bound for a given maximum schedule *gap*."""
    return MISSED_TICKS_TOLERATED * gap + DELAY_ALLOWANCE


def parse_bound_cap_hours(raw: str | None) -> timedelta | None:
    """Parse an optional cap in hours; empty means none. Raises ValueError otherwise."""
    text = (raw or "").strip()
    if not text:
        return None
    hours = float(text)
    if not math.isfinite(hours) or hours < 0:
        raise ValueError(f"bound cap {text!r} must be a finite number of hours >= 0")
    return timedelta(hours=hours)


def _object(get: _Get, path: str) -> dict:
    body = get(path)
    if not isinstance(body, dict):
        raise PeerReadError(f"GET {path} returned {type(body).__name__}, not an object")
    return body


def _timestamp(value: object, what: str) -> datetime:
    if not isinstance(value, str):
        raise PeerReadError(f"{what} has no timestamp ({value!r})")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PeerReadError(f"{what} has an unreadable timestamp {value!r}") from exc
    if moment.tzinfo is None:
        raise PeerReadError(f"{what} has a timestamp without a zone ({value!r})")
    return moment.astimezone(UTC)


def _runs(get: _Get, path: str) -> list[dict]:
    body = _object(get, path)
    runs = body.get("workflow_runs")
    if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
        raise PeerReadError(f"GET {path} has no usable workflow_runs list")
    return runs


def newest_tick(runs: Iterable[dict]) -> datetime | None:
    """Return the newest scheduled run that started a job, by ``created_at``."""
    ticks = [
        _timestamp(run.get("created_at"), f"run {run.get('id')}")
        for run in runs
        if run.get("event") == "schedule"
        and run.get("status") in _STARTED
        and run.get("conclusion") != _STARTUP_FAILURE
    ]
    return max(ticks) if ticks else None


def _file_text(get: _Get, path: str, branch: str) -> str:
    body = _object(
        get,
        f"/repos/{_PEER_REPOSITORY}/contents/{quote(path)}?ref={quote(branch, safe='')}",
    )
    if body.get("encoding") != "base64" or not isinstance(body.get("content"), str):
        raise PeerReadError(f"{path} came back without base64 content")
    try:
        return base64.b64decode(body["content"]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise PeerReadError(f"{path} content does not decode: {exc}") from exc


def _last_changed(get: _Get, path: str, branch: str) -> datetime:
    commits = get(
        f"/repos/{_PEER_REPOSITORY}/commits?path={quote(path, safe='')}"
        f"&sha={quote(branch, safe='')}&per_page=1"
    )
    if not isinstance(commits, list) or not commits or not isinstance(commits[0], dict):
        raise PeerReadError(f"no commit on {branch} touches {path}")
    committer = (commits[0].get("commit") or {}).get("committer") or {}
    return _timestamp(committer.get("date"), f"the last commit to {path}")


def _hours(span: timedelta) -> str:
    return f"{span.total_seconds() / 3600:.1f}h"


def evaluate_peer_liveness(
    get: _Get, *, now: datetime, bound_cap: timedelta | None = None
) -> PeerVerdict:
    """Evaluate the peer liveness verdict on truealpha's ``scheduler-liveness`` workflow."""
    workflow_path = f"/repos/{_PEER_REPOSITORY}/actions/workflows/{_PEER_WORKFLOW}"
    try:
        workflow = _object(get, workflow_path)
        state = workflow.get("state")
        if state != "active":
            return PeerVerdict(
                _DISABLED,
                f"{_PEER_REPOSITORY} {_PEER_WORKFLOW} state={state!r}, not 'active': "
                "its scheduler does not run",
            )
        path = workflow.get("path")
        if not isinstance(path, str) or not path:
            raise PeerReadError(f"GET {workflow_path} has no path")
        branch = _object(get, f"/repos/{_PEER_REPOSITORY}").get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise PeerReadError(f"{_PEER_REPOSITORY} has no default_branch")
        try:
            bound = bound_for(largest_gap(crons_in(_file_text(get, path, branch))))
        except ScheduleError as exc:
            return PeerVerdict(_INVALID, f"{_PEER_REPOSITORY} {path}: {exc}")
        if bound_cap is not None:
            bound = min(bound, bound_cap)

        newest = newest_tick(
            _runs(get, f"{workflow_path}/runs?event=schedule&per_page={_SCHEDULED_PAGE}")
        )
        if newest is None or now - newest > bound:
            witness = newest_tick(_runs(get, f"{workflow_path}/runs?per_page={_UNFILTERED_PAGE}"))
            if witness is not None and (newest is None or witness > newest):
                newest = witness
        if newest is not None:
            age = now - newest
            where = (
                f"{_PEER_REPOSITORY} {_PEER_WORKFLOW}: newest scheduled run {newest.isoformat()}"
            )
            if age > bound:
                return PeerVerdict(
                    _STALE,
                    f"{where} is {_hours(age)} old, beyond the {_hours(bound)} bound: "
                    "its scheduler stopped ticking",
                )
            return PeerVerdict(_OK, f"{where} is {_hours(age)} old (bound {_hours(bound)})")

        changed = _last_changed(get, path, branch)
        since = now - changed
        where = (
            f"{_PEER_REPOSITORY} {_PEER_WORKFLOW} never ran on schedule; "
            f"{path} changed {_hours(since)} ago"
        )
        if since > bound:
            return PeerVerdict(_NEVER, f"{where}, beyond the {_hours(bound)} bound")
        return PeerVerdict(_OK, f"{where}, waiting for its first tick (bound {_hours(bound)})")
    except PeerReadError as exc:
        return PeerVerdict(
            _UNVERIFIABLE, f"cannot verify {_PEER_REPOSITORY} {_PEER_WORKFLOW}: {exc}"
        )


# ---------------------------------------------------------------------------
# GitHub Issue Trail (truealpha#876 W4)
# ---------------------------------------------------------------------------

_TITLE_PREFIX = "ops-checks watchdog is red: "
_ISSUE_LABELS = ("incident",)
_SSH_OVERRIDE_ENV = "_".join(("INFRA2", "WATCHDOG", "SSH", "TARGETS", "OVERRIDDEN"))
_DRY_RUN_ENV = "_".join(("WATCHDOG", "DRY", "RUN"))
_GH_EVENT_NAME_KEY = "_".join(("GITHUB", "EVENT", "NAME"))
_GH_REF_KEY = "_".join(("GITHUB", "REF"))

_WATCHDOG_SOURCE = "out-of-band-watchdog"
_RUNNER_HEALTH_SOURCE = "iac-runner-health"
_EXPECTED_SOURCES = (_WATCHDOG_SOURCE, _RUNNER_HEALTH_SOURCE)

_FULL = "full"
_OPEN_ONLY = "open-only"
_OFF = "off"

_PER_PAGE = 100
_MAX_PAGES = 10
_MAX_DETAIL_CHARS = 1000
_SECRET_ENV_NAME = re.compile(
    r"^(?:INFRA2_|DOKPLOY_|CF_|FEISHU_|OP_|VAULT_)\w*"
    r"(?:HOST|USER|TOKEN|KEY|SECRET|PASSWORD|WEBHOOK|CHAT_ID|APP_ID)"
    r"|^INFRA2_OUT_OF_BAND_\w+"
    r"|^(?:GITHUB_TOKEN|GH_TOKEN)$"
)
_MIN_SCRUBBED_VALUE = 4
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_IPV6 = re.compile(
    r"(?<![\w:])(?:(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}"
    r"|[0-9A-Fa-f:]*::[0-9A-Fa-f:]*[0-9A-Fa-f])(?![\w:])"
)


@dataclass(frozen=True)
class CheckVerdict:
    name: str
    ok: bool
    detail: str = ""
    severity: str = ""
    failure_domain: str = ""


@dataclass
class Trail:
    """What one watchdog run decided, merged over every recording step."""

    failing: dict[str, CheckVerdict] = field(default_factory=dict)
    green: set[str] = field(default_factory=set)
    complete: bool = False

    def is_green(self, name: str) -> bool:
        return name not in self.failing and name in self.green

    def is_retired(self, name: str) -> bool:
        return self.complete and name not in self.failing and name not in self.green

    def renamed(self, rename) -> Trail:
        """Return the same verdicts under ``rename(name)`` (titles carry scrubbed names)."""
        return Trail(
            failing={rename(name): verdict for name, verdict in self.failing.items()},
            green={rename(name) for name in self.green},
            complete=self.complete,
        )


def title_for(name: str) -> str:
    return f"{_TITLE_PREFIX}{name}"


def check_name(title: str) -> str | None:
    """Return the check name a trail title names, or ``None`` for any other issue."""
    if not title.startswith(_TITLE_PREFIX):
        return None
    return title[len(_TITLE_PREFIX) :] or None


def scrub(text: str, env: Mapping[str, str]) -> str:
    """Return text safe for a public issue: no secret env values, no IP addresses."""
    secrets = sorted(
        {
            value.strip()
            for name, value in env.items()
            if _SECRET_ENV_NAME.search(name)
            and isinstance(value, str)
            and len(value.strip()) >= _MIN_SCRUBBED_VALUE
        },
        key=len,
        reverse=True,
    )
    for value in secrets:
        text = text.replace(value, "***")
    text = _IPV4.sub("<ip>", text)
    text = _IPV6.sub("<ip>", text)
    if len(text) > _MAX_DETAIL_CHARS:
        text = text[:_MAX_DETAIL_CHARS] + " ...(truncated)"
    return text


def record_verdicts(
    path: str | Path,
    *,
    source: str,
    checks: Iterable[CheckVerdict],
    complete: bool = True,
) -> None:
    """Append one step's verdicts to the job's verdict file."""
    line = json.dumps(
        {
            "source": source,
            "checks": [asdict(check) for check in checks],
            "complete": complete,
        },
        sort_keys=True,
    )
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _parse_record(line: str) -> tuple[str, list[CheckVerdict], bool] | None:
    try:
        record = json.loads(line)
        source = record["source"]
        checks = [
            CheckVerdict(
                name=str(item["name"]),
                ok=item["ok"] is True,
                detail=str(item.get("detail", "")),
                severity=str(item.get("severity", "")),
                failure_domain=str(item.get("failure_domain", "")),
            )
            for item in record["checks"]
        ]
        complete = record.get("complete", True) is True
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if not isinstance(source, str) or not source:
        return None
    return source, checks, complete


def load_trail(path: str | Path, expected_sources: Iterable[str] = _EXPECTED_SOURCES) -> Trail:
    """Merge the verdict file. A missing file or a torn line records nothing."""
    trail = Trail()
    seen: set[str] = set()
    all_complete = True
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        parsed = _parse_record(line) if line.strip() else None
        if parsed is None:
            continue
        source, checks, complete = parsed
        seen.add(source)
        all_complete = all_complete and complete
        trail.green.add(source)
        for check in checks:
            if check.ok:
                trail.green.add(check.name)
            else:
                trail.failing.setdefault(check.name, check)
    for source in expected_sources:
        if source not in seen:
            trail.failing[source] = CheckVerdict(
                name=source,
                ok=False,
                detail=(
                    "the step recorded no verdict: it crashed or was skipped before "
                    "recording one, so none of its checks were proven (see the run log)"
                ),
                severity="P1",
                failure_domain="watchdog-step",
            )
    trail.green.difference_update(trail.failing)
    trail.complete = all_complete and all(source in seen for source in expected_sources)
    return trail


def issue_trail_mode(env: Mapping[str, str]) -> str:
    """Return which issue writes this run may make (``full``, ``open-only``, or ``off``)."""
    if env.get(_DRY_RUN_ENV) == "1" or env.get(_SSH_OVERRIDE_ENV) == "1":
        return _OFF
    event = env.get(_GH_EVENT_NAME_KEY, "")
    if event not in ("schedule", "workflow_dispatch"):
        return _OFF
    if (env.get(_BOUND_CAP_ENV) or "").strip():
        return _OPEN_ONLY
    if event == "schedule" or env.get(_GH_REF_KEY) == "refs/heads/main":
        return _FULL
    return _OPEN_ONLY


class ListingFailed(RuntimeError):
    """The open issues could not be read, so nothing may be decided from them."""


class WriteFailed(RuntimeError):
    """A create, comment or close did not land."""


class IssueApi(Protocol):
    def open_issues(self) -> list[dict]: ...

    def create(self, title: str, body: str, labels: Iterable[str]) -> int: ...

    def comment(self, number: int, body: str) -> None: ...

    def close(self, number: int) -> None: ...


class GitHubIssues:
    """The REST calls the trail needs, for one repository."""

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        opener=urlopen,
        timeout: float = _API_TIMEOUT_SECONDS,
    ) -> None:
        self.repository = repository
        self._token = token
        self._opener = opener
        self._timeout = timeout

    def _call(self, method: str, path: str, payload: dict | None = None) -> object:
        headers = _github_headers(
            token=self._token,
            user_agent="infra2-watchdog-issue-trail/1.0",
            content_type="application/json",
        )
        return _urlopen_json(
            _github_api_url(f"/repos/{self.repository}{path}"),
            headers=headers,
            method=method,
            payload=payload,
            timeout=self._timeout,
            opener=self._opener,
        )

    def open_issues(self, *, with_body: bool = False) -> list[dict]:
        """Return open issues as ``{"number", "title"}`` dicts (plus body/author if *with_body*)."""
        issues: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            path = f"/issues?state=open&per_page={_PER_PAGE}&page={page}"
            try:
                items = self._call("GET", path)
            except (HTTPError, URLError, OSError, ValueError) as exc:
                raise ListingFailed(f"GET {path}: {exc}") from exc
            if not isinstance(items, list):
                raise ListingFailed(f"GET {path} returned {type(items).__name__}, not a list")
            for item in items:
                if not isinstance(item, dict):
                    raise ListingFailed(f"GET {path} returned a non-object issue")
                if "pull_request" in item:
                    continue
                number, title = item.get("number"), item.get("title")
                if not isinstance(number, int) or not isinstance(title, str):
                    raise ListingFailed(f"GET {path} returned an issue without number/title")
                entry = {"number": number, "title": title}
                if with_body:
                    author = item.get("user")
                    entry["body"] = item.get("body") or ""
                    entry["author"] = (
                        str(author.get("login") or "") if isinstance(author, dict) else ""
                    )
                issues.append(entry)
            if len(items) < _PER_PAGE:
                return issues
        raise ListingFailed(f"more than {_MAX_PAGES * _PER_PAGE} open issues; not read to the end")

    def _write(self, method: str, path: str, payload: dict) -> object:
        try:
            return self._call(method, path, payload)
        except (HTTPError, URLError, OSError, ValueError) as exc:
            raise WriteFailed(f"{method} {path}: {exc}") from exc

    def create(self, title: str, body: str, labels: Iterable[str]) -> int:
        created = self._write(
            "POST", "/issues", {"title": title, "body": body, "labels": list(labels)}
        )
        number = created.get("number") if isinstance(created, dict) else None
        if not isinstance(number, int):
            raise WriteFailed("POST /issues returned no issue number")
        return number

    def comment(self, number: int, body: str) -> None:
        self._write("POST", f"/issues/{number}/comments", {"body": body})

    def update_body(self, number: int, body: str) -> None:
        self._write("PATCH", f"/issues/{number}", {"body": body})

    def close(self, number: int) -> None:
        self._write(
            "PATCH",
            f"/issues/{number}",
            {"state": "closed", "state_reason": "completed"},
        )


def _red_body(
    name: str,
    verdict: CheckVerdict,
    *,
    run_url: str,
    context: str,
    env: Mapping[str, str],
) -> str:
    labels = " ".join(part for part in (verdict.severity, verdict.failure_domain) if part)
    lines = [
        f"**`{name}` is red** in the ops-checks watchdog" + (f" ({labels})" if labels else ""),
        "",
        f"- Run: {run_url or '(no run URL)'}",
        f"- Run kind: {context}",
        f"- Detail: {scrub(verdict.detail, env) or '(none)'}",
        "",
        "Recorded by the ops-checks watchdog issue trail (`tools/watchdog_issue_trail.py`, "
        "truealpha#876) in addition to the Feishu page. One open issue per check: a later "
        "red run comments here, and the next green scheduled run closes it.",
    ]
    return "\n".join(lines)


def _green_body(name: str, *, run_url: str, context: str) -> str:
    return "\n".join(
        [
            f"**`{name}` is green again** in {run_url or 'the latest run'} ({context}).",
            "",
            "Closed by the ops-checks watchdog issue trail (truealpha#876). "
            "A later red run opens a new issue.",
        ]
    )


def _retired_body(name: str, *, run_url: str, context: str) -> str:
    return "\n".join(
        [
            f"**`{name}` no longer reports to this trail** "
            f"({run_url or 'latest run'}, {context}): every watchdog step recorded "
            "its paging checks and none recorded this one.",
            "",
            "It is report-only now (#908: its failures go to the daily infra2 report, "
            "not the pager) or no longer exists. Closed by the ops-checks watchdog "
            "issue trail so it is not left open forever.",
        ]
    )


def reconcile_issue_trail(
    api: IssueApi,
    trail: Trail,
    *,
    mode: str,
    run_url: str = "",
    context: str = "",
    env: Mapping[str, str] | None = None,
) -> int:
    """Open or comment on every red check's issue; close green ones when allowed."""
    env = env or {}
    if mode not in (_FULL, _OPEN_ONLY, _OFF):
        print(f"::error::unknown issue-trail mode {mode!r}")
        return 1
    if mode == _OFF:
        print(
            "issue trail: off for this run "
            "(dry run, manual diagnostics, or not a scheduled/dispatched run)"
        )
        return 0
    try:
        listed = api.open_issues()
    except ListingFailed as exc:
        print(
            f"::warning::issue trail: open issues could not be listed ({exc}); "
            "nothing written, the next run retries"
        )
        return 0

    trail = trail.renamed(lambda name: scrub(name, env))
    by_title: dict[str, list[int]] = {}
    for issue in listed:
        by_title.setdefault(issue["title"], []).append(issue["number"])
    for numbers in by_title.values():
        numbers.sort()

    failed_writes = 0
    for name in sorted(trail.failing):
        title = title_for(name)
        body = _red_body(name, trail.failing[name], run_url=run_url, context=context, env=env)
        existing = by_title.get(title)
        try:
            if existing:
                api.comment(existing[0], body)
                print(f"issue trail: commented on #{existing[0]} ({title})")
            else:
                number = api.create(title, body, _ISSUE_LABELS)
                by_title[title] = [number]
                print(f"issue trail: opened #{number} ({title})")
        except WriteFailed as exc:
            failed_writes += 1
            print(f"::error::issue trail: could not record {title!r}: {exc}")

    if mode == _FULL:
        for title in sorted(by_title):
            name = check_name(title)
            if name is None:
                continue
            if trail.is_green(name):
                body = _green_body(name, run_url=run_url, context=context)
            elif trail.is_retired(name):
                body = _retired_body(name, run_url=run_url, context=context)
            else:
                continue
            for number in by_title[title]:
                try:
                    api.comment(number, body)
                    api.close(number)
                    print(f"issue trail: closed #{number} ({title})")
                except WriteFailed as exc:
                    failed_writes += 1
                    print(f"::error::issue trail: could not close #{number}: {exc}")
    else:
        print("issue trail: open-only run (drill or branch dispatch); no issue is closed")

    return 1 if failed_writes else 0


# ---------------------------------------------------------------------------
# OpenPanel export query & funnel summary engine (#133)
# ---------------------------------------------------------------------------

_DEFAULT_OPENPANEL_API_URL = "https://openpanel.zitian.party/api"
_OPENPANEL_KEY_ENV = "_".join(("OPENPANEL", "API", "KEY"))
_OPENPANEL_URL_ENV = "_".join(("OPENPANEL", "API", "URL"))
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_loopback_host(host: str) -> bool:
    return host.strip("[]").lower() in _LOOPBACK_HOSTS


def resolve_openpanel_api_url(
    explicit: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    default_url: str = _DEFAULT_OPENPANEL_API_URL,
) -> str:
    """Resolve and validate the OpenPanel API base URL: explicit > env var > default.

    Refuses any non-HTTPS URL except loopback (localhost / 127.0.0.1 / ::1) so the
    client secret is never sent over plaintext HTTP to a remote host.
    """
    source = env if env is not None else os.environ
    candidate = (explicit or source.get(_OPENPANEL_URL_ENV) or "").strip()
    url = candidate or default_url
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http" and parsed.hostname and _is_loopback_host(parsed.hostname):
        return url
    raise SystemExit(
        f"error: refusing to send the API key to a non-HTTPS endpoint: {url!r}; "
        "use https:// (or http:// only for localhost)."
    )


def resolve_openpanel_api_key(env: Mapping[str, str] | None = None) -> str:
    """Return the OpenPanel API key from the environment or exit with a clear error."""
    source = env if env is not None else os.environ
    key = (source.get(_OPENPANEL_KEY_ENV) or "").strip()
    if not key:
        raise SystemExit(
            f"error: {_OPENPANEL_KEY_ENV} is not set; export it before querying OpenPanel."
        )
    return key


def load_openpanel_funnel_presets(contract_path: str | Path) -> dict[str, tuple[str, ...]]:
    """Load named funnel step presets from a declarative ``openpanel_analytics.json`` file."""
    raw = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    funnels = raw.get("funnels") if isinstance(raw, Mapping) else None
    if not isinstance(funnels, list):
        raise ValueError(f"{contract_path}: missing 'funnels' list")
    presets: dict[str, tuple[str, ...]] = {}
    for entry in funnels:
        if not isinstance(entry, Mapping):
            continue
        steps_raw = entry.get("steps")
        if not isinstance(steps_raw, list):
            continue
        steps = tuple(str(step).strip() for step in steps_raw if str(step).strip())
        if len(steps) < 2:
            continue
        preset = str(entry.get("preset") or "").strip()
        if not preset:
            name = str(entry.get("name") or "").lower()
            desc = str(entry.get("description") or "").lower()
            if "north star" in name or "core loop" in desc or "review" in name:
                preset = "core-funnel"
            elif "activation" in desc or "upload" in name:
                preset = "activation"
        if preset:
            presets[preset] = steps
    return presets


def build_openpanel_payload(
    *,
    command: str,
    limit: int = 100,
    env: str | None = None,
    event: str | None = None,
    steps: Sequence[str] | None = None,
) -> dict[str, object]:
    """Build the JSON request body for an OpenPanel ``events`` or ``funnel`` query."""
    payload: dict[str, object] = {"limit": limit}
    if env:
        payload["filters"] = [{"name": "environment", "operator": "is", "value": [env]}]
    if command == "events":
        payload["event"] = event or "*"
    elif command == "funnel":
        payload["steps"] = list(steps or ())
    return payload


def format_openpanel_markdown(
    result: Mapping[str, object], *, command: str, env: str | None = None
) -> str:
    """Format an OpenPanel events or funnel response as a Markdown summary table."""
    scope = f" ({env})" if env else ""
    if command == "funnel":
        rows = result.get("steps") or result.get("data") or []
        lines = [
            f"### OpenPanel Funnel Summary{scope}",
            "",
            "| Step | Event | Count | Conversion |",
            "|---|---|---|---|",
        ]
        if isinstance(rows, list) and rows:
            first_count = 0
            for idx, item in enumerate(rows, start=1):
                if not isinstance(item, Mapping):
                    continue
                name = str(item.get("event") or item.get("name") or f"step_{idx}")
                count = int(item.get("count") or item.get("value") or 0)
                if idx == 1:
                    first_count = count
                pct = (
                    round((count / first_count) * 100.0, 1)
                    if first_count > 0
                    else (100.0 if count > 0 else 0.0)
                )
                lines.append(f"| {idx} | `{name}` | {count} | {pct}% |")
        else:
            lines.append("| - | (no funnel steps returned) | 0 | 0.0% |")
        return "\n".join(lines)

    events = result.get("events") or result.get("data") or []
    return "\n".join(
        [
            f"### OpenPanel Events Summary{scope}",
            "",
            f"Total returned: {len(events) if isinstance(events, list) else 0}",
        ]
    )


def post_openpanel_json(
    url: str,
    api_key: str,
    payload: Mapping[str, object],
    *,
    timeout: float = 30.0,
    opener: Callable[..., object] = urlopen,
) -> dict[str, object]:
    """POST a JSON payload to the OpenPanel export API and return the decoded response."""
    body = _urlopen_json(
        url,
        headers={
            "Content-Type": "application/json",
            "openpanel-client-secret": api_key,
        },
        method="POST",
        payload=payload,
        timeout=timeout,
        opener=opener,
    )
    if not isinstance(body, dict):
        raise ValueError(f"OpenPanel response from {url} is not a JSON object")
    return body


__all__ = [
    "CheckVerdict",
    "ConsecutiveObservationState",
    "DELAY_ALLOWANCE",
    "GitHubIssues",
    "IssueApi",
    "ListingFailed",
    "MISSED_TICKS_TOLERATED",
    "PeerReadError",
    "PeerVerdict",
    "ScheduleError",
    "Trail",
    "WriteFailed",
    "bound_for",
    "build_openpanel_payload",
    "check_name",
    "crons_in",
    "evaluate_consecutive_hysteresis",
    "evaluate_peer_liveness",
    "format_openpanel_markdown",
    "github_getter",
    "is_recently_flapping",
    "issue_trail_mode",
    "largest_gap",
    "load_openpanel_funnel_presets",
    "load_trail",
    "newest_tick",
    "parse_bound_cap_hours",
    "post_openpanel_json",
    "reconcile_issue_trail",
    "record_verdicts",
    "resolve_openpanel_api_key",
    "resolve_openpanel_api_url",
    "scrub",
    "title_for",
]
