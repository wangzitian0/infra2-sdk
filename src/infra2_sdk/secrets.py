"""Secret-store adapters and manifest-driven resolver."""

from __future__ import annotations

import json
import secrets as _secrets
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from infra2_sdk.runtime.config_schema import EnvironmentField, EnvironmentManifest, _FieldSource
from infra2_sdk.transport import HttpResponse, HttpTransport, urllib_transport

_VAULT_USER_AGENT = "infra2-sdk/secrets (+https://github.com/wangzitian0/infra2-sdk)"
_WRITE_MODES = ("patch", "update")


class SecretsError(RuntimeError):
    pass


@dataclass(frozen=True)
class WriteResult:
    changed: tuple[str, ...] = ()

    @property
    def wrote(self) -> bool:
        return bool(self.changed)


class _SecretsBackend(Protocol):
    def read(self, path: str) -> Mapping[str, str]: ...
    def write(self, path: str, values: Mapping[str, str]) -> WriteResult: ...


def vault_path(project: str, env: str, service: str) -> str:
    return f"{project}/{env}/{service}"


def op_item(project: str, env: str, service: str, *, scope: str = "env") -> str:
    return (
        f"bootstrap/{service}"
        if project == "bootstrap"
        else f"{project}/{'shared' if scope == 'project' else env}/{service}"
    )


class VaultKvBackend:
    def __init__(
        self,
        address: str,
        *,
        token: str,
        mount: str = "secret",
        transport: HttpTransport | None = None,
        user_agent: str = _VAULT_USER_AGENT,
        write_mode: str = "patch",
    ) -> None:
        if not address.startswith(("https://", "http://")):
            raise ValueError("address must be an http(s) URL")
        if not token:
            raise ValueError("token is required")
        if write_mode not in _WRITE_MODES:
            raise ValueError(f"write_mode must be one of {_WRITE_MODES}")
        self._address = address.rstrip("/")
        self._token = token
        self._mount = mount.strip("/")
        self._send = transport or urllib_transport()
        self._user_agent = user_agent
        self._write_mode = write_mode

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str],
        *,
        transport: HttpTransport | None = None,
        write_mode: str = "patch",
    ) -> VaultKvBackend:
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
        return cls(address, token=token, transport=transport, write_mode=write_mode)

    @staticmethod
    def login_approle(
        address: str, role_id: str, secret_id: str, *, transport: HttpTransport | None = None
    ) -> str:
        send = transport or urllib_transport()
        resp = send(
            "POST",
            f"{address.rstrip('/')}/v1/auth/approle/login",
            {"Content-Type": "application/json", "User-Agent": _VAULT_USER_AGENT},
            json.dumps({"role_id": role_id, "secret_id": secret_id}).encode("utf-8"),
        )
        if resp.status != 200:
            raise SecretsError(f"AppRole login failed with HTTP {resp.status}")
        if not (token := _json(resp).get("auth", {}).get("client_token", "")):
            raise SecretsError("AppRole login returned no client token")
        return str(token)

    def replace(self, path: str, values: Mapping[str, str]) -> WriteResult:
        cur = dict(self.read(path))
        desired = {str(k): str(v) for k, v in values.items()}
        changed = tuple(
            sorted(set(cur) ^ set(desired) | {k for k, v in desired.items() if cur.get(k) != v})
        )
        if not changed:
            return WriteResult()
        self._post(path, desired)
        return WriteResult(changed)

    def token_status(self) -> _TokenStatus:
        return vault_token_status(self._address, self._token, transport=self._send)

    def _url(self, kind: str, path: str) -> str:
        return f"{self._address}/v1/{self._mount}/{kind}/{path.strip('/')}"

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        h = {"X-Vault-Token": self._token, "User-Agent": self._user_agent}
        if content_type:
            h["Content-Type"] = content_type
        return h

    def _post(self, path: str, doc: Mapping[str, str], *, patch: bool = False) -> None:
        method = "PATCH" if patch else "POST"
        ctype = "application/merge-patch+json" if patch else "application/json"
        resp = self._send(
            method,
            self._url("data", path),
            self._headers(ctype),
            json.dumps({"data": doc}).encode(),
        )
        if resp.status not in (200, 204):
            raise SecretsError(f"Vault write to {path} failed with HTTP {resp.status}")

    def read(self, path: str) -> Mapping[str, str]:
        resp = self._send("GET", self._url("data", path), self._headers(), None)
        if resp.status == 404:
            return {}
        if resp.status != 200:
            raise SecretsError(f"Vault read of {path} failed with HTTP {resp.status}")
        data = _json(resp).get("data", {}).get("data", {})
        return {str(k): "" if v is None else str(v) for k, v in data.items()}

    def write(self, path: str, values: Mapping[str, str]) -> WriteResult:
        cur = self.read(path)
        changed = {k: v for k, v in values.items() if cur.get(k) != v}
        if not changed:
            return WriteResult()
        if cur and self._write_mode == "patch":
            self._post(path, changed, patch=True)
        else:
            self._post(path, {**cur, **changed} if cur else dict(values))
        return WriteResult(tuple(sorted(changed)))


@dataclass(frozen=True)
class _TokenStatus:
    valid: bool
    ttl_seconds: int
    renewable: bool
    error: str = ""

    @property
    def ttl_hours(self) -> float:
        return round(self.ttl_seconds / 3600, 2)

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "ttl_seconds": self.ttl_seconds,
            "ttl_hours": self.ttl_hours,
            "renewable": self.renewable,
            "error": self.error,
        }


def vault_token_status(
    address: str,
    token: str,
    *,
    transport: HttpTransport | None = None,
    min_ttl_seconds: int = 0,
    user_agent: str = _VAULT_USER_AGENT,
) -> _TokenStatus:
    send = transport or urllib_transport()
    try:
        resp = send(
            "GET",
            f"{address.rstrip('/')}/v1/auth/token/lookup-self",
            {"X-Vault-Token": token, "User-Agent": user_agent},
            None,
        )
    except OSError as err:
        return _TokenStatus(False, -1, False, f"cannot reach Vault: {err}")
    if resp.status == 403:
        return _TokenStatus(False, -1, False, "token expired or invalid (HTTP 403)")
    if resp.status != 200:
        return _TokenStatus(False, -1, False, f"Vault returned HTTP {resp.status}")
    data = _json(resp).get("data", {})
    ttl, ren = int(data.get("ttl", 0) or 0), bool(data.get("renewable", False))
    if ttl < min_ttl_seconds:
        return _TokenStatus(False, ttl, ren, f"TTL too low: {ttl}s < {min_ttl_seconds}s")
    return _TokenStatus(True, ttl, ren)


class OnePasswordBackend:
    def __init__(
        self,
        vault: str = "Infra2",
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        category: str = "Login",
        timeout: float = 30.0,
    ) -> None:
        self._vault, self._run, self._category, self._timeout = vault, runner, category, timeout

    def _op(
        self, *args: str, check: bool = True, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        kw: dict[str, Any] = {"capture_output": True, "text": True}
        if self._timeout is not None:
            kw["timeout"] = self._timeout
        if input is not None:
            kw["input"] = input
        try:
            res = self._run(["op", *args], **kw)
        except subprocess.TimeoutExpired as exc:
            raise SecretsError(f"op {args[0]} {args[1]} timed out after {self._timeout}s") from exc
        if check and res.returncode != 0:
            tail = (res.stderr or "").strip().splitlines()[-1:] or ["no stderr"]
            raise SecretsError(f"op {args[0]} {args[1]} failed: {tail[0][:160]}")
        return res

    def _item(self, title: str) -> dict[str, Any] | None:
        res = self._op("item", "get", title, f"--vault={self._vault}", "--format=json", check=False)
        if res.returncode != 0:
            err = res.stderr or ""
            if "isn't an item" in err or "not found" in err.lower():
                return None
            tail = err.strip().splitlines()[-1:] or ["no stderr"]
            raise SecretsError(f"op item get failed: {tail[0][:160]}")
        return json.loads(res.stdout)

    def read(self, path: str) -> Mapping[str, str]:
        item = self._item(path)
        return (
            {}
            if item is None
            else {
                str(lbl): str(f.get("value") or "")
                for f in item.get("fields", [])
                if (lbl := f.get("label") or f.get("id")) and f.get("purpose") != "NOTES"
            }
        )

    def write(self, path: str, values: Mapping[str, str]) -> WriteResult:
        item = self._item(path)
        if item is None:
            payload = {
                "title": path,
                "category": self._category,
                "fields": [
                    {"id": k, "label": k, "type": "CONCEALED", "value": v}
                    for k, v in values.items()
                ],
            }
            self._op(
                "item",
                "create",
                "-",
                f"--vault={self._vault}",
                f"--category={self._category}",
                f"--title={path}",
                input=json.dumps(payload),
            )
            return WriteResult(tuple(sorted(values)))
        existing = {
            lbl: f for f in item.get("fields", []) if (lbl := f.get("label") or f.get("id"))
        }
        changed = [
            k
            for k, v in values.items()
            if (c := existing.get(k)) is None or str(c.get("value") or "") != v
        ]
        if changed:
            seen: set[str] = set()
            fields = [
                (
                    seen.add(str(lbl)) or {**f, "value": values[str(lbl)]}
                    if (lbl := f.get("label") or f.get("id")) and str(lbl) in values
                    else dict(f)
                )
                for f in item.get("fields", [])
            ]
            fields.extend(
                {"id": k, "label": k, "type": "CONCEALED", "value": v}
                for k, v in values.items()
                if k not in seen
            )
            self._op(
                "item",
                "edit",
                str(item["id"]),
                f"--vault={self._vault}",
                input=json.dumps({**item, "fields": fields}),
            )
        return WriteResult(tuple(sorted(changed)))


@dataclass(frozen=True)
class _ReconcileReport:
    missing: tuple[str, ...] = ()
    empty: tuple[str, ...] = ()
    unclassified: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not (self.missing or self.empty or self.unclassified or self.stale)

    def to_dict(self) -> dict[str, list[str]]:
        return {k: list(getattr(self, k)) for k in ("missing", "empty", "unclassified", "stale")}


def _reconcile(
    manifest: EnvironmentManifest,
    values: Mapping[str, str],
    *,
    expected: Mapping[str, str] | None = None,
    ignore_prefixes: tuple[str, ...] = ("_",),
) -> _ReconcileReport:
    missing = [f.key for f in manifest.store_backed if f.key not in values and not f.empty_ok]
    empty = [
        f.key
        for f in manifest.store_backed
        if f.key in values and not values[f.key].strip() and not f.empty_ok
    ]
    declared = {f.key for f in manifest.fields}
    unclassified = [k for k in values if k not in declared and not k.startswith(ignore_prefixes)]
    stale = [k for k, w in (expected or {}).items() if k in values and values[k] != w]
    return _ReconcileReport(
        tuple(sorted(missing)),
        tuple(sorted(empty)),
        tuple(sorted(unclassified)),
        tuple(sorted(stale)),
    )


@dataclass(frozen=True)
class _SyncReport:
    changed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, list[str]]:
        return {k: list(getattr(self, k)) for k in ("changed", "unchanged", "missing")}


def _generate_secret(field: EnvironmentField) -> str:
    return _secrets.token_urlsafe(32)


class SecretsResolver:
    def __init__(
        self,
        manifest: EnvironmentManifest,
        *,
        project: str,
        service: str,
        env: str,
        store: _SecretsBackend,
        human: _SecretsBackend | None = None,
        release: Mapping[str, str] | None = None,
        decisions: Mapping[str, str] | None = None,
        generator: Callable[[EnvironmentField], str] = _generate_secret,
    ) -> None:
        self.manifest, self.project, self.service, self.env = manifest, project, service, env
        self.store, self.human, self._generate = store, human, generator
        self.release, self.decisions = dict(release or {}), dict(decisions or {})
        self.path = vault_path(project, env, service)
        self._items: dict[str, Mapping[str, str]] = {}

    def _human_values(self, field: EnvironmentField) -> Mapping[str, str]:
        if self.human is None:
            raise SecretsError("no human backend configured")
        item = op_item(self.project, self.env, self.service, scope=field.scope)
        if item not in self._items:
            self._items[item] = self.human.read(item)
        return self._items[item]

    def human_expected(self) -> dict[str, str]:
        return {
            f.key: v
            for f in self.manifest.by_source(_FieldSource.HUMAN)
            if f.store_backed and (v := self._human_values(f).get(f.key))
        }

    def sync_human(self) -> _SyncReport:
        exp = self.human_expected()
        missing = tuple(
            sorted(
                f.key
                for f in self.manifest.by_source(_FieldSource.HUMAN)
                if f.store_backed and f.key not in exp and not f.empty_ok
            )
        )
        return self._apply(exp, missing=missing)

    def ensure_runtime(self) -> _SyncReport:
        cur = self.store.read(self.path)
        rt = [f for f in self.manifest.by_source(_FieldSource.RUNTIME) if f.store_backed]
        fresh = {f.key: self._generate(f) for f in rt if not f.empty_ok and not cur.get(f.key)}
        present = tuple(sorted(f.key for f in rt if f.key not in fresh))
        return (
            _SyncReport(self.store.write(self.path, fresh).changed, present)
            if fresh
            else _SyncReport(unchanged=present)
        )

    def mirror(self) -> _SyncReport:
        if self.human is None:
            raise SecretsError("no human backend configured")
        cur = self.store.read(self.path)
        changed, unchanged, missing = [], [], []
        for f in self.manifest.by_source(_FieldSource.RUNTIME):
            if not f.mirror_to_1password:
                continue
            if not (val := cur.get(f.key)):
                missing.append(f.key)
            else:
                item = op_item(self.project, self.env, self.service, scope=f.scope)
                (changed if self.human.write(item, {f.key: val}).wrote else unchanged).append(f.key)
        return _SyncReport(tuple(sorted(changed)), tuple(sorted(unchanged)), tuple(sorted(missing)))

    def compose_env(self) -> dict[str, str]:
        out: dict[str, str] = {}
        missing: list[str] = []
        for f in self.manifest.by_source(_FieldSource.RELEASE, _FieldSource.DECISION):
            src = self.release if f.source == _FieldSource.RELEASE else self.decisions
            if val := src.get(f.env, ""):
                out[f.env] = val
            elif f.required or not f.empty_ok:
                missing.append(f.env)
        if missing:
            raise SecretsError(f"release/decision values missing: {', '.join(sorted(missing))}")
        return out

    def reconcile(self) -> _ReconcileReport:
        exp = self.human_expected() if self.human is not None else None
        return _reconcile(self.manifest, self.store.read(self.path), expected=exp)

    def _apply(self, desired: Mapping[str, str], *, missing: tuple[str, ...]) -> _SyncReport:
        cur = self.store.read(self.path)
        diff = {k: v for k, v in desired.items() if cur.get(k) != v}
        unchanged = tuple(sorted(k for k in desired if k not in diff))
        return (
            _SyncReport(self.store.write(self.path, diff).changed, unchanged, missing)
            if diff
            else _SyncReport(unchanged=unchanged, missing=missing)
        )


def render_agent_template(
    manifest: EnvironmentManifest,
    *,
    project: str,
    service: str,
    source_env: str | None = None,
    exclude_groups: Sequence[str] = (),
) -> str:
    excluded = set(exclude_groups)
    env_expr = f'"{source_env}"' if source_env else "$env"
    lines = [
        f"{{{{- /* Generated from {manifest.source} by infra2_sdk.secrets. Do not edit. */ -}}}}",
        '{{- $env := env "ENV" -}}',
    ]
    own: list[EnvironmentField] = []
    providers: dict[str, list[EnvironmentField]] = {}
    for f in manifest.fields:
        if f.group in excluded:
            continue
        if f.provided_by:
            providers.setdefault(f.provided_by.split(":", 1)[0], []).append(f)
        elif f.rendered:
            own.append(f)
    for prov, flds in providers.items():
        pp, ps = prov.split("/", 1)
        lines.append(f'{{{{- with secret (printf "secret/data/{pp}/%s/{ps}" {env_expr}) }}}}')
        lines.extend(_render_line(f, key=f.provided_by.split(":", 1)[1]) for f in flds)
        lines.append("{{- end }}")
    if own:
        lines.append(
            f'{{{{- with secret (printf "secret/data/{project}/%s/{service}" {env_expr}) }}}}'
        )
        lines.extend(_render_line(f, key=f.key) for f in own)
        lines.append("{{- end }}")
    return "\n".join(lines) + "\n"


def render_agent_policy(
    manifest: EnvironmentManifest,
    *,
    project: str,
    service: str,
    source_env: str | None = None,
) -> str:
    env = source_env or "{{env}}"
    paths = [f"{project}/{env}/{service}"]
    for f in manifest.fields:
        if f.provided_by:
            pp, ps = f.provided_by.split(":", 1)[0].split("/", 1)
            if (cand := f"{pp}/{env}/{ps}") not in paths:
                paths.append(cand)
    blocks = [f"# Generated from {manifest.source} by infra2_sdk.secrets. Do not edit."]
    for p in paths:
        blocks.append(f'path "secret/data/{p}" {{\n  capabilities = ["read"]\n}}')
        blocks.append(f'path "secret/metadata/{p}" {{\n  capabilities = ["read", "list"]\n}}')
    blocks.append('path "auth/token/renew-self" {\n  capabilities = ["update"]\n}')
    blocks.append('path "auth/token/lookup-self" {\n  capabilities = ["read"]\n}')
    return "\n\n".join(blocks) + "\n"


def _render_line(f: EnvironmentField, *, key: str) -> str:
    if f.composed_from:
        fmt, args = f.composed_from, []
        for n in f.composed_keys:
            fmt = fmt.replace(f"{{{n}}}", "%s")
            args.append(f".Data.data.{n}")
        for n in f.composed_env:
            fmt = fmt.replace(f"{{env:{n}}}", "%s")
            args.append(f'(env "{n}")')
        return f'{f.env}={{{{ printf "%q" (printf "{fmt}" {" ".join(args)}) }}}}'
    if f.empty_ok:
        return (
            f'{{{{ with index .Data.data "{key}" }}}}{f.env}={{{{ printf "%q" . }}}}{{{{ end }}}}'
        )
    return f'{f.env}={{{{ printf "%q" .Data.data.{key} }}}}'


def _json(response: HttpResponse) -> dict[str, Any]:
    try:
        parsed = json.loads(response.body.decode("utf-8") or "{}")
    except ValueError as err:
        raise SecretsError("backend returned malformed JSON") from err
    return parsed if isinstance(parsed, dict) else {}


@dataclass(frozen=True)
class VaultTokenTarget:
    project: str
    service: str
    service_dir: str
    project_dir: str
    dokploy_project: str


def normalize_selector(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    norm = value.strip()
    if not norm:
        raise ValueError(f"{label} must not be empty")
    if "-" in norm or "/" in norm:
        raise ValueError(f"{label} must not include '-' or '/'")
    return norm


def _normalize_policy_env(value: str | None) -> str:
    if not value or not value.strip():
        return "production"
    val = value.strip().lower()
    mapped = {
        "prod": "production",
        "production": "production",
        "stg": "staging",
        "staging": "staging",
        "preview": "preview",
        "preview_env": "preview",
        "preview-env": "preview",
        "canary": "canary_preview",
        "canary-preview": "canary_preview",
        "canary_preview": "canary_preview",
    }
    if val in mapped:
        return mapped[val]
    if any(ch in val for ch in ("/", "\\", " ", "\t", "\n")):
        raise ValueError("ENV name must not include '/' or whitespace")
    return val.replace("-", "_")


def policy_name(project: str, env: str, service: str) -> str:
    return f"{project}-{_normalize_policy_env(env)}-{service}"


__all__ = [
    "OnePasswordBackend",
    "SecretsError",
    "SecretsResolver",
    "VaultKvBackend",
    "VaultTokenTarget",
    "normalize_selector",
    "policy_name",
    "render_agent_policy",
    "render_agent_template",
    "vault_path",
    "vault_token_status",
]
