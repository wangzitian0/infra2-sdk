"""Minimal injectable HTTP transport shared by the open-protocol adapters."""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


HttpTransport = Callable[[str, str, Mapping[str, str], bytes | None], HttpResponse]


class TransportErrorKind(StrEnum):
    """Closed transport failure classification before an HTTP status arrives (#611)."""

    DNS = "dns"
    TIMEOUT = "timeout"
    REFUSED = "refused"
    TLS = "tls"
    CONNECTION = "connection"


@dataclass(frozen=True)
class TransportFailure(RuntimeError):
    """Typed transport failure carrying the classified ``TransportErrorKind`` (#611)."""

    kind: TransportErrorKind
    detail: str

    def __str__(self) -> str:
        return f"{self.kind.value}: {self.detail}"


def classify_transport_error(exc: BaseException) -> TransportFailure:
    """Classify an ``OSError`` or ``urllib.error.URLError`` into a ``TransportFailure``."""
    if isinstance(exc, TransportFailure):
        return exc
    reason: BaseException = exc
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        reason = exc.reason
    detail = str(reason) or type(reason).__name__
    lowered = detail.lower()
    if isinstance(reason, ssl.SSLError) or "ssl" in lowered or "tls" in lowered:
        return TransportFailure(TransportErrorKind.TLS, detail)
    if isinstance(reason, TimeoutError | socket.timeout) or "timed out" in lowered:
        return TransportFailure(TransportErrorKind.TIMEOUT, detail)
    if (
        isinstance(reason, socket.gaierror)
        or "nodename nor servname" in lowered
        or "name or service not known" in lowered
    ):
        return TransportFailure(TransportErrorKind.DNS, detail)
    if isinstance(reason, ConnectionRefusedError) or "connection refused" in lowered:
        return TransportFailure(TransportErrorKind.REFUSED, detail)
    return TransportFailure(TransportErrorKind.CONNECTION, detail)


@dataclass(frozen=True)
class ThrottlePolicy:
    """Declarative per-key rate budget and spacing constraint (#611)."""

    requests_per_minute: int
    min_spacing_seconds: float = 0.0
    window_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        if self.min_spacing_seconds < 0:
            raise ValueError("min_spacing_seconds cannot be negative")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")


def paced_transport(
    transport: HttpTransport,
    policy: ThrottlePolicy,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> HttpTransport:
    """Wrap *transport* to enforce *policy* rate-window ceiling and minimum request spacing."""
    history: deque[float] = deque()

    def send(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> HttpResponse:
        while True:
            now = clock()
            while history and now - history[0] >= policy.window_seconds:
                history.popleft()
            window_wait = (
                policy.window_seconds - (now - history[0])
                if len(history) >= policy.requests_per_minute
                else 0.0
            )
            spacing_wait = (
                policy.min_spacing_seconds - (now - history[-1])
                if history and policy.min_spacing_seconds > 0
                else 0.0
            )
            wait = max(window_wait, spacing_wait)
            if wait <= 0:
                break
            sleep(wait)
        history.append(clock())
        return transport(method, url, headers, body)

    return send


def decode_decimal_json(raw: bytes | str) -> Any:
    """Decode JSON text or UTF-8 bytes with ``parse_float=Decimal`` (#611)."""
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    return json.loads(text, parse_float=Decimal)


def urllib_transport(*, timeout: float = 30.0) -> HttpTransport:
    """Return a transport over the standard library; error statuses are returned, not raised."""

    def send(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                headers = {k.lower(): v for k, v in response.headers.items()}
                return HttpResponse(response.status, headers, response.read())
        except urllib.error.HTTPError as error:
            try:
                headers = {k.lower(): v for k, v in error.headers.items()}
                return HttpResponse(error.code, headers, error.read())
            finally:
                error.close()

    return send


__all__ = [
    "ThrottlePolicy",
    "TransportErrorKind",
    "TransportFailure",
    "classify_transport_error",
    "decode_decimal_json",
    "paced_transport",
    "urllib_transport",
]
