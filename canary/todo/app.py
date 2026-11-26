#!/usr/bin/env python3
"""Canary Todo Service proving all six `infra2_sdk` modules end-to-end."""

from __future__ import annotations

import asyncio
import html
import logging
import math
import os
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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, StrictBool, StrictStr

from infra2_sdk import __version__ as _SDK_VERSION
from infra2_sdk.config import (
    EnvironmentManifest,
    EnvironmentTier,
    RuntimeEnvKey,
    assert_env,
    read_runtime_env,
)
from infra2_sdk.llm import (
    DecodingSpec,
    LlmSettings,
    create_llm_agent,
    llm_call_context,
    prompt_sha256,
    redact_llm_error,
    request_sha256,
    schema_sha256,
)
from infra2_sdk.service import (
    CallableCheck,
    Dependency,
    DependencyKind,
    DependencyManifest,
    DependencyStatus,
    HttpClientSettings,
    ProbeResult,
    RateLimitWindow,
    init_service,
    probe_http,
    run_probes,
)
from infra2_sdk.storage import (
    PostgresSettings,
    RedisSettings,
    S3Settings,
    probe_postgres,
    probe_redis,
    probe_s3,
    redact_postgres_error,
    redact_redis_error,
)
from infra2_sdk.telemetry import current_request_id, get_meter, get_tracer

try:
    from canary_schema import CANARY_CAPABILITY_ENUM_NAME, ENSURE_SCHEMA_SQL
    from canary_settings import CanarySettings
    from ui import HTML_TEMPLATE
except ImportError:
    _todo_dir = str(Path(__file__).resolve().parent)
    if _todo_dir not in sys.path:
        sys.path.insert(0, _todo_dir)
    from canary_schema import CANARY_CAPABILITY_ENUM_NAME, ENSURE_SCHEMA_SQL
    from canary_settings import CanarySettings
    from ui import HTML_TEMPLATE

LOGGER = logging.getLogger("platform-todo")
SERVICE_TRACER_NAME = "platform-todo"

CHECK_DEADLINE_SECONDS = 8.0
READINESS_TIMEOUT_SECONDS = CHECK_DEADLINE_SECONDS + 2.0

PASS = "pass"
FAIL = "fail"
UNCONFIGURED = "unconfigured"

GATING_CHECKS = ("postgres", "redis", "s3")
INFORMATIONAL_CHECKS = ("signoz", "openpanel", "authentik")

_STATUS_LOCK = threading.Lock()
_STATUS_CACHE: tuple[float, dict[str, Any]] | None = None
SERVER_START_TIME = time.time()


class _TodoStoreUnavailable(RuntimeError):
    """Raised when persistent PostgreSQL storage for todos is not configured."""


def _settings() -> CanarySettings:
    return CanarySettings.from_environ(os.environ)


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def _outcome(status: str, detail: str, *, latency_ms: float) -> dict[str, Any]:
    return {"status": status, "latency_ms": round(latency_ms, 1), "detail": detail}


def _to_outcome(res: ProbeResult) -> dict[str, Any]:
    status = PASS if res.status is DependencyStatus.PRESENT else FAIL
    return _outcome(status, res.detail, latency_ms=res.duration_ms)


def _unconfigured(start: float, detail: str) -> dict[str, Any]:
    return _outcome(UNCONFIGURED, detail, latency_ms=_elapsed_ms(start))


def _connect_todo_db() -> Any:
    raw = CanarySettings.read(os.environ, "database_url").strip()
    if not raw:
        raise _TodoStoreUnavailable("Missing PostgreSQL todo storage configuration")
    try:
        pg = PostgresSettings(url=raw, connect_timeout_seconds=5.0)
    except ValueError as exc:
        raise _TodoStoreUnavailable(redact_postgres_error(exc, raw)) from None
    import psycopg

    return psycopg.connect(pg.url, connect_timeout=5, options="-c statement_timeout=5000")


def _ensure_todos_schema(conn: Any) -> None:
    exists_row = conn.execute("SELECT to_regclass('public.todos')").fetchone()
    if not exists_row or not exists_row[0]:
        conn.execute(ENSURE_SCHEMA_SQL)


def _format_todo_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if value is None else str(value)


def _row_to_todo(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "title": str(row[1]),
        "completed": bool(row[2]),
        "capability": str(row[3] or "general"),
        "created_at": _format_todo_timestamp(row[4]),
    }


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
            "DELETE FROM todos WHERE id = %s RETURNING id, title, completed, capability, created_at",
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
    request_id: str | None = None
    status: str
    service: str
    domain: str
    environment: str = "unknown"
    configuration_sha256: str = "unknown"
    sdk_version: str = "unknown"
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


def _check_postgres(raw_url: str) -> dict[str, Any]:
    started = time.perf_counter()
    cleaned = raw_url.strip()
    if not cleaned:
        return _unconfigured(started, "DATABASE_URL is not configured")
    return _to_outcome(probe_postgres(cleaned, connect_timeout=2.0, verify_write=True))


def _check_redis(raw_url: str) -> dict[str, Any]:
    started = time.perf_counter()
    cleaned = raw_url.strip()
    if not cleaned:
        return _unconfigured(started, "REDIS_URL is not configured")
    try:
        settings = RedisSettings(url=cleaned, connect_timeout_seconds=2.0)
    except ValueError as exc:
        return _outcome(FAIL, redact_redis_error(exc, cleaned), latency_ms=_elapsed_ms(started))
    if not urllib.parse.urlsplit(settings.url).password:
        return _unconfigured(
            started, "REDIS_URL has no password; the canary cannot prove an authenticated write"
        )
    return _to_outcome(
        probe_redis(settings.url, key_prefix="canary:probe:", connect_timeout=2.0, write_probe=True)
    )


def _check_s3(cfg: CanarySettings) -> dict[str, Any]:
    started = time.perf_counter()
    missing = [
        name
        for name, val in (
            ("S3_ENDPOINT", cfg.s3_endpoint),
            ("S3_BUCKET", cfg.s3_bucket),
            ("S3_ACCESS_KEY", cfg.s3_access_key),
            ("S3_SECRET_KEY", cfg.s3_secret_key),
        )
        if not val
    ]
    if missing:
        return _unconfigured(started, f"{', '.join(missing)} not configured")
    try:
        settings = S3Settings(
            endpoint_url=cfg.s3_endpoint,
            bucket=cfg.s3_bucket,
            access_key_id=cfg.s3_access_key,
            secret_access_key=cfg.s3_secret_key,
            use_path_style=True,
            connect_timeout_seconds=3.0,
            read_timeout_seconds=3.0,
        )
    except ValueError as exc:
        return _outcome(FAIL, str(exc), latency_ms=_elapsed_ms(started))
    return _to_outcome(probe_s3(settings, key_prefix="canary/", round_trip=True))


async def _check_http(label: str, url: str) -> dict[str, Any]:
    started = time.perf_counter()
    if not url:
        return _unconfigured(started, f"{label} is not configured")
    res = await probe_http(url, name=label, settings=HttpClientSettings(timeout_seconds=3.0))
    ok = res.status is DependencyStatus.PRESENT and res.detail.startswith(("HTTP 2", "HTTP 3"))
    return _outcome(PASS if ok else FAIL, res.detail, latency_ms=res.duration_ms)


def _record_probe_telemetry(name: str, result: dict[str, Any]) -> ProbeResult:
    meter = get_meter(SERVICE_TRACER_NAME)
    attrs = {"canary.check": name, "canary.status": str(result.get("status", "unknown"))}
    meter.create_histogram("canary.probe.duration_ms", unit="ms").record(
        float(result.get("latency_ms") or 0.0), attrs
    )
    meter.create_counter("canary.probe.outcomes", unit="1").add(1, attrs)
    if result["status"] != PASS:
        LOGGER.warning("canary check %s is %s: %s", name, result["status"], result["detail"])
    status = DependencyStatus.PRESENT if result["status"] == PASS else DependencyStatus.ABSENT
    return ProbeResult(
        name=name,
        status=status,
        detail=str(result["detail"]),
        duration_ms=float(result.get("latency_ms") or 0.0),
    )


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
    """Execute all gating and informational dependency probes through `service.run_probes`."""
    cfg = _settings()
    outcomes: dict[str, dict[str, Any]] = {}
    tracer = get_tracer(SERVICE_TRACER_NAME)

    def _probe_check(name: str, fn: Callable[[], Any], *, is_async: bool = False) -> CallableCheck:
        async def _run() -> ProbeResult:
            with tracer.start_as_current_span(
                f"canary.check.{name}", attributes={"canary.check": name}
            ) as span:
                res = await fn() if is_async else await asyncio.to_thread(fn)
                span.set_attribute("canary.status", res["status"])
                span.set_attribute("canary.latency_ms", res["latency_ms"])
                outcomes[name] = res
                return _record_probe_telemetry(name, res)

        return CallableCheck(name, _run)

    checks = [
        _probe_check(
            "postgres", lambda: _check_postgres(CanarySettings.read(os.environ, "database_url"))
        ),
        _probe_check("redis", lambda: _check_redis(cfg.redis_url)),
        _probe_check("s3", lambda: _check_s3(cfg)),
        _probe_check(
            "signoz",
            lambda: _check_http("SIGNOZ_OTEL_COLLECTOR", cfg.signoz_otel_collector),
            is_async=True,
        ),
        _probe_check(
            "openpanel",
            lambda: _check_http(
                "OPENPANEL_API_URL", cfg.openpanel_health_url or cfg.openpanel_api_url
            ),
            is_async=True,
        ),
        _probe_check(
            "authentik",
            lambda: _check_http("AUTHENTIK_ENDPOINT", cfg.authentik_endpoint),
            is_async=True,
        ),
    ]

    with tracer.start_as_current_span("canary.run") as run_span:
        probe_results = asyncio.run(run_probes(checks, timeout_seconds=CHECK_DEADLINE_SECONDS))
        final_checks: dict[str, dict[str, Any]] = {}
        for res in probe_results:
            final_checks[res.name] = outcomes.get(
                res.name, _outcome(FAIL, res.detail, latency_ms=res.duration_ms)
            )
        unconfigured = [n for n in GATING_CHECKS if final_checks[n]["status"] == UNCONFIGURED]
        failed = [n for n in GATING_CHECKS if final_checks[n]["status"] not in (PASS, UNCONFIGURED)]
        all_pass = not failed and not unconfigured
        run_span.set_attribute("canary.ok", all_pass)
        if all_pass:
            LOGGER.info("canary run ok: %d write paths passed", len(GATING_CHECKS))
        else:
            LOGGER.error(
                "canary run red: failed=%s unconfigured=%s",
                ",".join(failed) or "-",
                ",".join(unconfigured) or "-",
            )

    return {
        "ok": all_pass,
        "unconfigured": unconfigured,
        "failed": failed,
        "service": "platform/todo",
        "domain": "todo.zitian.party",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gating": list(GATING_CHECKS),
        "informational": list(INFORMATIONAL_CHECKS),
        "checks": final_checks,
        "capabilities": _capability_summary(cfg),
    }


def cached_canary_status() -> dict[str, Any]:
    """Return cached canary status with single-flight locking."""
    global _STATUS_CACHE
    ttl = _settings().status_cache_ttl_seconds
    with _STATUS_LOCK:
        cached = _STATUS_CACHE
        if cached is None or time.monotonic() - cached[0] >= ttl:
            cached = (time.monotonic(), run_all_checks())
            _STATUS_CACHE = cached
        doc = dict(cached[1])
        doc["age_seconds"] = round(time.monotonic() - cached[0], 1)
        return doc


CANARY_DEPENDENCY_MANIFEST = DependencyManifest(
    dependencies=(
        Dependency(
            name="postgres",
            kind=DependencyKind.CODE_DOMINANT,
            required_in=frozenset(EnvironmentTier),
            env_vars=frozenset({"DATABASE_URL"}),
        ),
        Dependency(
            name="redis",
            kind=DependencyKind.CODE_DOMINANT,
            required_in=frozenset(EnvironmentTier),
            env_vars=frozenset({"REDIS_URL"}),
        ),
        Dependency(
            name="s3",
            kind=DependencyKind.CODE_DOMINANT,
            required_in=frozenset(EnvironmentTier),
            env_vars=frozenset({"S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY"}),
        ),
    )
)


class _CanaryDependencyCheck:
    def __init__(self, dep_name: str) -> None:
        self.name = dep_name

    def probe(self) -> ProbeResult:
        started = time.perf_counter()
        report = cached_canary_status()
        entry = (report.get("checks") or {}).get(self.name) or {}
        present = entry.get("status") == PASS
        return ProbeResult(
            name=self.name,
            status=DependencyStatus.PRESENT if present else DependencyStatus.ABSENT,
            detail=str(entry.get("detail") or entry.get("status") or "absent"),
            duration_ms=max(0.0, float(entry.get("latency_ms") or _elapsed_ms(started))),
        )


def _todo_health_extra(verdict: Mapping[str, Any] | None = None) -> dict[str, Any]:
    doc = dict(verdict) if verdict is not None else dict(_STATUS_CACHE[1]) if _STATUS_CACHE else {}
    canary_ok = doc.get("ok") is True
    return {
        "ok": canary_ok,
        "request_id": current_request_id(),
        "domain": "todo.zitian.party",
        "environment": read_runtime_env(RuntimeEnvKey.ENVIRONMENT) or "unknown",
        "configuration_sha256": read_runtime_env(RuntimeEnvKey.CONFIGURATION_SHA256) or "unknown",
        "sdk_version": _SDK_VERSION,
        "canary": {"ok": canary_ok, "checked_at": doc.get("timestamp")},
        "uptime_seconds": round(time.time() - SERVER_START_TIME, 1),
    }


def _db_unavailable_response(exc: Exception) -> JSONResponse | None:
    if not (
        isinstance(exc, (_TodoStoreUnavailable, OSError))
        or type(exc).__module__.startswith("psycopg")
    ):
        return None
    LOGGER.warning("Todo PostgreSQL storage unavailable: %s", exc)
    return JSONResponse(
        status_code=503, content={"error": "Todo database unavailable", "detail": str(exc)}
    )


def _run_todo_db(fn: Callable[[], Response]) -> Response:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        if (resp := _db_unavailable_response(exc)) is not None:
            return resp
        raise


fastapi_app = FastAPI(
    title="Canary Todo", description="Canary Service", docs_url="/docs", redoc_url=None
)
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_service(
    fastapi_app,
    name="platform-todo",
    checks=[_CanaryDependencyCheck(name) for name in GATING_CHECKS],
    manifest=CANARY_DEPENDENCY_MANIFEST,
    enable_otel=True,
    strict_otel=True,
    require_issued_identity=True,
    check_timeout_seconds=READINESS_TIMEOUT_SECONDS,
    suppress_probe_telemetry=True,
    health_extra=_todo_health_extra,
)

for _library_logger in ("httpx", "httpcore"):
    logging.getLogger(_library_logger).setLevel(logging.WARNING)


@fastapi_app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    del request
    first = exc.errors()[0] if exc.errors() else {}
    return JSONResponse(
        status_code=400, content={"error": first.get("msg", "Invalid request"), "detail": str(exc)}
    )


@fastapi_app.get(
    "/api/health",
    tags=["Observability"],
    response_model=TodoHealthResponse,
    responses={503: {"model": TodoHealthResponse}},
)
@fastapi_app.head("/api/health", include_in_schema=False)
def fastapi_api_health() -> Response:
    try:
        verdict = cached_canary_status()
    except Exception:  # noqa: BLE001
        verdict = {"ok": False}
    extra = _todo_health_extra(verdict)
    canary_ok = bool(extra["ok"])
    rel = getattr(fastapi_app.state, "service_release", {})
    return JSONResponse(
        status_code=200 if canary_ok else 503,
        content={
            **extra,
            "status": "healthy" if canary_ok else "unhealthy",
            "service": "platform/todo",
            "version": rel.get("version", "unknown"),
            "git_sha": rel.get("git_sha", "unknown"),
        },
    )


@fastapi_app.get(
    "/api/canary/status",
    tags=["Observability"],
    response_model=CanaryStatusResponse,
    responses={503: {"model": CanaryStatusResponse}},
)
@fastapi_app.head("/api/canary/status", include_in_schema=False)
def fastapi_api_canary_status() -> Response:
    result = cached_canary_status()
    return JSONResponse(status_code=200 if result.get("ok") else 503, content=result)


@fastapi_app.get(
    "/api/todos",
    tags=["Todos"],
    response_model=list[TodoItem],
    responses={503: {"model": TodoErrorResponse}},
)
@fastapi_app.head("/api/todos", include_in_schema=False)
def fastapi_get_todos() -> Response:
    return _run_todo_db(lambda: JSONResponse(status_code=200, content=_list_todos_db()))


@fastapi_app.post(
    "/api/todos",
    tags=["Todos"],
    status_code=201,
    response_model=TodoItem,
    responses={400: {"model": TodoErrorResponse}, 503: {"model": TodoErrorResponse}},
)
def fastapi_create_todo(payload: TodoCreateRequest) -> Response:
    title = payload.title.strip() if payload.title is not None else f"Todo #{int(time.time())}"
    if not title:
        return JSONResponse(status_code=400, content={"error": "Field 'title' must not be blank"})
    return _run_todo_db(
        lambda: JSONResponse(
            status_code=201,
            content=_create_todo_db(title, bool(payload.completed), str(payload.capability)),
        )
    )


@fastapi_app.patch(
    "/api/todos/{todo_id}",
    tags=["Todos"],
    response_model=TodoItem,
    responses={
        400: {"model": TodoErrorResponse},
        404: {"model": TodoErrorResponse},
        503: {"model": TodoErrorResponse},
    },
)
def fastapi_update_todo(
    todo_id: int, payload: TodoPatchRequest | None = Body(default=None)
) -> Response:
    patch = payload or TodoPatchRequest()
    title = patch.title.strip() if patch.title is not None else None
    if patch.title is not None and not title:
        return JSONResponse(status_code=400, content={"error": "Field 'title' must not be blank"})

    def _do() -> Response:
        updated = _patch_todo_db(
            todo_id, title=title, completed=patch.completed, capability=patch.capability
        )
        return (
            JSONResponse(status_code=200, content=updated)
            if updated
            else JSONResponse(status_code=404, content={"error": "Todo not found"})
        )

    return _run_todo_db(_do)


@fastapi_app.delete(
    "/api/todos/{todo_id}",
    tags=["Todos"],
    response_model=TodoDeleteResponse,
    responses={404: {"model": TodoErrorResponse}, 503: {"model": TodoErrorResponse}},
)
def fastapi_delete_todo(todo_id: int) -> Response:
    def _do() -> Response:
        removed = _delete_todo_db(todo_id)
        if removed:
            return JSONResponse(
                status_code=200, content={"ok": True, "deleted_id": todo_id, "item": removed}
            )
        return JSONResponse(status_code=404, content={"error": "Todo not found"})

    return _run_todo_db(_do)


TODO_SUMMARY_CALL_SITE = "todo.summary"
TODO_SUMMARY_DECODING = DecodingSpec(
    decoding_version="todo.summary.v1",
    thinking=False,
    temperature=0.0,
    max_tokens=512,
    timeout=20.0,
)
SUMMARY_RATE_LIMIT = RateLimitWindow(max_requests=30, window_seconds=60.0)


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
    """Summarize recent persisted todos through `infra2_sdk.llm.create_llm_agent`."""
    settings = LlmSettings.from_env(os.environ)
    if model_override is None and not (settings.api_key and settings.api_key.strip()):
        return TodoSummaryResponse(
            status=UNCONFIGURED,
            decoding_version=TODO_SUMMARY_DECODING.decoding_version,
            detail="Missing LLM_API_KEY",
        )

    bounded = max(1, min(int(limit), 50))
    items = (todos_loader or _list_todos_db)(bounded)[:bounded]
    lines = [
        f"- [{'done' if item.get('completed') else 'open'}] ({item.get('capability', 'general')}) {item.get('title', '')}"
        for item in items
    ]
    prompt_text = "Recent todos:\n" + ("\n".join(lines) if lines else "(none)")
    p_sha = prompt_sha256(prompt_text)
    s_sha = schema_sha256(TodoSummaryOutput)
    model_label = settings.role_model("small")
    r_sha = request_sha256(
        model=model_label, prompt=prompt_text, schema=s_sha, decoding=TODO_SUMMARY_DECODING
    )

    from pydantic_ai import UsageLimits

    eff_limits = usage_limits or UsageLimits(
        request_limit=1,
        output_tokens_limit=1024,
        total_tokens_limit=4096,
        cost_limit=Decimal("0.05"),
    )
    providers = getattr(fastapi_app.state, "telemetry_providers", None)
    eff_tp = (
        tracer_provider
        if tracer_provider is not None
        else getattr(providers, "tracer_provider", None)
    )
    eff_mp = (
        meter_provider if meter_provider is not None else getattr(providers, "meter_provider", None)
    )

    agent = create_llm_agent(
        settings,
        role="small",
        model=model_override,
        call_site=TODO_SUMMARY_CALL_SITE,
        decoding=TODO_SUMMARY_DECODING,
        output_type=TodoSummaryOutput,
        instructions="Summarize recent infrastructure todo items concisely. Report open_count, completed_count, a brief headline, and the next focus.",
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
        LOGGER.info("todo summary ok: todos=%d stream=%s model=%s", len(items), stream, model_label)

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
)
async def fastapi_summarize_todos(
    payload: TodoSummaryRequest | None = Body(default=None),
) -> Response:
    allowed, retry_after = SUMMARY_RATE_LIMIT.acquire()
    if not allowed:
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            content={
                "error": "Summary rate limit exceeded",
                "detail": "rate_limited",
                "request_id": current_request_id(),
            },
        )
    req = payload or TodoSummaryRequest()
    from pydantic_ai.exceptions import UsageLimitExceeded

    try:
        res = await summarize_recent_todos(limit=req.limit, stream=req.stream)
    except UsageLimitExceeded as exc:
        return JSONResponse(
            status_code=429,
            content={"error": "LLM usage limit exceeded", "detail": type(exc).__name__},
        )
    except Exception as exc:  # noqa: BLE001
        if (db_resp := _db_unavailable_response(exc)) is not None:
            return db_resp
        scrubbed = redact_llm_error(exc)
        LOGGER.warning("todo summary failed: %s", scrubbed)
        return JSONResponse(
            status_code=502, content={"error": "LLM summary failed", "detail": str(scrubbed)}
        )
    return JSONResponse(status_code=200, content=res.model_dump(mode="json"))


@fastapi_app.get("/api/auth/me", tags=["Auth"], response_model=AuthMeResponse)
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


@fastapi_app.get("/", tags=["UI"], response_class=HTMLResponse)
@fastapi_app.head("/", include_in_schema=False)
@fastapi_app.get("/index.html", include_in_schema=False, response_class=HTMLResponse)
@fastapi_app.head("/index.html", include_in_schema=False)
def fastapi_ui_index(request: Request) -> HTMLResponse:
    username = request.headers.get("X-authentik-username", "")
    user_display = request.headers.get("X-authentik-name", "") or username
    badge = f"SSO: {html.escape(user_display)}" if username else "Direct mode"
    resolved_sha = getattr(fastapi_app.state, "service_release", {}).get("git_sha", "unknown")
    sha = resolved_sha[:7] if resolved_sha != "unknown" else "unknown"
    return HTMLResponse(
        content=HTML_TEMPLATE.replace("${GIT_COMMIT_SHA}", sha).replace("${AUTH_BADGE}", badge),
        status_code=200,
    )


app = fastapi_app


def validate_startup_environment(environ: Mapping[str, str] | None = None) -> None:
    """Fail closed at startup when any required variable in CanarySettings is missing."""
    assert_env(
        EnvironmentManifest.from_model(CanarySettings, source="canary/todo"),
        environ=environ,
        require_injected=True,
    )


def main() -> None:
    validate_startup_environment()
    port = _settings().port
    import uvicorn

    LOGGER.info("Canary Todo starting on port %d", port)
    uvicorn.run(fastapi_app, host="0.0.0.0", port=port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    main()
