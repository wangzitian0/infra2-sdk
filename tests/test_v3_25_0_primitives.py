"""Falsifiable unit and integration tests for v3.25.0 cross-repository primitives (#193)."""

from __future__ import annotations

import ast
import email.utils
import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.testclient import TestClient

from infra2_sdk.capacity import RateLimitWindow
from infra2_sdk.deploy import _main as deploy_cli_main
from infra2_sdk.deploy import default_http_get, parse_wheel_version
from infra2_sdk.fastapi import _ProbeAccessFilter, init_service
from infra2_sdk.observability import (
    CheckVerdict,
    Trail,
    check_name,
    reconcile_issue_trail,
    title_for,
)
from infra2_sdk.rules import (
    LayerRule,
    check_import_boundaries,
    find_dependency_cycle,
    scan_module_imports,
)
from infra2_sdk.runtime.environment import EnvironmentTier
from infra2_sdk.runtime.health import (
    Dependency,
    DependencyKind,
    DependencyManifest,
    DependencyStatus,
    ProbeResult,
)
from infra2_sdk.runtime.http import request_with_retry
from infra2_sdk.runtime.otel import OtelSettings, _build_sampler
from infra2_sdk.runtime.postgres import PostgresSettings, probe_postgres
from infra2_sdk.runtime.s3 import put_object_bytes

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "infra2_sdk"


def test_sdk_source_byte_budget() -> None:
    total = sum(
        p.stat().st_size
        for p in SRC_ROOT.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )
    # 4.0.0 measured 409,805 B after #236 Release B and K6 removed LlmClient and schema 2
    # and #238/#239 added the compose compiler, schema_check and the settings reference.
    # 4.5.0 measured 412,686 B after the bootstrap cap and `bootstrap_seconds` were added.
    # 4.6.0 measured 410,284 B after Wave 1 removed dead shims and `arequest_with_retry`.
    # A new feature raises this cap with a comment; a removal lowers it.
    assert 150_000 <= total <= 411_000, f"src/infra2_sdk byte budget drifted: {total} B"


def test_init_service_multi_checks_and_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "staging")

    class DbCheck:
        name = "database"

        def probe(self) -> ProbeResult:
            return ProbeResult("database", DependencyStatus.PRESENT, "ok")

    class CacheCheck:
        def __init__(self, status: DependencyStatus) -> None:
            self.name = "cache"
            self._status = status

        async def probe(self) -> ProbeResult:
            return ProbeResult("cache", self._status, "redis-down")

    manifest = DependencyManifest(
        [
            Dependency(
                name="database",
                kind=DependencyKind.CODE_DOMINANT,
                required_in=frozenset({EnvironmentTier.STAGING, EnvironmentTier.PRODUCTION}),
                env_vars=frozenset({"DATABASE_URL"}),
            ),
            Dependency(
                name="cache",
                kind=DependencyKind.CODE_DOMINANT,
                required_in=frozenset({EnvironmentTier.PRODUCTION}),
                env_vars=frozenset({"REDIS_URL"}),
            ),
        ]
    )

    app = FastAPI()
    init_service(
        app,
        name="multi-svc",
        checks=[DbCheck(), CacheCheck(DependencyStatus.ABSENT)],
        manifest=manifest,
        enable_otel=False,
    )
    with TestClient(app) as client:
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready", "service": "multi-svc"}
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "degraded"

    monkeypatch.setenv("ENVIRONMENT", "production")
    prod_app = FastAPI()
    init_service(
        prod_app,
        name="multi-svc",
        checks=[DbCheck(), CacheCheck(DependencyStatus.ABSENT)],
        manifest=manifest,
        enable_otel=False,
    )
    with TestClient(prod_app) as client:
        assert client.get("/readyz").status_code == 503
        assert client.get("/health").status_code == 503


def test_suppress_probe_telemetry_sampler_and_access_filter() -> None:
    import opentelemetry.sdk.trace.sampling as sampling

    settings = OtelSettings(
        service_name="quiet-svc",
        endpoint="http://collector:4318",
        suppress_probe_telemetry=True,
    )
    sampler = _build_sampler(sampling, settings)
    assert "DropProbeSampler" in sampler.get_description()

    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=sampler)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("GET /livez", attributes={"http.route": "/livez"}):
        pass
    with tracer.start_as_current_span("http", attributes={"url.path": "/health?verbose=1"}):
        pass
    with tracer.start_as_current_span("GET /api/orders", attributes={"url.path": "/api/orders"}):
        pass

    recorded = [s.name for s in exporter.get_finished_spans()]
    assert recorded == ["GET /api/orders"]

    flt = _ProbeAccessFilter()
    probe_rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/readyz?full=1", "1.1", 200),
        None,
    )
    msg_rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '127.0.0.1 - "GET /livez HTTP/1.1" 200',
        (),
        None,
    )
    app_rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/api/orders", "1.1", 200),
        None,
    )
    assert not flt.filter(probe_rec)
    assert not flt.filter(msg_rec)
    assert flt.filter(app_rec)

    app = FastAPI()
    init_service(app, name="quiet-app", enable_otel=False, suppress_probe_telemetry=True)
    uv_logger = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, _ProbeAccessFilter) for f in uv_logger.filters)


class _FakeHttpResponse:
    def __init__(
        self, status_code: int, payload: Any = None, headers: dict[str, str] | None = None
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload


class _SequenceSyncClient:
    def __init__(self, steps: list[Any]) -> None:
        self.steps = list(steps)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeHttpResponse:
        self.calls.append((method, url, kwargs))
        item = self.steps.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def test_request_with_retry() -> None:
    sleeps: list[float] = []
    future_dt = email.utils.format_datetime(datetime.now(UTC) + timedelta(seconds=2))
    sync_client = _SequenceSyncClient(
        [
            OSError("reset"),
            _FakeHttpResponse(429, headers={"Retry-After": "1.25"}),
            _FakeHttpResponse(503, headers={"Retry-After": future_dt}),
            _FakeHttpResponse(200, {"ok": True}),
        ]
    )
    resp = request_with_retry(
        sync_client, "GET", "https://example.test/v1", max_attempts=4, sleep=sleeps.append
    )
    assert resp.status_code == 200
    assert len(sleeps) == 3
    assert sleeps[1] == 1.25

    with pytest.raises(ValueError, match="at least 1"):
        request_with_retry(sync_client, "GET", "https://example.test", max_attempts=0)

    err_client = _SequenceSyncClient([OSError("boom"), OSError("boom2")])
    with pytest.raises(OSError, match="boom2"):
        request_with_retry(
            err_client, "GET", "https://example.test", max_attempts=2, sleep=lambda _: None
        )


def test_rate_limit_window_sliding_budget_and_cost() -> None:
    with pytest.raises(ValueError, match="limit and window_seconds"):
        RateLimitWindow(0, 10.0)
    with pytest.raises(ValueError, match="limit and window_seconds"):
        RateLimitWindow(5, 0.0)

    now = 100.0
    win = RateLimitWindow(3, 10.0, clock=lambda: now)
    with pytest.raises(ValueError, match="cost must be between 1 and limit"):
        win.acquire(0)
    with pytest.raises(ValueError, match="cost must be between 1 and limit"):
        win.acquire(4)

    assert win.acquire(2) == (True, 0.0)
    now += 3.0
    assert win.acquire(1) == (True, 0.0)
    assert win.used == 3

    allowed, retry_after = win.acquire(2)
    assert allowed is False
    assert retry_after == pytest.approx(7.0)

    now += 7.1
    assert win.used == 1
    assert win.acquire(2) == (True, 0.0)


def test_probe_postgres_custom_query_and_put_object_bytes() -> None:
    executed: list[str] = []

    class Conn:
        def execute(self, sql: str) -> Conn:
            executed.append(sql)
            return self

        def fetchone(self) -> tuple[int]:
            return (1,)

        def close(self) -> None:
            pass

    sql = "INSERT INTO canary_probe_log DEFAULT VALUES RETURNING 1"
    res = probe_postgres(
        PostgresSettings("postgresql://u:p@localhost/db"),
        connector=lambda *a, **kw: Conn(),
        query=sql,
    )
    assert res.status is DependencyStatus.PRESENT
    assert executed == [sql]

    puts: list[dict[str, Any]] = []

    class FakeS3:
        def put_object(self, **kwargs: Any) -> None:
            puts.append(kwargs)

    s3 = FakeS3()
    put_object_bytes(s3, bucket="bkt", key="k1", data=b"hello", content_type="text/plain")
    put_object_bytes(s3, bucket="bkt", key="k2", data=b"raw")
    assert puts == [
        {"Bucket": "bkt", "Key": "k1", "Body": b"hello", "ContentType": "text/plain"},
        {
            "Bucket": "bkt",
            "Key": "k2",
            "Body": b"raw",
            "ContentType": "application/octet-stream",
        },
    ]


def test_deploy_parse_wheel_version_and_cli() -> None:
    dockerfile = (
        "RUN pip install https://github.com/wangzitian0/infra2-sdk/releases/download/"
        "v3.25.0/infra2_sdk-3.25.0-py3-none-any.whl\n"
        'RUN pip install "infra2-sdk[all] @ git+https://github.com/wangzitian0/infra2-sdk.git@v3.25.0"\n'
    )
    assert parse_wheel_version(dockerfile) == "3.25.0"

    with pytest.raises(ValueError, match="no pinned infra2-sdk"):
        parse_wheel_version("FROM python:3.12-slim\n")

    with pytest.raises(ValueError, match="conflicting infra2-sdk pins"):
        parse_wheel_version(
            "infra2_sdk-3.24.0-py3-none-any.whl\ninfra2_sdk-3.25.0-py3-none-any.whl\n"
        )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/health")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "healthy", "version": "abc1234"}')

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        getter_no_redir = default_http_get(timeout=2.0, follow_redirects=False)
        code, _ = getter_no_redir(f"{base}/redirect")
        assert code == 302

        assert (
            deploy_cli_main(
                [
                    "--url",
                    f"{base}/redirect",
                    "--expected-version",
                    "abc1234",
                    "--require-status",
                    "healthy",
                    "--max-attempts",
                    "2",
                    "--interval-seconds",
                    "0.01",
                ]
            )
            == 0
        )
        assert (
            deploy_cli_main(
                [
                    "--url",
                    f"{base}/redirect",
                    "--no-follow-redirects",
                    "--max-attempts",
                    "1",
                ]
            )
            == 1
        )
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "infra2_sdk.deploy",
                "--url",
                f"{base}/health",
                "--expected-version",
                "abc1234",
                "--max-attempts",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert proc.returncode == 0
        assert "healthy after 1 attempt(s)" in proc.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_observability_parameterized_issue_trail() -> None:
    prefix = "fin-audit is red: "
    t = title_for("sec-edgar-lag", title_prefix=prefix)
    assert t == "fin-audit is red: sec-edgar-lag"
    assert check_name(t, title_prefix=prefix) == "sec-edgar-lag"
    assert check_name("ops-checks watchdog is red: dns", title_prefix=prefix) is None

    class FakeIssues:
        def __init__(self) -> None:
            self.issues: list[dict[str, Any]] = [
                {"number": 10, "title": "ops-checks watchdog is red: untouched"}
            ]
            self.created: list[tuple[str, str, tuple[str, ...]]] = []
            self.closed: list[int] = []

        def open_issues(self) -> list[dict[str, Any]]:
            return list(self.issues)

        def create(self, title: str, body: str, labels: Any) -> int:
            num = 100 + len(self.created)
            self.created.append((title, body, tuple(labels)))
            self.issues.append({"number": num, "title": title})
            return num

        def comment(self, number: int, body: str) -> None:
            pass

        def close(self, number: int) -> None:
            self.closed.append(number)

    api = FakeIssues()
    red_trail = Trail(
        failing={"sec-edgar-lag": CheckVerdict("sec-edgar-lag", False, "lag 900s", "P2", "ingest")},
        complete=True,
    )
    rc = reconcile_issue_trail(
        api,
        red_trail,
        mode="full",
        run_url="https://github.com/o/r/actions/runs/1",
        context="schedule",
        title_prefix=prefix,
        labels=("fin-alert",),
        source="finance-report audit",
    )
    assert rc == 0
    assert len(api.created) == 1
    assert api.created[0][0] == "fin-audit is red: sec-edgar-lag"
    assert api.created[0][2] == ("fin-alert",)
    assert "finance-report audit" in api.created[0][1]
    assert api.closed == []

    green_trail = Trail(green={"sec-edgar-lag"}, complete=True)
    assert (
        reconcile_issue_trail(
            api,
            green_trail,
            mode="full",
            title_prefix=prefix,
            labels=("fin-alert",),
            source="finance-report audit",
        )
        == 0
    )
    assert api.closed == [100]


def test_rules_imports_layers_cycles_type_checking_and_public_all(tmp_path: Path) -> None:
    pkg = tmp_path / "myapp"
    (pkg / "domain").mkdir(parents=True)
    (pkg / "service").mkdir(parents=True)
    (pkg / "api").mkdir(parents=True)

    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "domain" / "__init__.py").write_text(
        'from .entity import Order\n__all__ = ["Order"]\n', encoding="utf-8"
    )
    (pkg / "domain" / "entity.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from myapp.api import routes\n"
        "class Order:\n    pass\n"
        "class _SecretEntity:\n    pass\n",
        encoding="utf-8",
    )
    (pkg / "service" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "service" / "usecase.py").write_text(
        "from myapp.domain import Order\n", encoding="utf-8"
    )
    (pkg / "api" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "api" / "routes.py").write_text("from ..service import usecase\n", encoding="utf-8")

    layers = [
        LayerRule("domain", ("myapp.domain",), rank=0, depends_on=frozenset()),
        LayerRule("service", ("myapp.service",), rank=1, depends_on=frozenset({"domain"})),
        LayerRule("api", ("myapp.api",), rank=2),
    ]
    graph = scan_module_imports(pkg, package_prefix="myapp")
    assert "myapp.domain" in graph["myapp.service.usecase"]
    assert find_dependency_cycle(graph) is None
    assert (
        check_import_boundaries(pkg, layers, package_prefix="myapp", enforce_public_all=True) == []
    )

    strict_tc = check_import_boundaries(
        pkg, layers, package_prefix="myapp", ignore_type_checking=False
    )
    assert [v.kind for v in strict_tc] == ["forbidden_layer", "cycle"]

    (pkg / "service" / "usecase.py").write_text(
        "from myapp.domain.entity import Order\n"
        "from myapp.domain import _SecretEntity\n"
        "from myapp.api import routes\n",
        encoding="utf-8",
    )
    (pkg / "api" / "routes.py").write_text("from myapp.service import usecase\n", encoding="utf-8")
    violations = check_import_boundaries(
        pkg,
        [
            LayerRule("domain", ("myapp.domain",), rank=0),
            LayerRule("service", ("myapp.service",), rank=1),
            LayerRule("api", ("myapp.api",), rank=2),
        ],
        package_prefix="myapp",
        enforce_public_all=True,
    )
    kinds = {v.kind for v in violations}
    assert kinds == {"layer_rank", "deep_import", "private_export", "cycle"}


def test_rules_imports_skip_hidden_dirs_and_read_import_module_strings(tmp_path: Path) -> None:
    """wangzitian0/infra2#1823 Step 4.3: a repository-root scan must not walk `.venv/`, and
    `importlib.import_module("a.b")` is an import edge."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "lib" / "core.py").write_text(
        'import importlib\nplugin = importlib.import_module("cli.main")\n', encoding="utf-8"
    )
    (tmp_path / "lib" / "late.py").write_text(
        "from importlib import import_module\ndef load():\n    return import_module('cli.main')\n",
        encoding="utf-8",
    )
    (tmp_path / "cli").mkdir()
    (tmp_path / "cli" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "cli" / "main.py").write_text("from lib import core\n", encoding="utf-8")
    venv = tmp_path / ".venv" / "lib" / "site-packages" / "dep"
    venv.mkdir(parents=True)
    (venv / "a.py").write_text("from . import b\n", encoding="utf-8")
    (venv / "b.py").write_text("from . import a\n", encoding="utf-8")

    graph = scan_module_imports(tmp_path)
    assert not any(module.startswith(".venv") for module in graph)
    assert graph["lib.core"] == {"cli.main"}
    assert graph["lib.late"] == {"cli.main"}

    layers = [LayerRule("lib", ("lib",), rank=0), LayerRule("cli", ("cli",), rank=1)]
    violations = check_import_boundaries(tmp_path, layers, forbid_cycles=False)
    assert sorted((v.source_module, v.kind) for v in violations) == [
        ("lib.core", "layer_rank"),
        ("lib.late", "layer_rank"),
    ]


def test_rules_imports_dynamic_edges_only_for_importlib_and_absolute_literals() -> None:
    from infra2_sdk.rules.imports import _walk_imports

    def names(source: str, *, ignore_tc: bool = True) -> list[str]:
        nodes = _walk_imports(ast.parse(source), ignore_tc=ignore_tc)
        return sorted(
            node.module or "" if isinstance(node, ast.ImportFrom) else alias.name
            for node in nodes
            for alias in node.names
        )

    assert names('import importlib\nimportlib.import_module("a.b")\n') == ["a.b", "importlib"]
    assert names('loader.import_module("a.b")\n') == []
    assert names('import importlib\nimportlib.import_module(".rel", package="p")\n') == [
        "importlib"
    ]
    assert names("import importlib\nimportlib.import_module(name)\n") == ["importlib"]
    tc = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import_module('tc.only')\n"
        "else:\n"
        "    import else_direct\n"
    )
    assert names(tc) == ["else_direct", "typing"]
    assert names(tc, ignore_tc=False) == ["else_direct", "tc.only", "typing"]


def test_rules_imports_scan_root_under_a_hidden_directory(tmp_path: Path) -> None:
    root = tmp_path / ".cache" / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "a.py").write_text("from pkg import b\n", encoding="utf-8")
    (root / "pkg" / "b.py").write_text("", encoding="utf-8")
    assert scan_module_imports(root)["pkg.a"] == {"pkg.b"}
