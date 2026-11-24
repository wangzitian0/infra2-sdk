"""Pure rules for compose file validation and resource ceiling enforcement."""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from infra2_sdk._wire import _yaml

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtb]?b?)\s*$", re.I)
_UNITS = {
    "": 1,
    "b": 1,
    "k": 2**10,
    "kb": 2**10,
    "m": 2**20,
    "mb": 2**20,
    "g": 2**30,
    "gb": 2**30,
    "t": 2**40,
    "tb": 2**40,
}
_IMAGE_RE = re.compile(r"^\s*image:\s*(\S+)\s*$")


def _is_memory_ceiling(value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int | float):
        return value > 0
    if not isinstance(value, str):
        return False
    match = _SIZE_RE.match(value)
    return bool(match and float(match.group(1)) * _UNITS.get(match.group(2).lower(), 0) > 0)


def _tag_of_image_ref(ref: str) -> str:
    last = ref.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[-1] if ":" in last else ""


def find_bare_latest_violations(compose_text: str) -> list[str]:
    violations: list[str] = []
    for line in compose_text.splitlines():
        match = _IMAGE_RE.match(line)
        if not match:
            continue
        ref = match.group(1)
        if "${" not in ref and "@sha256:" not in ref and _tag_of_image_ref(ref) == "latest":
            violations.append(ref)
    return violations


class ComposeCompileError(ValueError):
    """Raised when an `x-infra2` compose source is invalid. The compiler fails closed."""


_XINFRA2_KEYS = frozenset({"vault", "preview"})
_XINFRA2_PREVIEW_KEYS = frozenset({"db_container"})
_SERVICE_XPREVIEW_KEYS = frozenset({"remove", "database_url", "environment"})
_MODES = frozenset({"deploy", "preview"})
# One constant for the image of the ephemeral preview database (#238 Slice 4).
PREVIEW_DB_IMAGE = "postgres:16-alpine"
_PREVIEW_DB = "db"
_PREVIEW_DB_VOLUME = "preview_db"
_PREVIEW_DB_URL_RE = re.compile(r"^postgresql(\+[a-z0-9]+)?$")
_XINFRA2_VAULT_KEYS = frozenset({"name"})
_SERVICE_XVAULT_KEYS = frozenset({"max_wait_seconds"})
_ROUTES_KEYS = frozenset({"service", "port", "routes"})
_ROUTE_KEYS = frozenset({"name", "rule", "priority", "tls", "strip_prefix", "sso"})
# Compose interpolates these in the labels on purpose, so the SDK does not escape them.
_SUFFIX = "${ENV_DOMAIN_SUFFIX}"
_SSO_ADDRESS = (
    "http://platform-authentik-server${ENV_SUFFIX}:9000/outpost.goauthentik.io/auth/traefik"
)
_SSO_HEADERS = (
    "X-authentik-username,X-authentik-groups,X-authentik-email,X-authentik-name,X-authentik-uid"
)
_VAULT_SIDECAR = "vault-agent"
_VAULT_IMAGE = "hashicorp/vault:1.15"
_DEFAULT_WAIT_SECONDS = 300
_NETWORK = "dokploy-network"
_SHA_KEY = "CONFIGURATION_SHA256"
_SHA_REF = "${CONFIGURATION_SHA256:-}"
_VAULT_AGENT_HCL = """\
vault {
  # VAULT_ADDR is read from the container environment.
}
auto_auth {
  method "approle" {
    config = {
      role_id_file_path                   = "/vault/role_id"
      secret_id_file_path                 = "/vault/secret_id"
      remove_secret_id_file_after_reading = false
    }
  }

  sink {
    type = "file"
    config = {
      path = "/vault/.token"
    }
  }

  exit_on_err = true
}

template_config {
  static_secret_render_interval = "5m"
  exit_on_retry_failure = true
}

template {
  source      = "/vault/secrets.ctmpl"
  destination = "/vault/secrets/.env"
  perms       = 0644
  # A required key missing in Vault must fail the render instead of rendering `%!q(<nil>)`.
  error_on_missing_key = true
}
"""
_SIDECAR_SCRIPT = """\
rm -f /vault/secrets/.env /vault/.token /vault/role_id /vault/secret_id 2>/dev/null || true
if [ -z "$VAULT_ROLE_ID" ] || [ -z "$VAULT_SECRET_ID" ]; then
  echo "VAULT_ROLE_ID and VAULT_SECRET_ID are required"
  exit 1
fi
if [ -z "$VAULT_ADDR" ]; then
  echo "VAULT_ADDR is required"
  exit 1
fi
echo "$VAULT_ROLE_ID" > /vault/role_id
echo "$VAULT_SECRET_ID" > /vault/secret_id
printf '%s\\n' "$VAULT_AGENT_HCL" > /vault/agent.hcl
printf '%s\\n' "$VAULT_SECRETS_TEMPLATE" > /vault/secrets.ctmpl
# AppRole auto_auth: vault-agent logs in and renews the token itself.
exec vault agent -config=/vault/agent.hcl
"""
_SIDECAR_HEALTHCHECK = (
    'test -s /vault/secrets/.env && ! grep -q "<no value>" /vault/secrets/.env'
    " && test -s /vault/.token || exit 1\n"
    'if [ -z "$(find /tmp/.vault_hc -mmin -5 2>/dev/null)" ]; then\n'
    '  wget -q -T 3 --header="X-Vault-Token: $(cat /vault/.token)"'
    ' "$VAULT_ADDR/v1/auth/token/lookup-self" -O /dev/null || exit 1\n'
    "  touch /tmp/.vault_hc\n"
    "fi\n"
)
_WAIT_SCRIPT = """\
set -e
waited=0
max_wait="{max_wait}"
while [ ! -s /secrets/.env ] && [ "$waited" -lt "$max_wait" ]; do
  echo "Waiting for secrets file..."
  sleep 2
  waited=$((waited + 2))
done
if [ ! -s /secrets/.env ]; then
  echo "ERROR: /secrets/.env is missing or empty"
  exit 1
fi
set -a
. /secrets/.env
set +a
{overrides}exec "$@"
"""


def _dollar(text: str) -> str:
    """Escape `$` so Compose does not interpolate text that the SDK injects."""
    return text.replace("$", "$$")


def _require_mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ComposeCompileError(f"{where} must be a mapping")
    return value


def _reject_unknown(block: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise ComposeCompileError(f"{where}: unknown key(s) {unknown}; allowed: {sorted(allowed)}")


def _vault_sidecar(name: str, template: str) -> dict[str, Any]:
    return {
        "init": True,
        "image": _VAULT_IMAGE,
        "container_name": f"{name}-vault-agent${{ENV_SUFFIX}}",
        "restart": "always",
        "mem_limit": "128m",
        "cpu_shares": 256,
        "entrypoint": ["sh", "-c", _dollar(_SIDECAR_SCRIPT)],
        "environment": {
            "VAULT_ADDR": "${VAULT_ADDR}",
            "VAULT_ROLE_ID": "${VAULT_ROLE_ID:-}",
            "VAULT_SECRET_ID": "${VAULT_SECRET_ID:-}",
            "ENV": "${ENV}",
            # A template reads it with `env "ENV_SUFFIX"`, for example to build DATABASE_URL.
            "ENV_SUFFIX": "${ENV_SUFFIX:-}",
            _SHA_KEY: _SHA_REF,
            "VAULT_AGENT_HCL": _dollar(_VAULT_AGENT_HCL),
            "VAULT_SECRETS_TEMPLATE": _dollar(template),
        },
        "healthcheck": {
            "test": ["CMD-SHELL", _dollar(_SIDECAR_HEALTHCHECK)],
            "interval": "30s",
            "timeout": "5s",
            "retries": 12,
            "start_period": "10s",
        },
        "volumes": ["secrets:/vault/secrets"],
        "labels": [
            "infra2.vault.config.sha256=" + hashlib.sha256(_VAULT_AGENT_HCL.encode()).hexdigest(),
            "infra2.vault.template.sha256=" + hashlib.sha256(template.encode()).hexdigest(),
        ],
    }


def _wrap_vault_service(
    spec: dict[str, Any], options: dict[str, Any], where: str, overrides: str = ""
) -> None:
    _reject_unknown(options, _SERVICE_XVAULT_KEYS, f"{where}.x-vault")
    wait = options.get("max_wait_seconds", _DEFAULT_WAIT_SECONDS)
    if isinstance(wait, bool) or not isinstance(wait, int) or wait <= 0:
        raise ComposeCompileError(f"{where}.x-vault.max_wait_seconds must be a positive integer")
    if "entrypoint" in spec:
        raise ComposeCompileError(f"{where}: x-vault owns `entrypoint`; set `command` instead")
    command = spec.get("command")
    if not isinstance(command, list) or not command:
        raise ComposeCompileError(f"{where}: x-vault needs `command` as a non-empty list")
    spec["entrypoint"] = [
        "sh",
        "-c",
        _dollar(_WAIT_SCRIPT.format(max_wait=wait, overrides=overrides)),
        "--",
    ]
    depends = spec.get("depends_on", [])
    if isinstance(depends, list):
        if _VAULT_SIDECAR not in depends:
            spec["depends_on"] = [*depends, _VAULT_SIDECAR]
    elif isinstance(depends, dict):
        depends.setdefault(_VAULT_SIDECAR, {"condition": "service_started"})
    else:
        raise ComposeCompileError(f"{where}.depends_on must be a list or mapping")
    volumes = spec.setdefault("volumes", [])
    if not isinstance(volumes, list):
        raise ComposeCompileError(f"{where}.volumes must be a list")
    if "secrets:/secrets:ro" not in volumes:
        volumes.append("secrets:/secrets:ro")
    environment = spec.get("environment", {})
    if not isinstance(environment, dict):
        raise ComposeCompileError(f"{where}.environment must be a mapping when x-vault is set")
    # An explicit value in the source wins over this default.
    spec["environment"] = {_SHA_KEY: _SHA_REF, **environment}


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ComposeCompileError(f"{where} must be a non-empty string")
    return value


def _route_labels(spec: dict[str, Any], options: dict[str, Any], where: str) -> None:
    """Append the Traefik labels of one `x-routes` block. Label order has no meaning."""
    where = f"{where}.x-routes"
    _reject_unknown(options, _ROUTES_KEYS, where)
    service = _text(options.get("service"), f"{where}.service")
    port = options.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or port <= 0:
        raise ComposeCompileError(f"{where}.port must be a positive integer")
    routes = options.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ComposeCompileError(f"{where}.routes must be a non-empty list")
    existing = spec.get("labels", [])
    if not isinstance(existing, list):
        raise ComposeCompileError(f"{where}: `labels` must be a list when x-routes is set")
    if any(str(label).startswith("traefik.") for label in existing):
        raise ComposeCompileError(f"{where} owns every `traefik.*` label; remove them")
    labels = [
        "traefik.enable=true",
        f"traefik.http.services.{service}{_SUFFIX}.loadbalancer.server.port={port}",
    ]
    names: set[str] = set()
    sso_names: set[str] = set()
    for index, route in enumerate(routes):
        here = f"{where}.routes[{index}]"
        route = _require_mapping(route, here)
        _reject_unknown(route, _ROUTE_KEYS, here)
        name = _text(route.get("name"), f"{here}.name")
        if name in names:
            raise ComposeCompileError(f"{here}.name {name!r} is used twice")
        names.add(name)
        router = f"traefik.http.routers.{name}{_SUFFIX}"
        labels += [
            f"{router}.rule={_text(route.get('rule'), f'{here}.rule')}",
            f"{router}.entrypoints=websecure",
        ]
        tls = route.get("tls", False)
        if not isinstance(tls, bool):
            raise ComposeCompileError(f"{here}.tls must be a boolean")
        if tls:
            labels.append(f"{router}.tls=true")
        labels.append(f"{router}.tls.certresolver=letsencrypt")
        priority = route.get("priority")
        if priority is not None:
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise ComposeCompileError(f"{here}.priority must be an integer")
            labels.append(f"{router}.priority={priority}")
        strip, sso = route.get("strip_prefix"), route.get("sso")
        if strip is not None and sso is not None:
            raise ComposeCompileError(f"{here}: strip_prefix and sso together are not supported")
        middleware = None
        if strip is not None:
            if not _text(strip, f"{here}.strip_prefix").startswith("/"):
                raise ComposeCompileError(f"{here}.strip_prefix must start with `/`")
            middleware = f"{name}-strip"
            labels.append(
                f"traefik.http.middlewares.{middleware}{_SUFFIX}.stripprefix.prefixes={strip}"
            )
        elif sso is not None:
            middleware = _text(sso, f"{here}.sso")
            if middleware not in sso_names:
                sso_names.add(middleware)
                auth = f"traefik.http.middlewares.{middleware}{_SUFFIX}.forwardauth"
                labels += [
                    f"{auth}.address={_SSO_ADDRESS}",
                    f"{auth}.trustForwardHeader=true",
                    f"{auth}.authResponseHeaders={_SSO_HEADERS}",
                ]
        if middleware:
            labels.append(f"{router}.middlewares={middleware}{_SUFFIX}@docker")
        labels.append(f"{router}.service={service}{_SUFFIX}")
    spec["labels"] = [*existing, *labels]


_COLLECTOR = "http://platform-signoz-otel-collector:4318"
_CAPABILITY_KEYS: dict[str, frozenset[str]] = {
    "s3": frozenset({"public"}),
    "openpanel": frozenset({"script"}),
    "otel": frozenset({"service_name", "service_name_var", "attributes_var", "export"}),
    "browser_otel": frozenset(),
}
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _flag(options: dict[str, Any], key: str, where: str) -> bool:
    value = options.get(key, False)
    if not isinstance(value, bool):
        raise ComposeCompileError(f"{where}.{key} must be true or false")
    return value


def _env_name(options: dict[str, Any], key: str, default: str, where: str) -> str:
    value = options.get(key, default)
    if not isinstance(value, str) or not _ENV_NAME_RE.match(value):
        raise ComposeCompileError(f"{where}.{key} must be an UPPER_CASE variable name")
    return value


def _capability_env(name: str, options: dict[str, Any], where: str) -> dict[str, str]:
    """The environment block of one capability. Values keep `${...}` for Compose."""
    if name == "s3":
        env = {"S3_ENDPOINT": "http://platform-s3${ENV_SUFFIX}:9000"}
        if _flag(options, "public", where):
            env = {**env, "S3_PUBLIC_ENDPOINT": "https://s3${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}"}
        return env
    if name == "openpanel":
        env = {
            "OPENPANEL_CLIENT_ID": "${OPENPANEL_CLIENT_ID:-}",
            "OPENPANEL_API_URL": "https://openpanel.${INTERNAL_DOMAIN}/api",
            "OPENPANEL_ENVIRONMENT": "${OPENPANEL_ENVIRONMENT:-${ENV}}",
        }
        if _flag(options, "script", where):
            env = {**env, "OPENPANEL_SCRIPT_URL": "https://openpanel.${INTERNAL_DOMAIN}/op1.js"}
        return env
    if name == "otel":
        service_name = options.get("service_name", "")
        if not isinstance(service_name, str):
            raise ComposeCompileError(f"{where}.service_name must be a string")
        name_var = _env_name(options, "service_name_var", "OTEL_SERVICE_NAME", where)
        attrs_var = _env_name(options, "attributes_var", "OTEL_RESOURCE_ATTRIBUTES", where)
        export = options.get("export", "always")
        if export == "always":
            endpoint = _COLLECTOR
        elif export == "when_issued":
            # Export turns on only when the deploy issued the identity (#1431).
            endpoint = f"${{{attrs_var}:+{_COLLECTOR}}}"
        else:
            raise ComposeCompileError(f"{where}.export must be 'always' or 'when_issued'")
        return {
            "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
            "OTEL_SERVICE_NAME": f"${{{name_var}:-{service_name}}}",
            "OTEL_RESOURCE_ATTRIBUTES": f"${{{attrs_var}:-}}",
        }
    return {
        "NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT": "${NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT:-}",
        "NEXT_PUBLIC_DEPLOYMENT_ENVIRONMENT": "${ENV}",
        "NEXT_PUBLIC_GIT_SHA": "${GIT_COMMIT_SHA:-unknown}",
    }


def _apply_capabilities(spec: dict[str, Any], capabilities: object, where: str) -> None:
    """Merge the environment blocks of `x-capabilities` into the service environment.

    `x-capabilities` is a list of names or a mapping of name to options. A key that the
    service also sets with another value raises: one fact has one source.
    """
    where = f"{where}.x-capabilities"
    if isinstance(capabilities, list):
        if not all(isinstance(item, str) for item in capabilities):
            raise ComposeCompileError(f"{where}: a list holds capability names only")
        if len(set(capabilities)) != len(capabilities):
            raise ComposeCompileError(f"{where}: a capability is listed twice")
        capabilities = {item: {} for item in capabilities}
    blocks = _require_mapping(capabilities, where)
    if not blocks:
        raise ComposeCompileError(f"{where} must name at least one capability")
    environment = spec.setdefault("environment", {})
    if not isinstance(environment, dict):
        raise ComposeCompileError(f"{where}: `environment` must be a mapping")
    for name, options in blocks.items():
        if name not in _CAPABILITY_KEYS:
            raise ComposeCompileError(
                f"{where}: unknown capability {name!r}; known: {sorted(_CAPABILITY_KEYS)}"
            )
        options = {} if options is None else _require_mapping(options, f"{where}.{name}")
        _reject_unknown(options, _CAPABILITY_KEYS[name], f"{where}.{name}")
        for key, value in _capability_env(name, options, f"{where}.{name}").items():
            if key in environment and str(environment[key]) != value:
                raise ComposeCompileError(
                    f"{where}.{name}: environment {key} is set by the service and by the "
                    "capability; remove one"
                )
            environment[key] = value


def _preview_options(spec: dict[str, Any], where: str) -> dict[str, Any]:
    options = spec.pop("x-preview", None)
    if options is None:
        return {}
    options = _require_mapping(options, f"{where}.x-preview")
    _reject_unknown(options, _SERVICE_XPREVIEW_KEYS, f"{where}.x-preview")
    for key in ("remove", "environment"):
        _flag(options, key, f"{where}.x-preview")
    scheme = options.get("database_url")
    if scheme is not None and (not isinstance(scheme, str) or not _PREVIEW_DB_URL_RE.match(scheme)):
        raise ComposeCompileError(
            f"{where}.x-preview.database_url must be a postgresql URL scheme, "
            "for example 'postgresql' or 'postgresql+asyncpg'"
        )
    if options.get("remove") and len(options) > 1:
        raise ComposeCompileError(f"{where}.x-preview: `remove` takes no other key")
    if (scheme is not None or options.get("environment")) and "x-vault" not in spec:
        raise ComposeCompileError(
            f"{where}.x-preview overrides values after /secrets/.env is sourced: it needs x-vault"
        )
    return options


def _preview_overrides(options: dict[str, Any], db_container: str) -> str:
    """Shell lines that run after /secrets/.env is sourced, so they win over Vault values."""
    lines = []
    scheme = options.get("database_url")
    if scheme is not None:
        url = f"{scheme}://ephemeral:ephemeral@{db_container}:5432/preview"
        lines.append(f'export DATABASE_URL="{url}"')
    if scheme is not None or options.get("environment"):
        lines.append('export ENVIRONMENT="$ENV"')
    return "".join(f"{line}\n" for line in lines)


def _preview_db(db_container: str) -> dict[str, Any]:
    return {
        "image": PREVIEW_DB_IMAGE,
        "container_name": db_container,
        "mem_limit": "1g",
        "environment": {
            "POSTGRES_USER": "ephemeral",
            "POSTGRES_PASSWORD": "ephemeral",
            "POSTGRES_DB": "preview",
            "PGDATA": "/var/lib/postgresql/data/pgdata",
        },
        "healthcheck": {
            "test": ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"],
            "start_period": "20s",
            "interval": "10s",
            "retries": 10,
            "timeout": "5s",
        },
        "volumes": [f"{_PREVIEW_DB_VOLUME}:/var/lib/postgresql/data"],
        "labels": ["traefik.enable=false"],
    }


def _apply_service_defaults(spec: dict[str, Any]) -> None:
    spec.setdefault("restart", "always")
    spec.setdefault(
        "logging", {"driver": "json-file", "options": {"max-size": "5m", "max-file": "2"}}
    )
    if "network_mode" not in spec:
        spec.setdefault("networks", [_NETWORK])


def _compile_mapping(
    source: Mapping[str, Any], vault_template: str | None, mode: str = "deploy"
) -> dict[str, Any]:
    if mode not in _MODES:
        raise ComposeCompileError(f"mode must be one of {sorted(_MODES)}, not {mode!r}")
    doc = copy.deepcopy(dict(source))
    infra2 = _require_mapping(doc.pop("x-infra2", None), "x-infra2")
    _reject_unknown(infra2, _XINFRA2_KEYS, "x-infra2")
    services = _require_mapping(doc.get("services"), "services")
    vault_cfg = infra2.get("vault")
    if vault_cfg is not None:
        vault_cfg = _require_mapping(vault_cfg, "x-infra2.vault")
        _reject_unknown(vault_cfg, _XINFRA2_VAULT_KEYS, "x-infra2.vault")
    users = [n for n, spec in services.items() if isinstance(spec, dict) and "x-vault" in spec]
    if users and vault_cfg is None:
        raise ComposeCompileError(f"x-vault used by {users} but x-infra2.vault is missing")
    if vault_cfg is not None:
        name = vault_cfg.get("name")
        if not isinstance(name, str) or not name:
            raise ComposeCompileError("x-infra2.vault.name must be a non-empty string")
        if not isinstance(vault_template, str) or not vault_template:
            raise ComposeCompileError("x-infra2.vault is set: pass a non-empty vault_template")
        if _VAULT_SIDECAR in services:
            raise ComposeCompileError(f"service name {_VAULT_SIDECAR!r} is reserved for x-vault")
    preview_cfg = infra2.get("preview")
    db_container = ""
    if preview_cfg is not None:
        preview_cfg = _require_mapping(preview_cfg, "x-infra2.preview")
        _reject_unknown(preview_cfg, _XINFRA2_PREVIEW_KEYS, "x-infra2.preview")
        db_container = _text(preview_cfg.get("db_container"), "x-infra2.preview.db_container")
    out_services: dict[str, Any] = {}
    if vault_cfg is not None:
        out_services[_VAULT_SIDECAR] = _vault_sidecar(vault_cfg["name"], vault_template)
    removed: list[str] = []
    wants_db = False
    for svc_name, spec in services.items():
        where = f"services.{svc_name}"
        spec = _require_mapping(spec, where)
        preview = _preview_options(spec, where)
        if preview.get("database_url") is not None:
            if not db_container:
                raise ComposeCompileError(
                    f"{where}.x-preview.database_url needs x-infra2.preview.db_container"
                )
            wants_db = True
        if mode == "preview" and preview.get("remove"):
            removed.append(svc_name)
            continue
        overrides = _preview_overrides(preview, db_container) if mode == "preview" else ""
        options = spec.pop("x-vault", None)
        if options is not None:
            _wrap_vault_service(
                spec, _require_mapping(options, f"{where}.x-vault"), where, overrides
            )
        if mode == "preview" and preview.get("database_url") is not None:
            depends = spec.get("depends_on", [])
            if isinstance(depends, list):
                spec["depends_on"] = {
                    **{name: {"condition": "service_started"} for name in depends},
                    _PREVIEW_DB: {"condition": "service_healthy"},
                }
            else:
                depends[_PREVIEW_DB] = {"condition": "service_healthy"}
        routes = spec.pop("x-routes", None)
        if routes is not None:
            _route_labels(spec, _require_mapping(routes, f"{where}.x-routes"), where)
        capabilities = spec.pop("x-capabilities", None)
        if capabilities is not None:
            _apply_capabilities(spec, capabilities, where)
        out_services[svc_name] = spec
    for svc_name, spec in out_services.items():
        gone = sorted(set(spec.get("depends_on") or []) & set(removed))
        if gone:
            raise ComposeCompileError(
                f"services.{svc_name} depends on {gone}, which x-preview removes in preview"
            )
    if mode == "preview" and wants_db:
        if _PREVIEW_DB in out_services:
            raise ComposeCompileError(f"service name {_PREVIEW_DB!r} is reserved in preview")
        out_services[_PREVIEW_DB] = _preview_db(db_container)
    for spec in out_services.values():
        _apply_service_defaults(spec)
    doc["services"] = out_services
    if mode == "preview" and wants_db:
        db_volumes = doc.setdefault("volumes", {})
        if not isinstance(db_volumes, dict):
            raise ComposeCompileError("volumes must be a mapping")
        db_volumes.setdefault(_PREVIEW_DB_VOLUME, {})
    networks = doc.setdefault("networks", {})
    if not isinstance(networks, dict):
        raise ComposeCompileError("networks must be a mapping")
    networks.setdefault(_NETWORK, {"external": True})
    if vault_cfg is not None:
        volumes = doc.setdefault("volumes", {})
        if not isinstance(volumes, dict):
            raise ComposeCompileError("volumes must be a mapping")
        volumes.setdefault(
            "secrets",
            {"driver_opts": {"type": "tmpfs", "device": "tmpfs", "o": "size=10m,mode=0755"}},
        )
    return doc


def _dump(doc: Mapping[str, Any]) -> str:
    yaml = _yaml()

    class _Dumper(yaml.SafeDumper):
        pass

    def _str(dumper: Any, value: str) -> Any:
        style = "|" if "\n" in value and value.endswith("\n") and value != "\n" else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    _Dumper.add_representer(str, _str)
    return yaml.dump(doc, Dumper=_Dumper, sort_keys=False, width=10**6, allow_unicode=True)


def compile_compose(
    source: str | Mapping[str, Any],
    *,
    vault_template: str | None = None,
    mode: str = "deploy",
) -> str:
    """Compile an `x-infra2` compose source into plain Docker Compose YAML.

    The function is pure: it does no file, network, or environment access, and it returns
    the same bytes for the same input. It applies the baseline service defaults and expands
    `x-vault` into one inline `vault-agent` sidecar, `x-routes` into Traefik labels and
    `x-capabilities` (`s3`, `openpanel`, `otel`, `browser_otel`) into environment blocks.
    `vault_template` holds the rendered Vault Agent template text. `mode="preview"` applies
    each service `x-preview` block (`remove`, `database_url`, `environment`) and adds one
    `db` service of `PREVIEW_DB_IMAGE`; `mode="deploy"` drops those blocks. Any unknown
    `x-infra2`, `x-vault`, `x-routes`, `x-preview` or capability key raises
    `ComposeCompileError`.
    """
    if isinstance(source, str):
        yaml = _yaml()
        try:
            loaded = yaml.safe_load(source)
        except yaml.YAMLError as exc:
            raise ComposeCompileError(f"YAML parse error: {exc}") from exc
        source = _require_mapping(loaded, "compose source")
    return _dump(_compile_mapping(source, vault_template, mode))


@dataclass(frozen=True)
class _ComposeReport:
    path: str
    bare_latest_violations: tuple[str, ...]
    compliant_services: tuple[str, ...]
    unlimited_services: tuple[str, ...]
    errors: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not (self.bare_latest_violations or self.unlimited_services or self.errors)


def _inspect_service_resource_limits(
    services: Mapping[str, Any] | str | Path,
) -> tuple[list[str], list[str]]:
    if isinstance(services, (str, Path)):
        report = inspect_compose(services)
        if report.errors:
            raise ValueError(f"Failed to inspect compose: {'; '.join(report.errors)}")
        return list(report.compliant_services), list(report.unlimited_services)

    if (
        isinstance(services, Mapping)
        and "services" in services
        and isinstance(services["services"], Mapping)
    ):
        services = services["services"]

    compliant: list[str] = []
    non_compliant: list[str] = []
    for name, spec in sorted(services.items()):
        if not isinstance(spec, dict):
            non_compliant.append(name)
        elif "extends" in spec:
            continue
        elif _is_memory_ceiling(spec.get("mem_limit")):
            compliant.append(name)
        else:
            non_compliant.append(name)
    return compliant, non_compliant


def inspect_compose(path_or_text: str | Path) -> _ComposeReport:
    path_str = str(path_or_text)
    is_path = isinstance(path_or_text, Path) or (
        isinstance(path_or_text, str)
        and "\n" not in path_or_text
        and (
            Path(path_or_text).exists()
            or "/" in path_or_text
            or "\\" in path_or_text
            or path_or_text.endswith((".yaml", ".yml"))
        )
    )
    if is_path:
        try:
            text = Path(path_or_text).read_text(encoding="utf-8")
        except OSError as exc:
            return _ComposeReport(path_str, (), (), (), (f"cannot read file: {exc}",))
    else:
        text = str(path_or_text)
        path_str = "<inline>"

    bare_violations = tuple(find_bare_latest_violations(text))
    yaml = _yaml()
    try:
        docs = [d for d in yaml.safe_load_all(text) if d]
    except yaml.YAMLError as exc:
        return _ComposeReport(path_str, bare_violations, (), (), (f"YAML parse error: {exc}",))

    compliant: list[str] = []
    unlimited: list[str] = []
    for doc in docs:
        if not isinstance(doc, dict) or ("include" in doc and not doc.get("services")):
            continue
        if "x-infra2" in doc:
            try:
                # The template text does not change a service's memory ceiling.
                doc = _compile_mapping(doc, "inspect")
            except ComposeCompileError as exc:
                return _ComposeReport(path_str, bare_violations, (), (), (str(exc),))
        services = doc.get("services")
        if isinstance(services, dict):
            comp, unlim = _inspect_service_resource_limits(services)
            compliant.extend(comp)
            unlimited.extend(unlim)

    return _ComposeReport(
        path=path_str,
        bare_latest_violations=bare_violations,
        compliant_services=tuple(sorted(compliant)),
        unlimited_services=tuple(sorted(unlimited)),
    )


__all__ = [
    "ComposeCompileError",
    "compile_compose",
    "inspect_compose",
]
