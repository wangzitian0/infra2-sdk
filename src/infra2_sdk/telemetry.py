"""OpenTelemetry OTLP worker bootstrap, structured contextvars, and instrumentation helpers."""

from __future__ import annotations

import atexit
import contextlib
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from infra2_sdk._core import (
    bind_log_context as _bind_log_context,
)
from infra2_sdk._core import (
    bootstrap_telemetry as _bootstrap_telemetry,
)
from infra2_sdk._core import (
    clear_active_global as _clear_active_global,
)
from infra2_sdk._core import (
    configure_telemetry as _configure_telemetry,
)
from infra2_sdk._core import (
    current_request_id as _current_request_id,
)


class _NoopSpan:
    def __enter__(self) -> _NoopSpan:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    def set_attribute(self, key: str, value: Any) -> None:
        return None


class _NoopTracer:
    def start_as_current_span(self, name: str, **_kwargs: Any) -> _NoopSpan:
        return _NoopSpan()


class _NoopMetric:
    def record(self, value: float, attributes: Mapping[str, Any] | None = None) -> None:
        return None

    def add(self, amount: int | float, attributes: Mapping[str, Any] | None = None) -> None:
        return None


class _NoopMeter:
    def create_histogram(self, *_args: Any, **_kwargs: Any) -> _NoopMetric:
        return _NoopMetric()

    def create_counter(self, *_args: Any, **_kwargs: Any) -> _NoopMetric:
        return _NoopMetric()


def get_tracer(name: str, tracer_provider: Any | None = None) -> Any:
    """Return an OpenTelemetry tracer when installed, else a lightweight no-op tracer."""
    try:
        from opentelemetry import trace as otel_trace
    except ImportError:
        return _NoopTracer()
    provider = tracer_provider or otel_trace.get_tracer_provider()
    return provider.get_tracer(name)


def get_meter(name: str, meter_provider: Any | None = None) -> Any:
    """Return an OpenTelemetry meter when installed, else a lightweight no-op meter."""
    try:
        from opentelemetry import metrics as otel_metrics
    except ImportError:
        return _NoopMeter()
    provider = meter_provider or otel_metrics.get_meter_provider()
    return provider.get_meter(name)


def current_request_id() -> str | None:
    """Return the active request ID bound to the current execution context."""
    return _current_request_id()


@contextmanager
def bind_log_context(**fields: Any) -> Iterator[dict[str, Any]]:
    """Bind structured context fields onto JSON logs and OTLP log records."""
    with _bind_log_context(**fields) as bound:
        yield bound


@dataclass(slots=True)
class TelemetryProviders:
    """Active OpenTelemetry trace, metric, and log providers with lifecycle controls."""

    tracer_provider: Any | None = None
    meter_provider: Any | None = None
    logger_provider: Any | None = None
    logging_instrumentor: Any | None = None
    logging_handler: logging.Handler | None = None
    _closed: bool = False

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Flush buffered spans, metrics, and logs across all attached providers."""
        ok = True
        for provider in (self.tracer_provider, self.meter_provider, self.logger_provider):
            flush = getattr(provider, "force_flush", None)
            if callable(flush):
                try:
                    if flush(timeout_millis) is False:
                        ok = False
                except Exception:  # noqa: BLE001
                    ok = False
        return ok

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        _clear_active_global(self)
        if self.logging_handler is not None:
            with contextlib.suppress(Exception):
                logging.getLogger().removeHandler(self.logging_handler)
            self.logging_handler = None
        if self.logging_instrumentor is not None:
            with contextlib.suppress(Exception):
                self.logging_instrumentor.uninstrument()
        for provider in (self.logger_provider, self.meter_provider, self.tracer_provider):
            if provider is not None:
                with contextlib.suppress(Exception):
                    provider.shutdown()


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
    _parsed, active_settings, _tier, _release_info = _bootstrap_telemetry(
        name,
        environ=environ,
        enable_otel=True,
        strict_otel=strict,
        require_issued_identity=require_issued_identity,
        suppress_probes=False,
        log_level=log_level,
        set_global=set_global,
    )
    if active_settings is None:
        return None
    try:
        providers = _configure_telemetry(active_settings, set_global=set_global)
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise
        from warnings import warn

        warn(str(exc), RuntimeWarning, stacklevel=2)
        return None
    if set_global:
        atexit.register(providers.shutdown)
    return providers


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = (
    "TelemetryProviders",
    "bind_log_context",
    "current_request_id",
    "get_meter",
    "get_tracer",
    "init_worker",
)
