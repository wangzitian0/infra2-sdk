"""Tests for infra2_sdk.storage including live Postgres catalog & schema gate."""

from __future__ import annotations

import io
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from threading import Thread
from typing import Any

import pytest

from infra2_sdk import service, storage


@dataclass(frozen=True)
class _MetaColumn:
    name: str
    type: object


@dataclass(frozen=True)
class _MetaTable:
    name: str
    columns: tuple[_MetaColumn, ...]


@dataclass(frozen=True)
class _FakeMetadata:
    tables: dict[str, _MetaTable]


DSN_ENV = "INFRA2_SDK_TEST_POSTGRES_DSN"
IMAGE = "docker.io/library/postgres:16-alpine"

SHAPES: list[tuple[str, str, frozenset[str]]] = [
    ("direct", "sdkt_e_direct", frozenset({"sdkt_direct"})),
    ("array", "sdkt_e_array", frozenset({"sdkt_array"})),
    ("domain", "sdkt_e_domain", frozenset({"sdkt_domain"})),
    ("domain_over_domain", "sdkt_e_domdom", frozenset({"sdkt_domdom"})),
    ("domain_over_array", "sdkt_e_domarr", frozenset({"sdkt_domarr"})),
    ("array_of_domain", "sdkt_e_arrdom", frozenset({"sdkt_arrdom"})),
    ("composite_field", "sdkt_e_comp", frozenset({"sdkt_comp"})),
    ("table_row_type", "sdkt_e_row", frozenset({"sdkt_row", "sdkt_rowuser"})),
    ("range", "sdkt_e_range", frozenset({"sdkt_range"})),
    ("multirange", "sdkt_e_multi", frozenset({"sdkt_multi"})),
    ("partitioned", "sdkt_e_part", frozenset({"sdkt_part", "sdkt_part_a"})),
    ("other_schema", "sdkt_e_s2", frozenset({"sdkt_s2"})),
    ("two_domains_mix", "sdkt_e_mix", frozenset({"sdkt_gone", "sdkt_kept"})),
    ("view_only", "sdkt_e_view", frozenset()),
    ("matview_only", "sdkt_e_matview", frozenset()),
    ("function_only", "sdkt_e_func", frozenset()),
    ("unused", "sdkt_e_unused", frozenset()),
    ("dropped_column", "sdkt_e_dropped", frozenset()),
]

DDL = """
CREATE TYPE sdkt_e_direct AS ENUM ('a');
CREATE TABLE sdkt_direct (v sdkt_e_direct);
CREATE TYPE sdkt_e_array AS ENUM ('a');
CREATE TABLE sdkt_array (v sdkt_e_array[]);
CREATE TYPE sdkt_e_domain AS ENUM ('a');
CREATE DOMAIN sdkt_d_domain AS sdkt_e_domain;
CREATE TABLE sdkt_domain (v sdkt_d_domain);
CREATE TYPE sdkt_e_domdom AS ENUM ('a');
CREATE DOMAIN sdkt_d_domdom_in AS sdkt_e_domdom;
CREATE DOMAIN sdkt_d_domdom_out AS sdkt_d_domdom_in;
CREATE TABLE sdkt_domdom (v sdkt_d_domdom_out);
CREATE TYPE sdkt_e_domarr AS ENUM ('a');
CREATE DOMAIN sdkt_d_domarr AS sdkt_e_domarr[];
CREATE TABLE sdkt_domarr (v sdkt_d_domarr);
CREATE TYPE sdkt_e_arrdom AS ENUM ('a');
CREATE DOMAIN sdkt_d_arrdom AS sdkt_e_arrdom;
CREATE TABLE sdkt_arrdom (v sdkt_d_arrdom[]);
CREATE TYPE sdkt_e_comp AS ENUM ('a');
CREATE TYPE sdkt_c_comp AS (x sdkt_e_comp);
CREATE TABLE sdkt_comp (v sdkt_c_comp);
CREATE TYPE sdkt_e_row AS ENUM ('a');
CREATE TABLE sdkt_row (v sdkt_e_row);
CREATE TABLE sdkt_rowuser (r sdkt_row);
CREATE TYPE sdkt_e_range AS ENUM ('a', 'b');
CREATE TYPE sdkt_r_range AS RANGE (subtype = sdkt_e_range);
CREATE TABLE sdkt_range (v sdkt_r_range);
CREATE TYPE sdkt_e_multi AS ENUM ('a', 'b');
CREATE TYPE sdkt_r_multi AS RANGE (subtype = sdkt_e_multi, multirange_type_name = sdkt_mr_multi);
CREATE TABLE sdkt_multi (v sdkt_mr_multi);
CREATE TYPE sdkt_e_part AS ENUM ('a');
CREATE TABLE sdkt_part (v sdkt_e_part) PARTITION BY LIST (v);
CREATE TABLE sdkt_part_a PARTITION OF sdkt_part FOR VALUES IN ('a');
CREATE TYPE sdkt_e_s2 AS ENUM ('a');
CREATE TABLE {s2}.sdkt_s2 (v sdkt_e_s2);
CREATE TYPE sdkt_e_mix AS ENUM ('a');
CREATE TABLE sdkt_gone (v sdkt_e_mix);
CREATE DOMAIN sdkt_d_mix_in AS sdkt_e_mix;
CREATE DOMAIN sdkt_d_mix_out AS sdkt_d_mix_in;
CREATE TABLE sdkt_kept (v sdkt_d_mix_out);
CREATE TYPE sdkt_e_view AS ENUM ('a');
CREATE VIEW sdkt_view AS SELECT 'a'::sdkt_e_view AS x;
CREATE TYPE sdkt_e_matview AS ENUM ('a');
CREATE MATERIALIZED VIEW sdkt_matview AS SELECT 'a'::sdkt_e_matview AS x;
CREATE TYPE sdkt_e_func AS ENUM ('a');
CREATE FUNCTION sdkt_func(sdkt_e_func) RETURNS int LANGUAGE sql AS 'SELECT 1';
CREATE TYPE sdkt_e_unused AS ENUM ('a');
CREATE TYPE sdkt_e_dropped AS ENUM ('a');
CREATE TABLE sdkt_dropped (id int, v sdkt_e_dropped);
ALTER TABLE sdkt_dropped DROP COLUMN v;
"""


def test_postgres_dsn_and_redaction() -> None:
    assert (
        storage._normalize_postgres_dsn(
            "postgresql+psycopg://u:p@db.internal:5432/app?sslmode=require"
        )
        == "postgresql://u:p@db.internal:5432/app?sslmode=require"
    )
    assert storage._normalize_postgres_dsn("postgres://u:p@h/d") == "postgresql://u:p@h/d"
    assert storage._normalize_postgres_dsn("postgresql:///app?host=/var/run/postgresql").startswith(
        "postgresql://"
    )
    with pytest.raises(ValueError):
        storage._normalize_postgres_dsn("mysql://u:p@h/d")

    cfg = storage.PostgresSettings.from_env({"DATABASE_URL": "postgresql://u:secretpw@h/d"})
    assert "secretpw" not in repr(cfg)
    scrubbed = storage.redact_postgres_error(
        RuntimeError("failed postgres://u:secretpw@h/d"), cfg.url
    )
    assert "secretpw" not in scrubbed

    # Regression guard: password == "postgres" must not corrupt "postgresql://".
    pg_pass_err = storage.redact_postgres_error(
        RuntimeError("connect postgresql://postgres:postgres@db.internal:5432/app failed"),
        "postgresql://postgres:postgres@db.internal:5432/app",
    )
    assert "postgresql://[REDACTED]" in pg_pass_err
    assert "<redacted>ql://" not in pg_pass_err

    down = storage.probe_postgres("postgresql://u:secretpw@127.0.0.1:1/d", connect_timeout=0.5)
    assert down.status is service.DependencyStatus.ABSENT and "secretpw" not in down.detail


def _start_resp_server(handler):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            try:
                handler(conn)
            finally:
                conn.close()

    Thread(target=loop, daemon=True).start()
    return srv, port


def test_redis_probe_roundtrip_unquoting_and_readonly() -> None:
    stored: dict[str, bytes] = {}
    auth_calls: list[list[str]] = []
    writes_seen = 0

    def _read_cmd(conn: socket.socket, buf: bytearray) -> list[str] | None:
        def _line() -> bytes | None:
            while b"\r\n" not in buf:
                chunk = conn.recv(1024)
                if not chunk:
                    return None
                buf.extend(chunk)
            i = buf.index(b"\r\n")
            val = bytes(buf[:i])
            del buf[: i + 2]
            return val

        hdr = _line()
        if hdr is None:
            return None
        n = int(hdr[1:])
        args: list[str] = []
        for _ in range(n):
            sz = int(_line()[1:])  # type: ignore[index]
            while len(buf) < sz + 2:
                buf.extend(conn.recv(1024))
            args.append(bytes(buf[:sz]).decode())
            del buf[: sz + 2]
        return args

    def _handle(conn: socket.socket) -> None:
        nonlocal writes_seen
        buf = bytearray()
        while (cmd := _read_cmd(conn, buf)) is not None:
            op = cmd[0].upper()
            if op == "AUTH":
                auth_calls.append(cmd[1:])
                reply = b"-WRONGPASS bad\r\n" if cmd[-1] == "badpw" else b"+OK\r\n"
                conn.sendall(reply)
            elif op in {"SELECT", "SETEX"}:
                if op == "SETEX":
                    writes_seen += 1
                    stored[cmd[1]] = cmd[3].encode()
                conn.sendall(b"+OK\r\n")
            elif op == "PING":
                conn.sendall(b"+PONG\r\n")
            elif op == "GET":
                val = stored.get(cmd[1], b"")
                conn.sendall(f"${len(val)}\r\n".encode() + val + b"\r\n")
            elif op == "DEL":
                stored.pop(cmd[1], None)
                conn.sendall(b":1\r\n")
            else:
                conn.sendall(b"-ERR unknown\r\n")

    srv, port = _start_resp_server(_handle)
    try:
        res = storage.probe_redis(f"redis://user%40x:p%40ss%3A1@127.0.0.1:{port}/2")
        assert res.status is service.DependencyStatus.PRESENT
        assert auth_calls[-1] == ["user@x", "p@ss:1"]
        assert writes_seen == 1

        ro = storage.probe_redis(f"redis://:pw@127.0.0.1:{port}/0", write_probe=False)
        assert ro.status is service.DependencyStatus.PRESENT and writes_seen == 1

        auth_bad = storage.probe_redis(f"redis://:badpw@127.0.0.1:{port}/0")
        assert auth_bad.status is service.DependencyStatus.ABSENT and "badpw" not in auth_bad.detail
    finally:
        srv.close()

    cfg = storage.RedisSettings.from_env({"REDIS_URL": "redis://:secretpw@h:6379/1"})
    assert "secretpw" not in repr(cfg)


def test_redis_probe_uses_monotonic_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    def _handle(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(b"+PONG\r\n")

    srv, port = _start_resp_server(_handle)
    try:
        # Skew perf_counter vs monotonic to verify clock mismatch cannot trigger a false timeout.
        orig_perf = time.perf_counter
        orig_mono = time.monotonic
        monkeypatch.setattr(time, "perf_counter", lambda: orig_perf() + 5000.0)
        monkeypatch.setattr(time, "monotonic", lambda: orig_mono() + 10.0)
        res = storage.probe_redis(
            f"redis://127.0.0.1:{port}/0", connect_timeout=1.0, write_probe=False
        )
        assert res.status is service.DependencyStatus.PRESENT
    finally:
        srv.close()


def test_enum_diff_metadata_unwrapping_and_sql_safety_and_cli_import(tmp_path) -> None:
    @dataclass(frozen=True)
    class _BaseEnum:
        name: str
        enums: tuple[str, ...]
        native_enum: bool = True

    @dataclass(frozen=True)
    class _ArrayType:
        item_type: _BaseEnum

    @dataclass(frozen=True)
    class _DecorType:
        impl_instance: _BaseEnum

    meta = _FakeMetadata(
        tables={
            "items": _MetaTable(
                name="items",
                columns=(
                    _MetaColumn("tags", _ArrayType(_BaseEnum("tag_e", ("a", "b")))),
                    _MetaColumn("state", _DecorType(_BaseEnum("state_e", ("open",)))),
                    _MetaColumn("non_native", _BaseEnum("ignored_e", ("x",), native_enum=False)),
                ),
            )
        }
    )
    extracted = storage._enums_from_metadata(meta)
    assert [(e.name, e.labels) for e in extracted] == [
        ("state_e", ("open",)),
        ("tag_e", ("a", "b")),
    ]

    spec = storage._EnumSchemaSpec(schema_name="public", name="status", labels=("open", "closed"))
    add_diff = storage._check_enums([spec], {("public", "status"): ("open",)})
    assert add_diff[0].kind == "additive_labels"
    assert (
        storage._SchemaDiffReport(enum_discrepancies=add_diff, migrations_pending=False).is_blocking
        is True
    )
    assert (
        storage._SchemaDiffReport(enum_discrepancies=add_diff, migrations_pending=True).is_blocking
        is False
    )

    extra_diff = storage._check_enums(
        [spec], {("public", "status"): ("open", "closed"), ("public", "old_e"): ("x",)}
    )
    assert any(d.kind == "extra_type" and d.name == "old_e" for d in extra_diff)
    assert (
        storage._classify_rollback(enum_discrepancies=extra_diff, migrations_pending=False)
        == storage._ROLLBACK_CLASS_C
    )
    assert (
        storage._classify_rollback(
            enum_discrepancies=extra_diff,
            enum_tables={("public", "old_e"): {("public", "old_tbl")}},
            code_tables={("public", "live_tbl")},
            migrations_pending=True,
        )
        == storage._ROLLBACK_CLASS_A
    )

    missing_diff = storage._check_enums([spec], {})
    assert storage._classify_rollback(enum_discrepancies=missing_diff) == storage._ROLLBACK_CLASS_C
    assert (
        storage._classify_rollback(enum_discrepancies=missing_diff, new_tables={("public", "t")})
        == storage._ROLLBACK_CLASS_A
    )

    assert (
        storage._validate_readonly_sql("SELECT 'a -- b' FROM todos;")
        == "SELECT 'a -- b' FROM todos"
    )
    with pytest.raises(ValueError, match="dollar-quoted"):
        storage._validate_readonly_sql("SELECT $$x$$")
    with pytest.raises(ValueError, match="forbids keyword 'SET_CONFIG'"):
        storage._validate_readonly_sql("SELECT set_config('a','b',false)")

    with pytest.raises(storage._GateNotEvaluated):
        storage._check_schema("postgresql://u:pw@127.0.0.1:1/db", connect_timeout=0.5)
    assert "check_schema_cli" in storage.__all__
    assert (
        storage.check_schema_cli(["check-schema", "--database-url", ""])
        == storage._EXIT_NOT_EVALUATED
    )
    assert storage._main(["check-schema", "--database-url", ""]) == storage._EXIT_NOT_EVALUATED

    # CLI --import option imports side-effect module before resolving --metadata.
    (tmp_path / "custom_side_effect.py").write_text("IMPORTED = True\n", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "infra2_sdk.storage",
            "check-schema",
            "--import",
            "custom_side_effect",
            "--metadata",
            "nonexistent:METADATA",
            "--db-url",
            "postgresql://u:pw@127.0.0.1:1/db",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == storage._EXIT_NOT_EVALUATED
    assert "No module named 'nonexistent'" in proc.stderr


def test_s3_helpers_cleanup_and_redaction() -> None:
    cfg = storage.S3Settings.from_env(
        {
            "S3_ENDPOINT": "http://127.0.0.1:9000",
            "S3_BUCKET": "canary",
            "S3_ACCESS_KEY": "ak-123",
            "S3_SECRET_KEY": "sk-456",
            "S3_REGION": "us-west-2",
        }
    )
    assert "sk-456" not in repr(cfg)
    assert "X-Amz-Signature=REDACTED" in storage.redact_presigned_url(
        "https://ak-123:sk-456@s3.example/b/k?X-Amz-Signature=topsecret"
    )
    assert storage.redact_presigned_url("not a url [unclosed") == "not a url [unclosed"

    store: dict[str, bytes] = {}
    body_closed = [False]
    fail_mode = ["none"]

    class _Stream(io.BytesIO):
        def close(self) -> None:
            body_closed[0] = True
            super().close()

    class _FakeS3:
        def head_bucket(self, Bucket: str) -> None:  # noqa: N803
            return None

        def put_object(self, Bucket: str, Key: str, Body: bytes, **_kw: object) -> dict[str, str]:  # noqa: N803
            store[Key] = Body
            return {"ETag": '"abc"'}

        def get_object(self, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
            if fail_mode[0] == "url_prose":
                raise RuntimeError(
                    "connect failed on https://s3user:s3pass@s3.example/canary?X-Amz-Signature=sig123"
                )
            return {"Body": _Stream(store[Key])}

        def delete_object(self, Bucket: str, Key: str) -> None:  # noqa: N803
            store.pop(Key, None)

    fake = _FakeS3()
    assert storage.put_object_bytes(fake, "canary", "k", b"hi") == "abc"
    assert storage.read_object_bytes(fake, "canary", "k") == b"hi" and body_closed[0] is True
    assert (
        storage.probe_s3(cfg, round_trip=True, client=fake).status
        is service.DependencyStatus.PRESENT
    )

    fail_mode[0] = "url_prose"
    failed = storage.probe_s3(cfg, round_trip=True, client=fake)
    assert failed.status is service.DependencyStatus.ABSENT
    assert failed.detail.startswith("RuntimeError:")
    assert "s3pass" not in failed.detail and "sig123" not in failed.detail
    assert [k for k in store if k.startswith("sdk/probe/")] == []


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(url: str, psycopg) -> None:
    deadline = time.monotonic() + 90
    while True:
        try:
            with psycopg.connect(url, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


@pytest.fixture(scope="module")
def pg_url() -> Iterator[tuple[str, bool]]:
    psycopg = pytest.importorskip("psycopg", reason="psycopg is absent")
    dsn = os.environ.get(DSN_ENV, "")
    if dsn:
        _wait_for(dsn, psycopg)
        yield dsn, False
        return
    runtime = "docker" if shutil.which("docker") else ("podman" if shutil.which("podman") else None)
    if runtime is None:
        pytest.skip(f"no PostgreSQL: set {DSN_ENV} or install docker/podman")
    port = _free_port()
    name = f"sdkt-{secrets.token_hex(4)}"
    password = secrets.token_hex(8)
    run = subprocess.run(
        [
            runtime,
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-p",
            f"127.0.0.1:{port}:5432",
            IMAGE,
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if run.returncode != 0:
        pytest.skip(f"{runtime} cannot start {IMAGE}: {run.stderr.strip()[-200:]}")
    url = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"
    try:
        _wait_for(url, psycopg)
        yield url, True
    finally:
        subprocess.run(
            [runtime, "stop", "-t", "2", name], capture_output=True, timeout=120, check=False
        )


@pytest.fixture(scope="module")
def enum_tables(pg_url: tuple[str, bool]) -> Iterator[tuple[str, str, dict[str, frozenset[str]]]]:
    import psycopg

    base_url, owned = pg_url
    schema = f"sdkt_{secrets.token_hex(4)}"
    s2 = f"{schema}_s2"
    with psycopg.connect(base_url, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"CREATE SCHEMA {s2}")
        conn.execute(f"SET search_path TO {schema}")
        conn.execute(DDL.replace("{s2}", s2))
    try:
        with psycopg.connect(base_url) as conn, conn.cursor() as cur:
            raw = storage._fetch_db_enum_tables(cur)
        by_enum = {
            enum_name: frozenset(t for _, t in tables)
            for (_es, enum_name), tables in raw.items()
            if enum_name.startswith("sdkt_")
        }
        yield base_url, schema, by_enum
    finally:
        if not owned:
            with psycopg.connect(base_url, autocommit=True) as conn:
                conn.execute(f"DROP SCHEMA {schema}, {s2} CASCADE")


@pytest.mark.parametrize(("shape", "enum_name", "tables"), SHAPES, ids=[s[0] for s in SHAPES])
def test_enum_tables_query_shapes(
    enum_tables: tuple[str, str, dict[str, frozenset[str]]],
    shape: str,
    enum_name: str,
    tables: frozenset[str],
) -> None:
    _url, _schema, mapping = enum_tables
    assert mapping.get(enum_name, frozenset()) == tables, shape


def test_probe_and_check_schema_live(
    enum_tables: tuple[str, str, dict[str, frozenset[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, _schema, _ = enum_tables
    probe = storage.probe_postgres(url, verify_write=True)
    assert probe.status is service.DependencyStatus.PRESENT and "write verified" in probe.detail

    bad_pre = storage._check_schema(url, preconditions=["SELECT 0"])
    assert bad_pre.is_blocking is True and bad_pre.preconditions_passed is False

    missing_meta = _FakeMetadata(
        tables={"nonexistent_tbl": _MetaTable("nonexistent_tbl", (_MetaColumn("id", int),))}
    )
    rep = storage._check_schema(url, metadata=missing_meta)
    assert ("public", "nonexistent_tbl") in rep.new_tables
    assert rep.is_blocking is True

    monkeypatch.syspath_prepend("canary/todo")
    rc = storage._main(
        ["check-schema", "--database-url", url, "--metadata", "canary_schema:METADATA", "--json"]
    )
    assert rc in {storage._EXIT_OK, storage._EXIT_BLOCKED}


def test_s3_public_endpoint_presign_and_alembic_error(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = storage.S3Settings.from_env(
        {
            "S3_ENDPOINT": "http://platform-s3:9000",
            "S3_PUBLIC_ENDPOINT": "https://s3.zitian.party",
            "S3_BUCKET": "bucket-1",
            "S3_ACCESS_KEY": "ak",
            "S3_SECRET_KEY": "sk",
        }
    )
    assert cfg.public_endpoint_url == "https://s3.zitian.party"

    created_endpoints: list[str] = []

    class _FakePresignClient:
        def __init__(self, endpoint: str) -> None:
            self._endpoint = endpoint
            self.closed = False

        def generate_presigned_url(
            self,
            op: str,
            *,
            Params: dict[str, Any],
            ExpiresIn: int,  # noqa: N803
        ) -> str:
            assert op == "get_object"
            assert ExpiresIn == 120
            return f"{self._endpoint}/{Params['Bucket']}/{Params['Key']}?sig=1"

        def close(self) -> None:
            self.closed = True

    def _fake_create(s: storage.S3Settings) -> _FakePresignClient:
        created_endpoints.append(s.endpoint_url)
        return _FakePresignClient(s.endpoint_url)

    monkeypatch.setattr(storage, "create_s3_client", _fake_create)
    pub_url = storage.presign_get_url(cfg, "reports/a.pdf", expires_in=120, public=True)
    priv_url = storage.presign_get_url(cfg, "reports/a.pdf", expires_in=120, public=False)
    assert pub_url.startswith("https://s3.zitian.party/bucket-1/reports/a.pdf")
    assert priv_url.startswith("http://platform-s3:9000/bucket-1/reports/a.pdf")
    assert created_endpoints == ["https://s3.zitian.party", "http://platform-s3:9000"]

    monkeypatch.setattr(
        storage,
        "_require_optional",
        lambda mod, *, extra: (_ for _ in ()).throw(RuntimeError(f"missing {mod} [{extra}]")),
    )
    with pytest.raises(RuntimeError, match="application environment"):
        storage._check_alembic_pending(None, alembic_config_path="alembic.ini")
