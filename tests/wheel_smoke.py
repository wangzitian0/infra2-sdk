"""Offline smoke checks for independently installed runtime extras."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

# Modules removed in 3.3.0 (issue #90) and 3.4.0 (issue #102).
# One frozen list: tests/test_removed_modules.py imports it.
REMOVED_MODULES = (
    "infra2_sdk.deploy_health",
    "infra2_sdk.dispatch",
    "infra2_sdk.release",
    "infra2_sdk.runtime.environ",
    "infra2_sdk.runtime.dependencies",
    "infra2_sdk.runtime.probes",
    "infra2_sdk.snapshot",  # 3.4.0: no user was left after infra2 #1206
)

# Names removed in 3.4.0 (issue #102), as (module, dotted name). A dotted name is a method.
# One frozen list: tests/test_removed_names.py imports it.
REMOVED_NAMES = (
    ("infra2_sdk.capacity", "LEVELS"),
    ("infra2_sdk.capacity", "onepassword_capacity"),
    ("infra2_sdk.delivery", "PREVIEW_RELEVANT_STAGES"),
    ("infra2_sdk.refs", "resolve_release_identity"),
    ("infra2_sdk.refs", "verify_runtime_identity"),
    ("infra2_sdk.routing", "resolve_dokploy_domains"),
    ("infra2_sdk.runtime.config_schema", "settings_json_schema"),
    ("infra2_sdk.runtime.health", "assert_required_dependencies"),
    ("infra2_sdk.runtime.http", "retryable_request"),
    ("infra2_sdk.runtime.http", "parse_retry_after"),
    ("infra2_sdk.runtime.http", "HttpCheck"),
    ("infra2_sdk.runtime.identity", "runtime_identity_fingerprint"),
    ("infra2_sdk.runtime.otel", "inject_trace_context"),
    ("infra2_sdk.runtime.otel", "extract_trace_context"),
    ("infra2_sdk.runtime.postgres", "PostgresCheck"),
    ("infra2_sdk.runtime.s3", "S3Check"),
    ("infra2_sdk.secrets", "EnvBackend"),
    ("infra2_sdk.snapshot", "verify_snapshot_artifact"),
    # The three environment readers.
    ("infra2_sdk.runtime.http", "HttpClientSettings.from_env"),
    ("infra2_sdk.runtime.postgres", "PostgresSettings.from_env"),
    ("infra2_sdk.runtime.s3", "S3Settings.from_env"),
    # Names that only the removed code used.
    ("infra2_sdk.capacity", "OnePasswordCapacityReport"),
    ("infra2_sdk.capacity", "Runner"),
    ("infra2_sdk.refs", "ReleaseIdentity"),
    ("infra2_sdk.routing", "DokployDomainSpec"),
    ("infra2_sdk.runtime.environment", "env_bool"),
    ("infra2_sdk.runtime.environment", "env_float"),
    ("infra2_sdk.runtime.health", "DependencyUnavailableError"),
    ("infra2_sdk.secrets", "ReadOnlyBackendError"),
    # Found without a user by the audit of PR #104 (references inside the own body do not count).
    ("infra2_sdk.deploy", "DeployStatus"),
    # The public names of the module infra2_sdk.snapshot (see REMOVED_MODULES). The module is
    # gone, so the owner check does not apply to these names.
    ("infra2_sdk.snapshot", "AnonymizedSnapshotManifest"),
    ("infra2_sdk.snapshot", "ResidualScanProof"),
    ("infra2_sdk.snapshot", "ResidualScanStatus"),
    ("infra2_sdk.snapshot", "SNAPSHOT_MANIFEST_VERSION"),
    ("infra2_sdk.snapshot", "SnapshotArtifact"),
    ("infra2_sdk.snapshot", "SnapshotArtifactFormat"),
    ("infra2_sdk.snapshot", "SnapshotProducer"),
)

# RuntimeEnvKey members removed in 3.4.0 (issue #102, decision 2).
REMOVED_ENV_MEMBERS = (
    "HTTP_TIMEOUT_SECONDS",
    "HTTP_CONNECT_TIMEOUT_SECONDS",
    "HTTP_MAX_CONNECTIONS",
    "HTTP_MAX_KEEPALIVE_CONNECTIONS",
    "HTTP_CONNECT_RETRIES",
    "HTTP_USER_AGENT",
    "HTTP_FOLLOW_REDIRECTS",
    "OBJECT_STORAGE_PROTOCOL",
    "S3_BUCKET",
    "S3_ENDPOINT_URL",
    "S3_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "S3_CONNECT_TIMEOUT_SECONDS",
    "S3_READ_TIMEOUT_SECONDS",
    "S3_ADDRESSING_STYLE",
    "DATABASE_URL",
    "DATABASE_CONNECT_TIMEOUT_SECONDS",
    "DATABASE_STATEMENT_TIMEOUT_SECONDS",
)

# Environment variable names removed from runtime_env_contract() in 3.4.0 (issue #102,
# decisions 2 and 3): the canonical names, their aliases, and the dead alias SERVICE_NAME.
REMOVED_ENV_NAMES = (
    "HTTP_TIMEOUT_SECONDS",
    "HTTP_CONNECT_TIMEOUT_SECONDS",
    "HTTP_MAX_CONNECTIONS",
    "HTTP_MAX_KEEPALIVE_CONNECTIONS",
    "HTTP_CONNECT_RETRIES",
    "HTTP_USER_AGENT",
    "HTTP_FOLLOW_REDIRECTS",
    "OBJECT_STORAGE_PROTOCOL",
    "OBJECT_STORAGE_DRIVER",
    "S3_BUCKET",
    "AWS_ENDPOINT_URL_S3",
    "S3_ENDPOINT_URL",
    "S3_ENDPOINT",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "S3_REGION",
    "AWS_ACCESS_KEY_ID",
    "S3_ACCESS_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "S3_SECRET_KEY",
    "AWS_SESSION_TOKEN",
    "S3_SESSION_TOKEN",
    "S3_CONNECT_TIMEOUT_SECONDS",
    "S3_READ_TIMEOUT_SECONDS",
    "S3_ADDRESSING_STYLE",
    "DATABASE_URL",
    "DATABASE_CONNECT_TIMEOUT_SECONDS",
    "DATABASE_STATEMENT_TIMEOUT_SECONDS",
    "SERVICE_NAME",
)


def smoke_core() -> None:
    from infra2_sdk.refs import classify_ref
    from infra2_sdk.runtime import environment_from_env, runtime_env_contract

    assert classify_ref("v1.2.3") == "tag"
    assert environment_from_env({}).name == "local_dev"
    version = runtime_env_contract()["contract_version"]
    assert type(version) is int and version == 2, (
        f"contract_version must be the integer 2: {version!r}"
    )
    import infra2_sdk.rules.compose  # noqa: F401
    import infra2_sdk.runtime.health  # noqa: F401
    import infra2_sdk.runtime.http  # noqa: F401
    import infra2_sdk.runtime.otel  # noqa: F401
    import infra2_sdk.runtime.postgres  # noqa: F401
    import infra2_sdk.runtime.s3  # noqa: F401

    smoke_package_data()
    smoke_removed_names()
    smoke_health()


def smoke_package_data() -> None:
    """The wheel ships py.typed and the data files; the removed modules are absent."""

    from importlib.resources import files

    package = files("infra2_sdk")
    assert package.joinpath("py.typed").is_file(), "py.typed must ship in the wheel"
    assert package.joinpath("data/stages.yaml").is_file()
    assert not package.joinpath("data/platform_images.yaml").is_file()
    try:
        importlib.import_module("infra2_sdk.images")
    except ModuleNotFoundError:
        pass
    else:
        raise AssertionError("infra2_sdk.images was removed in 3.0.0 and must not be importable")
    assert len(REMOVED_MODULES) == 7, (
        f"REMOVED_MODULES has {len(REMOVED_MODULES)} entries, expected 7"
    )
    for module in REMOVED_MODULES:
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as exc:
            assert exc.name == module, (module, exc.name)
        else:
            raise AssertionError(f"{module} was removed and must not be importable")


def smoke_removed_names() -> None:
    """The wheel does not carry the names, members and variables removed in 3.4.0."""

    import infra2_sdk
    import infra2_sdk.runtime
    from infra2_sdk.runtime.environment import RuntimeEnvKey, runtime_env_contract

    assert len(REMOVED_NAMES) == 37 and len(set(REMOVED_NAMES)) == 37, (
        f"REMOVED_NAMES has {len(REMOVED_NAMES)} entries ({len(set(REMOVED_NAMES))} unique), "
        "expected 37"
    )
    for module, dotted in REMOVED_NAMES:
        *parents, leaf = dotted.split(".")
        if module not in REMOVED_MODULES:  # a removed module has no owner to look into
            owner = importlib.import_module(module)
            for part in parents:
                owner = getattr(owner, part)  # AttributeError: the owner vanished, not the name
            assert not hasattr(owner, leaf), f"{module}.{dotted} was removed in 3.4.0"
        if not parents:
            for package in (infra2_sdk, infra2_sdk.runtime):
                assert not hasattr(package, leaf), f"{package.__name__}.{leaf} was removed"
                assert leaf not in package.__all__, f"{package.__name__}.__all__ lists {leaf}"

    assert len(REMOVED_ENV_MEMBERS) == 20 and len(REMOVED_ENV_NAMES) == 29, (
        f"REMOVED_ENV_MEMBERS has {len(REMOVED_ENV_MEMBERS)} entries, expected 20; "
        f"REMOVED_ENV_NAMES has {len(REMOVED_ENV_NAMES)} entries, expected 29"
    )
    present = [member for member in REMOVED_ENV_MEMBERS if hasattr(RuntimeEnvKey, member)]
    assert not present, f"RuntimeEnvKey still has removed members: {present}"
    contract = runtime_env_contract()["variables"]
    names = {item["name"] for item in contract} | {a for item in contract for a in item["aliases"]}
    still_named = sorted(set(REMOVED_ENV_NAMES) & (names | {key.value for key in RuntimeEnvKey}))
    assert not still_named, f"runtime_env_contract() still names removed variables: {still_named}"


def smoke_health() -> None:
    from infra2_sdk.runtime import DependencyStatus, ProbeResult, check_health

    class Probe:
        def __init__(self, name: str, status: DependencyStatus) -> None:
            self.name = name
            self.status = status

        def probe(self) -> ProbeResult:
            return ProbeResult(self.name, self.status)

    code, body = asyncio.run(check_health([Probe("db", DependencyStatus.PRESENT)]))
    assert (code, body["status"]) == (200, "healthy"), (code, body)
    code, body = asyncio.run(check_health([Probe("db", DependencyStatus.ABSENT)]))
    assert (code, body["status"]) == (503, "unhealthy"), (code, body)


def smoke_s3() -> None:
    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
    from infra2_sdk.runtime.s3 import S3Settings, create_s3_client

    settings = S3Settings(
        bucket="smoke-bucket",
        endpoint_url="http://127.0.0.1:9000",
        access_key_id="smoke",
        secret_access_key="smoke",
    )
    client = create_s3_client(settings)
    assert client.meta.service_model.service_name == "s3"
    client.close()


def smoke_postgres() -> None:
    import psycopg  # noqa: F401

    from infra2_sdk.runtime.postgres import PostgresSettings

    settings = PostgresSettings(dsn="postgresql+asyncpg://user:password@localhost/database")
    assert settings.psycopg_dsn == "postgresql://user:password@localhost/database"


def smoke_http() -> None:
    from infra2_sdk.runtime.http import create_http_client

    client = create_http_client()
    assert type(client).__name__ == "Client"
    client.close()
    smoke_standalone_app()


def smoke_standalone_app() -> None:
    """Exercise the documented app entrypoint with an independently installed SDK."""

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/healthy" else 503)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    example = Path(__file__).resolve().parents[1] / "examples/runtime_check.py"
    endpoint = f"http://127.0.0.1:{server.server_port}"
    try:
        for path, ready in (("/healthy", True), ("/unhealthy", False), ("", False)):
            # Preserve process prerequisites without inheriting application identity,
            # dependency URLs, or proxy configuration from the invoking environment.
            env = {
                key: value
                for key, value in os.environ.items()
                if key
                in {
                    "PATH",
                    "SYSTEMROOT",
                    "SystemRoot",
                    "WINDIR",
                    "TEMP",
                    "TMP",
                    "TMPDIR",
                    "LANG",
                    "LANGUAGE",
                    "LD_LIBRARY_PATH",
                    "DYLD_LIBRARY_PATH",
                }
                or key.startswith("LC_")
            }
            env.update(ENVIRONMENT="local_dev", OTEL_SERVICE_NAME="standalone-example")
            if path:
                env["CATALOG_HEALTH_URL"] = endpoint + path
            try:
                result = subprocess.run(
                    [sys.executable, "-I", str(example)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise AssertionError(
                    f"example timed out after {exc.timeout}s for path {path!r}; "
                    f"stdout={exc.stdout!r}; stderr={exc.stderr!r}"
                ) from exc
            diagnostic = (
                f"exit={result.returncode}; stdout={result.stdout!r}; stderr={result.stderr!r}"
            )
            assert result.returncode == (0 if ready else 1), diagnostic
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise AssertionError(diagnostic) from exc
            assert isinstance(payload, dict) and payload.get("ready") is ready, diagnostic
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def smoke_otel() -> None:
    import opentelemetry.sdk.trace  # noqa: F401

    from infra2_sdk.runtime.otel import OtelSettings, configure_telemetry, signal_endpoint

    settings = OtelSettings.from_env(
        {
            "OTEL_SDK_DISABLED": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "grpc://ignored",
        }
    )
    assert not settings.enabled
    assert configure_telemetry(settings).tracer_provider is None
    assert signal_endpoint("http://collector:4318", "logs") == "http://collector:4318/v1/logs"


async def _asgi_get(app, path: str) -> tuple[int, dict]:
    """Send one GET through the ASGI interface; the fastapi extra does not ship httpx."""

    messages = []
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        messages.append(msg)

    await app(scope, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, json.loads(body.decode())


def smoke_fastapi() -> None:
    from fastapi import FastAPI

    from infra2_sdk.fastapi import init_service

    app = FastAPI()
    init_service(app, name="fastapi-smoke", enable_otel=False)

    status, livez = asyncio.run(_asgi_get(app, "/livez"))
    assert (status, livez) == (200, {"status": "alive", "service": "fastapi-smoke"})

    status, readyz = asyncio.run(_asgi_get(app, "/readyz"))
    assert (status, readyz) == (200, {"status": "ready", "service": "fastapi-smoke"})

    status, health = asyncio.run(_asgi_get(app, "/health"))
    assert status == 200 and health.get("status") == "healthy"

    async def down() -> bool:
        return False

    failing = FastAPI()
    init_service(failing, name="fastapi-smoke", db_checker=down, enable_otel=False)
    status, readyz = asyncio.run(_asgi_get(failing, "/readyz"))
    assert status == 503 and readyz["status"] == "not_ready", (status, readyz)
    status, health = asyncio.run(_asgi_get(failing, "/health"))
    assert status == 503 and health["status"] == "unhealthy", (status, health)

    # Fail closed: strict_otel defaults to True, so a missing tier stops the service.
    for variable in ("ENVIRONMENT", "ENV", "APP_ENV"):
        os.environ.pop(variable, None)
    hint = (
        "ENVIRONMENT is required; set one of ENVIRONMENT, ENV, APP_ENV to a known tier, "
        "or pass strict_otel=False for a local run"
    )
    try:
        init_service(FastAPI(), name="fastapi-smoke")
    except ValueError as exc:
        assert str(exc) == hint, exc
    else:
        raise AssertionError("init_service must refuse to start without a tier")

    smoke_fastapi_otel()


def smoke_fastapi_otel() -> None:
    """Start OTLP export through init_service and stop it with the app lifespan, offline."""

    import logging

    import opentelemetry.exporter.otlp.proto.http._log_exporter as log_exporter
    import opentelemetry.exporter.otlp.proto.http.metric_exporter as metric_exporter
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as trace_exporter
    from fastapi import FastAPI
    from opentelemetry.sdk._logs import export as log_export
    from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from infra2_sdk.fastapi import init_service

    class MemoryMetricExporter(MetricExporter):
        def export(self, metrics_data, timeout_millis=10_000, **kwargs):
            return MetricExportResult.SUCCESS

        def force_flush(self, timeout_millis=10_000):
            return True

        def shutdown(self, timeout_millis=30_000, **kwargs):
            return None

    spans = InMemorySpanExporter()
    endpoints: list[str] = []

    def memory_exporter(exporter):
        def build(*, endpoint):
            endpoints.append(endpoint)
            return exporter

        return build

    log_memory = (
        getattr(log_export, "InMemoryLogRecordExporter", None) or log_export.InMemoryLogExporter
    )()
    trace_exporter.OTLPSpanExporter = memory_exporter(spans)
    log_exporter.OTLPLogExporter = memory_exporter(log_memory)
    metric_exporter.OTLPMetricExporter = memory_exporter(MemoryMetricExporter())
    os.environ.update(
        ENVIRONMENT="staging",
        OTEL_EXPORTER_OTLP_ENDPOINT="http://collector.invalid:4318",
        OTEL_METRIC_EXPORT_INTERVAL="3600000",
    )

    app = FastAPI()
    init_service(app, name="fastapi-otel-smoke", enable_otel=True)
    providers = app.state.telemetry_providers
    root = logging.getLogger()
    assert providers.logging_handler in root.handlers
    assert sorted(endpoints) == [
        "http://collector.invalid:4318/v1/logs",
        "http://collector.invalid:4318/v1/metrics",
        "http://collector.invalid:4318/v1/traces",
    ], endpoints

    async def run_app() -> None:
        incoming: asyncio.Queue = asyncio.Queue()
        sent: list[dict] = []

        async def receive():
            return await incoming.get()

        async def send(msg):
            sent.append(msg)

        lifespan = asyncio.create_task(app({"type": "lifespan"}, receive, send))
        await incoming.put({"type": "lifespan.startup"})
        while not sent and not lifespan.done():
            await asyncio.sleep(0.01)
        assert sent and sent[0]["type"] == "lifespan.startup.complete", sent
        status, livez = await _asgi_get(app, "/livez")
        assert (status, livez) == (200, {"status": "alive", "service": "fastapi-otel-smoke"})
        await incoming.put({"type": "lifespan.shutdown"})
        await lifespan

    asyncio.run(run_app())
    assert "GET /livez" in [span.name for span in spans.get_finished_spans()]
    assert providers.logging_handler is None and not [
        handler for handler in root.handlers if type(handler).__name__ == "LoggingHandler"
    ], "the lifespan shutdown must detach the OpenTelemetry logging handler"


def smoke_all() -> None:
    import boto3  # noqa: F401
    import httpx  # noqa: F401
    import opentelemetry.sdk.trace  # noqa: F401
    import psycopg  # noqa: F401

    from infra2_sdk.runtime import RuntimeIdentity

    assert RuntimeIdentity.from_env({"OTEL_SERVICE_NAME": "all-smoke"}).service_name == "all-smoke"


SMOKES = {
    "core": smoke_core,
    "s3": smoke_s3,
    "postgres": smoke_postgres,
    "http": smoke_http,
    "otel": smoke_otel,
    "fastapi": smoke_fastapi,
    "all": smoke_all,
}


if __name__ == "__main__":
    try:
        smoke = SMOKES[sys.argv[1]]
    except (IndexError, KeyError):
        raise SystemExit(f"usage: {sys.argv[0]} <{'|'.join(SMOKES)}>") from None
    smoke()
