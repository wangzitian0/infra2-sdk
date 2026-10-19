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
    EnvironmentTier,
    RuntimeEnvKey,
    UnknownEnvironmentPolicy,
    environment_from_env,
    resolve_runtime_env,
    runtime_env_spec,
    strict_environment_from_env,
)
from infra2_sdk.runtime.health import (
    _HTTP_OK,
    DependencyStatus,
    HealthStatus,
    ProbeResult,
    check_health,
)

if TYPE_CHECKING:
    from infra2_sdk.runtime.otel import OtelSettings

_logger = logging.getLogger("infra2_sdk.fastapi")

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_INITIALIZED_ATTRIBUTE = "infra2_service_name"

_CheckerResult = bool | tuple[bool, str]

# strict_otel is a parameter of init_service only. The messages of from_env cannot name it.
_RELAX_HINT = "pass strict_otel=False to log the problem and start"


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
    """Return True when the tier variables name preview, staging or production.

    Non-strict mode calls this function after a failed ``OtelSettings.from_env``. The failed
    call gives no settings, so ``deployment.environment.name`` does not count. Only the tier
    variables count. A problem in the settings can therefore lower the level of the
    missing-endpoint line to DEBUG, also when the attribute names a deployed tier.

    An unknown or conflicting tier also returns True, so the caller logs more, not less.
    A missing tier counts as ``local_dev``, so the function returns False for it.
    Strict mode never calls this function.
    """
    try:
        tier = environment_from_env(unknown=UnknownEnvironmentPolicy.PRODUCTION).tier
    except ValueError:
        return True
    return tier in PLATFORM_OWNED_TIERS


def _require_known_tier() -> None:
    """Raise ``ValueError`` when the environment names no known tier.

    Strict mode calls this before it looks for the endpoint. A service without an endpoint
    stops too, so a missing tier cannot hide behind missing telemetry. A conflict between the
    tier variables keeps its type and ends with the way out of strict mode.
    """
    try:
        strict_environment_from_env()
    except EnvironmentConflictError as exc:
        # The message already names the conflicting variables. It needs only the way out.
        raise _with_relax_hint(exc) from exc
    except ValueError as exc:
        spec = runtime_env_spec(RuntimeEnvKey.ENVIRONMENT)
        names = ", ".join((spec.key.value, *spec.aliases))
        raise ValueError(
            f"{exc}; set one of {names} to a known tier, or pass strict_otel=False for a local run"
        ) from exc


def _with_relax_hint(exc: ValueError) -> ValueError:
    """Return a new strict error: the message of ``exc`` followed by the way out.

    The type of an ``EnvironmentConflictError`` stays. Another ``ValueError`` becomes a plain one.
    """
    if isinstance(exc, EnvironmentConflictError):
        return EnvironmentConflictError(f"{exc}; {_RELAX_HINT}")
    return ValueError(f"{exc}; {_RELAX_HINT}")


def _log_missing_endpoint(*, deployed: bool) -> None:
    level = logging.WARNING if deployed else logging.DEBUG
    _logger.log(
        level,
        "%s is not set; OpenTelemetry export is off",
        RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT.value,
    )


def _resolve_telemetry(
    name: str,
    *,
    strict: bool,
    require_issued_identity: bool = False,
) -> OtelSettings | None:
    """Resolve and check the telemetry settings. Return them when telemetry must start.

    ``init_service`` calls this before it changes the app. Strict mode raises ``ValueError``
    for a problem. Non-strict mode logs the problem, never stops, and returns ``None``.
    """
    from infra2_sdk.runtime.otel import OtelSettings

    if strict and not require_issued_identity:
        _require_known_tier()
    try:
        # from_env is the one reader of the endpoint, the service name and the tier of the
        # settings. Two other readings of the tier exist, for a message or a log level only:
        # _require_known_tier (strict mode) and _in_deployed_tier (after a failed from_env).
        from_env_kwargs: dict[str, Any] = {
            "strict": strict,
            "default_service_name": name,
        }
        if require_issued_identity:
            from_env_kwargs["require_issued_identity"] = True
        settings = OtelSettings.from_env(**from_env_kwargs)
    except Exception as exc:
        if strict:
            if isinstance(exc, ValueError):
                raise _with_relax_hint(exc) from exc
            raise  # any other error passes through unchanged
        # A failed from_env cannot tell whether an endpoint is set. Read the variable here to
        # choose the log line. Without an endpoint, telemetry is off. The missing-endpoint
        # line is then the only message.
        if resolve_runtime_env(None, RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT).value is None:
            _log_missing_endpoint(deployed=_in_deployed_tier())
        else:
            _logger.warning("Could not initialize OpenTelemetry: %s", exc)
        return None
    if settings.sdk_disabled:
        _logger.debug("OpenTelemetry is disabled by the environment")
        return None
    if settings.endpoint is None:
        # The tier that from_env resolved, also when it inferred the tier from the attribute
        # deployment.environment.name.
        _log_missing_endpoint(
            deployed=EnvironmentTier(settings.environment) in PLATFORM_OWNED_TIERS
        )
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
    enable_otel: bool = True,
    strict_otel: bool = True,
    require_issued_identity: bool = False,
    log_level: int | None = logging.INFO,
    check_timeout_seconds: float = 10.0,
) -> None:
    """Register /livez, /readyz and /health on a FastAPI app and start OpenTelemetry.

    Call it once per app: a second call raises ``RuntimeError``. Call it after you set the
    app lifespan; a lifespan set later replaces the SDK shutdown. A call that raises for the
    name, for ``check_timeout_seconds`` or for a failed strict check leaves the app unchanged,
    so a retry meets the real error and not ``RuntimeError``.

    One strict failure is different: a failure while the providers start, for example when the
    exporter package is not installed. It happens after the probe routes exist. A retry on the
    same app raises ``RuntimeError: init_service was already called for this app``.

    Parameters:
        app: The FastAPI application.
        name: Logical service name. It must not be blank: a blank name raises ``ValueError``
            before the app changes. The telemetry service name is ``OTEL_SERVICE_NAME``, then
            ``service.name`` in ``OTEL_RESOURCE_ATTRIBUTES``, then ``name``. A blank variable
            or attribute value counts as missing.
        db_checker: Optional sync or async callable. It returns ``bool`` or ``(bool, detail)``.
            A false result, an exception or a timeout makes /readyz and /health answer 503.
            A sync db_checker must set its own protocol-level timeout; a timed-out call keeps
            its thread.
        enable_otel: Start OpenTelemetry when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. A
            deployed tier (preview, staging, production) without the endpoint logs a warning,
            unless ``OTEL_SDK_DISABLED=true``. ``enable_otel=False`` skips every telemetry
            check. The blank-name check always runs.
        strict_otel: Fail closed. The default is True. A strict start raises ``ValueError``,
            before the app changes, when:

            - the tier is missing or unknown;
            - ``OTEL_RESOURCE_ATTRIBUTES`` is malformed, or its ``deployment.environment.name``
              disagrees with the tier;
            - ``OTEL_SDK_DISABLED``, ``OTEL_TRACES_SAMPLER``, ``OTEL_TRACES_SAMPLER_ARG`` or
              ``OTEL_METRIC_EXPORT_INTERVAL`` is invalid (the sampler argument counts only for
              a ratio sampler);
            - two aliases of one variable hold different values. ``init_service`` reads two
              groups of aliases: the tier variables (``ENVIRONMENT``, ``ENV``, ``APP_ENV``),
              and ``SERVICE_VERSION`` with ``IMAGE_TAG``. The contract has no other alias.

            An ``OTEL_EXPORTER_OTLP_ENDPOINT`` that is not a valid http(s) URL also stops the
            start, unless ``OTEL_SDK_DISABLED=true``. The checks run with or without an
            endpoint. ``OTEL_SDK_DISABLED=true`` does not skip the four checks above.
            ``enable_otel=False`` skips every telemetry check; the blank-name check always
            runs. The SDK does not read ``OTEL_EXPORTER_OTLP_TIMEOUT``,
            ``OTEL_EXPORTER_OTLP_HEADERS``, ``OTEL_BSP_*`` or ``OTEL_EXPORTER_OTLP_PROTOCOL``,
            so it never checks them.

            Pass ``strict_otel=False`` for a laptop and for services that must never stop on
            telemetry, such as a pager bridge. A missing tier then counts as ``local_dev``.
            A malformed ``OTEL_RESOURCE_ATTRIBUTES``, an invalid ``OTEL_SDK_DISABLED`` and an
            invalid sampler variable give a Python ``RuntimeWarning``, also when no endpoint is
            set. Every other problem is a log record of the logger ``infra2_sdk.fastapi``. With
            an endpoint, it is ``Could not initialize OpenTelemetry: ...`` at WARNING. Without
            an endpoint, the only record is the missing-endpoint line. Its level is WARNING in
            a deployed tier and DEBUG elsewhere. An unknown or conflicting tier counts as
            deployed. The tier comes from the tier variables. When no tier variable is set and
            ``OtelSettings.from_env`` succeeds, it comes from ``deployment.environment.name``.
            After a failed ``OtelSettings.from_env``, only the tier variables count, so a
            problem in the settings can lower the level to DEBUG. With
            ``OTEL_SDK_DISABLED=true`` and no other problem, the record is "OpenTelemetry is
            disabled by the environment" at DEBUG.
        require_issued_identity: When True, ignore ``ENVIRONMENT``, ``ENV``, and ``APP_ENV``
            and derive the runtime tier from ``deployment.environment.name`` in
            ``OTEL_RESOURCE_ATTRIBUTES``. When an OTLP endpoint is active, require
            ``OTEL_SERVICE_NAME`` and the four ``REQUIRED_IDENTITY_ATTRIBUTES``
            (``deployment.environment.name``, ``infra.service.id``, ``service.version``,
            ``infra.iac.ref``).
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
    settings = (
        _resolve_telemetry(
            name,
            strict=strict_otel,
            require_issued_identity=require_issued_identity,
        )
        if enable_otel
        else None
    )
    setattr(app.state, _INITIALIZED_ATTRIBUTE, name)

    async def report() -> tuple[int, dict[str, Any]]:
        if db_checker is None:
            return _HTTP_OK, {"status": HealthStatus.HEALTHY.value, "checks": {}, "reasons": []}
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


__all__ = [
    "init_service",
]
