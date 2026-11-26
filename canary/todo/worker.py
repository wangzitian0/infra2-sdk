#!/usr/bin/env python3
"""Canary Todo background worker proving `init_worker`, `assert_env`, and `request_with_retry`."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from infra2_sdk.config import EnvironmentManifest, assert_env
from infra2_sdk.service import HttpClientSettings, create_http_client, request_with_retry
from infra2_sdk.telemetry import get_tracer, init_worker

try:
    from canary_settings import CanarySettings
except ImportError:
    _todo_dir = str(Path(__file__).resolve().parent)
    if _todo_dir not in sys.path:
        sys.path.insert(0, _todo_dir)
    from canary_settings import CanarySettings

LOGGER = logging.getLogger("platform-todo-worker")
DEFAULT_SERVICE_NAME = "platform-todo-worker"


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
    active_tracer = tracer if tracer is not None else get_tracer(DEFAULT_SERVICE_NAME)
    client_cm: Any = (
        contextlib.nullcontext(client)
        if client is not None
        else create_http_client(HttpClientSettings(timeout_seconds=timeout))
    )

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
        except Exception as exc:  # noqa: BLE001
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
    parser.add_argument("--health-url", default="", help="Target /readyz URL.")
    parser.add_argument("--validate-env", action="store_true")
    args = parser.parse_args(argv)

    if args.validate_env:
        assert_env(
            EnvironmentManifest.from_model(CanarySettings, source="canary/todo"),
            environ=os.environ,
            require_injected=True,
        )

    providers = init_worker(DEFAULT_SERVICE_NAME, strict=True, require_issued_identity=True)
    health_url = args.health_url.strip() or (
        f"http://127.0.0.1:{CanarySettings.from_environ(os.environ).port}/readyz"
    )
    try:
        result = run_once(health_url=health_url)
        print(json.dumps(result))
        return 0 if result["ok"] else 1
    finally:
        if providers is not None:
            providers.shutdown()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
