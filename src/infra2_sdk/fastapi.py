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
from typing import TYPE_CHECKING, Any

from infra2_sdk.runtime.environment import (
    PLATFORM_OWNED_TIERS,
    EnvironmentConflictError,
    RuntimeEnvKey,
    UnknownEnvironmentPolicy,
    environment_from_env,
    resolve_runtime_env,
    runtime_env_spec,
    strict_environment_from_env,
)
from infra2_sdk.runtime.health import (
    HTTP_OK,
    DependencyStatus,
    HealthStatus,
    ProbeResult,
    check_health,
)

if TYPE_CHECKING:
    from infra2_sdk.runtime.otel import OtelSettings

logger = logging.getLogger("infra2_sdk.fastapi")

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_INITIALIZED_ATTRIBUTE = "infra2_service_name"

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
    A missing environment counts as ``local_dev`` in non-strict mode, so this function returns
    False for it. Strict mode never reaches this function with a missing tier, because
    ``_require_known_tier`` raises first.
    """
    try:
        tier = environment_from_env(unknown=UnknownEnvironmentPolicy.PRODUCTION).tier
    except ValueError:
        return True
    return tier in PLATFORM_OWNED_TIERS


def _require_known_tier() -> None:
    """Raise ``ValueError`` when the environment names no known tier.

    Strict mode calls this before it looks for the endpoint. A service without an endpoint
    stops too, so a missing tier cannot hide behind missing telemetry.
    """
    try:
        strict_environment_from_env()
    except EnvironmentConflictError:
        raise  # the message already names the conflicting variables
    except ValueError as exc:
        spec = runtime_env_spec(RuntimeEnvKey.ENVIRONMENT)
        names = ", ".join((spec.key.value, *spec.aliases))
        raise ValueError(
            f"{exc}; set one of {names} to a known tier, or pass strict_otel=False for a local run"
        ) from exc


def _log_missing_endpoint() -> None:
    level = logging.WARNING if _in_deployed_tier() else logging.DEBUG
    logger.log(
        level,
        "%s is not set; OpenTelemetry export is off",
        RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT.value,
    )


def _resolve_telemetry(name: str, *, strict: bool) -> OtelSettings | None:
    """Resolve and check the telemetry settings. Return them when telemetry must start.

    ``init_service`` calls this before it changes the app. Strict mode raises ``ValueError``
    for a problem. Non-strict mode logs the problem, never stops, and returns ``None``.
    """
    from infra2_sdk.runtime.otel import OtelSettings

    if strict:
        _require_known_tier()
    try:
        settings = OtelSettings.from_env(strict=strict, default_service_name=name)
    except Exception as exc:
        if strict:
            raise
        # A failed from_env cannot tell whether an endpoint is set. Read the variable here to
        # choose the log line. Without an endpoint, telemetry is off. The missing-endpoint
        # line is then the only message.
        if resolve_runtime_env(None, RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT).value is None:
            _log_missing_endpoint()
        else:
            logger.warning("Could not initialize OpenTelemetry: %s", exc)
        return None
    if settings.sdk_disabled:
        logger.debug("OpenTelemetry is disabled by the environment")
        return None
    if settings.endpoint is None:
        _log_missing_endpoint()
        return None
    return settings


def _start_telemetry(
    app: Any, settings: OtelSettings, *, strict: bool, log_level: int | None
) -> None:
    try:
        from infra2_sdk.runtime.otel import configure_telemetry

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
    strict_otel: bool = True,
    log_level: int | None = logging.INFO,
    check_timeout_seconds: float = 10.0,
) -> None:
    """Register /livez, /readyz and /health on a FastAPI app and start OpenTelemetry.

    Call it once per app: a second call raises ``RuntimeError``. Call it after you set the
    app lifespan; a lifespan set later replaces the SDK shutdown. A call that raises for the
    name, for ``check_timeout_seconds`` or for a strict telemetry problem leaves the app
    unchanged, so a retry meets the real error and not ``RuntimeError``.

    Parameters:
        app: The FastAPI application.
        name: Logical service name. It must not be blank: a blank name raises ``ValueError``
            before the app changes. The telemetry service name is ``OTEL_SERVICE_NAME``, then
            ``service.name`` in ``OTEL_RESOURCE_ATTRIBUTES``, then ``name``.
        db_checker: Optional sync or async callable. It returns ``bool`` or ``(bool, detail)``.
            A false result, an exception or a timeout makes /readyz and /health answer 503.
            A sync db_checker must set its own protocol-level timeout; a timed-out call keeps
            its thread.
        enable_otel: Start OpenTelemetry when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. A
            deployed tier (preview, staging, production) without the endpoint logs a warning.
            With ``enable_otel=False`` nothing is checked.
        strict_otel: Fail closed. A missing or unknown tier (``ENVIRONMENT``, or its aliases
            ``ENV`` and ``APP_ENV``) and a malformed ``OTEL_*`` value raise ``ValueError``
            at start-up. The check runs before the endpoint check, so it also runs when no
            endpoint is set. Pass ``strict_otel=False`` for local runs and for services that
            must never stop on telemetry, such as a pager bridge. A missing tier then counts
            as ``local_dev``, and a malformed ``OTEL_*`` value gives a warning.
        log_level: Level for the ``logging.basicConfig`` call that runs before OpenTelemetry
            attaches its handler. The call changes nothing when the root logger already has a
            handler, so an application that configured logging keeps its level. An application
            that only called ``root.setLevel`` and has no handler gets ``log_level``. ``None``
            skips the call.
        check_timeout_seconds: Time limit of one ``db_checker`` call.
    """
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
    # Resolve and check the telemetry settings before the app changes. A strict problem raises
    # here, so a failed start leaves no marker and no routes, and a retry sees the real error.
    settings = _resolve_telemetry(name, strict=strict_otel) if enable_otel else None
    setattr(app.state, _INITIALIZED_ATTRIBUTE, name)

    async def report() -> tuple[int, dict[str, Any]]:
        if db_checker is None:
            return HTTP_OK, {"status": HealthStatus.HEALTHY.value, "checks": {}, "reasons": []}
        return await check_health(
            [_DatabaseCheck(db_checker)], timeout_seconds=check_timeout_seconds
        )

    # Do not add ``-> Response`` to these handlers. The module uses ``from __future__ import
    # annotations``, so the annotation would stay a ForwardRef and the OpenAPI build would
    # fail for /health.

    # /livez never checks a remote dependency: a database outage must not restart the container.
    @app.get("/livez", tags=["Observability"], include_in_schema=False)
    def livez():
        return JSONResponse(status_code=200, content={"status": "alive", "service": name})

    @app.get("/readyz", tags=["Observability"], include_in_schema=False)
    async def readyz():
        status_code, body = await report()
        if status_code == HTTP_OK:
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
