"""Offline smoke checks for independently installed runtime extras."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread


def smoke_core() -> None:
    from infra2_sdk.runtime import environment_from_env, runtime_env_contract
    from infra2_sdk.snapshot import SNAPSHOT_MANIFEST_VERSION

    assert SNAPSHOT_MANIFEST_VERSION == 1
    assert environment_from_env({}).name == "local_dev"
    assert runtime_env_contract()["contract_version"] == 1
    import infra2_sdk.images  # noqa: F401
    import infra2_sdk.rules.compose  # noqa: F401
    import infra2_sdk.runtime.http  # noqa: F401
    import infra2_sdk.runtime.otel  # noqa: F401
    import infra2_sdk.runtime.postgres  # noqa: F401
    import infra2_sdk.runtime.s3  # noqa: F401


def smoke_s3() -> None:
    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
    from infra2_sdk.runtime.s3 import S3Settings, create_s3_client

    settings = S3Settings.from_env(
        {
            "S3_BUCKET": "smoke-bucket",
            "AWS_ENDPOINT_URL_S3": "http://127.0.0.1:9000",
            "AWS_ACCESS_KEY_ID": "smoke",
            "AWS_SECRET_ACCESS_KEY": "smoke",
        }
    )
    client = create_s3_client(settings)
    assert client.meta.service_model.service_name == "s3"
    client.close()


def smoke_postgres() -> None:
    import psycopg  # noqa: F401

    from infra2_sdk.runtime.postgres import PostgresSettings

    settings = PostgresSettings.from_env(
        {"DATABASE_URL": "postgresql+asyncpg://user:password@localhost/database"}
    )
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

    from infra2_sdk.runtime.otel import OtelSettings, configure_telemetry

    settings = OtelSettings.from_env(
        {
            "OTEL_SDK_DISABLED": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "grpc://ignored",
        }
    )
    assert not settings.enabled
    assert configure_telemetry(settings).tracer_provider is None


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
    "all": smoke_all,
}


if __name__ == "__main__":
    try:
        smoke = SMOKES[sys.argv[1]]
    except (IndexError, KeyError):
        raise SystemExit(f"usage: {sys.argv[0]} <{'|'.join(SMOKES)}>") from None
    smoke()
