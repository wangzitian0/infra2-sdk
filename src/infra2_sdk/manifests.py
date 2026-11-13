"""Environment and runtime-hooks manifest CLI driver."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import warnings
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from importlib import import_module
from pathlib import Path
from types import MappingProxyType
from typing import Any

from infra2_sdk._wire import reject_unknown
from infra2_sdk.ci import validate_manifest_offline
from infra2_sdk.observability import CheckVerdict, record_verdicts, scrub
from infra2_sdk.runtime.config_schema import (
    EnvironmentManifest,
    _load_manifest,
    _validate_env,
    environment_manifest_from_model,
)

CURRENT_HOOKS_SCHEMA_VERSION = 2
HOOK_SLOTS = ("smoke", "preview_gate", "staging_gate", "prod_gate", "scheduled_audit")
LEGACY_V1_HOOK_SLOTS = ("smoke", "preview_gate", "staging_gate", "scheduled_audit")
SLOT_MAX_TIMEOUT_SECONDS: Mapping[str, int] = MappingProxyType(
    {
        "smoke": 120,
        "preview_gate": 360,
        "staging_gate": 600,
        "prod_gate": 300,
        "scheduled_audit": 3600,
    }
)


_RELEASE_PRE_CHECKS = ("contract", "provenance", "env", "upstream_post")


@dataclass(frozen=True)
class StageHookPlan:
    stage: str
    pre_checks: tuple[str, ...]
    operation_slot: str | None
    post_slot: str | None

    @property
    def deploy_slot(self) -> str | None:
        warnings.warn(
            "StageHookPlan.deploy_slot is removed in v3.31.0; use operation_slot",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.operation_slot


STAGE_HOOK_PLANS: Mapping[str, StageHookPlan] = MappingProxyType(
    {
        "preview": StageHookPlan("preview", ("contract",), "smoke", "preview_gate"),
        "staging": StageHookPlan("staging", _RELEASE_PRE_CHECKS, "smoke", "staging_gate"),
        "prod": StageHookPlan("prod", _RELEASE_PRE_CHECKS, "smoke", "prod_gate"),
        "schedule": StageHookPlan("schedule", (), None, "scheduled_audit"),
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
_HOOK_SLOT_FIELDS = frozenset(
    {
        "run",
        "bootstrap",
        "timeout_seconds",
        "required_env",
        "optional_env",
        "artifacts_dir",
        "checkout_lineage",
        "checks",
    }
)
_HOOK_MANIFEST_FIELDS = frozenset({"schema_version", "service", "slots"})
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


@dataclass(frozen=True)
class CheckSpec:
    id: str
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("hook check 'id' must be a non-empty string")
        if not isinstance(self.argv, (list, tuple)) or not self.argv:
            raise ValueError(f"hook check {self.id!r} 'argv' must be a non-empty list")
        if any(not isinstance(a, str) or not a for a in self.argv):
            raise ValueError(f"hook check {self.id!r} 'argv' must hold non-empty strings")
        object.__setattr__(self, "argv", tuple(self.argv))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CheckSpec:
        if not isinstance(data, Mapping):
            raise ValueError("hook check must be a JSON object")
        reject_unknown(data, {"id", "argv"}, "hook check")
        return cls(data.get("id", ""), data.get("argv", ()))


@dataclass(frozen=True)
class HookSlotSpec:
    run: str = ""
    bootstrap: str = ""
    timeout_seconds: int = 300
    required_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    artifacts_dir: str = ".artifacts/hooks"
    checkout_lineage: bool = field(default=False, kw_only=True)
    checks: tuple[CheckSpec, ...] = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.run, str) or bool(self.run.strip()) == bool(self.checks):
            raise ValueError("hook slot needs 'run' as a non-empty string or 'checks', not both")
        checks = tuple(
            c if isinstance(c, CheckSpec) else CheckSpec.from_dict(c) for c in self.checks
        )
        if len({c.id for c in checks}) != len(checks):
            raise ValueError("hook slot 'checks' ids must be unique")
        object.__setattr__(self, "checks", checks)
        if not isinstance(self.bootstrap, str):
            raise ValueError("hook slot 'bootstrap' must be a string")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or not (0 < self.timeout_seconds <= 3600)
        ):
            raise ValueError(
                "hook slot 'timeout_seconds' must be an int in 1..3600, "
                f"got {self.timeout_seconds!r}"
            )
        if not isinstance(self.checkout_lineage, bool):
            raise ValueError("hook slot 'checkout_lineage' must be a bool")
        for f in ("required_env", "optional_env"):
            val = getattr(self, f)
            if not isinstance(val, (list, tuple)) or any(not isinstance(i, str) for i in val):
                raise ValueError(f"hook slot '{f}' must be a list of strings")
            object.__setattr__(self, f, tuple(val))
            _validate_hook_env_names(getattr(self, f), field_name=f)
        if overlap := sorted(set(self.required_env) & set(self.optional_env)):
            raise ValueError(
                f"variables cannot be in both required_env and optional_env: {overlap}"
            )
        if not isinstance(self.artifacts_dir, str) or not self.artifacts_dir.strip():
            raise ValueError("hook slot 'artifacts_dir' must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "run": self.run,
            "bootstrap": self.bootstrap,
            "timeout_seconds": self.timeout_seconds,
            "required_env": list(self.required_env),
            "optional_env": list(self.optional_env),
            "artifacts_dir": self.artifacts_dir,
        }
        if self.checkout_lineage:
            out["checkout_lineage"] = True
        if self.checks:
            out["checks"] = [{"id": c.id, "argv": list(c.argv)} for c in self.checks]
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HookSlotSpec:
        if not isinstance(data, Mapping):
            raise ValueError("hook slot spec must be a JSON object")
        reject_unknown(data, _HOOK_SLOT_FIELDS, "hook slot")
        return cls(**data)


def _check_slot_keys(slots: Mapping[str, Any], *, schema_version: int) -> None:
    req = HOOK_SLOTS if schema_version == CURRENT_HOOKS_SCHEMA_VERSION else LEGACY_V1_HOOK_SLOTS
    if invalid := sorted(set(slots) - set(HOOK_SLOTS)):
        raise ValueError(f"unknown hook slot(s) {invalid}; allowed slots are {list(HOOK_SLOTS)}")
    if schema_version == 1 and "prod_gate" in slots:
        raise ValueError("schema_version 1 cannot declare 'prod_gate'; upgrade to schema_version 2")
    if missing := [s for s in req if s not in slots]:
        raise ValueError(f"runtime hooks manifest is missing required slot(s): {missing}")


def _check_schema_version(sv: Any) -> int:
    if isinstance(sv, bool) or sv not in (1, CURRENT_HOOKS_SCHEMA_VERSION):
        raise ValueError(f"runtime hooks manifest requires schema_version 1 or 2, got {sv!r}")
    return sv


@dataclass(frozen=True)
class RuntimeHooksManifest:
    service: str
    slots: Mapping[str, HookSlotSpec]
    schema_version: int = CURRENT_HOOKS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _check_schema_version(self.schema_version)
        if not isinstance(self.service, str) or not self.service.strip():
            raise ValueError("runtime hooks manifest 'service' must be a non-empty string")
        if not isinstance(self.slots, Mapping):
            raise ValueError("runtime hooks manifest 'slots' must be a mapping")
        norm = dict(self.slots)
        _check_slot_keys(norm, schema_version=self.schema_version)
        if self.schema_version == 1:
            norm["prod_gate"] = norm["smoke"]
        for s in HOOK_SLOTS:
            spec = norm[s]
            if not isinstance(spec, HookSlotSpec):
                raise ValueError(f"slot {s!r} must be a HookSlotSpec")
            if self.schema_version == CURRENT_HOOKS_SCHEMA_VERSION:
                if spec.timeout_seconds > (lim := SLOT_MAX_TIMEOUT_SECONDS[s]):
                    raise ValueError(
                        f"slot {s!r} timeout_seconds {spec.timeout_seconds} exceeds {lim}"
                    )
                if s == "smoke" and spec.bootstrap.strip():
                    raise ValueError("slot 'smoke' cannot declare bootstrap in schema_version 2")
        object.__setattr__(self, "slots", MappingProxyType({s: norm[s] for s in HOOK_SLOTS}))

    def to_dict(self) -> dict[str, Any]:
        keys = (
            HOOK_SLOTS
            if self.schema_version == CURRENT_HOOKS_SCHEMA_VERSION
            else LEGACY_V1_HOOK_SLOTS
        )
        return {
            "schema_version": self.schema_version,
            "service": self.service,
            "slots": {s: self.slots[s].to_dict() for s in keys},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RuntimeHooksManifest:
        if not isinstance(data, Mapping):
            raise ValueError("runtime hooks manifest must be a JSON object")
        reject_unknown(data, _HOOK_MANIFEST_FIELDS, "runtime hooks manifest")
        if "schema_version" not in data:
            raise ValueError("runtime hooks manifest requires 'schema_version'")
        sv = _check_schema_version(data["schema_version"])
        raw = data.get("slots")
        if not isinstance(raw, Mapping):
            raise ValueError("runtime hooks manifest 'slots' must be a JSON object")
        _check_slot_keys(raw, schema_version=sv)
        keys = HOOK_SLOTS if sv == CURRENT_HOOKS_SCHEMA_VERSION else LEGACY_V1_HOOK_SLOTS
        return cls(
            schema_version=sv,
            service=data.get("service", ""),
            slots={
                s: HookSlotSpec.from_dict(raw[s]) if isinstance(raw[s], Mapping) else raw[s]
                for s in keys
            },
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
    checks: tuple[CheckVerdict, ...] = ()

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
        return d


def build_hook_env(
    slot_spec: HookSlotSpec,
    *,
    slot: str,
    app_url: str,
    environment: str,
    version_ref: str,
    artifacts_dir: str,
    ambient_env: Mapping[str, str],
) -> dict[str, str]:
    clean = {
        k: ambient_env[k] for k in ambient_env if k in _HOOK_PASSTHROUGH_KEYS and ambient_env[k]
    }
    for env_key, env_val in (("PYTHONPATH", "."),):
        clean[env_key] = env_val
    if missing := [k for k in slot_spec.required_env if not ambient_env.get(k, "").strip()]:
        raise ValueError(f"missing required hook env: {', '.join(missing)}")
    for k in slot_spec.required_env:
        clean[k] = ambient_env[k]
    for k in slot_spec.optional_env:
        if (v := ambient_env.get(k, "")).strip():
            clean[k] = v
    for env_key, env_val in (
        ("APP_URL", app_url),
        ("DEPLOY_BASE_URL", app_url),
        ("STAGING_BASE_URL", app_url),
        ("TA_BASE_URL", app_url),
        ("ENVIRONMENT", environment),
        ("APP_VERSION_REF", version_ref),
        ("EXPECTED_SHA", version_ref),
        ("HOOK_SLOT", slot),
        ("HOOK_ARTIFACTS_DIR", artifacts_dir),
    ):
        clean[env_key] = env_val
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


def run_hook_slot(
    manifest: RuntimeHooksManifest,
    slot: str,
    *,
    app_url: str,
    environment: str = "local_dev",
    version_ref: str = "local",
    cwd: Path | None = None,
    ambient_env: Mapping[str, str] | None = None,
    result_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> HookRunResult:
    if slot not in manifest.slots:
        raise ValueError(f"unknown hook slot {slot!r}; available: {list(manifest.slots)}")
    if not app_url or not app_url.strip():
        raise ValueError("app_url must be a non-empty string")
    spec = manifest.slots[slot]
    src_env = os.environ if ambient_env is None else ambient_env
    url, start = app_url.strip(), time.monotonic()
    checks_file = (cwd or Path()) / spec.artifacts_dir / HOOK_CHECKS_FILE
    checks_file.unlink(missing_ok=True)
    done: list[CheckVerdict] = []

    def finish(st: str, code: int, fstep: str, summary: str, tail: str = "") -> HookRunResult:
        if st == "fail" and all(c.ok for c in done):
            done.append(CheckVerdict(slot, False, summary))
        res = HookRunResult(
            manifest.service,
            slot,
            environment,
            url,
            version_ref,
            st,
            code,
            time.monotonic() - start,
            fstep,
            summary,
            tail,
            tuple(done),
        )
        checks_file.parent.mkdir(parents=True, exist_ok=True)
        record_verdicts(checks_file, source=slot, checks=done)
        if result_path is not None:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(
                json.dumps(res.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        return res

    try:
        clean_env = build_hook_env(
            spec,
            slot=slot,
            app_url=url,
            environment=environment,
            version_ref=version_ref,
            artifacts_dir=spec.artifacts_dir,
            ambient_env=src_env,
        )
    except ValueError as err:
        return finish("fail", 2, "env", str(err), str(err))

    steps = [(c.id, list(c.argv)) for c in spec.checks] or [(slot, ["sh", "-lc", spec.run])]
    if spec.bootstrap.strip():
        steps.insert(0, ("", ["sh", "-lc", spec.bootstrap]))
    for check_id, argv in steps:
        step_name = "run" if check_id else "bootstrap"
        t0 = time.monotonic()
        try:
            proc = runner(
                argv,
                env=clean_env,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=spec.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            msg = f"{step_name} timed out after {spec.timeout_seconds}s"
            done.append(CheckVerdict(check_id or slot, False, msg, seconds=time.monotonic() - t0))
            return finish(
                "fail", 124, "timeout", msg, _format_hook_output_tail(exc.stdout, exc.stderr, msg)
            )
        if proc.returncode != 0:
            msg = f"{step_name} exited with code {proc.returncode}"
            tail = _format_hook_output_tail(proc.stdout, proc.stderr, msg)
            first = next(
                (ln for ln in tail.splitlines() if ln.strip() and not ln.startswith("--- std")), msg
            )
            done.append(
                CheckVerdict(
                    check_id or slot,
                    False,
                    scrub(first.strip(), clean_env),
                    seconds=time.monotonic() - t0,
                )
            )
            return finish("fail", proc.returncode, step_name, msg, tail)
        if check_id:
            done.append(CheckVerdict(check_id, True, seconds=time.monotonic() - t0))
    return finish("pass", 0, "", f"{slot} passed")


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
    for flag in ("--validate-env", "--check-hooks"):
        m.add_argument(flag)
    m.add_argument("--run-hook", choices=HOOK_SLOTS)
    p.add_argument("--no-require-injected", action="store_true")
    p.add_argument("--allow-legacy-schema", action="store_true")
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
        if not a.allow_legacy_schema and hm.schema_version != CURRENT_HOOKS_SCHEMA_VERSION:
            echo(
                f"{a.check_hooks}: requires schema_version {CURRENT_HOOKS_SCHEMA_VERSION}, "
                f"got {hm.schema_version}"
            )
            return 1
        slot_count = (
            len(HOOK_SLOTS)
            if hm.schema_version == CURRENT_HOOKS_SCHEMA_VERSION
            else len(LEGACY_V1_HOOK_SLOTS)
        )
        echo(f"runtime hooks manifest: ok ({hm.service}, v{hm.schema_version}, {slot_count} slots)")
        return 0
    if a.run_hook:
        if not a.app_url.strip():
            echo("--run-hook requires --app-url")
            return 2
        try:
            hm = load_hooks_manifest(a.hooks_manifest, root=r)
        except (OSError, ValueError) as err:
            echo(f"{a.hooks_manifest}: {err}")
            return 2
        rp = (c if (c := Path(a.result_file)).is_absolute() else r / c) if a.result_file else None
        res = run_hook_slot(
            hm,
            a.run_hook,
            app_url=a.app_url,
            environment=a.environment,
            version_ref=a.version_ref,
            cwd=r,
            result_path=rp,
        )
        echo(json.dumps(res.to_dict(), ensure_ascii=False))
        return 0 if res.status == "pass" else (res.exit_code or 1)
    missing = _validate_env(a.validate_env, root=r, require_injected=not a.no_require_injected)
    for name in missing:
        echo(f"missing: {name}")
    echo("environment: ok" if not missing else f"environment: {len(missing)} missing")
    return 0 if not missing else 1


__all__ = [
    "CURRENT_HOOKS_SCHEMA_VERSION",
    "FORBIDDEN_HOOK_ENV_NAMES",
    "FORBIDDEN_HOOK_ENV_PREFIXES",
    "HOOK_CHECKS_FILE",
    "HOOK_SLOTS",
    "CheckSpec",
    "HookRunResult",
    "HookSlotSpec",
    "LEGACY_V1_HOOK_SLOTS",
    "ManifestSpec",
    "RuntimeHooksManifest",
    "STAGE_HOOK_PLANS",
    "StageHookPlan",
    "all_manifests",
    "build_hook_env",
    "check",
    "load",
    "load_hooks_manifest",
    "main",
    "run_hook_slot",
    "stage_hook_plan",
]

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(root=Path.cwd(), specs=()))
