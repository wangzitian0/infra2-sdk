"""Tests for FastAPI 1-liner runtime integration and observability endpoints."""

from fastapi import FastAPI
from starlette.testclient import TestClient

from infra2_sdk.fastapi import init_service


def test_fastapi_init_endpoints_without_db():
    app = FastAPI()
    init_service(app, name="auth-service", enable_otel=False)
    client = TestClient(app)

    # 1. /livez (process liveness)
    livez_res = client.get("/livez")
    assert livez_res.status_code == 200
    assert livez_res.json() == {"status": "alive", "service": "auth-service"}

    # 2. /readyz (no db configured -> ready)
    readyz_res = client.get("/readyz")
    assert readyz_res.status_code == 200
    assert readyz_res.json() == {"status": "ready", "service": "auth-service"}

    # 3. /health (aggregate)
    health_res = client.get("/health")
    assert health_res.status_code == 200
    data = health_res.json()
    assert data["status"] == "healthy"
    assert data["service"] == "auth-service"
    assert data["checks"] == {}
    assert data["reasons"] == []


def test_fastapi_init_with_healthy_db():
    app = FastAPI()
    init_service(
        app,
        name="finance-backend",
        db_checker=lambda: (True, "postgres ok"),
        enable_otel=False,
    )
    client = TestClient(app)

    # /readyz
    res = client.get("/readyz")
    assert res.status_code == 200
    assert res.json()["status"] == "ready"

    # /health
    health_res = client.get("/health")
    assert health_res.status_code == 200
    data = health_res.json()
    assert data["status"] == "healthy"
    assert data["checks"]["database"]["status"] == "present"
    assert "duration_ms" in data["checks"]["database"]
    assert data["reasons"] == []


def test_fastapi_init_with_unhealthy_db_fails_closed():
    app = FastAPI()
    init_service(
        app,
        name="orders-api",
        db_checker=lambda: (False, "connection pool timeout"),
        enable_otel=False,
    )
    client = TestClient(app)

    # /livez must still be 200 (anti-puppet: external failure must NOT crash container)
    livez = client.get("/livez")
    assert livez.status_code == 200

    # /readyz must return 503
    readyz = client.get("/readyz")
    assert readyz.status_code == 503
    assert "connection pool timeout" in readyz.json()["reasons"]

    # /health must return 503 with reasons
    health = client.get("/health")
    assert health.status_code == 503
    data = health.json()
    assert data["status"] == "unhealthy"
    assert data["checks"]["database"]["status"] == "failed"
    assert "connection pool timeout" in data["reasons"]


def test_fastapi_init_with_db_exception():
    def exploding_checker():
        raise ConnectionRefusedError("socket closed by peer")

    app = FastAPI()
    init_service(
        app,
        name="payment-svc",
        db_checker=exploding_checker,
        enable_otel=False,
    )
    client = TestClient(app)

    # Must fail closed with 503 without crashing the process
    readyz = client.get("/readyz")
    assert readyz.status_code == 503
    assert any("ConnectionRefusedError" in r for r in readyz.json()["reasons"])

    health = client.get("/health")
    assert health.status_code == 503
    assert health.json()["status"] == "unhealthy"


def test_fastapi_init_with_otel_enabled(monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES",
        "deployment.environment=staging,service.version=1.0.0",
    )
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
        MagicMock(),
    )
    app = FastAPI()
    init_service(app, name="metrics-svc", enable_otel=True)
    client = TestClient(app)
    res = client.get("/livez")
    assert res.status_code == 200
    assert res.json() == {"status": "alive", "service": "metrics-svc"}
