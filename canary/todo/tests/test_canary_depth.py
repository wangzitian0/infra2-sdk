"""Canary depth for platform/30.todo (#991).

The canary exists to prove the platform's real paths. Before #991 it passed on `-NOAUTH`,
on a Postgres `E` reply and on S3 liveness, nobody read its status, and it sent no
telemetry. These tests pin the opposite behaviour:

1. Redis needs a real AUTH, then SETEX/GET/DEL of a random value. NOAUTH fails.
2. Postgres needs a real login as a dedicated role and `SELECT 1`. An `E` reply fails.
3. S3 needs PUT, GET and DELETE of one object under a canary prefix.
4. The probe runner consumes `/api/canary/status` through a ProbeFacet.
6. The service configures OpenTelemetry from the identity the deploy issues.

Moved from infra2 common/tests/test_todo_canary_depth.py (#247).
Deploy-declaration tests stay in infra2.
"""

from __future__ import annotations

import contextlib
import contextvars
import http.server
import json
import re
import secrets
import socket
import struct
import sys
import threading
import time
from urllib.parse import unquote, urlsplit

import pytest
from conftest import TODO_APP, load_todo_module

COLLECTOR_OTLP_HTTP = "http://platform-signoz-otel-collector:4318"


PG_PASSWORD = secrets.token_urlsafe(24)


REDIS_PW = secrets.token_urlsafe(16)


WRONG_PW = secrets.token_urlsafe(16)


S3_ACCESS_KEY = "AK" + secrets.token_hex(8).upper()


S3_SECRET_KEY = secrets.token_urlsafe(24)


@pytest.fixture()
def app():
    return load_todo_module("todo_app_depth")


class FakeRedis:
    """An in-memory Redis speaking RESP, with `requirepass` semantics."""

    def __init__(self, password=None, *, ping_reply=None, get_reply=None):
        self.password = password
        self.ping_reply = ping_reply
        self.get_reply = get_reply
        self.store: dict[str, bytes] = {}
        self.commands: list[list[str]] = []
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._server.close()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    @staticmethod
    def _read_command(rf) -> list[str] | None:
        header = rf.readline()
        if not header:
            return None
        assert header.startswith(b"*"), header
        parts = []
        for _ in range(int(header[1:])):
            length = int(rf.readline()[1:])
            parts.append(rf.read(length + 2)[:-2].decode())
        return parts

    def _handle(self, conn: socket.socket) -> None:
        authed = self.password is None
        with conn, conn.makefile("rb") as rf:
            while True:
                command = self._read_command(rf)
                if command is None:
                    return
                self.commands.append(command)
                name = command[0].upper()
                if name == "AUTH":
                    if self.password is None:
                        conn.sendall(b"-ERR AUTH called without any password configured\r\n")
                    elif command[-1] == self.password:
                        authed = True
                        conn.sendall(b"+OK\r\n")
                    else:
                        conn.sendall(b"-WRONGPASS invalid username-password pair\r\n")
                elif not authed:
                    conn.sendall(b"-NOAUTH Authentication required.\r\n")
                elif name == "PING":
                    conn.sendall(self.ping_reply or b"+PONG\r\n")
                elif name == "SETEX":
                    self.store[command[1]] = command[3].encode()
                    conn.sendall(b"+OK\r\n")
                elif name == "GET":
                    value = self.store.get(command[1])
                    if self.get_reply is not None:
                        conn.sendall(self.get_reply)
                    elif value is None:
                        conn.sendall(b"$-1\r\n")
                    else:
                        conn.sendall(b"$%d\r\n%s\r\n" % (len(value), value))
                elif name == "DEL":
                    conn.sendall(b":%d\r\n" % (1 if self.store.pop(command[1], None) else 0))
                else:
                    conn.sendall(b"-ERR unknown command\r\n")


class FakePostgresRefusal:
    """Answers a login with a Postgres ErrorResponse (`E`), as a full server does."""

    def __init__(self, message: str = "sorry, too many clients already"):
        body = b"SFATAL\0VFATAL\0C53300\0M" + message.encode() + b"\0\0"
        self.response = b"E" + struct.pack("!I", 4 + len(body)) + body
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self.port = self._server.getsockname()[1]
        self.startup_params: bytes = b""
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._server.close()

    @staticmethod
    def _recv_exact(conn: socket.socket, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                raise ConnectionError("client closed")
            data += chunk
        return data

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            try:
                with conn:
                    while True:
                        (length,) = struct.unpack("!I", self._recv_exact(conn, 4))
                        payload = self._recv_exact(conn, length - 4)
                        (code,) = struct.unpack("!I", payload[:4])
                        if code in (80877103, 80877104):  # SSLRequest, GSSENCRequest
                            conn.sendall(b"N")
                            continue
                        self.startup_params = payload[4:]
                        conn.sendall(self.response)
                        break
            except (ConnectionError, OSError):
                continue


class FakeS3:
    """A path-style S3 endpoint storing objects in memory."""

    def __init__(self, *, deny: tuple[str, ...] = (), corrupt_get: bool = False):
        self.objects: dict[str, bytes] = {}
        self.requests: list[tuple[str, str, str]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def _reply(self, status: int, body: bytes = b"") -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                auth = self.headers.get("Authorization", "")
                access_key = re.search(r"Credential=([^/]+)/", auth)
                outer.requests.append(
                    (self.command, self.path, access_key.group(1) if access_key else "")
                )
                if self.command in deny:
                    self._reply(403, b"<Error><Code>AccessDenied</Code></Error>")
                elif self.command == "PUT":
                    outer.objects[self.path] = body
                    self._reply(200)
                elif self.command == "GET":
                    if self.path not in outer.objects:
                        self._reply(404, b"<Error><Code>NoSuchKey</Code></Error>")
                    elif corrupt_get:
                        self._reply(200, b"not-what-was-written")
                    else:
                        self._reply(200, outer.objects[self.path])
                elif self.command == "DELETE":
                    outer.objects.pop(self.path, None)
                    self._reply(204)
                else:
                    self._reply(405)

            do_PUT = do_GET = do_DELETE = do_HEAD = _handle

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _FakeConnection:
    def __init__(self, readback: str = "canary-write-ok"):
        self.statements: list[str] = []
        self._readback = readback
        self._last_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        sets_mode = "transaction_read_only" in sql or "READ WRITE" in sql.upper()
        if sets_mode and self.statements and sql.upper().startswith(("SET", "BEGIN")):
            raise RuntimeError(
                "ActiveSqlTransaction: transaction read-write mode must be set before any query"
            )
        self.statements.append(sql)
        self._last_sql = sql
        return self

    def fetchone(self):
        if self._last_sql == "SELECT 1":
            return (1,)
        return (self._readback,)

    def close(self):
        return None


def _s3_args(server: FakeS3) -> dict[str, object]:
    return {
        "endpoint_url": server.url,
        "bucket": "platform-canary",
        "access_key": S3_ACCESS_KEY,
        "secret_key": S3_SECRET_KEY,
        "timeout": 3.0,
    }


def _env(monkeypatch, **values: str) -> None:
    for key in (
        "REDIS_PASSWORD",
        "CANARY_POSTGRES_USER",
        "CANARY_POSTGRES_PASSWORD",
        "S3_ENDPOINT",
        "S3_BUCKET",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "CANARY_S3_ENDPOINT_URL",
        "CANARY_S3_BUCKET",
        "CANARY_S3_ACCESS_KEY",
        "CANARY_S3_SECRET_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


OK_RESULT = {"status": "pass", "latency_ms": 1.0, "detail": "ok"}


def _patch_probes(app, monkeypatch, **overrides) -> None:
    """Every probe passes except the ones named: redis, postgres, s3, and http by host."""
    redis = overrides.get("redis", OK_RESULT)
    postgres = overrides.get("postgres", OK_RESULT)
    s3 = overrides.get("s3", OK_RESULT)
    monkeypatch.setattr(app, "_probe_redis", lambda *a, **k: dict(redis))
    monkeypatch.setattr(app, "_probe_postgres", lambda *a, **k: dict(postgres))
    monkeypatch.setattr(app, "_probe_s3", lambda **k: dict(s3))

    def http(url, **kwargs):
        for name in ("signoz", "openpanel", "authentik"):
            if name in url and name in overrides:
                return dict(overrides[name])
        return dict(OK_RESULT)

    monkeypatch.setattr(app, "_probe_http", http)


def _get_status(app) -> tuple[int, str]:
    """The raw status response as the probe runner receives it: (HTTP code, body)."""
    from starlette.testclient import TestClient

    client = TestClient(app.fastapi_app, raise_server_exceptions=False)
    response = client.get("/api/canary/status")
    return response.status_code, response.text


def _served_paths() -> set[str]:
    """Every exact path the service serves, read from the app source."""
    source = TODO_APP.read_text(encoding="utf-8")
    return set(re.findall(r'fastapi_app\.(?:api_route|get|post|put|delete)\(\s*"(/[^"]*)"', source))


def _issued_telemetry_env(environment: str) -> dict[str, str]:
    """The identity the infra2 deploy issues (infra2 common/core/service_identity.py)."""
    sha = "a" * 40
    attributes = {
        "deployment.environment": environment,
        "deployment.environment.name": environment,
        "infra.component": "todo",
        "infra.iac.ref": sha,
        "infra.service.id": "platform/todo",
        "service.name": "platform-todo",
        "service.version": sha,
    }
    return {
        "ENV": environment,
        "OTEL_EXPORTER_OTLP_ENDPOINT": COLLECTOR_OTLP_HTTP,
        "OTEL_SERVICE_NAME": "platform-todo",
        "OTEL_RESOURCE_ATTRIBUTES": ",".join(f"{k}={v}" for k, v in sorted(attributes.items())),
    }


def _load_todo_app():
    """Load the app as the container does: the telemetry starts while the module loads."""
    return load_todo_module("todo_app_telemetry")


@pytest.fixture()
def telemetry_calls(monkeypatch):
    """Replace the SDK exporter bootstrap so the test needs no collector."""
    import infra2_sdk.runtime.otel as sdk_otel

    calls: list[dict] = []

    class _Providers:
        def shutdown(self):
            calls.append({"shutdown": True})

    def fake_configure(settings, **kwargs):
        calls.append({"settings": settings, **kwargs})
        return _Providers()

    monkeypatch.setattr(sdk_otel, "configure_telemetry", fake_configure)
    return calls


class _FakeSpan:
    def __init__(self, name, attributes):
        self.name = name
        self.attributes = dict(attributes or {})
        self.parent = None

    def set_attribute(self, key, value):
        self.attributes[key] = value


class _FakeTracer:
    """Tracks the current span in a ContextVar, as OpenTelemetry's context does."""

    def __init__(self):
        self.spans: list[_FakeSpan] = []
        self._current: contextvars.ContextVar[_FakeSpan | None] = contextvars.ContextVar(
            "fake_current_span", default=None
        )

    def start_as_current_span(self, name, attributes=None, **kwargs):
        @contextlib.contextmanager
        def manager():
            span = _FakeSpan(name, attributes)
            parent = self._current.get()
            span.parent = parent.name if parent else None
            self.spans.append(span)
            token = self._current.set(span)
            try:
                yield span
            finally:
                self._current.reset(token)

        return manager()


class _Run:
    def __init__(self, ok=True, stdout="", stderr=""):
        self.ok, self.failed = ok, not ok
        self.stdout, self.stderr = stdout, stderr


class _Host:
    def __init__(self, ok=True, stderr=""):
        self.commands: list[str] = []
        self.stdin: list[str] = []
        self._ok, self._stderr = ok, stderr

    def run(self, command, in_stream=None, hide=True, warn=True, **kwargs):
        self.commands.append(command)
        self.stdin.append(in_stream.read() if in_stream is not None else "")
        return _Run(self._ok, stderr=self._stderr)


class _Store:
    def __init__(self, values):
        self.values = values

    def get(self, key):
        return self.values.get(key)


def _stub_role_env(monkeypatch, deployer, store_values) -> None:
    monkeypatch.setattr(
        deployer,
        "env",
        classmethod(
            lambda cls: {
                "ENV": "staging",
                "ENV_SUFFIX": "-staging",
                "VPS_HOST": "vps.example",
            }
        ),
    )
    monkeypatch.setattr(
        deployer,
        "secrets_backend",
        classmethod(lambda cls, env=None: _Store(store_values)),
    )


def _now(offset: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))


def test_redis_probe_reports_unconfigured_without_a_password(app) -> None:
    """The production canary ran with an empty REDIS_PASSWORD and passed. No password
    still keeps the check red, but as `unconfigured`: a configuration gap is not a
    write-path failure, and an operator must be able to tell the two apart."""
    redis = FakeRedis(password=REDIS_PW)
    try:
        result = app._probe_redis("127.0.0.1", redis.port, password=None, timeout=1.0)
    finally:
        redis.close()
    assert result["status"] == "unconfigured"
    assert "REDIS_PASSWORD" in result["detail"]
    assert redis.store == {}
    assert redis.commands == [], "an unconfigured check sends nothing"


@pytest.mark.parametrize(
    ("redis_kwargs", "expected_detail"),
    [
        ({"ping_reply": b"-NOAUTH Authentication required.\r\n"}, "NOAUTH"),
        ({"get_reply": b"$5\r\nstale\r\n"}, "GET returned"),
    ],
)
def test_redis_probe_fails_on_unexpected_reply(app, redis_kwargs, expected_detail) -> None:
    """A server returning -NOAUTH after AUTH or stale GET data must fail."""
    redis = FakeRedis(password=REDIS_PW, **redis_kwargs)
    try:
        result = app._probe_redis("127.0.0.1", redis.port, password=REDIS_PW, timeout=1.0)
    finally:
        redis.close()
    assert result["status"] == "fail"
    assert expected_detail in result["detail"]


def test_redis_probe_fails_on_rejected_credentials_without_leaking_them(app) -> None:
    redis = FakeRedis(password=REDIS_PW)
    try:
        result = app._probe_redis("127.0.0.1", redis.port, password=WRONG_PW, timeout=1.0)
    finally:
        redis.close()
    assert result["status"] == "fail"
    assert "AUTH failed" in result["detail"]
    assert WRONG_PW not in result["detail"]


def test_redis_probe_writes_reads_and_deletes_a_random_value(app) -> None:
    redis = FakeRedis(password=REDIS_PW)
    try:
        result = app._probe_redis("127.0.0.1", redis.port, password=REDIS_PW, timeout=1.0)
    finally:
        redis.close()
    assert result["status"] == "pass", result
    names = [command[0].upper() for command in redis.commands]
    assert names == ["AUTH", "PING", "SETEX", "GET", "DEL"]
    setex = next(c for c in redis.commands if c[0].upper() == "SETEX")
    get = next(c for c in redis.commands if c[0].upper() == "GET")
    assert get[1] == setex[1] and setex[1].startswith("canary:")
    assert int(setex[2]) > 0, "the key needs a TTL so a crashed probe leaves nothing behind"
    assert redis.store == {}, "the probe key must be deleted"


def test_redis_probe_values_differ_between_runs(app) -> None:
    """A constant value lets a stale key from the previous run pass a broken SETEX."""
    redis = FakeRedis(password=REDIS_PW)
    try:
        for _ in range(2):
            app._probe_redis("127.0.0.1", redis.port, password=REDIS_PW, timeout=1.0)
    finally:
        redis.close()
    values = [c[3] for c in redis.commands if c[0].upper() == "SETEX"]
    assert len(values) == 2 and values[0] != values[1]


@pytest.mark.parametrize(("user", "password"), [("", ""), ("canary_ro", ""), ("", "x" * 20)])
def test_postgres_probe_reports_unconfigured_without_credentials(app, user, password) -> None:
    result = app._probe_postgres("127.0.0.1", 1, user=user, password=password, timeout=1.0)
    assert result["status"] == "unconfigured"
    assert "CANARY_POSTGRES" in result["detail"]


def test_postgres_probe_fails_when_the_server_answers_an_error_response(app) -> None:
    """Before #991 any `E` reply passed. A server that refuses the login is not healthy."""
    server = FakePostgresRefusal("sorry, too many clients already")
    try:
        result = app._probe_postgres(
            "127.0.0.1",
            server.port,
            user="canary_ro",
            password=PG_PASSWORD,
            timeout=2.0,
        )
    finally:
        server.close()
    assert result["status"] == "fail", result
    assert "too many clients" in result["detail"]
    assert PG_PASSWORD not in result["detail"]
    assert b"canary_ro" in server.startup_params, "the probe must log in as the canary role"


def test_postgres_probe_logs_in_as_the_canary_role_and_selects_one(app) -> None:
    seen: dict[str, object] = {}
    connection = _FakeConnection()

    def connector(dsn, **kwargs):
        seen["dsn"] = dsn
        seen["kwargs"] = kwargs
        return connection

    password = "p@ss:w/rd#" + secrets.token_hex(4) + "?x"
    result = app._probe_postgres(
        "platform-postgres-staging",
        5432,
        user="canary_ro",
        password=password,
        dbname="postgres",
        timeout=2.0,
        connector=connector,
    )
    assert result["status"] == "pass", result
    parts = urlsplit(seen["dsn"])
    assert (parts.hostname, parts.port, parts.path) == (
        "platform-postgres-staging",
        5432,
        "/postgres",
    )
    assert parts.username == "canary_ro"
    assert unquote(parts.password) == password
    assert connection.statements == [
        "SET LOCAL transaction_read_only = off",
        "SELECT 1",
        "CREATE TEMP TABLE IF NOT EXISTS canary_todo_probe (id TEXT PRIMARY KEY, title TEXT NOT NULL)",
        "INSERT INTO canary_todo_probe (id, title) VALUES ('probe-1', 'canary-write-ok') ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title",
        "SELECT title FROM canary_todo_probe WHERE id = 'probe-1'",
        "DELETE FROM canary_todo_probe WHERE id = 'probe-1'",
    ]
    assert "canary_ro" in result["detail"]


def test_postgres_probe_fails_when_the_connector_raises(app) -> None:
    def connector(dsn, **kwargs):
        raise ConnectionError(f"could not connect using {dsn}")

    result = app._probe_postgres(
        "h", 5432, user="canary_ro", password=PG_PASSWORD, connector=connector
    )
    assert result["status"] == "fail"
    assert PG_PASSWORD not in result["detail"], "the SDK redacts the DSN from errors"


def test_s3_probe_puts_gets_and_deletes_one_object_under_the_canary_prefix(app) -> None:
    server = FakeS3()
    try:
        result = app._probe_s3(**_s3_args(server))
        methods = [method for method, _path, _key in server.requests]
        paths = {path for _method, path, _key in server.requests}
        keys = {key for _method, _path, key in server.requests}
        leftover = dict(server.objects)
    finally:
        server.close()
    assert result["status"] == "pass", result
    assert methods == ["PUT", "GET", "DELETE"]
    assert len(paths) == 1
    assert next(iter(paths)).startswith("/platform-canary/canary/")
    assert keys == {S3_ACCESS_KEY}, "the request must be signed with the canary key"
    assert leftover == {}, "the canary object must be deleted"


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_s3_probe_fails_when_object_op_is_denied(app, method: str) -> None:
    """Denied PUT or DELETE must fail the S3 probe check."""
    server = FakeS3(deny=(method,))
    try:
        result = app._probe_s3(**_s3_args(server))
    finally:
        server.close()
    assert result["status"] == "fail"
    assert "AccessDenied" in result["detail"]


def test_s3_probe_fails_on_corrupt_read_and_still_cleans_up(app) -> None:
    server = FakeS3(corrupt_get=True)
    try:
        result = app._probe_s3(**_s3_args(server))
        methods = [method for method, _path, _key in server.requests]
        leftover = dict(server.objects)
    finally:
        server.close()
    assert result["status"] == "fail"
    assert "differs" in result["detail"]
    assert methods[-1] == "DELETE" and leftover == {}


@pytest.mark.parametrize("missing", ["bucket", "access_key", "secret_key"])
def test_s3_probe_reports_unconfigured_when_credentials_are_missing(app, missing) -> None:
    server = FakeS3()
    try:
        args = _s3_args(server)
        args[missing] = ""
        result = app._probe_s3(**args)
        requests = list(server.requests)
    finally:
        server.close()
    assert result["status"] == "unconfigured"
    assert "S3_" in result["detail"]
    assert requests == [], "no request may be sent without credentials"


def test_s3_probe_uses_a_new_key_every_run_and_deletes_only_its_own(app) -> None:
    """A fixed key lets one run delete the object another run is about to read, and lets
    a stale object from a crashed run pass the next read."""
    server = FakeS3()
    other = "/platform-canary/canary/written-by-someone-else"
    server.objects[other] = b"keep me"
    try:
        for _ in range(2):
            assert app._probe_s3(**_s3_args(server))["status"] == "pass"
        requests = list(server.requests)
        leftover = dict(server.objects)
    finally:
        server.close()
    puts = [path for method, path, _key in requests if method == "PUT"]
    deletes = [path for method, path, _key in requests if method == "DELETE"]
    assert len(puts) == 2 and puts[0] != puts[1]
    assert deletes == puts, "each run deletes its own key and no other"
    assert leftover == {other: b"keep me"}


def test_s3_probe_runs_in_parallel_do_not_share_a_key(app) -> None:
    server = FakeS3()
    results: list[dict] = []
    try:
        threads = [
            threading.Thread(target=lambda: results.append(app._probe_s3(**_s3_args(server))))
            for _ in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        puts = [path for method, path, _key in server.requests if method == "PUT"]
        leftover = dict(server.objects)
    finally:
        server.close()
    assert [r["status"] for r in results] == ["pass"] * 6, results
    assert len(set(puts)) == 6
    assert leftover == {}


def test_run_all_checks_hands_each_probe_its_credentials(app, monkeypatch) -> None:
    _env(
        monkeypatch,
        ENV_SUFFIX="-staging",
        REDIS_PASSWORD=REDIS_PW,
        CANARY_POSTGRES_USER="canary_ro",
        CANARY_POSTGRES_PASSWORD=PG_PASSWORD,
        S3_ENDPOINT="http://platform-s3-staging:9000",
        S3_BUCKET="platform-canary",
        S3_ACCESS_KEY=S3_ACCESS_KEY,
        S3_SECRET_KEY=S3_SECRET_KEY,
    )
    calls: dict[str, dict] = {}

    def record(name):
        def probe(*args, **kwargs):
            calls[name] = {"args": args, "kwargs": kwargs}
            return {"status": "pass", "latency_ms": 1.0, "detail": name}

        return probe

    monkeypatch.setattr(app, "_probe_redis", record("redis"))
    monkeypatch.setattr(app, "_probe_postgres", record("postgres"))
    monkeypatch.setattr(app, "_probe_s3", record("s3"))
    monkeypatch.setattr(app, "_probe_http", record("http"))

    report = app.run_all_checks()

    assert report["ok"] is True
    assert set(report["checks"]) == {
        "postgres",
        "redis",
        "s3",
        "signoz",
        "openpanel",
        "authentik",
    }
    assert "minio" not in report["checks"]
    assert calls["redis"]["args"][0] == "platform-redis-staging"
    assert calls["redis"]["kwargs"]["password"] == REDIS_PW
    assert calls["postgres"]["args"][0] == "platform-postgres-staging"
    assert calls["postgres"]["kwargs"]["user"] == "canary_ro"
    assert calls["postgres"]["kwargs"]["password"] == PG_PASSWORD
    assert calls["s3"]["kwargs"] == {
        "endpoint_url": "http://platform-s3-staging:9000",
        "bucket": "platform-canary",
        "access_key": S3_ACCESS_KEY,
        "secret_key": S3_SECRET_KEY,
    }


def test_retired_canary_s3_names_do_not_configure_the_s3_probe(app, monkeypatch) -> None:
    """CANARY_S3_* were replaced by the shared S3_* names. Setting only the old names
    must leave the probe without an endpoint override, bucket and credentials."""
    _env(
        monkeypatch,
        ENV_SUFFIX="-staging",
        CANARY_S3_ENDPOINT_URL="http://old-host:9000",
        CANARY_S3_BUCKET="old-bucket",
        CANARY_S3_ACCESS_KEY="old-access",
        CANARY_S3_SECRET_KEY="old-secret",
    )
    seen: dict = {}

    def s3(**kwargs):
        seen.update(kwargs)
        return {"status": "pass", "latency_ms": 1.0, "detail": "s3"}

    monkeypatch.setattr(app, "_probe_redis", lambda *a, **k: dict(OK_RESULT))
    monkeypatch.setattr(app, "_probe_postgres", lambda *a, **k: dict(OK_RESULT))
    monkeypatch.setattr(app, "_probe_s3", s3)
    monkeypatch.setattr(app, "_probe_http", lambda *a, **k: dict(OK_RESULT))

    app.run_all_checks()

    assert seen == {
        "endpoint_url": "http://platform-s3-staging:9000",
        "bucket": "",
        "access_key": "",
        "secret_key": "",
    }


def test_a_failed_write_path_turns_the_document_red_as_503(app, monkeypatch) -> None:
    """A forced failure turns the status document red, which is what the probe reads."""
    _env(monkeypatch)
    failed = {"status": "fail", "latency_ms": 2.0, "detail": "AccessDenied"}
    _patch_probes(app, monkeypatch, s3=failed)

    code, raw = _get_status(app)
    body = json.loads(raw)

    assert code == 503
    assert body["ok"] is False
    assert body["failed"] == ["s3"] and body["unconfigured"] == []
    assert body["checks"]["s3"]["detail"] == "AccessDenied"
    assert "minio" not in body["checks"]


def test_an_unconfigured_write_path_is_red_but_never_a_failure(app, monkeypatch) -> None:
    """A missing credential must keep the probe red (a gap is not green) and must not be
    reported as a failed write path (#991 audit: a false P1 page)."""
    _env(monkeypatch)
    gap = {
        "status": "unconfigured",
        "latency_ms": 0.1,
        "detail": "S3_ACCESS_KEY, S3_SECRET_KEY not configured",
    }
    _patch_probes(app, monkeypatch, s3=gap)

    code, raw = _get_status(app)
    body = json.loads(raw)

    assert code == 503, "an unconfigured check keeps the probe red"
    assert body["ok"] is False
    assert body["unconfigured"] == ["s3"]
    assert body["failed"] == [], "a configuration gap is not a write-path failure"
    assert body["checks"]["s3"]["status"] == "unconfigured"
    assert body["checks"]["redis"]["status"] == "pass"


def test_failed_and_unconfigured_checks_are_listed_apart(app, monkeypatch) -> None:
    _env(monkeypatch)
    _patch_probes(
        app,
        monkeypatch,
        redis={"status": "fail", "latency_ms": 1.0, "detail": "AUTH failed"},
        s3={"status": "unconfigured", "latency_ms": 0.1, "detail": "gap"},
    )
    body = app.run_all_checks()
    assert body["failed"] == ["redis"] and body["unconfigured"] == ["s3"]
    assert body["ok"] is False


def test_every_write_path_gates_the_document(app, monkeypatch) -> None:
    _env(monkeypatch)
    down = {"status": "fail", "latency_ms": 1.0, "detail": "down"}
    for name in ("postgres", "redis", "s3"):
        _patch_probes(app, monkeypatch, **{name: down})
        body = app.run_all_checks()
        assert body["ok"] is False and body["failed"] == [name], name


def test_run_all_checks_bounds_a_hung_probe(app, monkeypatch) -> None:
    """The probe runner waits a fixed time. One hung dependency must not hold the whole
    status document past it, and it must show as a failed check."""
    _env(monkeypatch)
    monkeypatch.setattr(app, "CHECK_DEADLINE_SECONDS", 0.3)
    monkeypatch.setattr(app, "_probe_redis", lambda *a, **k: dict(OK_RESULT))
    monkeypatch.setattr(app, "_probe_postgres", lambda *a, **k: dict(OK_RESULT))
    monkeypatch.setattr(app, "_probe_s3", lambda **k: dict(OK_RESULT))

    def hung_http(url, **kwargs):
        if "openpanel" in url:
            time.sleep(1.5)
        return dict(OK_RESULT)

    monkeypatch.setattr(app, "_probe_http", hung_http)
    monkeypatch.setenv("OPENPANEL_API_URL", "http://platform-openpanel-api:3000/healthcheck")

    started = time.monotonic()
    report = app.run_all_checks()
    elapsed = time.monotonic() - started

    assert elapsed < 1.2, f"the status document waited for the hung probe ({elapsed:.2f}s)"
    assert report["checks"]["openpanel"]["status"] == "fail"
    assert "did not finish" in report["checks"]["openpanel"]["detail"]
    assert report["checks"]["redis"]["status"] == "pass"
    assert report["ok"] is True, "OpenPanel is informational and does not gate"


def test_a_hung_write_path_fails_the_document_within_the_deadline(app, monkeypatch) -> None:
    _env(monkeypatch)
    monkeypatch.setattr(app, "CHECK_DEADLINE_SECONDS", 0.3)
    _patch_probes(app, monkeypatch)

    def hung_redis(*args, **kwargs):
        time.sleep(1.5)
        return dict(OK_RESULT)

    monkeypatch.setattr(app, "_probe_redis", hung_redis)
    started = time.monotonic()
    report = app.run_all_checks()
    assert time.monotonic() - started < 1.2
    assert report["ok"] is False and report["failed"] == ["redis"]


def test_concurrent_requests_share_one_run(app, monkeypatch) -> None:
    """Each run does real writes and opens a Postgres connection. The canary role holds 5
    connections, so five parallel page loads must not become five runs."""
    runs: list[int] = []
    release = threading.Event()

    def slow_run():
        runs.append(1)
        release.wait(timeout=5)
        return {"ok": True, "timestamp": "t", "checks": {}}

    monkeypatch.setattr(app, "run_all_checks", slow_run)
    answers: list[dict] = []
    threads = [
        threading.Thread(target=lambda: answers.append(app.cached_canary_status()))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 2
    while not runs and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert len(runs) == 1, f"{len(runs)} runs for 8 concurrent requests"
    assert len(answers) == 8 and all(a["ok"] is True for a in answers)


def test_requests_inside_the_ttl_reuse_the_result_and_say_how_old_it_is(app, monkeypatch) -> None:
    runs: list[int] = []

    def run():
        runs.append(1)
        return {"ok": True, "timestamp": "t", "checks": {}}

    monkeypatch.setattr(app, "run_all_checks", run)
    first = app.cached_canary_status()
    second = app.cached_canary_status()
    assert len(runs) == 1
    assert first["age_seconds"] == 0.0
    assert 0.0 <= second["age_seconds"] < app.STATUS_CACHE_TTL_SECONDS, (
        "a reused result must state its age, never pass as fresh"
    )


def test_a_result_older_than_the_ttl_is_run_again(app, monkeypatch) -> None:
    runs: list[int] = []
    monkeypatch.setattr(app, "STATUS_CACHE_TTL_SECONDS", 0.05)
    monkeypatch.setattr(
        app,
        "run_all_checks",
        lambda: runs.append(1) or {"ok": True, "timestamp": "t", "checks": {}},
    )
    app.cached_canary_status()
    time.sleep(0.12)
    app.cached_canary_status()
    assert len(runs) == 2


def test_parallel_http_requests_run_the_probes_once(app, monkeypatch) -> None:

    _env(monkeypatch)
    calls: list[int] = []

    def counting_postgres(*args, **kwargs):
        calls.append(1)
        time.sleep(0.2)
        return dict(OK_RESULT)

    _patch_probes(app, monkeypatch)
    monkeypatch.setattr(app, "_probe_postgres", counting_postgres)

    codes: list[int] = []

    def fetch():
        from starlette.testclient import TestClient

        client = TestClient(app.fastapi_app)
        response = client.get("/api/canary/status")
        codes.append(response.status_code)

    threads = [threading.Thread(target=fetch) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert codes == [200] * 6
    assert len(calls) == 1, "six page loads must open one Postgres connection"


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_startup_configures_telemetry_once_from_the_issued_identity(
    monkeypatch, telemetry_calls, environment
) -> None:
    for key, value in _issued_telemetry_env(environment).items():
        monkeypatch.setenv(key, value)

    module = _load_todo_app()

    (call,) = telemetry_calls
    settings = call["settings"]
    assert call["set_global"] is True
    assert settings.service_name == "platform-todo"
    assert settings.endpoint == COLLECTOR_OTLP_HTTP
    assert settings.deployment_environment == environment
    assert settings.resource_attributes["infra.service.id"] == "platform/todo"
    assert settings.enabled is True
    assert module.fastapi_app.state.telemetry_providers is not None


def test_main_starts_no_second_telemetry_bootstrap(monkeypatch, telemetry_calls) -> None:
    """`main()` once called a second bootstrap, which logged a RuntimeWarning at every start."""
    import signal
    import types

    for key, value in _issued_telemetry_env("staging").items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CANARY_POSTGRES_PASSWORD", PG_PASSWORD)
    monkeypatch.setenv("TODO_POSTGRES_PASSWORD", PG_PASSWORD)
    # main() installs a SIGTERM handler and starts the server: do neither in the test run
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    server = types.ModuleType("uvicorn")
    server.run = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "uvicorn", server)
    module = _load_todo_app()
    assert len(telemetry_calls) == 1

    module.main()

    assert len(telemetry_calls) == 1, "main() must not start OpenTelemetry again"
    assert not hasattr(module, "configure_service_telemetry")


def test_main_validates_startup_environment_against_manifest_and_fails_closed(
    monkeypatch, telemetry_calls
) -> None:
    """`main()` calls validate_startup_environment() and fails closed with RuntimeError
    when CANARY_POSTGRES_PASSWORD (required by env.manifest.json) is missing."""
    for key, value in _issued_telemetry_env("staging").items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("CANARY_POSTGRES_PASSWORD", raising=False)
    module = _load_todo_app()

    with pytest.raises(RuntimeError, match="CANARY_POSTGRES_PASSWORD"):
        module.main()


def test_the_providers_shut_down_with_the_app(monkeypatch, telemetry_calls) -> None:
    from starlette.testclient import TestClient

    for key, value in _issued_telemetry_env("staging").items():
        monkeypatch.setenv(key, value)
    module = _load_todo_app()
    assert {"shutdown": True} not in telemetry_calls

    with TestClient(module.fastapi_app):
        assert {"shutdown": True} not in telemetry_calls

    assert telemetry_calls.count({"shutdown": True}) == 1


def test_startup_skips_telemetry_without_an_endpoint(telemetry_calls) -> None:
    module = _load_todo_app()
    assert telemetry_calls == []
    assert not hasattr(module.fastapi_app.state, "telemetry_providers")


def test_startup_fails_fast_when_the_endpoint_comes_without_an_identity(
    monkeypatch, telemetry_calls
) -> None:
    """The endpoint turns export on. `init_service` would name the data `platform-todo` on
    its own, so it runs with `require_issued_identity=True` and refuses a deploy that issued
    no identity. The error names every missing key and no value."""
    monkeypatch.setenv("ENV", "staging")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", COLLECTOR_OTLP_HTTP)
    with pytest.raises(ValueError, match="OTEL_SERVICE_NAME") as failure:
        _load_todo_app()
    for key in (
        "deployment.environment.name",
        "infra.service.id",
        "service.version",
        "infra.iac.ref",
    ):
        assert key in str(failure.value)
    assert telemetry_calls == []


def test_startup_fails_fast_when_the_issued_environment_is_unknown(
    monkeypatch, telemetry_calls
) -> None:
    """The telemetry tier is the issued `deployment.environment.name`, not `ENV`."""
    env = _issued_telemetry_env("staging")
    env["OTEL_RESOURCE_ATTRIBUTES"] = env["OTEL_RESOURCE_ATTRIBUTES"].replace(
        "deployment.environment.name=staging", "deployment.environment.name=bogus-tier"
    )
    assert "bogus-tier" in env["OTEL_RESOURCE_ATTRIBUTES"]
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=r"deployment\.environment\.name"):
        _load_todo_app()
    assert telemetry_calls == []


def test_init_service_gets_the_strict_flag_and_the_readiness_timeout(
    monkeypatch,
) -> None:
    import infra2_sdk.fastapi as sdk_fastapi

    seen: list[dict] = []
    original = sdk_fastapi.init_service

    def recording(app, name, **kwargs):
        seen.append({"name": name, **kwargs})
        return original(app, name, **kwargs)

    monkeypatch.setattr(sdk_fastapi, "init_service", recording)

    module = _load_todo_app()

    (call,) = seen
    assert call["name"] == "platform-todo"
    assert call["enable_otel"] is True
    assert call["strict_otel"] is True
    assert call["check_timeout_seconds"] == module.READINESS_TIMEOUT_SECONDS
    assert "log_level" not in call, "init_service owns the logging baseline"


def test_the_readiness_timeout_is_above_the_check_deadline(app) -> None:
    """A cold /readyz call waits for one full run. A run ends at the check deadline."""
    assert app.READINESS_TIMEOUT_SECONDS > app.CHECK_DEADLINE_SECONDS


def test_a_cold_readiness_call_ends_with_the_check_deadline(app, monkeypatch) -> None:
    """Every check hangs. The call still returns at the deadline with the reasons, which
    is why the timeout only has to exceed the deadline."""
    hang = threading.Event()

    def hung_probe(*args, **kwargs):
        hang.wait(timeout=5)
        return {"status": "pass", "latency_ms": 0.0, "detail": "late"}

    for probe in ("_probe_postgres", "_probe_redis", "_probe_http", "_probe_s3"):
        monkeypatch.setattr(app, probe, hung_probe)
    monkeypatch.setattr(app, "CHECK_DEADLINE_SECONDS", 0.3)
    monkeypatch.setattr(app, "_STATUS_CACHE", None)

    started = time.perf_counter()
    ok, detail = app._canary_readiness_checker()
    elapsed = time.perf_counter() - started
    hang.set()

    assert ok is False and detail == "failed=postgres,redis,s3"
    assert 0.3 <= elapsed < 0.3 + 1.0, elapsed


def test_canary_run_emits_a_root_span_and_one_span_per_check(app, monkeypatch) -> None:
    import opentelemetry.trace as otel_trace

    _env(monkeypatch)
    tracer = _FakeTracer()
    requested: list[str] = []

    def get_tracer(name, *args, **kwargs):
        requested.append(name)
        return tracer

    monkeypatch.setattr(otel_trace, "get_tracer", get_tracer)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", COLLECTOR_OTLP_HTTP)
    ok = {"status": "pass", "latency_ms": 1.0, "detail": "ok"}
    monkeypatch.setattr(app, "_probe_redis", lambda *a, **k: dict(ok))
    monkeypatch.setattr(app, "_probe_postgres", lambda *a, **k: dict(ok))
    monkeypatch.setattr(app, "_probe_http", lambda *a, **k: dict(ok))
    monkeypatch.setattr(
        app,
        "_probe_s3",
        lambda **k: {"status": "fail", "latency_ms": 3.0, "detail": "denied"},
    )

    app.run_all_checks()

    assert set(requested) == {"platform-todo"}
    by_name = {span.name: span for span in tracer.spans}
    assert "canary.run" in by_name
    checks = {n for n in by_name if n.startswith("canary.check.")}
    assert checks == {
        "canary.check.postgres",
        "canary.check.redis",
        "canary.check.s3",
        "canary.check.signoz",
        "canary.check.openpanel",
        "canary.check.authentik",
    }
    assert all(by_name[name].parent == "canary.run" for name in checks), (
        "check spans must nest under the run span across worker threads"
    )
    s3 = by_name["canary.check.s3"]
    assert s3.attributes["canary.status"] == "fail"
    assert s3.attributes["canary.detail"] == "denied"
    assert by_name["canary.run"].attributes["canary.ok"] is False


def test_canary_run_works_without_a_tracer(app, monkeypatch) -> None:
    """No OTLP endpoint means no span and no tracer lookup."""
    import opentelemetry.trace as otel_trace

    _env(monkeypatch)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    def no_tracer(*args, **kwargs):
        raise AssertionError("a tracer was requested without an OTLP endpoint")

    monkeypatch.setattr(otel_trace, "get_tracer", no_tracer)
    ok = {"status": "pass", "latency_ms": 1.0, "detail": "ok"}
    monkeypatch.setattr(app, "_probe_redis", lambda *a, **k: dict(ok))
    monkeypatch.setattr(app, "_probe_postgres", lambda *a, **k: dict(ok))
    monkeypatch.setattr(app, "_probe_http", lambda *a, **k: dict(ok))
    monkeypatch.setattr(app, "_probe_s3", lambda **k: dict(ok))
    assert app.run_all_checks()["ok"] is True


def test_traced_check_emits_duration_histogram_and_outcomes_counter_with_otlp(
    app, monkeypatch
) -> None:
    """#1503: when OTEL_EXPORTER_OTLP_ENDPOINT is set, _traced_check records both
    canary.probe.duration_ms and canary.probe.outcomes via opentelemetry.metrics."""
    import opentelemetry.metrics as otel_metrics
    import opentelemetry.trace as otel_trace

    _env(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", COLLECTOR_OTLP_HTTP)
    tracer = _FakeTracer()
    monkeypatch.setattr(otel_trace, "get_tracer", lambda name: tracer)

    recorded_histograms: list[tuple[str, float, dict[str, str], str, str]] = []
    recorded_counters: list[tuple[str, int, dict[str, str], str, str]] = []
    meter_names: list[str] = []

    class _FakeHistogram:
        def __init__(self, name: str, unit: str, description: str):
            self.name = name
            self.unit = unit
            self.description = description

        def record(self, amount: float, attributes: dict[str, str] | None = None) -> None:
            recorded_histograms.append(
                (self.name, amount, dict(attributes or {}), self.unit, self.description)
            )

    class _FakeCounter:
        def __init__(self, name: str, unit: str, description: str):
            self.name = name
            self.unit = unit
            self.description = description

        def add(self, amount: int, attributes: dict[str, str] | None = None) -> None:
            recorded_counters.append(
                (self.name, amount, dict(attributes or {}), self.unit, self.description)
            )

    class _FakeMeter:
        def create_histogram(self, name: str, unit: str = "", description: str = ""):
            return _FakeHistogram(name, unit, description)

        def create_counter(self, name: str, unit: str = "", description: str = ""):
            return _FakeCounter(name, unit, description)

    def _get_meter(name: str):
        meter_names.append(name)
        return _FakeMeter()

    monkeypatch.setattr(otel_metrics, "get_meter", _get_meter)
    ok = {"status": "pass", "latency_ms": 4.5, "detail": "ok"}
    monkeypatch.setattr(app, "_probe_redis", lambda *a, **k: dict(ok))
    monkeypatch.setattr(app, "_probe_postgres", lambda *a, **k: dict(ok))
    monkeypatch.setattr(app, "_probe_http", lambda *a, **k: dict(ok))
    monkeypatch.setattr(
        app,
        "_probe_s3",
        lambda **k: {"status": "fail", "latency_ms": 12.25, "detail": "denied"},
    )

    app.run_all_checks()

    assert set(meter_names) == {"platform-todo"}
    assert len(recorded_histograms) == 6
    assert len(recorded_counters) == 6
    hist_by_check = {
        attrs["canary.check"]: (name, val, attrs, unit)
        for name, val, attrs, unit, _ in recorded_histograms
    }
    assert hist_by_check["s3"] == (
        "canary.probe.duration_ms",
        12.25,
        {"canary.check": "s3", "canary.status": "fail"},
        "ms",
    )
    assert hist_by_check["postgres"] == (
        "canary.probe.duration_ms",
        4.5,
        {"canary.check": "postgres", "canary.status": "pass"},
        "ms",
    )
    cnt_by_check = {
        attrs["canary.check"]: (name, val, attrs, unit)
        for name, val, attrs, unit, _ in recorded_counters
    }
    assert cnt_by_check["s3"] == (
        "canary.probe.outcomes",
        1,
        {"canary.check": "s3", "canary.status": "fail"},
        "1",
    )


def test_traced_check_does_not_request_meter_without_otlp_endpoint(app, monkeypatch) -> None:
    """#1503: when OTEL_EXPORTER_OTLP_ENDPOINT is unset/empty, neither get_tracer nor
    get_meter is called."""
    import opentelemetry.metrics as otel_metrics
    import opentelemetry.trace as otel_trace

    _env(monkeypatch)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    def _forbid(*args, **kwargs):
        raise AssertionError("OTel meter/tracer requested without OTLP endpoint")

    monkeypatch.setattr(otel_trace, "get_tracer", _forbid)
    monkeypatch.setattr(otel_metrics, "get_meter", _forbid)
    ok = {"status": "pass", "latency_ms": 1.0, "detail": "ok"}
    monkeypatch.setattr(app, "_probe_redis", lambda *a, **k: dict(ok))
    monkeypatch.setattr(app, "_probe_postgres", lambda *a, **k: dict(ok))
    monkeypatch.setattr(app, "_probe_http", lambda *a, **k: dict(ok))
    monkeypatch.setattr(app, "_probe_s3", lambda **k: dict(ok))

    assert app.run_all_checks()["ok"] is True


def test_postgres_probe_crud_executes_write_read_delete_roundtrip(app) -> None:
    """#1503: verify_write=True / probe_postgres_crud executes SELECT 1 plus a temp-table
    CREATE, INSERT, SELECT and DELETE roundtrip, and fails on read-back mismatch."""

    class _CrudConn:
        def __init__(self, readback: str = "canary-write-ok"):
            self.statements: list[str] = []
            self._readback = readback
            self._last_sql = ""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql: str):
            self.statements.append(sql)
            self._last_sql = sql
            return self

        def fetchone(self):
            if self._last_sql == "SELECT 1":
                return (1,)
            return (self._readback,)

        def close(self):
            return None

    conn = _CrudConn("canary-write-ok")
    res = app.probe_postgres_crud(
        "platform-postgres-staging",
        5432,
        user="canary_rw",
        password=PG_PASSWORD,
        dbname="postgres",
        timeout=2.0,
        connector=lambda dsn, **kw: conn,
    )
    assert res["status"] == "pass", res
    assert conn.statements == [
        "SET LOCAL transaction_read_only = off",
        "SELECT 1",
        "CREATE TEMP TABLE IF NOT EXISTS canary_todo_probe (id TEXT PRIMARY KEY, title TEXT NOT NULL)",
        "INSERT INTO canary_todo_probe (id, title) VALUES ('probe-1', 'canary-write-ok') ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title",
        "SELECT title FROM canary_todo_probe WHERE id = 'probe-1'",
        "DELETE FROM canary_todo_probe WHERE id = 'probe-1'",
    ]

    bad_conn = _CrudConn("corrupted-value")
    bad_res = app._probe_postgres(
        "platform-postgres-staging",
        5432,
        user="canary_rw",
        password=PG_PASSWORD,
        dbname="postgres",
        timeout=2.0,
        connector=lambda dsn, **kw: bad_conn,
        verify_write=True,
    )
    assert bad_res["status"] == "fail"
    assert "corrupted-value" in bad_res["detail"]
    assert PG_PASSWORD not in bad_res["detail"]


def test_health_body_carries_the_cached_canary_verdict(app, monkeypatch) -> None:
    """#1635: public /api/health stays 200 and holds `canary` and `git_sha`, no host or error."""
    from starlette.testclient import TestClient

    _env(monkeypatch)
    _patch_probes(
        app,
        monkeypatch,
        s3={"status": "fail", "latency_ms": 2.0, "detail": "AccessDenied"},
    )
    client = TestClient(app.fastapi_app)
    assert client.get("/api/health").json()["canary"]["ok"] is False  # no run yet
    app.cached_canary_status()
    response = client.get("/api/health")
    body = response.json()
    assert response.status_code == 200
    assert body["canary"]["ok"] is False
    assert body["canary"]["checked_at"]
    assert "git_sha" in body
    assert "AccessDenied" not in response.text


# The probe runner contract that infra2 declares for this service (platform/30.todo deploy
# declaration and platform/12.alerting). infra2 tests pin the same values on its side.
INFRA2_PROBE_INTERVAL_SECONDS = 60.0
INFRA2_PROBE_TIMEOUT_SECONDS = 15.0
INFRA2_PROBE_PATH = "/api/canary/status"
SERVED_PATHS = {
    "/",
    "/api/health",
    "/index.html",
    "/api/canary/status",
    "/api/todos",
    "/api/todos/{todo_id}",
    "/api/todos/summary",
    "/api/auth/me",
}


def test_the_ttl_is_shorter_than_the_probe_interval(app) -> None:
    """Every probe round must see a fresh run: the cache must expire inside one interval."""
    assert 0 < app.STATUS_CACHE_TTL_SECONDS < INFRA2_PROBE_INTERVAL_SECONDS


def test_probe_budget_fits_inside_the_declared_probe_timeout(app) -> None:
    assert app.CHECK_DEADLINE_SECONDS < INFRA2_PROBE_TIMEOUT_SECONDS


def test_the_app_serves_the_probe_path(app) -> None:
    assert INFRA2_PROBE_PATH in _served_paths()


def test_served_paths_are_the_routed_contract(app) -> None:
    """infra2 routes exactly these paths (only /api/health without SSO). A new path must
    change this set and the infra2 routing test together."""
    assert _served_paths() == SERVED_PATHS


def test_the_classification_sits_inside_the_first_128_bytes(app, monkeypatch) -> None:
    """The infra2 probe runner keeps only the first 128 bytes of the body (infra2
    common/observability/probes.py). The red/gap classification must sit inside them."""
    _env(monkeypatch)
    gap = {"status": "unconfigured", "latency_ms": 0.1, "detail": "CANARY_S3 gap"}
    _patch_probes(app, monkeypatch, s3=gap)
    _code, body = _get_status(app)
    head = body[:128] if isinstance(body, str) else body[:128].decode()
    assert '"unconfigured":["s3"]' in head or '"unconfigured": ["s3"]' in head
    assert '"failed":[]' in head or '"failed": []' in head
    assert '"ok":false' in head or '"ok": false' in head
