"""Tests for the FastAPI integration: probe routes, health bodies and OpenTelemetry."""

import asyncio
import inspect
import io
import logging
import time
import warnings
from contextlib import asynccontextmanager, contextmanager

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from infra2_sdk.fastapi import _INITIALIZED_ATTRIBUTE, init_service
from infra2_sdk.runtime.environment import EnvironmentConflictError

# Every variable that selects the tier, the service identity or the OTLP endpoint.
_AMBIENT_VARIABLES = (
    "ENVIRONMENT",
    "ENV",
    "APP_ENV",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_SDK_DISABLED",
    "OTEL_SERVICE_NAME",
    "SERVICE_NAME",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
    "OTEL_METRIC_EXPORT_INTERVAL",
)
_PROBE_ROUTES = {"/livez", "/readyz", "/health"}


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch):
    for name in _AMBIENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def _sdk_records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == "infra2_sdk.fastapi"]


@contextmanager
def _root_logger_as_found_by_a_fresh_process(*handlers: logging.Handler):
    """Give the root logger the state of a process that did not configure logging yet.

    pytest attaches its own capture handlers to the root logger, so the test body must reset
    them. The state before the test returns on exit.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    root.handlers[:] = list(handlers)
    root.setLevel(logging.WARNING)
    try:
        yield root
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def _app(**options) -> FastAPI:
    app = FastAPI()
    options.setdefault("enable_otel", False)
    init_service(app, "svc", **options)
    return app


def test_fastapi_init_endpoints_without_db():
    app = FastAPI()
    init_service(app, name="auth-service", enable_otel=False)
    client = TestClient(app)

    # 1. /livez (process liveness)
    livez_res = client.get("/livez")
    assert livez_res.status_code == 200
    assert livez_res.json() == {"status": "alive", "service": "auth-service"}

    # 2. /readyz (no db configured -> ready)
    readyz_res = client.get("/readyz")
    assert readyz_res.status_code == 200
    assert readyz_res.json() == {"status": "ready", "service": "auth-service"}

    # 3. /health (aggregate)
    health_res = client.get("/health")
    assert health_res.status_code == 200
    data = health_res.json()
    assert data["status"] == "healthy"
    assert data["service"] == "auth-service"
    assert data["checks"] == {}
    assert data["reasons"] == []


def test_fastapi_init_with_healthy_db():
    app = FastAPI()
    init_service(
        app,
        name="finance-backend",
        db_checker=lambda: (True, "postgres ok"),
        enable_otel=False,
    )
    client = TestClient(app)

    # /readyz
    res = client.get("/readyz")
    assert res.status_code == 200
    assert res.json()["status"] == "ready"

    # /health
    health_res = client.get("/health")
    assert health_res.status_code == 200
    data = health_res.json()
    assert data["status"] == "healthy"
    assert data["checks"]["database"]["status"] == "present"
    assert data["checks"]["database"]["detail"] == "postgres ok"
    assert data["checks"]["database"]["required"] is True
    assert "duration_ms" in data["checks"]["database"]
    assert data["reasons"] == []


def test_fastapi_init_with_unhealthy_db_fails_closed():
    app = FastAPI()
    init_service(
        app,
        name="orders-api",
        db_checker=lambda: (False, "connection pool timeout"),
        enable_otel=False,
    )
    client = TestClient(app)

    # /livez must still be 200 (anti-puppet: external failure must NOT crash container)
    livez = client.get("/livez")
    assert livez.status_code == 200

    # /readyz must return 503
    readyz = client.get("/readyz")
    assert readyz.status_code == 503
    assert readyz.json()["status"] == "not_ready"
    assert readyz.json()["service"] == "orders-api"
    assert "database: connection pool timeout" in readyz.json()["reasons"]

    # /health must return 503 with reasons
    health = client.get("/health")
    assert health.status_code == 503
    data = health.json()
    assert data["status"] == "unhealthy"
    assert data["service"] == "orders-api"
    assert data["checks"]["database"]["status"] == "absent"
    assert "database: connection pool timeout" in data["reasons"]


def test_fastapi_init_with_db_exception():
    def exploding_checker():
        raise ConnectionRefusedError("socket closed by peer")

    app = FastAPI()
    init_service(
        app,
        name="payment-svc",
        db_checker=exploding_checker,
        enable_otel=False,
    )
    client = TestClient(app)

    # Must fail closed with 503 without crashing the process
    readyz = client.get("/readyz")
    assert readyz.status_code == 503
    assert any("ConnectionRefusedError" in r for r in readyz.json()["reasons"])

    health = client.get("/health")
    assert health.status_code == 503
    assert health.json()["status"] == "unhealthy"


# --- the checker contract: bool or (bool, detail), sync or async, never a silent 200 ----------


def _sync(result):
    return lambda: result


def _async(result):
    async def checker():
        return result

    return checker


_FALSE_VALUE_REASON = "db_checker returned a false value"


@pytest.mark.parametrize("wrap", [_sync, _async], ids=["sync", "async"])
@pytest.mark.parametrize(
    "result,status,detail",
    [
        (False, 503, _FALSE_VALUE_REASON),
        (True, 200, "ok"),
        ((False, "pool exhausted"), 503, "pool exhausted"),
        ((True, "replica lag 2s"), 200, "replica lag 2s"),
        ((True, ""), 200, "ok"),
        (None, 503, _FALSE_VALUE_REASON),
    ],
    ids=["bare-false", "bare-true", "tuple-false", "tuple-true", "tuple-true-empty", "none"],
)
def test_db_checker_result_decides_readyz_and_health(wrap, result, status, detail):
    client = TestClient(_app(db_checker=wrap(result)))

    readyz = client.get("/readyz")
    health = client.get("/health")

    assert readyz.status_code == status
    assert health.status_code == status
    assert health.json()["status"] == ("healthy" if status == 200 else "unhealthy")
    assert readyz.json()["status"] == ("ready" if status == 200 else "not_ready")
    assert health.json()["checks"]["database"]["detail"] == detail
    if status == 503:
        assert readyz.json()["reasons"] == [f"database: {detail}"]
        assert health.json()["reasons"] == [f"database: {detail}"]


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_health_reports_the_checker_duration_in_milliseconds(kind):
    if kind == "sync":

        def checker():
            time.sleep(0.05)
            return True

    else:

        async def checker():
            await asyncio.sleep(0.05)
            return True

    health = TestClient(_app(db_checker=checker)).get("/health")

    duration_ms = health.json()["checks"]["database"]["duration_ms"]
    assert 40 <= duration_ms < 2000


def test_async_checker_returning_false_is_unready_and_unhealthy():
    client = TestClient(_app(db_checker=_async(False)))

    readyz = client.get("/readyz")
    health = client.get("/health")

    assert readyz.status_code == 503
    assert health.status_code == 503
    assert health.json()["status"] == "unhealthy"
    assert health.json()["checks"]["database"]["status"] == "absent"


def test_sync_checker_returning_an_awaitable_is_awaited():
    async def check():
        return False

    client = TestClient(_app(db_checker=lambda: check()))

    assert client.get("/readyz").status_code == 503


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_raising_checker_is_unready_never_ready(kind):
    if kind == "sync":

        def checker():
            raise ConnectionRefusedError("socket closed by peer")

    else:

        async def checker():
            raise ConnectionRefusedError("socket closed by peer")

    client = TestClient(_app(db_checker=checker))

    readyz = client.get("/readyz")
    health = client.get("/health")

    assert readyz.status_code == 503
    assert health.status_code == 503
    assert health.json()["status"] == "unhealthy"
    assert "database: ConnectionRefusedError: socket closed by peer" in readyz.json()["reasons"]


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_checker_slower_than_the_timeout_is_unready_with_a_timeout_reason(kind):
    if kind == "sync":

        def checker():
            time.sleep(1.0)
            return True

    else:

        async def checker():
            await asyncio.sleep(5.0)
            return True

    client = TestClient(_app(db_checker=checker, check_timeout_seconds=0.05))

    started = time.perf_counter()
    readyz = client.get("/readyz")
    health = client.get("/health")
    elapsed = time.perf_counter() - started

    assert readyz.status_code == 503
    assert health.status_code == 503
    assert "timed out" in readyz.json()["reasons"][0]
    assert "timed out" in health.json()["checks"]["database"]["detail"]
    assert elapsed < 0.9, "the response must not wait for the slow checker"


@pytest.mark.parametrize("seconds", [0, -1.0, float("-inf"), float("nan")])
def test_non_positive_or_undefined_check_timeout_is_rejected_at_init(seconds):
    with pytest.raises(ValueError, match="check_timeout_seconds"):
        _app(db_checker=_sync(True), check_timeout_seconds=seconds)


async def test_sync_checker_does_not_block_the_event_loop():
    def checker():
        time.sleep(0.4)
        return True

    app = _app(db_checker=checker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://svc") as client:
        request = asyncio.create_task(client.get("/readyz"))
        worst_lag = 0.0
        while not request.done():
            started = time.perf_counter()
            await asyncio.sleep(0.01)
            worst_lag = max(worst_lag, time.perf_counter() - started - 0.01)
        response = await request

    assert response.status_code == 200
    assert worst_lag < 0.2, f"the event loop stalled for {worst_lag:.3f}s"


def test_probe_routes_are_the_only_routes_and_stay_top_level():
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    init_service(app, "svc", db_checker=_sync(True), enable_otel=False)

    assert {route.path for route in app.router.routes} == _PROBE_ROUTES
    assert all(route.methods == {"GET"} for route in app.router.routes)


def test_probe_routes_do_not_collide_with_the_default_documentation_routes():
    app = _app()

    paths = {route.path for route in app.router.routes}

    assert paths >= _PROBE_ROUTES
    assert paths - _PROBE_ROUTES <= {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def test_openapi_json_builds_and_documents_only_health():
    app = _app(db_checker=_sync(True))

    response = TestClient(app, raise_server_exceptions=False).get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/health" in paths
    assert "/livez" not in paths
    assert "/readyz" not in paths


def test_second_init_service_call_on_one_app_raises_and_changes_nothing():
    app = _app(db_checker=_sync(True))
    routes_before = list(app.router.routes)

    with pytest.raises(RuntimeError, match="init_service was already called"):
        init_service(app, "svc", enable_otel=False)

    assert app.router.routes == routes_before
    assert [route.path for route in app.router.routes].count("/livez") == 1


# --- a blank name stops the start before any side effect ---------------------------------------


@pytest.mark.parametrize("name", ["", " ", "\t\n"], ids=["empty", "space", "tab-and-newline"])
@pytest.mark.parametrize("strict", [True, False], ids=["strict", "non-strict"])
@pytest.mark.parametrize("enable_otel", [True, False], ids=["otel", "no-otel"])
def test_init_service_rejects_a_blank_name_before_any_side_effect(
    monkeypatch, name, strict, enable_otel
):
    monkeypatch.setenv("ENVIRONMENT", "local_dev")  # a known tier: only the name is at fault
    app = FastAPI()
    routes_before = list(app.router.routes)

    with pytest.raises(ValueError, match="name must not be blank"):
        init_service(app, name, enable_otel=enable_otel, strict_otel=strict)

    assert app.router.routes == routes_before
    assert not hasattr(app.state, _INITIALIZED_ATTRIBUTE)
    init_service(app, "svc", enable_otel=False)  # the app is still free for a valid call
    assert {"/livez", "/readyz", "/health"} <= {route.path for route in app.router.routes}


def test_init_service_marks_only_the_app_it_ran_on():
    for app in (_app(), _app()):
        assert [route.path for route in app.router.routes].count("/livez") == 1


# --- the application lifespan: the SDK stops telemetry after the application's own lifespan ---


def _otel_environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "local_dev")  # the default strict_otel needs a tier
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "3600000")


def _record_sdk_shutdown(app, events):
    providers = app.state.telemetry_providers
    real_shutdown = providers.shutdown

    def recording_shutdown():
        events.append("SDK_SHUTDOWN")
        real_shutdown()

    providers.shutdown = recording_shutdown


def test_sdk_shutdown_runs_after_the_user_lifespan_stops(
    monkeypatch, memory_otlp, clean_otel_globals
):
    _otel_environment(monkeypatch)
    events = []

    @asynccontextmanager
    async def user_lifespan(app):
        events.append("START")
        yield
        events.append("STOP")

    app = FastAPI(lifespan=user_lifespan)
    init_service(app, "svc", enable_otel=True)
    _record_sdk_shutdown(app, events)

    with TestClient(app) as client:
        assert events == ["START"]
        assert client.get("/livez").status_code == 200

    assert events == ["START", "STOP", "SDK_SHUTDOWN"]


def test_state_yielded_by_the_user_lifespan_reaches_the_request(
    monkeypatch, memory_otlp, clean_otel_globals
):
    _otel_environment(monkeypatch)

    @asynccontextmanager
    async def user_lifespan(app):
        yield {"pool": "pool-1"}

    app = FastAPI(lifespan=user_lifespan)
    init_service(app, "svc", enable_otel=True)

    @app.get("/pool")
    def pool(request: Request):
        return {"pool": request.state.pool}

    with TestClient(app) as client:
        assert client.get("/pool").json() == {"pool": "pool-1"}


def test_sdk_shutdown_runs_when_the_user_lifespan_fails_at_startup(
    monkeypatch, memory_otlp, clean_otel_globals
):
    _otel_environment(monkeypatch)
    events = []

    @asynccontextmanager
    async def failing_lifespan(app):
        events.append("START")
        raise RuntimeError("startup failed")
        yield  # pragma: no cover - makes this function an async generator

    app = FastAPI(lifespan=failing_lifespan)
    init_service(app, "svc", enable_otel=True)
    _record_sdk_shutdown(app, events)

    with pytest.raises(RuntimeError, match="startup failed"), TestClient(app):
        pass

    assert events == ["START", "SDK_SHUTDOWN"]


# --- OpenTelemetry: one bootstrap, three signals, stopped with the app ------------------------


def test_otel_pipeline_exports_all_signals_and_stops_with_the_app(
    monkeypatch, memory_otlp, clean_otel_globals
):
    from opentelemetry import trace

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "3600000")
    app = FastAPI()

    init_service(app, "svc", enable_otel=True)

    providers = app.state.telemetry_providers
    handler = providers.logging_handler
    root = logging.getLogger()
    assert providers.tracer_provider is not None
    assert providers.logger_provider is not None
    assert providers.meter_provider is not None
    assert handler is not None
    assert handler in root.handlers
    assert trace.get_tracer_provider() is providers.tracer_provider
    assert sorted(memory_otlp.endpoints) == [
        ("logs", "http://collector:4318/v1/logs"),
        ("metrics", "http://collector:4318/v1/metrics"),
        ("traces", "http://collector:4318/v1/traces"),
    ]

    with TestClient(app) as client:
        assert client.get("/livez").json() == {"status": "alive", "service": "svc"}
        with trace.get_tracer("fastapi-test").start_as_current_span("unit-of-work"):
            logging.getLogger("svc.test").warning("something happened")
        providers.tracer_provider.force_flush()
        providers.logger_provider.force_flush()

        spans = memory_otlp.spans.get_finished_spans()
        span_names = [span.name for span in spans]
        assert "unit-of-work" in span_names
        # FastAPI 0.142+ emits request spans itself; no instrumentor package is installed.
        assert "GET /livez" in span_names
        assert {span.resource.attributes["service.name"] for span in spans} == {"svc"}
        assert "something happened" in [
            record.log_record.body for record in memory_otlp.logs.get_finished_logs()
        ]
        assert handler in root.handlers, "the handler must stay attached while the app runs"

    assert handler not in root.handlers
    assert providers.logging_handler is None


def test_otel_service_name_from_the_environment_beats_the_init_argument(
    monkeypatch, memory_otlp, clean_otel_globals
):
    monkeypatch.setenv("ENVIRONMENT", "local_dev")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "from-env")
    app = FastAPI()

    init_service(app, "from-argument", enable_otel=True)

    provider = app.state.telemetry_providers.tracer_provider
    try:
        assert provider.resource.attributes["service.name"] == "from-env"
    finally:
        app.state.telemetry_providers.shutdown()


def test_otel_service_name_defaults_to_the_init_argument(
    monkeypatch, memory_otlp, clean_otel_globals
):
    monkeypatch.setenv("ENVIRONMENT", "local_dev")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    app = FastAPI()

    init_service(app, "from-argument", enable_otel=True)

    provider = app.state.telemetry_providers.tracer_provider
    try:
        assert provider.resource.attributes["service.name"] == "from-argument"
    finally:
        app.state.telemetry_providers.shutdown()


def _exported_attributes(monkeypatch, environ: dict[str, str]) -> dict:
    """Start ``init_service("from-argument")`` with ``environ`` and return the exported resource."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("ENVIRONMENT", "local_dev")
    for variable, value in environ.items():
        monkeypatch.setenv(variable, value)
    app = FastAPI()

    init_service(app, "from-argument", enable_otel=True)

    providers = app.state.telemetry_providers
    try:
        return dict(providers.tracer_provider.resource.attributes)
    finally:
        providers.shutdown()


def _exported_service_name(monkeypatch, environ: dict[str, str]) -> str:
    return _exported_attributes(monkeypatch, environ)["service.name"]


@pytest.mark.parametrize(
    "environ,expected",
    [
        pytest.param(
            {
                "OTEL_SERVICE_NAME": "from-env",
                "OTEL_RESOURCE_ATTRIBUTES": "service.name=from-attribute",
            },
            "from-env",
            id="env-beats-attribute-and-argument",
        ),
        pytest.param(
            {"OTEL_RESOURCE_ATTRIBUTES": "service.name=from-attribute"},
            "from-attribute",
            id="attribute-beats-argument",
        ),
        pytest.param({}, "from-argument", id="argument-alone"),
    ],
)
def test_init_service_exports_the_service_name_by_the_one_precedence_rule(
    monkeypatch, memory_otlp, clean_otel_globals, environ, expected
):
    assert _exported_service_name(monkeypatch, environ) == expected


def _settings_passed_to_telemetry(monkeypatch, environ: dict[str, str]):
    """Start ``init_service("from-argument")`` with ``environ``; return the settings it hands over.

    The OpenTelemetry SDK also reads ``OTEL_RESOURCE_ATTRIBUTES`` from the process environment
    when it builds a resource. The settings show what ``init_service`` decides, free of that.
    """
    from infra2_sdk.runtime import otel as otel_module

    handed_over = []

    def configure(settings, **options):
        handed_over.append(settings)
        return otel_module.TelemetryProviders()

    monkeypatch.setattr(otel_module, "configure_telemetry", configure)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("ENVIRONMENT", "local_dev")
    for variable, value in environ.items():
        monkeypatch.setenv(variable, value)

    init_service(FastAPI(), "from-argument", enable_otel=True)

    (settings,) = handed_over
    return settings


@pytest.mark.parametrize(
    "raw,stripped",
    [("%20", None), ("%09", None), ("%20x%20", "x")],
    ids=["space", "tab", "padded"],
)
@pytest.mark.parametrize(
    "attribute,field,missing",
    [
        ("service.name", "service_name", "from-argument"),
        ("service.version", "service_version", "unknown"),
        ("service.instance.id", "instance_id", ""),
    ],
)
def test_init_service_reads_a_blank_or_padded_attribute_as_missing_or_stripped(
    monkeypatch, attribute, field, missing, raw, stripped
):
    environ = {"OTEL_RESOURCE_ATTRIBUTES": f"{attribute}={raw}"}

    settings = _settings_passed_to_telemetry(monkeypatch, environ)

    assert getattr(settings, field) == (missing if stripped is None else stripped)
    assert (attribute in settings.resource_attributes) is (stripped is not None)


@pytest.mark.parametrize("raw", ["%20", "%09"])
def test_init_service_exports_its_name_for_a_blank_service_name_attribute(
    monkeypatch, memory_otlp, clean_otel_globals, raw
):
    environ = {"OTEL_RESOURCE_ATTRIBUTES": f"service.name={raw}"}

    assert _exported_attributes(monkeypatch, environ)["service.name"] == "from-argument"


# --- one resolution: init_service reads each fact once, through OtelSettings.from_env ----------


@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.parametrize("with_endpoint", [True, False], ids=["endpoint", "no-endpoint"])
def test_init_service_resolves_the_service_identity_once(
    monkeypatch, memory_otlp, clean_otel_globals, resolver_calls, strict, with_endpoint
):
    monkeypatch.setenv("ENVIRONMENT", "local_dev")
    if with_endpoint:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    app = FastAPI()

    init_service(app, "svc", enable_otel=True, strict_otel=strict)

    if with_endpoint:
        app.state.telemetry_providers.shutdown()
    assert len(resolver_calls) == 1
    assert resolver_calls[0]["strict"] is strict
    assert resolver_calls[0]["default_service_name"] == "svc"


def test_init_service_reads_the_endpoint_and_the_service_name_once(
    monkeypatch, memory_otlp, clean_otel_globals, environment_reads
):
    monkeypatch.setenv("ENVIRONMENT", "local_dev")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    app = FastAPI()

    init_service(app, "svc", enable_otel=True)

    app.state.telemetry_providers.shutdown()
    assert environment_reads.count("OTEL_EXPORTER_OTLP_ENDPOINT") == 1
    assert environment_reads.count("OTEL_SERVICE_NAME") == 1


def test_enable_otel_false_installs_nothing_even_with_an_endpoint(
    monkeypatch, memory_otlp, clean_otel_globals
):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    app = FastAPI()

    init_service(app, "svc", enable_otel=False)

    assert not hasattr(app.state, "telemetry_providers")
    assert memory_otlp.endpoints == []


def test_sdk_disabled_flag_keeps_telemetry_off_even_with_an_endpoint(
    monkeypatch, memory_otlp, clean_otel_globals
):
    monkeypatch.setenv("ENVIRONMENT", "local_dev")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    app = FastAPI()

    init_service(app, "svc", enable_otel=True)

    assert not hasattr(app.state, "telemetry_providers")
    assert memory_otlp.endpoints == []


def test_sdk_disabled_without_an_endpoint_says_disabled_and_does_not_warn(monkeypatch, caplog):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    with caplog.at_level(logging.DEBUG, logger="infra2_sdk.fastapi"):
        init_service(FastAPI(), "svc", enable_otel=True)

    records = _sdk_records(caplog)
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.DEBUG, "OpenTelemetry is disabled by the environment")
    ]


def test_strict_otel_raises_on_invalid_telemetry_settings(
    monkeypatch, memory_otlp, clean_otel_globals
):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")

    with pytest.raises(ValueError, match="ENVIRONMENT is required"):
        init_service(FastAPI(), "svc", enable_otel=True, strict_otel=True)


# --- fail closed: strict_otel is the default and the tier is checked before the endpoint -------


def test_strict_otel_is_the_default():
    assert inspect.signature(init_service).parameters["strict_otel"].default is True


@pytest.mark.parametrize("with_endpoint", [True, False], ids=["endpoint", "no-endpoint"])
def test_init_service_without_a_tier_refuses_to_start_by_default(monkeypatch, with_endpoint):
    if with_endpoint:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")

    with pytest.raises(ValueError, match=r"ENVIRONMENT.*ENV.*APP_ENV"):
        init_service(FastAPI(), "svc")


@pytest.mark.parametrize("with_endpoint", [True, False], ids=["endpoint", "no-endpoint"])
def test_init_service_with_an_unknown_tier_refuses_to_start_by_default(monkeypatch, with_endpoint):
    monkeypatch.setenv("ENVIRONMENT", "bogus-tier")
    if with_endpoint:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")

    with pytest.raises(ValueError, match=r"bogus-tier.*ENVIRONMENT.*ENV.*APP_ENV"):
        init_service(FastAPI(), "svc")


def test_init_service_with_conflicting_tier_names_refuses_to_start_by_default(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(EnvironmentConflictError, match="ENVIRONMENT"):
        init_service(FastAPI(), "svc")


@pytest.mark.parametrize("environment", [None, "bogus-tier"])
def test_init_service_without_strict_otel_starts_without_a_known_tier(
    monkeypatch, caplog, environment
):
    if environment is not None:
        monkeypatch.setenv("ENVIRONMENT", environment)

    with caplog.at_level(logging.DEBUG, logger="infra2_sdk.fastapi"):
        init_service(FastAPI(), "svc", strict_otel=False)

    assert any("OTEL_EXPORTER_OTLP_ENDPOINT" in r.getMessage() for r in _sdk_records(caplog))


def test_init_service_checks_nothing_when_the_caller_turns_otel_off(monkeypatch, caplog):
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "not-a-pair")

    with (
        caplog.at_level(logging.DEBUG, logger="infra2_sdk.fastapi"),
        warnings.catch_warnings(record=True) as caught,
    ):
        warnings.simplefilter("always")
        init_service(FastAPI(), "svc", enable_otel=False)

    assert caught == []
    assert _sdk_records(caplog) == []


@pytest.mark.parametrize(
    "environ",
    [
        pytest.param({}, id="missing-tier"),
        pytest.param({"ENVIRONMENT": "bogus-tier"}, id="unknown-tier"),
        pytest.param(
            {"ENVIRONMENT": "staging", "OTEL_RESOURCE_ATTRIBUTES": "not-a-pair"},
            id="malformed-attribute",
        ),
        pytest.param(
            {"ENVIRONMENT": "staging", "OTEL_SDK_DISABLED": "junk"}, id="invalid-disabled-flag"
        ),
    ],
)
@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # the non-strict retry warns, then starts
def test_a_strict_error_leaves_the_app_untouched_and_a_retry_shows_no_other_error(
    monkeypatch, environ
):
    for variable, value in environ.items():
        monkeypatch.setenv(variable, value)
    app = FastAPI()
    routes_before = list(app.router.routes)

    with pytest.raises(ValueError):
        init_service(app, "svc")

    assert app.router.routes == routes_before
    assert _PROBE_ROUTES.isdisjoint({route.path for route in app.router.routes})
    assert not hasattr(app.state, _INITIALIZED_ATTRIBUTE)
    init_service(app, "svc", strict_otel=False)  # the retry meets no "already called" error
    assert {route.path for route in app.router.routes} >= _PROBE_ROUTES


def test_a_second_strict_call_reports_the_real_error_again(monkeypatch):
    app = FastAPI()

    for _ in range(2):
        with pytest.raises(ValueError, match="ENVIRONMENT is required"):
            init_service(app, "svc")


@pytest.mark.parametrize("environment", [None, "bogus-tier"], ids=["missing-tier", "unknown-tier"])
def test_strict_otel_checks_the_tier_before_it_looks_for_the_endpoint(monkeypatch, environment):
    if environment is not None:
        monkeypatch.setenv("ENVIRONMENT", environment)

    with pytest.raises(ValueError, match="ENVIRONMENT"):
        init_service(FastAPI(), "svc", strict_otel=True)


@pytest.mark.parametrize("environment", ["staging", "production", "preview", "pr-42"])
def test_strict_otel_with_a_deployed_tier_and_no_endpoint_warns_and_starts(
    monkeypatch, caplog, environment
):
    monkeypatch.setenv("ENVIRONMENT", environment)

    with caplog.at_level(logging.DEBUG, logger="infra2_sdk.fastapi"):
        init_service(FastAPI(), "svc", strict_otel=True)

    warnings = [r for r in _sdk_records(caplog) if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in warnings[0].getMessage()


@pytest.mark.parametrize("environment", ["local_dev", "dev", "test", "github_ci"])
def test_strict_otel_with_a_local_tier_and_no_endpoint_stays_at_debug(
    monkeypatch, caplog, environment
):
    monkeypatch.setenv("ENVIRONMENT", environment)

    with caplog.at_level(logging.DEBUG, logger="infra2_sdk.fastapi"):
        init_service(FastAPI(), "svc", strict_otel=True)

    assert [r for r in _sdk_records(caplog) if r.levelno >= logging.WARNING] == []
    assert any("OTEL_EXPORTER_OTLP_ENDPOINT" in r.getMessage() for r in _sdk_records(caplog))


@pytest.mark.parametrize(
    "variable,value,message",
    [
        ("OTEL_RESOURCE_ATTRIBUTES", "not-a-pair", "OTEL_RESOURCE_ATTRIBUTES"),
        ("OTEL_SDK_DISABLED", "1", "true or false"),
        ("OTEL_TRACES_SAMPLER", "sometimes", "OTEL_TRACES_SAMPLER"),
    ],
)
def test_strict_otel_refuses_a_malformed_otel_value_even_without_an_endpoint(
    monkeypatch, variable, value, message
):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValueError, match=message):
        init_service(FastAPI(), "svc")


def test_non_strict_otel_keeps_a_malformed_otel_value_to_a_warning(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "not-a-pair")

    with pytest.warns(RuntimeWarning, match="OTEL_RESOURCE_ATTRIBUTES"):
        init_service(FastAPI(), "svc", strict_otel=False)


def test_lenient_otel_reports_invalid_telemetry_settings_and_keeps_serving(
    monkeypatch, memory_otlp, clean_otel_globals, caplog
):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("ENVIRONMENT", "typo-tier")
    app = FastAPI()

    with caplog.at_level(logging.WARNING, logger="infra2_sdk.fastapi"):
        init_service(app, "svc", enable_otel=True, strict_otel=False)

    assert "Could not initialize OpenTelemetry" in caplog.text
    assert memory_otlp.endpoints == []
    assert TestClient(app).get("/livez").status_code == 200


@pytest.mark.parametrize(
    "environment", ["staging", "production", "prod", "preview", "pr-42", "unheard-of-tier"]
)
def test_missing_endpoint_in_a_deployed_tier_logs_a_warning_naming_the_variable(
    monkeypatch, caplog, environment
):
    monkeypatch.setenv("ENVIRONMENT", environment)

    with caplog.at_level(logging.DEBUG, logger="infra2_sdk.fastapi"):
        init_service(FastAPI(), "svc", enable_otel=True, strict_otel=False)

    warnings = [r for r in _sdk_records(caplog) if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in warnings[0].getMessage()


@pytest.mark.parametrize("environment", [None, "local_dev", "dev", "test", "github_ci"])
def test_missing_endpoint_outside_a_deployed_tier_stays_at_debug(monkeypatch, caplog, environment):
    if environment is not None:
        monkeypatch.setenv("ENVIRONMENT", environment)

    with caplog.at_level(logging.DEBUG, logger="infra2_sdk.fastapi"):
        init_service(FastAPI(), "svc", enable_otel=True, strict_otel=False)

    assert [r for r in _sdk_records(caplog) if r.levelno >= logging.WARNING] == []
    assert any("OTEL_EXPORTER_OTLP_ENDPOINT" in r.getMessage() for r in _sdk_records(caplog))


def test_missing_endpoint_is_silent_when_the_caller_turns_otel_off(monkeypatch, caplog):
    monkeypatch.setenv("ENVIRONMENT", "production")

    with caplog.at_level(logging.DEBUG, logger="infra2_sdk.fastapi"):
        init_service(FastAPI(), "svc", enable_otel=False)

    assert _sdk_records(caplog) == []


def test_conflicting_environment_aliases_do_not_raise_and_count_as_deployed(monkeypatch, caplog):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("APP_ENV", "production")

    with caplog.at_level(logging.DEBUG, logger="infra2_sdk.fastapi"):
        init_service(FastAPI(), "svc", enable_otel=True, strict_otel=False)

    assert [r for r in _sdk_records(caplog) if r.levelno == logging.WARNING]


# --- logging: the OpenTelemetry handler must not block the app's own log configuration --------


def _exported_log_bodies(memory_otlp, providers) -> list[str]:
    providers.logger_provider.force_flush()
    return [record.log_record.body for record in memory_otlp.logs.get_finished_logs()]


def test_init_service_gives_an_unconfigured_root_logger_info_level_and_a_stream_handler(
    monkeypatch, memory_otlp, clean_otel_globals
):
    monkeypatch.setenv("ENVIRONMENT", "local_dev")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    app = FastAPI()

    with _root_logger_as_found_by_a_fresh_process() as root:
        init_service(app, "svc", enable_otel=True)
        providers = app.state.telemetry_providers
        try:
            logging.getLogger("svc").info("application started")

            assert "application started" in _exported_log_bodies(memory_otlp, providers)
            stream_handlers = [h for h in root.handlers if type(h) is logging.StreamHandler]
            assert len(stream_handlers) == 1
            assert stream_handlers[0].formatter._fmt == (
                "%(asctime)s %(levelname)s %(name)s %(message)s"
            )
            assert root.level == logging.INFO
        finally:
            providers.shutdown()


def test_init_service_keeps_the_root_logger_an_application_already_configured(
    monkeypatch, memory_otlp, clean_otel_globals
):
    monkeypatch.setenv("ENVIRONMENT", "local_dev")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    app_handler = logging.StreamHandler(io.StringIO())
    app_formatter = logging.Formatter("%(message)s")
    app_handler.setFormatter(app_formatter)
    app = FastAPI()

    with _root_logger_as_found_by_a_fresh_process(app_handler) as root:
        init_service(app, "svc", enable_otel=True)
        providers = app.state.telemetry_providers
        try:
            assert root.level == logging.WARNING
            assert [h for h in root.handlers if h is not providers.logging_handler] == [app_handler]
            assert app_handler.formatter is app_formatter
            assert providers.logging_handler in root.handlers
        finally:
            providers.shutdown()


def test_log_level_none_leaves_the_root_logger_untouched(
    monkeypatch, memory_otlp, clean_otel_globals
):
    monkeypatch.setenv("ENVIRONMENT", "local_dev")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    app = FastAPI()

    with _root_logger_as_found_by_a_fresh_process() as root:
        init_service(app, "svc", enable_otel=True, log_level=None)
        providers = app.state.telemetry_providers
        try:
            assert root.level == logging.WARNING
            assert root.handlers == [providers.logging_handler]
        finally:
            providers.shutdown()


def test_log_level_applies_the_requested_level(monkeypatch, memory_otlp, clean_otel_globals):
    monkeypatch.setenv("ENVIRONMENT", "local_dev")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    app = FastAPI()

    with _root_logger_as_found_by_a_fresh_process() as root:
        init_service(app, "svc", enable_otel=True, log_level=logging.DEBUG)
        try:
            assert root.level == logging.DEBUG
        finally:
            app.state.telemetry_providers.shutdown()


def test_log_level_is_ignored_while_telemetry_is_off(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "local_dev")

    with _root_logger_as_found_by_a_fresh_process() as root:
        init_service(FastAPI(), "svc", enable_otel=True)

        assert root.level == logging.WARNING
        assert root.handlers == []
