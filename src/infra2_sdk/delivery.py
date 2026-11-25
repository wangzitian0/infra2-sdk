"""Delivery pipeline cell policies, runtime hooks manifest/runner, and health polling."""

from __future__ import annotations

import argparse
import importlib.resources
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from infra2_sdk._core import (
    ContractModel,
    parse_iso_utc,
    redact_secrets,
    require_contract_version,
    validate_http_url,
)

POLICY_VERSION = 1
BOOTSTRAP_CAP_SECONDS = 120
HOOKS_SCHEMA_VERSION = 3

FORBIDDEN_HOOK_ENV_NAMES: frozenset[str] = frozenset(
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
FORBIDDEN_HOOK_ENV_PREFIXES: tuple[str, ...] = (
    "SECRET_",
    "PRIVATE_",
    "OP_",
    "INFRA2_SECRET_",
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
        "PYTHONPATH",
        "TARGET_URL",
        "TERM",
        "TZ",
        "USER",
        "VIRTUAL_ENV",
    }
)

NeedKind = Literal[
    "code",
    "recorded_llm",
    "running_service",
    "real_llm",
    "staging_data",
    "real_provider",
]


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
    capabilities: tuple[NeedKind, ...] = ()

    @field_validator("check_list", mode="before")
    @classmethod
    def _coerce_check_list(cls, value: Any) -> str | tuple[str, ...] | None:
        if isinstance(value, list):
            return tuple(str(x) for x in value)
        return value


def load_stages() -> dict[str, Any]:
    """Load and validate packaged stages.yaml pipeline policy definitions."""
    raw_text = (
        importlib.resources.files("infra2_sdk.data")
        .joinpath("stages.yaml")
        .read_text(encoding="utf-8")
    )
    doc = yaml.safe_load(raw_text)
    if not isinstance(doc, dict):
        raise ValueError("stages.yaml root must be a mapping")
    require_contract_version(doc.get("policy_version"), POLICY_VERSION, label="stages.yaml policy")
    if doc.get("bootstrap_cap_seconds") != BOOTSTRAP_CAP_SECONDS:
        raise ValueError("stages.yaml bootstrap_cap_seconds mismatch")
    raw_cells = doc.get("cells")
    if not isinstance(raw_cells, dict) or not raw_cells:
        raise ValueError("stages.yaml must define cells")
    cells = {str(name): CellPolicy.model_validate(spec) for name, spec in raw_cells.items()}
    return {
        "policy_version": int(doc["policy_version"]),
        "bootstrap_cap_seconds": int(doc["bootstrap_cap_seconds"]),
        "stages": dict(doc.get("stages") or {}),
        "cells": cells,
    }


_LOADED_STAGES = load_stages()
CELL_POLICIES: Mapping[str, CellPolicy] = MappingProxyType(_LOADED_STAGES["cells"])


class OverrideReason(ContractModel):
    """Explicit override excluding or including a check inside a delivery cell."""

    action: Literal["exclude", "include"] = "exclude"
    reason: str
    approved_by: str = ""
    expires_at: datetime | None = None

    @field_validator("reason")
    @classmethod
    def _require_reason(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("override reason must not be blank")
        return text


CellOverrides = dict[str, OverrideReason]


class CheckRow(ContractModel):
    """One executable verification step declared in runtime-hooks.manifest.json."""

    id: str
    argv: tuple[str, ...]
    needs: tuple[NeedKind, ...] = ("code",)
    writes: bool = False
    accepts_scale: bool = False
    timeout_seconds: int = 60
    overrides: dict[str, OverrideReason] = Field(default_factory=dict)

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

    @field_validator("overrides")
    @classmethod
    def _validate_override_cells(
        cls, value: dict[str, OverrideReason]
    ) -> dict[str, OverrideReason]:
        unknown = sorted(set(value) - set(CELL_POLICIES))
        if unknown:
            raise ValueError(f"override references unknown cells: {', '.join(unknown)}")
        return value


class RuntimeHooksManifest(ContractModel):
    """Schema v3 runtime-hooks.manifest.json contract."""

    schema_version: int = HOOKS_SCHEMA_VERSION
    service: str
    required_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    checks: tuple[CheckRow, ...]

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        return require_contract_version(value, HOOKS_SCHEMA_VERSION, label="runtime-hooks manifest")

    @field_validator("service")
    @classmethod
    def _validate_service(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("service must not be empty")
        return text

    @field_validator("required_env", "optional_env")
    @classmethod
    def _validate_env_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if name in FORBIDDEN_HOOK_ENV_NAMES or name.startswith(FORBIDDEN_HOOK_ENV_PREFIXES):
                raise ValueError(f"forbidden sensitive env var {name!r} in runtime hooks manifest")
        return value

    @model_validator(mode="after")
    def _validate_unique_checks(self) -> RuntimeHooksManifest:
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


class CheckVerdict(ContractModel):
    """Execution outcome of one check in a delivery cell."""

    id: str
    cell: str
    passed: bool
    exit_code: int
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""


class HookRunResult(ContractModel):
    """Aggregate execution result for one delivery cell."""

    cell: str
    service: str
    passed: bool
    verdicts: tuple[CheckVerdict, ...]


def resolve_check_cells(
    check: CheckRow,
    *,
    policies: Mapping[str, CellPolicy] = CELL_POLICIES,
) -> tuple[str, ...]:
    """Derive the ordered delivery cells in which a CheckRow should execute."""
    resolved: set[str] = set()
    needed = set(check.needs)
    for cell_name, policy in policies.items():
        if check.id == "smoke":
            if policy.check_list == "smoke" and needed <= set(policy.capabilities):
                resolved.add(cell_name)
            continue
        if policy.owner != "app" or not (needed <= set(policy.capabilities)):
            continue
        if check.writes and policy.scale == "read_only":
            continue
        if not check.accepts_scale and policy.scale == "full":
            continue
        resolved.add(cell_name)

    for cell_name, override in check.overrides.items():
        if override.action == "exclude":
            resolved.discard(cell_name)
        else:
            resolved.add(cell_name)

    order = list(policies)
    return tuple(sorted(resolved, key=order.index))


def build_hook_env(
    manifest: RuntimeHooksManifest,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Construct an allowlisted, secret-free environment dictionary for hook processes."""
    source = os.environ if environ is None else environ
    allowed_keys = (
        set(_HOOK_PASSTHROUGH_KEYS) | set(manifest.required_env) | set(manifest.optional_env)
    )
    missing = [k for k in manifest.required_env if not str(source.get(k, "")).strip()]
    if missing:
        raise ValueError(f"missing required hook env vars: {', '.join(missing)}")
    out: dict[str, str] = {}
    for key in sorted(allowed_keys):
        if key in FORBIDDEN_HOOK_ENV_NAMES or key.startswith(FORBIDDEN_HOOK_ENV_PREFIXES):
            raise ValueError(f"forbidden hook env var requested: {key!r}")
        if key in source:
            out[key] = str(source[key])
    return out


def record_verdicts(verdicts: Sequence[CheckVerdict], path: str | Path) -> Path:
    """Persist structured CheckVerdict records as canonical JSON."""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [v.to_dict() for v in verdicts]
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path


def run_cell(
    manifest: RuntimeHooksManifest,
    cell: str,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> HookRunResult:
    """Execute all manifest checks scheduled in a given delivery cell."""
    if cell not in CELL_POLICIES:
        raise ValueError(f"unknown delivery cell {cell!r}")
    policy = CELL_POLICIES[cell]
    selected = [c for c in manifest.checks if cell in resolve_check_cells(c)]
    env = build_hook_env(manifest, environ) if selected else {}
    exec_fn = subprocess.run if runner is None else runner
    source_env = os.environ if environ is None else environ

    verdicts: list[CheckVerdict] = []
    for check in selected:
        timeout = min(check.timeout_seconds, policy.budget_seconds)
        started = time.perf_counter()
        try:
            proc = exec_fn(
                list(check.argv),
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            verdict = CheckVerdict(
                id=check.id,
                cell=cell,
                passed=proc.returncode == 0,
                exit_code=int(proc.returncode),
                duration_seconds=time.perf_counter() - started,
                stdout=redact_secrets(proc.stdout or "", source_env),
                stderr=redact_secrets(proc.stderr or "", source_env),
            )
        except subprocess.TimeoutExpired as exc:
            out_raw = exc.stdout if isinstance(exc.stdout, str) else ""
            verdict = CheckVerdict(
                id=check.id,
                cell=cell,
                passed=False,
                exit_code=124,
                duration_seconds=time.perf_counter() - started,
                stdout=redact_secrets(out_raw, source_env),
                stderr=f"timed out after {timeout}s",
            )
        verdicts.append(verdict)
        if not verdict.passed and policy.stop_on_fail:
            break

    passed = all(v.passed for v in verdicts)
    return HookRunResult(
        cell=cell, service=manifest.service, passed=passed, verdicts=tuple(verdicts)
    )


class DeployOperation(StrEnum):
    """Lifecycle operation kind for deploy provenance contracts."""

    DEPLOY = "deploy"
    PROMOTE = "promote"
    ROLLBACK = "rollback"


class DeployType(StrEnum):
    """Target environment kind for a deployment request."""

    PREVIEW = "preview"
    STAGING = "staging"
    PROD = "prod"


class DeployEvidence(ContractModel):
    """Provenance record binding a deploy candidate to commit SHA, tag, and CI run."""

    commit_sha: str
    release_tag: str
    ci_run_id: str | None = None
    recorded_at: datetime


class DeployRequest(ContractModel):
    """Validated deployment request contract with UTC timestamps and target URL."""

    operation: DeployOperation
    deploy_type: DeployType
    target_ref: str
    health_url: str
    requested_at: datetime
    accept_until: datetime

    @field_validator("health_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        validate_http_url(value, label="health_url")
        return value.strip()

    @field_validator("requested_at", "accept_until", mode="before")
    @classmethod
    def _coerce_utc(cls, value: Any) -> datetime:
        return parse_iso_utc(value)


def parse_sender_deadline(raw: object) -> datetime:
    """Parse an ISO-8601 deadline with explicit timezone into UTC."""
    return parse_iso_utc(raw, allow_naive=False)


def production_request_accept_until(requested_at: datetime, *, ttl_seconds: int = 1800) -> datetime:
    """Compute the UTC expiry timestamp for a production approval window."""
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    dt = parse_iso_utc(requested_at, allow_naive=False)
    return dt + timedelta(seconds=ttl_seconds)


HttpGet = Callable[[str, float], tuple[int, bytes]]


class HealthPollResult(ContractModel):
    """Final result of polling a service health endpoint for version convergence."""

    ok: bool
    attempts: int
    status_code: int | None = None
    observed_version: str = ""
    detail: str = ""


def default_http_get(url: str, timeout_seconds: float) -> tuple[int, bytes]:
    """Perform a dependency-free stdlib HTTP GET request returning (status, body)."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310
            return (int(resp.status), resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return (int(exc.code), body)


def poll_until_healthy(
    url: str,
    *,
    expected_version: str | None = None,
    timeout_seconds: float = 60.0,
    request_timeout_seconds: float = 5.0,
    interval_seconds: float = 2.0,
    max_version_mismatch_attempts: int = 5,
    http_get: HttpGet = default_http_get,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> HealthPollResult:
    """Poll health endpoint until HTTP 200 (plus optional version match) or timeout."""
    validate_http_url(url, label="poll url")
    deadline = monotonic() + timeout_seconds
    attempts = 0
    mismatch_count = 0
    last_status: int | None = None
    last_version = ""
    last_detail = ""

    while True:
        attempts += 1
        try:
            status_code, body = http_get(url, request_timeout_seconds)
            last_status = status_code
            payload = json.loads(body.decode("utf-8")) if body else {}
            if isinstance(payload, Mapping):
                last_version = str(
                    payload.get("service_version")
                    or payload.get("version")
                    or payload.get("git_sha")
                    or ""
                )
            if status_code == 200:
                if not expected_version or last_version == expected_version:
                    return HealthPollResult(
                        ok=True,
                        attempts=attempts,
                        status_code=status_code,
                        observed_version=last_version,
                        detail="ok",
                    )
                mismatch_count += 1
                last_detail = (
                    f"version mismatch: expected {expected_version!r}, got {last_version!r}"
                )
                if mismatch_count >= max_version_mismatch_attempts:
                    return HealthPollResult(
                        ok=False,
                        attempts=attempts,
                        status_code=status_code,
                        observed_version=last_version,
                        detail=last_detail,
                    )
            else:
                last_detail = f"HTTP {status_code}"
        except Exception as exc:  # noqa: BLE001
            last_status = None
            last_detail = f"{type(exc).__name__}: {exc}"

        remaining = deadline - monotonic()
        if remaining <= 0:
            return HealthPollResult(
                ok=False,
                attempts=attempts,
                status_code=last_status,
                observed_version=last_version,
                detail=last_detail or "health poll timed out",
            )
        sleep(min(interval_seconds, remaining))


def _cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m infra2_sdk.delivery")
    sub = parser.add_subparsers(dest="command", required=True)

    chk = sub.add_parser("check-hooks", help="Validate runtime-hooks.manifest.json.")
    chk.add_argument("manifest", help="Path to runtime-hooks.manifest.json")

    run_p = sub.add_parser("run-cell", help="Run scheduled hook checks for one delivery cell.")
    run_p.add_argument("manifest", help="Path to runtime-hooks.manifest.json")
    run_p.add_argument("--cell", required=True, help="Delivery cell name, e.g. staging.operation")
    run_p.add_argument("--cwd", help="Working directory for hook commands")
    run_p.add_argument("--verdicts-out", help="Optional JSON path to write CheckVerdict records")

    poll_p = sub.add_parser("poll", help="Poll a live HTTP health endpoint until convergence.")
    poll_p.add_argument("--url", required=True)
    poll_p.add_argument("--expected-version")
    poll_p.add_argument("--timeout-seconds", type=float, default=60.0)
    poll_p.add_argument("--interval-seconds", type=float, default=2.0)

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "check-hooks":
        manifest = RuntimeHooksManifest.from_file(args.manifest)
        resolved = {c.id: resolve_check_cells(c) for c in manifest.checks}
        print(json.dumps({"service": manifest.service, "checks": resolved}, sort_keys=True))
        return 0
    if args.command == "run-cell":
        manifest = RuntimeHooksManifest.from_file(args.manifest)
        result = run_cell(manifest, args.cell, cwd=args.cwd)
        if args.verdicts_out:
            record_verdicts(result.verdicts, args.verdicts_out)
        print(json.dumps(result.to_dict(), sort_keys=True))
        return 0 if result.passed else 1
    if args.command == "poll":
        res = poll_until_healthy(
            args.url,
            expected_version=args.expected_version,
            timeout_seconds=args.timeout_seconds,
            interval_seconds=args.interval_seconds,
        )
        print(json.dumps(res.to_dict(), sort_keys=True))
        return 0 if res.ok else 1
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for runtime hooks validation, cell execution, and health polling."""
    return _cli(argv)


__all__ = (
    "BOOTSTRAP_CAP_SECONDS",
    "CELL_POLICIES",
    "CellOverrides",
    "CellPolicy",
    "CheckRow",
    "CheckVerdict",
    "DeployEvidence",
    "DeployOperation",
    "DeployRequest",
    "DeployType",
    "FORBIDDEN_HOOK_ENV_NAMES",
    "FORBIDDEN_HOOK_ENV_PREFIXES",
    "HOOKS_SCHEMA_VERSION",
    "HealthPollResult",
    "HookRunResult",
    "HttpGet",
    "NeedKind",
    "OverrideReason",
    "POLICY_VERSION",
    "RuntimeHooksManifest",
    "build_hook_env",
    "default_http_get",
    "load_stages",
    "main",
    "parse_sender_deadline",
    "poll_until_healthy",
    "production_request_accept_until",
    "record_verdicts",
    "resolve_check_cells",
    "run_cell",
)

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
