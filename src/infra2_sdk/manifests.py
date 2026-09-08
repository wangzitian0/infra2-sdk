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
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

from infra2_sdk.ci import validate_manifest_offline
from infra2_sdk.runtime.config_schema import (
    EnvironmentManifest,
    environment_manifest_from_model,
    validate_environment,
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
        return dict(table) if isinstance(table, Mapping) else None

    def build(self) -> EnvironmentManifest:
        return environment_manifest_from_model(
            self.resolve_model(), source=self.source, overrides=self.resolve_overrides()
        )


def render(manifest: EnvironmentManifest) -> str:
    """The canonical file form: two-space JSON, trailing newline, no extra envelope."""
    return json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n"


def load(path: str | Path, *, root: Path | None = None) -> EnvironmentManifest:
    target = Path(path) if root is None else root / path
    return EnvironmentManifest.from_dict(json.loads(target.read_text(encoding="utf-8")))


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
        expected = render(spec.build())
        target = root / spec.path
        if not target.exists() or target.read_text(encoding="utf-8") != expected:
            problems.append(f"{spec.path}: stale, regenerate with --write")
    for path, manifest in all_manifests(root=root, specs=specs, hand_written=hand_written).items():
        problems.extend(f"{path}: {error}" for error in validate_manifest_offline(manifest))
    return problems


def write(
    *, root: Path, specs: Iterable[ManifestSpec], echo: Callable[[str], None] = print
) -> list[str]:
    written: list[str] = []
    for spec in specs:
        target = root / spec.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(spec.build()), encoding="utf-8")
        written.append(spec.path)
        echo(f"wrote {spec.path}")
    return written


def validate_env(
    manifest: EnvironmentManifest | str | Path,
    environ: Mapping[str, str] | None = None,
    *,
    root: Path | None = None,
    require_injected: bool = True,
) -> list[str]:
    """Names the environment lacks (never values); empty when the process may boot."""
    if not isinstance(manifest, EnvironmentManifest):
        manifest = load(manifest, root=root)
    result = validate_environment(
        manifest, os.environ if environ is None else environ, require_injected=require_injected
    )
    return list(result.missing)


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path,
    specs: Iterable[ManifestSpec],
    hand_written: Iterable[str] = (),
    prog: str = "env_manifest",
    echo: Callable[[str], None] = print,
) -> int:
    """The ``--write`` / ``--check`` / ``--validate-env MANIFEST`` driver; returns the exit code."""
    parser = argparse.ArgumentParser(prog=prog, description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate every generated manifest")
    mode.add_argument("--check", action="store_true", help="fresh on disk + offline contract gate")
    mode.add_argument(
        "--validate-env", metavar="MANIFEST", help="the process environment satisfies MANIFEST"
    )
    parser.add_argument(
        "--no-require-injected",
        action="store_true",
        help="with --validate-env: tolerate absent deployment-injected values (local runs)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    specs = tuple(specs)
    if args.write:
        write(root=root, specs=specs, echo=echo)
        return 0
    if args.check:
        problems = check(root=root, specs=specs, hand_written=hand_written)
        for problem in problems:
            echo(problem)
        echo("env manifests: ok" if not problems else f"env manifests: {len(problems)} problem(s)")
        return 1 if problems else 0
    missing = validate_env(
        args.validate_env, root=root, require_injected=not args.no_require_injected
    )
    for name in missing:
        echo(f"missing: {name}")
    echo("environment: ok" if not missing else f"environment: {len(missing)} missing")
    return 0 if not missing else 1


__all__ = [
    "ManifestSpec",
    "all_manifests",
    "check",
    "load",
    "main",
    "render",
    "validate_env",
    "write",
]

if __name__ == "__main__":  # pragma: no cover - a repository wraps main() with its specs
    sys.exit(main(root=Path.cwd(), specs=()))
