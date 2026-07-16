"""Offline smoke checks for independently installed runtime extras."""

from __future__ import annotations

import os
import sys


def smoke_core() -> None:
    from infra2_sdk.runtime import environment_from_env, runtime_env_contract

    assert environment_from_env({}).name == "local_dev"
    assert runtime_env_contract()["contract_version"] == 1
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
