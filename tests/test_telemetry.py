"""Tests for infra2_sdk.telemetry."""

from __future__ import annotations

import json
import logging
import sys
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor

from infra2_sdk import _core, telemetry


def test_otel_settings_strict_and_lenient() -> None:
    for obsolete in (
        "OtelSettings",
        "configure_telemetry",
        "configure_logging",
        "JsonLogFormatter",
        "bind_request_id",
        "signal_endpoint",
        "PROBE_PATHS",
        "SignalKind",
        "REQUIRED_IDENTITY_ATTRIBUTES",
    ):
        assert not hasattr(telemetry, obsolete)

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
    settings = _core.OtelSettings.from_env(env, strict=True, require_issued_identity=True)
    assert settings.service_name == "billing-api"
    assert settings.export_interval_millis == 15000
    assert (
        _core.signal_endpoint("http://collector:4318", "traces")
        == "http://collector:4318/v1/traces"
    )
    assert (
        _core.signal_endpoint("http://collector:4318/v1/traces", "traces")
        == "http://collector:4318/v1/traces"
    )
    with pytest.raises(ValueError, match="OTLP endpoint"):
        _core.signal_endpoint("", "traces")

    preview_s = _core.OtelSettings(
        service_name="svc",
        endpoint="http://127.0.0.1:4318",
        environment="preview",
        deployment_environment="pr_12",
    )
    assert preview_s.deployment_environment == "pr-12"

    attrs = settings.resolved_resource_attributes()
    for key in _core._REQUIRED_IDENTITY_ATTRIBUTES:
        assert key in attrs
    assert attrs["service.instance.id"] == "inst-1"

    with pytest.raises(ValueError, match="OTEL_TRACES_SAMPLER"):
        _core.OtelSettings.from_env({**env, "OTEL_TRACES_SAMPLER": "invalid"}, strict=True)
    with pytest.raises(ValueError, match="OTEL_TRACES_SAMPLER_ARG"):
        _core.OtelSettings.from_env({**env, "OTEL_TRACES_SAMPLER_ARG": "2.5"}, strict=True)
    with pytest.raises(ValueError, match="OTEL_METRIC_EXPORT_INTERVAL"):
        _core.OtelSettings.from_env({**env, "OTEL_METRIC_EXPORT_INTERVAL": "abc"}, strict=True)
    with pytest.raises(ValueError, match="disagrees"):
        _core.OtelSettings(
            service_name="svc",
            endpoint="http://127.0.0.1:4318",
            environment="production",
            deployment_environment="staging",
        )

    with pytest.warns(RuntimeWarning):
        lenient = _core.OtelSettings.from_env(
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


def test_json_logging_preserves_safe_keys_and_scrubs_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:supersecretpw@db:5432/app")
    formatter = _core.JsonLogFormatter({"service.name": "demo"})
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
    record.err_obj = RuntimeError("postgresql://user:supersecretpw@db:5432/app")
    with (
        _core.bind_request_id("req-42") as rid,
        telemetry.bind_log_context(
            infra_key="v1",
            input_tokens=42,
            output_tokens=17,
            s3_key="docs/report.pdf",
            author="alice",
            authenticated=True,
            authorization="Bearer secret",
            raw_prompt="sensitive prompt",
            nested={"token": "x"},
        ),
    ):
        assert telemetry.current_request_id() == "req-42" == rid
        line = formatter.format(record)
    doc = json.loads(line)
    assert doc["request_id"] == "req-42"
    assert doc["infra_key"] == "v1"
    assert doc["input_tokens"] == 42
    assert doc["output_tokens"] == 17
    assert doc["s3_key"] == "docs/report.pdf"
    assert doc["author"] == "alice"
    assert doc["authenticated"] is True
    assert doc["authorization"] == "[REDACTED]"
    assert doc["raw_prompt"] == "[REDACTED]"
    assert doc["nested"] == {"token": "[REDACTED]"}
    assert "supersecretpw" not in line
    assert "user@example.com" not in line
    assert "[EMAIL]" in doc["message"]
    assert "exception" in doc and "supersecretpw" not in doc["exception"]

    with _core.bind_request_id("bad\nid") as sanitized:
        assert sanitized != "bad\nid" and len(sanitized) == 36

    _core.configure_logging(level=logging.INFO, service_name="demo", suppress_probes=True)
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


def test_init_worker_sampler_otlp_request_id_and_shutdown() -> None:
    from opentelemetry.sdk.trace import sampling

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return None

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    Thread(target=srv.serve_forever, daemon=True).start()
    try:
        providers = telemetry.init_worker(
            "demo-worker",
            environ={
                "ENVIRONMENT": "staging",
                "OTEL_SERVICE_NAME": "demo-worker",
                "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{srv.server_port}",
                "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
                "OTEL_TRACES_SAMPLER_ARG": "1.0",
            },
            log_level=None,
            set_global=False,
        )
        assert isinstance(providers, telemetry.TelemetryProviders)
        settings = _core.OtelSettings(
            service_name="demo-sampler",
            endpoint=f"http://127.0.0.1:{srv.server_port}",
            environment="staging",
            traces_sampler="parentbased_traceidratio",
            traces_sampler_arg="1.0",
            suppress_probe_telemetry=True,
        )
        sampler_p = _core.configure_telemetry(settings, set_global=False, capture_logs=False)
        sampler = sampler_p.tracer_provider.sampler
        assert "DropProbeSampler" in sampler.get_description()
        assert sampler.should_sample(None, 1, "GET /readyz").decision is sampling.Decision.DROP
        api_res = sampler.should_sample(
            None, 1, "GET /api/todos", attributes={"url.path": "/api/todos"}
        )
        assert api_res.decision is sampling.Decision.RECORD_AND_SAMPLE

        # Verify OTLP log record processor injects request_id from current_request_id().
        from opentelemetry.sdk._logs import LoggingHandler

        mem_logs = InMemoryLogRecordExporter()
        assert providers.logger_provider is not None
        providers.logger_provider.add_log_record_processor(SimpleLogRecordProcessor(mem_logs))
        otlp_h = LoggingHandler(level=logging.INFO, logger_provider=providers.logger_provider)
        test_logger = logging.getLogger("telemetry.otlp.test")
        test_logger.setLevel(logging.INFO)
        test_logger.addHandler(otlp_h)
        try:
            with (
                _core.bind_request_id("req-otlp-7"),
                telemetry.bind_log_context(input_tokens=9),
            ):
                test_logger.info("hello otlp")
            assert providers.force_flush() is True
            emitted = [r.log_record for r in mem_logs.get_finished_logs()]
            assert emitted
            rec_attrs = dict(emitted[-1].attributes or {})
            assert rec_attrs["request_id"] == "req-otlp-7"
            assert rec_attrs["input_tokens"] == 9
        finally:
            test_logger.removeHandler(otlp_h)

        tracer = telemetry.get_tracer("demo", tracer_provider=providers.tracer_provider)
        with tracer.start_as_current_span("op"):
            pass
        meter = telemetry.get_meter("demo", meter_provider=providers.meter_provider)
        meter.create_counter("demo.count").add(1)
        providers.shutdown()
        sampler_p.shutdown()

        # Verify shutdown isolates provider errors so remaining providers still close.
        closed: list[str] = []

        class _BoomProvider:
            def shutdown(self) -> None:
                raise RuntimeError("boom")

        class _OkProvider:
            def shutdown(self) -> None:
                closed.append("ok")

        partial_p = telemetry.TelemetryProviders(
            logger_provider=_BoomProvider(),
            meter_provider=_OkProvider(),
            tracer_provider=_OkProvider(),
        )
        partial_p.shutdown()
        assert closed == ["ok", "ok"]

        with pytest.warns(RuntimeWarning):
            assert (
                telemetry.init_worker(
                    "bad-worker",
                    environ={"ENVIRONMENT": "staging", "OTEL_EXPORTER_OTLP_ENDPOINT": "not-a-url"},
                    strict=False,
                    set_global=False,
                )
                is None
            )
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
        with _core.bind_request_id("r1"), telemetry.bind_log_context(foo="bar"):
            assert fake_cv._data["request_id"] == "r1"
            assert fake_cv._data["foo"] == "bar"
        assert "foo" not in fake_cv._data
    finally:
        sys.modules.pop("structlog", None)


def test_probe_paths_and_worker_run_sigterm_and_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    import signal

    assert "/api/health" in _core.PROBE_PATHS

    captured_env: list[str] = []
    flushed = [False]
    shut_down = [False]
    sigterm_cb_fired = [False]

    class _FakeProviders:
        def force_flush(self) -> bool:
            flushed[0] = True
            return True

        def shutdown(self) -> None:
            shut_down[0] = True

    def _fake_init_worker(name: str, *, environ=None, **_kw):
        captured_env.append(str((environ or {}).get("OTEL_RESOURCE_ATTRIBUTES", "")))
        return _FakeProviders()

    monkeypatch.setattr(telemetry, "init_worker", _fake_init_worker)
    orig_handler = signal.getsignal(signal.SIGTERM)
    with telemetry.worker_run(
        "demo-worker",
        environ={"OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=staging"},
        on_sigterm=lambda: sigterm_cb_fired.__setitem__(0, True),
    ):
        assert "service.instance.id=" in captured_env[0]
        active_handler = signal.getsignal(signal.SIGTERM)
        assert callable(active_handler)
        with pytest.raises(SystemExit) as sys_exit:
            active_handler(signal.SIGTERM, None)
        assert sys_exit.value.code == 128 + signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) is orig_handler
    assert sigterm_cb_fired[0] is True
    assert flushed[0] is True
    assert shut_down[0] is True
