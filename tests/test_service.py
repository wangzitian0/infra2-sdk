"""Tests for infra2_sdk.service."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from infra2_sdk import config, service


def test_dependency_manifest_and_probes_and_health() -> None:
    manifest = service.DependencyManifest(
        dependencies=(
            service.Dependency(
                name="db",
                kind=service.DependencyKind.CODE_DOMINANT,
                required_in=frozenset({config.EnvironmentTier.STAGING}),
                env_vars=frozenset({"DATABASE_URL"}),
            ),
            service.Dependency(
                name="cache",
                kind=service.DependencyKind.CODE_DOMINANT,
                required_in=frozenset({config.EnvironmentTier.PRODUCTION}),
                env_vars=frozenset({"REDIS_URL"}),
            ),
        )
    )
    assert manifest.required_for(config.EnvironmentTier.STAGING) == frozenset({"db"})
    assert manifest.names() == frozenset({"db", "cache"})
    assert len(manifest) == 2 and list(manifest)[0].name == "db"
    assert "db" in manifest and manifest["db"].name == "db"
    with pytest.raises(KeyError):
        _ = manifest["missing"]
    with pytest.raises(ValueError, match="duplicate"):
        service.DependencyManifest(dependencies=(manifest["db"], manifest["db"]))

    async def _scenario() -> None:
        async def _ok_async() -> tuple[bool, str]:
            return True, "async-ok"

        checks = [
            service.CallableCheck("db", lambda: True),
            service.CallableCheck("cache", _ok_async),
            service.CallableCheck("flaky", lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
        ]
        code, body = await service.evaluate_health(
            checks, manifest=manifest, tier="staging", timeout_seconds=2.0
        )
        assert code == 200
        assert body["status"] == service.HealthStatus.DEGRADED.value
        assert body["checks"]["db"]["status"] == service.DependencyStatus.PRESENT.value
        assert body["checks"]["flaky"]["status"] == service.DependencyStatus.ABSENT.value

        code2, body2 = await service.evaluate_health(
            [service.CallableCheck("cache", _ok_async)],
            manifest=manifest,
            tier="staging",
            timeout_seconds=2.0,
        )
        assert code2 == 503
        assert body2["status"] == service.HealthStatus.UNHEALTHY.value

        async def _hang() -> tuple[bool, str]:
            await asyncio.sleep(1.0)
            return True, "never"

        timed = await service.run_probes(
            [service.CallableCheck("slow", _hang)], timeout_seconds=0.05
        )
        assert timed[0].status is service.DependencyStatus.ABSENT
        assert "timed out" in timed[0].detail

    asyncio.run(_scenario())


def test_init_service_routes_and_manifest_and_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = service.DependencyManifest(
        dependencies=(
            service.Dependency(
                name="db",
                kind=service.DependencyKind.CODE_DOMINANT,
                required_in=frozenset(config.EnvironmentTier),
                env_vars=frozenset({"DATABASE_URL"}),
            ),
        )
    )
    app = FastAPI()
    service.init_service(
        app,
        "orders-api",
        checks=[service.CallableCheck("db", lambda: (True, "ok"))],
        manifest=manifest,
        enable_otel=True,
        strict_otel=False,
        health_extra=lambda: {"region": "us"},
    )
    client = TestClient(app)
    live = client.get("/livez", headers={"X-Request-ID": "req-abc"})
    assert live.status_code == 200
    assert live.headers["X-Request-ID"] == "req-abc"
    assert live.json() == {"status": "alive", "service": "orders-api"}

    ready = client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    health = client.get("/health")
    assert health.status_code == 200 and health.json()["region"] == "us"

    with pytest.raises(RuntimeError, match="already called"):
        service.init_service(app, "dup")

    # Live OTLP endpoint initialization + lifespan shutdown inside init_service
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    class _Ok(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return None

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Ok)
    Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("ENVIRONMENT", "staging")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{srv.server_port}")
        monkeypatch.setenv("OTEL_SERVICE_NAME", "orders-otel")
        monkeypatch.setenv(
            "OTEL_RESOURCE_ATTRIBUTES",
            "deployment.environment.name=staging,infra.service.id=platform/orders,"
            "service.version=1.0.0,infra.iac.ref=" + "a" * 40,
        )
        otel_app = FastAPI()
        service.init_service(
            otel_app,
            "orders-otel",
            enable_otel=True,
            strict_otel=True,
            require_issued_identity=True,
        )
        with TestClient(otel_app) as c:
            assert c.get("/livez").status_code == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_http_clients_retry_probe_and_rate_limit() -> None:
    with pytest.raises(ValueError, match="max_keepalive_connections"):
        service.HttpClientSettings(max_connections=2, max_keepalive_connections=5)

    cfg = service.HttpClientSettings(timeout_seconds=3.0)
    with service.create_http_client(cfg) as sync_c:
        assert "infra2-sdk/" in sync_c.headers["User-Agent"]

    future_hdr = format_datetime(datetime.now(UTC) + timedelta(seconds=1))
    calls = 0

    class _FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError("transient")
            if calls == 2:
                return SimpleNamespace(status_code=503, headers={"Retry-After": future_hdr})
            return SimpleNamespace(status_code=200, headers={})

    sleeps: list[float] = []
    resp = service.request_with_retry(
        _FakeClient(), "GET", "https://example.com", max_attempts=3, sleep=sleeps.append
    )
    assert resp.status_code == 200 and len(sleeps) == 2

    with pytest.raises(httpx.ConnectError):
        service.request_with_retry(
            SimpleNamespace(
                request=lambda *_a, **_k: (_ for _ in ()).throw(httpx.ConnectError("down"))
            ),
            "GET",
            "https://example.com",
            max_attempts=1,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/err":
            raise httpx.ConnectError("refused")
        return httpx.Response(204 if request.url.path == "/ok" else 503)

    async def _probe_run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as async_c:
            ok = await service.probe_http("http://svc.local/ok", client=async_c)
            bad = await service.probe_http("http://svc.local/down", client=async_c)
            err = await service.probe_http("http://svc.local/err", client=async_c)
            assert ok.status is service.DependencyStatus.PRESENT
            assert bad.status is service.DependencyStatus.ABSENT
            assert err.status is service.DependencyStatus.ABSENT

    asyncio.run(_probe_run())

    now = [0.0]
    win = service.RateLimitWindow(max_requests=2, window_seconds=10.0, clock=lambda: now[0])
    assert win.acquire() == (True, 0.0)
    now[0] = 2.0
    assert win.acquire() == (True, 0.0)
    allowed, retry = win.acquire()
    assert allowed is False and retry == pytest.approx(8.0)
    now[0] = 10.5
    assert win.used() == 1
    assert win.acquire() == (True, 0.0)
    with pytest.raises(ValueError):
        win.acquire(cost=3)
