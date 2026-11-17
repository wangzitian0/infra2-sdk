"""Explicit OpenTelemetry bootstrap using OTLP."""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from warnings import warn

from infra2_sdk._wire import redact_text, validate_http_url
from infra2_sdk.runtime._optional import require
from infra2_sdk.runtime._otel_env import parse_otel_boolean, parse_traces_sampler
from infra2_sdk.runtime.environment import (
    EnvironmentTier,
    RuntimeEnvironment,
    RuntimeEnvKey,
    _normalize_deployment_environment,
    _resolve_env,
    _resolve_service_identity,
    env_int,
    is_preview_alias,
    resolve_environment_tier,
    resolve_runtime_env,
)

_INTERVAL_MESSAGE = "export_interval_millis must be positive"
PROBE_PATHS: frozenset[str] = frozenset({"/livez", "/readyz", "/health"})
REQUIRED_IDENTITY_ATTRIBUTES: tuple[str, ...] = (
    "deployment.environment.name",
    "infra.service.id",
    "service.version",
    "infra.iac.ref",
)
_REQUEST_ID: ContextVar[str | None] = ContextVar("rid", default=None)
_SECRET_ENV = re.compile(
    r"(?:^|_)(?:SECRET|TOKEN|PASSWORD|PASSWD|KEY|AUTH|CREDENTIAL|WEBHOOK|DSN)(?:_URL)?$"
    r"|^(?:DATABASE|REDIS)_URL$|^INFRA2_OUT_OF_BAND_",
    re.I,
)
_EMAIL_RE = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_SECRET_VAL = re.compile(
    r"Bearer\s+[\w.-]+|eyJ[\w-]{8,}\.[\w-]+\.[\w-]*|(?:gh[ps]_|sk-|op_)[\w-]{8,}|(?<=:)[^:@/\s]+(?=@)"
)
_RISKY_KEY = re.compile(
    r"^(?:authorization|cookies?|api_key|token|secret|password"
    r"|raw_prompt|raw_response|response_body|error_body)$",
    re.I,
)
_STD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "otelTraceID",
    "otelSpanID",
    "otelTraceSampled",
    "otelServiceName",
}


def current_request_id() -> str | None:
    return _REQUEST_ID.get()


def _sl() -> Any:
    return getattr(sys.modules.get("structlog"), "contextvars", None)


def _ctx_rid(val: Any, tok: Any = None) -> Any:
    if c := _sl():
        c.bind_contextvars(request_id=val) if tok is None else c.unbind_contextvars("request_id")
    return _REQUEST_ID.set(val) if tok is None else _REQUEST_ID.reset(tok)


def _scrub(val: Any, env: Mapping[str, str], key: str | None = None) -> Any:
    if key and _RISKY_KEY.match(key):
        return "[REDACTED]"
    if isinstance(val, str):
        s = redact_text(_SECRET_VAL.sub("[REDACTED]", val), env, _SECRET_ENV)
        return _EMAIL_RE.sub("[EMAIL]", s)
    if isinstance(val, Mapping):
        return {k: _scrub(v, env, str(k)) for k, v in val.items()}
    return type(val)(_scrub(x, env) for x in val) if isinstance(val, (list, tuple)) else val


class _JsonLogFormatter(logging.Formatter):
    def __init__(self, ident: dict[str, str]) -> None:
        super().__init__()
        self._id = ident

    def format(self, r: logging.LogRecord) -> str:
        env, sl = os.environ, _sl()
        ctx = dict(sl.get_contextvars() or {}) if sl else {}
        tid, sid = r.__dict__.get("otelTraceID"), r.__dict__.get("otelSpanID")
        if tid and set(str(tid)) != {"0"}:
            tid, sid = str(tid), str(sid) if sid else None
        elif (ot := sys.modules.get("opentelemetry.trace")) and (
            c := ot.get_current_span().get_span_context()
        ).is_valid:
            tid, sid = format(c.trace_id, "032x"), format(c.span_id, "016x")
        else:
            tid = sid = None
        out = {
            "timestamp": datetime.fromtimestamp(r.created, UTC).isoformat(),
            "level": r.levelname,
            "logger": r.name,
            "message": _scrub(r.getMessage(), env),
            "trace_id": tid,
            "span_id": sid,
            "request_id": current_request_id() or (str(ctx.get("request_id") or "") or None),
            **self._id,
        }
        out.update(
            {
                k: _scrub(v, env, str(k))
                for k, v in {**r.__dict__, **ctx}.items()
                if k not in _STD_ATTRS and k not in out
            }
        )
        if r.exc_info:
            out["exception"] = _scrub(self.formatException(r.exc_info), env)
        return json.dumps(out, separators=(",", ":"), default=str)


def _chk_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must not be blank")


def _setup_logging(
    name: str,
    s: OtelSettings | None,
    *,
    tier: str = "local_dev",
    level: int | None,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    sha = resolve_runtime_env(env, RuntimeEnvKey.GIT_COMMIT_SHA).value or "unknown"
    ver = s.service_version if s else None
    if not s:
        with suppress(ValueError):
            ver = resolve_runtime_env(env, RuntimeEnvKey.SERVICE_VERSION).value
    rver = ver if ver and ver != "unknown" else sha
    if level is not None and not (root := logging.getLogger()).handlers:
        h = logging.StreamHandler(sys.stdout)
        attrs = {
            "service.name": s.service_name if s else name,
            "deployment.environment.name": s.deployment_environment if s else tier,
            "service.version": rver,
        }
        h.setFormatter(_JsonLogFormatter(attrs))
        root.addHandler(h)
        root.setLevel(level)
    return {"version": rver, "git_sha": sha}


class _RedactingLogRecordProcessor:
    def on_emit(self, log_record: Any) -> None:
        rec, env = getattr(log_record, "log_record", log_record), os.environ
        if rec.body is not None:
            rec.body = _scrub(rec.body, env)
        if rec.attributes:
            rec.attributes = {k: _scrub(v, env, str(k)) for k, v in rec.attributes.items()}

    def enabled(self, *_: Any, **__: Any) -> bool:
        return True

    def shutdown(self) -> None:
        pass

    def force_flush(self, _t: int = 30_000) -> bool:
        return True


def _ot(module: str) -> Any:
    return require(f"opentelemetry.{module}", extra="otel")


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
    sdk_disabled: bool = field(default=False, kw_only=True)
    suppress_probe_telemetry: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        tier = resolve_environment_tier(self.environment)
        object.__setattr__(self, "environment", tier.value)
        object.__setattr__(
            self,
            "deployment_environment",
            _normalize_deployment_environment(self.deployment_environment, tier),
        )
        if not self.service_name:
            raise ValueError("service_name is required")
        if self.enabled and not self.endpoint:
            raise ValueError("enabled telemetry requires an OTLP HTTP endpoint")
        if self.endpoint:
            validate_http_url(
                self.endpoint, label="OTLP endpoint", allow_fragment=False, validate_port=True
            )
        if self.export_interval_millis <= 0:
            raise ValueError(_INTERVAL_MESSAGE)
        sampler, _ = parse_traces_sampler(self.traces_sampler, self.traces_sampler_arg)
        object.__setattr__(self, "traces_sampler", sampler if self.traces_sampler.strip() else "")
        object.__setattr__(self, "traces_sampler_arg", self.traces_sampler_arg.strip())
        if any(
            not isinstance(k, str) or not k or not isinstance(v, str)
            for k, v in self.resource_attributes.items()
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
        suppress_probe_telemetry: bool = False,
    ) -> OtelSettings:
        identity = _resolve_service_identity(
            environ,
            strict=strict,
            default_service_name=default_service_name,
            ignore_environment=require_issued_identity,
        )
        endpoint = resolve_runtime_env(environ, RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT).value
        try:
            disabled = parse_otel_boolean(_resolve_env(environ, "OTEL_SDK_DISABLED").value)
        except ValueError as exc:
            if strict:
                raise ValueError(f"invalid OTEL_SDK_DISABLED: {exc}") from exc
            warn(f"invalid OTEL_SDK_DISABLED: {exc}", RuntimeWarning, stacklevel=2)
            disabled = False
        eff_ep = None if disabled else endpoint
        if require_issued_identity and eff_ep:
            missing_id = [
                *(
                    [RuntimeEnvKey.SERVICE_NAME.value]
                    if resolve_runtime_env(environ, RuntimeEnvKey.SERVICE_NAME).value is None
                    else []
                ),
                *(
                    a
                    for a in REQUIRED_IDENTITY_ATTRIBUTES
                    if not identity.attributes.get(a, "").strip()
                ),
            ]
            if missing_id:
                raise ValueError(
                    "telemetry requires ServiceIdentity attributes when an OTLP endpoint is set; "
                    f"missing: {', '.join(missing_id)}"
                )
        rt, dep_env = identity.environment, identity.deployment_environment
        try:
            if dep_env and (not identity.environment_is_set or require_issued_identity):
                inf = resolve_environment_tier(dep_env)
                dep_env = _normalize_deployment_environment(dep_env, inf)
                rt = RuntimeEnvironment(dep_env, inf)
            elif dep_env and rt.tier is EnvironmentTier.STAGING and is_preview_alias(dep_env):
                dep_env = _normalize_deployment_environment(dep_env, EnvironmentTier.PREVIEW)
                rt = RuntimeEnvironment(dep_env, EnvironmentTier.PREVIEW)
            elif dep_env:
                dep_env = _normalize_deployment_environment(dep_env, rt.tier)
        except ValueError as exc:
            if strict:
                raise ValueError(f"invalid deployment.environment.name: {exc}") from exc
            raise
        s_name = _resolve_env(environ, "OTEL_TRACES_SAMPLER").value or ""
        s_arg = _resolve_env(environ, "OTEL_TRACES_SAMPLER_ARG").value or ""
        try:
            parse_traces_sampler(s_name, s_arg)
        except ValueError as exc:
            if strict:
                raise
            warn(f"invalid OTEL_TRACES_SAMPLER: {exc}", RuntimeWarning, stacklevel=2)
            s_name = s_arg = ""
        try:
            return cls(
                service_name=identity.service_name or "unknown_service",
                endpoint=eff_ep,
                service_version=identity.service_version,
                environment=rt.tier.value,
                deployment_environment=dep_env or rt.name,
                instance_id=identity.instance_id,
                resource_attributes=identity.attributes,
                enabled=bool(eff_ep),
                export_interval_millis=env_int(
                    environ, "OTEL_METRIC_EXPORT_INTERVAL", default=60_000
                ),
                traces_sampler=s_name,
                traces_sampler_arg=s_arg,
                sdk_disabled=disabled,
                suppress_probe_telemetry=suppress_probe_telemetry,
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
        if getattr(self, "_closed", False):
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


def resource_attributes(settings: OtelSettings) -> dict[str, str]:
    attrs = {
        **settings.resource_attributes,
        "service.name": settings.service_name,
        "service.version": settings.service_version,
        "deployment.environment.name": settings.deployment_environment,
    }
    if settings.instance_id:
        attrs["service.instance.id"] = settings.instance_id
    return attrs


def configure_telemetry(
    settings: OtelSettings, *, set_global: bool = False, capture_logs: bool = True
) -> TelemetryProviders:
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
    name, ratio = parse_traces_sampler(settings.traces_sampler, settings.traces_sampler_arg)
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


def _logging_handler_class() -> Any:
    try:
        return _ot("instrumentation.logging.handler").LoggingHandler
    except RuntimeError:
        return _ot("sdk._logs").LoggingHandler


def _build_providers(
    settings: OtelSettings, *, set_global: bool, capture_logs: bool
) -> tuple[TelemetryProviders, list[str]]:
    trace_api, metrics_api, logs_api = _ot("trace"), _ot("metrics"), _ot("_logs")
    res = _ot("sdk.resources").Resource.create(resource_attributes(settings))
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
        handler = _logging_handler_class()(level=logging.NOTSET, logger_provider=lp)
        logging.getLogger().addHandler(handler)
        providers.logging_handler = handler
    return providers, not_installed


def init_worker(
    name: str,
    *,
    strict: bool = True,
    require_issued_identity: bool = False,
    log_level: int | None = logging.INFO,
    set_global: bool = True,
    environ: Mapping[str, str] | None = None,
) -> TelemetryProviders | None:
    """Init JSON logs, redaction, and OTLP telemetry for a worker."""
    _chk_name(name)
    s: OtelSettings | None = None
    try:
        s = OtelSettings.from_env(
            environ,
            strict=strict,
            default_service_name=name,
            require_issued_identity=require_issued_identity,
        )
    except ValueError as exc:
        if strict:
            raise
        warn(str(exc), RuntimeWarning, stacklevel=2)
    _setup_logging(name, s, level=log_level, env=environ)
    if not s or not s.enabled:
        return None
    providers = configure_telemetry(s, set_global=set_global)
    if set_global:
        atexit.register(providers.shutdown)
    return providers


def signal_endpoint(base: str | None, signal: str) -> str:
    if base is None:
        raise ValueError("OTLP endpoint is required")
    ep = urlsplit(base)
    cleaned = ep.path.rstrip("/")
    for known in ("traces", "metrics", "logs"):
        if cleaned.endswith(suf := f"/v1/{known}"):
            cleaned = cleaned[: -len(suf)]
            break
    return urlunsplit((ep.scheme, ep.netloc, f"{cleaned}/v1/{signal}", ep.query, ""))


__all__ = [
    "OtelSettings",
    "PROBE_PATHS",
    "REQUIRED_IDENTITY_ATTRIBUTES",
    "TelemetryProviders",
    "configure_telemetry",
    "current_request_id",
    "init_worker",
    "resource_attributes",
    "signal_endpoint",
]
