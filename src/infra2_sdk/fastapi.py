"""FastAPI integration for infra2 services: probe routes and one OpenTelemetry bootstrap.

``init_service`` registers three top-level routes (``ops.observability`` section 5.1):

- ``/livez``: liveness. It never calls a dependency.
- ``/readyz``: readiness. It answers 503 with reasons when the database check fails.
- ``/health``: the ``infra2_sdk.runtime.health`` report, plus the ``service`` name.

It also calls ``infra2_sdk.runtime.otel.configure_telemetry``. OTLP carries traces, metrics and
logs. The SDK shuts the providers down when the application lifespan ends. The SDK does not
serve a metrics endpoint.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from infra2_sdk.runtime.environment import (
    PLATFORM_OWNED_TIERS,
    RuntimeEnvKey,
    UnknownEnvironmentPolicy,
    environment_from_env,
    resolve_runtime_env,
)
from infra2_sdk.runtime.health import (
    HTTP_OK,
    DependencyStatus,
    HealthStatus,
    ProbeResult,
    check_health,
)

logger = logging.getLogger("infra2_sdk.fastapi")

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

_CheckerResult = bool | tuple[bool, str]


class _DatabaseCheck:
    """Adapt ``db_checker`` to the ``DependencyCheck`` protocol of ``runtime.health``."""

    name = "database"

    def __init__(self, checker: Callable[[], Any]) -> None:
        self._checker = checker

    def probe(self) -> ProbeResult | Awaitable[ProbeResult]:
        # run_probes calls this method in a worker thread. It awaits an awaitable result on
        # the event loop, so a sync checker never blocks the loop and an async checker works.
        started = time.perf_counter()
        outcome = self._checker()
        if inspect.isawaitable(outcome):
            return self._finish(outcome, started)
        return self._result(outcome, started)

    async def _finish(self, outcome: Awaitable[Any], started: float) -> ProbeResult:
        return self._result(await outcome, started)

    def _result(self, outcome: Any, started: float) -> ProbeResult:
        ok, detail = outcome if isinstance(outcome, tuple) else (outcome, "")
        status = DependencyStatus.PRESENT if ok else DependencyStatus.ABSENT
        fallback = "ok" if ok else "db_checker returned a false value"
        elapsed_ms = (time.perf_counter() - started) * 1000
        return ProbeResult(self.name, status, str(detail or fallback), elapsed_ms)


def _in_deployed_tier() -> bool:
    """Return True for preview, staging and production.

    An unknown or conflicting environment counts as deployed, so the caller logs more, not less.
    """
    try:
        tier = environment_from_env(unknown=UnknownEnvironmentPolicy.PRODUCTION).tier
    except ValueError:
        return True
    return tier in PLATFORM_OWNED_TIERS


def _start_telemetry(app: Any, name: str, *, strict: bool, log_level: int | None) -> None:
    endpoint_variable = RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT
    if resolve_runtime_env(None, endpoint_variable).value is None:
        level = logging.WARNING if _in_deployed_tier() else logging.DEBUG
        logger.log(level, "%s is not set; OpenTelemetry export is off", endpoint_variable.value)
        return

    try:
        from infra2_sdk.runtime.otel import OtelSettings, configure_telemetry

        settings = OtelSettings.from_env(strict=strict)
        if not settings.enabled:
            logger.debug("OpenTelemetry is disabled by the environment")
            return
        service_name = resolve_runtime_env(None, RuntimeEnvKey.SERVICE_NAME, default=name).value
        settings = replace(settings, service_name=service_name or name)
        if log_level is not None:
            # basicConfig does nothing when the root logger already has a handler. Without
            # this call the OpenTelemetry handler would make a later basicConfig a no-op and
            # the root level would stay at WARNING.
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
        logger.info(
            "OpenTelemetry started for '%s' -> %s", settings.service_name, settings.endpoint
        )
    except Exception as exc:
        if strict:
            raise
        logger.warning("Could not initialize OpenTelemetry: %s", exc)


def init_service(
    app: Any,
    name: str,
    *,
    db_checker: Callable[[], _CheckerResult | Awaitable[_CheckerResult]] | None = None,
    enable_otel: bool = True,
    strict_otel: bool = False,
    log_level: int | None = logging.INFO,
    check_timeout_seconds: float = 10.0,
) -> None:
    """Register /livez, /readyz and /health on a FastAPI app and start OpenTelemetry.

    Parameters:
        app: The FastAPI application.
        name: Logical service name. ``OTEL_SERVICE_NAME`` takes precedence for telemetry.
        db_checker: Optional sync or async callable. It returns ``bool`` or ``(bool, detail)``.
            A false result, an exception or a timeout makes /readyz and /health answer 503.
        enable_otel: Start OpenTelemetry when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. A
            deployed tier (preview, staging, production) without the endpoint logs a warning.
        strict_otel: Raise on invalid telemetry settings instead of logging a warning.
        log_level: Level for the ``logging.basicConfig`` call that runs before OpenTelemetry
            attaches its handler. The call changes nothing when the root logger already has a
            handler, so an application that configured logging keeps its level. ``None`` skips
            the call.
        check_timeout_seconds: Time limit of one ``db_checker`` call.
    """
    try:
        from fastapi import Response
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        raise ImportError("fastapi is required to use infra2_sdk.fastapi.init_service") from exc
    if not check_timeout_seconds > 0:
        raise ValueError("check_timeout_seconds must be positive")

    async def report() -> tuple[int, dict[str, Any]]:
        if db_checker is None:
            return HTTP_OK, {"status": HealthStatus.HEALTHY.value, "checks": {}, "reasons": []}
        return await check_health(
            [_DatabaseCheck(db_checker)], timeout_seconds=check_timeout_seconds
        )

    # /livez never checks a remote dependency: a database outage must not restart the container.
    @app.get("/livez", tags=["Observability"], include_in_schema=False)
    def livez() -> Response:
        return JSONResponse(status_code=200, content={"status": "alive", "service": name})

    @app.get("/readyz", tags=["Observability"], include_in_schema=False)
    async def readyz() -> Response:
        status_code, body = await report()
        if status_code == HTTP_OK:
            return JSONResponse(content={"status": "ready", "service": name})
        return JSONResponse(
            status_code=status_code,
            content={"status": "not_ready", "service": name, "reasons": body["reasons"]},
        )

    @app.get("/health", tags=["Observability"], include_in_schema=True)
    async def health() -> Response:
        status_code, body = await report()
        return JSONResponse(status_code=status_code, content={**body, "service": name})

    if enable_otel:
        _start_telemetry(app, name, strict=strict_otel, log_level=log_level)
