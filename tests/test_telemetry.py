"""Tests for infra2_sdk.telemetry."""

from __future__ import annotations

import json
import logging
import sys
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from infra2_sdk import telemetry


def test_otel_settings_strict_and_lenient() -> None:
    env = {
        "ENVIRONMENT": "staging",
        "OTEL_SERVICE_NAME": "billing-api",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
        "OTEL_RESOURCE_ATTRIBUTES": (
            "deployment.environment.name=staging,"
            "infra.service.id=platform/billing,"
            "service.version=1.2.3,"
            "infra.iac.ref=0123456789abcdef0123456789abcdef01234567,"
            "service.instance.id=inst-1"
        ),
        "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
        "OTEL_TRACES_SAMPLER_ARG": "0.5",
        "OTEL_METRIC_EXPORT_INTERVAL": "15000",
    }
    settings = telemetry.OtelSettings.from_env(env, strict=True, require_issued_identity=True)
    assert settings.service_name == "billing-api"
    assert settings.traces_sampler_arg == "0.5"
    assert settings.export_interval_millis == 15000
    assert settings.enabled is True
    assert (
        telemetry.signal_endpoint("http://collector:4318", "traces")
        == "http://collector:4318/v1/traces"
    )
    assert (
        telemetry.signal_endpoint("http://collector:4318/v1/traces", "traces")
        == "http://collector:4318/v1/traces"
    )
    attrs = settings.resolved_resource_attributes()
    for key in telemetry.REQUIRED_IDENTITY_ATTRIBUTES:
        assert key in attrs
    assert attrs["service.instance.id"] == "inst-1"

    with pytest.raises(ValueError, match="OTEL_TRACES_SAMPLER"):
        telemetry.OtelSettings.from_env(
            {**env, "OTEL_TRACES_SAMPLER": "invalid_sampler"}, strict=True
        )
    with pytest.raises(ValueError, match="OTEL_TRACES_SAMPLER_ARG"):
        telemetry.OtelSettings.from_env({**env, "OTEL_TRACES_SAMPLER_ARG": "2.5"}, strict=True)
    with pytest.raises(ValueError, match="OTEL_METRIC_EXPORT_INTERVAL"):
        telemetry.OtelSettings.from_env({**env, "OTEL_METRIC_EXPORT_INTERVAL": "abc"}, strict=True)
    with pytest.raises(ValueError, match="issued identity attributes"):
        telemetry.OtelSettings.from_env(
            {
                "ENVIRONMENT": "staging",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
                "OTEL_SERVICE_NAME": "svc",
                "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=staging",
            },
            strict=True,
            require_issued_identity=True,
        )
    with pytest.raises(ValueError, match="disagrees"):
        telemetry.OtelSettings(
            service_name="svc",
            endpoint="http://127.0.0.1:4318",
            environment="production",
            deployment_environment="staging",
        )

    with pytest.warns(RuntimeWarning):
        lenient = telemetry.OtelSettings.from_env(
            {
                "ENVIRONMENT": "local",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
                "OTEL_RESOURCE_ATTRIBUTES": "bad-entry",
                "OTEL_TRACES_SAMPLER": "bad",
                "OTEL_METRIC_EXPORT_INTERVAL": "-5",
                "OTEL_SDK_DISABLED": "notabool",
            },
            strict=False,
            default_service_name="svc",
        )
    assert lenient.export_interval_millis == 60_000


def test_json_logging_contextvars_and_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:supersecretpw@db:5432/app")
    formatter = telemetry.JsonLogFormatter({"service.name": "demo"})
    try:
        raise RuntimeError("boom secret postgresql://user:supersecretpw@db:5432/app")
    except RuntimeError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="demo.log",
        level=logging.INFO,
        pathname=__file__,
        lineno=42,
        msg="failed connecting user@example.com",
        args=(),
        exc_info=exc_info,
    )
    with (
        telemetry.bind_request_id("req-42") as rid,
        telemetry.bind_log_context(
            infra_key="v1", authorization="Bearer secret", nested={"token": "x"}
        ),
    ):
        assert telemetry.current_request_id() == "req-42" == rid
        line = formatter.format(record)
    doc = json.loads(line)
    assert doc["request_id"] == "req-42"
    assert "supersecretpw" not in line
    assert "user@example.com" not in line
    assert "[EMAIL]" in doc["message"]
    assert doc["authorization"] == "[REDACTED]"
    assert "exception" in doc and "supersecretpw" not in doc["exception"]

    telemetry.configure_logging(level=logging.INFO, service_name="demo", suppress_probes=True)
    access = logging.getLogger("uvicorn.access")
    assert access.filters
    probe_rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/1.1" %d',
        ("127.0.0.1", "GET", "/readyz", 200),
        None,
    )
    assert access.filters[-1].filter(probe_rec) is False


def test_configure_telemetry_sampler_and_init_worker() -> None:
    from opentelemetry.sdk.trace import sampling

    received: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            received.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return None

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    th = Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        settings = telemetry.OtelSettings(
            service_name="demo-sampler",
            endpoint=f"http://127.0.0.1:{srv.server_port}",
            environment="staging",
            traces_sampler="parentbased_traceidratio",
            traces_sampler_arg="1.0",
            suppress_probe_telemetry=True,
        )
        providers = telemetry.configure_telemetry(settings, set_global=False, capture_logs=True)
        sampler = providers.tracer_provider.sampler
        assert "DropProbeSampler" in sampler.get_description()
        assert sampler.should_sample(None, 1, "GET /readyz").decision is sampling.Decision.DROP
        api_res = sampler.should_sample(
            None, 1, "GET /api/todos", attributes={"url.path": "/api/todos"}
        )
        assert api_res.decision is sampling.Decision.RECORD_AND_SAMPLE
        providers.shutdown()

        worker_p = telemetry.init_worker(
            "demo-worker",
            environ={
                "ENVIRONMENT": "staging",
                "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{srv.server_port}",
                "OTEL_SERVICE_NAME": "demo-worker",
                "OTEL_RESOURCE_ATTRIBUTES": (
                    "deployment.environment.name=staging,"
                    "infra.service.id=platform/demo,"
                    "service.version=1.0.0,"
                    "infra.iac.ref=0123456789abcdef0123456789abcdef01234567"
                ),
            },
            strict=True,
            require_issued_identity=True,
            set_global=False,
        )
        assert worker_p is not None
        p_global = telemetry.configure_telemetry(settings, set_global=True, capture_logs=False)
        p_same = telemetry.configure_telemetry(settings, set_global=True, capture_logs=False)
        assert p_same is p_global
        with pytest.warns(RuntimeWarning, match="already configured"):
            telemetry.configure_telemetry(
                settings.model_copy(update={"service_name": "other"}),
                set_global=True,
                capture_logs=False,
            )
        p_global.shutdown()
        noop = telemetry.init_worker("noop", environ={"ENVIRONMENT": "local"}, set_global=False)
        assert noop is None
        worker_p.shutdown()
    finally:
        srv.shutdown()
        srv.server_close()

    fake_cv = types.SimpleNamespace(
        _data={},
        get_contextvars=lambda: fake_cv._data,
        bind_contextvars=lambda **kw: fake_cv._data.update(kw),
        unbind_contextvars=lambda *ks: [fake_cv._data.pop(k, None) for k in ks],
    )
    fake_sl = types.ModuleType("structlog")
    fake_sl.contextvars = fake_cv  # type: ignore[attr-defined]
    sys.modules["structlog"] = fake_sl
    try:
        with telemetry.bind_request_id("r1"), telemetry.bind_log_context(foo="bar"):
            assert fake_cv._data["request_id"] == "r1"
            assert fake_cv._data["foo"] == "bar"
        assert "foo" not in fake_cv._data
    finally:
        sys.modules.pop("structlog", None)
