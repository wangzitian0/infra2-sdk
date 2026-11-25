"""Behavior tests of the Canary Todo app (moved from infra2 common/tests/test_todo_service.py)."""

from __future__ import annotations

import gc
import importlib.util
import time
import warnings

import psycopg
import pytest
from conftest import TODO_APP, load_todo_module, wire_fake_todo_pg
from starlette.testclient import TestClient


def test_todo_app_endpoints_contract(monkeypatch) -> None:
    """Verify Todo app HTTP endpoints respond per the platform contract."""
    spec = importlib.util.spec_from_file_location("todo_app", TODO_APP)
    assert spec is not None and spec.loader is not None
    todo_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(todo_mod)

    # Mock probes to run deterministically in unit test
    monkeypatch.setattr(
        todo_mod,
        "_probe_postgres",
        lambda host, port, **kw: {
            "status": "pass",
            "latency_ms": 1.0,
            "detail": "mock postgres ok",
        },
    )
    monkeypatch.setattr(
        todo_mod,
        "_probe_redis",
        lambda *args, **kw: {
            "status": "pass",
            "latency_ms": 0.5,
            "detail": "mock redis pong & set/get ok",
        },
    )
    monkeypatch.setattr(
        todo_mod,
        "_probe_http",
        lambda url, **kw: {
            "status": "pass",
            "latency_ms": 1.5,
            "detail": "mock http 200",
        },
    )
    monkeypatch.setattr(
        todo_mod,
        "_probe_s3",
        lambda **kw: {
            "status": "pass",
            "latency_ms": 2.5,
            "detail": "mock s3 put/get/delete ok",
        },
    )

    client = TestClient(todo_mod.fastapi_app, raise_server_exceptions=False)

    # 1. /api/health
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["service"] == "platform/todo"
    assert data["domain"] == "todo.zitian.party"
    assert data["environment"] == "unknown"
    assert data["configuration_sha256"] == "unknown"
    assert data["version"] == "unknown"
    assert data["git_sha"] == "unknown"

    monkeypatch.setenv("GIT_COMMIT_SHA", "abcdef1234567890")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("CONFIGURATION_SHA256", "deadbeef01234567")
    reloaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reloaded)
    reloaded_client = TestClient(reloaded.fastapi_app, raise_server_exceptions=False)
    resp_ver = reloaded_client.get("/api/health")
    assert resp_ver.status_code == 503
    data_ver = resp_ver.json()
    assert data_ver["ok"] is False
    assert data_ver["status"] == "unhealthy"
    assert data_ver["environment"] == "staging"
    assert data_ver["configuration_sha256"] == "deadbeef01234567"
    assert data_ver["version"] == "abcdef1234567890"
    assert data_ver["git_sha"] == "abcdef1234567890"
    assert reloaded_client.get("/health").json()["git_sha"] == "abcdef1234567890"
    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("CONFIGURATION_SHA256", raising=False)

    # 2. /api/canary/status
    resp = client.get("/api/canary/status")
    assert resp.status_code == 200
    status_report = resp.json()
    assert status_report["ok"] is True
    assert "checks" in status_report
    checks = status_report["checks"]
    assert set(checks.keys()) == {
        "postgres",
        "redis",
        "s3",
        "signoz",
        "openpanel",
        "authentik",
    }
    assert "minio" not in checks
    for c in checks.values():
        assert c["status"] == "pass"

    # 3. /api/todos fails closed with 503 before PostgreSQL is configured
    assert client.get("/api/todos").status_code == 503

    # Wire fake persistent PostgreSQL store for CRUD roundtrips
    wire_fake_todo_pg(monkeypatch)

    # 3b. /api/todos (GET on clean database)
    resp = client.get("/api/todos")
    assert resp.status_code == 200
    assert resp.json() == []

    # 4. /api/todos (POST valid item)
    resp = client.post(
        "/api/todos",
        json={"title": "Canary Automated Task", "capability": "redis"},
    )
    assert resp.status_code == 201
    created = resp.json()
    assert created["title"] == "Canary Automated Task"
    assert created["capability"] == "redis"
    assert created["completed"] is False
    created_id = created["id"]

    # 5. /api/todos (POST invalid JSON -> 400 Bad Request)
    resp = client.post(
        "/api/todos",
        content=b"invalid-json{",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    err_body = resp.json()
    assert "error" in err_body

    # 5b. /api/todos (POST non-boolean completed -> 400 Bad Request)
    resp = client.post(
        "/api/todos",
        json={"title": "bad", "completed": "false"},
    )
    assert resp.status_code == 400
    err_body = resp.json()
    assert "completed" in err_body.get("error", "")

    # 6. /api/todos/{id} (PATCH empty toggle + explicit field update)
    resp = client.patch(f"/api/todos/{created_id}")
    assert resp.status_code == 200
    toggled = resp.json()
    assert toggled["id"] == created_id
    assert toggled["completed"] is True

    resp = client.patch(
        f"/api/todos/{created_id}",
        json={"title": "Updated Canary Task", "completed": False},
    )
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["title"] == "Updated Canary Task"
    assert updated["completed"] is False

    # 7. /api/todos/{id} (DELETE)
    resp = client.delete(f"/api/todos/{created_id}")
    assert resp.status_code == 200
    del_result = resp.json()
    assert del_result["ok"] is True
    assert del_result["deleted_id"] == created_id

    # 8. /api/todos/{id} (DELETE non-existent -> 404)
    resp = client.delete(f"/api/todos/{created_id}")
    assert resp.status_code == 404

    # 9. Web UI (/)
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "todo.zitian.party" in html
    assert "Canary Todo List" in html


def test_todo_app_concurrency_nonblocking(monkeypatch) -> None:
    """AC-canary.concurrency: a slow canary run must not block a health call.

    Both requests go through ONE ASGI client on ONE event loop. A `TestClient` per request
    gives each request its own loop in its own thread, so a blocked loop would go unseen.
    The canary endpoint is a sync `def`: FastAPI runs it in a worker thread and the loop
    stays free. An `async def` endpoint that blocks would hold the loop, the canary call
    would finish first, and this test would fail.
    """
    import asyncio

    import httpx

    todo_mod = load_todo_module("todo_app_concurrency")

    # Simulate slow probes: each check sleeps 0.5 s inside its worker thread
    def _slow_probe(*args, **kwargs):
        time.sleep(0.5)
        return {"status": "pass", "latency_ms": 500.0, "detail": "slow mock"}

    monkeypatch.setattr(todo_mod, "_probe_postgres", _slow_probe)
    monkeypatch.setattr(todo_mod, "_probe_redis", _slow_probe)
    monkeypatch.setattr(todo_mod, "_probe_http", _slow_probe)
    monkeypatch.setattr(todo_mod, "_probe_s3", _slow_probe)

    finished: list[str] = []

    async def scenario():
        transport = httpx.ASGITransport(app=todo_mod.fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://todo.test") as client:

            async def canary():
                response = await client.get("/api/canary/status")
                finished.append("canary")
                return response

            async def health():
                await asyncio.sleep(0.05)  # the canary request is in flight by now
                response = await client.get("/livez")
                finished.append("health")
                return response

            return await asyncio.gather(canary(), health())

    canary_resp, health_resp = asyncio.run(scenario())

    assert health_resp.status_code == 200
    assert finished == ["health", "canary"], (
        "the health call must finish while the slow canary run is still going"
    )
    assert canary_resp.status_code == 200, canary_resp.text
    assert "checks" in canary_resp.json()


def test_todo_app_authentik_identity_and_me_endpoint() -> None:
    """Verify /api/auth/me and UI rendering under Authentik ForwardAuth headers."""
    spec = importlib.util.spec_from_file_location("todo_app", TODO_APP)
    assert spec is not None and spec.loader is not None
    todo_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(todo_mod)

    client = TestClient(todo_mod.fastapi_app)

    # Unauthenticated /api/auth/me
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is False
    assert data["username"] is None
    assert data["groups"] == []

    # Authenticated /api/auth/me with ForwardAuth headers
    auth_headers = {
        "X-authentik-username": "wangzitian0",
        "X-authentik-name": "Zitian Wang",
        "X-authentik-email": "wangzitian0@gmail.com",
        "X-authentik-groups": "admins,developers",
    }
    resp = client.get("/api/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    auth_data = resp.json()
    assert auth_data["authenticated"] is True
    assert auth_data["username"] == "wangzitian0"
    assert auth_data["name"] == "Zitian Wang"
    assert auth_data["email"] == "wangzitian0@gmail.com"
    assert auth_data["groups"] == ["admins", "developers"]

    # UI renders authenticated badge with name
    ui_headers = {
        "X-authentik-username": "wangzitian0",
        "X-authentik-name": "Zitian Wang",
    }
    resp = client.get("/", headers=ui_headers)
    assert resp.status_code == 200
    html = resp.text
    assert "SSO 认证通过: Zitian Wang" in html

    # UI escapes HTML entities in user header to prevent XSS
    xss_headers = {
        "X-authentik-username": "attacker",
        "X-authentik-name": "<script>alert('xss')</script>",
    }
    resp = client.get("/", headers=xss_headers)
    assert resp.status_code == 200
    html_xss = resp.text
    assert "<script>alert('xss')</script>" not in html_xss
    assert "&lt;script&gt;" in html_xss

    # HTTP HEAD requests supported without 501 error and return no body
    resp_head = client.head("/api/health")
    assert resp_head.status_code == 503
    assert resp_head.headers.get("Content-Type") == "application/json"
    assert "HEAD" in (resp_head.headers.get("Access-Control-Allow-Methods") or "")
    assert resp_head.text == ""

    # HEAD request on HTML UI root returns headers but no body
    resp_ui_head = client.head("/")
    assert resp_ui_head.status_code == 200
    assert "text/html" in (resp_ui_head.headers.get("Content-Type") or "")
    assert "HEAD" in (resp_ui_head.headers.get("Access-Control-Allow-Methods") or "")
    assert resp_ui_head.text == ""

    # OPTIONS preflight request returns 204 with CORS headers
    resp_options = client.options("/api/todos")
    assert resp_options.status_code == 204
    assert resp_options.headers.get("Access-Control-Allow-Origin") == "*"
    assert "OPTIONS" in (resp_options.headers.get("Access-Control-Allow-Methods") or "")
    assert resp_options.headers.get("Content-Length") == "0"


def test_todo_readyz_and_health_follow_the_canary_status(monkeypatch) -> None:
    """/readyz and /health answer from the canary status: 200 when every gating check
    passes, 503 with the failed checks as reasons when one fails."""
    todo_mod = load_todo_module("todo_app_readiness")
    client = TestClient(todo_mod.fastapi_app)

    monkeypatch.setattr(todo_mod, "cached_canary_status", lambda: {"ok": True})
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "service": "platform-todo"}
    healthy = client.get("/health")
    assert healthy.status_code == 200
    assert healthy.json()["status"] == "healthy"
    assert healthy.json()["service"] == "platform-todo"
    assert healthy.json()["reasons"] == []
    assert set(healthy.json()["checks"]) == {"postgres", "redis", "s3"}
    assert healthy.json()["checks"]["redis"]["status"] == "present"

    monkeypatch.setattr(
        todo_mod,
        "cached_canary_status",
        lambda: {"ok": False, "failed": ["redis"]},
    )
    not_ready = client.get("/readyz")
    assert not_ready.status_code == 503
    assert not_ready.json() == {
        "status": "not_ready",
        "service": "platform-todo",
        "reasons": ["redis: failed=redis"],
    }
    unhealthy = client.get("/health")
    assert unhealthy.status_code == 503
    assert unhealthy.json()["status"] == "unhealthy"
    assert unhealthy.json()["service"] == "platform-todo"
    assert unhealthy.json()["reasons"] == ["redis: failed=redis"]
    assert unhealthy.json()["checks"]["redis"]["status"] == "absent"
    assert unhealthy.json()["checks"]["postgres"]["status"] == "present"


def _raise_boom() -> dict:
    raise RuntimeError("boom")


@pytest.mark.parametrize(
    ("status", "reasons"),
    [
        ({"ok": False, "unconfigured": ["s3"]}, ["s3: unconfigured=s3"]),
        ({"ok": False, "failed": ["redis"]}, ["redis: failed=redis"]),
        (
            {"ok": False, "failed": ["redis"], "unconfigured": ["s3"]},
            ["redis: failed=redis", "s3: unconfigured=s3"],
        ),
        (
            {"ok": False},
            [
                "postgres: canary checks not ok",
                "redis: canary checks not ok",
                "s3: canary checks not ok",
            ],
        ),
        (
            {},
            [
                "postgres: canary checks not ok",
                "redis: canary checks not ok",
                "s3: canary checks not ok",
            ],
        ),
        (
            _raise_boom,
            [
                "postgres: canary status check failed: boom",
                "redis: canary status check failed: boom",
                "s3: canary status check failed: boom",
            ],
        ),
    ],
    ids=[
        "unconfigured",
        "failed",
        "failed-and-unconfigured",
        "not-ok-without-a-list",
        "empty-document",
        "status-call-raises",
    ],
)
@pytest.mark.parametrize("path", ["/readyz", "/health"])
def test_todo_readiness_fails_closed_and_names_the_reason(
    monkeypatch, path: str, status, reasons: list[str]
) -> None:
    """A red, unconfigured or unreadable canary status is never ready."""
    todo_mod = load_todo_module("todo_app_fail_closed")
    client = TestClient(todo_mod.fastapi_app)
    monkeypatch.setattr(
        todo_mod,
        "cached_canary_status",
        status if callable(status) else (lambda: dict(status)),
    )

    response = client.get(path)

    assert response.status_code == 503
    assert response.json()["reasons"] == reasons
    assert response.json()["status"] in ("not_ready", "unhealthy")


@pytest.mark.parametrize("path", ["/readyz", "/health"])
def test_todo_readiness_passes_only_on_an_ok_document(monkeypatch, path: str) -> None:
    todo_mod = load_todo_module("todo_app_ready")
    client = TestClient(todo_mod.fastapi_app)
    monkeypatch.setattr(todo_mod, "cached_canary_status", lambda: {"ok": True})
    assert client.get(path).status_code == 200


def test_todo_fastapi_endpoints_contract(monkeypatch) -> None:
    """Verify Canary Todo exposes FastAPI endpoints with strict OpenAPI schemas."""
    wire_fake_todo_pg(monkeypatch)
    # Collect loops that earlier tests left to the garbage collector first; their
    # ResourceWarning is not a warning of this module load.
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        todo_mod = load_todo_module("todo_app_fastapi")
    assert caught == [], [str(w.message) for w in caught]

    client = TestClient(todo_mod.fastapi_app)

    # 1. Standard /livez probe from infra2_sdk.fastapi.init_service
    resp_livez = client.get("/livez")
    assert resp_livez.status_code == 200
    assert resp_livez.json() == {"status": "alive", "service": "platform-todo"}

    # 2. /readyz and 3. /health follow the canary status; the next test pins both answers

    # 4. Backwards-compatible /api/health fails closed when gating checks have not passed
    resp_api_health = client.get("/api/health")
    assert resp_api_health.status_code == 503
    assert resp_api_health.json()["ok"] is False
    assert resp_api_health.json()["service"] == "platform/todo"

    # 5. OpenAPI schemas on all JSON routes, typed request bodies, and unique operationIds
    openapi_res = client.get("/openapi.json")
    assert openapi_res.status_code == 200
    spec_doc = openapi_res.json()
    paths = spec_doc["paths"]
    for rp, method in (
        ("/health", "get"),
        ("/livez", "get"),
        ("/readyz", "get"),
        ("/api/health", "get"),
        ("/api/canary/status", "get"),
        ("/api/todos", "post"),
        ("/api/todos/{todo_id}", "patch"),
        ("/api/todos/{todo_id}", "delete"),
        ("/api/auth/me", "get"),
    ):
        resp_200 = paths[rp][method]["responses"].get("200") or paths[rp][method]["responses"].get(
            "201"
        )
        schema = resp_200["content"]["application/json"]["schema"]
        assert "$ref" in schema, f"{method.upper()} {rp} missing $ref schema: {schema}"
    todos_get_schema = paths["/api/todos"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert todos_get_schema["type"] == "array"
    assert "$ref" in todos_get_schema["items"]
    for write_method, write_path in (
        ("post", "/api/todos"),
        ("patch", "/api/todos/{todo_id}"),
    ):
        req_schema = paths[write_path][write_method]["requestBody"]["content"]["application/json"][
            "schema"
        ]
        assert "$ref" in req_schema or "anyOf" in req_schema, (
            f"{write_method.upper()} {write_path} missing Pydantic requestBody schema: {req_schema}"
        )
    assert "/api/todos/{todo_id}/toggle" not in paths
    op_ids = [
        op["operationId"]
        for path_item in paths.values()
        for op in path_item.values()
        if isinstance(op, dict) and "operationId" in op
    ]
    assert len(op_ids) == len(set(op_ids))

    # 6. Todo REST API
    resp_todos = client.get("/api/todos")
    assert resp_todos.status_code == 200
    assert isinstance(resp_todos.json(), list)


def test_todo_persists_across_module_reloads(monkeypatch) -> None:
    """Todos stored via one app instance remain visible after reloading app.py."""
    store = wire_fake_todo_pg(monkeypatch)
    mod_a = load_todo_module("todo_app_reload_a")
    client_a = TestClient(mod_a.fastapi_app)
    created = client_a.post(
        "/api/todos",
        json={"title": "Survive Restart", "capability": "postgres"},
    )
    assert created.status_code == 201
    todo_id = created.json()["id"]

    mod_b = load_todo_module("todo_app_reload_b")
    client_b = TestClient(mod_b.fastapi_app)
    listed = client_b.get("/api/todos")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [todo_id]
    assert listed.json()[0]["title"] == "Survive Restart"
    assert len(store.rows) == 1


def test_todo_db_unavailable_fails_closed_with_503(monkeypatch) -> None:
    """Missing DB credentials or psycopg OperationalError returns HTTP 503."""
    todo_mod = load_todo_module("todo_app_503")
    client = TestClient(todo_mod.fastapi_app, raise_server_exceptions=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    res_unconfigured = client.get("/api/todos")
    assert res_unconfigured.status_code == 503
    assert "error" in res_unconfigured.json()

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://todo_app:test_todo_pg_pass_1234@platform-postgres:5432/postgres",
    )

    def _boom(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", _boom)
    res_down = client.post("/api/todos", json={"title": "Should Fail Closed"})
    assert res_down.status_code == 503
    assert "error" in res_down.json()


def test_health_reports_the_installed_sdk_version(monkeypatch) -> None:
    """The infra2 staging gate compares this field with the Canary image pin (#247)."""
    from importlib.metadata import version

    wire_fake_todo_pg(monkeypatch)
    todo_mod = load_todo_module("todo_app_sdk_version")
    with TestClient(todo_mod.fastapi_app) as client:
        assert client.get("/api/health").json()["sdk_version"] == version("infra2-sdk")
        monkeypatch.setattr(todo_mod, "_installed_sdk_version", lambda: "0.0.0")
        assert client.get("/api/health").json()["sdk_version"] == "0.0.0"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql://todo_app:p%40ss%2Fword@db.internal:6543/todo%20db?sslmode=require",
            ("db.internal", 6543, "todo_app", "p@ss/word", "todo db"),
        ),
        ("postgresql+psycopg://u:p@h/d", ("h", 5432, "u", "p", "d")),
        ("postgres://u:p@h", ("h", 5432, "u", "p", "postgres")),
        ("postgresql://u:p@[::1]:6543/d", ("::1", 6543, "u", "p", "d")),
    ],
)
def test_database_target_reads_only_database_url(monkeypatch, url, expected) -> None:
    todo_mod = load_todo_module("todo_app_database_target")
    for retired in ("POSTGRES_HOST", "TODO_POSTGRES_USER", "CANARY_POSTGRES_USER"):
        monkeypatch.setenv(retired, "retired-name-must-not-be-read")
    monkeypatch.setenv("DATABASE_URL", url)
    assert todo_mod._database_target() == expected


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("", "DATABASE_URL is not configured"),
        ("mysql://u:p@h/d", "DATABASE_URL is not a PostgreSQL URL"),
        ("postgresql://u:p@h:notaport/d", "DATABASE_URL is not a PostgreSQL URL"),
        ("postgresql://u:SECRET/PW@h:5432/d", "DATABASE_URL is not a PostgreSQL URL"),
        ("postgresql://u:SECRET#PW@h/d", "DATABASE_URL is not a PostgreSQL URL"),
        ("postgresql://u@h/d", "DATABASE_URL is not a PostgreSQL URL"),
        ("postgresql:///d?host=h&user=u", "DATABASE_URL is not a PostgreSQL URL"),
    ],
)
def test_database_target_names_the_fault_and_hides_the_value(monkeypatch, url, message) -> None:
    todo_mod = load_todo_module("todo_app_database_fault")
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(ValueError) as caught:
        todo_mod._database_target()
    assert str(caught.value).startswith(message)
    assert "SECRET" not in str(caught.value)
    with pytest.raises(todo_mod._TodoStoreUnavailable, match=message):
        todo_mod._connect_todo_db()


def test_postgres_check_fails_on_a_malformed_database_url(monkeypatch) -> None:
    todo_mod = load_todo_module("todo_app_database_malformed")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:SECRET/PW@h:5432/d")
    monkeypatch.setattr(todo_mod, "_probe_postgres", lambda *a, **k: pytest.fail("no login"))
    check = todo_mod.run_all_checks()["checks"]["postgres"]
    assert check["status"] == "fail"
    assert check["detail"].startswith("DATABASE_URL is not a PostgreSQL URL")
    assert "SECRET" not in check["detail"]


def test_postgres_probe_brackets_an_ipv6_host() -> None:
    todo_mod = load_todo_module("todo_app_database_ipv6")
    seen: list[str] = []

    def _connector(dsn: str, **kwargs):
        seen.append(dsn)
        raise OSError("stop after capture")

    todo_mod._probe_postgres("::1", 6543, user="u", password="p" * 20, connector=_connector)
    assert seen and "@[::1]:6543/postgres" in seen[0]


def test_todo_store_connects_with_the_database_url_login(monkeypatch) -> None:
    todo_mod = load_todo_module("todo_app_database_connect")
    seen: dict = {}

    def _connect(**kwargs):
        seen.update(kwargs)
        raise psycopg.OperationalError("stop after capture")

    monkeypatch.setenv("DATABASE_URL", "postgresql://todo_app:s3cret%21@db.internal:6543/todos")
    monkeypatch.setattr(psycopg, "connect", _connect)
    with pytest.raises(psycopg.OperationalError):
        todo_mod._connect_todo_db()
    assert seen == {
        "host": "db.internal",
        "port": 6543,
        "user": "todo_app",
        "password": "s3cret!",
        "dbname": "todos",
        "connect_timeout": 5,
        "options": "-c statement_timeout=5000",
    }


def test_an_unset_address_makes_its_check_unconfigured_without_a_connection(monkeypatch) -> None:
    from conftest import DEPLOYMENT_ADDRESSES

    todo_mod = load_todo_module("todo_app_no_addresses")
    for name in (*DEPLOYMENT_ADDRESSES, "S3_ENDPOINT", "DATABASE_URL", "REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(todo_mod, "probe_redis", lambda *a, **k: pytest.fail("redis"))
    monkeypatch.setattr(todo_mod, "_probe_http", lambda *a, **k: pytest.fail("http"))
    monkeypatch.setattr(todo_mod, "_probe_http_async", lambda *a, **k: pytest.fail("http"))

    report = todo_mod.run_all_checks()

    details = {name: (c["status"], c["detail"]) for name, c in report["checks"].items()}
    assert details["redis"] == ("unconfigured", "REDIS_URL is not configured")
    assert details["postgres"] == ("unconfigured", "DATABASE_URL is not configured")
    assert details["s3"][0] == "unconfigured" and "S3_ENDPOINT" in details["s3"][1]
    assert details["signoz"] == ("unconfigured", "SIGNOZ_OTEL_COLLECTOR is not configured")
    assert details["openpanel"] == ("unconfigured", "OPENPANEL_API_URL is not configured")
    assert details["authentik"] == ("unconfigured", "AUTHENTIK_ENDPOINT is not configured")
    assert report["ok"] is False


def test_an_unset_liveness_address_does_not_gate_the_verdict(monkeypatch) -> None:
    todo_mod = load_todo_module("todo_app_no_liveness")
    for name in ("SIGNOZ_OTEL_COLLECTOR", "OPENPANEL_API_URL", "AUTHENTIK_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    ok = {"status": "pass", "latency_ms": 1.0, "detail": "ok"}
    for probe in ("_probe_redis", "_probe_postgres", "_probe_s3"):
        monkeypatch.setattr(todo_mod, probe, lambda *a, **k: dict(ok))
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pppppppppppppppp@h/d")

    report = todo_mod.run_all_checks()

    assert report["ok"] is True
    assert {report["checks"][n]["status"] for n in todo_mod.INFORMATIONAL_CHECKS} == {
        "unconfigured"
    }


def test_no_setting_default_names_a_host() -> None:
    from canary_settings import CanarySettings

    settings = CanarySettings.from_environ({})
    text = {
        name: getattr(settings, name)
        for name, info in CanarySettings.model_fields.items()
        if info.annotation is str
    }
    assert len(text) == 29
    assert text == dict.fromkeys(text, ""), "a text default would name a host or a value"
    assert settings.port == 8000


def test_a_sensitive_value_keeps_its_edge_whitespace_and_a_blank_value_is_unset() -> None:
    from canary_settings import CanarySettings

    settings = CanarySettings.from_environ(
        {
            "REDIS_URL": " redis://:pw@redis:6379/0 ",
            "S3_ENDPOINT": "  http://s3:9000  ",
            "S3_SECRET_KEY": "   ",
        }
    )
    assert settings.redis_url == " redis://:pw@redis:6379/0 "
    assert settings.s3_endpoint == "http://s3:9000"
    assert settings.s3_secret_key == ""


def test_a_malformed_redis_url_is_not_reported_as_a_database_fault(monkeypatch) -> None:
    todo_mod = load_todo_module("todo_app_bad_redis_url")
    monkeypatch.setenv("REDIS_URL", "not-a-redis-url")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pppppppppppppppp@h:6543/d")
    assert todo_mod._database_target() == ("h", 6543, "u", "pppppppppppppppp", "d")


def test_the_manifest_file_is_the_output_of_the_settings_class() -> None:
    import json

    from canary_settings import CanarySettings

    from infra2_sdk.runtime.config_schema import environment_manifest_from_model

    manifest = environment_manifest_from_model(CanarySettings, source="canary/todo")
    expected = json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n"
    assert (TODO_APP.parent / "env.manifest.json").read_text(encoding="utf-8") == expected
    # The app manifest holds no binding of one platform.
    assert [f.env for f in manifest.fields if f.provided_by or f.composed_from] == []
    assert manifest.validate_offline() == []


def test_app_reads_the_process_environment_only_through_the_settings_class() -> None:
    import ast

    tree = ast.parse(TODO_APP.read_text(encoding="utf-8"))
    # `os` has one name in the module: no alias and no `from os import ...`.
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert [n for n in imports if isinstance(n, ast.ImportFrom) and n.module == "os"] == []
    assert [
        alias.asname
        for n in imports
        if isinstance(n, ast.Import)
        for alias in n.names
        if alias.name == "os"
    ] == [None]

    names = {"environ", "environb", "getenv", "getenvb", "putenv"}

    def reads(node: ast.AST) -> bool:
        return (isinstance(node, ast.Attribute) and node.attr in names) or (
            isinstance(node, ast.Name) and node.id in names
        )

    # No lookup of an `os` attribute by a text name: `getattr(os, "environ")`.
    dynamic = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "vars"}
        and any(isinstance(arg, ast.Name) and arg.id == "os" for arg in node.args)
    ]
    assert dynamic == []

    functions = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    inside = {id(node): func.name for func in functions for node in ast.walk(func) if reads(node)}
    every = [node for node in ast.walk(tree) if reads(node)]
    assert every, "the guard must see the reads"
    assert [node.lineno for node in every if id(node) not in inside] == [], (
        "a read outside a function"
    )
    # `_settings` builds the settings. `_database_target` reads its one field through the
    # class. `summarize_recent_todos` hands the whole mapping to the SDK LLM settings reader.
    assert sorted(set(inside.values())) == [
        "_database_target",
        "_settings",
        "summarize_recent_todos",
        # Its `environ` parameter goes to the SDK manifest validation.
        "validate_startup_environment",
    ]
    # Each read passes the mapping on as a call argument: no `os.environ.get(...)` and
    # no `os.environ[...]`.
    for node in every:
        parent = next(p for p in ast.walk(tree) if node in ast.iter_child_nodes(p))
        if isinstance(parent, ast.keyword):
            parent = next(p for p in ast.walk(tree) if parent in ast.iter_child_nodes(p))
        assert isinstance(parent, ast.Call), ast.dump(parent)[:120]
        assert node is not parent.func and node not in ast.walk(parent.func), node.lineno
        # Only these readers receive the mapping. `dict(os.environ)` is not one of them.
        assert ast.unparse(parent.func) in {
            "CanarySettings.from_environ",
            "CanarySettings.read",
            "LlmSettings.from_mapping",
            "assert_environment",
        }, ast.unparse(parent.func)
