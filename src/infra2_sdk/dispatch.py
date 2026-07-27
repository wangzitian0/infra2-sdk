"""Dispatch a DeployRequest to infra2's receiver and correlate/verify the run.

Generalized off finance_report's own ``tools/app_deploy_transport.py`` (the only
one of the two apps that had this — truealpha's own dispatch was hand-rolled
inline bash with none of the three defenses below). This is infra2-receiver
boundary logic, identical for every caller: any app dispatching a
``DeployRequest`` needs to (1) know it correlated the RIGHT receiver run, not a
stale or concurrent one, and (2) know that run actually processed ITS request,
not just that *some* run with a plausible-looking outcome exists nearby in time.

Three defenses, all load-bearing (the report_watermark_race regression this
guards against: two overlapping dispatches, or a retry, landing runs close
enough together that a naive "most recent run" or "run with a matching title"
lookup can silently correlate to the wrong one):

1. Watermark — snapshot the newest existing run id before dispatching; only
   consider runs strictly newer than that. A run made by someone else's
   concurrent dispatch (or unrelated repository activity) can never look like
   "the one I just triggered."
2. Ambiguity guard — if more than one run is newer than the watermark, this is
   unresolvable by id/time alone; fail loudly rather than guess (a title-match
   `first(...)` — the pattern this replaces — silently picks one).
3. Log-content verification — even a single, uniquely-correlated, successful run
   is not proof it processed THIS request: fetch its logs and require the
   request's own ``request_id`` to appear verbatim before trusting the
   conclusion.

Requires the ``http`` extra (``httpx``) — see ``infra2_sdk.runtime.http`` for the
same optional-dependency convention.
"""

from __future__ import annotations

import time
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from infra2_sdk.deploy import DeployRequest

INFRA_REPOSITORY = "wangzitian0/infra2"
RECEIVER_WORKFLOW_FILE = "app-deploy-request.yml"
RECEIVER_EVENT_TYPE = "app-deploy-request"
_RUNS_PATH = (
    f"/repos/{INFRA_REPOSITORY}/actions/workflows/{RECEIVER_WORKFLOW_FILE}/runs"
    "?event=repository_dispatch&per_page=30"
)

# (method, path, json-body-or-None) -> parsed JSON response (GET) or None (POST).
# Raise on any non-2xx/non-204 response — dispatch_and_wait treats every Api call
# as fail-closed; there is no soft-error return value to check.
Api = Callable[[str, str, object], object]
# run_id -> the run's full log archive, decoded to a single searchable bytes blob.
LogFetcher = Callable[[int], bytes]


@dataclass(frozen=True)
class ReceiverRun:
    run_id: int
    url: str


def dispatch_and_wait(
    request: DeployRequest,
    *,
    api: Api,
    fetch_logs: LogFetcher,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 5.0,
    max_attempts: int = 300,
) -> ReceiverRun:
    """Dispatch ``request`` to infra2 and return the correlated, verified receiver run.

    Raises ``RuntimeError`` on: ambiguous correlation, a non-success conclusion,
    a run whose logs don't contain ``request.request_id``, a run with no
    canonical infra2 URL, or a timeout with no run ever appearing. The caller's
    own request-building/validation (``infra2_sdk.deploy.validate_wire_shape``,
    ``DeployRequest.from_dict``) must already be done — this function trusts
    ``request`` is a fully-validated, ready-to-send payload.
    """
    canonical = request.to_dict()
    baseline = _workflow_runs(api("GET", _RUNS_PATH, None))
    watermark = max((_run_id(run) for run in baseline), default=0)

    api(
        "POST",
        f"/repos/{INFRA_REPOSITORY}/dispatches",
        {"event_type": RECEIVER_EVENT_TYPE, "client_payload": canonical},
    )

    for attempt in range(max_attempts):
        runs = _workflow_runs(api("GET", _RUNS_PATH, None))
        candidates = [run for run in runs if _run_id(run) > watermark]
        if len(candidates) > 1:
            ids = sorted(_run_id(run) for run in candidates)
            raise RuntimeError(
                f"receiver run correlation is ambiguous after watermark {watermark}: {ids}"
            )
        if not candidates:
            if attempt + 1 < max_attempts:
                sleep(poll_interval)
            continue

        run = candidates[0]
        if run.get("status") != "completed":
            if attempt + 1 < max_attempts:
                sleep(poll_interval)
            continue
        run_id = _run_id(run)
        if run.get("conclusion") != "success":
            raise RuntimeError(f"infra2 receiver run {run_id} concluded {run.get('conclusion')!r}")
        request_id = request.request_id.encode("utf-8")
        if request_id not in fetch_logs(run_id):
            raise RuntimeError(
                f"infra2 receiver run {run_id} logs do not contain request_id "
                f"{request.request_id!r}"
            )
        url = run.get("html_url")
        if not isinstance(url, str) or not url.startswith(
            f"https://github.com/{INFRA_REPOSITORY}/actions/runs/"
        ):
            raise RuntimeError(f"infra2 receiver run {run_id} has no canonical URL")
        return ReceiverRun(run_id=run_id, url=url)

    raise RuntimeError(f"timed out waiting for an infra2 receiver run after watermark {watermark}")


def github_api_client(
    *,
    token: str,
    user_agent: str,
    timeout: float = 30.0,
    transport: Any = None,
) -> tuple[Api, LogFetcher]:
    """Build the default httpx-backed (``api``, ``fetch_logs``) pair for
    ``dispatch_and_wait``. Returned callables own an httpx.Client for their
    process lifetime — call from a single dispatch invocation, not held long-term.

    ``transport`` is an httpx transport override (e.g. ``httpx.MockTransport``)
    for tests; production callers omit it and get httpx's real network transport.
    """
    httpx = _require_httpx()
    client = httpx.Client(
        base_url="https://api.github.com",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        },
        follow_redirects=True,
        timeout=timeout,
        transport=transport,
    )

    def api(method: str, path: str, body: object) -> object:
        response = client.request(method, path, json=body if method == "POST" else None)
        if response.status_code >= 400:
            raise RuntimeError(
                f"GitHub API {method} {path.split('?', 1)[0]} failed with "
                f"HTTP {response.status_code}"
            )
        if method == "POST":
            if response.status_code != 204:
                raise RuntimeError(f"GitHub dispatch expected HTTP 204, got {response.status_code}")
            return None
        try:
            return response.json()
        except ValueError:
            raise RuntimeError("GitHub API response was not valid JSON") from None

    def fetch_logs(run_id: int) -> bytes:
        response = client.get(f"/repos/{INFRA_REPOSITORY}/actions/runs/{run_id}/logs")
        if response.status_code >= 400:
            raise RuntimeError(
                f"GitHub receiver logs request failed with HTTP {response.status_code}"
            )
        try:
            with zipfile.ZipFile(BytesIO(response.content)) as archive:
                return b"\n".join(archive.read(name) for name in archive.namelist())
        except zipfile.BadZipFile:
            raise RuntimeError("GitHub receiver logs response was not a zip archive") from None

    return api, fetch_logs


def _require_httpx() -> Any:
    from infra2_sdk.runtime._optional import require

    return require("httpx", extra="http")


def _workflow_runs(payload: object) -> list[Mapping[str, object]]:
    if not isinstance(payload, Mapping):
        raise RuntimeError("GitHub workflow-runs response must be an object")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list) or not all(isinstance(run, Mapping) for run in runs):
        raise RuntimeError("GitHub workflow-runs response must contain a run list")
    return runs


def _run_id(run: Mapping[str, object]) -> int:
    run_id = run.get("id")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise RuntimeError("GitHub workflow run id must be a positive integer")
    return run_id
