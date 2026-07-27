"""Poll a deployed app URL until the NEW version is actually live.

Extracted from finance_report's ``tools/_lib/shell/health_check.sh`` — that
script's own header comment already said this responsibility "is infra's, not
app's" (finance_report#1535, not yet acted on until now). Only the generic
algorithm moves here: HTTP-200 polling, an optional JSON ``status`` field check,
and an optional version/``git_sha`` prefix match with its own stable-mismatch
budget (so a server that's up but still serving the OLD version doesn't read as
successful just because it answers 200). App-specific diagnostics — route-shadow
probing, log-marker troubleshooting hints, observability-backend links — are
NOT part of this function; a caller catches the raised ``RuntimeError`` and
layers its own diagnostics around it.

Requires the ``http`` extra (``httpx``) if using ``default_http_get`` — the core
``poll_until_healthy`` takes an injected ``http_get`` and has no import-time
dependency on it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# (status_code, body_text). A connection-level failure (timeout/DNS/TLS/refused)
# returns (0, reason) rather than raising — matching the convention
# libs.deploy.preview._http_get already uses on the infra2 side of this boundary.
HttpGet = Callable[[str], tuple[int, str]]


@dataclass(frozen=True)
class HealthCheckResult:
    attempts: int
    status_code: int
    body: str


def poll_until_healthy(
    url: str,
    *,
    http_get: HttpGet,
    expected_version: str = "",
    version_json_keys: tuple[str, ...] = ("git_sha", "version"),
    require_status: str | None = None,
    max_attempts: int = 24,
    max_version_mismatch_attempts: int | None = None,
    interval_seconds: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
) -> HealthCheckResult:
    """Poll ``url`` until healthy, or raise ``RuntimeError`` once exhausted.

    "Healthy" means: HTTP 200, AND (if ``require_status`` is given) a JSON body
    with ``status == require_status`` — the two apps' own health endpoints don't
    even agree on this string today (``"healthy"`` vs ``"ok"``), which is exactly
    why this isn't hardcoded; pass what your app's endpoint actually returns, or
    omit it to accept any 200 — AND (if ``expected_version`` is given) one of
    ``version_json_keys`` present in the body prefix-matching ``expected_version``
    in either direction (handles a short sha vs a long sha).

    A version mismatch does not fail immediately — the server may just not have
    picked up the new deploy yet. It only fails once the SAME wrong version has
    been seen for ``max_version_mismatch_attempts`` consecutive attempts
    (default: the full ``max_attempts`` budget), so a rollout that briefly serves
    a transitional version isn't punished.
    """
    mismatch_budget = max_version_mismatch_attempts or max_attempts
    mismatch_streak = 0
    last_mismatch = ""
    last_status = 0

    for attempt in range(1, max_attempts + 1):
        status_code, body = http_get(url)
        last_status = status_code

        if status_code != 200:
            _maybe_sleep(sleep, interval_seconds, attempt, max_attempts)
            continue

        parsed = _parse_json_object(body)
        if require_status is not None and (
            parsed is None or parsed.get("status") != require_status
        ):
            _maybe_sleep(sleep, interval_seconds, attempt, max_attempts)
            continue

        if expected_version:
            actual = _first_present(parsed, version_json_keys) if parsed else ""
            if not _version_prefix_matches(actual, expected_version):
                if actual == last_mismatch:
                    mismatch_streak += 1
                else:
                    last_mismatch = actual
                    mismatch_streak = 1
                if mismatch_streak >= mismatch_budget:
                    raise RuntimeError(
                        f"{url}: still reporting version {actual!r} (expected "
                        f"{expected_version!r} or a prefix match) after "
                        f"{mismatch_streak} stable mismatches"
                    )
                _maybe_sleep(sleep, interval_seconds, attempt, max_attempts)
                continue

        return HealthCheckResult(attempts=attempt, status_code=status_code, body=body)

    raise RuntimeError(
        f"{url}: did not become healthy after {max_attempts} attempts "
        f"(last status: HTTP {last_status})"
    )


def default_http_get(*, timeout: float = 10.0) -> HttpGet:
    """httpx-backed ``http_get``: returns ``(status_code, body_text)``, or
    ``(0, error-text)`` on any connection-level failure."""
    httpx = _require_httpx()

    def http_get(url: str) -> tuple[int, str]:
        try:
            response = httpx.get(url, timeout=timeout, follow_redirects=True)
        except httpx.HTTPError as exc:
            return 0, str(exc)
        return response.status_code, response.text

    return http_get


def _require_httpx() -> Any:
    from infra2_sdk.runtime._optional import require

    return require("httpx", extra="http")


def _maybe_sleep(
    sleep: Callable[[float], None],
    interval_seconds: float,
    attempt: int,
    max_attempts: int,
) -> None:
    if attempt < max_attempts:
        sleep(interval_seconds)


def _parse_json_object(body: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _first_present(parsed: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = parsed.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _version_prefix_matches(actual: str, expected: str) -> bool:
    if not actual:
        return False
    return actual.startswith(expected) or expected.startswith(actual)
