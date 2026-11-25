#!/usr/bin/env python3
"""Canary Todo List & Infrastructure Validation Service.

Serves todo.zitian.party as the acceptance and verification tool for the platform's
real runtime paths:
- Postgres: login with `DATABASE_URL`, then `SET LOCAL transaction_read_only = off`, `SELECT 1`,
  and a temporary-table `canary_todo_probe` `CREATE`/`INSERT`/`SELECT`/`DELETE` roundtrip
- Redis: AUTH, then SETEX/GET/DEL of a random value
- S3: PUT, GET and DELETE of one object under a canary prefix
- SigNoz, OpenPanel, Authentik: liveness over HTTP
- Startup environment manifest validation (`validate_startup_environment` via `assert_environment`)

`/api/canary/status` runs these checks. The probe runner reads it over the Docker
network (ProbeFacet `todo-canary-status`). Traefik does not route it without SSO.
The service sends traces, metrics, and logs to SigNoz when the deploy issues an OTLP endpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import math
import os
import secrets
import signal
import socket
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, StrictBool, StrictStr

from infra2_sdk.fastapi import init_service
from infra2_sdk.runtime.config_schema import assert_environment
from infra2_sdk.runtime.environment import (
    RuntimeEnvKey,
    resolve_runtime_env,
)
from infra2_sdk.runtime.health import (
    DependencyStatus,
    ProbeResult,
    run_probes,
)
from infra2_sdk.runtime.llm import (
    DecodingSpec,
    LlmSettings,
    create_llm_agent,
    llm_call_context,
    prompt_sha256,
    redact_llm_error,
    request_sha256,
    schema_sha256,
)

LOGGER = logging.getLogger("platform-todo")
SERVICE_TRACER_NAME = "platform-todo"

# Object key prefix for the S3 write probe. The canary credential is scoped to one bucket.
CANARY_S3_PREFIX = "canary/"
# The probe runner waits ProbeFacet.timeout_seconds (15 s). One hung dependency must not
# hold the status document past it, so each check gets this hard deadline.
CHECK_DEADLINE_SECONDS = 8.0
# /readyz and /health run one canary status call. A cold call waits for one full run.
# A run ends at CHECK_DEADLINE_SECONDS at the latest. The SDK counts a longer call as a
# failure and gives no reason. The timeout stays above the deadline, so the reasons show.
READINESS_TIMEOUT_SECONDS = CHECK_DEADLINE_SECONDS + 2.0

# Check states. `unconfigured` means a credential the check needs is missing: the check
# is red (a gap is never green) but it is not a failure of the path it would prove.
PASS = "pass"
FAIL = "fail"
UNCONFIGURED = "unconfigured"

# Only the canary's own write paths gate `ok` and the HTTP status. SigNoz, OpenPanel and
# Authentik have their own probes and severities, so they stay informational here.
GATING_CHECKS = ("postgres", "redis", "s3")
INFORMATIONAL_CHECKS = ("signoz", "openpanel", "authentik")

# Each run does real writes and opens a Postgres connection, and the canary role allows 5.
# One run is in flight at a time, and a result is reused for this long. It stays below the
# probe runner's 60 s interval, so every probe round still sees a fresh run.
STATUS_CACHE_TTL_SECONDS = 30.0
_STATUS_LOCK = threading.Lock()
_STATUS_CACHE: tuple[float, dict[str, Any]] | None = None


try:
    from canary_schema import CANARY_CAPABILITY_ENUM_NAME
    from canary_settings import CanarySettings
    from ui import HTML_TEMPLATE
except ImportError:
    _todo_dir = str(Path(__file__).resolve().parent)
    if _todo_dir not in sys.path:
        sys.path.insert(0, _todo_dir)
    from canary_schema import CANARY_CAPABILITY_ENUM_NAME
    from canary_settings import CanarySettings
    from ui import HTML_TEMPLATE


def _settings() -> CanarySettings:
    """The one reader of the process environment in this module (read at each call)."""
    return CanarySettings.from_environ(os.environ)


class _TodoStoreUnavailable(RuntimeError):
    """Raised when persistent PostgreSQL storage for todos is not configured."""


def _is_psycopg_error(exc: Exception) -> bool:
    return type(exc).__module__.startswith("psycopg")


def _format_todo_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if value is None:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return str(value)


def _row_to_todo(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "title": str(row[1]),
        "completed": bool(row[2]),
        "capability": str(row[3] or "general"),
        "created_at": _format_todo_timestamp(row[4]),
    }


_DATABASE_URL_UNSET = "DATABASE_URL is not configured"
_DATABASE_URL_INVALID = (
    "DATABASE_URL is not a PostgreSQL URL with host, user and password "
    "(percent-encode reserved characters)"
)


def _database_target() -> tuple[str, int, str, str, str]:
    """Return host, port, user, password and database name from `DATABASE_URL`.

    The todo store and the Postgres probe use this one login. Only these five
    parts are read: query parameters are ignored. Raise `ValueError` with a
    message that has no part of the value when the URL is unset or malformed.
    """
    # One field only: a malformed REDIS_PORT is not a database fault.
    raw = CanarySettings.read(os.environ, "database_url").strip()
    if not raw:
        raise ValueError(_DATABASE_URL_UNSET)
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port or 5432
    except ValueError:
        raise ValueError(_DATABASE_URL_INVALID) from None
    host = parts.hostname or ""
    user = urllib.parse.unquote(parts.username or "")
    password = urllib.parse.unquote(parts.password or "")
    if parts.scheme.split("+", 1)[0] not in {"postgresql", "postgres"} or not (
        host and user and password
    ):
        raise ValueError(_DATABASE_URL_INVALID)
    return host, port, user, password, urllib.parse.unquote(parts.path.lstrip("/")) or "postgres"


def _connect_todo_db():
    try:
        host, port, user, password, dbname = _database_target()
    except ValueError as exc:
        raise _TodoStoreUnavailable(
            f"Missing PostgreSQL todo storage configuration: {exc}"
        ) from None
    import psycopg

    return psycopg.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        dbname=dbname,
        connect_timeout=5,
        options="-c statement_timeout=5000",
    )


def _ensure_todos_schema(conn: Any) -> None:
    """Create the todos table only if absent (superusers in preview/local DBs); deployed todo_app relies on TodoDeployer."""
    exists_row = conn.execute("SELECT to_regclass('public.todos')").fetchone()
    if exists_row and exists_row[0]:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS todos (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            completed BOOLEAN NOT NULL DEFAULT FALSE,
            capability TEXT NOT NULL DEFAULT 'general',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def _list_todos_db(limit: int | None = None) -> list[dict[str, Any]]:
    with _connect_todo_db() as conn:
        _ensure_todos_schema(conn)
        if limit is None:
            rows = conn.execute(
                "SELECT id, title, completed, capability, created_at FROM todos ORDER BY id ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title, completed, capability, created_at FROM todos ORDER BY id DESC LIMIT %s",
                (limit,),
            ).fetchall()
        return [_row_to_todo(r) for r in rows]


def _recent_todos_db(limit: int = 20) -> list[dict[str, Any]]:
    bounded = max(1, min(int(limit), 50))
    return _list_todos_db(limit=bounded)[:bounded]


def _create_todo_db(title: str, completed: bool, capability: str) -> dict[str, Any]:
    with _connect_todo_db() as conn:
        _ensure_todos_schema(conn)
        row = conn.execute(
            "INSERT INTO todos (title, completed, capability) VALUES (%s, %s, %s) "
            "RETURNING id, title, completed, capability, created_at",
            (title, completed, capability),
        ).fetchone()
        if not row:
            raise _TodoStoreUnavailable("INSERT into todos returned no row")
        return _row_to_todo(row)


def _patch_todo_db(
    todo_id: int,
    *,
    title: str | None = None,
    completed: bool | None = None,
    capability: str | None = None,
) -> dict[str, Any] | None:
    with _connect_todo_db() as conn:
        _ensure_todos_schema(conn)
        updates: list[str] = []
        params: list[Any] = []
        if title is not None:
            updates.append("title = %s")
            params.append(title)
        if completed is not None:
            updates.append("completed = %s")
            params.append(completed)
        if capability is not None:
            updates.append("capability = %s")
            params.append(capability)
        if not updates:
            updates.append("completed = NOT completed")
        params.append(todo_id)
        row = conn.execute(
            f"UPDATE todos SET {', '.join(updates)} WHERE id = %s "
            "RETURNING id, title, completed, capability, created_at",
            tuple(params),
        ).fetchone()
        return _row_to_todo(row) if row else None


def _delete_todo_db(todo_id: int) -> dict[str, Any] | None:
    with _connect_todo_db() as conn:
        _ensure_todos_schema(conn)
        row = conn.execute(
            "DELETE FROM todos WHERE id = %s "
            "RETURNING id, title, completed, capability, created_at",
            (todo_id,),
        ).fetchone()
        return _row_to_todo(row) if row else None


class TodoErrorResponse(BaseModel):
    error: str
    detail: str | None = None


class TodoItem(BaseModel):
    id: int
    title: str
    completed: bool
    capability: str = "general"
    created_at: str


class TodoCreateRequest(BaseModel):
    title: StrictStr | None = None
    completed: StrictBool = False
    capability: StrictStr = "general"


class TodoPatchRequest(BaseModel):
    title: StrictStr | None = None
    completed: StrictBool | None = None
    capability: StrictStr | None = None


class TodoDeleteResponse(BaseModel):
    ok: bool
    deleted_id: int
    item: TodoItem


class CanaryHealthSummary(BaseModel):
    ok: bool
    checked_at: str | None = None


class TodoHealthResponse(BaseModel):
    ok: bool
    status: str
    service: str
    domain: str
    environment: str = "unknown"
    configuration_sha256: str = "unknown"
    version: str
    git_sha: str
    canary: CanaryHealthSummary
    uptime_seconds: float


class CanaryCheckOutcome(BaseModel):
    status: str
    latency_ms: float | None = None
    detail: str


class CanaryStatusResponse(BaseModel):
    ok: bool
    unconfigured: list[str]
    failed: list[str]
    service: str
    domain: str
    timestamp: str
    gating: list[str]
    informational: list[str]
    checks: dict[str, CanaryCheckOutcome]
    age_seconds: float | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)


class AuthMeResponse(BaseModel):
    authenticated: bool
    username: str | None = None
    name: str | None = None
    email: str | None = None
    groups: list[str]


class TodoSummaryOutput(BaseModel):
    headline: str
    open_count: int
    completed_count: int
    focus: str


class TodoSummaryRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=50)
    stream: StrictBool = False


class TodoSummaryResponse(BaseModel):
    status: str
    summary: TodoSummaryOutput | None = None
    todo_count: int = 0
    model: str | None = None
    decoding_version: str
    prompt_sha256: str | None = None
    request_sha256: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    detail: str = ""


for _model in (
    TodoErrorResponse,
    TodoItem,
    TodoCreateRequest,
    TodoPatchRequest,
    TodoDeleteResponse,
    CanaryHealthSummary,
    TodoHealthResponse,
    CanaryCheckOutcome,
    CanaryStatusResponse,
    AuthMeResponse,
    TodoSummaryOutput,
    TodoSummaryRequest,
    TodoSummaryResponse,
):
    _model.model_rebuild(_types_namespace=globals())


class _ProbeFailure(Exception):
    """A check failed for a stated reason. The message is safe to publish."""


class _RedisReplyError(Exception):
    """Redis answered with an error reply (`-ERR`, `-NOAUTH`, `-WRONGPASS`, ...)."""


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def _outcome(status: str, start: float, detail: str) -> dict[str, Any]:
    return {"status": status, "latency_ms": _elapsed_ms(start), "detail": detail}


def _unconfigured(start: float, detail: str) -> dict[str, Any]:
    return _outcome(UNCONFIGURED, start, detail)


def _probe_postgres(
    host: str,
    port: int = 5432,
    *,
    user: str = "",
    password: str = "",
    dbname: str = "postgres",
    timeout: float = 2.0,
    connector: Callable[..., Any] | None = None,
    verify_write: bool = True,
) -> dict[str, Any]:
    """Log in with the `DATABASE_URL` login. Set the transaction read-write, run `SELECT 1`, then a temp-table write/read/delete roundtrip.

    A server that answers the login with an ErrorResponse (`E`), such as "too many
    clients", fails the check. The password never appears in the detail: the SDK
    redacts the DSN from error text.
    """
    start = time.perf_counter()
    if not (user and password):
        return _unconfigured(
            start,
            _DATABASE_URL_UNSET,
        )
    try:
        from infra2_sdk.runtime.postgres import (
            PostgresSettings,
            probe_postgres,
        )

        dsn = (
            f"postgresql://{urllib.parse.quote(user, safe='')}"
            f":{urllib.parse.quote(password, safe='')}"
            f"@{f'[{host}]' if ':' in host else host}:{int(port)}"
            f"/{urllib.parse.quote(dbname, safe='')}"
        )
        settings = PostgresSettings(
            dsn=dsn, connect_timeout_seconds=max(1, min(60, math.ceil(timeout)))
        )
        if not verify_write:
            result = probe_postgres(settings, connector=connector)
            if result.status != DependencyStatus.PRESENT:
                return _outcome("fail", start, f"login as {user} failed: {result.detail}")
            return _outcome("pass", start, f"login as {user}: {result.detail}")

        class _CrudSession:
            def __init__(self, raw: Any) -> None:
                self._raw = raw
                self._conn: Any = None

            def __enter__(self) -> Any:
                self._conn = self._raw.__enter__() if hasattr(self._raw, "__enter__") else self._raw
                return self._conn

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
                try:
                    if exc_type is None and self._conn is not None:
                        self._conn.execute("SELECT 1").fetchone()
                        self._conn.execute(
                            "CREATE TEMP TABLE IF NOT EXISTS canary_todo_probe (id TEXT PRIMARY KEY, title TEXT NOT NULL)"
                        )
                        self._conn.execute(
                            "INSERT INTO canary_todo_probe (id, title) VALUES ('probe-1', 'canary-write-ok') ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title"
                        )
                        row = self._conn.execute(
                            "SELECT title FROM canary_todo_probe WHERE id = 'probe-1'"
                        ).fetchone()
                        self._conn.execute("DELETE FROM canary_todo_probe WHERE id = 'probe-1'")
                        if not row or row[0] != "canary-write-ok":
                            raise _ProbeFailure(
                                f"SELECT returned {row!r} instead of ('canary-write-ok',)"
                            )
                finally:
                    if hasattr(self._raw, "__exit__"):
                        self._raw.__exit__(exc_type, exc, tb)
                    target = self._conn if self._conn is not None else self._raw
                    if target is not None and hasattr(target, "close"):
                        with contextlib.suppress(Exception):
                            target.close()
                return False

        def _crud_connector(psycopg_dsn: str, **kwargs: Any) -> _CrudSession:
            if connector is None:
                import psycopg

                kwargs.setdefault(
                    "options",
                    f"-c statement_timeout={int(settings.statement_timeout_seconds * 1000)}",
                )
                return _CrudSession(psycopg.connect(psycopg_dsn, **kwargs))
            return _CrudSession(connector(psycopg_dsn, **kwargs))

        result = probe_postgres(
            settings,
            connector=_crud_connector,
            query="SET LOCAL transaction_read_only = off",
        )
        if result.status != DependencyStatus.PRESENT:
            return _outcome("fail", start, f"login as {user} failed: {result.detail}")
        return _outcome(
            "pass",
            start,
            f"login as {user}: SELECT 1 and write/read/delete transaction succeeded",
        )
    except Exception as exc:  # noqa: BLE001 - a probe reports the failure
        return _outcome("fail", start, f"{type(exc).__name__}: {exc}")


def probe_postgres_crud(
    host: str,
    port: int = 5432,
    *,
    user: str = "",
    password: str = "",
    dbname: str = "postgres",
    timeout: float = 5.0,
    connector: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Execute authenticated Postgres login plus a temp-table write/read/delete transaction."""
    return _probe_postgres(
        host,
        port,
        user=user,
        password=password,
        dbname=dbname,
        timeout=timeout,
        connector=connector,
        verify_write=True,
    )


async def _probe_http_async(url: str, timeout: float = 3.0) -> dict[str, Any]:
    from infra2_sdk.runtime.http import HttpClientSettings, probe_http

    start = time.perf_counter()
    try:
        result = await probe_http(url, settings=HttpClientSettings(timeout_seconds=timeout))
    except Exception as exc:
        return _outcome("fail", start, str(exc))
    if result.status != DependencyStatus.PRESENT or not result.detail.startswith(
        ("HTTP 2", "HTTP 3")
    ):
        return _outcome("fail", start, result.detail)
    return _outcome("pass", start, result.detail)


def _probe_http(url: str, timeout: float = 3.0) -> dict[str, Any]:
    return asyncio.run(_probe_http_async(url, timeout=timeout))


_DEFAULT_PROBE_HTTP = _probe_http


def _resp_command(*parts: str) -> bytes:
    out = [b"*%d\r\n" % len(parts)]
    for part in parts:
        data = part.encode("utf-8")
        out.append(b"$%d\r\n%s\r\n" % (len(data), data))
    return b"".join(out)


def _read_reply(rf) -> str | int | bytes | None:
    """Read one RESP reply: a simple string, an integer, a bulk string or nil."""
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
        return rf.read(size + 2)[:-2]
    raise ConnectionError(f"unexpected RESP reply {line[:20]!r}")


def _probe_redis(
    host: str,
    port: int = 6379,
    password: str | None = None,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """AUTH, PING, then SETEX, GET and DEL of a random value on a per-run key.

    No password reports `unconfigured`: the check stays red, but it is a configuration
    gap, not a failed write path. A `-NOAUTH` reply fails the check: the old probe counted
    that reply as a pass and never wrote to Redis in production. The random value stops a
    stale key from the previous run passing a broken write.
    """
    start = time.perf_counter()
    if not host:
        return _unconfigured(start, "REDIS_HOST is not configured")
    if not password:
        return _unconfigured(
            start,
            "REDIS_PASSWORD is not configured; the canary cannot prove an authenticated write",
        )
    key = f"canary:probe:{secrets.token_hex(8)}"
    value = secrets.token_hex(16)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with sock.makefile("rb") as rf:

                def call(step: str, *parts: str) -> str | int | bytes | None:
                    sock.sendall(_resp_command(*parts))
                    try:
                        return _read_reply(rf)
                    except _RedisReplyError as err:
                        raise _ProbeFailure(f"{step} failed: {err}") from None

                call("AUTH", "AUTH", password)
                if call("PING", "PING") != "PONG":
                    raise _ProbeFailure("PING did not answer PONG")
                call("SETEX", "SETEX", key, "10", value)
                got = call("GET", "GET", key)
                if got != value.encode("utf-8"):
                    raise _ProbeFailure(f"GET returned {got!r} instead of the value written")
                call("DEL", "DEL", key)
        return _outcome("pass", start, "AUTH, PING, SETEX/GET/DEL verified")
    except _ProbeFailure as exc:
        return _outcome("fail", start, str(exc))
    except Exception as exc:  # noqa: BLE001 - a probe reports the failure
        return _outcome("fail", start, f"{type(exc).__name__}: {exc}")


def _probe_s3(
    *,
    endpoint_url: str,
    bucket: str,
    access_key: str,
    secret_key: str,
    timeout: float = 3.0,
    client: Any | None = None,
) -> dict[str, Any]:
    """PUT, GET and DELETE one object under the canary prefix, signed with the canary key.

    Liveness passed while writes failed in the past. A denied or corrupt write, read or
    delete fails the check. A written object is deleted even when the read-back fails.
    """
    start = time.perf_counter()
    missing = [
        name
        for name, value in (
            ("S3_ENDPOINT", endpoint_url),
            ("S3_BUCKET", bucket),
            ("S3_ACCESS_KEY", access_key),
            ("S3_SECRET_KEY", secret_key),
        )
        if not value
    ]
    if missing:
        return _unconfigured(start, f"{', '.join(missing)} not configured")
    key = f"{CANARY_S3_PREFIX}{secrets.token_hex(8)}"
    body = secrets.token_hex(16).encode("utf-8")
    owned = client is None
    try:
        from infra2_sdk.runtime.s3 import (
            S3Settings,
            create_s3_client,
            put_object_bytes,
            read_object_bytes,
        )

        s3 = client or create_s3_client(
            S3Settings(
                bucket=bucket,
                endpoint_url=endpoint_url,
                access_key_id=access_key,
                secret_access_key=secret_key,
                addressing_style="path",
                connect_timeout_seconds=timeout,
                read_timeout_seconds=timeout,
            )
        )
    except Exception as exc:  # noqa: BLE001 - a probe reports the failure
        return _outcome("fail", start, f"S3 client setup failed: {type(exc).__name__}: {exc}")
    failure: str | None = None
    written = False
    try:
        try:
            put_object_bytes(s3, bucket=bucket, key=key, data=body)
        except Exception as exc:  # noqa: BLE001
            raise _ProbeFailure(f"PUT failed: {exc}") from exc
        written = True
        try:
            got = read_object_bytes(s3, bucket=bucket, key=key)
        except Exception as exc:  # noqa: BLE001
            raise _ProbeFailure(f"GET failed: {exc}") from exc
        if got != body:
            raise _ProbeFailure("GET returned data that differs from the PUT body")
    except _ProbeFailure as exc:
        failure = str(exc)
    if written:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            failure = failure or f"DELETE failed: {exc}"
    if owned and hasattr(s3, "close"):
        with contextlib.suppress(Exception):
            s3.close()
    if failure:
        return _outcome("fail", start, failure)
    return _outcome("pass", start, "PUT, GET and DELETE verified under canary prefix")


def _otlp_active() -> bool:
    return bool(
        (resolve_runtime_env(None, RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT).value or "").strip()
    )


class _NullSpan:
    def set_attribute(self, key: str, value: Any) -> None:
        return None


def _span(name: str, **attributes: Any):
    """A span context manager. It is a no-op when no OTLP endpoint was issued.

    The tracer comes from the global provider that `init_service` installs at import.
    """
    if not _otlp_active():
        return contextlib.nullcontext(_NullSpan())
    from opentelemetry import trace

    return trace.get_tracer(SERVICE_TRACER_NAME).start_as_current_span(
        name, attributes=attributes or None
    )


def _record_check_telemetry(name: str, result: dict[str, Any]) -> dict[str, Any]:
    if _otlp_active():
        from opentelemetry import metrics as otel_metrics

        meter = otel_metrics.get_meter(SERVICE_TRACER_NAME)
        attrs = {
            "canary.check": name,
            "canary.status": str(result.get("status", "unknown")),
        }
        meter.create_histogram(
            "canary.probe.duration_ms",
            unit="ms",
            description="Latency of each canary dependency probe in milliseconds",
        ).record(float(result.get("latency_ms") or 0.0), attrs)
        meter.create_counter(
            "canary.probe.outcomes",
            unit="1",
            description="Count of canary dependency probe outcomes by check and status",
        ).add(1, attrs)
    if result["status"] != PASS:
        LOGGER.warning("canary check %s is %s: %s", name, result["status"], result["detail"])
    return result


def _traced_check(name: str, probe: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    with _span(f"canary.check.{name}", **{"canary.check": name}) as span:
        try:
            result = probe()
        except Exception as exc:  # noqa: BLE001 - one crashed probe must not hide the rest
            result = {
                "status": "fail",
                "latency_ms": 0.0,
                "detail": f"probe raised {type(exc).__name__}: {exc}",
            }
        span.set_attribute("canary.status", result["status"])
        span.set_attribute("canary.latency_ms", result["latency_ms"])
        span.set_attribute("canary.detail", result["detail"])
    return _record_check_telemetry(name, result)


async def _traced_check_async(name: str, probe: Callable[[], Any]) -> dict[str, Any]:
    with _span(f"canary.check.{name}", **{"canary.check": name}) as span:
        try:
            result = await probe()
        except Exception as exc:  # noqa: BLE001 - one crashed probe must not hide the rest
            result = {
                "status": "fail",
                "latency_ms": 0.0,
                "detail": f"probe raised {type(exc).__name__}: {exc}",
            }
        span.set_attribute("canary.status", result["status"])
        span.set_attribute("canary.latency_ms", result["latency_ms"])
        span.set_attribute("canary.detail", result["detail"])
    return _record_check_telemetry(name, result)


def _to_probe_result(name: str, raw: dict[str, Any]) -> ProbeResult:
    status = DependencyStatus.PRESENT if raw.get("status") == PASS else DependencyStatus.ABSENT
    return ProbeResult(
        name,
        status,
        str(raw.get("detail") or ""),
        float(raw.get("latency_ms") or 0.0),
    )


def _run_checks(
    plan: dict[str, Callable[[], Any]],
) -> dict[str, dict[str, Any]]:
    """Run every check in parallel through infra2_sdk.runtime.health.run_probes."""
    raw_outcomes: dict[str, dict[str, Any]] = {}
    checks: list[Any] = []
    for name, fn in plan.items():
        if asyncio.iscoroutinefunction(fn):

            class _AsyncCheck:
                def __init__(self, check_name: str, check_fn: Callable[[], Any]) -> None:
                    self.name = check_name
                    self._fn = check_fn

                async def probe(self) -> ProbeResult:
                    raw = await _traced_check_async(self.name, self._fn)
                    raw_outcomes[self.name] = raw
                    return _to_probe_result(self.name, raw)

            checks.append(_AsyncCheck(name, fn))
        else:

            class _SyncCheck:
                def __init__(self, check_name: str, check_fn: Callable[[], dict[str, Any]]) -> None:
                    self.name = check_name
                    self._fn = check_fn

                def probe(self) -> ProbeResult:
                    raw = _traced_check(self.name, self._fn)
                    raw_outcomes[self.name] = raw
                    return _to_probe_result(self.name, raw)

            checks.append(_SyncCheck(name, fn))

    coro = run_probes(checks, timeout_seconds=CHECK_DEADLINE_SECONDS)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        probe_results = asyncio.run(coro)
    else:
        import contextvars

        holder: list[Any] = []
        err_holder: list[BaseException] = []
        ctx = contextvars.copy_context()

        def _loop_runner() -> None:
            try:
                holder.append(ctx.run(asyncio.run, coro))
            except BaseException as exc:  # noqa: BLE001
                err_holder.append(exc)

        runner_thread = threading.Thread(target=_loop_runner, name="canary-probe-loop", daemon=True)
        runner_thread.start()
        runner_thread.join()
        if err_holder:
            raise err_holder[0]
        probe_results = holder[0]
    results: dict[str, dict[str, Any]] = {}
    for res in probe_results:
        if res.name in raw_outcomes:
            results[res.name] = raw_outcomes[res.name]
        else:
            detail = (
                f"check did not finish within {CHECK_DEADLINE_SECONDS:g}s"
                if res.detail.startswith("probe timed out after ")
                else res.detail
            )
            LOGGER.warning("canary check %s failed: %s", res.name, detail)
            results[res.name] = {
                "status": "fail",
                "latency_ms": round(CHECK_DEADLINE_SECONDS * 1000, 1),
                "detail": detail,
            }
    return {name: results[name] for name in plan}


def _capability_summary(cfg: CanarySettings) -> dict[str, Any]:
    return {
        "openpanel_configured": bool(cfg.openpanel_client_id),
        "openpanel_api_url_set": bool(cfg.openpanel_api_url),
        "openpanel_script_url_set": bool(cfg.openpanel_script_url),
        "browser_otel_configured": bool(cfg.next_public_otel_exporter_otlp_endpoint),
        "browser_deployment_environment": cfg.next_public_deployment_environment or None,
        "browser_git_sha": cfg.next_public_git_sha or None,
        "schema_enum": CANARY_CAPABILITY_ENUM_NAME,
    }


def run_all_checks() -> dict[str, Any]:
    """Run the real probes against the platform's infrastructure capabilities."""
    cfg = _settings()
    pg_problem = ""
    pg_host, pg_port, pg_user, pg_password, pg_database = "", 5432, "", "", "postgres"
    try:
        pg_host, pg_port, pg_user, pg_password, pg_database = _database_target()
    except ValueError as exc:
        pg_problem = str(exc)
    redis_host, redis_port = cfg.redis_host, cfg.redis_port
    redis_password = cfg.redis_password or None
    s3_endpoint, s3_bucket = cfg.s3_endpoint, cfg.s3_bucket
    s3_access_key, s3_secret_key = cfg.s3_access_key, cfg.s3_secret_key
    signoz_url = cfg.signoz_otel_collector
    openpanel_url = cfg.openpanel_health_url or cfg.openpanel_api_url
    authentik_url = cfg.authentik_endpoint

    def _http_entry(name: str, url: str) -> Callable[[], Any]:
        if not url:
            return lambda: _unconfigured(time.perf_counter(), f"{name} is not configured")
        if _probe_http is _DEFAULT_PROBE_HTTP:

            async def _run_http() -> dict[str, Any]:
                return await _probe_http_async(url)

            return _run_http
        return lambda: _probe_http(url)

    plan: dict[str, Callable[[], Any]] = {
        "postgres": lambda: (
            _outcome("fail", time.perf_counter(), pg_problem)
            if pg_problem == _DATABASE_URL_INVALID
            else _probe_postgres(
                pg_host, pg_port, user=pg_user, password=pg_password, dbname=pg_database
            )
        ),
        "redis": lambda: _probe_redis(redis_host, redis_port, password=redis_password),
        "s3": lambda: _probe_s3(
            endpoint_url=s3_endpoint,
            bucket=s3_bucket,
            access_key=s3_access_key,
            secret_key=s3_secret_key,
        ),
        "signoz": _http_entry("SIGNOZ_OTEL_COLLECTOR", signoz_url),
        "openpanel": _http_entry("OPENPANEL_API_URL", openpanel_url),
        "authentik": _http_entry("AUTHENTIK_ENDPOINT", authentik_url),
    }
    with _span("canary.run") as run_span:
        results = _run_checks(plan)
        checks = {
            "postgres": results["postgres"],
            "redis": results["redis"],
            "s3": results["s3"],
            "signoz": results["signoz"],
            "openpanel": results["openpanel"],
            "authentik": results["authentik"],
        }
        unconfigured = [n for n in GATING_CHECKS if results[n]["status"] == UNCONFIGURED]
        failed = [n for n in GATING_CHECKS if results[n]["status"] not in (PASS, UNCONFIGURED)]
        informational_down = [n for n in INFORMATIONAL_CHECKS if results[n]["status"] != PASS]
        # A status other than `pass` or `unconfigured` counts as a failure (fail closed).
        all_pass = not failed and not unconfigured
        run_span.set_attribute("canary.ok", all_pass)
        run_span.set_attribute("canary.failed_checks", ",".join(failed))
        run_span.set_attribute("canary.unconfigured_checks", ",".join(unconfigured))
        run_span.set_attribute("canary.informational_down", ",".join(informational_down))
        # inside the span, so the exported log record carries the trace id
        if all_pass:
            LOGGER.info("canary run ok: %d write paths passed", len(GATING_CHECKS))
        else:
            LOGGER.error(
                "canary run red: failed=%s unconfigured=%s",
                ",".join(failed) or "-",
                ",".join(unconfigured) or "-",
            )
        if informational_down:
            LOGGER.warning(
                "canary informational checks not passing: %s",
                ",".join(informational_down),
            )
    # The probe runner keeps only the first 128 bytes of this body as its observed text, so
    # the verdict and its classification come first.
    return {
        "ok": all_pass,
        "unconfigured": unconfigured,
        "failed": failed,
        "service": "platform/todo",
        "domain": "todo.zitian.party",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gating": list(GATING_CHECKS),
        "informational": list(INFORMATIONAL_CHECKS),
        "checks": checks,
        "capabilities": _capability_summary(cfg),
    }


def cached_canary_status() -> dict[str, Any]:
    """The status document, with one run in flight and a short-lived reuse.

    Requests queue on the lock. The first one runs the probes. The others get that result
    until STATUS_CACHE_TTL_SECONDS pass. `age_seconds` says how old the result is, so a
    reused result never reads as a fresh one.
    """
    global _STATUS_CACHE
    with _STATUS_LOCK:
        cached = _STATUS_CACHE
        if cached is None or time.monotonic() - cached[0] >= STATUS_CACHE_TTL_SECONDS:
            report = run_all_checks()
            cached = (time.monotonic(), report)
            _STATUS_CACHE = cached
        document = dict(cached[1])
        document["age_seconds"] = round(time.monotonic() - cached[0], 1)
        return document


def peek_canary_verdict() -> dict[str, Any]:
    """The last status document without a probe run; empty before the first run."""
    cached = _STATUS_CACHE  # one atomic read; the lock is held during a slow run
    return dict(cached[1]) if cached else {}


SERVER_START_TIME = time.time()


def _canary_readiness_checker() -> tuple[bool, str]:
    """Check readiness of Canary write paths for /readyz and /health."""
    try:
        report = cached_canary_status()
        if report.get("ok"):
            return True, "canary write paths passed"
        failed = report.get("failed") or []
        unconfigured = report.get("unconfigured") or []
        reasons = []
        if failed:
            reasons.append(f"failed={','.join(failed)}")
        if unconfigured:
            reasons.append(f"unconfigured={','.join(unconfigured)}")
        return False, "; ".join(reasons) or "canary checks not ok"
    except Exception as exc:
        return False, f"canary status check failed: {exc}"


def _installed_sdk_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("infra2-sdk")
    except PackageNotFoundError:
        return "unknown"


def _todo_health_extra() -> dict[str, Any]:
    verdict = peek_canary_verdict()
    env_name = resolve_runtime_env(None, RuntimeEnvKey.ENVIRONMENT).value or "unknown"
    config_sha = resolve_runtime_env(None, RuntimeEnvKey.CONFIGURATION_SHA256).value or "unknown"
    return {
        "ok": True,
        "domain": "todo.zitian.party",
        "environment": env_name,
        "configuration_sha256": config_sha,
        # The installed SDK. The infra2 staging gate compares it with the image pin (#247).
        "sdk_version": _installed_sdk_version(),
        "canary": {
            "ok": verdict.get("ok") is True,
            "checked_at": verdict.get("timestamp"),
        },
        "uptime_seconds": round(time.time() - SERVER_START_TIME, 1),
    }


def _db_unavailable_response(exc: Exception) -> JSONResponse:
    LOGGER.warning("Todo PostgreSQL storage unavailable: %s", exc)
    return JSONResponse(
        status_code=503,
        content={"error": "Todo database unavailable", "detail": str(exc)},
    )


# ---------------------------------------------------------------------------
# FastAPI runtime with turnkey infra2-sdk observability and lifecycle hooks
# ---------------------------------------------------------------------------

fastapi_app = FastAPI(
    title="Canary Todo",
    description="Canary Infrastructure Validation Service",
    docs_url="/docs",
    redoc_url=None,
)


# The one OpenTelemetry bootstrap (#1209). init_service mounts /livez, /readyz and /health.
# It sets the root logger to INFO before the OpenTelemetry handler attaches.
# It starts traces, metrics and logs when the endpoint is set and the SDK is not disabled.
# strict_otel stops the service on a malformed OTEL_* value.
# require_issued_identity stops it on such an endpoint without the issued identity.
# The tier is the issued deployment.environment.name. The SDK does not read ENV.
init_service(
    fastapi_app,
    name="platform-todo",
    db_checker=_canary_readiness_checker,
    enable_otel=True,
    strict_otel=True,
    require_issued_identity=True,
    check_timeout_seconds=READINESS_TIMEOUT_SECONDS,
    suppress_probe_telemetry=True,
    health_extra=_todo_health_extra,
)


@fastapi_app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    del request
    errors = exc.errors()
    first = errors[0] if errors else {}
    err_type = first.get("type", "")
    loc = first.get("loc", ())
    if err_type == "json_invalid":
        message = "Invalid JSON body"
    elif "completed" in loc:
        message = "Field 'completed' must be a boolean"
    elif "title" in loc:
        message = "Field 'title' must be a string"
    elif "capability" in loc:
        message = "Field 'capability' must be a string"
    else:
        message = first.get("msg", "Invalid request body")
    return JSONResponse(
        status_code=400,
        content={"error": message, "detail": str(exc)},
    )


# The probes call httpx, and httpx logs one INFO line for each successful request. With the
# root logger at INFO, those lines were 75% of the exported log lines (#1436). The libraries
# keep their WARNING and ERROR lines. The service logs each failed check itself, with the
# reason, and it logs the canary result line for each run.
for _library_logger in ("httpx", "httpcore"):
    logging.getLogger(_library_logger).setLevel(logging.WARNING)


@fastapi_app.middleware("http")
async def add_cors_headers(request: Request, call_next: Any) -> Response:
    if request.method == "OPTIONS":
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": ("GET, POST, PATCH, PUT, DELETE, OPTIONS, HEAD"),
                "Access-Control-Allow-Headers": ("Content-Type, Authorization"),
                "Content-Length": "0",
            },
        )
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = (
        "GET, POST, PATCH, PUT, DELETE, OPTIONS, HEAD"
    )
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


@fastapi_app.get(
    "/api/health",
    tags=["Observability"],
    response_model=TodoHealthResponse,
    operation_id="get_api_health",
)
@fastapi_app.head("/api/health", include_in_schema=False)
def fastapi_api_health() -> Response:
    rel = getattr(fastapi_app.state, "service_release", {})
    payload = {
        **_todo_health_extra(),
        "status": "healthy",
        "service": "platform/todo",
        "version": rel.get("version", "unknown"),
        "git_sha": rel.get("git_sha", "unknown"),
    }
    return JSONResponse(status_code=200, content=payload)


@fastapi_app.get(
    "/api/canary/status",
    tags=["Observability"],
    response_model=CanaryStatusResponse,
    responses={503: {"model": CanaryStatusResponse}},
    operation_id="get_canary_status",
)
@fastapi_app.head("/api/canary/status", include_in_schema=False)
def fastapi_api_canary_status() -> Response:
    result = cached_canary_status()
    status_code = 200 if result.get("ok") else 503
    return JSONResponse(status_code=status_code, content=result)


@fastapi_app.get(
    "/api/todos",
    tags=["Todos"],
    response_model=list[TodoItem],
    responses={503: {"model": TodoErrorResponse}},
    operation_id="list_todos",
)
@fastapi_app.head("/api/todos", include_in_schema=False)
def fastapi_get_todos() -> Response:
    try:
        items = _list_todos_db()
    except Exception as exc:  # noqa: BLE001 - fail closed as 503 only for DB outages
        if isinstance(exc, (_TodoStoreUnavailable, OSError)) or _is_psycopg_error(exc):
            return _db_unavailable_response(exc)
        raise
    return JSONResponse(status_code=200, content=items)


@fastapi_app.post(
    "/api/todos",
    tags=["Todos"],
    status_code=201,
    response_model=TodoItem,
    responses={
        400: {"model": TodoErrorResponse},
        503: {"model": TodoErrorResponse},
    },
    operation_id="create_todo",
)
def fastapi_create_todo(payload: TodoCreateRequest) -> Response:
    title = payload.title.strip() if payload.title is not None else f"Todo #{int(time.time())}"
    if not title:
        return JSONResponse(status_code=400, content={"error": "Field 'title' must not be blank"})
    try:
        item = _create_todo_db(title, bool(payload.completed), str(payload.capability))
    except Exception as exc:  # noqa: BLE001 - fail closed as 503 only for DB outages
        if isinstance(exc, (_TodoStoreUnavailable, OSError)) or _is_psycopg_error(exc):
            return _db_unavailable_response(exc)
        raise
    return JSONResponse(status_code=201, content=item)


@fastapi_app.patch(
    "/api/todos/{todo_id}",
    tags=["Todos"],
    response_model=TodoItem,
    responses={
        400: {"model": TodoErrorResponse},
        404: {"model": TodoErrorResponse},
        503: {"model": TodoErrorResponse},
    },
    operation_id="update_todo",
)
def fastapi_update_todo(
    todo_id: int,
    payload: TodoPatchRequest | None = Body(default=None),
) -> Response:
    patch = payload or TodoPatchRequest()
    title = patch.title.strip() if patch.title is not None else None
    if patch.title is not None and not title:
        return JSONResponse(status_code=400, content={"error": "Field 'title' must not be blank"})
    try:
        updated = _patch_todo_db(
            todo_id,
            title=title,
            completed=patch.completed,
            capability=patch.capability,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed as 503 only for DB outages
        if isinstance(exc, (_TodoStoreUnavailable, OSError)) or _is_psycopg_error(exc):
            return _db_unavailable_response(exc)
        raise
    if updated is not None:
        return JSONResponse(status_code=200, content=updated)
    return JSONResponse(status_code=404, content={"error": "Todo not found"})


@fastapi_app.delete(
    "/api/todos/{todo_id}",
    tags=["Todos"],
    response_model=TodoDeleteResponse,
    responses={
        404: {"model": TodoErrorResponse},
        503: {"model": TodoErrorResponse},
    },
    operation_id="delete_todo",
)
def fastapi_delete_todo(todo_id: int) -> Response:
    try:
        removed = _delete_todo_db(todo_id)
    except Exception as exc:  # noqa: BLE001 - fail closed as 503 only for DB outages
        if isinstance(exc, (_TodoStoreUnavailable, OSError)) or _is_psycopg_error(exc):
            return _db_unavailable_response(exc)
        raise
    if removed is not None:
        return JSONResponse(
            status_code=200,
            content={"ok": True, "deleted_id": todo_id, "item": removed},
        )
    return JSONResponse(status_code=404, content={"error": "Todo not found"})


TODO_SUMMARY_CALL_SITE = "todo.summary"
TODO_SUMMARY_DECODING = DecodingSpec(
    "todo.summary.v1",
    thinking=False,
    temperature=0.0,
    max_tokens=512,
    timeout=20.0,
)


def _telemetry_providers() -> tuple[Any | None, Any | None]:
    providers = getattr(fastapi_app.state, "telemetry_providers", None)
    return (
        getattr(providers, "tracer_provider", None),
        getattr(providers, "meter_provider", None),
    )


async def summarize_recent_todos(
    *,
    limit: int = 20,
    stream: bool = False,
    model_override: Any | None = None,
    todos_loader: Callable[[int], list[dict[str, Any]]] | None = None,
    tracer_provider: Any | None = None,
    meter_provider: Any | None = None,
    max_attempts: int = 3,
    usage_limits: Any | None = None,
) -> TodoSummaryResponse:
    """Summarize recent persisted todos through the single SDK Agent entrypoint."""
    settings = LlmSettings.from_mapping(os.environ)
    if model_override is None and not (settings.api_key and settings.api_key.strip()):
        return TodoSummaryResponse(
            status=UNCONFIGURED,
            decoding_version=TODO_SUMMARY_DECODING.decoding_version,
            detail="Missing LLM_API_KEY",
        )

    bounded_limit = max(1, min(int(limit), 50))
    items = (todos_loader or _recent_todos_db)(bounded_limit)
    lines = [
        f"- [{'done' if item.get('completed') else 'open'}] ({item.get('capability', 'general')}) {item.get('title', '')}"
        for item in items
    ]
    prompt_text = "Recent todos:\n" + ("\n".join(lines) if lines else "(none)")
    p_sha = prompt_sha256(prompt_text)
    s_sha = schema_sha256(TodoSummaryOutput)
    model_label = settings.role_model("small")
    r_sha = request_sha256(
        model=model_label,
        prompt=prompt_text,
        schema=s_sha,
        decoding=TODO_SUMMARY_DECODING,
    )

    from pydantic_ai import UsageLimits

    eff_limits = usage_limits or UsageLimits(
        request_limit=1,
        output_tokens_limit=1024,
        total_tokens_limit=4096,
        cost_limit=Decimal("0.05"),
    )
    default_tp, default_mp = _telemetry_providers()
    eff_tp = tracer_provider if tracer_provider is not None else default_tp
    eff_mp = meter_provider if meter_provider is not None else default_mp
    agent = create_llm_agent(
        settings,
        role="small",
        model=model_override,
        call_site=TODO_SUMMARY_CALL_SITE,
        decoding=TODO_SUMMARY_DECODING,
        output_type=TodoSummaryOutput,
        instructions=(
            "Summarize recent infrastructure todo items concisely. "
            "Report open_count, completed_count, a brief headline, and the next focus."
        ),
        usage_limits=eff_limits,
        max_attempts=max_attempts,
        tracer_provider=eff_tp,
        meter_provider=eff_mp,
    )

    with llm_call_context(
        call_site=TODO_SUMMARY_CALL_SITE,
        decoding_version=TODO_SUMMARY_DECODING.decoding_version,
        prompt_sha256=p_sha,
    ):
        if stream:
            async with agent.run_stream(prompt_text) as stream_res:
                summary_out = await stream_res.get_output()
                usage = stream_res.usage
        else:
            run_res = await agent.run(prompt_text)
            summary_out = run_res.output
            usage = run_res.usage
        LOGGER.info(
            "todo summary ok: todos=%d stream=%s model=%s",
            len(items),
            stream,
            model_label,
        )

    return TodoSummaryResponse(
        status=PASS,
        summary=summary_out,
        todo_count=len(items),
        model=model_label,
        decoding_version=TODO_SUMMARY_DECODING.decoding_version,
        prompt_sha256=p_sha,
        request_sha256=r_sha,
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        detail=f"summarized {len(items)} todo item(s)",
    )


@fastapi_app.post(
    "/api/todos/summary",
    tags=["Todos"],
    response_model=TodoSummaryResponse,
    responses={
        429: {"model": TodoErrorResponse},
        502: {"model": TodoErrorResponse},
        503: {"model": TodoErrorResponse},
    },
    operation_id="summarize_todos",
)
async def fastapi_summarize_todos(
    payload: TodoSummaryRequest | None = Body(default=None),
) -> Response:
    req = payload or TodoSummaryRequest()
    from pydantic_ai.exceptions import UsageLimitExceeded

    try:
        res = await summarize_recent_todos(limit=req.limit, stream=req.stream)
    except UsageLimitExceeded as exc:
        return JSONResponse(
            status_code=429,
            content={"error": "LLM usage limit exceeded", "detail": type(exc).__name__},
        )
    except Exception as exc:  # noqa: BLE001 - fail closed for DB outages and scrub LLM faults
        if isinstance(exc, (_TodoStoreUnavailable, OSError)) or _is_psycopg_error(exc):
            return _db_unavailable_response(exc)
        scrubbed = redact_llm_error(exc)
        LOGGER.warning("todo summary failed: %s", scrubbed)
        return JSONResponse(
            status_code=502,
            content={"error": "LLM summary failed", "detail": str(scrubbed)},
        )
    return JSONResponse(status_code=200, content=res.model_dump(mode="json"))


@fastapi_app.get(
    "/api/auth/me",
    tags=["Auth"],
    response_model=AuthMeResponse,
    operation_id="get_auth_me",
)
@fastapi_app.head("/api/auth/me", include_in_schema=False)
def fastapi_auth_me(request: Request) -> Response:
    username = request.headers.get("X-authentik-username", "")
    groups = [
        g.strip() for g in request.headers.get("X-authentik-groups", "").split(",") if g.strip()
    ]
    return JSONResponse(
        status_code=200,
        content={
            "authenticated": bool(username),
            "username": username or None,
            "name": request.headers.get("X-authentik-name") or None,
            "email": request.headers.get("X-authentik-email") or None,
            "groups": groups,
        },
    )


@fastapi_app.get("/", tags=["UI"], response_class=HTMLResponse, operation_id="get_ui_index")
@fastapi_app.head("/", include_in_schema=False)
@fastapi_app.get("/index.html", include_in_schema=False, response_class=HTMLResponse)
@fastapi_app.head("/index.html", include_in_schema=False)
def fastapi_ui_index(request: Request) -> HTMLResponse:
    username = request.headers.get("X-authentik-username", "")
    name = request.headers.get("X-authentik-name", "")
    email = request.headers.get("X-authentik-email", "")
    user_display = name or username or email
    if username:
        safe_display = html.escape(user_display)
        auth_badge = (
            f'<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">'
            f'<span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>'
            f"SSO 认证通过: {safe_display}"
            f"</span>"
        )
    else:
        auth_badge = (
            '<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium bg-slate-800 text-slate-400 border border-slate-700">'
            '<span class="w-2 h-2 rounded-full bg-slate-500"></span>'
            "未检测到 ForwardAuth 头 (开发/直连模式)"
            "</span>"
        )
    resolved_sha = getattr(fastapi_app.state, "service_release", {}).get("git_sha", "unknown")
    sha = resolved_sha[:7] if resolved_sha != "unknown" else "unknown"
    rendered = HTML_TEMPLATE.replace("${GIT_COMMIT_SHA}", sha).replace("${AUTH_BADGE}", auth_badge)
    return HTMLResponse(content=rendered, status_code=200)


# Standard ASGI entrypoint
app = fastapi_app


ENV_MANIFEST_PATH = Path(__file__).resolve().parent / "env.manifest.json"


def validate_startup_environment(environ: Mapping[str, str] | None = None) -> None:
    """Fail closed at startup when any required variable in env.manifest.json is missing."""
    assert_environment(ENV_MANIFEST_PATH, environ=environ, require_injected=True)


def main() -> None:
    validate_startup_environment()
    port = _settings().port
    LOGGER.info("Canary Todo service starting on port %d with FastAPI and infra2_sdk", port)
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    import uvicorn

    uvicorn.run(fastapi_app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
