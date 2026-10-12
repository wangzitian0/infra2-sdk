import logging
import sys
from pathlib import Path

import pytest

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


# --- in-memory OpenTelemetry harness: no network, no ambient global state ----------------------
# Shared by tests/test_runtime_otel.py and tests/test_fastapi.py.


class _MemoryOtlp:
    """Replaces the OTLP/HTTP exporters with in-memory ones and records their construction."""

    def __init__(self) -> None:
        from opentelemetry.sdk._logs import export as log_export
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        # Newer SDKs renamed the in-memory log exporter and deprecate the old name.
        memory_logs = getattr(log_export, "InMemoryLogRecordExporter", None)
        self.spans = InMemorySpanExporter()
        self.logs = (memory_logs or log_export.InMemoryLogExporter)()
        self.endpoints: list[tuple[str, str]] = []

    def metric_exporter(self, endpoint: str):
        from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult

        class Memory(MetricExporter):
            def export(self, metrics_data, timeout_millis=10_000, **kwargs):
                return MetricExportResult.SUCCESS

            def force_flush(self, timeout_millis=10_000):
                return True

            def shutdown(self, timeout_millis=30_000, **kwargs):
                return None

        self.endpoints.append(("metrics", endpoint))
        return Memory()


@pytest.fixture
def memory_otlp(monkeypatch):
    import opentelemetry.exporter.otlp.proto.http._log_exporter as log_exporter
    import opentelemetry.exporter.otlp.proto.http.metric_exporter as metric_exporter
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as trace_exporter

    memory = _MemoryOtlp()

    def span_exporter(*, endpoint):
        memory.endpoints.append(("traces", endpoint))
        return memory.spans

    def log_exporter_factory(*, endpoint):
        memory.endpoints.append(("logs", endpoint))
        return memory.logs

    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", span_exporter)
    monkeypatch.setattr(log_exporter, "OTLPLogExporter", log_exporter_factory)
    monkeypatch.setattr(
        metric_exporter, "OTLPMetricExporter", lambda *, endpoint: memory.metric_exporter(endpoint)
    )
    return memory


@pytest.fixture
def clean_otel_globals():
    """OpenTelemetry providers are set-once per process: reset them around a test."""
    from opentelemetry import trace
    from opentelemetry._logs import _internal as logs_internal
    from opentelemetry.metrics import _internal as metrics_internal
    from opentelemetry.util._once import Once

    from infra2_sdk.runtime import otel as otel_module

    targets = (
        (trace, "_TRACER_PROVIDER_SET_ONCE", "_TRACER_PROVIDER"),
        (metrics_internal, "_METER_PROVIDER_SET_ONCE", "_METER_PROVIDER"),
        (logs_internal, "_LOGGER_PROVIDER_SET_ONCE", "_LOGGER_PROVIDER"),
    )
    saved = [
        (module, once, getattr(module, once), provider, getattr(module, provider))
        for module, once, provider in targets
    ]
    root = logging.getLogger()
    handlers_before = list(root.handlers)

    def reset() -> None:
        for module, once, _, provider, _ in saved:
            setattr(module, once, Once())
            setattr(module, provider, None)
        otel_module._ACTIVE_GLOBAL = None

    reset()
    yield
    from opentelemetry.instrumentation.logging import LoggingInstrumentor

    instrumentor = LoggingInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:  # a failed test must not leak it
        instrumentor.uninstrument()
    for handler in list(root.handlers):
        if handler not in handlers_before:
            root.removeHandler(handler)
    for module, once, once_value, provider, provider_value in saved:
        setattr(module, once, once_value)
        setattr(module, provider, provider_value)
    otel_module._ACTIVE_GLOBAL = None
