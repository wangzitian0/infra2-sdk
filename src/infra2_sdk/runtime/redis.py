"""Redis settings, credential redaction, and stdlib RESP probes."""

from __future__ import annotations

import contextlib
import re
import secrets as _secrets
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from infra2_sdk.runtime.health import DependencyStatus, ProbeResult, _elapsed

_REDIS_URL_RE = re.compile(r"(rediss?://)[^\s@/]+@")


class _RedisReplyError(RuntimeError):
    """Error frame (`-ERR` / `-NOAUTH` / `-WRONGPASS`) returned by Redis."""


def _parse_redis_url(raw: str) -> tuple[str, int, str, str, int, bool]:
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("url must use redis:// or rediss://")
    parts = urlsplit(cleaned)
    scheme = parts.scheme.lower()
    if scheme not in {"redis", "rediss"} or not parts.hostname:
        raise ValueError("url must use redis:// or rediss:// with a host")
    try:
        port = parts.port or 6379
    except ValueError as exc:
        raise ValueError("url carries an invalid Redis port") from exc
    username = unquote(parts.username or "")
    password = unquote(parts.password or "")
    path = parts.path.lstrip("/")
    if not path:
        db = 0
    elif path.isdigit():
        db = int(path)
    else:
        raise ValueError(f"url database index must be an integer, got {path!r}")
    return parts.hostname, port, username, password, db, scheme == "rediss"


@dataclass(frozen=True)
class RedisSettings:
    url: str = field(repr=False)
    connect_timeout_seconds: float = 5.0
    socket_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        _parse_redis_url(self.url)
        for attr in ("connect_timeout_seconds", "socket_timeout_seconds"):
            val = getattr(self, attr)
            if isinstance(val, bool) or not 0.1 <= val <= 60:
                raise ValueError(f"{attr} must be between 0.1 and 60")

    @property
    def host(self) -> str:
        return _parse_redis_url(self.url)[0]

    @property
    def port(self) -> int:
        return _parse_redis_url(self.url)[1]

    @property
    def username(self) -> str:
        return _parse_redis_url(self.url)[2]

    @property
    def password(self) -> str:
        return _parse_redis_url(self.url)[3]

    @property
    def db(self) -> int:
        return _parse_redis_url(self.url)[4]

    @property
    def use_tls(self) -> bool:
        return _parse_redis_url(self.url)[5]


def _resp_command(*parts: str) -> bytes:
    out = [b"*%d\r\n" % len(parts)]
    for part in parts:
        data = part.encode("utf-8")
        out.append(b"$%d\r\n%s\r\n" % (len(data), data))
    return b"".join(out)


def _read_reply(rf: Any) -> str | int | bytes | None:
    line = rf.readline()
    if not line:
        raise ConnectionError("Redis closed the connection")
    kind, body = line[:1], line[1:].rstrip(b"\r\n")
    if kind == b"+":
        return body.decode("latin1")
    if kind == b"-":
        raise _RedisReplyError(body.decode("latin1"))
    if kind == b":":
        return int(body)
    if kind == b"$":
        size = int(body)
        if size < 0:
            return None
        data = rf.read(size + 2)
        if len(data) < size + 2:
            raise ConnectionError("Redis closed the connection")
        return data[:-2]
    raise ConnectionError(f"unexpected RESP reply {line[:20]!r}")


def _open_redis_socket(settings: RedisSettings) -> socket.socket:
    sock = socket.create_connection(
        (settings.host, settings.port),
        timeout=settings.connect_timeout_seconds,
    )
    sock.settimeout(settings.socket_timeout_seconds)
    if not settings.use_tls:
        return sock
    try:
        ctx = ssl.create_default_context()
        return ctx.wrap_socket(sock, server_hostname=settings.host)
    except Exception:
        with contextlib.suppress(Exception):
            sock.close()
        raise


def probe_redis(
    settings: RedisSettings,
    *,
    write_probe: bool = False,
    key_prefix: str = "canary:probe:",
    ttl_seconds: int = 10,
) -> ProbeResult:
    """Execute a bounded Redis AUTH/PING (and optional SETEX/GET/DEL write) probe."""
    if ttl_seconds < 1:
        raise ValueError("ttl_seconds must be at least 1")
    started = time.perf_counter()
    try:
        with _open_redis_socket(settings) as sock, sock.makefile("rb") as rf:

            def call(step: str, *parts: str) -> str | int | bytes | None:
                sock.sendall(_resp_command(*parts))
                try:
                    return _read_reply(rf)
                except _RedisReplyError as err:
                    raise RuntimeError(f"{step} failed: {err}") from None

            if settings.password:
                if settings.username:
                    call("AUTH", "AUTH", settings.username, settings.password)
                else:
                    call("AUTH", "AUTH", settings.password)
            if settings.db:
                call("SELECT", "SELECT", str(settings.db))
            if call("PING", "PING") != "PONG":
                raise RuntimeError("PING did not answer PONG")
            if write_probe:
                key = f"{key_prefix}{_secrets.token_hex(8)}"
                value = _secrets.token_hex(16)
                call("SETEX", "SETEX", key, str(ttl_seconds), value)
                got = call("GET", "GET", key)
                if got != value.encode("utf-8"):
                    raise RuntimeError(f"GET returned {got!r} instead of the value written")
                call("DEL", "DEL", key)
        detail = (
            "AUTH, PING, SETEX/GET/DEL verified"
            if write_probe and settings.password
            else ("SETEX/GET/DEL verified" if write_probe else "PING succeeded")
        )
        return ProbeResult("redis", DependencyStatus.PRESENT, detail, _elapsed(started))
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            "redis",
            DependencyStatus.ABSENT,
            f"{type(exc).__name__}: {redact_redis_error(str(exc), settings)}",
            _elapsed(started),
        )


def redact_redis_error(detail: str, settings: RedisSettings | str) -> str:
    """Redact Redis URLs and embedded credentials from error text."""
    url = settings.url if isinstance(settings, RedisSettings) else settings
    redacted = detail
    if url:
        redacted = redacted.replace(url.strip(), "<redacted-redis-url>")
        with contextlib.suppress(ValueError):
            parsed = urlsplit(url.strip())
            secrets: set[str] = set()
            if parsed.password:
                secrets.update((parsed.password, unquote(parsed.password)))
            if parsed.username:
                secrets.update((parsed.username, unquote(parsed.username)))
            for secret in sorted((s for s in secrets if s), key=len, reverse=True):
                redacted = redacted.replace(secret, "<redacted>")
    return _REDIS_URL_RE.sub(r"\1<redacted>@", redacted)


__all__ = [
    "RedisSettings",
    "probe_redis",
    "redact_redis_error",
]
