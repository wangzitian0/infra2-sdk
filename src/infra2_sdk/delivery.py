"""Delivery pipeline cell policies, runtime hooks manifest/runner, and health polling."""

from __future__ import annotations

import argparse
import functools
import importlib.resources
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from infra2_sdk._core import (
    ContractModel,
    parse_iso_utc,
    redact_secrets,
    require_contract_version,
    validate_http_url,
)

_POLICY_VERSION = 1
_HOOKS_SCHEMA_VERSION = 3

_FORBIDDEN_HOOK_ENV_NAMES: frozenset[str] = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "DATABASE_URL",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "LLM_API_KEY",
        "OP_SERVICE_ACCOUNT_TOKEN",
        "REDIS_URL",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
    }
)
_FORBIDDEN_HOOK_ENV_PREFIXES: tuple[str, ...] = (
    "AWS_",
    "CLOUDFLARE_",
    "DOKPLOY_",
    "INFRA2_SECRET_",
    "OP_",
    "PRIVATE_",
    "SECRET_",
    "SSH_",
    "VAULT_",
)

_HOOK_PASSTHROUGH_KEYS: frozenset[str] = frozenset(
    {
        "APP_URL",
        "ENVIRONMENT",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_RESOURCE_ATTRIBUTES",
        "OTEL_SERVICE_NAME",
        "PATH",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PYTHONPATH",
        "TARGET_URL",
        "TERM",
        "TMPDIR",
        "TZ",
        "USER",
        "UV_CACHE_DIR",
        "VIRTUAL_ENV",
    }
)

_NeedKind = Literal[
    "code",
    "recorded_llm",
    "running_service",
    "real_llm",
    "staging_data",
    "real_provider",
]
_CODE_ONLY_NEEDS: frozenset[str] = frozenset({"code", "recorded_llm"})
_ENV_NAME_RE = re.compile(r"\A[A-Z][A-Z0-9_]*\Z")


def _validate_hook_env_names(names: tuple[str, ...], *, label: str = "manifest") -> tuple[str, ...]:
    seen: set[str] = set()
    for name in names:
        if not _ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid env var {name!r} in runtime hooks {label}")
        if name in seen:
            raise ValueError(f"duplicate env var {name!r} in runtime hooks {label}")
        seen.add(name)
        if name in _FORBIDDEN_HOOK_ENV_NAMES or name.startswith(_FORBIDDEN_HOOK_ENV_PREFIXES):
            raise ValueError(f"forbidden sensitive env var {name!r} in runtime hooks manifest")
    return names


class CellPolicy(ContractModel):
    """Typed delivery policy definition for one pipeline cell from stages.yaml."""

    stage: str
    phase: str
    stage_id: str
    owner: Literal["app", "platform"]
    check_list: str | tuple[str, ...] | None = None
    trigger: str
    budget_seconds: int
    scale: str
    enforcement: Literal["blocks", "reports"]
    stop_on_fail: bool
    capabilities: tuple[_NeedKind, ...] = ()

    @field_validator("check_list", mode="before")
    @classmethod
    def _coerce_check_list(cls, value: Any) -> str | tuple[str, ...] | None:
        if isinstance(value, list):
            return tuple(str(x) for x in value)
        return value


@functools.cache
def _load_stages_cached() -> tuple[Mapping[str, Any], Mapping[str, CellPolicy]]:
    raw_text = (
        importlib.resources.files("infra2_sdk.data")
        .joinpath("stages.yaml")
        .read_text(encoding="utf-8")
    )
    doc = yaml.safe_load(raw_text)
    if not isinstance(doc, dict):
        raise ValueError("stages.yaml root must be a mapping")
    require_contract_version(doc.get("policy_version"), _POLICY_VERSION, label="stages.yaml policy")
    raw_stages = dict(doc.get("stages") or {})
    raw_cells = doc.get("cells")
    if not isinstance(raw_cells, dict) or not raw_cells:
        raise ValueError("stages.yaml must define cells")
    cells: dict[str, CellPolicy] = {}
    for name, spec in raw_cells.items():
        policy = CellPolicy.model_validate(spec)
        if raw_stages and policy.stage_id not in raw_stages:
            raise ValueError(f"stages.yaml cell {name!r} references unknown stage_id")
        cells[str(name)] = policy
    frozen_cells = MappingProxyType(cells)
    payload = MappingProxyType(
        {
            "policy_version": int(doc["policy_version"]),
            "bootstrap_cap_seconds": int(doc.get("bootstrap_cap_seconds", 120)),
            "stages": MappingProxyType(raw_stages),
            "cells": frozen_cells,
        }
    )
    return payload, frozen_cells


if TYPE_CHECKING:
    CELL_POLICIES: Mapping[str, CellPolicy]


def _cell_policies() -> Mapping[str, CellPolicy]:
    return _load_stages_cached()[1]


def __getattr__(name: str) -> Any:
    if name == "CELL_POLICIES":
        return _cell_policies()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _OverrideReason(ContractModel):
    reason: str
    issue: str = ""
    expires_at: datetime | None = None

    @field_validator("reason")
    @classmethod
    def _require_reason(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("override reason must not be blank")
        return text

    @field_validator("expires_at", mode="before")
    @classmethod
    def _coerce_expires(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        return parse_iso_utc(value)

    def is_active(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return True
        current = now or datetime.now(UTC)
        current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
        return self.expires_at > current


class _CellOverrides(ContractModel):
    add: dict[str, _OverrideReason] = Field(default_factory=dict)
    remove: dict[str, _OverrideReason] = Field(default_factory=dict)

    @field_validator("add", "remove")
    @classmethod
    def _validate_cells(cls, value: dict[str, _OverrideReason]) -> dict[str, _OverrideReason]:
        known = _cell_policies()
        unknown = sorted(set(value) - set(known))
        if unknown:
            raise ValueError(f"cells override references unknown cells: {', '.join(unknown)}")
        return value

    @model_validator(mode="after")
    def _disjoint_add_remove(self) -> _CellOverrides:
        overlap = sorted(set(self.add) & set(self.remove))
        if overlap:
            raise ValueError(f"cells cannot both add and remove: {', '.join(overlap)}")
        return self


class CheckRow(ContractModel):
    """One executable verification step declared in Schema v3 runtime-hooks.manifest.json."""

    id: str
    argv: tuple[str, ...]
    needs: tuple[_NeedKind, ...] = ("code",)
    writes: bool = False
    accepts_scale: bool = False
    timeout_seconds: int = 60
    bootstrap: str = ""
    required_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    checkout_lineage: bool = False
    cells: _CellOverrides = Field(default_factory=_CellOverrides)
    off: _OverrideReason | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        text = value.strip()
        if not text or not text.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"invalid check id {value!r}")
        return text

    @field_validator("argv")
    @classmethod
    def _validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not str(part).strip() for part in value):
            raise ValueError("check argv must be a non-empty list of strings")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def _validate_timeout(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("timeout_seconds must be positive")
        return int(value)

    @field_validator("required_env", "optional_env")
    @classmethod
    def _validate_env_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_hook_env_names(value, label="check")


class RuntimeHooksManifest(ContractModel):
    """Schema v3 runtime-hooks.manifest.json contract."""

    schema_version: int = _HOOKS_SCHEMA_VERSION
    service: str
    artifacts_dir: str = ".artifacts/hooks"
    required_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    checks: tuple[CheckRow, ...]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _validate_schema_version(cls, value: Any) -> int:
        return require_contract_version(
            value, _HOOKS_SCHEMA_VERSION, label="runtime-hooks manifest"
        )

    @field_validator("service", "artifacts_dir")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("service and artifacts_dir must not be empty")
        return text

    @field_validator("required_env", "optional_env")
    @classmethod
    def _validate_env_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_hook_env_names(value, label="manifest")

    @model_validator(mode="after")
    def _validate_manifest(self) -> RuntimeHooksManifest:
        if overlap := sorted(set(self.required_env) & set(self.optional_env)):
            raise ValueError(f"env var(s) declared in both required and optional: {overlap}")
        if not self.checks:
            raise ValueError("runtime-hooks manifest must declare at least one check")
        ids = [c.id for c in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate check id in runtime-hooks manifest")
        return self

    @classmethod
    def from_file(cls, path: str | Path) -> RuntimeHooksManifest:
        """Load and validate a runtime-hooks.manifest.json file."""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(raw)


class _CheckVerdict(ContractModel):
    id: str
    cell: str
    passed: bool
    exit_code: int
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""


class _HookRunResult(ContractModel):
    cell: str
    service: str
    passed: bool
    verdicts: tuple[_CheckVerdict, ...]


def resolve_check_cells(
    check: CheckRow,
    *,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Derive the ordered delivery cells in which a CheckRow should execute."""
    active_policies = _cell_policies()
    current_time = now or datetime.now(UTC)
    if check.off is not None and check.off.is_active(current_time):
        return ()
    needed = set(check.needs)
    resolved: set[str] = set()

    for cell_name, policy in active_policies.items():
        if check.id == "smoke":
            if policy.check_list == "smoke" and needed <= set(policy.capabilities):
                resolved.add(cell_name)
            continue
        if policy.owner != "app" or not (needed <= set(policy.capabilities)):
            continue
        if check.writes and policy.stage == "prod":
            continue
        if needed <= _CODE_ONLY_NEEDS and not (set(policy.capabilities) <= _CODE_ONLY_NEEDS):
            continue
        if check.writes and policy.scale == "read_only":
            continue
        if not check.accepts_scale and policy.scale == "full" and policy.stage != "schedule":
            continue
        resolved.add(cell_name)

    for cell_name, override in check.cells.remove.items():
        if override.is_active(current_time):
            resolved.discard(cell_name)

    for cell_name, override in check.cells.add.items():
        if not override.is_active(current_time):
            continue
        policy = active_policies[cell_name]
        if policy.owner == "platform" and policy.check_list != "smoke":
            raise ValueError(f"check {check.id!r} cannot add platform-owned cell {cell_name!r}")
        if check.writes and policy.stage == "prod":
            raise ValueError(f"check {check.id!r} has writes=True and cannot run in prod")
        if not (needed <= set(policy.capabilities)):
            missing = sorted(needed - set(policy.capabilities))
            raise ValueError(
                f"check {check.id!r} requires {missing} which cell {cell_name!r} lacks"
            )
        resolved.add(cell_name)

    order = list(active_policies)
    return tuple(sorted(resolved, key=order.index))


def _build_hook_env(
    manifest: RuntimeHooksManifest,
    environ: Mapping[str, str] | None = None,
    checks: Sequence[CheckRow] = (),
    *,
    cell: str | None = None,
) -> dict[str, str]:
    source = os.environ if environ is None else environ
    required_keys = tuple(
        dict.fromkeys(
            [*manifest.required_env, *(k for check in checks for k in check.required_env)]
        )
    )
    optional_keys = tuple(
        dict.fromkeys(
            [*manifest.optional_env, *(k for check in checks for k in check.optional_env)]
        )
    )
    allowed_keys = set(_HOOK_PASSTHROUGH_KEYS) | set(required_keys) | set(optional_keys)
    missing = [k for k in required_keys if not str(source.get(k, "")).strip()]
    if missing:
        raise ValueError(f"missing required hook env vars: {', '.join(missing)}")
    out: dict[str, str] = {}
    for key in sorted(allowed_keys):
        if key in _FORBIDDEN_HOOK_ENV_NAMES or key.startswith(_FORBIDDEN_HOOK_ENV_PREFIXES):
            raise ValueError(f"forbidden hook env var requested: {key!r}")
        if key in source:
            out[key] = str(source[key])
    out["HOOK_ARTIFACTS_DIR"] = manifest.artifacts_dir
    if cell is not None and cell in (policies := _cell_policies()):
        out["HOOK_CELL"] = cell
        out["HOOK_SCALE"] = policies[cell].scale
    return out


def _decode_stream(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _run_cell(
    manifest: RuntimeHooksManifest,
    cell: str,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    now: datetime | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> _HookRunResult:
    policies = _cell_policies()
    if cell not in policies:
        raise ValueError(f"unknown delivery cell {cell!r}")
    policy = policies[cell]
    current_time = now or datetime.now(UTC)
    selected = [
        c
        for c in manifest.checks
        if cell in resolve_check_cells(c, now=current_time)
        and (c.off is None or not c.off.is_active(current_time))
    ]
    if not selected:
        return _HookRunResult(
            cell=cell,
            service=manifest.service,
            passed=False,
            verdicts=(
                _CheckVerdict(
                    id=cell,
                    cell=cell,
                    passed=False,
                    exit_code=1,
                    duration_seconds=0.0,
                    stderr=f"cell {cell!r} matched 0 checks in {manifest.service!r}",
                ),
            ),
        )
    env = _build_hook_env(manifest, environ, checks=selected, cell=cell)
    exec_fn = subprocess.run if runner is None else runner
    source_env = os.environ if environ is None else environ
    bootstrap_cap = float(_load_stages_cached()[0]["bootstrap_cap_seconds"])
    cell_deadline = monotonic() + policy.budget_seconds

    def _exec_step(
        check_id: str,
        argv: list[str],
        *,
        timeout: float,
        started: float,
        prefix: str = "",
    ) -> _CheckVerdict:
        try:
            proc = exec_fn(
                argv,
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            rc = int(proc.returncode)
            err_text = redact_secrets(_decode_stream(proc.stderr), source_env)
            if prefix and rc != 0:
                stripped = err_text.strip()
                err_text = (
                    f"{prefix}failed: {stripped}"
                    if stripped
                    else f"{prefix}failed with exit code {rc}"
                )
            return _CheckVerdict(
                id=check_id,
                cell=cell,
                passed=rc == 0,
                exit_code=rc,
                duration_seconds=time.perf_counter() - started,
                stdout=redact_secrets(_decode_stream(proc.stdout), source_env),
                stderr=err_text,
            )
        except subprocess.TimeoutExpired as exc:
            err_tail = redact_secrets(_decode_stream(exc.stderr), source_env)
            timeout_note = f"{prefix}timed out after {timeout:g}s"
            combined_err = f"{timeout_note}\n{err_tail}".strip() if err_tail else timeout_note
            return _CheckVerdict(
                id=check_id,
                cell=cell,
                passed=False,
                exit_code=124,
                duration_seconds=time.perf_counter() - started,
                stdout=redact_secrets(_decode_stream(exc.stdout), source_env),
                stderr=combined_err,
            )
        except OSError as exc:
            return _CheckVerdict(
                id=check_id,
                cell=cell,
                passed=False,
                exit_code=127,
                duration_seconds=time.perf_counter() - started,
                stderr=redact_secrets(f"{prefix}{type(exc).__name__}: {exc}", source_env),
            )

    verdicts: list[_CheckVerdict] = []
    for check in selected:
        remaining = cell_deadline - monotonic()
        if remaining <= 0:
            verdicts.append(
                _CheckVerdict(
                    id=check.id,
                    cell=cell,
                    passed=False,
                    exit_code=124,
                    duration_seconds=0.0,
                    stderr=f"cell {cell!r} exhausted {policy.budget_seconds}s budget",
                )
            )
            break
        started = time.perf_counter()
        if check.bootstrap.strip():
            boot_verdict = _exec_step(
                check.id,
                ["sh", "-c", check.bootstrap],
                timeout=min(bootstrap_cap, remaining),
                started=started,
                prefix="bootstrap ",
            )
            if not boot_verdict.passed:
                verdicts.append(boot_verdict)
                if policy.stop_on_fail:
                    break
                continue
            remaining = cell_deadline - monotonic()
            if remaining <= 0:
                verdicts.append(
                    _CheckVerdict(
                        id=check.id,
                        cell=cell,
                        passed=False,
                        exit_code=124,
                        duration_seconds=time.perf_counter() - started,
                        stderr=f"cell {cell!r} exhausted {policy.budget_seconds}s budget",
                    )
                )
                break
        timeout = min(float(check.timeout_seconds), remaining)
        verdict = _exec_step(check.id, list(check.argv), timeout=timeout, started=started)
        verdicts.append(verdict)
        if not verdict.passed and policy.stop_on_fail:
            break

    return _HookRunResult(
        cell=cell,
        service=manifest.service,
        passed=all(v.passed for v in verdicts),
        verdicts=tuple(verdicts),
    )


_HttpGet = Callable[[str, float], tuple[int, bytes]]


class _HealthPollResult(ContractModel):
    ok: bool
    attempts: int
    status_code: int | None = None
    observed_version: str = ""
    detail: str = ""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, hdrs: Any, newurl: str):
        return None


def _default_http_get(
    url: str, timeout_seconds: float, *, follow_redirects: bool = False
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method="GET")
    opener = (
        urllib.request.build_opener()
        if follow_redirects
        else urllib.request.build_opener(_NoRedirectHandler())
    )
    try:
        with opener.open(req, timeout=timeout_seconds) as resp:  # noqa: S310
            return (int(resp.status), resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return (int(exc.code), body)


def _version_matches(expected: str, candidates: Sequence[str]) -> bool:
    exp = expected.strip()
    if not exp:
        return True
    for raw in candidates:
        cand = raw.strip()
        if not cand:
            continue
        if cand == exp:
            return True
        if len(exp) >= 7 and len(cand) >= 7 and (cand.startswith(exp) or exp.startswith(cand)):
            return True
    return False


def _poll_until_healthy(
    url: str,
    *,
    expected_version: str | None = None,
    timeout_seconds: float = 60.0,
    request_timeout_seconds: float = 5.0,
    interval_seconds: float = 2.0,
    follow_redirects: bool = False,
    http_get: _HttpGet | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> _HealthPollResult:
    validate_http_url(url, label="poll url")
    for label, val in (
        ("timeout_seconds", timeout_seconds),
        ("request_timeout_seconds", request_timeout_seconds),
        ("interval_seconds", interval_seconds),
    ):
        if not math.isfinite(val) or val <= 0:
            raise ValueError(f"{label} must be positive")

    getter: _HttpGet = (
        (lambda u, t: _default_http_get(u, t, follow_redirects=follow_redirects))
        if http_get is None
        else http_get
    )
    deadline = monotonic() + timeout_seconds
    attempts = 0
    last_status: int | None = None
    last_version = ""
    last_detail = ""

    while True:
        attempts += 1
        try:
            status_code, body = getter(url, request_timeout_seconds)
            last_status = status_code
            payload: Mapping[str, Any] = {}
            if body:
                try:
                    parsed = json.loads(body.decode("utf-8", errors="replace"))
                    if isinstance(parsed, Mapping):
                        payload = parsed
                except json.JSONDecodeError:
                    payload = {}
            version_candidates = [
                str(payload.get(k) or "").strip()
                for k in ("service_version", "version", "git_sha")
                if str(payload.get(k) or "").strip()
            ]
            last_version = version_candidates[0] if version_candidates else ""
            if status_code == 200:
                if not expected_version or _version_matches(expected_version, version_candidates):
                    return _HealthPollResult(
                        ok=True,
                        attempts=attempts,
                        status_code=status_code,
                        observed_version=last_version,
                        detail="ok",
                    )
                last_detail = (
                    f"version mismatch: expected {expected_version!r}, got {version_candidates!r}"
                )
            else:
                last_detail = f"HTTP {status_code}"
        except Exception as exc:  # noqa: BLE001
            last_status = None
            last_detail = f"{type(exc).__name__}: {exc}"

        remaining = deadline - monotonic()
        if remaining <= 0:
            return _HealthPollResult(
                ok=False,
                attempts=attempts,
                status_code=last_status,
                observed_version=last_version,
                detail=last_detail or "health poll timed out",
            )
        sleep(min(interval_seconds, remaining))


def _main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for runtime hooks validation, cell execution, and health polling."""
    parser = argparse.ArgumentParser(prog="python -m infra2_sdk.delivery")
    sub = parser.add_subparsers(dest="command", required=True)

    chk = sub.add_parser("check-hooks", help="Validate runtime-hooks.manifest.json.")
    chk.add_argument("manifest", help="Path to runtime-hooks.manifest.json")

    run_p = sub.add_parser("run-cell", help="Run scheduled hook checks for one delivery cell.")
    run_p.add_argument("manifest", help="Path to runtime-hooks.manifest.json")
    run_p.add_argument("--cell", required=True, help="Delivery cell name, e.g. staging.operation")
    run_p.add_argument("--cwd", help="Working directory for hook commands")

    poll_p = sub.add_parser("poll", help="Poll a live HTTP health endpoint until convergence.")
    poll_p.add_argument("--url", required=True)
    poll_p.add_argument("--expected-version")
    poll_p.add_argument("--timeout-seconds", type=float, default=60.0)
    poll_p.add_argument("--interval-seconds", type=float, default=2.0)
    poll_p.add_argument("--follow-redirects", action="store_true")

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "check-hooks":
        manifest = RuntimeHooksManifest.from_file(args.manifest)
        resolved = {c.id: resolve_check_cells(c) for c in manifest.checks}
        print(json.dumps({"service": manifest.service, "checks": resolved}, sort_keys=True))
        return 0
    if args.command == "run-cell":
        manifest = RuntimeHooksManifest.from_file(args.manifest)
        result = _run_cell(manifest, args.cell, cwd=args.cwd)
        print(json.dumps(result.to_dict(), sort_keys=True))
        return 0 if result.passed else 1

    res = _poll_until_healthy(
        args.url,
        expected_version=args.expected_version,
        timeout_seconds=args.timeout_seconds,
        interval_seconds=args.interval_seconds,
        follow_redirects=args.follow_redirects,
    )
    print(json.dumps(res.to_dict(), sort_keys=True))
    return 0 if res.ok else 1


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = (
    "CELL_POLICIES",
    "CellPolicy",
    "CheckRow",
    "RuntimeHooksManifest",
    "resolve_check_cells",
)

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
