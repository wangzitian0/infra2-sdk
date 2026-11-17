"""FastAPI integration: /livez, /readyz, /health routes and OpenTelemetry bootstrap."""

from __future__ import annotations

import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING, Any

from infra2_sdk.runtime.environment import (
    _PLATFORM_OWNED_TIERS,
    EnvironmentConflictError,
    EnvironmentTier,
    RuntimeEnvKey,
    _UnknownEnvironmentPolicy,
    environment_from_env,
    resolve_runtime_env,
    runtime_env_spec,
    strict_environment_from_env,
)
from infra2_sdk.runtime.health import (
    _HTTP_OK,
    DependencyCheck,
    DependencyManifest,
    DependencyStatus,
    ProbeResult,
    _elapsed,
    _HealthStatus,
    check_health,
)

if TYPE_CHECKING:
    from infra2_sdk.runtime.otel import OtelSettings

_logger = logging.getLogger("infra2_sdk.fastapi")
_INITIALIZED_ATTRIBUTE = "infra2_service_name"
_CheckerResult = bool | tuple[bool, str]
_RELAX_HINT = "pass strict_otel=False to log the problem and start"

try:
    from pydantic import BaseModel, ConfigDict
except ImportError:  # pragma: no cover
    _HAS_PYDANTIC = False
else:
    _HAS_PYDANTIC = True

    class ProbeResponse(BaseModel):
        status: str
        service: str
        reasons: list[str] | None = None

    class CheckDetail(BaseModel):
        status: str
        detail: str
        duration_ms: float
        required: bool

    class HealthResponse(BaseModel):
        model_config = ConfigDict(extra="allow")
        status: str
        service: str
        version: str
        git_sha: str
        checks: dict[str, CheckDetail]
        reasons: list[str]

    class ErrorResponse(BaseModel):
        error_id: str
        detail: str
        request_id: str | None = None


async def _run_rid(inner: Any, scope: Any, receive: Any, send: Any) -> None:
    if scope["type"] != "http":
        await inner(scope, receive, send)
        return
    from infra2_sdk.runtime import otel as _o

    hdr = dict(scope.get("headers") or ()).get(b"x-request-id", b"")
    rid = hdr.decode("latin1").strip() or str(uuid.uuid4())
    scope.setdefault("state", {})["request_id"] = rid
    tok = _o._ctx_rid(rid)

    async def _send(msg: Any) -> None:
        if msg["type"] == "http.response.start":
            h = [p for p in msg.get("headers", ()) if p[0].lower() != b"x-request-id"]
            msg = {**msg, "headers": [*h, (b"x-request-id", rid.encode("latin1"))]}
        await send(msg)

    try:
        await inner(scope, receive, _send)
    finally:
        _o._ctx_rid(None, tok)


class _ProbeAccessFilter(logging.Filter):
    """Drop uvicorn access log records for standard probe paths."""

    def filter(self, record: logging.LogRecord) -> bool:
        from infra2_sdk.runtime.otel import PROBE_PATHS

        if isinstance(record.args, tuple) and len(record.args) >= 3:
            path = str(record.args[2]).split("?", 1)[0]
            if path in PROBE_PATHS:
                return False
        msg = record.getMessage()
        return not any(f" {p} " in msg or f' "{p} ' in msg for p in PROBE_PATHS)


class _DatabaseCheck:
    name = "database"

    def __init__(self, checker: Callable[[], Any]) -> None:
        self._checker = checker

    def probe(self) -> ProbeResult | Awaitable[ProbeResult]:
        started = time.perf_counter()
        outcome = self._checker()
        return (
            self._finish(outcome, started)
            if inspect.isawaitable(outcome)
            else self._result(outcome, started)
        )

    async def _finish(self, outcome: Awaitable[Any], started: float) -> ProbeResult:
        return self._result(await outcome, started)

    def _result(self, outcome: Any, started: float) -> ProbeResult:
        ok, detail = outcome if isinstance(outcome, tuple) else (outcome, "")
        status = DependencyStatus.PRESENT if ok else DependencyStatus.ABSENT
        fallback = "ok" if ok else "db_checker returned a false value"
        return ProbeResult(self.name, status, str(detail or fallback), _elapsed(started))


def _in_deployed_tier() -> bool:
    try:
        tier = environment_from_env(unknown=_UnknownEnvironmentPolicy.PRODUCTION).tier
    except ValueError:
        return True
    return tier in _PLATFORM_OWNED_TIERS


def _with_relax_hint(exc: ValueError) -> ValueError:
    return type(exc)(f"{exc}; {_RELAX_HINT}")


def _require_known_tier() -> None:
    try:
        strict_environment_from_env()
    except EnvironmentConflictError as exc:
        raise _with_relax_hint(exc) from exc
    except ValueError as exc:
        spec = runtime_env_spec(RuntimeEnvKey.ENVIRONMENT)
        names = ", ".join((spec.key.value, *spec.aliases))
        raise ValueError(
            f"{exc}; set one of {names} to a known tier, or pass strict_otel=False for a local run"
        ) from exc


def _log_missing_endpoint(*, deployed: bool) -> None:
    _logger.log(
        logging.WARNING if deployed else logging.DEBUG,
        "%s is not set; OpenTelemetry export is off",
        RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT.value,
    )


def _resolve_telemetry(
    name: str,
    *,
    strict: bool,
    require_issued_identity: bool = False,
    suppress_probe_telemetry: bool = False,
) -> tuple[OtelSettings | None, str, OtelSettings | None]:
    from infra2_sdk.runtime.otel import OtelSettings

    if strict and not require_issued_identity:
        _require_known_tier()
    try:
        settings = OtelSettings.from_env(
            strict=strict,
            default_service_name=name,
            suppress_probe_telemetry=suppress_probe_telemetry,
            require_issued_identity=require_issued_identity,
        )
    except Exception as exc:
        if strict:
            if isinstance(exc, ValueError):
                raise _with_relax_hint(exc) from exc
            raise
        if resolve_runtime_env(None, RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT).value is None:
            _log_missing_endpoint(deployed=_in_deployed_tier())
        else:
            _logger.warning("Could not initialize OpenTelemetry: %s", exc)
        return None, EnvironmentTier.LOCAL_DEV.value, None
    tier = settings.environment
    if settings.sdk_disabled:
        _logger.debug("OpenTelemetry is disabled by the environment")
        return None, tier, settings
    if settings.endpoint is None:
        _log_missing_endpoint(deployed=EnvironmentTier(tier) in _PLATFORM_OWNED_TIERS)
        return None, tier, settings
    return settings, tier, settings


def _start_telemetry(app: Any, settings: OtelSettings, *, strict: bool) -> None:
    try:
        from infra2_sdk.runtime.otel import configure_telemetry

        providers = configure_telemetry(settings, set_global=True)
        app.state.telemetry_providers = providers
        original = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(scope_app: Any):
            try:
                async with original(scope_app) as state:
                    yield state
            finally:
                providers.shutdown()

        app.router.lifespan_context = lifespan
        _logger.info(
            "OpenTelemetry started for '%s' -> %s", settings.service_name, settings.endpoint
        )
    except Exception as exc:
        if strict:
            raise
        _logger.warning("Could not initialize OpenTelemetry: %s", exc)


def init_service(
    app: Any,
    name: str,
    *,
    db_checker: Callable[[], _CheckerResult | Awaitable[_CheckerResult]] | None = None,
    checks: Iterable[DependencyCheck] = (),
    manifest: DependencyManifest | None = None,
    enable_otel: bool = True,
    strict_otel: bool = True,
    require_issued_identity: bool = False,
    suppress_probe_telemetry: bool = True,
    log_level: int | None = logging.INFO,
    check_timeout_seconds: float = 10.0,
    health_extra: Callable[[], Mapping[str, Any]] | None = None,
) -> None:
    """Mount ``/livez``, ``/readyz``, ``/health`` and init telemetry."""
    try:
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        raise ImportError("fastapi is required for infra2_sdk.fastapi.init_service") from exc
    if hasattr(app.state, _INITIALIZED_ATTRIBUTE):
        raise RuntimeError("init_service was already called for this app")
    if not check_timeout_seconds > 0:
        raise ValueError("check_timeout_seconds must be positive")
    from infra2_sdk.runtime import otel as _otel

    all_checks = ([_DatabaseCheck(db_checker)] if db_checker else []) + list(checks)
    _otel._chk_name(name)
    settings, tier, parsed = (
        _resolve_telemetry(
            name,
            strict=strict_otel,
            require_issued_identity=require_issued_identity,
            suppress_probe_telemetry=suppress_probe_telemetry,
        )
        if enable_otel
        else (None, environment_from_env().tier.value, None)
    )
    release = _otel._setup_logging(name, parsed, tier=tier, level=log_level)
    app.state.service_release = dict(release)
    setattr(app.state, _INITIALIZED_ATTRIBUTE, name)
    if suppress_probe_telemetry:
        access = logging.getLogger("uvicorn.access")
        if not any(isinstance(f, _ProbeAccessFilter) for f in access.filters):
            access.addFilter(_ProbeAccessFilter())
        if isinstance(tel := getattr(app, "_telemetry", None), dict):
            prev = tel.get("exclude")
            tel["exclude"] = lambda s, p=prev: s.get("path") in _otel.PROBE_PATHS or (p and p(s))

    _orig = app.build_middleware_stack
    app.build_middleware_stack, app.middleware_stack = (lambda: partial(_run_rid, _orig())), None

    async def report() -> tuple[int, dict[str, Any]]:
        if not all_checks and manifest is None:
            return _HTTP_OK, {"status": _HealthStatus.HEALTHY.value, "checks": {}, "reasons": []}
        kw: dict[str, Any] = {"timeout_seconds": check_timeout_seconds}
        if manifest is not None:
            kw.update(manifest=manifest, tier=tier)
        return await check_health(all_checks, **kw)

    t = ["Observability"]

    @app.get("/livez", tags=t, response_model=ProbeResponse)
    def livez():
        return JSONResponse(status_code=200, content={"status": "alive", "service": name})

    @app.get(
        "/readyz", tags=t, response_model=ProbeResponse, responses={503: {"model": ProbeResponse}}
    )
    async def readyz():
        code, body = await report()
        c: dict[str, Any] = {
            "status": "ready" if code == _HTTP_OK else "not_ready",
            "service": name,
        }
        if code != _HTTP_OK:
            c["reasons"] = body["reasons"]
        return JSONResponse(status_code=code, content=c)

    @app.get(
        "/health", tags=t, response_model=HealthResponse, responses={503: {"model": HealthResponse}}
    )
    async def health():
        code, body = await report()
        extra = health_extra() if health_extra else {}
        return JSONResponse(status_code=code, content={**extra, **body, "service": name, **release})

    if settings is not None:
        _start_telemetry(app, settings, strict=strict_otel)


__all__ = (
    ["CheckDetail", "ErrorResponse", "HealthResponse", "ProbeResponse", "init_service"]
    if _HAS_PYDANTIC
    else ["init_service"]
)
