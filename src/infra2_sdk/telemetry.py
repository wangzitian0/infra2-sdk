"""OpenTelemetry OTLP bootstrap, structured JSON logs, contextvars, and worker setup."""

from __future__ import annotations

import atexit
import json
import logging
import math
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Collection, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal
from urllib.parse import unquote_to_bytes, urlsplit, urlunsplit
from warnings import warn

from pydantic import Field, field_validator, model_validator

from infra2_sdk._core import (
    ContractModel,
    is_sensitive_header,
    redact_secrets,
    require_optional,
    validate_http_url,
)
from infra2_sdk.config import (
    EnvironmentTier,
    RuntimeEnvironment,
    RuntimeEnvKey,
    read_runtime_env,
    resolve_environment,
    resolve_tier,
)

SignalKind = Literal["traces", "metrics", "logs"]

PROBE_PATHS: Final[frozenset[str]] = frozenset({"/livez", "/readyz", "/health"})
REQUIRED_IDENTITY_ATTRIBUTES: Final[tuple[str, ...]] = (
    "deployment.environment.name",
    "infra.service.id",
    "service.version",
    "infra.iac.ref",
)

_INTERVAL_MESSAGE = "export_interval_millis must be positive"
_INVALID_PERCENT_RE = re.compile(r"%(?![0-9a-fA-F]{2})")
_OTLP_KEY_RE = re.compile(r"(?:infra\.llm\.|code\.|request_id$)")
_RATIO_SAMPLERS = frozenset({"traceidratio", "parentbased_traceidratio"})
_TRACES_SAMPLER_NAMES = (
    "always_on",
    "always_off",
    "traceidratio",
    "parentbased_always_on",
    "parentbased_always_off",
    "parentbased_traceidratio",
)
_DEFAULT_SAMPLER = "parentbased_always_on"

_REQUEST_ID: ContextVar[str | None] = ContextVar("infra2_request_id", default=None)
_LOG_CONTEXT: ContextVar[Mapping[str, Any] | None] = ContextVar("infra2_log_ctx", default=None)

_STD_LOG_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "otelTraceID",
    "otelSpanID",
    "otelTraceSampled",
    "otelServiceName",
}


def _structlog_cvars() -> Any:
    return getattr(sys.modules.get("structlog"), "contextvars", None)


def current_request_id() -> str | None:
    """Return the active request ID bound to the current execution context."""
    return _REQUEST_ID.get()


@contextmanager
def _bind_request_id_cm(request_id: str | None = None) -> Iterator[str]:
    rid = (request_id or "").strip() or str(uuid.uuid4())
    sl = _structlog_cvars()
    prev_sl = sl.get_contextvars().get("request_id") if sl is not None else None
    if sl is not None:
        sl.bind_contextvars(request_id=rid)
    tok = _REQUEST_ID.set(rid)
    try:
        yield rid
    finally:
        if sl is not None:
            if prev_sl is None:
                sl.unbind_contextvars("request_id")
            else:
                sl.bind_contextvars(request_id=prev_sl)
        try:
            _REQUEST_ID.reset(tok)
        except ValueError:
            _REQUEST_ID.set(None)


def bind_request_id(request_id: str | None = None) -> AbstractContextManager[str]:
    """Bind ``request_id`` (or generate a UUID4) across logs and contextvars."""
    return _bind_request_id_cm(request_id)


@contextmanager
def _bind_log_context_cm(**fields: Any) -> Iterator[dict[str, Any]]:
    clean = {k: v for k, v in fields.items() if v is not None}
    if not clean:
        yield dict(_LOG_CONTEXT.get() or {})
        return
    prev = dict(_LOG_CONTEXT.get() or {})
    merged = {**prev, **clean}
    tok = _LOG_CONTEXT.set(merged)
    sl = _structlog_cvars()
    prev_sl: dict[str, Any] = dict(sl.get_contextvars() or {}) if sl is not None else {}
    if sl is not None:
        sl.bind_contextvars(**clean)
    try:
        yield merged
    finally:
        if sl is not None:
            to_unbind = [k for k in clean if k not in prev_sl]
            to_restore = {k: prev_sl[k] for k in clean if k in prev_sl}
            if to_unbind:
                sl.unbind_contextvars(*to_unbind)
            if to_restore:
                sl.bind_contextvars(**to_restore)
        try:
            _LOG_CONTEXT.reset(tok)
        except ValueError:
            _LOG_CONTEXT.set(prev or None)


def bind_log_context(**fields: Any) -> AbstractContextManager[dict[str, Any]]:
    """Bind structured context fields onto JSON logs and OTLP log records."""
    return _bind_log_context_cm(**fields)


def _scrub(val: Any, env: Mapping[str, str], key: str | None = None) -> Any:
    if key and is_sensitive_header(key):
        return "[REDACTED]"
    if isinstance(val, str):
        return redact_secrets(val, env)
    if isinstance(val, Mapping):
        return {k: _scrub(v, env, str(k)) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return type(val)(_scrub(x, env) for x in val)
    return val


class JsonLogFormatter(logging.Formatter):
    """Compact single-line JSON formatter with OTel trace ID and secret scrubbing."""

    def __init__(self, identity: Mapping[str, str] | None = None) -> None:
        super().__init__()
        self._identity = dict(identity or {})

    def format(self, record: logging.LogRecord) -> str:
        env = os.environ
        sl = _structlog_cvars()
        sl_ctx = dict(sl.get_contextvars() or {}) if sl else {}
        ctx = dict(_LOG_CONTEXT.get() or {}) | sl_ctx

        tid, sid = record.__dict__.get("otelTraceID"), record.__dict__.get("otelSpanID")
        if tid and set(str(tid)) != {"0"}:
            tid_s: str | None = str(tid)
            sid_s: str | None = str(sid) if sid else None
        elif (ot := sys.modules.get("opentelemetry.trace")) and (
            sc := ot.get_current_span().get_span_context()
        ).is_valid:
            tid_s, sid_s = format(sc.trace_id, "032x"), format(sc.span_id, "016x")
        else:
            tid_s = sid_s = None

        out: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _scrub(record.getMessage(), env),
            "trace_id": tid_s,
            "span_id": sid_s,
            "request_id": current_request_id() or ctx.get("request_id") or None,
            **self._identity,
        }
        for k, v in (record.__dict__ | ctx).items():
            if k not in _STD_LOG_ATTRS and k not in out:
                out[k] = _scrub(v, env, str(k))
        if record.exc_info:
            out["exception"] = _scrub(self.formatException(record.exc_info), env)
        return json.dumps(out, separators=(",", ":"), default=str)


def configure_logging(
    *,
    level: int | None = logging.INFO,
    service_name: str = "",
    environment: str = EnvironmentTier.LOCAL_DEV.value,
    service_version: str = "unknown",
    probe_paths: Collection[str] = PROBE_PATHS,
    suppress_probes: bool = True,
) -> None:
    """Attach the JSON stdout handler once and optionally silence uvicorn probe noise."""
    if level is not None:
        root = logging.getLogger()
        if not root.handlers:
            handler = logging.StreamHandler(sys.stdout)
            ident = {
                "service.name": service_name or "unknown_service",
                "deployment.environment.name": environment,
                "service.version": service_version,
            }
            handler.setFormatter(JsonLogFormatter(ident))
            root.addHandler(handler)
            root.setLevel(level)
    if suppress_probes:
        access = logging.getLogger("uvicorn.access")
        if not any(getattr(f, "_infra2_probe_filter", False) for f in access.filters):
            paths = frozenset(probe_paths)

            class _AccessFilter(logging.Filter):
                _infra2_probe_filter = True

                def filter(self, record: logging.LogRecord) -> bool:
                    if isinstance(record.args, tuple) and len(record.args) >= 3:
                        raw_path = str(record.args[2]).split("?", 1)[0]
                        if raw_path in paths:
                            return False
                    msg = record.getMessage()
                    return not any(f" {p} " in msg or f' "{p} ' in msg for p in paths)

            access.addFilter(_AccessFilter())


def _parse_resource_attributes(value: str) -> dict[str, str]:
    if not value.strip():
        return {}
    attributes: dict[str, str] = {}
    for item in value.split(","):
        if "=" not in item:
            raise ValueError("OTEL_RESOURCE_ATTRIBUTES entries must use key=value")
        ek, raw = (part.strip() for part in item.split("=", 1))
        if _INVALID_PERCENT_RE.search(ek) or _INVALID_PERCENT_RE.search(raw):
            raise ValueError("OTEL_RESOURCE_ATTRIBUTES contains an invalid percent escape")
        try:
            key = unquote_to_bytes(ek).decode("utf-8")
            decoded = unquote_to_bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("OTEL_RESOURCE_ATTRIBUTES contains invalid UTF-8") from None
        if not key or not decoded:
            raise ValueError("OTEL_RESOURCE_ATTRIBUTES entries must be non-empty")
        if key in attributes:
            raise ValueError(f"OTEL_RESOURCE_ATTRIBUTES repeats {key!r}")
        attributes[key] = decoded
    return attributes


def _parse_otel_boolean(value: str | None, *, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    norm = value.strip().lower()
    if norm == "true":
        return True
    if norm == "false":
        return False
    raise ValueError("OpenTelemetry boolean environment variables must be true or false")


def _parse_traces_sampler(name: str | None, arg: str | None = None) -> tuple[str, float | None]:
    norm = (name or "").strip().lower()
    if not norm:
        return _DEFAULT_SAMPLER, None
    if norm not in _TRACES_SAMPLER_NAMES:
        expected = ", ".join(_TRACES_SAMPLER_NAMES)
        raise ValueError(f"unsupported OTEL_TRACES_SAMPLER {norm!r}; expected one of {expected}")
    if norm not in _RATIO_SAMPLERS:
        return norm, None
    raw = (arg or "").strip()
    if not raw:
        return norm, 1.0
    try:
        ratio = float(raw)
    except ValueError:
        raise ValueError("OTEL_TRACES_SAMPLER_ARG must be a number between 0 and 1") from None
    if not math.isfinite(ratio) or not 0.0 <= ratio <= 1.0:
        raise ValueError("OTEL_TRACES_SAMPLER_ARG must be a number between 0 and 1")
    return norm, ratio


def _normalize_deployment_env(value: str, tier: str | EnvironmentTier) -> str:
    resolved = resolve_tier(tier)
    cleaned = value.strip().lower()
    if not cleaned:
        return resolved.value
    if resolved is EnvironmentTier.PREVIEW:
        return cleaned
    if resolve_tier(cleaned) is not resolved:
        raise ValueError("deployment_environment disagrees with environment tier")
    return resolved.value


class OtelSettings(ContractModel):
    """Explicit OpenTelemetry exporter, resource identity, and sampling configuration."""

    service_name: str
    endpoint: str | None = None
    service_version: str = "unknown"
    environment: str = EnvironmentTier.LOCAL_DEV.value
    deployment_environment: str = ""
    instance_id: str = ""
    resource_attributes: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    export_interval_millis: int = 60_000
    traces_sampler: str = ""
    traces_sampler_arg: str = ""
    sdk_disabled: bool = False
    suppress_probe_telemetry: bool = False

    @field_validator("export_interval_millis")
    @classmethod
    def _positive_interval(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(_INTERVAL_MESSAGE)
        return v

    @model_validator(mode="after")
    def _normalize_settings(self) -> OtelSettings:
        if not self.service_name.strip():
            raise ValueError("service_name is required")
        tier = resolve_tier(self.environment)
        object.__setattr__(self, "environment", tier.value)
        dep_env = _normalize_deployment_env(self.deployment_environment, tier)
        object.__setattr__(self, "deployment_environment", dep_env)
        if self.enabled and not self.endpoint:
            raise ValueError("enabled telemetry requires an OTLP HTTP endpoint")
        if self.endpoint:
            validate_http_url(
                self.endpoint, label="OTLP endpoint", allow_fragment=False, validate_port=True
            )
        sampler, _ = _parse_traces_sampler(self.traces_sampler, self.traces_sampler_arg)
        object.__setattr__(self, "traces_sampler", sampler if self.traces_sampler.strip() else "")
        object.__setattr__(self, "traces_sampler_arg", self.traces_sampler_arg.strip())
        if any(not k or not v for k, v in self.resource_attributes.items()):
            raise ValueError("resource attributes must have non-empty string keys and values")
        return self

    def resolved_resource_attributes(self) -> dict[str, str]:
        attrs = {
            **self.resource_attributes,
            "service.name": self.service_name,
            "service.version": self.service_version,
            "deployment.environment.name": self.deployment_environment,
        }
        if self.instance_id:
            attrs["service.instance.id"] = self.instance_id
        return attrs

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        default_service_name: str | None = None,
        strict: bool = True,
        require_issued_identity: bool = False,
        suppress_probe_telemetry: bool = False,
    ) -> OtelSettings:
        values = os.environ if environ is None else environ
        if require_issued_identity:
            rt = RuntimeEnvironment(
                name=EnvironmentTier.LOCAL_DEV.value, tier=EnvironmentTier.LOCAL_DEV
            )
            env_is_set = False
        else:
            rt = resolve_environment(values, required=strict)
            env_is_set = bool(read_runtime_env(RuntimeEnvKey.ENVIRONMENT, values))

        raw_attrs = read_runtime_env(RuntimeEnvKey.OTEL_RESOURCE_ATTRIBUTES, values)
        try:
            attrs = _parse_resource_attributes(raw_attrs)
        except ValueError as exc:
            if strict:
                raise
            warn(f"invalid OTEL_RESOURCE_ATTRIBUTES: {exc}", RuntimeWarning, stacklevel=2)
            attrs = {}
        for k in ("service.name", "service.version", "service.instance.id"):
            if k in attrs:
                if attrs[k].strip():
                    attrs[k] = attrs[k].strip()
                else:
                    del attrs[k]
        dep_env = attrs.get("deployment.environment.name", "").strip() or None

        service_name = (
            read_runtime_env(RuntimeEnvKey.SERVICE_NAME, values)
            or attrs.get("service.name")
            or default_service_name
            or "unknown_service"
        )
        service_version = (
            read_runtime_env(RuntimeEnvKey.SERVICE_VERSION, values)
            or attrs.get("service.version")
            or "unknown"
        )
        instance_id = attrs.get("service.instance.id", "")

        endpoint = read_runtime_env(RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT, values) or None
        try:
            disabled = _parse_otel_boolean(values.get("OTEL_SDK_DISABLED"))
        except ValueError as exc:
            if strict:
                raise ValueError(f"invalid OTEL_SDK_DISABLED: {exc}") from exc
            warn(f"invalid OTEL_SDK_DISABLED: {exc}", RuntimeWarning, stacklevel=2)
            disabled = False
        eff_ep = None if disabled else endpoint

        if require_issued_identity and eff_ep:
            missing = [
                *(
                    [RuntimeEnvKey.SERVICE_NAME.value]
                    if not read_runtime_env(RuntimeEnvKey.SERVICE_NAME, values)
                    else []
                ),
                *(a for a in REQUIRED_IDENTITY_ATTRIBUTES if not attrs.get(a, "").strip()),
            ]
            if missing:
                raise ValueError(
                    "telemetry requires issued identity attributes when OTLP endpoint is set; "
                    f"missing: {', '.join(missing)}"
                )

        if dep_env and (not env_is_set or require_issued_identity):
            try:
                inferred = resolve_tier(dep_env)
            except ValueError as exc:
                raise ValueError(
                    f"invalid deployment.environment.name {dep_env!r}: {exc}"
                ) from None
            dep_env = _normalize_deployment_env(dep_env, inferred)
            rt = RuntimeEnvironment(name=dep_env, tier=inferred)
        elif dep_env:
            dep_env = _normalize_deployment_env(dep_env, rt.tier)

        s_name = str(values.get("OTEL_TRACES_SAMPLER") or "").strip()
        s_arg = str(values.get("OTEL_TRACES_SAMPLER_ARG") or "").strip()
        try:
            _parse_traces_sampler(s_name, s_arg)
        except ValueError as exc:
            if strict:
                raise
            warn(f"invalid OTEL_TRACES_SAMPLER: {exc}", RuntimeWarning, stacklevel=2)
            s_name = s_arg = ""

        raw_interval = str(values.get("OTEL_METRIC_EXPORT_INTERVAL") or "").strip()
        interval = 60_000
        if raw_interval:
            try:
                interval = int(raw_interval)
            except ValueError:
                if strict:
                    raise ValueError("invalid OTEL_METRIC_EXPORT_INTERVAL: expected int") from None
                warn(
                    "invalid OTEL_METRIC_EXPORT_INTERVAL: expected int",
                    RuntimeWarning,
                    stacklevel=2,
                )
            else:
                if interval <= 0 and not strict:
                    warn(
                        "invalid OTEL_METRIC_EXPORT_INTERVAL: must be positive",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                    interval = 60_000

        return cls(
            service_name=service_name,
            endpoint=eff_ep,
            service_version=service_version,
            environment=rt.tier.value,
            deployment_environment=dep_env or rt.name,
            instance_id=instance_id,
            resource_attributes=attrs,
            enabled=bool(eff_ep),
            export_interval_millis=interval,
            traces_sampler=s_name,
            traces_sampler_arg=s_arg,
            sdk_disabled=disabled,
            suppress_probe_telemetry=suppress_probe_telemetry,
        )


@dataclass(slots=True)
class TelemetryProviders:
    tracer_provider: Any | None = None
    meter_provider: Any | None = None
    logger_provider: Any | None = None
    logging_instrumentor: Any | None = None
    logging_handler: logging.Handler | None = None
    _closed: bool = False

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        _release_global(self)
        if self.logging_handler is not None:
            logging.getLogger().removeHandler(self.logging_handler)
            self.logging_handler = None
        if self.logging_instrumentor is not None:
            self.logging_instrumentor.uninstrument()
        for p in (self.logger_provider, self.meter_provider, self.tracer_provider):
            if p is not None:
                p.shutdown()


_GLOBAL_LOCK = threading.Lock()
_ACTIVE_GLOBAL: tuple[TelemetryProviders, OtelSettings] | None = None


def _release_global(providers: TelemetryProviders) -> None:
    global _ACTIVE_GLOBAL
    with _GLOBAL_LOCK:
        if _ACTIVE_GLOBAL is not None and _ACTIVE_GLOBAL[0] is providers:
            _ACTIVE_GLOBAL = None


class _RedactingLogRecordProcessor:
    def on_emit(self, record: Any) -> None:
        rec, env = getattr(record, "log_record", record), os.environ
        if rec.body is not None:
            rec.body = _scrub(rec.body, env)
        sl = _structlog_cvars()
        sl_ctx = dict(sl.get_contextvars() or {}) if sl else {}
        log_ctx = dict(_LOG_CONTEXT.get() or {}) | {
            k: v for k, v in sl_ctx.items() if _OTLP_KEY_RE.match(k)
        }
        if rec.attributes or log_ctx:
            merged = dict(rec.attributes or {}) | log_ctx
            rec.attributes = {k: _scrub(v, env, str(k)) for k, v in merged.items()}

    def enabled(self, *_: Any, **__: Any) -> bool:
        return True

    def shutdown(self) -> None:
        pass

    def force_flush(self, _t: int = 30_000) -> bool:
        return True


def _ot(module: str) -> Any:
    return require_optional(f"opentelemetry.{module}", extra="telemetry")


def signal_endpoint(base_endpoint: str | None, signal: SignalKind) -> str:
    """Derive the OTLP/HTTP per-signal URL while preserving query parameters."""
    if base_endpoint is None:
        raise ValueError("OTLP endpoint is required")
    ep = urlsplit(base_endpoint)
    cleaned = ep.path.rstrip("/")
    for known in ("traces", "metrics", "logs"):
        if cleaned.endswith(suffix := f"/v1/{known}"):
            cleaned = cleaned[: -len(suffix)]
            break
    return urlunsplit((ep.scheme, ep.netloc, f"{cleaned}/v1/{signal}", ep.query, ""))


def _is_probe_span(name: str, attributes: Mapping[str, Any] | None) -> bool:
    if any(name == p or name.endswith(f" {p}") or name.startswith(f"{p} ") for p in PROBE_PATHS):
        return True
    return bool(
        attributes
        and any(
            isinstance(v := attributes.get(k), str) and v.split("?", 1)[0] in PROBE_PATHS
            for k in ("url.path", "http.target", "http.route")
        )
    )


def _build_sampler(sampling: Any, settings: OtelSettings) -> Any:
    name, ratio = _parse_traces_sampler(settings.traces_sampler, settings.traces_sampler_arg)
    root = {"always_on": sampling.ALWAYS_ON, "always_off": sampling.ALWAYS_OFF}.get(
        name.removeprefix("parentbased_")
    ) or sampling.TraceIdRatioBased(ratio)
    delegate = sampling.ParentBased(root) if name.startswith("parentbased_") else root
    if not settings.suppress_probe_telemetry:
        return delegate

    class _DropProbeSampler(sampling.Sampler):  # type: ignore[misc]
        def should_sample(
            self,
            parent_context: Any,
            trace_id: int,
            span_name: str,
            kind: Any = None,
            attributes: Any = None,
            links: Any = None,
            trace_state: Any = None,
        ) -> Any:
            if _is_probe_span(span_name, attributes):
                return sampling.SamplingResult(sampling.Decision.DROP, {}, trace_state)
            return delegate.should_sample(
                parent_context, trace_id, span_name, kind, attributes, links, trace_state
            )

        def get_description(self) -> str:
            return f"DropProbeSampler({delegate.get_description()})"

    return _DropProbeSampler()


def _build_providers(
    settings: OtelSettings, *, set_global: bool, capture_logs: bool
) -> tuple[TelemetryProviders, list[str]]:
    trace_api, metrics_api, logs_api = _ot("trace"), _ot("metrics"), _ot("_logs")
    res = _ot("sdk.resources").Resource.create(settings.resolved_resource_attributes())
    tp = _ot("sdk.trace").TracerProvider(
        resource=res, sampler=_build_sampler(_ot("sdk.trace.sampling"), settings)
    )
    tp.add_span_processor(
        _ot("sdk.trace.export").BatchSpanProcessor(
            _ot("exporter.otlp.proto.http.trace_exporter").OTLPSpanExporter(
                endpoint=signal_endpoint(settings.endpoint, "traces")
            )
        )
    )
    mp = _ot("sdk.metrics").MeterProvider(
        resource=res,
        metric_readers=[
            _ot("sdk.metrics.export").PeriodicExportingMetricReader(
                _ot("exporter.otlp.proto.http.metric_exporter").OTLPMetricExporter(
                    endpoint=signal_endpoint(settings.endpoint, "metrics")
                ),
                export_interval_millis=settings.export_interval_millis,
            )
        ],
    )
    started = time.monotonic()
    mp.get_meter("infra2_sdk").create_observable_gauge(
        "process.uptime",
        callbacks=[lambda _: (metrics_api.Observation(time.monotonic() - started),)],
        unit="s",
    )
    lp = _ot("sdk._logs").LoggerProvider(resource=res)
    lp.add_log_record_processor(_RedactingLogRecordProcessor())
    lp.add_log_record_processor(
        _ot("sdk._logs.export").BatchLogRecordProcessor(
            _ot("exporter.otlp.proto.http._log_exporter").OTLPLogExporter(
                endpoint=signal_endpoint(settings.endpoint, "logs")
            )
        )
    )
    providers = TelemetryProviders(tp, mp, lp)
    if not set_global:
        return providers, []
    trace_api.set_tracer_provider(tp)
    metrics_api.set_meter_provider(mp)
    logs_api.set_logger_provider(lp)
    not_installed = [
        sig
        for sig, ok in (
            ("tracer", trace_api.get_tracer_provider() is tp),
            ("meter", metrics_api.get_meter_provider() is mp),
            ("logger", logs_api.get_logger_provider() is lp),
        )
        if not ok
    ]
    inst = _ot("instrumentation.logging").LoggingInstrumentor()
    inst.instrument(
        tracer_provider=tp, inject_trace_context=True, enable_log_auto_instrumentation=False
    )
    providers.logging_instrumentor = inst
    if capture_logs:
        handler_cls = _ot("instrumentation.logging.handler").LoggingHandler
        handler = handler_cls(level=logging.NOTSET, logger_provider=lp)
        logging.getLogger().addHandler(handler)
        providers.logging_handler = handler
    return providers, not_installed


def configure_telemetry(
    settings: OtelSettings,
    *,
    set_global: bool = False,
    capture_logs: bool = True,
) -> TelemetryProviders:
    """Create (and optionally install) OpenTelemetry trace, metric, and log providers."""
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


def init_worker(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
    strict: bool = True,
    require_issued_identity: bool = False,
    log_level: int | None = logging.INFO,
    set_global: bool = True,
) -> TelemetryProviders | None:
    """Configure JSON stdout logging and OTLP telemetry for a non-HTTP worker."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must not be blank")
    values = os.environ if environ is None else environ
    settings: OtelSettings | None = None
    try:
        settings = OtelSettings.from_env(
            values,
            strict=strict,
            default_service_name=name,
            require_issued_identity=require_issued_identity,
        )
    except ValueError as exc:
        if strict:
            raise
        warn(str(exc), RuntimeWarning, stacklevel=2)

    sha = read_runtime_env(RuntimeEnvKey.GIT_COMMIT_SHA, values) or "unknown"
    ver = (
        settings.service_version
        if settings
        else read_runtime_env(RuntimeEnvKey.SERVICE_VERSION, values)
    )
    rver = ver if ver and ver != "unknown" else sha
    configure_logging(
        level=log_level,
        service_name=settings.service_name if settings else name,
        environment=(
            settings.deployment_environment if settings else EnvironmentTier.LOCAL_DEV.value
        ),
        service_version=rver,
        suppress_probes=False,
    )
    if not settings or not settings.enabled:
        return None
    providers = configure_telemetry(settings, set_global=set_global)
    if set_global:
        atexit.register(providers.shutdown)
    return providers


__all__ = (
    "SignalKind",
    "PROBE_PATHS",
    "REQUIRED_IDENTITY_ATTRIBUTES",
    "OtelSettings",
    "TelemetryProviders",
    "JsonLogFormatter",
    "current_request_id",
    "bind_request_id",
    "bind_log_context",
    "configure_logging",
    "signal_endpoint",
    "configure_telemetry",
    "init_worker",
)
