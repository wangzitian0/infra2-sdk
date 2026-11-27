"""OpenTelemetry OTLP worker bootstrap, structured contextvars, and instrumentation helpers."""

from __future__ import annotations

import atexit as _atexit
import contextlib as _contextlib
import logging as _logging
from collections.abc import Iterator as _Iterator
from collections.abc import Mapping as _Mapping
from contextlib import contextmanager as _contextmanager
from dataclasses import dataclass as _dataclass
from typing import Any as _Any

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

    def __exit__(self, exc_type: _Any, exc: _Any, tb: _Any) -> bool:
        return False

    def set_attribute(self, key: str, value: _Any) -> None:
        return None


class _NoopTracer:
    def start_as_current_span(self, name: str, **_kwargs: _Any) -> _NoopSpan:
        return _NoopSpan()


class _NoopMetric:
    def record(self, value: float, attributes: _Mapping[str, _Any] | None = None) -> None:
        return None

    def add(self, amount: int | float, attributes: _Mapping[str, _Any] | None = None) -> None:
        return None


class _NoopMeter:
    def create_histogram(self, *_args: _Any, **_kwargs: _Any) -> _NoopMetric:
        return _NoopMetric()

    def create_counter(self, *_args: _Any, **_kwargs: _Any) -> _NoopMetric:
        return _NoopMetric()

    def create_up_down_counter(self, *_args: _Any, **_kwargs: _Any) -> _NoopMetric:
        return _NoopMetric()

    def create_observable_gauge(self, *_args: _Any, **_kwargs: _Any) -> _NoopMetric:
        return _NoopMetric()


def get_tracer(name: str, tracer_provider: _Any | None = None) -> _Any:
    """Return an OpenTelemetry tracer when installed, else a lightweight no-op tracer."""
    try:
        from opentelemetry import trace as otel_trace
    except ImportError:
        return _NoopTracer()
    provider = tracer_provider or otel_trace.get_tracer_provider()
    return provider.get_tracer(name)


def get_meter(name: str, meter_provider: _Any | None = None) -> _Any:
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


@_contextmanager
def bind_log_context(**fields: _Any) -> _Iterator[dict[str, _Any]]:
    """Bind structured context fields onto JSON logs and OTLP log records."""
    with _bind_log_context(**fields) as bound:
        yield bound


@_dataclass(slots=True)
class TelemetryProviders:
    """Active OpenTelemetry trace, metric, and log providers with lifecycle controls."""

    tracer_provider: _Any | None = None
    meter_provider: _Any | None = None
    logger_provider: _Any | None = None
    logging_instrumentor: _Any | None = None
    logging_handler: _logging.Handler | None = None
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
            with _contextlib.suppress(Exception):
                _logging.getLogger().removeHandler(self.logging_handler)
            self.logging_handler = None
        if self.logging_instrumentor is not None:
            with _contextlib.suppress(Exception):
                self.logging_instrumentor.uninstrument()
        for provider in (self.logger_provider, self.meter_provider, self.tracer_provider):
            if provider is not None:
                with _contextlib.suppress(Exception):
                    provider.shutdown()


def init_worker(
    name: str,
    *,
    environ: _Mapping[str, str] | None = None,
    strict: bool = True,
    require_issued_identity: bool = False,
    log_level: int | None = _logging.INFO,
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
        _atexit.register(providers.shutdown)
    return providers


@_contextmanager
def worker_run(
    name: str,
    *,
    environ: _Mapping[str, str] | None = None,
    strict: bool = False,
    require_issued_identity: bool = False,
    log_level: int | None = _logging.INFO,
    set_global: bool = True,
    clear_root_handlers: bool = False,
    on_sigterm: _Any | None = None,
) -> _Iterator[TelemetryProviders | None]:
    """Run a background worker task with unique instance ID, SIGTERM handling, and forced flush."""
    import os
    import signal
    import threading
    import uuid

    from infra2_sdk.config import RuntimeEnvKey

    if clear_root_handlers:
        root = _logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)

    merged_env = dict(os.environ if environ is None else environ)
    raw_attrs = merged_env.get(RuntimeEnvKey.OTEL_RESOURCE_ATTRIBUTES.value, "").strip()
    if "service.instance.id=" not in raw_attrs:
        instance_attr = f"service.instance.id={uuid.uuid4()}"
        merged_env[RuntimeEnvKey.OTEL_RESOURCE_ATTRIBUTES.value] = (
            f"{raw_attrs},{instance_attr}" if raw_attrs else instance_attr
        )

    prev_sigterm: _Any = None
    installed_sigterm = False
    if threading.current_thread() is threading.main_thread():
        prev_sigterm = signal.getsignal(signal.SIGTERM)

        def _handle_sigterm(signum: int, _frame: _Any) -> None:
            if callable(on_sigterm):
                on_sigterm()
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, _handle_sigterm)
        installed_sigterm = True

    providers: TelemetryProviders | None = None
    try:
        providers = init_worker(
            name,
            environ=merged_env,
            strict=strict,
            require_issued_identity=require_issued_identity,
            log_level=log_level,
            set_global=set_global,
        )
        yield providers
    finally:
        if providers is not None:
            with _contextlib.suppress(Exception):
                providers.force_flush()
            providers.shutdown()
            _atexit.unregister(providers.shutdown)
        if installed_sigterm:
            signal.signal(signal.SIGTERM, prev_sigterm)


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = (
    "TelemetryProviders",
    "bind_log_context",
    "current_request_id",
    "get_meter",
    "get_tracer",
    "init_worker",
    "worker_run",
)
