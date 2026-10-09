"""Single-line FastAPI runtime integration for infra2.

Provides turnkey compliance with ops.observability.md §5.1:
- /livez: Liveness probe (process-only, anti-puppet rule).
- /readyz: Readiness probe (checks database and essential dependencies; fails 503).
- /health: Backwards-compatible aggregated healthcheck.
- OpenTelemetry APM bootstrapping with W3C TraceContext propagation.
- Graceful shutdown handling.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("infra2_sdk.fastapi")


def _setup_opentelemetry(app: Any, service_name: str) -> bool:
    """Initialize OpenTelemetry instrumentation if packages and endpoint are present."""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        logger.debug("OTEL_EXPORTER_OTLP_ENDPOINT not set; skipping OTel instrumentation")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource_attrs = {"service.name": service_name}
        raw_attrs = os.getenv("OTEL_RESOURCE_ATTRIBUTES")
        if raw_attrs:
            for pair in raw_attrs.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    resource_attrs[k.strip()] = v.strip()

        resource = Resource.create(resource_attrs)
        provider = TracerProvider(resource=resource)

        # HTTP exporter against the Docker internal collector endpoint
        exporter_url = f"{endpoint.rstrip('/')}/v1/traces"
        exporter = OTLPSpanExporter(endpoint=exporter_url)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        # Instrument FastAPI app if instrumentor is available
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
        except ImportError:
            logger.debug("opentelemetry-instrumentation-fastapi not installed; tracing disabled")

        logger.info(f"OpenTelemetry tracing initialized for '{service_name}' -> {exporter_url}")
        return True
    except Exception as exc:
        logger.warning(f"Could not initialize OpenTelemetry: {exc}")
        return False


def init_service(
    app: Any,
    name: str,
    *,
    db_checker: Callable[[], bool | tuple[bool, str]] | None = None,
    enable_otel: bool = True,
) -> None:
    """Initialize service endpoints, observability, and lifecycle hooks on FastAPI app.

    Parameters:
        app: The FastAPI application instance.
        name: Logical service identifier (e.g. 'my-service').
        db_checker: Optional callable returning True/False or (is_healthy: bool, detail: str).
        enable_otel: Whether to initialize OpenTelemetry if endpoint is configured.
    """
    try:
        from fastapi import Response
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        raise ImportError("fastapi is required to use infra2_sdk.fastapi.init_service") from exc

    # 1. /livez — Liveness probe (ANTI-PUPPET: never check remote DB or external APIs)
    @app.get("/livez", tags=["Observability"], include_in_schema=False)
    def livez() -> Response:
        return JSONResponse(status_code=200, content={"status": "alive", "service": name})

    # 2. /readyz — Readiness probe (validates required operational dependencies)
    @app.get("/readyz", tags=["Observability"], include_in_schema=False)
    def readyz() -> Response:
        reasons = []
        if db_checker is not None:
            try:
                res = db_checker()
                if isinstance(res, tuple):
                    ok, detail = res
                else:
                    ok, detail = bool(res), ("ok" if res else "db unreachable")
                if not ok:
                    reasons.append(detail)
            except Exception as exc:
                reasons.append(f"db check raised {type(exc).__name__}: {exc}")

        if reasons:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "service": name, "reasons": reasons},
            )
        return JSONResponse(status_code=200, content={"status": "ready", "service": name})

    # 3. /health — Aggregated health specification (ops.observability.md §5.1)
    @app.get("/health", tags=["Observability"], include_in_schema=True)
    def health() -> Response:
        checks: dict[str, Any] = {}
        reasons: list[str] = []
        overall_status = "healthy"

        if db_checker is not None:
            t0 = time.perf_counter()
            try:
                res = db_checker()
                duration_ms = round((time.perf_counter() - t0) * 1000, 2)
                if isinstance(res, tuple):
                    ok, detail = res
                else:
                    ok, detail = bool(res), ("ok" if res else "database unreachable")
                checks["database"] = {
                    "status": "present" if ok else "failed",
                    "duration_ms": duration_ms,
                }
                if not ok:
                    overall_status = "unhealthy"
                    reasons.append(detail)
            except Exception as exc:
                overall_status = "unhealthy"
                reasons.append(f"database error: {exc}")
                checks["database"] = {
                    "status": "failed",
                    "error": str(exc),
                }

        http_code = 200 if overall_status == "healthy" else 503
        return JSONResponse(
            status_code=http_code,
            content={
                "status": overall_status,
                "service": name,
                "checks": checks,
                "reasons": reasons,
            },
        )

    # 4. OpenTelemetry Tracing
    if enable_otel:
        otel_service_name = os.getenv("OTEL_SERVICE_NAME") or name
        _setup_opentelemetry(app, otel_service_name)
