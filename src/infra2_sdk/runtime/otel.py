"""Explicit OpenTelemetry bootstrap using OTLP."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from warnings import warn

from infra2_sdk.runtime._optional import require
from infra2_sdk.runtime._otel_env import parse_otel_boolean, parse_traces_sampler
from infra2_sdk.runtime.environment import (
    EnvironmentTier,
    RuntimeEnvironment,
    RuntimeEnvKey,
    _resolve_service_identity,
    env_int,
    is_preview_alias,
    normalize_deployment_environment,
    resolve_env,
    resolve_environment_tier,
    resolve_runtime_env,
)

_INTERVAL_MESSAGE = "export_interval_millis must be positive"

REQUIRED_IDENTITY_ATTRIBUTES: tuple[str, ...] = (
    "deployment.environment.name",
    "infra.service.id",
    "service.version",
    "infra.iac.ref",
)


@dataclass(frozen=True)
class OtelSettings:
    service_name: str
    endpoint: str | None = None
    service_version: str = "unknown"
    environment: str = "local_dev"
    deployment_environment: str = field(default="", kw_only=True)
    instance_id: str = ""
    resource_attributes: Mapping[str, str] = field(default_factory=dict)
    enabled: bool = True
    export_interval_millis: int = 60_000
    traces_sampler: str = field(default="", kw_only=True)
    traces_sampler_arg: str = field(default="", kw_only=True)
    #: True when ``OTEL_SDK_DISABLED`` is true. ``from_env`` then sets ``endpoint`` to ``None``.
    sdk_disabled: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        tier = resolve_environment_tier(self.environment)
        object.__setattr__(self, "environment", tier.value)
        display = normalize_deployment_environment(self.deployment_environment, tier)
        object.__setattr__(self, "deployment_environment", display)
        if not self.service_name:
            raise ValueError("service_name is required")
        if self.enabled and not self.endpoint:
            raise ValueError("enabled telemetry requires an OTLP HTTP endpoint")
        if self.endpoint:
            _validate_endpoint(self.endpoint)
        if self.export_interval_millis <= 0:
            raise ValueError(_INTERVAL_MESSAGE)
        sampler, _ = parse_traces_sampler(self.traces_sampler, self.traces_sampler_arg)
        object.__setattr__(self, "traces_sampler", sampler if self.traces_sampler.strip() else "")
        object.__setattr__(self, "traces_sampler_arg", self.traces_sampler_arg.strip())
        if any(
            not isinstance(key, str) or not key or not isinstance(value, str)
            for key, value in self.resource_attributes.items()
        ):
            raise ValueError("resource attributes must have non-empty string keys and values")

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        strict: bool = False,
        default_service_name: str | None = None,
        require_issued_identity: bool = False,
    ) -> OtelSettings:
        """Build the settings from the environment.

        The service name is ``OTEL_SERVICE_NAME``, then ``service.name`` in
        ``OTEL_RESOURCE_ATTRIBUTES``, then ``default_service_name``, then ``unknown_service``.
        """

        resolver_kwargs: dict[str, Any] = {
            "strict": strict,
            "default_service_name": default_service_name,
        }
        if require_issued_identity:
            resolver_kwargs["ignore_environment"] = True
        identity = _resolve_service_identity(environ, **resolver_kwargs)
        endpoint = resolve_runtime_env(environ, RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT).value
        try:
            disabled = parse_otel_boolean(resolve_env(environ, "OTEL_SDK_DISABLED").value)
        except ValueError as exc:
            if strict:
                raise ValueError(f"invalid OTEL_SDK_DISABLED: {exc}") from exc
            warn(f"invalid OTEL_SDK_DISABLED: {exc}", RuntimeWarning, stacklevel=2)
            disabled = False
        effective_endpoint = None if disabled else endpoint
        if require_issued_identity and effective_endpoint:
            missing_identity: list[str] = []
            if resolve_runtime_env(environ, RuntimeEnvKey.SERVICE_NAME).value is None:
                missing_identity.append(RuntimeEnvKey.SERVICE_NAME.value)
            for attr in REQUIRED_IDENTITY_ATTRIBUTES:
                if not identity.attributes.get(attr, "").strip():
                    missing_identity.append(attr)
            if missing_identity:
                raise ValueError(
                    "telemetry requires ServiceIdentity attributes when an OTLP endpoint is set; "
                    f"missing: {', '.join(missing_identity)}"
                )
        runtime = identity.environment
        deployment_environment = identity.deployment_environment
        try:
            if deployment_environment and (
                not identity.environment_is_set or require_issued_identity
            ):
                inferred_tier = resolve_environment_tier(deployment_environment)
                normalized_display = normalize_deployment_environment(
                    deployment_environment,
                    inferred_tier,
                )
                runtime = RuntimeEnvironment(normalized_display, inferred_tier)
                deployment_environment = normalized_display
            elif (
                deployment_environment
                and runtime.tier is EnvironmentTier.STAGING
                and is_preview_alias(deployment_environment)
            ):
                normalized_preview = normalize_deployment_environment(
                    deployment_environment,
                    EnvironmentTier.PREVIEW,
                )
                runtime = RuntimeEnvironment(normalized_preview, EnvironmentTier.PREVIEW)
                deployment_environment = normalized_preview
            elif deployment_environment:
                deployment_environment = normalize_deployment_environment(
                    deployment_environment,
                    runtime.tier,
                )
        except ValueError as exc:
            if strict:
                raise ValueError(f"invalid deployment.environment.name: {exc}") from exc
            raise
        sampler_name = resolve_env(environ, "OTEL_TRACES_SAMPLER").value
        sampler_arg = resolve_env(environ, "OTEL_TRACES_SAMPLER_ARG").value
        sampler_name, sampler_arg = sampler_name or "", sampler_arg or ""
        try:
            parse_traces_sampler(sampler_name, sampler_arg)
        except ValueError as exc:
            if strict:
                raise
            warn(f"invalid OTEL_TRACES_SAMPLER: {exc}", RuntimeWarning, stacklevel=2)
            sampler_name = sampler_arg = ""
        try:
            return cls(
                service_name=identity.service_name or "unknown_service",
                endpoint=effective_endpoint,
                service_version=identity.service_version,
                environment=runtime.tier.value,
                deployment_environment=deployment_environment or runtime.name,
                instance_id=identity.instance_id,
                resource_attributes=identity.attributes,
                enabled=bool(effective_endpoint),
                export_interval_millis=env_int(
                    environ, "OTEL_METRIC_EXPORT_INTERVAL", default=60_000
                ),
                traces_sampler=sampler_name,
                traces_sampler_arg=sampler_arg,
                sdk_disabled=disabled,
            )
        except ValueError as exc:
            if strict and str(exc) == _INTERVAL_MESSAGE:
                raise ValueError(f"invalid OTEL_METRIC_EXPORT_INTERVAL: {exc}") from exc
            raise


@dataclass
class TelemetryProviders:
    tracer_provider: Any | None = None
    meter_provider: Any | None = None
    logger_provider: Any | None = None
    logging_instrumentor: Any | None = None
    logging_handler: Any | None = None

    def shutdown(self) -> None:
        _release_global(self)
        if self.logging_handler is not None:
            logging.getLogger().removeHandler(self.logging_handler)
            self.logging_handler = None
        if self.logging_instrumentor is not None:
            self.logging_instrumentor.uninstrument()
        for provider in (self.logger_provider, self.meter_provider, self.tracer_provider):
            if provider is not None:
                provider.shutdown()


# The OpenTelemetry API lets a process set each global provider exactly once, so a second
# ``set_global=True`` call cannot install anything new. Remembering the active installation
# makes that call return it instead of building (and leaking) a second set of exporters
# and attaching a second logging handler.
_GLOBAL_LOCK = threading.Lock()
_ACTIVE_GLOBAL: tuple[TelemetryProviders, OtelSettings] | None = None


def _release_global(providers: TelemetryProviders) -> None:
    global _ACTIVE_GLOBAL
    with _GLOBAL_LOCK:
        if _ACTIVE_GLOBAL is not None and _ACTIVE_GLOBAL[0] is providers:
            _ACTIVE_GLOBAL = None


def resource_attributes(settings: OtelSettings) -> dict[str, str]:
    attributes = {
        **settings.resource_attributes,
        "service.name": settings.service_name,
        "service.version": settings.service_version,
        "deployment.environment.name": settings.deployment_environment,
    }
    if settings.instance_id:
        attributes["service.instance.id"] = settings.instance_id
    return attributes


def configure_telemetry(
    settings: OtelSettings,
    *,
    set_global: bool = False,
    capture_logs: bool = True,
) -> TelemetryProviders:
    """Build OTLP trace, metric, and log providers; globals change only when requested.

    Without ``set_global`` nothing outside the returned providers is touched. With it:

    * the three providers become the OpenTelemetry globals (the API allows this once per
      process and ``shutdown()`` does not undo it; a provider another component already
      installed is reported with a ``RuntimeWarning`` and stays in place);
    * ``LoggingInstrumentor`` adds ``otelTraceID``/``otelSpanID`` to every ``LogRecord``;
    * unless ``capture_logs=False``, an OpenTelemetry ``LoggingHandler`` bound to *this*
      call's ``LoggerProvider`` is attached to the root logger, so records at or above the
      root logger's level are exported over OTLP. The handler is attached here, not left to
      the instrumentor's version-dependent auto-instrumentation, and ``shutdown()`` removes it.
    * calling it again while the first installation is active returns that installation
      (with a ``RuntimeWarning`` if the settings differ) and changes nothing.

    The sampler follows ``settings.traces_sampler`` / ``traces_sampler_arg`` (loaded by
    ``OtelSettings.from_env`` from ``OTEL_TRACES_SAMPLER`` / ``OTEL_TRACES_SAMPLER_ARG``).
    Ambient ``os.environ`` is not consulted here.
    """

    global _ACTIVE_GLOBAL
    if not settings.enabled:
        return TelemetryProviders()
    with _GLOBAL_LOCK:
        if set_global and _ACTIVE_GLOBAL is not None:
            active, active_settings = _ACTIVE_GLOBAL
            if active_settings != settings:
                warn(
                    "telemetry is already configured globally; ignoring the new settings",
                    RuntimeWarning,
                    stacklevel=2,
                )
            return active
        providers, not_installed = _build_providers(
            settings, set_global=set_global, capture_logs=capture_logs
        )
        if set_global:
            _ACTIVE_GLOBAL = (providers, settings)
    if not_installed:
        warn(
            f"the global {', '.join(not_installed)} provider was already set and stays in "
            "place; this call's provider is not global",
            RuntimeWarning,
            stacklevel=2,
        )
    return providers


def _build_providers(
    settings: OtelSettings,
    *,
    set_global: bool,
    capture_logs: bool,
) -> tuple[TelemetryProviders, list[str]]:
    trace_api = require("opentelemetry.trace", extra="otel")
    metrics_api = require("opentelemetry.metrics", extra="otel")
    logs_api = require("opentelemetry._logs", extra="otel")
    resources = require("opentelemetry.sdk.resources", extra="otel")
    trace_sdk = require("opentelemetry.sdk.trace", extra="otel")
    trace_export = require("opentelemetry.sdk.trace.export", extra="otel")
    sampling = require("opentelemetry.sdk.trace.sampling", extra="otel")
    metric_sdk = require("opentelemetry.sdk.metrics", extra="otel")
    metric_export = require("opentelemetry.sdk.metrics.export", extra="otel")
    log_sdk = require("opentelemetry.sdk._logs", extra="otel")
    log_export = require("opentelemetry.sdk._logs.export", extra="otel")
    otlp_trace = require("opentelemetry.exporter.otlp.proto.http.trace_exporter", extra="otel")
    otlp_metric = require("opentelemetry.exporter.otlp.proto.http.metric_exporter", extra="otel")
    otlp_log = require("opentelemetry.exporter.otlp.proto.http._log_exporter", extra="otel")

    resource = resources.Resource.create(resource_attributes(settings))
    tracer_provider = trace_sdk.TracerProvider(
        resource=resource, sampler=_build_sampler(sampling, settings)
    )
    tracer_provider.add_span_processor(
        trace_export.BatchSpanProcessor(
            otlp_trace.OTLPSpanExporter(endpoint=signal_endpoint(settings.endpoint, "traces"))
        )
    )
    metric_reader = metric_export.PeriodicExportingMetricReader(
        otlp_metric.OTLPMetricExporter(endpoint=signal_endpoint(settings.endpoint, "metrics")),
        export_interval_millis=settings.export_interval_millis,
    )
    meter_provider = metric_sdk.MeterProvider(resource=resource, metric_readers=[metric_reader])
    logger_provider = log_sdk.LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        log_export.BatchLogRecordProcessor(
            otlp_log.OTLPLogExporter(endpoint=signal_endpoint(settings.endpoint, "logs"))
        )
    )
    providers = TelemetryProviders(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
    )
    if not set_global:
        return providers, []

    trace_api.set_tracer_provider(tracer_provider)
    metrics_api.set_meter_provider(meter_provider)
    logs_api.set_logger_provider(logger_provider)
    not_installed = [
        signal
        for signal, installed in (
            ("tracer", trace_api.get_tracer_provider() is tracer_provider),
            ("meter", metrics_api.get_meter_provider() is meter_provider),
            ("logger", logs_api.get_logger_provider() is logger_provider),
        )
        if not installed
    ]
    logging_module = require("opentelemetry.instrumentation.logging", extra="otel")
    instrumentor = logging_module.LoggingInstrumentor()
    instrumentor.instrument(
        tracer_provider=tracer_provider,
        inject_trace_context=True,
        # Exporting is the explicit handler's job below; leaving the instrumentor's own
        # auto-instrumentation on would add a second handler on some versions.
        enable_log_auto_instrumentation=False,
    )
    providers.logging_instrumentor = instrumentor
    if capture_logs:
        handler = _logging_handler_class()(level=logging.NOTSET, logger_provider=logger_provider)
        logging.getLogger().addHandler(handler)
        providers.logging_handler = handler
    return providers, not_installed


def _build_sampler(sampling: Any, settings: OtelSettings) -> Any:
    name, ratio = parse_traces_sampler(settings.traces_sampler, settings.traces_sampler_arg)
    root = {"always_on": sampling.ALWAYS_ON, "always_off": sampling.ALWAYS_OFF}.get(
        name.removeprefix("parentbased_")
    )
    if root is None:
        root = sampling.TraceIdRatioBased(ratio)
    return sampling.ParentBased(root) if name.startswith("parentbased_") else root


def _logging_handler_class() -> Any:
    """The OpenTelemetry stdlib-logging handler: the instrumentation package's own handler
    where it ships one, else the SDK's (which newer SDKs deprecate)."""

    try:
        return require("opentelemetry.instrumentation.logging.handler", extra="otel").LoggingHandler
    except RuntimeError:
        return require("opentelemetry.sdk._logs", extra="otel").LoggingHandler


def signal_endpoint(base: str | None, signal: str) -> str:
    """Derive the OTLP/HTTP URL of one signal (``traces``, ``metrics`` or ``logs``) from a base
    endpoint: any trailing ``/v1/<signal>`` is replaced, the query string is preserved and the
    fragment dropped."""

    if base is None:
        raise ValueError("OTLP endpoint is required")
    endpoint = urlsplit(base)
    cleaned = endpoint.path.rstrip("/")
    for known_signal in ("traces", "metrics", "logs"):
        suffix = f"/v1/{known_signal}"
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    path = cleaned + f"/v1/{signal}"
    return urlunsplit((endpoint.scheme, endpoint.netloc, path, endpoint.query, ""))


def _validate_endpoint(value: str) -> None:
    try:
        endpoint = urlsplit(value)
        hostname = endpoint.hostname
        _port = endpoint.port
    except ValueError:
        raise ValueError("OTLP endpoint must be a valid HTTP URL") from None
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.netloc
        or not hostname
        or any(char.isspace() for char in endpoint.netloc)
    ):
        raise ValueError("OTLP endpoint must use http:// or https:// with a host")
    # urlsplit returns a username (maybe "") whenever a password exists: one check is enough.
    if endpoint.username is not None:
        raise ValueError("OTLP endpoint must not contain credentials")
    if endpoint.fragment:
        raise ValueError("OTLP endpoint must not contain a fragment")


__all__ = [
    "OtelSettings",
    "REQUIRED_IDENTITY_ATTRIBUTES",
    "configure_telemetry",
    "signal_endpoint",
]
