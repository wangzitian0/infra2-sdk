"""Secret-store adapters and the manifest-driven resolver.

Two open protocols, one resolver, two renderers. The resolver is the only writer of the
deployment secret store: human-class values are copied from the human store (1Password),
runtime-class values are generated once, release/decision values never enter the store.
Every operation is idempotent (read, diff, write only what differs) and reports names only.
"""

from __future__ import annotations

import json
import secrets as _secrets
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from infra2_sdk._transport import HttpResponse, HttpTransport, urllib_transport
from infra2_sdk.runtime.config_schema import (
    EnvironmentField,
    EnvironmentManifest,
    FieldSource,
    ReconcileReport,
    reconcile,
)

VAULT_USER_AGENT = "infra2-sdk/secrets (+https://github.com/wangzitian0/infra2-sdk)"


class SecretsError(RuntimeError):
    """A backend refused or failed; the message never carries a secret value."""


class ReadOnlyBackendError(SecretsError):
    pass


@dataclass(frozen=True)
class WriteResult:
    changed: tuple[str, ...] = ()

    @property
    def wrote(self) -> bool:
        return bool(self.changed)


class SecretsBackend(Protocol):
    def read(self, path: str) -> Mapping[str, str]: ...

    def write(self, path: str, values: Mapping[str, str]) -> WriteResult: ...


# --------------------------------------------------------------------------- paths


def vault_path(project: str, env: str, service: str) -> str:
    """Mount-relative KV path; the same coordinate every infra2 service already uses."""
    return f"{project}/{env}/{service}"


def op_item(project: str, env: str, service: str, *, scope: str = "env") -> str:
    """1Password item title for a service's human-class values.

    ``project/env/service`` per environment, ``project/shared/service`` for
    project-scoped values, and ``bootstrap/service`` for the bootstrap project, which
    has no environment layer.
    """
    if project == "bootstrap":
        return f"bootstrap/{service}"
    return f"{project}/{'shared' if scope == 'project' else env}/{service}"


# --------------------------------------------------------------------------- backends


class EnvBackend:
    """Read-only view over process environment (and an optional dotenv file).

    The only backend an application needs: with it a standalone process or CI resolves the
    same manifest without any infrastructure.
    """

    def __init__(self, environ: Mapping[str, str], *, dotenv_path: str | None = None) -> None:
        merged: dict[str, str] = {}
        if dotenv_path:
            merged.update(_parse_dotenv(dotenv_path))
        merged.update({k: v for k, v in environ.items() if isinstance(v, str)})
        self._values = merged

    def read(self, path: str) -> Mapping[str, str]:
        return dict(self._values)

    def write(self, path: str, values: Mapping[str, str]) -> WriteResult:
        raise ReadOnlyBackendError("the environment backend is read-only")


class VaultKvBackend:
    """HashiCorp Vault KV v2 over its HTTP API. Writes are merge patches of changed keys."""

    def __init__(
        self,
        address: str,
        *,
        token: str,
        mount: str = "secret",
        transport: HttpTransport | None = None,
        user_agent: str = VAULT_USER_AGENT,
    ) -> None:
        if not address.startswith(("https://", "http://")):
            raise ValueError("address must be an http(s) URL")
        if not token:
            raise ValueError("token is required")
        self._address = address.rstrip("/")
        self._token = token
        self._mount = mount.strip("/")
        self._send = transport or urllib_transport()
        self._user_agent = user_agent

    @classmethod
    def from_environ(
        cls, environ: Mapping[str, str], *, transport: HttpTransport | None = None
    ) -> VaultKvBackend:
        """VAULT_ADDR + VAULT_TOKEN, or VAULT_ADDR + VAULT_ROLE_ID/VAULT_SECRET_ID (AppRole)."""
        address = environ.get("VAULT_ADDR", "").strip()
        if not address:
            raise SecretsError("VAULT_ADDR is not set")
        token = environ.get("VAULT_TOKEN", "").strip()
        if not token:
            role_id = environ.get("VAULT_ROLE_ID", "").strip()
            secret_id = environ.get("VAULT_SECRET_ID", "").strip()
            if not (role_id and secret_id):
                raise SecretsError("VAULT_TOKEN or VAULT_ROLE_ID/VAULT_SECRET_ID is required")
            token = cls.login_approle(address, role_id, secret_id, transport=transport)
        return cls(address, token=token, transport=transport)

    @staticmethod
    def login_approle(
        address: str, role_id: str, secret_id: str, *, transport: HttpTransport | None = None
    ) -> str:
        send = transport or urllib_transport()
        body = json.dumps({"role_id": role_id, "secret_id": secret_id}).encode("utf-8")
        response = send(
            "POST",
            f"{address.rstrip('/')}/v1/auth/approle/login",
            {"Content-Type": "application/json", "User-Agent": VAULT_USER_AGENT},
            body,
        )
        if response.status != 200:
            raise SecretsError(f"AppRole login failed with HTTP {response.status}")
        token = _json(response).get("auth", {}).get("client_token", "")
        if not token:
            raise SecretsError("AppRole login returned no client token")
        return str(token)

    def _url(self, kind: str, path: str) -> str:
        return f"{self._address}/v1/{self._mount}/{kind}/{path.strip('/')}"

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {"X-Vault-Token": self._token, "User-Agent": self._user_agent}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def read(self, path: str) -> Mapping[str, str]:
        response = self._send("GET", self._url("data", path), self._headers(), None)
        if response.status == 404:
            return {}
        if response.status != 200:
            raise SecretsError(f"Vault read of {path} failed with HTTP {response.status}")
        data = _json(response).get("data", {}).get("data", {})
        return {str(k): "" if v is None else str(v) for k, v in data.items()}

    def read_version(self, path: str) -> int:
        response = self._send("GET", self._url("data", path), self._headers(), None)
        if response.status == 404:
            return 0
        if response.status != 200:
            raise SecretsError(f"Vault read of {path} failed with HTTP {response.status}")
        return int(_json(response).get("data", {}).get("metadata", {}).get("version", 0))

    def write(self, path: str, values: Mapping[str, str]) -> WriteResult:
        current = self.read(path)
        changed = {k: v for k, v in values.items() if current.get(k) != v}
        if not changed:
            return WriteResult()
        if current:
            response = self._send(
                "PATCH",
                self._url("data", path),
                self._headers("application/merge-patch+json"),
                json.dumps({"data": changed}).encode("utf-8"),
            )
        else:
            response = self._send(
                "POST",
                self._url("data", path),
                self._headers("application/json"),
                json.dumps({"data": dict(values)}).encode("utf-8"),
            )
        if response.status not in (200, 204):
            raise SecretsError(f"Vault write to {path} failed with HTTP {response.status}")
        return WriteResult(tuple(sorted(changed)))


Runner = Callable[..., subprocess.CompletedProcess[str]]


class OnePasswordBackend:
    """1Password via the ``op`` CLI. A path is an item title; values are labelled fields.

    Item titles may contain ``/`` (``project/env/service``), which ``op://`` references
    cannot express, so this adapter only ever addresses items by title or id.
    """

    def __init__(
        self, vault: str = "Infra2", *, runner: Runner = subprocess.run, category: str = "Login"
    ) -> None:
        self._vault = vault
        self._run = runner
        self._category = category

    def _op(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = self._run(["op", *args], capture_output=True, text=True)
        if check and result.returncode != 0:
            tail = (result.stderr or "").strip().splitlines()[-1:] or ["no stderr"]
            raise SecretsError(f"op {args[0]} {args[1]} failed: {tail[0][:160]}")
        return result

    def _item(self, title: str) -> dict[str, Any] | None:
        result = self._op(
            "item", "get", title, f"--vault={self._vault}", "--format=json", check=False
        )
        if result.returncode != 0:
            if (
                "isn't an item" in (result.stderr or "")
                or "not found" in (result.stderr or "").lower()
            ):
                return None
            tail = (result.stderr or "").strip().splitlines()[-1:] or ["no stderr"]
            raise SecretsError(f"op item get failed: {tail[0][:160]}")
        return json.loads(result.stdout)

    def read(self, path: str) -> Mapping[str, str]:
        item = self._item(path)
        if item is None:
            return {}
        values: dict[str, str] = {}
        for field in item.get("fields", []):
            label = field.get("label") or field.get("id")
            if not label or field.get("purpose") == "NOTES":
                continue
            values[str(label)] = str(field.get("value") or "")
        return values

    def write(self, path: str, values: Mapping[str, str]) -> WriteResult:
        item = self._item(path)
        if item is None:
            assignments = [f"{label}[password]={value}" for label, value in values.items()]
            self._op(
                "item",
                "create",
                f"--vault={self._vault}",
                f"--category={self._category}",
                f"--title={path}",
                *assignments,
            )
            return WriteResult(tuple(sorted(values)))
        existing = {
            (f.get("label") or f.get("id")): f
            for f in item.get("fields", [])
            if f.get("label") or f.get("id")
        }
        assignments: list[str] = []
        changed: list[str] = []
        for label, value in values.items():
            current = existing.get(label)
            if current is not None and str(current.get("value") or "") == value:
                continue
            section = (current or {}).get("section") or {}
            prefix = section.get("label") or section.get("id")
            target = f"{prefix}.{label}" if prefix else label
            assignments.append(f"{target}[password]={value}")
            changed.append(label)
        if assignments:
            self._op("item", "edit", str(item["id"]), f"--vault={self._vault}", *assignments)
        return WriteResult(tuple(sorted(changed)))


# --------------------------------------------------------------------------- resolver


@dataclass(frozen=True)
class SyncReport:
    changed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "changed": list(self.changed),
            "unchanged": list(self.unchanged),
            "missing": list(self.missing),
        }


def generate_secret(field: EnvironmentField) -> str:
    """Default runtime-secret generator: 43 URL-safe characters (256 bits)."""
    return _secrets.token_urlsafe(32)


class SecretsResolver:
    """Apply one manifest to one ``project/env/service`` coordinate."""

    def __init__(
        self,
        manifest: EnvironmentManifest,
        *,
        project: str,
        service: str,
        env: str,
        store: SecretsBackend,
        human: SecretsBackend | None = None,
        release: Mapping[str, str] | None = None,
        decisions: Mapping[str, str] | None = None,
        generator: Callable[[EnvironmentField], str] = generate_secret,
    ) -> None:
        self.manifest = manifest
        self.project = project
        self.service = service
        self.env = env
        self.store = store
        self.human = human
        self.release = dict(release or {})
        self.decisions = dict(decisions or {})
        self._generate = generator
        self.path = vault_path(project, env, service)
        self._items: dict[str, Mapping[str, str]] = {}

    # human store ------------------------------------------------------------------
    def _human_values(self, field: EnvironmentField) -> Mapping[str, str]:
        if self.human is None:
            raise SecretsError("no human backend configured")
        item = op_item(self.project, self.env, self.service, scope=field.scope)
        if item not in self._items:
            self._items[item] = self.human.read(item)
        return self._items[item]

    def human_expected(self) -> dict[str, str]:
        """Values the human store holds for this service's human-class fields."""
        expected: dict[str, str] = {}
        for field in self.manifest.by_source(FieldSource.HUMAN):
            if not field.store_backed:
                continue
            value = self._human_values(field).get(field.key)
            if value:
                expected[field.key] = value
        return expected

    def sync_human(self) -> SyncReport:
        """Copy human-class values into the store; write only what differs."""
        expected = self.human_expected()
        missing = tuple(
            sorted(
                field.key
                for field in self.manifest.by_source(FieldSource.HUMAN)
                if field.store_backed and field.key not in expected and not field.empty_ok
            )
        )
        return self._apply(expected, missing=missing)

    # runtime ----------------------------------------------------------------------
    def ensure_runtime(self) -> SyncReport:
        """Generate runtime-class values that the store does not hold yet.

        ``empty_ok`` runtime values are optional by contract (a feature the deployment
        may leave off); they are never generated here, only reported as unchanged.
        """
        current = self.store.read(self.path)
        candidates = [
            field
            for field in self.manifest.by_source(FieldSource.RUNTIME)
            if field.store_backed and not field.empty_ok
        ]
        fresh = {
            field.key: self._generate(field) for field in candidates if not current.get(field.key)
        }
        present = tuple(
            sorted(
                field.key
                for field in self.manifest.by_source(FieldSource.RUNTIME)
                if field.store_backed and field.key not in fresh
            )
        )
        if not fresh:
            return SyncReport(unchanged=present)
        result = self.store.write(self.path, fresh)
        return SyncReport(changed=result.changed, unchanged=present)

    def mirror(self) -> SyncReport:
        """Copy runtime values flagged ``mirror_to_1password`` into the human store."""
        if self.human is None:
            raise SecretsError("no human backend configured")
        current = self.store.read(self.path)
        changed: list[str] = []
        unchanged: list[str] = []
        missing: list[str] = []
        for field in self.manifest.by_source(FieldSource.RUNTIME):
            if not field.mirror_to_1password:
                continue
            value = current.get(field.key)
            if not value:
                missing.append(field.key)
                continue
            item = op_item(self.project, self.env, self.service, scope=field.scope)
            if self.human.write(item, {field.key: value}).wrote:
                changed.append(field.key)
            else:
                unchanged.append(field.key)
        return SyncReport(tuple(sorted(changed)), tuple(sorted(unchanged)), tuple(sorted(missing)))

    # deployment -------------------------------------------------------------------
    def compose_env(self) -> dict[str, str]:
        """Release and decision values for the compose environment; never from the store."""
        values: dict[str, str] = {}
        missing: list[str] = []
        for field in self.manifest.by_source(FieldSource.RELEASE, FieldSource.DECISION):
            source = self.release if field.source == FieldSource.RELEASE else self.decisions
            value = source.get(field.env, "")
            if value:
                values[field.env] = value
            elif field.required or not field.empty_ok:
                missing.append(field.env)
        if missing:
            raise SecretsError(f"release/decision values missing: {', '.join(sorted(missing))}")
        return values

    def reconcile(self) -> ReconcileReport:
        expected = self.human_expected() if self.human is not None else None
        return reconcile(self.manifest, self.store.read(self.path), expected=expected)

    # shared -----------------------------------------------------------------------
    def _apply(self, desired: Mapping[str, str], *, missing: tuple[str, ...]) -> SyncReport:
        current = self.store.read(self.path)
        to_write = {k: v for k, v in desired.items() if current.get(k) != v}
        unchanged = tuple(sorted(k for k in desired if k not in to_write))
        if not to_write:
            return SyncReport(unchanged=unchanged, missing=missing)
        result = self.store.write(self.path, to_write)
        return SyncReport(result.changed, unchanged, missing)


# --------------------------------------------------------------------------- renderers


def render_agent_template(
    manifest: EnvironmentManifest,
    *,
    project: str,
    service: str,
    source_env: str | None = None,
    exclude_groups: Sequence[str] = (),
) -> str:
    """Vault Agent ``secrets.ctmpl`` for a service, derived from its manifest.

    Store-backed, ``provided_by`` and ``composed_from`` fields render (``{env:NAME}`` reads
    the agent's host environment). An ``empty_ok`` field is omitted when the store holds
    nothing (looked up with ``index``, safe under ``error_on_missing_key``), so the
    application sees it unset; a missing required value is a direct ``.Data.data.KEY``
    access and fails the render under that option instead of hiding behind ``""``.
    Release, decision, and code fields never appear: the deployment supplies the first two
    and the settings model owns the third. ``source_env`` pins every path to one environment
    (preview aliases read staging).
    """

    excluded = set(exclude_groups)
    env_expr = f'"{source_env}"' if source_env else "$env"
    lines: list[str] = [
        f"{{{{- /* Generated from {manifest.source} by infra2_sdk.secrets. Do not edit. */ -}}}}",
        '{{- $env := env "ENV" -}}',
    ]
    own: list[EnvironmentField] = []
    providers: dict[str, list[EnvironmentField]] = {}
    for field in manifest.fields:
        if field.group in excluded:
            continue
        if field.provided_by:
            providers.setdefault(field.provided_by.split(":", 1)[0], []).append(field)
        elif field.rendered:
            own.append(field)
    for provider, fields in providers.items():
        provider_project, provider_service = provider.split("/", 1)
        lines.append(
            f'{{{{- with secret (printf "secret/data/{provider_project}/%s/{provider_service}" '
            f"{env_expr}) }}}}"
        )
        for field in fields:
            lines.append(_render_line(field, key=field.provided_by.split(":", 1)[1]))
        lines.append("{{- end }}")
    if own:
        lines.append(
            f'{{{{- with secret (printf "secret/data/{project}/%s/{service}" {env_expr}) }}}}'
        )
        for field in own:
            lines.append(_render_line(field, key=field.key))
        lines.append("{{- end }}")
    return "\n".join(lines) + "\n"


def render_agent_policy(
    manifest: EnvironmentManifest,
    *,
    project: str,
    service: str,
    source_env: str | None = None,
) -> str:
    """Vault policy granting exactly the reads the template needs. ``{{env}}`` is the
    placeholder ``vault.setup-approle`` substitutes per environment."""

    env = source_env or "{{env}}"
    paths = [f"{project}/{env}/{service}"]
    for field in manifest.fields:
        if field.provided_by:
            provider_project, provider_service = field.provided_by.split(":", 1)[0].split("/", 1)
            candidate = f"{provider_project}/{env}/{provider_service}"
            if candidate not in paths:
                paths.append(candidate)
    blocks = [f"# Generated from {manifest.source} by infra2_sdk.secrets. Do not edit."]
    for path in paths:
        blocks.append(f'path "secret/data/{path}" {{\n  capabilities = ["read"]\n}}')
        blocks.append(f'path "secret/metadata/{path}" {{\n  capabilities = ["read", "list"]\n}}')
    blocks.append('path "auth/token/lookup-self" {\n  capabilities = ["read"]\n}')
    return "\n\n".join(blocks) + "\n"


def _render_line(field: EnvironmentField, *, key: str) -> str:
    if field.composed_from:
        fmt = field.composed_from
        args: list[str] = []
        for name in field.composed_keys:
            fmt = fmt.replace(f"{{{name}}}", "%s")
            args.append(f".Data.data.{name}")
        for name in field.composed_env:
            fmt = fmt.replace(f"{{env:{name}}}", "%s")
            args.append(f'(env "{name}")')
        return f'{field.env}={{{{ printf "%q" (printf "{fmt}" {" ".join(args)}) }}}}'
    if field.empty_ok:
        # Omit the line when the store holds nothing: the application sees the variable
        # as unset and applies its own default, and nothing ever renders as "". The
        # lookup goes through ``index`` so that an agent running with
        # ``error_on_missing_key = true`` (required keys fail the render) does not treat
        # an absent optional key as an error: ``index`` returns the zero value, while
        # ``.Data.data.KEY`` on a missing key is exactly what that option rejects.
        return f'{{{{ with index .Data.data "{key}" }}}}{field.env}={{{{ printf "%q" . }}}}{{{{ end }}}}'
    return f'{field.env}={{{{ printf "%q" .Data.data.{key} }}}}'


# --------------------------------------------------------------------------- helpers


def _json(response: HttpResponse) -> dict[str, Any]:
    try:
        parsed = json.loads(response.body.decode("utf-8") or "{}")
    except ValueError as error:
        raise SecretsError("backend returned malformed JSON") from error
    return parsed if isinstance(parsed, dict) else {}


def _parse_dotenv(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.removeprefix("export ").strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key] = value
    except FileNotFoundError:
        return {}
    return values
