"""Scaffolding engine and CLI for infra2 services.

Generates a service skeleton:
- compose.yaml (explicit memory ceilings, tmpfs secrets, health checks)
- deploy.py (literal AST-evaluable ProbeFacet, SignalFacet, SecretsFacet)
- secrets.ctmpl and vault-policy.hcl (rendered by infra2_sdk.secrets from one manifest)
- vault-agent.hcl (AppRole sidecar configuration)
- entrypoint.sh (secrets synchronization and alembic database migrations)
- app.py (FastAPI app that calls init_service for /livez, /readyz and /health)
- Dockerfile and requirements.txt

The standalone profile skips the Vault files and writes .env.example instead.

Usage:
    python -m infra2_sdk.scaffold <name> [--project apps] [--port 8000] [--db postgres]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

import infra2_sdk
from infra2_sdk.runtime.config_schema import EnvironmentField, EnvironmentManifest, FieldSource
from infra2_sdk.secrets import render_agent_policy, render_agent_template


def _clean_ident(value: str) -> str:
    """Normalize identifier for Postgres/Vault (lowercase, replace '-' with '_')."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", value.strip().lower())


def generate_compose_yaml(
    service_name: str,
    project: str,
    port: int,
    db: str = "postgres",
    profile: str = "infra2",
) -> str:
    """Generate compose.yaml compliant with ops.standards.md §5 and memory limit lint."""
    clean_service = _clean_ident(service_name)
    clean_project = _clean_ident(project)
    container_prefix = f"{clean_project}-{clean_service}"
    db_env = "      DATABASE_URL: ${DATABASE_URL:-}\n" if db == "postgres" else ""
    # A manifest without fields renders an empty .env, so the sidecar check tests existence.
    env_file_test = "-s" if scaffold_manifest(service_name, project, db).fields else "-e"

    if profile == "standalone":
        return f"""services:
  {clean_service}:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: {clean_service}
    restart: always
    deploy:
      resources:
        limits:
          memory: 256M
    env_file:
      - .env
    environment:
{db_env}      PORT: "{port}"
      ENV: ${{ENV:-local_dev}}
      OTEL_EXPORTER_OTLP_ENDPOINT: ${{OTEL_EXPORTER_OTLP_ENDPOINT:-}}
      OTEL_SERVICE_NAME: ${{OTEL_SERVICE_NAME:-{clean_service}}}
      OTEL_RESOURCE_ATTRIBUTES: ${{OTEL_RESOURCE_ATTRIBUTES:-}}
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost:{port}/health || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
"""

    return f"""services:
  vault-agent:
    init: true
    image: hashicorp/vault:1.15
    container_name: {container_prefix}-vault-agent${{ENV_SUFFIX}}
    restart: always
    deploy:
      resources:
        limits:
          memory: 128M
    entrypoint:
      - sh
      - -c
      - |
        rm -f /vault/secrets/.env /vault/.token /vault/role_id /vault/secret_id 2>/dev/null || true
        if [ -z "$$VAULT_ROLE_ID" ] || [ -z "$$VAULT_SECRET_ID" ]; then
          echo "VAULT_ROLE_ID and VAULT_SECRET_ID are required"
          exit 1
        fi
        if [ -z "$$VAULT_ADDR" ]; then
          echo "VAULT_ADDR is required"
          exit 1
        fi
        echo "$$VAULT_ROLE_ID" > /vault/role_id
        echo "$$VAULT_SECRET_ID" > /vault/secret_id
        exec vault agent -config=/etc/vault/vault-agent.hcl
    environment:
      VAULT_ADDR: ${{VAULT_ADDR}}
      VAULT_ROLE_ID: ${{VAULT_ROLE_ID:-}}
      VAULT_SECRET_ID: ${{VAULT_SECRET_ID:-}}
      ENV: ${{ENV}}
      IAC_CONFIG_HASH: ${{IAC_CONFIG_HASH:-}}
    volumes:
      - ./vault-agent.hcl:/etc/vault/vault-agent.hcl:ro
      - ./secrets.ctmpl:/etc/vault/secrets.ctmpl:ro
      - secrets:/vault/secrets
    networks:
      - dokploy-network
    logging:
      driver: json-file
      options:
        max-size: "5m"
        max-file: "2"
    healthcheck:
      test:
        - CMD-SHELL
        - |
          test {env_file_test} /vault/secrets/.env && \\
            ! grep -q "<no value>" /vault/secrets/.env && \\
            test -s /vault/.token || exit 1
          if [ -z "$$(find /tmp/.vault_hc -mmin -5 2>/dev/null)" ]; then
            wget -q -T 3 --header="X-Vault-Token: $$(cat /vault/.token)" \\
              "$$VAULT_ADDR/v1/auth/token/lookup-self" -O /dev/null || exit 1
            touch /tmp/.vault_hc
          fi
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

  {clean_service}:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: {container_prefix}${{ENV_SUFFIX}}
    restart: always
    deploy:
      resources:
        limits:
          memory: 256M
    depends_on:
      - vault-agent
    volumes:
      - secrets:/secrets:ro
    environment:
{db_env}      PORT: "{port}"
      ENV: ${{ENV}}
      OTEL_EXPORTER_OTLP_ENDPOINT: ${{OTEL_EXPORTER_OTLP_ENDPOINT:-}}
      OTEL_SERVICE_NAME: ${{OTEL_SERVICE_NAME:-}}
      OTEL_RESOURCE_ATTRIBUTES: ${{OTEL_RESOURCE_ATTRIBUTES:-}}
    networks:
      - dokploy-network
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost:{port}/health || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 120s

volumes:
  secrets:
    driver_opts:
      type: tmpfs
      device: tmpfs
      o: "size=10m,mode=0755"

networks:
  dokploy-network:
    external: true
"""


def generate_deploy_py(
    service_name: str,
    project: str,
    port: int,
) -> str:
    """Generate deploy.py with literal AST-compliant facet assignments on ClassDef.body."""
    clean_service = _clean_ident(service_name)
    clean_project = _clean_ident(project)
    class_name = "".join(part.capitalize() for part in clean_service.split("_")) + "Deployer"
    container_prefix = f"{clean_project}-{clean_service}"

    return f'''"""{clean_service} service deployment — AST-compliant Deployer definition."""

from __future__ import annotations

import sys
from invoke import task

from libs.core.facets import ProbeFacet, SecretsFacet, SignalFacet
from libs.deploy.deployer import Deployer, make_tasks

shared_tasks = sys.modules.get("{clean_project}.{clean_service}.shared")


class {class_name}(Deployer):
    service = "{clean_service}"
    project = "{clean_project}"
    service_port = {port}
    subdomain = "{clean_service}"

    backups = ()

    probes = (
        ProbeFacet(
            name="{clean_service}-http",
            kind="http",
            target="http://{container_prefix}${{ENV_SUFFIX}}:{port}/health",
            expected="200",
            timeout_seconds=10,
        ),
    )

    signals = (
        SignalFacet(
            tier="minute",
            type="alert",
            consecutive_failures=3,
            renotify_window_sec=0,
        ),
    )

    secrets = (
        SecretsFacet(
            vault_agent_container="{container_prefix}-vault-agent${{ENV_SUFFIX}}",
            app_containers=("{container_prefix}${{ENV_SUFFIX}}",),
            auth_method="approle",
        ),
    )


if shared_tasks:
    _tasks = make_tasks({class_name}, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
'''


def scaffold_manifest(service_name: str, project: str, db: str = "postgres") -> EnvironmentManifest:
    """Declare the secrets the generated app reads.

    ``infra2_sdk.secrets`` renders the Vault Agent template and policy from this manifest.
    """
    fields: tuple[EnvironmentField, ...] = ()
    if db == "postgres":
        fields = (
            EnvironmentField(
                field="database_url",
                env="DATABASE_URL",
                required=True,
                has_default=False,
                sensitive=True,
                group="Database",
                source=FieldSource.HUMAN,
            ),
        )
    source = f"scaffold:{_clean_ident(project)}/{_clean_ident(service_name)}"
    return EnvironmentManifest(source=source, fields=fields)


def generate_vault_agent_hcl() -> str:
    """Generate platform canonical Vault Agent configuration."""
    return """vault {
  address = "${VAULT_ADDR}"
}

auto_auth {
  method "approle" {
    config = {
      role_id_file_path                   = "/vault/role_id"
      secret_id_file_path                 = "/vault/secret_id"
      remove_secret_id_file_after_reading = false
    }
  }

  sink "file" {
    config = {
      path = "/vault/.token"
    }
  }

  exit_on_err = true
}

template_config {
  static_secret_render_interval = "5m"
  exit_on_retry_failure         = true
}

template {
  source               = "/etc/vault/secrets.ctmpl"
  destination          = "/vault/secrets/.env"
  error_on_missing_key = true
}
"""


def generate_entrypoint_sh(port: int, profile: str = "infra2") -> str:
    """Generate entrypoint script waiting for tmpfs secrets and executing migrations."""
    if profile == "standalone":
        return f"""#!/bin/sh
set -e

# Source .env if present
if [ -f ".env" ]; then
  set -a
  . ./.env
  set +a
elif [ -f "/secrets/.env" ]; then
  set -a
  . /secrets/.env
  set +a
fi

# Apply database migrations if alembic exists
if [ -f "alembic.ini" ]; then
  echo "Applying database migrations..."
  alembic upgrade head || echo "Alembic upgrade completed with warnings"
fi

echo "Starting service on port ${{PORT:-{port}}}..."
exec "$@"
"""

    return f"""#!/bin/sh
set -e

# Wait for Vault Agent sidecar to render secrets
echo "Waiting for /secrets/.env..."
while [ ! -f /secrets/.env ]; do
  sleep 1
done

# Source runtime secrets into environment
set -a
. /secrets/.env
set +a

# Apply database migrations if alembic exists
if [ -f "alembic.ini" ]; then
  echo "Applying database migrations..."
  alembic upgrade head || echo "Alembic upgrade completed with warnings"
fi

echo "Starting service on port ${{PORT:-{port}}}..."
exec "$@"
"""


def generate_env_example(service_name: str, port: int, db: str = "postgres") -> str:
    """Generate 12-factor environment template."""
    clean_service = _clean_ident(service_name)
    lines = [
        f"PORT={port}",
        "ENV=local_dev",
        f"OTEL_SERVICE_NAME={clean_service}",
        "OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318",
    ]
    if db == "postgres":
        lines.append(f"DATABASE_URL=postgresql://postgres:postgres@localhost:5432/{clean_service}")
    return "\n".join(lines) + "\n"


def generate_fastapi_app(service_name: str, db: str = "postgres", profile: str = "infra2") -> str:
    """Generate starter FastAPI application with 1-liner init_service.

    The infra2 profile keeps the default ``strict_otel=True``: its compose file passes ``ENV``
    from the deployment, and an unset ``ENV`` stops the service. The standalone profile passes
    ``strict_otel=False``, because a laptop sets no tier. Its compose file defaults ``ENV`` to
    ``local_dev``, so a deployment must also set a real ``ENV``.
    """
    clean_service = _clean_ident(service_name)
    if profile == "standalone":
        strict_note = (
            "# A laptop sets no ENVIRONMENT, so strict_otel=False lets the service start.\n"
            "# For a deployment, remove it and set ENV to the real tier.\n"
            "# The compose file defaults ENV to local_dev: removing the keyword is not enough.\n"
        )
        strict_option = ", strict_otel=False"
    else:
        strict_note = strict_option = ""
    if db == "postgres":
        return f'''"""Application entry point for {clean_service}."""

import os

from fastapi import FastAPI
from infra2_sdk.fastapi import init_service
from infra2_sdk.runtime.postgres import PostgresSettings, probe_postgres

app = FastAPI(title="{clean_service}")

# The compose file passes an empty DATABASE_URL when the shell sets none.
database_url = os.environ.get("DATABASE_URL", "").strip()
if not database_url:
    raise RuntimeError("DATABASE_URL is required")

# Database reachability probe via infra2_sdk standard PostgreSQL adapter
db_settings = PostgresSettings(dsn=database_url)


def check_db() -> tuple[bool, str]:
    res = probe_postgres(db_settings)
    return (res.status.value == "present", res.detail or "ok")


# Registers /livez, /readyz and /health.
# Starts OpenTelemetry when OTEL_EXPORTER_OTLP_ENDPOINT is set.
{strict_note}init_service(app, name="{clean_service}", db_checker=check_db{strict_option})


@app.get("/")
def read_root():
    return {{"service": "{clean_service}", "status": "running"}}
'''

    return f'''"""Application entry point for {clean_service}."""

from fastapi import FastAPI
from infra2_sdk.fastapi import init_service

app = FastAPI(title="{clean_service}")

# Registers /livez, /readyz and /health.
# Starts OpenTelemetry when OTEL_EXPORTER_OTLP_ENDPOINT is set.
{strict_note}init_service(app, name="{clean_service}"{strict_option})


@app.get("/")
def read_root():
    return {{"service": "{clean_service}", "status": "running"}}
'''


def generate_dockerfile(port: int) -> str:
    """Generate standard lightweight Dockerfile."""
    return f"""FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \\
    curl \\
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh

EXPOSE {port}

ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "{port}"]
"""


def _sdk_release_version() -> str:
    """Return the release part (``major.minor.patch``) of the running SDK version.

    The generated app relies on the behavior of the SDK that generated it.
    A suffix such as ``.dev1``, ``+local``, ``rc1`` or ``.post1`` is dropped.
    Raise ``ValueError`` when the version has no release part.
    """
    sdk_version = infra2_sdk.__version__
    match = re.match(r"\d+\.\d+\.\d+", sdk_version)
    if match is None:
        raise ValueError(f"SDK version {sdk_version!r} has no major.minor.patch release part")
    return match.group()


def generate_requirements_txt(template: str = "fastapi", db: str = "postgres") -> str:
    """Generate minimum starter dependencies.

    The lower bound of ``infra2-sdk`` is the release part of the SDK version that runs
    this function. The bound follows the SDK release without a second version number.
    """
    extras = ["fastapi", "otel"]
    if db == "postgres":
        extras.append("postgres")
    extras_str = ",".join(extras)
    return f"""uvicorn[standard]>=0.28.0,<1.0.0
infra2-sdk[{extras_str}]>={_sdk_release_version()}
"""


def scaffold_service(
    name: str,
    target_dir: Path,
    project: str = "apps",
    port: int = 8000,
    template: str = "fastapi",
    db: str = "postgres",
    profile: str = "infra2",
) -> list[str]:
    """Scaffold a new microservice repository or subdirectory.

    Returns list of created relative file paths.
    """
    if template != "fastapi":
        raise ValueError(f"unsupported template {template!r}: only 'fastapi' exists")
    target_dir.mkdir(parents=True, exist_ok=True)
    created: list[str] = []

    files: dict[str, str] = {
        "compose.yaml": generate_compose_yaml(name, project, port, db, profile=profile),
        "entrypoint.sh": generate_entrypoint_sh(port, profile=profile),
        "Dockerfile": generate_dockerfile(port),
        "requirements.txt": generate_requirements_txt(template=template, db=db),
        "app.py": generate_fastapi_app(name, db=db, profile=profile),
    }

    if profile == "infra2":
        manifest = scaffold_manifest(name, project, db)
        vault_names = {"project": _clean_ident(project), "service": _clean_ident(name)}
        files["deploy.py"] = generate_deploy_py(name, project, port)
        files["secrets.ctmpl"] = render_agent_template(manifest, **vault_names)
        files["vault-policy.hcl"] = render_agent_policy(manifest, **vault_names)
        files["vault-agent.hcl"] = generate_vault_agent_hcl()
    elif profile == "standalone":
        files[".env.example"] = generate_env_example(name, port, db)

    for rel_path, content in files.items():
        file_path = target_dir / rel_path
        file_path.write_text(content, encoding="utf-8")
        if rel_path.endswith(".sh"):
            file_path.chmod(0o755)
        created.append(rel_path)

    return created


def main(argv: Sequence[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "scaffold":
        argv = argv[1:]

    parser = argparse.ArgumentParser(description="Zero-Compromise Microservice Scaffolder")
    parser.add_argument("name", help="Service name (e.g. demo-app)")
    parser.add_argument("--project", default="apps", help="Project identifier (default: apps)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP service port (default: 8000)")
    parser.add_argument(
        "--template",
        choices=["fastapi"],
        default="fastapi",
        help="App template",
    )
    parser.add_argument(
        "--db",
        choices=["postgres", "none"],
        default="postgres",
        help="Database backend",
    )
    parser.add_argument(
        "--profile",
        choices=["infra2", "standalone"],
        default="infra2",
        help="Target profile: infra2 (Dokploy/Vault sidecar) or standalone (pure 12-factor .env)",
    )
    parser.add_argument(
        "--out-dir", default=None, help="Target output directory (default: ./<name>)"
    )

    args = parser.parse_args(argv)
    out = Path(args.out_dir) if args.out_dir else Path(f"./{args.name}")

    try:
        created = scaffold_service(
            name=args.name,
            target_dir=out,
            project=args.project,
            port=args.port,
            template=args.template,
            db=args.db,
            profile=args.profile,
        )
        print(f"✅ Successfully scaffolded '{args.name}' in {out.resolve()}:")
        for f in created:
            print(f"  - {f}")
        return 0
    except Exception as exc:
        print(f"❌ Scaffolding failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
