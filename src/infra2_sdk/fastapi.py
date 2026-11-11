"""FastAPI integration: /livez, /readyz, /health routes and OpenTelemetry bootstrap."""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
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
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_INITIALIZED_ATTRIBUTE = "infra2_service_name"
_CheckerResult = bool | tuple[bool, str]
_RELAX_HINT = "pass strict_otel=False to log the problem and start"


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
    if isinstance(exc, EnvironmentConflictError):
        return EnvironmentConflictError(f"{exc}; {_RELAX_HINT}")
    return ValueError(f"{exc}; {_RELAX_HINT}")


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
) -> tuple[OtelSettings | None, str]:
    from infra2_sdk.runtime.otel import OtelSettings

    if strict and not require_issued_identity:
        _require_known_tier()
    try:
        kwargs: dict[str, Any] = {
            "strict": strict,
            "default_service_name": name,
            "suppress_probe_telemetry": suppress_probe_telemetry,
        }
        if require_issued_identity:
            kwargs["require_issued_identity"] = True
        settings = OtelSettings.from_env(**kwargs)
    except Exception as exc:
        if strict:
            if isinstance(exc, ValueError):
                raise _with_relax_hint(exc) from exc
            raise
        if resolve_runtime_env(None, RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT).value is None:
            _log_missing_endpoint(deployed=_in_deployed_tier())
        else:
            _logger.warning("Could not initialize OpenTelemetry: %s", exc)
        return None, EnvironmentTier.LOCAL_DEV.value
    tier = settings.environment
    if settings.sdk_disabled:
        _logger.debug("OpenTelemetry is disabled by the environment")
        return None, tier
    if settings.endpoint is None:
        _log_missing_endpoint(deployed=EnvironmentTier(tier) in _PLATFORM_OWNED_TIERS)
        return None, tier
    return settings, tier


def _start_telemetry(
    app: Any, settings: OtelSettings, *, strict: bool, log_level: int | None
) -> None:
    try:
        from infra2_sdk.runtime.otel import configure_telemetry

        if log_level is not None:
            logging.basicConfig(level=log_level, format=_LOG_FORMAT)
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
    suppress_probe_telemetry: bool = False,
    log_level: int | None = logging.INFO,
    check_timeout_seconds: float = 10.0,
) -> None:
    """Register ``/livez``, ``/readyz``, and ``/health`` routes and initialize OpenTelemetry."""
    try:
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        raise ImportError("fastapi is required to use infra2_sdk.fastapi.init_service") from exc
    if hasattr(app.state, _INITIALIZED_ATTRIBUTE):
        raise RuntimeError("init_service was already called for this app")
    if not check_timeout_seconds > 0:
        raise ValueError("check_timeout_seconds must be positive")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must not be blank")
    all_checks: list[DependencyCheck] = []
    if db_checker is not None:
        all_checks.append(_DatabaseCheck(db_checker))
    all_checks.extend(checks)

    settings, resolved_tier = (
        _resolve_telemetry(
            name,
            strict=strict_otel,
            require_issued_identity=require_issued_identity,
            suppress_probe_telemetry=suppress_probe_telemetry,
        )
        if enable_otel
        else (None, environment_from_env().tier.value)
    )
    setattr(app.state, _INITIALIZED_ATTRIBUTE, name)
    if suppress_probe_telemetry:
        access_logger = logging.getLogger("uvicorn.access")
        if not any(isinstance(f, _ProbeAccessFilter) for f in access_logger.filters):
            access_logger.addFilter(_ProbeAccessFilter())

    async def report() -> tuple[int, dict[str, Any]]:
        if not all_checks and manifest is None:
            return _HTTP_OK, {"status": _HealthStatus.HEALTHY.value, "checks": {}, "reasons": []}
        if manifest is not None:
            return await check_health(
                all_checks,
                manifest=manifest,
                tier=resolved_tier,
                timeout_seconds=check_timeout_seconds,
            )
        return await check_health(all_checks, timeout_seconds=check_timeout_seconds)

    @app.get("/livez", tags=["Observability"], include_in_schema=False)
    def livez():
        return JSONResponse(status_code=200, content={"status": "alive", "service": name})

    @app.get("/readyz", tags=["Observability"], include_in_schema=False)
    async def readyz():
        status_code, body = await report()
        if status_code == _HTTP_OK:
            return JSONResponse(content={"status": "ready", "service": name})
        return JSONResponse(
            status_code=status_code,
            content={"status": "not_ready", "service": name, "reasons": body["reasons"]},
        )

    @app.get("/health", tags=["Observability"], include_in_schema=True)
    async def health():
        status_code, body = await report()
        return JSONResponse(status_code=status_code, content={**body, "service": name})

    if settings is not None:
        _start_telemetry(app, settings, strict=strict_otel, log_level=log_level)


__all__ = ["init_service"]
