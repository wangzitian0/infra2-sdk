"""Environment and runtime-hooks manifest CLI driver."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from importlib import import_module
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any

from infra2_sdk._wire import _yaml, reject_unknown
from infra2_sdk.ci import validate_manifest_offline
from infra2_sdk.observability import CheckVerdict, record_verdicts, scrub
from infra2_sdk.runtime.config_schema import (
    EnvironmentManifest,
    _load_manifest,
    _validate_env,
    environment_manifest_from_model,
)
from infra2_sdk.runtime.environment import check_shared_env_conformance

CURRENT_HOOKS_SCHEMA_VERSION = 3
SUPPORTED_HOOKS_SCHEMA_VERSIONS = (3,)

_STAGES = ("local", "ci", "preview", "staging", "prod")
_PHASES = ("pre", "operation", "post")
_VALID_NEEDS = frozenset(
    {"code", "recorded_llm", "running_service", "real_llm", "staging_data", "real_provider"}
)
_PRE_NEEDS = frozenset({"code", "recorded_llm"})


@dataclass(frozen=True)
class CellPolicy:
    cell: str
    stage: str
    phase: str
    stage_id: str
    owner: str
    check_list: str | tuple[str, ...] | None
    trigger: str
    budget_seconds: int
    scale: str
    enforcement: str
    stop_on_fail: bool
    capabilities: frozenset[str]


def _parse_scalar(raw: str) -> Any:
    val = raw.strip()
    if val in ("null", "~", ""):
        return None
    if val == "true":
        return True
    if val == "false":
        return False
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        return [_parse_scalar(part) for part in inner.split(",")] if inner else []
    if val.isdigit() or (val.startswith("-") and val[1:].isdigit()):
        return int(val)
    return val.strip("\"'")


def _parse_yaml_fallback(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        while stack and indent <= stack[-1][0]:
            stack.pop()
        key, _, rest = line.strip().partition(":")
        key = key.strip()
        val_raw = rest.strip()
        parent = stack[-1][1]
        if val_raw == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_scalar(val_raw)
    return root


def _load_stages_doc(text: str) -> Mapping[str, Any]:
    try:
        return _yaml().safe_load(text) or {}
    except ImportError:
        return _parse_yaml_fallback(text)


def _load_policy_table(
    path: str | Path | None = None,
) -> tuple[int, int, Mapping[str, CellPolicy]]:
    src = Path(path) if path is not None else files("infra2_sdk").joinpath("data/stages.yaml")
    data = _load_stages_doc(src.read_text(encoding="utf-8"))
    pv = data.get("policy_version")
    if type(pv) is not int or pv < 1:
        raise ValueError(f"stages.yaml: invalid policy_version {pv!r}")
    boot_cap = data.get("bootstrap_cap_seconds")
    if type(boot_cap) is not int or boot_cap <= 0:
        raise ValueError(f"stages.yaml: invalid bootstrap_cap_seconds {boot_cap!r}")
    stages = data.get("stages")
    raw_cells = data.get("cells")
    if not isinstance(stages, Mapping) or not isinstance(raw_cells, Mapping):
        raise ValueError("stages.yaml: 'stages' and 'cells' must be mappings")
    expected = {f"{s}.{p}" for s in _STAGES for p in _PHASES} | {"scheduled_audit"}
    if set(raw_cells) != expected:
        raise ValueError(f"stages.yaml: cells mismatch {sorted(set(raw_cells) ^ expected)}")
    policies: dict[str, CellPolicy] = {}
    for cid, entry in raw_cells.items():
        if not isinstance(entry, Mapping):
            raise ValueError(f"stages.yaml: cell {cid!r} must be a mapping")
        sid = entry.get("stage_id")
        if sid not in stages:
            raise ValueError(f"stages.yaml: cell {cid!r} unknown stage_id {sid!r}")
        owner = entry.get("owner")
        enf = entry.get("enforcement")
        sof = entry.get("stop_on_fail")
        budget = entry.get("budget_seconds")
        caps = entry.get("capabilities")
        if owner not in ("app", "platform"):
            raise ValueError(f"stages.yaml: cell {cid!r} invalid owner {owner!r}")
        if enf not in ("blocks", "reports"):
            raise ValueError(f"stages.yaml: cell {cid!r} invalid enforcement {enf!r}")
        if type(sof) is not bool or type(budget) is not int or budget <= 0:
            raise ValueError(f"stages.yaml: cell {cid!r} invalid stop_on_fail or budget_seconds")
        if not isinstance(caps, list) or not set(caps) <= _VALID_NEEDS:
            raise ValueError(f"stages.yaml: cell {cid!r} invalid capabilities {caps!r}")
        raw_cl = entry.get("check_list")
        cl: str | tuple[str, ...] | None = tuple(raw_cl) if isinstance(raw_cl, list) else raw_cl
        policies[cid] = CellPolicy(
            cell=cid,
            stage=str(entry["stage"]),
            phase=str(entry["phase"]),
            stage_id=str(sid),
            owner=owner,
            check_list=cl,
            trigger=str(entry["trigger"]),
            budget_seconds=budget,
            scale=str(entry["scale"]),
            enforcement=enf,
            stop_on_fail=sof,
            capabilities=frozenset(caps),
        )
    return pv, boot_cap, MappingProxyType(policies)


POLICY_VERSION, BOOTSTRAP_CAP_SECONDS, CELL_POLICIES = _load_policy_table()


@dataclass(frozen=True)
class StageHookPlan:
    stage: str
    pre_checks: tuple[str, ...]
    operation_slot: str | None
    post_slot: str | None
    stage_id: str = field(kw_only=True)


def _derive_stage_plans(policies: Mapping[str, CellPolicy]) -> Mapping[str, StageHookPlan]:
    plans: dict[str, StageHookPlan] = {}
    for st in ("preview", "staging", "prod", "schedule"):
        pre_p = policies.get(f"{st}.pre")
        op_p = policies.get(f"{st}.operation")
        post_p = policies["scheduled_audit"] if st == "schedule" else policies[f"{st}.post"]
        pre_checks = pre_p.check_list if pre_p and isinstance(pre_p.check_list, tuple) else ()
        op_slot = op_p.check_list if op_p and isinstance(op_p.check_list, str) else None
        post_slot = post_p.check_list if isinstance(post_p.check_list, str) else None
        plans[st] = StageHookPlan(st, pre_checks, op_slot, post_slot, stage_id=post_p.stage_id)
    return MappingProxyType(plans)


STAGE_HOOK_PLANS = _derive_stage_plans(CELL_POLICIES)

_SLOT_TO_CELL: Mapping[str, str] = MappingProxyType(
    {
        "smoke": "preview.operation",
        "preview_gate": "preview.post",
        "staging_gate": "staging.post",
        "prod_gate": "prod.post",
        "scheduled_audit": "scheduled_audit",
    }
)


def stage_hook_plan(stage: str) -> StageHookPlan:
    if not isinstance(stage, str):
        raise ValueError(f"stage must be a string, got {type(stage).__name__}")
    key = stage.strip().lower()
    if key == "production":
        key = "prod"
    if key not in STAGE_HOOK_PLANS:
        raise ValueError(f"unknown stage {stage!r}; allowed: {list(STAGE_HOOK_PLANS)}")
    return STAGE_HOOK_PLANS[key]


FORBIDDEN_HOOK_ENV_NAMES = frozenset(
    {"INFRA2_PAT", "DOKPLOY_API_KEY", "OP_SERVICE_ACCOUNT_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"}
)
FORBIDDEN_HOOK_ENV_PREFIXES = (
    "CLOUDFLARE_",
    "VAULT_",
    "AWS_",
    "SSH_",
    "INFRA2_WATCHDOG_",
    "DOKPLOY_",
)
_HOOK_ENV_NAME_RE = re.compile(r"\A[A-Z][A-Z0-9_]*\Z")
_HOOK_PASSTHROUGH_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TERM",
        "TMPDIR",
        "USER",
        "VIRTUAL_ENV",
        "UV_CACHE_DIR",
        "PLAYWRIGHT_BROWSERS_PATH",
    }
)
_HOOK_MANIFEST_FIELDS = frozenset(
    {"schema_version", "service", "artifacts_dir", "required_env", "optional_env", "checks"}
)
_CHECK_ROW_FIELDS = frozenset(
    {
        "id",
        "argv",
        "needs",
        "writes",
        "accepts_scale",
        "timeout_seconds",
        "bootstrap",
        "required_env",
        "optional_env",
        "checkout_lineage",
        "cells",
        "off",
    }
)
_CHECK_ROW_REQUIRED = frozenset(
    {"id", "argv", "needs", "writes", "accepts_scale", "timeout_seconds"}
)
_HOOK_FAILED_STEPS = frozenset({"", "env", "bootstrap", "run", "timeout"})
_HOOK_STDERR_TAIL_LIMIT = 2000
HOOK_CHECKS_FILE = "checks.jsonl"


def _validate_hook_env_names(names: tuple[str, ...], *, field_name: str) -> None:
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate variable in {field_name}: {names!r}")
    for n in names:
        if not isinstance(n, str) or not _HOOK_ENV_NAME_RE.match(n):
            raise ValueError(f"{field_name} entry {n!r} must match ^[A-Z][A-Z0-9_]*$")
        if n in FORBIDDEN_HOOK_ENV_NAMES or n.startswith(FORBIDDEN_HOOK_ENV_PREFIXES):
            raise ValueError(f"{field_name} entry {n!r} is a forbidden platform secret")


def _validate_env_lists(
    req: Any, opt: Any, *, owner: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    for name, val in (("required_env", req), ("optional_env", opt)):
        if not isinstance(val, (list, tuple)) or any(not isinstance(i, str) for i in val):
            raise ValueError(f"{owner} '{name}' must be a list of strings")
        _validate_hook_env_names(tuple(val), field_name=name)
    req_t, opt_t = tuple(req), tuple(opt)
    if overlap := sorted(set(req_t) & set(opt_t)):
        raise ValueError(f"variables cannot be in both required_env and optional_env: {overlap}")
    return req_t, opt_t


def _check_timeout(val: Any, *, owner: str) -> int:
    if isinstance(val, bool) or not isinstance(val, int) or not (0 < val <= 3600):
        raise ValueError(f"{owner} 'timeout_seconds' must be an int in 1..3600, got {val!r}")
    return val


@dataclass(frozen=True)
class OverrideReason:
    reason: str
    issue: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("override 'reason' must be a non-empty string")
        if not isinstance(self.issue, str) or not self.issue.strip():
            raise ValueError("override 'issue' must be a non-empty string")

    def to_dict(self) -> dict[str, str]:
        return {"reason": self.reason, "issue": self.issue}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OverrideReason:
        if not isinstance(data, Mapping):
            raise ValueError("override must be a JSON object")
        reject_unknown(data, {"reason", "issue"}, "override")
        return cls(data.get("reason", ""), data.get("issue", ""))


def _freeze_cell_map(raw: Any, kind: str) -> Mapping[str, OverrideReason]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"cells.{kind} must be a mapping")
    out: dict[str, OverrideReason] = {}
    for cid, val in raw.items():
        if cid not in CELL_POLICIES:
            raise ValueError(f"unknown cell {cid!r} in cells.{kind}")
        out[cid] = val if isinstance(val, OverrideReason) else OverrideReason.from_dict(val)
    return MappingProxyType(out)


@dataclass(frozen=True)
class CellOverrides:
    add: Mapping[str, OverrideReason] = field(default_factory=lambda: MappingProxyType({}))
    remove: Mapping[str, OverrideReason] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        add_m = _freeze_cell_map(self.add, "add")
        rem_m = _freeze_cell_map(self.remove, "remove")
        if overlap := sorted(set(add_m) & set(rem_m)):
            raise ValueError(f"cell(s) in both cells.add and cells.remove: {overlap}")
        object.__setattr__(self, "add", add_m)
        object.__setattr__(self, "remove", rem_m)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.add:
            out["add"] = {k: v.to_dict() for k, v in self.add.items()}
        if self.remove:
            out["remove"] = {k: v.to_dict() for k, v in self.remove.items()}
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CellOverrides:
        if not isinstance(data, Mapping):
            raise ValueError("cells must be a JSON object")
        reject_unknown(data, {"add", "remove"}, "cells")
        return cls(add=data.get("add", {}), remove=data.get("remove", {}))


@dataclass(frozen=True)
class CheckRow:
    id: str
    argv: tuple[str, ...]
    needs: tuple[str, ...]
    writes: bool = False
    accepts_scale: bool = False
    timeout_seconds: int = 300
    bootstrap: str = field(default="", kw_only=True)
    required_env: tuple[str, ...] = field(default=(), kw_only=True)
    optional_env: tuple[str, ...] = field(default=(), kw_only=True)
    checkout_lineage: bool = field(default=False, kw_only=True)
    cells: CellOverrides = field(default_factory=CellOverrides, kw_only=True)
    off: OverrideReason | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        a = self.argv
        argv_ok = isinstance(a, (list, tuple)) and a and all(isinstance(x, str) and x for x in a)
        if not (isinstance(self.id, str) and self.id.strip() and argv_ok):
            raise ValueError(f"check {self.id!r} needs a non-empty id and non-empty argv")
        object.__setattr__(self, "argv", tuple(a))
        n = self.needs
        if (
            not isinstance(n, (list, tuple))
            or not n
            or any(not isinstance(x, str) or x not in _VALID_NEEDS for x in n)
        ):
            raise ValueError(f"check {self.id!r} needs must be non-empty subset of {_VALID_NEEDS}")
        if len(n) != len(set(n)):
            raise ValueError(f"check {self.id!r} has duplicate needs")
        object.__setattr__(self, "needs", tuple(n))
        if type(self.writes) is not bool or type(self.accepts_scale) is not bool:
            raise ValueError(f"check {self.id!r} 'writes' and 'accepts_scale' must be bools")
        _check_timeout(self.timeout_seconds, owner=f"check {self.id!r}")
        if not isinstance(self.bootstrap, str):
            raise ValueError(f"check {self.id!r} 'bootstrap' must be a string")
        if type(self.checkout_lineage) is not bool:
            raise ValueError(f"check {self.id!r} 'checkout_lineage' must be a bool")
        req_t, opt_t = _validate_env_lists(
            self.required_env, self.optional_env, owner=f"check {self.id!r}"
        )
        object.__setattr__(self, "required_env", req_t)
        object.__setattr__(self, "optional_env", opt_t)
        if not isinstance(self.cells, CellOverrides):
            raise ValueError(f"check {self.id!r} 'cells' must be CellOverrides")
        if self.off is not None and not isinstance(self.off, OverrideReason):
            raise ValueError(f"check {self.id!r} 'off' must be OverrideReason or None")
        resolve_check_cells(self)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "argv": list(self.argv),
            "needs": list(self.needs),
            "writes": self.writes,
            "accepts_scale": self.accepts_scale,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.bootstrap:
            out["bootstrap"] = self.bootstrap
        if self.required_env:
            out["required_env"] = list(self.required_env)
        if self.optional_env:
            out["optional_env"] = list(self.optional_env)
        if self.checkout_lineage:
            out["checkout_lineage"] = True
        if cdict := self.cells.to_dict():
            out["cells"] = cdict
        if self.off is not None:
            out["off"] = self.off.to_dict()
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CheckRow:
        if not isinstance(data, Mapping):
            raise ValueError("check row must be a JSON object")
        reject_unknown(data, _CHECK_ROW_FIELDS, "check row")
        if missing := sorted(_CHECK_ROW_REQUIRED - set(data)):
            raise ValueError(f"check row missing required field(s): {missing}")
        cells_raw = data.get("cells")
        off_raw = data.get("off")
        return cls(
            id=data["id"],
            argv=data["argv"],
            needs=data["needs"],
            writes=data["writes"],
            accepts_scale=data["accepts_scale"],
            timeout_seconds=data["timeout_seconds"],
            bootstrap=data.get("bootstrap", ""),
            required_env=data.get("required_env", ()),
            optional_env=data.get("optional_env", ()),
            checkout_lineage=data.get("checkout_lineage", False),
            cells=CellOverrides.from_dict(cells_raw) if cells_raw is not None else CellOverrides(),
            off=OverrideReason.from_dict(off_raw) if off_raw is not None else None,
        )


def resolve_check_cells(
    check: CheckRow, policies: Mapping[str, CellPolicy] = CELL_POLICIES
) -> tuple[str, ...]:
    """Compute and validate target cells for ``check`` from ``needs``, ``writes``, and overrides."""
    needs_set = set(check.needs)
    if needs_set <= _PRE_NEEDS:
        candidates: tuple[str, ...] = ("local.pre", "ci.pre")
    elif check.id == "smoke":
        candidates = ("preview.operation", "staging.operation", "prod.operation")
    else:
        candidates = ("preview.post", "staging.post", "prod.post", "scheduled_audit")
    defaults = {
        cid
        for cid in candidates
        if needs_set <= policies[cid].capabilities
        and not (check.writes and policies[cid].stage == "prod")
    }
    add_map, rem_map = check.cells.add, check.cells.remove
    for cid in add_map:
        p = policies[cid]
        if p.owner == "platform" and p.check_list != "smoke":
            raise ValueError(f"check {check.id!r} cannot add platform-owned cell {cid!r}")
    resolved = tuple(
        cid for cid in policies if (cid in defaults and cid not in rem_map) or cid in add_map
    )
    for cid in resolved:
        p = policies[cid]
        if check.writes and p.stage == "prod":
            raise ValueError(f"check {check.id!r} has writes=True and cannot run in prod ({cid!r})")
        if not (needs_set <= p.capabilities):
            missing = sorted(needs_set - p.capabilities)
            raise ValueError(f"check {check.id!r} requires {missing} which cell {cid!r} lacks")
        if p.check_list == "smoke" and check.bootstrap.strip():
            raise ValueError(
                f"check {check.id!r} in cell {cid!r} ('smoke') cannot declare bootstrap"
            )
    return resolved


def _check_schema_version(sv: Any) -> int:
    if not isinstance(sv, bool) and sv in SUPPORTED_HOOKS_SCHEMA_VERSIONS:
        return sv
    if sv == 2 and not isinstance(sv, bool):
        raise ValueError(
            "runtime hooks manifest schema_version 2 was removed in 4.0.0; "
            "migrate it to schema_version 3 and run it with run_cell"
        )
    raise ValueError(f"runtime hooks manifest requires schema_version 3, got {sv!r}")


@dataclass(frozen=True)
class RuntimeHooksManifest:
    service: str
    schema_version: int = CURRENT_HOOKS_SCHEMA_VERSION
    checks: tuple[CheckRow, ...] = field(default=(), kw_only=True)
    artifacts_dir: str = field(default=".artifacts/hooks", kw_only=True)
    required_env: tuple[str, ...] = field(default=(), kw_only=True)
    optional_env: tuple[str, ...] = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        _check_schema_version(self.schema_version)
        if not isinstance(self.service, str) or not self.service.strip():
            raise ValueError("runtime hooks manifest 'service' must be a non-empty string")
        if not isinstance(self.artifacts_dir, str) or not self.artifacts_dir.strip():
            raise ValueError("runtime hooks manifest 'artifacts_dir' must be a non-empty string")
        req_t, opt_t = _validate_env_lists(
            self.required_env, self.optional_env, owner="runtime hooks manifest"
        )
        object.__setattr__(self, "required_env", req_t)
        object.__setattr__(self, "optional_env", opt_t)
        if not isinstance(self.checks, (list, tuple)) or not self.checks:
            raise ValueError("schema_version 3 manifest requires a non-empty 'checks' list")
        if any(not isinstance(c, CheckRow) for c in self.checks):
            raise ValueError("every entry in 'checks' must be a CheckRow")
        if len({c.id for c in self.checks}) != len(self.checks):
            raise ValueError("duplicate check id in manifest")
        object.__setattr__(self, "checks", tuple(self.checks))

    def checks_for_cell(self, cell_id: str) -> tuple[CheckRow, ...]:
        if cell_id not in CELL_POLICIES:
            raise ValueError(f"unknown cell {cell_id!r}")
        return tuple(c for c in self.checks if cell_id in resolve_check_cells(c))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"schema_version": 3, "service": self.service}
        if self.artifacts_dir != ".artifacts/hooks":
            out["artifacts_dir"] = self.artifacts_dir
        if self.required_env:
            out["required_env"] = list(self.required_env)
        if self.optional_env:
            out["optional_env"] = list(self.optional_env)
        out["checks"] = [c.to_dict() for c in self.checks]
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RuntimeHooksManifest:
        if not isinstance(data, Mapping):
            raise ValueError("runtime hooks manifest must be a JSON object")
        if "schema_version" not in data:
            raise ValueError("runtime hooks manifest requires 'schema_version'")
        _check_schema_version(data["schema_version"])
        reject_unknown(data, _HOOK_MANIFEST_FIELDS, "runtime hooks manifest")
        raw_checks = data.get("checks")
        if not isinstance(raw_checks, list):
            raise ValueError("schema_version 3 manifest 'checks' must be a JSON list")
        return cls(
            schema_version=3,
            service=data.get("service", ""),
            checks=tuple(CheckRow.from_dict(c) for c in raw_checks),
            artifacts_dir=data.get("artifacts_dir", ".artifacts/hooks"),
            required_env=data.get("required_env", ()),
            optional_env=data.get("optional_env", ()),
        )


def load_hooks_manifest(path: str | Path, *, root: Path | None = None) -> RuntimeHooksManifest:
    target = Path(path) if root is None else root / path
    return RuntimeHooksManifest.from_dict(json.loads(target.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class HookRunResult:
    service: str
    slot: str
    environment: str
    app_url: str
    version_ref: str
    status: str
    exit_code: int
    duration_seconds: float
    failed_step: str = ""
    summary: str = ""
    stderr_tail: str = ""
    checks: tuple[CheckVerdict, ...] = field(default=(), kw_only=True)
    bootstrap_seconds: float = field(default=0.0, kw_only=True)
    cell: str = field(default="", kw_only=True)
    policy_version: int = field(default=POLICY_VERSION, kw_only=True)

    def __post_init__(self) -> None:
        if self.status not in ("pass", "fail"):
            raise ValueError(f"status must be 'pass' or 'fail', got {self.status!r}")
        if self.failed_step not in _HOOK_FAILED_STEPS:
            raise ValueError(
                f"failed_step must be one of {sorted(_HOOK_FAILED_STEPS)}, got {self.failed_step!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["duration_seconds"] = round(self.duration_seconds, 3)
        d["bootstrap_seconds"] = round(self.bootstrap_seconds, 3)
        return d


def build_hook_env(
    *,
    required_env: tuple[str, ...] = (),
    optional_env: tuple[str, ...] = (),
    slot: str,
    app_url: str,
    environment: str,
    version_ref: str,
    artifacts_dir: str,
    ambient_env: Mapping[str, str],
    extra_env: Mapping[str, str] = MappingProxyType({}),
) -> dict[str, str]:
    clean = {
        k: ambient_env[k] for k in ambient_env if k in _HOOK_PASSTHROUGH_KEYS and ambient_env[k]
    }
    for env_key, env_val in (("PYTHONPATH", "."),):
        clean[env_key] = env_val
    if missing := [k for k in required_env if not ambient_env.get(k, "").strip()]:
        raise ValueError(f"missing required hook env: {', '.join(missing)}")
    for k in required_env:
        clean[k] = ambient_env[k]
    for k in optional_env:
        if (v := ambient_env.get(k, "")).strip():
            clean[k] = v
    for env_key, env_val in (
        *((("APP_URL", app_url),) if app_url else ()),
        ("ENVIRONMENT", environment),
        ("APP_VERSION_REF", version_ref),
        ("HOOK_SLOT", slot),
        ("HOOK_ARTIFACTS_DIR", artifacts_dir),
    ):
        clean[env_key] = env_val
    for k, v in extra_env.items():
        clean[k] = v
    return clean


def _strip_stream(v: str | bytes | None) -> str:
    return (
        "" if v is None else (v.decode("utf-8", "replace") if isinstance(v, bytes) else v).strip()
    )


def _format_hook_output_tail(
    stdout: str | bytes | None, stderr: str | bytes | None, fallback: str
) -> str:
    out, err = _strip_stream(stdout), _strip_stream(stderr)
    if out and err:
        lim = (_HOOK_STDERR_TAIL_LIMIT - len("--- stdout ---\n\n--- stderr ---\n")) // 2
        return f"--- stdout ---\n{out[-lim:]}\n--- stderr ---\n{err[-lim:]}"
    return (err or out)[-_HOOK_STDERR_TAIL_LIMIT:] if (err or out) else fallback


def _resolve_cell_id(stage_or_cell: str, phase: str | None = None, *, environment: str = "") -> str:
    if phase is None:
        key = stage_or_cell.strip().lower()
        if key in ("schedule", "scheduled_audit"):
            return "scheduled_audit"
        if key == "smoke":
            env_k = environment.strip().lower()
            if env_k in ("prod", "production"):
                return "prod.operation"
            if env_k == "staging":
                return "staging.operation"
            return "preview.operation"
        if key in _SLOT_TO_CELL:
            return _SLOT_TO_CELL[key]
        if key in CELL_POLICIES:
            return key
        raise ValueError(f"unknown cell {stage_or_cell!r}")
    st = "prod" if stage_or_cell.strip().lower() == "production" else stage_or_cell.strip().lower()
    ph = phase.strip().lower()
    if st == "schedule" and ph == "post":
        return "scheduled_audit"
    cid = f"{st}.{ph}"
    if cid not in CELL_POLICIES:
        raise ValueError(f"unknown cell ({stage_or_cell!r}, {phase!r})")
    return cid


def _cell_rows_and_spec(
    manifest: RuntimeHooksManifest, cell_id: str
) -> tuple[list[CheckRow], str, int, tuple[str, ...], tuple[str, ...]]:
    rows = [c for c in manifest.checks if cell_id in resolve_check_cells(c)]
    active = [c for c in rows if c.off is None]
    policy = CELL_POLICIES[cell_id]
    req = tuple(
        dict.fromkeys([*manifest.required_env, *(e for r in active for e in r.required_env)])
    )
    opt = tuple(
        dict.fromkeys(
            e
            for e in [*manifest.optional_env, *(e for r in active for e in r.optional_env)]
            if e not in req
        )
    )
    budget = (
        min(sum(r.timeout_seconds for r in active), policy.budget_seconds)
        if active
        else policy.budget_seconds
    )
    return rows, manifest.artifacts_dir, budget, req, opt


def _run_step(
    argv: Sequence[str],
    *,
    step_name: str,
    check_id: str,
    timeout: float,
    clean_env: Mapping[str, str],
    cwd: Path | None,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    bad: Callable[[str, str, float], None],
    flush: Callable[[], None],
    timeout_label: str,
) -> tuple[bool, int, str, str, float]:
    t0 = time.monotonic()
    try:
        proc = runner(
            list(argv),
            env=clean_env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"{step_name} timed out {timeout_label}"
        bad(check_id, msg, t0)
        return (
            False,
            124,
            "timeout",
            _format_hook_output_tail(exc.stdout, exc.stderr, msg),
            time.monotonic() - t0,
        )
    except BaseException as exc:
        bad(check_id, scrub(str(exc), clean_env), t0)
        flush()
        raise
    dur = time.monotonic() - t0
    if proc.returncode != 0:
        msg = f"{step_name} exited with code {proc.returncode}"
        tail = _format_hook_output_tail(proc.stdout, proc.stderr, msg)
        first = (proc.stderr or "").strip() or (proc.stdout or "").strip() or msg
        bad(check_id, scrub(first.splitlines()[0], clean_env), t0)
        return False, proc.returncode, step_name, tail, dur
    return True, 0, "", "", dur


def _execute_cell(
    manifest: RuntimeHooksManifest,
    cell_id: str,
    *,
    slot_label: str,
    app_url: str,
    environment: str,
    version_ref: str,
    cwd: Path | None,
    ambient_env: Mapping[str, str] | None,
    result_path: Path | None,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> HookRunResult:
    policy = CELL_POLICIES[cell_id]
    effective_stop = policy.stop_on_fail
    if "running_service" in policy.capabilities and (not app_url or not app_url.strip()):
        raise ValueError("app_url must be a non-empty string")
    src_env = os.environ if ambient_env is None else ambient_env
    url, start = app_url.strip(), time.monotonic()
    rows, artifacts_dir, budget, req_env, opt_env = _cell_rows_and_spec(manifest, cell_id)
    checks_file = (cwd or Path()) / artifacts_dir / HOOK_CHECKS_FILE
    checks_file.unlink(missing_ok=True)
    done: list[CheckVerdict] = []
    boot_seconds = 0.0

    def flush() -> None:
        checks_file.parent.mkdir(parents=True, exist_ok=True)
        record_verdicts(checks_file, source=slot_label, checks=done)

    def bad(check_id: str, detail: str, t0: float) -> None:
        done.append(
            CheckVerdict(check_id or slot_label, False, detail, seconds=time.monotonic() - t0)
        )

    def finish(st: str, code: int, fstep: str, summary: str, tail: str = "") -> HookRunResult:
        if st == "fail" and not done:
            bad(slot_label, summary, start)
        res = HookRunResult(
            manifest.service,
            slot_label,
            environment,
            url,
            version_ref,
            st,
            code,
            time.monotonic() - start,
            fstep,
            summary,
            tail,
            checks=tuple(done),
            bootstrap_seconds=boot_seconds,
            cell=cell_id,
            policy_version=POLICY_VERSION,
        )
        flush()
        if result_path is not None:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(
                json.dumps(res.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        return res

    active = [r for r in rows if r.off is None]
    if not active:
        for r in rows:
            done.append(
                CheckVerdict(r.id, False, f"skipped: {r.off.reason} ({r.off.issue})", skipped=True)
            )
        return finish("fail", 1, "run", f"cell {cell_id} ran 0 active checks")

    extra: dict[str, str] = {
        "HOOK_CELL": cell_id,
        "HOOK_POLICY_VERSION": str(POLICY_VERSION),
        **({"HOOK_SCALE": policy.scale} if any(r.accepts_scale for r in active) else {}),
    }
    try:
        clean_env = build_hook_env(
            required_env=req_env,
            optional_env=opt_env,
            slot=slot_label,
            app_url=url,
            environment=environment,
            version_ref=version_ref,
            artifacts_dir=artifacts_dir,
            ambient_env=src_env,
            extra_env=extra,
        )
    except ValueError as err:
        return finish("fail", 2, "env", str(err), str(err))

    boot_cmds = list(dict.fromkeys(r.bootstrap.strip() for r in active if r.bootstrap.strip()))
    boot_start = time.monotonic()
    boot_end = boot_start + BOOTSTRAP_CAP_SECONDS
    boot_label = f"after {BOOTSTRAP_CAP_SECONDS}s (bootstrap cap {BOOTSTRAP_CAP_SECONDS}s)"
    for cmd in boot_cmds:
        ok, code, fstep, tail, _ = _run_step(
            ["sh", "-lc", cmd],
            step_name="bootstrap",
            check_id="",
            timeout=max(boot_end - time.monotonic(), 0.001),
            clean_env=clean_env,
            cwd=cwd,
            runner=runner,
            bad=bad,
            flush=flush,
            timeout_label=boot_label,
        )
        boot_seconds = time.monotonic() - boot_start
        if not ok:
            msg = (
                f"bootstrap timed out {boot_label}"
                if fstep == "timeout"
                else f"bootstrap exited with code {code}"
            )
            return finish("fail", code, fstep, msg, tail)
    # The check budget starts after bootstrap, so a slow bootstrap never shortens a check.
    end = time.monotonic() + budget

    first_fail: tuple[int, str, str, str] | None = None
    for r in rows:
        if r.off is not None:
            done.append(
                CheckVerdict(r.id, False, f"skipped: {r.off.reason} ({r.off.issue})", skipped=True)
            )
            continue
        rem = max(end - time.monotonic(), 0.001)
        if r.timeout_seconds < budget and r.timeout_seconds <= rem:
            t_label = f"after {r.timeout_seconds}s (check timeout {r.timeout_seconds}s)"
        else:
            t_label = f"after {budget}s (cell budget {budget}s)"
        ok, code, fstep, tail, dur = _run_step(
            r.argv,
            step_name="run",
            check_id=r.id,
            timeout=min(r.timeout_seconds, rem),
            clean_env=clean_env,
            cwd=cwd,
            runner=runner,
            bad=bad,
            flush=flush,
            timeout_label=t_label,
        )
        if not ok:
            msg = (
                f"run timed out {t_label}" if fstep == "timeout" else f"run exited with code {code}"
            )
            if first_fail is None:
                first_fail = (code, fstep, msg, tail)
            if effective_stop or (fstep == "timeout" and end - time.monotonic() <= 0):
                return finish("fail", *first_fail)
        else:
            done.append(CheckVerdict(r.id, True, seconds=dur))
    if first_fail is not None:
        return finish("fail", *first_fail)
    return finish("pass", 0, "", f"{slot_label} passed")


def run_cell(
    manifest: RuntimeHooksManifest,
    stage_or_cell: str,
    phase: str | None = None,
    *,
    app_url: str = "",
    environment: str = "local_dev",
    version_ref: str = "local",
    cwd: Path | None = None,
    ambient_env: Mapping[str, str] | None = None,
    result_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> HookRunResult:
    """Execute checks of ``manifest`` for ``(stage, phase)`` or ``scheduled_audit``."""
    cell_id = _resolve_cell_id(stage_or_cell, phase, environment=environment)
    p = CELL_POLICIES[cell_id]
    slot_label = p.check_list if isinstance(p.check_list, str) else cell_id
    return _execute_cell(
        manifest,
        cell_id,
        slot_label=slot_label,
        app_url=app_url,
        environment=environment,
        version_ref=version_ref,
        cwd=cwd,
        ambient_env=ambient_env,
        result_path=result_path,
        runner=runner,
    )


def format_check_grid(manifest: RuntimeHooksManifest) -> str:
    """Render a markdown table of checks by hook-grid cells for ``manifest``."""
    cols = [cid for cid, p in CELL_POLICIES.items() if p.owner == "app" or p.check_list == "smoke"]
    header = (
        f"Service: {manifest.service} (schema v{manifest.schema_version}, policy v{POLICY_VERSION})"
    )
    lines = [
        header,
        "",
        "| Check | " + " | ".join(cols) + " |",
        "|---|" + "|".join("---" for _ in cols) + "|",
    ]
    for row in manifest.checks:
        resolved = set(resolve_check_cells(row))
        cells = [
            ("off" if row.off is not None else f"{row.timeout_seconds}s")
            if cid in resolved
            else "-"
            for cid in cols
        ]
        lines.append(f"| `{row.id}` | " + " | ".join(cells) + " |")
    return "\n".join(lines)


@dataclass(frozen=True)
class ManifestSpec:
    path: str
    model: str | type
    source: str | None = None
    overrides: str | Mapping[str, Mapping[str, object]] | None = None

    def resolve_model(self) -> type:
        if isinstance(self.model, type):
            return self.model
        mod, _, attr = self.model.partition(":")
        if not attr:
            raise ValueError(f"model must be 'package.module:Class', got {self.model!r}")
        return getattr(import_module(mod), attr)

    def resolve_overrides(self) -> Mapping[str, Mapping[str, object]] | None:
        if self.overrides is None or isinstance(self.overrides, Mapping):
            return self.overrides
        mod, _, attr = self.overrides.partition(":")
        if not attr:
            raise ValueError(f"overrides must be 'package.module:NAME', got {self.overrides!r}")
        tbl = getattr(import_module(mod), attr, None)
        if not isinstance(tbl, Mapping):
            raise ValueError(f"overrides {self.overrides!r} must name an existing mapping")
        return dict(tbl)

    def build(self) -> EnvironmentManifest:
        return environment_manifest_from_model(
            self.resolve_model(), source=self.source, overrides=self.resolve_overrides()
        )


def _render(manifest: EnvironmentManifest) -> str:
    return json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n"


def load(path: str | Path, *, root: Path | None = None) -> EnvironmentManifest:
    return _load_manifest(path, root=root)


def all_manifests(
    *, root: Path, specs: Iterable[ManifestSpec], hand_written: Iterable[str] = ()
) -> dict[str, EnvironmentManifest]:
    return {
        **{s.path: s.build() for s in specs},
        **{p: load(p, root=root) for p in hand_written},
    }


def check(
    *, root: Path, specs: Iterable[ManifestSpec], hand_written: Iterable[str] = ()
) -> list[str]:
    specs = tuple(specs)
    problems = [
        f"{s.path}: stale, regenerate with --write"
        for s in specs
        if not (t := root / s.path).exists() or t.read_text(encoding="utf-8") != _render(s.build())
    ]
    for path, mf in all_manifests(root=root, specs=specs, hand_written=hand_written).items():
        problems.extend(f"{path}: {err}" for err in validate_manifest_offline(mf))
        names = [name for fld in mf.fields for name in (fld.env, *fld.aliases)]
        problems.extend(f"{path}: {err}" for err in check_shared_env_conformance(names))
    return problems


def _write(
    *, root: Path, specs: Iterable[ManifestSpec], echo: Callable[[str], None] = print
) -> list[str]:
    written: list[str] = []
    for s in specs:
        t = root / s.path
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(_render(s.build()), encoding="utf-8")
        written.append(s.path)
        echo(f"wrote {s.path}")
    return written


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path | None = None,
    specs: Iterable[ManifestSpec] = (),
    hand_written: Iterable[str] = (),
    prog: str = "env_manifest",
    echo: Callable[[str], None] = print,
) -> int:
    r = Path.cwd() if root is None else root
    p = argparse.ArgumentParser(prog=prog, description=__doc__.splitlines()[0])
    m = p.add_mutually_exclusive_group(required=True)
    for flag in ("--write", "--check"):
        m.add_argument(flag, action="store_true")
    for flag in ("--validate-env", "--check-hooks", "--run-cell"):
        m.add_argument(flag)
    p.add_argument("--no-require-injected", action="store_true")
    p.add_argument("--hooks-manifest", default="runtime-hooks.manifest.json")
    p.add_argument("--app-url", "--base-url", dest="app_url", default="")
    p.add_argument("--env", dest="environment", default="local_dev")
    p.add_argument("--version-ref", default="local")
    p.add_argument("--result-file", default="")
    a = p.parse_args(list(argv) if argv is not None else None)
    specs = tuple(specs)
    if a.write:
        _write(root=r, specs=specs, echo=echo)
        return 0
    if a.check:
        probs = check(root=r, specs=specs, hand_written=hand_written)
        for pr in probs:
            echo(pr)
        echo("env manifests: ok" if not probs else f"env manifests: {len(probs)} problem(s)")
        return 1 if probs else 0
    if a.check_hooks:
        try:
            hm = load_hooks_manifest(a.check_hooks, root=r)
        except (OSError, ValueError) as err:
            echo(f"{a.check_hooks}: {err}")
            return 1
        detail = f"{hm.service}, v{hm.schema_version}, {len(hm.checks)} checks"
        echo(f"runtime hooks manifest: ok ({detail})")
        return 0
    if a.run_cell:
        if not a.app_url.strip():
            echo("--run-cell requires --app-url")
            return 2
        try:
            hm = load_hooks_manifest(a.hooks_manifest, root=r)
        except (OSError, ValueError) as err:
            echo(f"{a.hooks_manifest}: {err}")
            return 2
        rp = (c if (c := Path(a.result_file)).is_absolute() else r / c) if a.result_file else None
        try:
            res = run_cell(
                hm,
                a.run_cell,
                app_url=a.app_url,
                environment=a.environment,
                version_ref=a.version_ref,
                cwd=r,
                result_path=rp,
            )
        except ValueError as err:
            echo(f"--run-cell {a.run_cell}: {err}")
            return 2
        echo(json.dumps(res.to_dict(), ensure_ascii=False))
        return 0 if res.status == "pass" else (res.exit_code or 1)
    missing = _validate_env(a.validate_env, root=r, require_injected=not a.no_require_injected)
    for name in missing:
        echo(f"missing: {name}")
    echo("environment: ok" if not missing else f"environment: {len(missing)} missing")
    return 0 if not missing else 1


__all__ = [
    "CELL_POLICIES",
    "CURRENT_HOOKS_SCHEMA_VERSION",
    "FORBIDDEN_HOOK_ENV_NAMES",
    "FORBIDDEN_HOOK_ENV_PREFIXES",
    "HOOK_CHECKS_FILE",
    "POLICY_VERSION",
    "STAGE_HOOK_PLANS",
    "SUPPORTED_HOOKS_SCHEMA_VERSIONS",
    "CellOverrides",
    "CellPolicy",
    "CheckRow",
    "HookRunResult",
    "ManifestSpec",
    "OverrideReason",
    "RuntimeHooksManifest",
    "StageHookPlan",
    "all_manifests",
    "build_hook_env",
    "check",
    "format_check_grid",
    "load",
    "load_hooks_manifest",
    "main",
    "resolve_check_cells",
    "run_cell",
    "stage_hook_plan",
]

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(root=Path.cwd(), specs=()))
