"""Explicit OpenTelemetry bootstrap using OTLP and W3C Trace Context."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from infra2_sdk.runtime._optional import require
from infra2_sdk.runtime.environment import resolve_environment_tier
from infra2_sdk.runtime.identity import RuntimeIdentity


@dataclass(frozen=True)
class OtelSettings:
    service_name: str
    endpoint: str | None = None
    service_version: str = "unknown"
    environment: str = "local_dev"
    instance_id: str = ""
    resource_attributes: Mapping[str, str] = field(default_factory=dict)
    enabled: bool = True
    export_interval_millis: int = 60_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", resolve_environment_tier(self.environment).value)
        if not self.service_name:
            raise ValueError("service_name is required")
        if self.enabled and not self.endpoint:
            raise ValueError("enabled telemetry requires an OTLP HTTP endpoint")
        if self.endpoint and not self.endpoint.startswith(("http://", "https://")):
            raise ValueError("OTLP endpoint must use http:// or https://")
        if self.export_interval_millis <= 0:
            raise ValueError("export_interval_millis must be positive")
        if any(
            not isinstance(key, str) or not key or not isinstance(value, str)
            for key, value in self.resource_attributes.items()
        ):
            raise ValueError("resource attributes must have non-empty string keys and values")

    @classmethod
    def from_identity(
        cls,
        identity: RuntimeIdentity,
        *,
        endpoint: str | None,
        enabled: bool = True,
        resource_attributes: dict[str, str] | None = None,
    ) -> OtelSettings:
        attributes = dict(resource_attributes or {})
        attributes.update(identity.to_otel_resource_attributes())
        return cls(
            service_name=identity.service_name,
            service_version=identity.service_version,
            environment=identity.environment.value,
            instance_id=identity.instance_id,
            endpoint=endpoint,
            enabled=enabled,
            resource_attributes=attributes,
        )


@dataclass
class TelemetryProviders:
    tracer_provider: Any | None = None
    meter_provider: Any | None = None
    logger_provider: Any | None = None
    logging_instrumentor: Any | None = None

    def shutdown(self) -> None:
        if self.logging_instrumentor is not None:
            self.logging_instrumentor.uninstrument()
        for provider in (self.logger_provider, self.meter_provider, self.tracer_provider):
            if provider is not None:
                provider.shutdown()


def resource_attributes(settings: OtelSettings) -> dict[str, str]:
    attributes = {
        **settings.resource_attributes,
        "service.name": settings.service_name,
        "service.version": settings.service_version,
        "deployment.environment.name": settings.environment,
    }
    if settings.instance_id:
        attributes["service.instance.id"] = settings.instance_id
    return attributes


def configure_telemetry(
    settings: OtelSettings,
    *,
    set_global: bool = False,
) -> TelemetryProviders:
    """Build OTLP trace, metric, and log providers; globals change only when requested."""

    if not settings.enabled:
        return TelemetryProviders()

    trace_api = require("opentelemetry.trace", extra="otel")
    metrics_api = require("opentelemetry.metrics", extra="otel")
    logs_api = require("opentelemetry._logs", extra="otel")
    resources = require("opentelemetry.sdk.resources", extra="otel")
    trace_sdk = require("opentelemetry.sdk.trace", extra="otel")
    trace_export = require("opentelemetry.sdk.trace.export", extra="otel")
    metric_sdk = require("opentelemetry.sdk.metrics", extra="otel")
    metric_export = require("opentelemetry.sdk.metrics.export", extra="otel")
    log_sdk = require("opentelemetry.sdk._logs", extra="otel")
    log_export = require("opentelemetry.sdk._logs.export", extra="otel")
    otlp_trace = require("opentelemetry.exporter.otlp.proto.http.trace_exporter", extra="otel")
    otlp_metric = require("opentelemetry.exporter.otlp.proto.http.metric_exporter", extra="otel")
    otlp_log = require("opentelemetry.exporter.otlp.proto.http._log_exporter", extra="otel")

    resource = resources.Resource.create(resource_attributes(settings))
    tracer_provider = trace_sdk.TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        trace_export.BatchSpanProcessor(
            otlp_trace.OTLPSpanExporter(endpoint=_signal_endpoint(settings.endpoint, "traces"))
        )
    )
    metric_reader = metric_export.PeriodicExportingMetricReader(
        otlp_metric.OTLPMetricExporter(endpoint=_signal_endpoint(settings.endpoint, "metrics")),
        export_interval_millis=settings.export_interval_millis,
    )
    meter_provider = metric_sdk.MeterProvider(resource=resource, metric_readers=[metric_reader])
    logger_provider = log_sdk.LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        log_export.BatchLogRecordProcessor(
            otlp_log.OTLPLogExporter(endpoint=_signal_endpoint(settings.endpoint, "logs"))
        )
    )
    logging_instrumentor = None
    if set_global:
        trace_api.set_tracer_provider(tracer_provider)
        metrics_api.set_meter_provider(meter_provider)
        logs_api.set_logger_provider(logger_provider)
        logging_module = require("opentelemetry.instrumentation.logging", extra="otel")
        logging_instrumentor = logging_module.LoggingInstrumentor()
        logging_instrumentor.instrument(
            tracer_provider=tracer_provider,
            inject_trace_context=True,
            enable_log_auto_instrumentation=True,
        )
    return TelemetryProviders(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        logging_instrumentor=logging_instrumentor,
    )


def inject_trace_context(headers: dict[str, str] | None = None) -> dict[str, str]:
    """Inject the configured W3C trace context into an HTTP-compatible carrier."""

    propagate = require("opentelemetry.propagate", extra="otel")
    carrier = dict(headers or {})
    propagate.inject(carrier)
    return carrier


def _signal_endpoint(base: str | None, signal: str) -> str:
    if base is None:
        raise ValueError("OTLP endpoint is required")
    cleaned = base.rstrip("/")
    for known_signal in ("traces", "metrics", "logs"):
        suffix = f"/v1/{known_signal}"
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    suffix = f"/v1/{signal}"
    return cleaned + suffix
