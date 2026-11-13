"""Observability primitives: hysteresis, peer liveness, issue trails, and OpenPanel queries."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from infra2_sdk._wire import _is_loopback_host, parse_iso_utc
from infra2_sdk.transport import (
    _github_api_url,
    _github_headers,
    _github_json_fetcher,
    _urlopen_json,
)

if TYPE_CHECKING:
    from infra2_sdk.stage_record import StageRecord


def is_recently_flapping(
    *,
    event_count: int,
    last_event_at: float,
    now: float,
    count_threshold: int,
    recency_window_seconds: int,
) -> bool:
    return event_count > count_threshold and max(0.0, now - last_event_at) <= recency_window_seconds


@dataclass
class ConsecutiveObservationState:
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
    if is_bad_now:
        state.good_streak = 0
        state.bad_streak += 1
        if not state.active:
            if state.bad_streak < max(1, failure_threshold):
                return "none"
            state.active, state.last_alert_at = True, now
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
    state.active, state.good_streak, state.last_alert_at = False, 0, 0.0
    return "resolve"


MISSED_TICKS_TOLERATED = 2
DELAY_ALLOWANCE = timedelta(hours=1)
_STARTED = ("in_progress", "completed")
_STARTUP_FAILURE = "startup_failure"
_API_TIMEOUT_SECONDS = 20.0


class PeerReadError(RuntimeError):
    pass


def github_getter(
    token: str = "", *, timeout: float = _API_TIMEOUT_SECONDS, opener=urlopen
) -> Callable[[str], object]:
    return _github_json_fetcher(
        headers=_github_headers(token=token, user_agent="infra2-scheduler-peer-liveness/1.0"),
        timeout=timeout,
        error_cls=PeerReadError,
        opener=opener,
    )


def bound_for(gap: timedelta) -> timedelta:
    return MISSED_TICKS_TOLERATED * gap + DELAY_ALLOWANCE


def parse_bound_cap_hours(raw: str | None) -> timedelta | None:
    text = (raw or "").strip()
    if not text:
        return None
    hours = float(text)
    if not math.isfinite(hours) or hours < 0:
        raise ValueError(f"bound cap {text!r} must be a finite number of hours >= 0")
    return timedelta(hours=hours)


def _timestamp(value: object, what: str) -> datetime:
    if not isinstance(value, str):
        raise PeerReadError(f"{what} has no timestamp ({value!r})")
    try:
        return parse_iso_utc(value)
    except ValueError as exc:
        msg = "without a zone" if "time zone" in str(exc) else "an unreadable"
        if msg == "without a zone":
            raise PeerReadError(f"{what} has a timestamp without a zone ({value!r})") from exc
        raise PeerReadError(f"{what} has an unreadable timestamp {value!r}") from exc


def newest_tick(runs: Iterable[dict]) -> datetime | None:
    ticks = [
        _timestamp(r.get("created_at"), f"run {r.get('id')}")
        for r in runs
        if r.get("event") == "schedule"
        and r.get("status") in _STARTED
        and r.get("conclusion") != _STARTUP_FAILURE
    ]
    return max(ticks) if ticks else None


_TITLE_PREFIX = "ops-checks watchdog is red: "
_ISSUE_LABELS = ("incident",)
_SSH_OVERRIDE_ENV = "_".join(("INFRA2", "WATCHDOG", "SSH", "TARGETS", "OVERRIDDEN"))
_DRY_RUN_ENV = "_".join(("WATCHDOG", "DRY", "RUN"))
_EXPECTED_SOURCES = ("out-of-band-watchdog", "iac-runner-health")
_FULL, _OFF = "full", "off"
_MAIN_REF, _TAG_REFS = "refs/heads/main", "refs/tags/"
_PER_PAGE, _MAX_PAGES, _MAX_DETAIL_CHARS, _MIN_SCRUBBED_VALUE = 100, 10, 1000, 4
_SECRET_ENV_NAME = re.compile(
    r"^(?:INFRA2_|DOKPLOY_|CF_|FEISHU_|OP_|VAULT_)\w*"
    r"(?:HOST|USER|TOKEN|KEY|SECRET|PASSWORD|WEBHOOK|CHAT_ID|APP_ID)"
    r"|^INFRA2_OUT_OF_BAND_\w+|^(?:GITHUB_TOKEN|GH_TOKEN)$"
)
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
    seconds: float = field(default=0.0, kw_only=True)


@dataclass
class Trail:
    failing: dict[str, CheckVerdict] = field(default_factory=dict)
    green: set[str] = field(default_factory=set)
    complete: bool = False

    def is_green(self, name: str) -> bool:
        return name not in self.failing and name in self.green

    def is_retired(self, name: str) -> bool:
        return self.complete and name not in self.failing and name not in self.green

    def renamed(self, rename) -> Trail:
        return Trail(
            {rename(k): v for k, v in self.failing.items()},
            {rename(n) for n in self.green},
            self.complete,
        )


def title_for(name: str, *, title_prefix: str = _TITLE_PREFIX) -> str:
    if not title_prefix or not title_prefix.strip():
        raise ValueError("title_prefix must not be empty or whitespace")
    return f"{title_prefix}{name}"


def check_name(title: str, *, title_prefix: str = _TITLE_PREFIX) -> str | None:
    if not title_prefix or not title_prefix.strip():
        raise ValueError("title_prefix must not be empty or whitespace")
    return (title[len(title_prefix) :] or None) if title.startswith(title_prefix) else None


def scrub(text: str, env: Mapping[str, str]) -> str:
    secrets = sorted(
        {
            v.strip()
            for k, v in env.items()
            if _SECRET_ENV_NAME.search(k)
            and isinstance(v, str)
            and len(v.strip()) >= _MIN_SCRUBBED_VALUE
        },
        key=len,
        reverse=True,
    )
    for val in secrets:
        text = text.replace(val, "***")
    text = _IPV6.sub("<ip>", _IPV4.sub("<ip>", text))
    return text[:_MAX_DETAIL_CHARS] + " ...(truncated)" if len(text) > _MAX_DETAIL_CHARS else text


def record_verdicts(
    path: str | Path, *, source: str, checks: Iterable[CheckVerdict], complete: bool = True
) -> None:
    line = json.dumps(
        {"source": source, "checks": [asdict(c) for c in checks], "complete": complete},
        sort_keys=True,
    )
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _parse_record(line: str) -> tuple[str, list[CheckVerdict], bool] | None:
    try:
        rec = json.loads(line)
        src = rec["source"]
        checks = [
            CheckVerdict(
                str(i["name"]),
                i["ok"] is True,
                str(i.get("detail", "")),
                str(i.get("severity", "")),
                str(i.get("failure_domain", "")),
                seconds=float(i.get("seconds", 0)),
            )
            for i in rec["checks"]
        ]
        comp = rec.get("complete", True) is True
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return (src, checks, comp) if isinstance(src, str) and src else None


def load_trail(path: str | Path, expected_sources: Iterable[str] = _EXPECTED_SOURCES) -> Trail:
    trail = Trail()
    seen: set[str] = set()
    all_complete = True
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        if not line.strip() or (parsed := _parse_record(line)) is None:
            continue
        src, checks, comp = parsed
        seen.add(src)
        all_complete = all_complete and comp
        trail.green.add(src)
        for c in checks:
            if c.ok:
                trail.green.add(c.name)
            else:
                trail.failing.setdefault(c.name, c)
    for src in expected_sources:
        if src not in seen:
            trail.failing[src] = CheckVerdict(
                src,
                False,
                "the step recorded no verdict: it crashed or was skipped before recording one",
                "P1",
                "watchdog-step",
            )
    trail.green.difference_update(trail.failing)
    trail.complete = all_complete and all(s in seen for s in expected_sources)
    return trail


def issue_trail_mode(env: Mapping[str, str]) -> str:
    return _OFF if env.get(_DRY_RUN_ENV) == "1" or env.get(_SSH_OVERRIDE_ENV) == "1" else _FULL


class ListingFailed(RuntimeError):
    pass


class WriteFailed(RuntimeError):
    pass


class IssueApi(Protocol):
    def open_issues(self) -> list[dict]: ...
    def create(self, title: str, body: str, labels: Iterable[str]) -> int: ...
    def comment(self, number: int, body: str) -> None: ...
    def close(self, number: int) -> None: ...


class GitHubIssues:
    def __init__(
        self,
        repository: str,
        token: str,
        *,
        opener=urlopen,
        timeout: float = _API_TIMEOUT_SECONDS,
    ) -> None:
        self.repository, self._token, self._opener, self._timeout = (
            repository,
            token,
            opener,
            timeout,
        )

    def _call(self, method: str, path: str, payload: dict | None = None) -> object:
        hdrs = _github_headers(
            token=self._token,
            user_agent="infra2-watchdog-issue-trail/1.0",
            content_type="application/json",
        )
        return _urlopen_json(
            _github_api_url(f"/repos/{self.repository}{path}"),
            headers=hdrs,
            method=method,
            payload=payload,
            timeout=self._timeout,
            opener=self._opener,
        )

    def open_issues(self, *, with_body: bool = False) -> list[dict]:
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
                num, title = item.get("number"), item.get("title")
                if not isinstance(num, int) or not isinstance(title, str):
                    raise ListingFailed(f"GET {path} returned an issue without number/title")
                entry: dict[str, object] = {"number": num, "title": title}
                if with_body:
                    u = item.get("user")
                    entry["body"] = item.get("body") or ""
                    entry["author"] = str(u.get("login") or "") if isinstance(u, dict) else ""
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
        res = self._write("POST", "/issues", {"title": title, "body": body, "labels": list(labels)})
        num = res.get("number") if isinstance(res, dict) else None
        if not isinstance(num, int):
            raise WriteFailed("POST /issues returned no issue number")
        return num

    def comment(self, number: int, body: str) -> None:
        self._write("POST", f"/issues/{number}/comments", {"body": body})

    def update_body(self, number: int, body: str) -> None:
        self._write("PATCH", f"/issues/{number}", {"body": body})

    def close(self, number: int) -> None:
        self._write("PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "completed"})


def _red_body(
    name: str,
    verdict: CheckVerdict,
    *,
    run_url: str,
    context: str,
    env: Mapping[str, str],
    source: str = "ops-checks watchdog",
    record: StageRecord | None = None,
    history: Sequence[StageRecord] = (),
    runbook: str = "",
) -> str:
    lbls = " ".join(p for p in (verdict.severity, verdict.failure_domain) if p)
    stage, subject, url = (record.env, record.subject, record.run_url) if record else ("",) * 3
    last = [
        f"{'green' if c.ok else 'red'} {r.subject}"
        for r in history
        for c in r.checks
        if c.name == name
    ]
    return "\n".join(
        [
            f"**`{name}` is red** in the {source}" + (f" ({lbls})" if lbls else ""),
            "",
            f"- Run: {run_url or '(no run URL)'}",
            f"- Run kind: {context}",
            f"- Env: {stage or '(none)'}",
            f"- Subject: {subject or '(none)'}",
            f"- Record: {url or '(none)'}",
            f"- Last verdicts: {', '.join(last[-3:]) or '(none)'}",
            f"- Runbook: {runbook or '(none)'}",
            f"- Detail: {scrub(verdict.detail, env) or '(none)'}",
            "",
            f"Recorded by the {source} issue trail (`tools/watchdog_issue_trail.py`, "
            "truealpha#876). One open issue per check.",
        ]
    )


def _green_body(
    name: str, *, run_url: str, context: str, source: str = "ops-checks watchdog"
) -> str:
    return (
        f"**`{name}` is green again** in {run_url or 'the latest run'} ({context}).\n\n"
        f"Closed by the {source} issue trail (truealpha#876)."
    )


def _retired_body(
    name: str, *, run_url: str, context: str, source: str = "ops-checks watchdog"
) -> str:
    return (
        f"**`{name}` no longer reports to this trail** ({run_url or 'latest run'}, {context}).\n\n"
        f"Closed by the {source} issue trail."
    )


def reconcile_issue_trail(
    api: IssueApi,
    trail: Trail,
    *,
    mode: str,
    run_url: str = "",
    context: str = "",
    env: Mapping[str, str] | None = None,
    title_prefix: str = _TITLE_PREFIX,
    labels: Iterable[str] = _ISSUE_LABELS,
    source: str = "ops-checks watchdog",
    record: StageRecord | None = None,
    history: Sequence[StageRecord] = (),
    runbooks: Mapping[str, str] | None = None,
    close: bool = True,
) -> int:
    env = env or {}
    if not title_prefix or not title_prefix.strip():
        raise ValueError("title_prefix must not be empty or whitespace")
    if mode not in (_FULL, _OFF):
        if mode == "open-only":
            raise ValueError("issue-trail mode 'open-only' was removed; pass 'full' or 'off'")
        print(f"::error::unknown issue-trail mode {mode!r}")
        return 1
    if mode == _OFF:
        print("issue trail: off for this run")
        return 0
    try:
        listed = api.open_issues()
    except ListingFailed as exc:
        print(f"::warning::issue trail: open issues could not be listed ({exc})")
        return 0

    trail = trail.renamed(lambda n: scrub(n, env))
    by_title: dict[str, list[int]] = {}
    for issue in listed:
        by_title.setdefault(issue["title"], []).append(issue["number"])
    for nums in by_title.values():
        nums.sort()

    failed = 0
    for name in sorted(trail.failing):
        title = title_for(name, title_prefix=title_prefix)
        body = _red_body(
            name,
            trail.failing[name],
            run_url=run_url,
            context=context,
            env=env,
            source=source,
            record=record,
            history=history,
            runbook=(runbooks or {}).get(name, ""),
        )
        existing = by_title.get(title)
        try:
            if existing:
                api.comment(existing[0], body)
                print(f"issue trail: commented on #{existing[0]} ({title})")
            else:
                num = api.create(title, body, tuple(labels))
                by_title[title] = [num]
                print(f"issue trail: opened #{num} ({title})")
        except WriteFailed as exc:
            failed += 1
            print(f"::error::issue trail: could not record {title!r}: {exc}")

    if not close or (
        record is not None and not (record.ref == _MAIN_REF or record.ref.startswith(_TAG_REFS))
    ):
        print("issue trail: this run closes no issue")
        return 1 if failed else 0
    for title in sorted(by_title):
        name = check_name(title, title_prefix=title_prefix)
        if name is None:
            continue
        if trail.is_green(name):
            body = _green_body(name, run_url=run_url, context=context, source=source)
        elif trail.is_retired(name):
            body = _retired_body(name, run_url=run_url, context=context, source=source)
        else:
            continue
        for num in by_title[title]:
            try:
                api.comment(num, body)
                api.close(num)
                print(f"issue trail: closed #{num} ({title})")
            except WriteFailed as exc:
                failed += 1
                print(f"::error::issue trail: could not close #{num}: {exc}")
    return 1 if failed else 0


_DEFAULT_OPENPANEL_API_URL = "https://openpanel.zitian.party/api"
_OPENPANEL_KEY_ENV = "_".join(("OPENPANEL", "API", "KEY"))
_OPENPANEL_URL_ENV = "_".join(("OPENPANEL", "API", "URL"))


def resolve_openpanel_api_url(
    explicit: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    default_url: str = _DEFAULT_OPENPANEL_API_URL,
) -> str:
    src = os.environ if env is None else env
    url = (explicit or src.get(_OPENPANEL_URL_ENV) or "").strip() or default_url
    p = urlparse(url)
    if p.scheme == "https" or (p.scheme == "http" and p.hostname and _is_loopback_host(p.hostname)):
        return url
    raise SystemExit(
        f"error: refusing to send the API key to a non-HTTPS endpoint: {url!r}; "
        "use https:// (or http:// only for localhost)."
    )


def resolve_openpanel_api_key(env: Mapping[str, str] | None = None) -> str:
    key = ((os.environ if env is None else env).get(_OPENPANEL_KEY_ENV) or "").strip()
    if not key:
        raise SystemExit(
            f"error: {_OPENPANEL_KEY_ENV} is not set; export it before querying OpenPanel."
        )
    return key


def load_openpanel_funnel_presets(contract_path: str | Path) -> dict[str, tuple[str, ...]]:
    raw = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    funnels = raw.get("funnels") if isinstance(raw, Mapping) else None
    if not isinstance(funnels, list):
        raise ValueError(f"{contract_path}: missing 'funnels' list")
    presets: dict[str, tuple[str, ...]] = {}
    for entry in (i for i in funnels if isinstance(i, Mapping)):
        steps_raw = entry.get("steps")
        if not isinstance(steps_raw, list):
            continue
        steps = tuple(s for step in steps_raw if (s := str(step).strip()))
        if len(steps) < 2:
            continue
        preset = str(entry.get("preset") or "").strip()
        if not preset:
            nm = str(entry.get("name") or "").lower()
            ds = str(entry.get("description") or "").lower()
            if "north star" in nm or "core loop" in ds or "review" in nm:
                preset = "core-funnel"
            elif "activation" in ds or "upload" in nm:
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
    scope = f" ({env})" if env else ""
    if command != "funnel":
        events = result.get("events") or result.get("data") or []
        total = len(events) if isinstance(events, list) else 0
        return f"### OpenPanel Events Summary{scope}\n\nTotal returned: {total}"
    rows = result.get("steps") or result.get("data") or []
    lines = [
        f"### OpenPanel Funnel Summary{scope}",
        "",
        "| Step | Event | Count | Conversion |",
        "|---|---|---|---|",
    ]
    if isinstance(rows, list) and rows:
        first = 0
        for idx, item in enumerate(rows, start=1):
            if not isinstance(item, Mapping):
                continue
            nm = str(item.get("event") or item.get("name") or f"step_{idx}")
            cnt = int(item.get("count") or item.get("value") or 0)
            if idx == 1:
                first = cnt
            pct = round((cnt / first) * 100.0, 1) if first > 0 else (100.0 if cnt > 0 else 0.0)
            lines.append(f"| {idx} | `{nm}` | {cnt} | {pct}% |")
    else:
        lines.append("| - | (no funnel steps returned) | 0 | 0.0% |")
    return "\n".join(lines)


def post_openpanel_json(
    url: str,
    api_key: str,
    payload: Mapping[str, object],
    *,
    timeout: float = 30.0,
    opener: Callable[..., object] = urlopen,
) -> dict[str, object]:
    hdrs = {"Content-Type": "application/json", "openpanel-client-secret": api_key}
    body = _urlopen_json(
        url, headers=hdrs, method="POST", payload=payload, timeout=timeout, opener=opener
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
    "Trail",
    "WriteFailed",
    "bound_for",
    "build_openpanel_payload",
    "check_name",
    "evaluate_consecutive_hysteresis",
    "format_openpanel_markdown",
    "github_getter",
    "is_recently_flapping",
    "issue_trail_mode",
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
