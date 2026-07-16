"""Standard HTTP transport policy over httpx without provider-specific behavior."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from infra2_sdk import __version__
from infra2_sdk.runtime._optional import require
from infra2_sdk.runtime.environ import (
    RuntimeEnvKey,
    env_bool,
    env_float,
    env_int,
    resolve_runtime_env,
)
from infra2_sdk.runtime.probes import DependencyStatus, ProbeResult

_IDEMPOTENT_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PUT", "TRACE"})
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


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

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> HttpClientSettings:
        user_agent = resolve_runtime_env(
            environ, RuntimeEnvKey.HTTP_USER_AGENT, default=f"infra2-sdk/{__version__}"
        ).value
        assert user_agent is not None
        return cls(
            timeout_seconds=env_float(environ, RuntimeEnvKey.HTTP_TIMEOUT_SECONDS, default=10.0),
            connect_timeout_seconds=env_float(
                environ, RuntimeEnvKey.HTTP_CONNECT_TIMEOUT_SECONDS, default=5.0
            ),
            max_connections=env_int(environ, RuntimeEnvKey.HTTP_MAX_CONNECTIONS, default=100),
            max_keepalive_connections=env_int(
                environ, RuntimeEnvKey.HTTP_MAX_KEEPALIVE_CONNECTIONS, default=20
            ),
            connect_retries=env_int(environ, RuntimeEnvKey.HTTP_CONNECT_RETRIES, default=0),
            user_agent=user_agent,
            follow_redirects=env_bool(environ, RuntimeEnvKey.HTTP_FOLLOW_REDIRECTS, default=False),
        )


def create_http_client(
    settings: HttpClientSettings | None = None,
    *,
    async_client: bool = False,
    headers: dict[str, str] | None = None,
) -> Any:
    """Return the standard httpx Client or AsyncClient configured with transport policy."""

    config = settings or HttpClientSettings()
    httpx = require("httpx", extra="http")
    limits = httpx.Limits(
        max_connections=config.max_connections,
        max_keepalive_connections=config.max_keepalive_connections,
    )
    timeout = httpx.Timeout(config.timeout_seconds, connect=config.connect_timeout_seconds)
    transport_type = httpx.AsyncHTTPTransport if async_client else httpx.HTTPTransport
    transport = transport_type(retries=config.connect_retries, limits=limits)
    merged_headers = {"User-Agent": config.user_agent}
    merged_headers.update(headers or {})
    client_type = httpx.AsyncClient if async_client else httpx.Client
    return client_type(
        timeout=timeout,
        transport=transport,
        headers=merged_headers,
        follow_redirects=config.follow_redirects,
    )


def retryable_request(
    method: str,
    status_code: int,
    *,
    has_idempotency_key: bool = False,
) -> bool:
    """Apply HTTP retry semantics without choosing an app-specific retry budget."""

    safe_to_repeat = method.upper() in _IDEMPOTENT_METHODS or has_idempotency_key
    return safe_to_repeat and status_code in _RETRYABLE_STATUS_CODES


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse the HTTP ``Retry-After`` delta-seconds or HTTP-date value."""

    if not value:
        return None
    cleaned = value.strip()
    if cleaned.isdigit():
        return float(cleaned)
    try:
        target = parsedate_to_datetime(cleaned)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return max(0.0, (target - reference).total_seconds())


async def probe_http(
    url: str,
    *,
    name: str = "http",
    client: Any | None = None,
    settings: HttpClientSettings | None = None,
) -> ProbeResult:
    """Treat any response below 500 as reachable while preserving its status evidence."""

    started = time.perf_counter()
    owned = client is None
    transport = client
    try:
        transport = transport or create_http_client(settings, async_client=True)
        response = await transport.get(url)
        status = (
            DependencyStatus.PRESENT if int(response.status_code) < 500 else DependencyStatus.ABSENT
        )
        return ProbeResult(name, status, f"HTTP {response.status_code}", _elapsed(started))
    except Exception as exc:  # noqa: BLE001 - a probe reports absence
        return ProbeResult(
            name,
            DependencyStatus.ABSENT,
            f"{type(exc).__name__}: {exc}",
            _elapsed(started),
        )
    finally:
        if owned and transport is not None:
            await transport.aclose()


class HttpCheck:
    def __init__(
        self,
        name: str,
        url: str,
        *,
        client: Any | None = None,
        settings: HttpClientSettings | None = None,
    ) -> None:
        if not name or not url.startswith(("http://", "https://")):
            raise ValueError("HTTP check requires a name and absolute HTTP URL")
        self.name = name
        self.url = url
        self.client = client
        self.settings = settings

    async def probe(self) -> ProbeResult:
        return await probe_http(
            self.url,
            name=self.name,
            client=self.client,
            settings=self.settings,
        )


def _elapsed(started: float) -> float:
    return (time.perf_counter() - started) * 1000
