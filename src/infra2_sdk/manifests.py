"""Environment manifests as a repository maintains them: generate, check, validate.

Every application keeps one ``required-env.generated.json`` per settings model (contract
v2) and, for services without a Python settings model, a hand-written manifest. The
platform derives Vault Agent templates, policies and the daily reconciliation from those
files; the repository's only job is to keep them true. This module is the one driver
for that job, so no repository re-implements freshness checks, the offline gate, or the
boot-time environment validation::

    # tools/env_manifest.py (an application repository)
    from infra2_sdk.manifests import ManifestSpec, main

    SPECS = (
        ManifestSpec("apps/data-engine/required-env.generated.json",
                     "data_engine.config:Settings", source="apps/data-engine"),
    )
    HAND_WRITTEN = ("apps/app-web/required-env.manifest.json",)
    raise SystemExit(main(root=ROOT, specs=SPECS, hand_written=HAND_WRITTEN))

    uv run python tools/env_manifest.py --write
    uv run python tools/env_manifest.py --check                  # CI: fresh + offline gate
    uv run python tools/env_manifest.py --validate-env apps/x/required-env.generated.json

A model's supply-chain attributes may live in a side table next to it (``overrides``,
keyed by field name) when its ``Field()`` signatures are frozen by a compatibility gate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from infra2_sdk.ci import validate_manifest_offline
from infra2_sdk.runtime.config_schema import (
    EnvironmentManifest,
    _load_manifest,
    _validate_env,
    environment_manifest_from_model,
)

HOOK_SLOTS = ("smoke", "preview_gate", "staging_gate", "scheduled_audit")
FORBIDDEN_HOOK_ENV_NAMES = frozenset(
    {
        "INFRA2_PAT",
        "DOKPLOY_API_KEY",
        "OP_SERVICE_ACCOUNT_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
    }
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
    }
)
_HOOK_MANIFEST_FIELDS = frozenset({"schema_version", "service", "slots"})
_HOOK_FAILED_STEPS = frozenset({"", "env", "bootstrap", "run", "timeout"})
_HOOK_STDERR_TAIL_LIMIT = 2000


def _validate_hook_env_names(names: tuple[str, ...], *, field_name: str) -> None:
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate variable in {field_name}: {names!r}")
    for name in names:
        if not isinstance(name, str) or not _HOOK_ENV_NAME_RE.match(name):
            raise ValueError(f"{field_name} entry {name!r} must match ^[A-Z][A-Z0-9_]*$")
        if name in FORBIDDEN_HOOK_ENV_NAMES or name.startswith(FORBIDDEN_HOOK_ENV_PREFIXES):
            raise ValueError(f"{field_name} entry {name!r} is a forbidden platform secret")


@dataclass(frozen=True)
class HookSlotSpec:
    """Declarative specification for one verification hook slot."""

    run: str
    bootstrap: str = ""
    timeout_seconds: int = 300
    required_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    artifacts_dir: str = ".artifacts/hooks"

    def __post_init__(self) -> None:
        if not isinstance(self.run, str) or not self.run.strip():
            raise ValueError("hook slot 'run' must be a non-empty command string")
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
        object.__setattr__(self, "required_env", tuple(self.required_env))
        object.__setattr__(self, "optional_env", tuple(self.optional_env))
        _validate_hook_env_names(self.required_env, field_name="required_env")
        _validate_hook_env_names(self.optional_env, field_name="optional_env")
        overlap = sorted(set(self.required_env) & set(self.optional_env))
        if overlap:
            raise ValueError(
                f"variables cannot be in both required_env and optional_env: {overlap}"
            )
        if not isinstance(self.artifacts_dir, str) or not self.artifacts_dir.strip():
            raise ValueError("hook slot 'artifacts_dir' must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run": self.run,
            "bootstrap": self.bootstrap,
            "timeout_seconds": self.timeout_seconds,
            "required_env": list(self.required_env),
            "optional_env": list(self.optional_env),
            "artifacts_dir": self.artifacts_dir,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HookSlotSpec:
        if not isinstance(data, Mapping):
            raise ValueError("hook slot spec must be a JSON object")
        unknown = sorted(set(data) - _HOOK_SLOT_FIELDS)
        if unknown:
            raise ValueError(f"unknown hook slot field(s): {unknown}")
        run = data.get("run")
        if not isinstance(run, str) or not run.strip():
            raise ValueError("hook slot 'run' must be a non-empty string")
        bootstrap = data.get("bootstrap", "")
        if not isinstance(bootstrap, str):
            raise ValueError("hook slot 'bootstrap' must be a string")
        timeout_seconds = data.get("timeout_seconds", 300)
        required_env_raw = data.get("required_env", ())
        optional_env_raw = data.get("optional_env", ())
        if not isinstance(required_env_raw, (list, tuple)) or any(
            not isinstance(item, str) for item in required_env_raw
        ):
            raise ValueError("hook slot 'required_env' must be a list of strings")
        if not isinstance(optional_env_raw, (list, tuple)) or any(
            not isinstance(item, str) for item in optional_env_raw
        ):
            raise ValueError("hook slot 'optional_env' must be a list of strings")
        artifacts_dir = data.get("artifacts_dir", ".artifacts/hooks")
        if not isinstance(artifacts_dir, str) or not artifacts_dir.strip():
            raise ValueError("hook slot 'artifacts_dir' must be a non-empty string")
        return cls(
            run=run,
            bootstrap=bootstrap,
            timeout_seconds=timeout_seconds,
            required_env=tuple(required_env_raw),
            optional_env=tuple(optional_env_raw),
            artifacts_dir=artifacts_dir,
        )


@dataclass(frozen=True)
class RuntimeHooksManifest:
    """Declarative 4-slot runtime verification hook contract (``runtime-hooks.manifest.json``)."""

    service: str
    slots: Mapping[str, HookSlotSpec]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError(
                f"runtime hooks manifest requires schema_version 1, got {self.schema_version!r}"
            )
        if not isinstance(self.service, str) or not self.service.strip():
            raise ValueError("runtime hooks manifest 'service' must be a non-empty string")
        if not isinstance(self.slots, Mapping):
            raise ValueError("runtime hooks manifest 'slots' must be a mapping")
        invalid_slots = sorted(set(self.slots) - set(HOOK_SLOTS))
        if invalid_slots:
            raise ValueError(
                f"unknown hook slot(s) {invalid_slots}; allowed slots are {list(HOOK_SLOTS)}"
            )
        missing_slots = [slot for slot in HOOK_SLOTS if slot not in self.slots]
        if missing_slots:
            raise ValueError(f"runtime hooks manifest is missing required slot(s): {missing_slots}")
        normalized_slots: dict[str, HookSlotSpec] = {}
        for slot in HOOK_SLOTS:
            spec = self.slots[slot]
            if not isinstance(spec, HookSlotSpec):
                raise ValueError(f"slot {slot!r} must be a HookSlotSpec")
            normalized_slots[slot] = spec
        object.__setattr__(self, "slots", normalized_slots)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "service": self.service,
            "slots": {slot: self.slots[slot].to_dict() for slot in HOOK_SLOTS},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RuntimeHooksManifest:
        if not isinstance(data, Mapping):
            raise ValueError("runtime hooks manifest must be a JSON object")
        unknown = sorted(set(data) - _HOOK_MANIFEST_FIELDS)
        if unknown:
            raise ValueError(f"unknown runtime hooks manifest field(s): {unknown}")
        if "schema_version" not in data:
            raise ValueError("runtime hooks manifest requires 'schema_version'")
        raw_slots = data.get("slots")
        if not isinstance(raw_slots, Mapping):
            raise ValueError("runtime hooks manifest 'slots' must be a JSON object")
        invalid_slots = sorted(set(raw_slots) - set(HOOK_SLOTS))
        if invalid_slots:
            raise ValueError(
                f"unknown hook slot(s) {invalid_slots}; allowed slots are {list(HOOK_SLOTS)}"
            )
        missing_slots = [slot for slot in HOOK_SLOTS if slot not in raw_slots]
        if missing_slots:
            raise ValueError(f"runtime hooks manifest is missing required slot(s): {missing_slots}")
        slots = {slot: HookSlotSpec.from_dict(raw_slots[slot]) for slot in HOOK_SLOTS}
        return cls(
            schema_version=data["schema_version"],
            service=data.get("service", ""),
            slots=slots,
        )


def load_hooks_manifest(path: str | Path, *, root: Path | None = None) -> RuntimeHooksManifest:
    """Read and validate a ``runtime-hooks.manifest.json`` file from disk."""
    target = Path(path) if root is None else root / path
    raw = json.loads(target.read_text(encoding="utf-8"))
    return RuntimeHooksManifest.from_dict(raw)


@dataclass(frozen=True)
class HookRunResult:
    """Normalized execution record emitted by a runtime verification hook slot."""

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

    def __post_init__(self) -> None:
        if self.status not in ("pass", "fail"):
            raise ValueError(f"status must be 'pass' or 'fail', got {self.status!r}")
        if self.failed_step not in _HOOK_FAILED_STEPS:
            raise ValueError(
                f"failed_step must be one of {sorted(_HOOK_FAILED_STEPS)}, got {self.failed_step!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "slot": self.slot,
            "environment": self.environment,
            "app_url": self.app_url,
            "version_ref": self.version_ref,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
            "failed_step": self.failed_step,
            "summary": self.summary,
            "stderr_tail": self.stderr_tail,
        }


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
    """Construct a clean-room environment for a hook subprocess without platform secrets."""
    clean: dict[str, str] = {}
    for key in ambient_env:
        if key in _HOOK_PASSTHROUGH_KEYS:
            value = ambient_env[key]
            if value:
                clean[key] = value
    missing: list[str] = []
    for req_key in slot_spec.required_env:
        val = ambient_env.get(req_key, "")
        if not val.strip():
            missing.append(req_key)
        else:
            clean[req_key] = val
    if missing:
        raise ValueError(f"missing required hook env: {', '.join(missing)}")
    for opt_key in slot_spec.optional_env:
        val = ambient_env.get(opt_key, "")
        if val.strip():
            clean[opt_key] = val
    injected = (
        ("APP_URL", app_url),
        ("DEPLOY_BASE_URL", app_url),
        ("STAGING_BASE_URL", app_url),
        ("TA_BASE_URL", app_url),
        ("ENVIRONMENT", environment),
        ("APP_VERSION_REF", version_ref),
        ("EXPECTED_SHA", version_ref),
        ("HOOK_SLOT", slot),
        ("HOOK_ARTIFACTS_DIR", artifacts_dir),
    )
    for env_key, env_val in injected:
        clean[env_key] = env_val
    return clean


def _tail_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    return text.strip()[-_HOOK_STDERR_TAIL_LIMIT:]


def _write_hook_result(result: HookRunResult, result_path: Path | None) -> HookRunResult:
    if result_path is not None:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return result


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
    """Execute one verification hook slot in a clean-room environment."""
    if slot not in manifest.slots:
        raise ValueError(f"unknown hook slot {slot!r}; available: {list(manifest.slots)}")
    if not app_url or not app_url.strip():
        raise ValueError("app_url must be a non-empty string")
    slot_spec = manifest.slots[slot]
    source_env: Mapping[str, str] = os.environ if ambient_env is None else ambient_env
    start = time.monotonic()
    try:
        clean_env = build_hook_env(
            slot_spec,
            slot=slot,
            app_url=app_url.strip(),
            environment=environment,
            version_ref=version_ref,
            artifacts_dir=slot_spec.artifacts_dir,
            ambient_env=source_env,
        )
    except ValueError as err:
        duration = time.monotonic() - start
        return _write_hook_result(
            HookRunResult(
                service=manifest.service,
                slot=slot,
                environment=environment,
                app_url=app_url.strip(),
                version_ref=version_ref,
                status="fail",
                exit_code=2,
                duration_seconds=duration,
                failed_step="env",
                summary=str(err),
                stderr_tail=str(err),
            ),
            result_path,
        )

    steps: list[tuple[str, str]] = []
    if slot_spec.bootstrap.strip():
        steps.append(("bootstrap", slot_spec.bootstrap))
    steps.append(("run", slot_spec.run))

    for step_name, command in steps:
        try:
            proc = runner(
                ["sh", "-lc", command],
                env=clean_env,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=slot_spec.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            summary = f"{step_name} timed out after {slot_spec.timeout_seconds}s"
            stderr_tail = _tail_text(exc.stderr) or _tail_text(exc.stdout) or summary
            return _write_hook_result(
                HookRunResult(
                    service=manifest.service,
                    slot=slot,
                    environment=environment,
                    app_url=app_url.strip(),
                    version_ref=version_ref,
                    status="fail",
                    exit_code=124,
                    duration_seconds=duration,
                    failed_step="timeout",
                    summary=summary,
                    stderr_tail=stderr_tail,
                ),
                result_path,
            )
        if proc.returncode != 0:
            duration = time.monotonic() - start
            summary = f"{step_name} exited with code {proc.returncode}"
            stderr_tail = _tail_text(proc.stderr) or _tail_text(proc.stdout) or summary
            return _write_hook_result(
                HookRunResult(
                    service=manifest.service,
                    slot=slot,
                    environment=environment,
                    app_url=app_url.strip(),
                    version_ref=version_ref,
                    status="fail",
                    exit_code=proc.returncode,
                    duration_seconds=duration,
                    failed_step=step_name,
                    summary=summary,
                    stderr_tail=stderr_tail,
                ),
                result_path,
            )

    duration = time.monotonic() - start
    return _write_hook_result(
        HookRunResult(
            service=manifest.service,
            slot=slot,
            environment=environment,
            app_url=app_url.strip(),
            version_ref=version_ref,
            status="pass",
            exit_code=0,
            duration_seconds=duration,
            failed_step="",
            summary=f"{slot} passed",
            stderr_tail="",
        ),
        result_path,
    )


@dataclass(frozen=True)
class ManifestSpec:
    """One generated manifest: where it lives and which settings model produces it."""

    path: str
    model: str | type  # "package.module:Class" or the class itself
    source: str | None = None
    overrides: str | Mapping[str, Mapping[str, object]] | None = None
    """Side table of supply-chain attributes: ``"package.module:NAME"`` or a mapping."""

    def resolve_model(self) -> type:
        if isinstance(self.model, type):
            return self.model
        module_name, _, attr = self.model.partition(":")
        if not attr:
            raise ValueError(f"model must be 'package.module:Class', got {self.model!r}")
        return getattr(import_module(module_name), attr)

    def resolve_overrides(self) -> Mapping[str, Mapping[str, object]] | None:
        if self.overrides is None or isinstance(self.overrides, Mapping):
            return self.overrides
        module_name, _, attr = self.overrides.partition(":")
        if not attr:
            raise ValueError(f"overrides must be 'package.module:NAME', got {self.overrides!r}")
        table = getattr(import_module(module_name), attr, None)
        if not isinstance(table, Mapping):
            raise ValueError(f"overrides {self.overrides!r} must name an existing mapping")
        return dict(table)

    def build(self) -> EnvironmentManifest:
        return environment_manifest_from_model(
            self.resolve_model(), source=self.source, overrides=self.resolve_overrides()
        )


def _render(manifest: EnvironmentManifest) -> str:
    """The canonical file form: two-space JSON, trailing newline, no extra envelope."""
    return json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n"


def load(path: str | Path, *, root: Path | None = None) -> EnvironmentManifest:
    return _load_manifest(path, root=root)


def all_manifests(
    *, root: Path, specs: Iterable[ManifestSpec], hand_written: Iterable[str] = ()
) -> dict[str, EnvironmentManifest]:
    manifests = {spec.path: spec.build() for spec in specs}
    manifests.update({path: load(path, root=root) for path in hand_written})
    return manifests


def check(
    *, root: Path, specs: Iterable[ManifestSpec], hand_written: Iterable[str] = ()
) -> list[str]:
    """Stale generated files plus offline-gate violations, as ``path: problem`` lines."""
    specs = tuple(specs)
    problems: list[str] = []
    for spec in specs:
        expected = _render(spec.build())
        target = root / spec.path
        if not target.exists() or target.read_text(encoding="utf-8") != expected:
            problems.append(f"{spec.path}: stale, regenerate with --write")
    for path, manifest in all_manifests(root=root, specs=specs, hand_written=hand_written).items():
        problems.extend(f"{path}: {error}" for error in validate_manifest_offline(manifest))
    return problems


def _write(
    *, root: Path, specs: Iterable[ManifestSpec], echo: Callable[[str], None] = print
) -> list[str]:
    written: list[str] = []
    for spec in specs:
        target = root / spec.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_render(spec.build()), encoding="utf-8")
        written.append(spec.path)
        echo(f"wrote {spec.path}")
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
    """The manifest and runtime-hooks CLI driver; returns the exit code."""
    resolved_root = Path.cwd() if root is None else root
    parser = argparse.ArgumentParser(prog=prog, description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate every generated manifest")
    mode.add_argument("--check", action="store_true", help="fresh on disk + offline contract gate")
    mode.add_argument(
        "--validate-env", metavar="MANIFEST", help="the process environment satisfies MANIFEST"
    )
    mode.add_argument(
        "--check-hooks",
        metavar="MANIFEST",
        help="validate a runtime-hooks.manifest.json file offline",
    )
    mode.add_argument(
        "--run-hook",
        choices=HOOK_SLOTS,
        metavar="SLOT",
        help="execute a verification hook slot in a clean-room environment",
    )
    parser.add_argument(
        "--no-require-injected",
        action="store_true",
        help="with --validate-env: tolerate absent deployment-injected values (local runs)",
    )
    parser.add_argument(
        "--hooks-manifest",
        default="runtime-hooks.manifest.json",
        help="with --run-hook: path to runtime-hooks.manifest.json",
    )
    parser.add_argument(
        "--app-url",
        "--base-url",
        dest="app_url",
        default="",
        help="with --run-hook: target application URL (APP_URL / DEPLOY_BASE_URL)",
    )
    parser.add_argument(
        "--env",
        dest="environment",
        default="local_dev",
        help="with --run-hook: target environment tier (default: local_dev)",
    )
    parser.add_argument(
        "--version-ref",
        default="local",
        help="with --run-hook: expected application version ref or SHA (default: local)",
    )
    parser.add_argument(
        "--result-file",
        default="",
        help="with --run-hook: optional file path to write HookRunResult JSON",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    specs = tuple(specs)
    if args.write:
        _write(root=resolved_root, specs=specs, echo=echo)
        return 0
    if args.check:
        problems = check(root=resolved_root, specs=specs, hand_written=hand_written)
        for problem in problems:
            echo(problem)
        echo("env manifests: ok" if not problems else f"env manifests: {len(problems)} problem(s)")
        return 1 if problems else 0
    if args.check_hooks:
        try:
            hooks_manifest = load_hooks_manifest(args.check_hooks, root=resolved_root)
        except (OSError, ValueError) as err:
            echo(f"{args.check_hooks}: {err}")
            return 1
        echo(
            f"runtime hooks manifest: ok ({hooks_manifest.service}, "
            f"{len(hooks_manifest.slots)} slots)"
        )
        return 0
    if args.run_hook:
        if not args.app_url.strip():
            echo("--run-hook requires --app-url")
            return 2
        try:
            hooks_manifest = load_hooks_manifest(args.hooks_manifest, root=resolved_root)
        except (OSError, ValueError) as err:
            echo(f"{args.hooks_manifest}: {err}")
            return 2
        result_path: Path | None = None
        if args.result_file:
            candidate = Path(args.result_file)
            result_path = candidate if candidate.is_absolute() else resolved_root / candidate
        result = run_hook_slot(
            hooks_manifest,
            args.run_hook,
            app_url=args.app_url,
            environment=args.environment,
            version_ref=args.version_ref,
            cwd=resolved_root,
            result_path=result_path,
        )
        echo(json.dumps(result.to_dict(), ensure_ascii=False))
        return 0 if result.status == "pass" else (result.exit_code or 1)
    missing = _validate_env(
        args.validate_env, root=resolved_root, require_injected=not args.no_require_injected
    )
    for name in missing:
        echo(f"missing: {name}")
    echo("environment: ok" if not missing else f"environment: {len(missing)} missing")
    return 0 if not missing else 1


__all__ = [
    "FORBIDDEN_HOOK_ENV_NAMES",
    "FORBIDDEN_HOOK_ENV_PREFIXES",
    "HOOK_SLOTS",
    "HookRunResult",
    "HookSlotSpec",
    "ManifestSpec",
    "RuntimeHooksManifest",
    "all_manifests",
    "build_hook_env",
    "check",
    "load",
    "load_hooks_manifest",
    "main",
    "run_hook_slot",
]

if __name__ == "__main__":  # pragma: no cover - a repository wraps main() with its specs
    sys.exit(main(root=Path.cwd(), specs=()))
