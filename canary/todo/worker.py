#!/usr/bin/env python3
"""Canary Todo background worker proving `init_worker` and `request_with_retry`."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from infra2_sdk.runtime.config_schema import assert_environment
from infra2_sdk.runtime.http import request_with_retry
from infra2_sdk.runtime.otel import init_worker

try:
    from canary_settings import CanarySettings
except ImportError:
    _todo_dir = str(Path(__file__).resolve().parent)
    if _todo_dir not in sys.path:
        sys.path.insert(0, _todo_dir)
    from canary_settings import CanarySettings

LOGGER = logging.getLogger("platform-todo-worker")
ENV_MANIFEST_PATH = Path(__file__).resolve().parent / "env.manifest.json"
DEFAULT_SERVICE_NAME = "platform-todo-worker"


class _NoopSpan:
    def __enter__(self) -> _NoopSpan:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    def set_attribute(self, key: str, value: Any) -> None:
        return None


class _NoopTracer:
    def start_as_current_span(self, name: str) -> _NoopSpan:
        return _NoopSpan()


def _default_tracer() -> Any:
    try:
        from opentelemetry import trace as otel_trace
    except ImportError:
        return _NoopTracer()
    return otel_trace.get_tracer(DEFAULT_SERVICE_NAME)


def run_once(
    *,
    health_url: str,
    timeout: float = 5.0,
    max_attempts: int = 3,
    client: Any | None = None,
    tracer: Any | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Probe the web health endpoint once with `request_with_retry` inside an OTel span."""
    active_tracer = tracer if tracer is not None else _default_tracer()
    if client is not None:
        client_cm: Any = contextlib.nullcontext(client)
    else:
        import httpx

        client_cm = httpx.Client(timeout=timeout)

    with active_tracer.start_as_current_span("canary.worker.tick") as span:
        try:
            with client_cm as http_client:
                retry_kwargs: dict[str, Any] = {"max_attempts": max_attempts}
                if sleep is not None:
                    retry_kwargs["sleep"] = sleep
                response = request_with_retry(http_client, "GET", health_url, **retry_kwargs)
            status_code = int(response.status_code)
            payload = response.json() if hasattr(response, "json") else {}
            service = payload.get("service") if isinstance(payload, Mapping) else None
            ok = (
                status_code == 200
                and isinstance(payload, Mapping)
                and (
                    payload.get("ok") is True or payload.get("status") in {"ready", "healthy", "ok"}
                )
            )
            detail = (
                f"health ok (service={service or 'unknown'})"
                if ok
                else f"unexpected health response status={status_code}"
            )
        except Exception as exc:  # noqa: BLE001 - report probe failures in worker tick payload
            status_code = 0
            service = None
            ok = False
            detail = f"{type(exc).__name__}: {exc}"

        span.set_attribute("canary.worker.health_url", health_url)
        span.set_attribute("canary.worker.status_code", status_code)
        span.set_attribute("canary.worker.ok", ok)
        if ok:
            LOGGER.info("canary worker tick ok status=%d url=%s", status_code, health_url)
        else:
            LOGGER.error("canary worker tick failed detail=%s url=%s", detail, health_url)
        return {
            "ok": ok,
            "status_code": status_code,
            "service": service,
            "health_url": health_url,
            "detail": detail,
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Canary Todo background health worker")
    parser.add_argument("--once", action="store_true", help="Run one probe tick and exit.")
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Seconds between probe ticks in daemon mode.",
    )
    parser.add_argument(
        "--health-url",
        default="",
        help="Target /readyz URL. Defaults to http://127.0.0.1:<PORT>/readyz.",
    )
    parser.add_argument(
        "--service-name",
        default=DEFAULT_SERVICE_NAME,
        help="Telemetry service name passed to init_worker().",
    )
    parser.add_argument(
        "--no-require-issued-identity",
        action="store_true",
        help="Allow unissued OTEL_* attributes when OTLP export is enabled locally.",
    )
    parser.add_argument(
        "--validate-env",
        action="store_true",
        help="Validate environment against env.manifest.json before probing.",
    )
    args = parser.parse_args(argv)

    if args.validate_env:
        assert_environment(ENV_MANIFEST_PATH, environ=os.environ, require_injected=True)

    providers = init_worker(
        args.service_name,
        strict=True,
        require_issued_identity=not args.no_require_issued_identity,
    )
    health_url = args.health_url.strip() or (
        f"http://127.0.0.1:{CanarySettings.from_environ(os.environ).port}/readyz"
    )
    if args.once:
        result = run_once(health_url=health_url)
        print(json.dumps(result))
        if providers is not None:
            providers.shutdown()
        return 0 if result["ok"] else 1

    stop = False

    def _stop(signum: int, frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    import time

    try:
        while not stop:
            run_once(health_url=health_url)
            if not stop:
                time.sleep(max(1.0, args.interval))
    finally:
        if providers is not None:
            providers.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
