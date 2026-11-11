"""Standard HTTP transport policy, resilient retries, and reachability probes over httpx."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from infra2_sdk import __version__
from infra2_sdk.runtime._optional import require
from infra2_sdk.runtime.health import DependencyStatus, ProbeResult, _elapsed

_DEFAULT_RETRY_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})


@dataclass(frozen=True)
class HttpClientSettings:
    timeout_seconds: float = 10.0
    connect_timeout_seconds: float = 5.0
    max_connections: int = 100
    max_keepalive_connections: int = 20
    connect_retries: int = 0
    user_agent: str = f"infra2-sdk/{__version__}"
    follow_redirects: bool = False

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.connect_timeout_seconds <= 0:
            raise ValueError("HTTP timeouts must be positive")
        if self.max_connections <= 0 or self.max_keepalive_connections < 0:
            raise ValueError("HTTP connection limits are invalid")
        if self.max_keepalive_connections > self.max_connections:
            raise ValueError("keepalive connections cannot exceed max_connections")
        if self.connect_retries < 0:
            raise ValueError("connect_retries must be non-negative")


def _build_client(
    settings: HttpClientSettings | None = None,
    *,
    async_client: bool = False,
    headers: dict[str, str] | None = None,
) -> Any:
    cfg = settings or HttpClientSettings()
    httpx = require("httpx", extra="http")
    limits = httpx.Limits(
        max_connections=cfg.max_connections,
        max_keepalive_connections=cfg.max_keepalive_connections,
    )
    timeout = httpx.Timeout(cfg.timeout_seconds, connect=cfg.connect_timeout_seconds)
    transport_cls = httpx.AsyncHTTPTransport if async_client else httpx.HTTPTransport
    client_cls = httpx.AsyncClient if async_client else httpx.Client
    return client_cls(
        timeout=timeout,
        transport=transport_cls(retries=cfg.connect_retries, limits=limits),
        headers={"User-Agent": cfg.user_agent, **(headers or {})},
        follow_redirects=cfg.follow_redirects,
    )


def _retry_delay(
    headers: Any,
    attempt: int,
    *,
    backoff_base: float,
    max_backoff: float,
    now: Callable[[], datetime] | None = None,
) -> float:
    raw = (
        str(headers.get("retry-after") or headers.get("Retry-After") or "").strip()
        if headers is not None
        else ""
    )
    if raw:
        try:
            return min(max_backoff, max(0.0, float(raw)))
        except ValueError:
            try:
                dt = parsedate_to_datetime(raw)
                ref = (now or (lambda: datetime.now(UTC)))()
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return min(max_backoff, max(0.0, (dt - ref).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(max_backoff, backoff_base * (2**attempt))


def request_with_retry(
    client: Any,
    method: str,
    url: str,
    *,
    max_attempts: int = 3,
    retry_statuses: Collection[int] = _DEFAULT_RETRY_STATUSES,
    backoff_base: float = 0.5,
    max_backoff: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> Any:
    """Execute a synchronous httpx request with Retry-After and exponential backoff."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    httpx = require("httpx", extra="http")
    for attempt in range(max_attempts):
        try:
            resp = client.request(method, url, **kwargs)
        except (httpx.TransportError, OSError):
            if attempt + 1 >= max_attempts:
                raise
            sleep(_retry_delay(None, attempt, backoff_base=backoff_base, max_backoff=max_backoff))
            continue
        if int(resp.status_code) not in retry_statuses or attempt + 1 >= max_attempts:
            return resp
        sleep(
            _retry_delay(resp.headers, attempt, backoff_base=backoff_base, max_backoff=max_backoff)
        )
    raise AssertionError("unreachable")  # pragma: no cover


async def arequest_with_retry(
    client: Any,
    method: str,
    url: str,
    *,
    max_attempts: int = 3,
    retry_statuses: Collection[int] = _DEFAULT_RETRY_STATUSES,
    backoff_base: float = 0.5,
    max_backoff: float = 30.0,
    asleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    **kwargs: Any,
) -> Any:
    """Execute an async httpx request with Retry-After and exponential backoff."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    httpx = require("httpx", extra="http")
    for attempt in range(max_attempts):
        try:
            resp = await client.request(method, url, **kwargs)
        except (httpx.TransportError, OSError):
            if attempt + 1 >= max_attempts:
                raise
            await asleep(
                _retry_delay(None, attempt, backoff_base=backoff_base, max_backoff=max_backoff)
            )
            continue
        if int(resp.status_code) not in retry_statuses or attempt + 1 >= max_attempts:
            return resp
        await asleep(
            _retry_delay(resp.headers, attempt, backoff_base=backoff_base, max_backoff=max_backoff)
        )
    raise AssertionError("unreachable")  # pragma: no cover


async def probe_http(
    url: str,
    *,
    name: str = "http",
    client: Any | None = None,
    settings: HttpClientSettings | None = None,
) -> ProbeResult:
    """Treat any HTTP response below 500 as reachable while preserving status evidence."""
    started = time.perf_counter()
    owned = client is None
    transport = client
    try:
        transport = transport or _build_client(settings, async_client=True)
        resp = await transport.get(url)
        code = int(resp.status_code)
        status = DependencyStatus.PRESENT if code < 500 else DependencyStatus.ABSENT
        return ProbeResult(name, status, f"HTTP {code}", _elapsed(started))
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            name, DependencyStatus.ABSENT, f"{type(exc).__name__}: {exc}", _elapsed(started)
        )
    finally:
        if owned and transport is not None:
            await transport.aclose()


__all__ = [
    "HttpClientSettings",
    "arequest_with_retry",
    "probe_http",
    "request_with_retry",
]
