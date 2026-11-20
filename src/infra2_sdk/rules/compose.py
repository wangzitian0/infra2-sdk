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


_XINFRA2_KEYS = frozenset({"vault"})
_XINFRA2_VAULT_KEYS = frozenset({"name"})
_SERVICE_XVAULT_KEYS = frozenset({"max_wait_seconds"})
# Later slices of infra2-sdk#238 own these service keys. A silent pass-through would
# make Compose ignore them, so the compiler rejects them until that slice ships.
_RESERVED_SERVICE_KEYS = frozenset({"x-routes", "x-capabilities", "x-preview"})
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
exec "$@"
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


def _wrap_vault_service(spec: dict[str, Any], options: dict[str, Any], where: str) -> None:
    _reject_unknown(options, _SERVICE_XVAULT_KEYS, f"{where}.x-vault")
    wait = options.get("max_wait_seconds", _DEFAULT_WAIT_SECONDS)
    if isinstance(wait, bool) or not isinstance(wait, int) or wait <= 0:
        raise ComposeCompileError(f"{where}.x-vault.max_wait_seconds must be a positive integer")
    if "entrypoint" in spec:
        raise ComposeCompileError(f"{where}: x-vault owns `entrypoint`; set `command` instead")
    command = spec.get("command")
    if not isinstance(command, list) or not command:
        raise ComposeCompileError(f"{where}: x-vault needs `command` as a non-empty list")
    spec["entrypoint"] = ["sh", "-c", _dollar(_WAIT_SCRIPT.format(max_wait=wait)), "--"]
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


def _apply_service_defaults(spec: dict[str, Any]) -> None:
    spec.setdefault("restart", "always")
    spec.setdefault(
        "logging", {"driver": "json-file", "options": {"max-size": "5m", "max-file": "2"}}
    )
    if "network_mode" not in spec:
        spec.setdefault("networks", [_NETWORK])


def _compile_mapping(source: Mapping[str, Any], vault_template: str | None) -> dict[str, Any]:
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
    out_services: dict[str, Any] = {}
    if vault_cfg is not None:
        out_services[_VAULT_SIDECAR] = _vault_sidecar(vault_cfg["name"], vault_template)
    for svc_name, spec in services.items():
        where = f"services.{svc_name}"
        spec = _require_mapping(spec, where)
        reserved = sorted(set(spec) & _RESERVED_SERVICE_KEYS)
        if reserved:
            raise ComposeCompileError(f"{where}: {reserved} is not supported yet (infra2-sdk#238)")
        options = spec.pop("x-vault", None)
        if options is not None:
            _wrap_vault_service(spec, _require_mapping(options, f"{where}.x-vault"), where)
        out_services[svc_name] = spec
    for spec in out_services.values():
        _apply_service_defaults(spec)
    doc["services"] = out_services
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


def compile_compose(source: str | Mapping[str, Any], *, vault_template: str | None = None) -> str:
    """Compile an `x-infra2` compose source into plain Docker Compose YAML.

    The function is pure: it does no file, network, or environment access, and it returns
    the same bytes for the same input. It applies the baseline service defaults and expands
    `x-vault` into one inline `vault-agent` sidecar. `vault_template` holds the rendered
    Vault Agent template text. Any unknown `x-infra2` or `x-vault` key raises
    `ComposeCompileError`.
    """
    if isinstance(source, str):
        yaml = _yaml()
        try:
            loaded = yaml.safe_load(source)
        except yaml.YAMLError as exc:
            raise ComposeCompileError(f"YAML parse error: {exc}") from exc
        source = _require_mapping(loaded, "compose source")
    return _dump(_compile_mapping(source, vault_template))


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
