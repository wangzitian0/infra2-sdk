"""Scaffolding engine and CLI for infra2 services."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

import infra2_sdk
from infra2_sdk.runtime.config_schema import EnvironmentField, EnvironmentManifest, _FieldSource
from infra2_sdk.secrets import render_agent_policy, render_agent_template


def _clean_ident(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", value.strip().lower())


def _generate_compose_yaml(
    service_name: str,
    project: str,
    port: int,
    db: str = "postgres",
    profile: str = "infra2",
) -> str:
    svc = _clean_ident(service_name)
    pfx = f"{_clean_ident(project)}-{svc}"
    db_env = "      DATABASE_URL: ${DATABASE_URL:-}\n" if db == "postgres" else ""
    ef_test = "-s" if _scaffold_manifest(service_name, project, db).fields else "-e"
    is_sa = profile == "standalone"
    cname = svc if is_sa else f"{pfx}${{ENV_SUFFIX}}"
    mid = (
        "    env_file:\n      - .env"
        if is_sa
        else "    depends_on:\n      - vault-agent\n    volumes:\n      - secrets:/secrets:ro"
    )
    env_val = "${ENV:-local_dev}" if is_sa else "${ENV}"
    otel_svc = f"${{OTEL_SERVICE_NAME:-{svc}}}" if is_sa else "${OTEL_SERVICE_NAME:-}"
    net_blk = "" if is_sa else "    networks:\n      - dokploy-network\n"
    start_p = "30s" if is_sa else "120s"
    app_blk = f"""  {svc}:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: {cname}
    restart: always
    mem_limit: 256M
    deploy:
      resources:
        limits:
          memory: 256M
{mid}
    environment:
{db_env}      PORT: "{port}"
      ENV: {env_val}
      OTEL_EXPORTER_OTLP_ENDPOINT: ${{OTEL_EXPORTER_OTLP_ENDPOINT:-}}
      OTEL_SERVICE_NAME: {otel_svc}
      OTEL_RESOURCE_ATTRIBUTES: ${{OTEL_RESOURCE_ATTRIBUTES:-}}
{net_blk}    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost:{port}/health || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: {start_p}
"""
    if is_sa:
        return f"services:\n{app_blk}"
    return f"""services:
  vault-agent:
    init: true
    image: hashicorp/vault:1.15
    container_name: {pfx}-vault-agent${{ENV_SUFFIX}}
    restart: always
    mem_limit: 128M
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
      CONFIGURATION_SHA256: ${{CONFIGURATION_SHA256:-}}
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
          test {ef_test} /vault/secrets/.env && \\
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

{app_blk}
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


def _generate_deploy_py(service_name: str, project: str, port: int) -> str:
    svc, prj = _clean_ident(service_name), _clean_ident(project)
    cls_name = "".join(p.capitalize() for p in svc.split("_")) + "Deployer"
    pfx = f"{prj}-{svc}"
    return f'''"""{svc} service deployment — AST-compliant Deployer definition."""

from __future__ import annotations

import sys
from invoke import task

from common.core.facets import ProbeFacet, PublicRouteFacet, SecretsFacet, SignalFacet
from common.deploy.deployer import Deployer, make_tasks

shared_tasks = sys.modules.get("{prj}.{svc}.shared")


class {cls_name}(Deployer):
    service = "{svc}"
    project = "{prj}"
    compose_path = "{prj}/{svc}/compose.yaml"
    service_port = {port}
    subdomain = "{svc}"
    public_route = PublicRouteFacet()

    backups = ()

    probes = (
        ProbeFacet(
            name="{svc}-http",
            kind="http",
            target="http://{pfx}${{ENV_SUFFIX}}:{port}/health",
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
            vault_agent_container="{pfx}-vault-agent${{ENV_SUFFIX}}",
            app_containers=("{pfx}${{ENV_SUFFIX}}",),
            auth_method="approle",
        ),
    )


if shared_tasks:
    _tasks = make_tasks({cls_name}, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
'''


def _scaffold_manifest(
    service_name: str, project: str, db: str = "postgres"
) -> EnvironmentManifest:
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
                source=_FieldSource.HUMAN,
            ),
        )
    return EnvironmentManifest(
        source=f"scaffold:{_clean_ident(project)}/{_clean_ident(service_name)}", fields=fields
    )


def _generate_vault_agent_hcl() -> str:
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


def _generate_entrypoint_sh(port: int, profile: str = "infra2") -> str:
    head = (
        """if [ -f ".env" ]; then
  set -a
  . ./.env
  set +a
elif [ -f "/secrets/.env" ]; then
  set -a
  . /secrets/.env
  set +a
fi"""
        if profile == "standalone"
        else """echo "Waiting for /secrets/.env..."
while [ ! -f /secrets/.env ]; do
  sleep 1
done
set -a
. /secrets/.env
set +a"""
    )
    return f"""#!/bin/sh
set -e

{head}

if [ -f "alembic.ini" ]; then
  echo "Applying database migrations..."
  alembic upgrade head || echo "Alembic upgrade completed with warnings"
fi

echo "Starting service on port ${{PORT:-{port}}}..."
exec "$@"
"""


def _generate_env_example(service_name: str, port: int, db: str = "postgres") -> str:
    svc = _clean_ident(service_name)
    lines = [
        f"PORT={port}",
        "ENV=local_dev",
        f"OTEL_SERVICE_NAME={svc}",
        "OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318",
    ]
    if db == "postgres":
        lines.append(f"DATABASE_URL=postgresql://postgres:postgres@localhost:5432/{svc}")
    return "\n".join(lines) + "\n"


def _generate_fastapi_app(service_name: str, db: str = "postgres", profile: str = "infra2") -> str:
    svc = _clean_ident(service_name)
    if profile == "standalone":
        note = (
            "# A laptop sets no ENVIRONMENT, so strict_otel=False lets the service start.\n"
            "# For a deployment, remove it and set ENV to the real tier.\n"
            "# The compose file defaults ENV to local_dev: removing the keyword is not enough.\n"
        )
        opt = ", strict_otel=False"
    else:
        note = opt = ""
    if db == "postgres":
        imports = (
            "import os\n\nfrom fastapi import FastAPI\n"
            "from infra2_sdk.fastapi import init_service\n"
            "from infra2_sdk.runtime.postgres import PostgresSettings, probe_postgres"
        )
        db_blk = """
database_url = os.environ.get("DATABASE_URL", "").strip()
if not database_url:
    raise RuntimeError("DATABASE_URL is required")

db_settings = PostgresSettings(dsn=database_url)


def check_db() -> tuple[bool, str]:
    res = probe_postgres(db_settings)
    return (res.status.value == "present", res.detail or "ok")
"""
        db_kw = ", db_checker=check_db"
    else:
        imports = "from fastapi import FastAPI\nfrom infra2_sdk.fastapi import init_service"
        db_blk = db_kw = ""
    return f'''"""Application entry point for {svc}."""

{imports}

app = FastAPI(title="{svc}")
{db_blk}

{note}init_service(app, name="{svc}"{db_kw}{opt})


@app.get("/")
def read_root():
    return {{"service": "{svc}", "status": "running"}}
'''


def _generate_dockerfile(port: int) -> str:
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
    m = re.match(r"\d+\.\d+\.\d+", infra2_sdk.__version__)
    if m is None:
        raise ValueError(
            f"SDK version {infra2_sdk.__version__!r} has no major.minor.patch release part"
        )
    return m.group()


def _generate_requirements_txt(template: str = "fastapi", db: str = "postgres") -> str:
    extras = ",".join(["fastapi", "otel", *(["postgres"] if db == "postgres" else [])])
    return f"uvicorn[standard]>=0.28.0,<1.0.0\ninfra2-sdk[{extras}]>={_sdk_release_version()}\n"


def scaffold_service(
    name: str,
    target_dir: Path,
    project: str = "apps",
    port: int = 8000,
    template: str = "fastapi",
    db: str = "postgres",
    profile: str = "infra2",
) -> list[str]:
    if template != "fastapi":
        raise ValueError(f"unsupported template {template!r}: only 'fastapi' exists")
    target_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {
        "compose.yaml": _generate_compose_yaml(name, project, port, db, profile=profile),
        "entrypoint.sh": _generate_entrypoint_sh(port, profile=profile),
        "Dockerfile": _generate_dockerfile(port),
        "requirements.txt": _generate_requirements_txt(template=template, db=db),
        "app.py": _generate_fastapi_app(name, db=db, profile=profile),
    }
    if profile == "infra2":
        mf = _scaffold_manifest(name, project, db)
        vn = {"project": _clean_ident(project), "service": _clean_ident(name)}
        files.update(
            {
                "deploy.py": _generate_deploy_py(name, project, port),
                "secrets.ctmpl": render_agent_template(mf, **vn),
                "vault-policy.hcl": render_agent_policy(mf, **vn),
                "vault-agent.hcl": _generate_vault_agent_hcl(),
            }
        )
    elif profile == "standalone":
        files[".env.example"] = _generate_env_example(name, port, db)
    for rel, content in files.items():
        fp = target_dir / rel
        fp.write_text(content, encoding="utf-8")
        if rel.endswith(".sh"):
            fp.chmod(0o755)
    return list(files)


def main(argv: Sequence[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["scaffold"]:
        argv = argv[1:]
    p = argparse.ArgumentParser(description="Zero-Compromise Microservice Scaffolder")
    p.add_argument("name")
    p.add_argument("--project", default="apps")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--template", choices=["fastapi"], default="fastapi")
    p.add_argument("--db", choices=["postgres", "none"], default="postgres")
    p.add_argument("--profile", choices=["infra2", "standalone"], default="infra2")
    p.add_argument("--out-dir", default=None)
    a = p.parse_args(argv)
    out = Path(a.out_dir or f"./{a.name}")
    try:
        created = scaffold_service(a.name, out, a.project, a.port, a.template, a.db, a.profile)
        print(f"Successfully scaffolded '{a.name}' in {out.resolve()}:")
        for f in created:
            print(f"  - {f}")
        return 0
    except Exception as exc:
        print(f"Scaffolding failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

__all__ = ["main", "scaffold_service"]
