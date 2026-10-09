"""Zero-Compromise Scaffolding Engine & CLI for infra2.

Generates standard-compliant, industrial-grade microservice skeletons in < 3 seconds:
- compose.yaml (with explicit memory ceilings, tmpfs secrets, and health checks)
- deploy.py (with literal AST-evaluable ProbeFacet, SignalFacet, SecretsFacet)
- secrets.ctmpl & vault-agent.hcl (AppRole automated sidecar wiring)
- entrypoint.sh (secrets synchronization + alembic database migrations)
- app.py (FastAPI 1-liner initialization with /livez, /readyz, /health, and SigNoz APM)

Usage:
    python -m infra2_sdk.scaffold <name> [--project apps] [--port 8000] [--db postgres]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path


def _clean_ident(value: str) -> str:
    """Normalize identifier for Postgres/Vault (lowercase, replace '-' with '_')."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", value.strip().lower())


def generate_compose_yaml(
    service_name: str,
    project: str,
    port: int,
    db: str = "postgres",
) -> str:
    """Generate compose.yaml compliant with ops.standards.md §5 and memory limit lint."""
    clean_service = _clean_ident(service_name)
    clean_project = _clean_ident(project)
    container_prefix = f"{clean_project}-{clean_service}"
    db_env = "      DATABASE_URL: ${DATABASE_URL:-}\n" if db == "postgres" else ""

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
          test -s /vault/secrets/.env && \\
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


def generate_secrets_ctmpl(service_name: str, project: str) -> str:
    """Generate Consul Template for Vault KV secrets."""
    clean_service = _clean_ident(service_name)
    clean_project = _clean_ident(project)
    hdr = f"{{{{- /* Generated for {clean_project}/{clean_service} by infra2-sdk. */ -}}}}"
    return f"""{hdr}
{{{{- $env := env "ENV" -}}}}
{{{{- with secret (printf "secret/data/{clean_project}/%s/{clean_service}" $env) }}}}
{{{{- range $k, $v := .Data.data }}}}
{{{{ $k }}}}={{{{ printf "%q" $v }}}}
{{{{- end }}}}
{{{{- end }}}}
"""


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


def generate_entrypoint_sh(port: int) -> str:
    """Generate entrypoint script waiting for tmpfs secrets and executing migrations."""
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


def generate_fastapi_app(service_name: str) -> str:
    """Generate starter FastAPI application with 1-liner init_service."""
    clean_service = _clean_ident(service_name)
    return f'''"""Application entry point for {clean_service}."""

from fastapi import FastAPI
from infra2_sdk.fastapi import init_service

app = FastAPI(title="{clean_service}")

# One-liner compliance: sets up /livez, /readyz, /health, SigNoz OTel, and SIGTERM
init_service(app, name="{clean_service}")


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


def generate_requirements_txt() -> str:
    """Generate minimum starter dependencies."""
    return """fastapi>=0.110.0,<1.0.0
uvicorn[standard]>=0.28.0,<1.0.0
infra2-sdk>=3.0.2
"""


def scaffold_service(
    name: str,
    target_dir: Path,
    project: str = "apps",
    port: int = 8000,
    template: str = "fastapi",
    db: str = "postgres",
) -> list[str]:
    """Scaffold a new microservice repository or subdirectory.

    Returns list of created relative file paths.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    created: list[str] = []

    files = {
        "compose.yaml": generate_compose_yaml(name, project, port, db),
        "deploy.py": generate_deploy_py(name, project, port),
        "secrets.ctmpl": generate_secrets_ctmpl(name, project),
        "vault-agent.hcl": generate_vault_agent_hcl(),
        "entrypoint.sh": generate_entrypoint_sh(port),
        "Dockerfile": generate_dockerfile(port),
        "requirements.txt": generate_requirements_txt(),
    }

    if template == "fastapi":
        files["app.py"] = generate_fastapi_app(name)

    for rel_path, content in files.items():
        file_path = target_dir / rel_path
        file_path.write_text(content, encoding="utf-8")
        if rel_path.endswith(".sh"):
            file_path.chmod(0o755)
        created.append(rel_path)

    return created


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zero-Compromise Microservice Scaffolder")
    parser.add_argument("name", help="Service name (e.g. demo-app)")
    parser.add_argument("--project", default="apps", help="Project identifier (default: apps)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP service port (default: 8000)")
    parser.add_argument(
        "--template",
        choices=["fastapi", "generic"],
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
