"""Tests for infra2_sdk.service."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.testclient import TestClient

from infra2_sdk import _core, config, service


def test_dependency_manifest_and_probes_and_health() -> None:
    for obsolete in (
        "evaluate_health",
        "run_probes",
        "HealthStatus",
        "ProbeResponse",
        "HealthResponse",
        "DependencyCheck",
        "CheckOutcome",
    ):
        assert not hasattr(service, obsolete)

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
    assert "db" in manifest and manifest["db"].name == "db"
    with pytest.raises(TypeError, match="collection"):
        service.Dependency(
            name="bad",
            kind=service.DependencyKind.CODE_DOMINANT,
            required_in="staging",  # type: ignore[arg-type]
            env_vars=frozenset({"X"}),
        )
    with pytest.raises(ValueError, match="duration_ms"):
        service.ProbeResult(
            name="x", status=service.DependencyStatus.PRESENT, duration_ms=float("nan")
        )

    async def _scenario() -> None:
        main_ident = threading.get_ident()
        async_ident: list[int] = []

        async def _ok_async() -> tuple[bool, str]:
            async_ident.append(threading.get_ident())
            return True, "async-ok"

        checks = [
            service.CallableCheck("db", lambda: True),
            service.CallableCheck("cache", _ok_async),
            service.CallableCheck("flaky", lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
        ]
        code, body = await service._evaluate_health(
            checks, manifest=manifest, tier="staging", timeout_seconds=2.0
        )
        assert code == 200
        assert async_ident == [main_ident]
        assert body["status"] == service._HealthStatus.DEGRADED.value

        code2, body2 = await service._evaluate_health(
            [service.CallableCheck("cache", _ok_async)],
            manifest=manifest,
            tier="staging",
            timeout_seconds=2.0,
        )
        assert code2 == 503 and body2["status"] == service._HealthStatus.UNHEALTHY.value

        gate = threading.Event()
        slow_finished = threading.Event()

        def _blocked_sync() -> tuple[bool, str]:
            try:
                gate.wait(timeout=2.0)
                return True, "late"
            finally:
                slow_finished.set()

        timed = await service._run_probes(
            [service.CallableCheck("slow", _blocked_sync)], timeout_seconds=0.05
        )
        assert timed[0].status is service.DependencyStatus.ABSENT
        assert "timed out" in timed[0].detail
        sem = service._sync_semaphore()
        assert sem._value == service._MAX_CONCURRENT_SYNC_PROBES - 1
        gate.set()
        assert slow_finished.wait(timeout=2.0)
        await asyncio.sleep(0.02)
        assert sem._value == service._MAX_CONCURRENT_SYNC_PROBES

    asyncio.run(_scenario())


def test_init_service_probe_exclusion_telemetry_and_health_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()

    @app.get("/items")
    def _items() -> dict[str, str]:
        return {"ok": "yes"}

    span_exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(span_exporter))
    metric_reader = InMemoryMetricReader()
    mp = MeterProvider(metric_readers=[metric_reader])

    service.init_service(
        app,
        "orders-api",
        checks=[service.CallableCheck("db", lambda: (True, "ok"))],
        enable_otel=False,
        suppress_probe_telemetry=True,
        health_extra=lambda: {"region": "us"},
    )
    app._telemetry["tracer_provider"] = tp
    app._telemetry["meter_provider"] = mp
    app._telemetry["logs"] = False

    with TestClient(app) as client:
        live = client.get("/livez", headers={"X-Request-ID": "req-abc"})
        assert live.status_code == 200 and live.headers["X-Request-ID"] == "req-abc"
        assert client.get("/readyz").status_code == 200
        assert client.get("/health").json()["region"] == "us"
        assert client.get("/items", headers={"X-Request-ID": "req-item-1"}).status_code == 200

    spans = span_exporter.get_finished_spans()
    span_attrs = [dict(s.attributes or {}) for s in spans]
    assert not any(
        a.get("http.route") in _core.PROBE_PATHS or a.get("url.path") in _core.PROBE_PATHS
        for a in span_attrs
    )
    assert any(a.get("request_id") == "req-item-1" for a in span_attrs)

    m_data = metric_reader.get_metrics_data()
    http_paths = [
        dp.attributes.get("http.route")
        for rm in m_data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "http.server.request.duration"
        for dp in m.data.data_points
    ]
    assert "/items" in http_paths
    assert not (set(http_paths) & _core.PROBE_PATHS)
    tp.shutdown()
    mp.shutdown()

    bad_app = FastAPI()
    service.init_service(
        bad_app,
        "bad-extra",
        checks=[service.CallableCheck("db", lambda: True)],
        enable_otel=False,
        health_extra=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    res_503 = TestClient(bad_app).get("/health")
    assert res_503.status_code == 503 and res_503.json()["health_extra_error"] == "RuntimeError"

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
        assert (
            otel_app._telemetry["tracer_provider"]
            is otel_app.state.telemetry_providers.tracer_provider
        )
        with TestClient(otel_app) as c:
            assert c.get("/livez").status_code == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_http_clients_retry_probe_and_rate_limit() -> None:
    cfg = service.HttpClientSettings(timeout_seconds=3.0)
    seen_req_hdrs: list[dict[str, str]] = []

    def _capture_sync(request: httpx.Request) -> httpx.Response:
        seen_req_hdrs.append(dict(request.headers))
        return httpx.Response(200)

    with service.create_http_client(cfg) as sync_c:
        sync_c._transport = httpx.MockTransport(_capture_sync)
        with _core.bind_request_id("cid-sync-1"):
            sync_c.get("https://example.com")
    assert seen_req_hdrs[-1]["x-request-id"] == "cid-sync-1"

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

    post_calls = 0

    class _PostClient:
        def request(self, *_a: object, **_k: object) -> SimpleNamespace:
            nonlocal post_calls
            post_calls += 1
            return SimpleNamespace(status_code=503, headers={})

    assert (
        service.request_with_retry(_PostClient(), "POST", "https://example.com").status_code == 503
    )
    assert post_calls == 1

    async def _async_checks() -> None:
        a_calls = 0

        class _AsyncFake:
            async def request(self, method: str, url: str, **_k: object) -> SimpleNamespace:
                nonlocal a_calls
                a_calls += 1
                if a_calls == 1:
                    return SimpleNamespace(status_code=503, headers={})
                return SimpleNamespace(status_code=200, headers={})

        a_sleeps: list[float] = []

        async def _asleep(s: float) -> None:
            a_sleeps.append(s)

        aresp = await service.async_request_with_retry(
            _AsyncFake(), "GET", "https://example.com", max_attempts=2, sleep=_asleep
        )
        assert aresp.status_code == 200 and len(a_sleeps) == 1

        async with service.create_async_http_client(cfg) as ac:
            ac._transport = httpx.MockTransport(_capture_sync)
            with _core.bind_request_id("cid-async-1"):
                await ac.get("https://example.com/async")
        assert seen_req_hdrs[-1]["x-request-id"] == "cid-async-1"

        err_probe = await service.probe_http(
            "http://user:topsecret@127.0.0.1:1/health",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _r: (_ for _ in ()).throw(
                        httpx.ConnectError("failed http://user:topsecret@127.0.0.1:1/health")
                    )
                )
            ),
        )
        assert err_probe.status is service.DependencyStatus.ABSENT
        assert "topsecret" not in err_probe.detail

    asyncio.run(_async_checks())

    now = [0.0]
    win = service.RateLimitWindow(max_requests=2, window_seconds=10.0, clock=lambda: now[0])
    assert win.acquire() == (True, 0.0)
    now[0] = 2.0
    assert win.acquire() == (True, 0.0)
    assert win.acquire()[0] is False
    now[0] = 10.5
    assert win.acquire() == (True, 0.0)
    assert win.acquire()[0] is False
