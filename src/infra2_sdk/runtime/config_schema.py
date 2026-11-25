"""Environment-provided application settings contracts (manifest contract v2)."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, get_args

from infra2_sdk._wire import (
    canonical_sha256 as _canonical_sha256,
)
from infra2_sdk._wire import (
    parse_bool,
    parse_contract_version,
    parse_string,
    require_contract_version,
)
from infra2_sdk.runtime.environment import EnvironmentConflictError

_ENVIRONMENT_MANIFEST_VERSION = 2


class _FieldSource(StrEnum):
    BOOTSTRAP = "bootstrap"
    HUMAN = "human"
    RUNTIME = "runtime"
    RELEASE = "release"
    DECISION = "decision"
    CODE = "code"


_STORE_BACKED_SOURCES = frozenset({_FieldSource.HUMAN, _FieldSource.RUNTIME})
_COMPOSE_SOURCES = frozenset({_FieldSource.RELEASE, _FieldSource.DECISION})
_FIELD_SCOPES = ("env", "project")
_CI_POLICIES = ("forbidden", "allowed")
_PROVIDED_BY_RE = re.compile(r"\A[a-z][a-z0-9_]*/[a-z][a-z0-9_-]*:[A-Za-z_][A-Za-z0-9_]*\Z")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_PLACEHOLDER_RE = re.compile(r"\{env:([A-Z][A-Z0-9_]*)\}")


@dataclass(frozen=True)
class EnvironmentField:
    """One environment variable exposed by an application settings model."""

    field: str
    env: str
    aliases: tuple[str, ...] = ()
    required: bool = False
    injected: bool = False
    has_default: bool = True
    sensitive: bool = False
    group: str = "Application"
    description: str = ""
    source: str = _FieldSource.CODE
    empty_ok: bool = False
    scope: str = "env"
    provided_by: str = ""
    composed_from: str = ""
    mirror_to_1password: bool = False
    ci: str = "forbidden"
    store_key: str = ""

    def __post_init__(self) -> None:
        if not self.field or not self.env:
            raise ValueError("field and env must be non-empty")
        names = (self.env, *self.aliases)
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate environment alias for {self.field}")
        if self.source not in set(_FieldSource):
            raise ValueError(f"{self.field}: unknown source {self.source!r}")
        object.__setattr__(self, "source", str(_FieldSource(self.source)))
        if self.scope not in _FIELD_SCOPES:
            raise ValueError(f"{self.field}: scope must be one of {_FIELD_SCOPES}")
        if self.ci not in _CI_POLICIES:
            raise ValueError(f"{self.field}: ci must be one of {_CI_POLICIES}")
        if self.provided_by and not _PROVIDED_BY_RE.match(self.provided_by):
            raise ValueError(f"{self.field}: provided_by must look like project/service:KEY")
        if self.mirror_to_1password and self.source != _FieldSource.RUNTIME:
            raise ValueError(f"{self.field}: only runtime fields can mirror to 1Password")
        if self.required and self.empty_ok:
            raise ValueError(f"{self.field}: a required field cannot be empty_ok")
        if self.store_key and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.store_key):
            raise ValueError(f"{self.field}: store_key must be an identifier")
        if self.composed_from and not (
            _PLACEHOLDER_RE.search(self.composed_from)
            or _ENV_PLACEHOLDER_RE.search(self.composed_from)
        ):
            raise ValueError(
                f"{self.field}: composed_from needs a {{KEY}} or {{env:NAME}} placeholder"
            )

    @property
    def key(self) -> str:
        return self.store_key or self.env

    @property
    def store_backed(self) -> bool:
        return self.source in _STORE_BACKED_SOURCES and not self.provided_by

    @property
    def composed_keys(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_PLACEHOLDER_RE.findall(self.composed_from)))

    @property
    def composed_env(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_ENV_PLACEHOLDER_RE.findall(self.composed_from)))

    @property
    def rendered(self) -> bool:
        return self.store_backed or bool(self.provided_by) or bool(self.composed_from)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["aliases"] = list(self.aliases)
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> EnvironmentField:
        aliases = raw.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(v, str) for v in aliases):
            raise ValueError("aliases must be an array of strings")
        return cls(
            field=parse_string(raw, "field"),
            env=parse_string(raw, "env"),
            aliases=tuple(aliases),
            required=parse_bool(raw, "required"),
            injected=parse_bool(raw, "injected"),
            has_default=parse_bool(raw, "has_default", default=True),
            sensitive=parse_bool(raw, "sensitive"),
            group=parse_string(raw, "group", required=False) or "Application",
            description=parse_string(raw, "description", required=False),
            source=parse_string(raw, "source", required=False) or _FieldSource.CODE,
            empty_ok=parse_bool(raw, "empty_ok"),
            scope=parse_string(raw, "scope", required=False) or "env",
            provided_by=parse_string(raw, "provided_by", required=False),
            composed_from=parse_string(raw, "composed_from", required=False),
            mirror_to_1password=parse_bool(raw, "mirror_to_1password"),
            ci=parse_string(raw, "ci", required=False) or "forbidden",
            store_key=parse_string(raw, "store_key", required=False),
        )


@dataclass(frozen=True)
class EnvironmentManifest:
    """Versioned application configuration contract."""

    source: str
    fields: tuple[EnvironmentField, ...]
    contract_version: int = _ENVIRONMENT_MANIFEST_VERSION

    def __post_init__(self) -> None:
        require_contract_version(
            self.contract_version,
            _ENVIRONMENT_MANIFEST_VERSION,
            description="environment manifest version",
        )
        if not self.source:
            raise ValueError("source is required")
        owners: dict[str, str] = {}
        for fld in self.fields:
            for env_name in (fld.env, *fld.aliases):
                if (owner := owners.get(env_name)) is not None:
                    raise ValueError(
                        f"environment variable {env_name!r} is shared by "
                        f"{owner!r} and {fld.field!r}"
                    )
                owners[env_name] = fld.field

    def by_source(self, *sources: _FieldSource | str) -> tuple[EnvironmentField, ...]:
        wanted = {str(_FieldSource(s)) for s in sources}
        return tuple(f for f in self.fields if f.source in wanted)

    @property
    def store_backed(self) -> tuple[EnvironmentField, ...]:
        return tuple(f for f in self.fields if f.store_backed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "source": self.source,
            "fields": [f.to_dict() for f in self.fields],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> EnvironmentManifest:
        version = parse_contract_version(
            raw, _ENVIRONMENT_MANIFEST_VERSION, description="environment manifest version"
        )
        raw_fields = raw.get("fields")
        if not isinstance(raw_fields, list) or any(not isinstance(v, Mapping) for v in raw_fields):
            raise ValueError("fields must be an array of objects")
        return cls(
            contract_version=version,
            source=parse_string(raw, "source"),
            fields=tuple(EnvironmentField.from_dict(v) for v in raw_fields),
        )

    def validate_offline(self) -> list[str]:
        """Return contract violations in this manifest without external infrastructure."""
        errors: list[str] = []
        declared = {f.env for f in self.fields}
        for f in self.fields:
            settled = f.empty_ok or f.required or f.injected
            if f.store_backed and f.has_default and not settled:
                errors.append(f"{f.env}: {f.source} value must be required, injected or empty_ok")
            if f.source in _COMPOSE_SOURCES and not f.injected:
                errors.append(f"{f.env}: {f.source} value must be injected by the deployment")
            if f.sensitive and f.source == _FieldSource.CODE and not f.provided_by:
                errors.append(f"{f.env}: a sensitive value cannot be a code default")
            if f.source == _FieldSource.BOOTSTRAP:
                errors.append(f"{f.env}: bootstrap values never enter an application manifest")
            provider_key = f.provided_by.split(":", 1)[1] if f.provided_by else ""
            # A store-backed field may compose from its own store key: the store holds the
            # secret part and only the composed value enters the process (infra2#1823 N2).
            # Only a store-backed, non-empty_ok field gets that key written by the resolver.
            own_key = f.store_key if f.store_key and f.store_backed and not f.empty_ok else ""
            for key in f.composed_keys:
                if key not in declared and key not in (provider_key, own_key):
                    errors.append(f"{f.env}: composed_from references undeclared {key}")
        return errors


@dataclass(frozen=True)
class _EnvironmentValidation:
    missing: tuple[str, ...]
    resolved: Mapping[str, str]

    @property
    def valid(self) -> bool:
        return not self.missing


def environment_manifest_from_model(
    model: type,
    *,
    source: str | None = None,
    overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> EnvironmentManifest:
    """Extract an ``EnvironmentManifest`` from a Pydantic v2 settings model."""
    model_fields = getattr(model, "model_fields", None)
    if not isinstance(model_fields, Mapping):
        raise TypeError("model must expose Pydantic v2 model_fields")
    cfg = getattr(model, "model_config", {})
    prefix = cfg.get("env_prefix", "") if isinstance(cfg, Mapping) else ""
    prefix = prefix if isinstance(prefix, str) else ""
    fields: list[EnvironmentField] = []
    for name, info in model_fields.items():
        val_aliases = list(_validation_aliases(getattr(info, "validation_alias", None)))
        canonical = (
            val_aliases[0]
            if val_aliases
            else f"{prefix}{getattr(info, 'alias', None) or name}".upper()
        )
        aliases = val_aliases[1:]
        extra = getattr(info, "json_schema_extra", None)
        extra = extra if isinstance(extra, Mapping) else {}
        f_source = str(extra.get("source") or _FieldSource.CODE)
        is_req = getattr(info, "is_required", None)
        required = bool(is_req()) if callable(is_req) else False
        fields.append(
            EnvironmentField(
                field=str(name),
                env=str(canonical),
                aliases=tuple(a for a in aliases if a != canonical),
                required=required,
                injected=bool(extra.get("injected") or f_source in _COMPOSE_SOURCES),
                has_default=not required,
                sensitive=bool(extra.get("sensitive"))
                or _is_secret(getattr(info, "annotation", None)),
                group=str(extra.get("group", "Application")),
                description=str(getattr(info, "description", None) or ""),
                source=f_source,
                empty_ok=bool(extra.get("empty_ok", False)),
                scope=str(extra.get("scope") or "env"),
                provided_by=str(extra.get("provided_by") or ""),
                composed_from=str(extra.get("composed_from") or ""),
                mirror_to_1password=bool(extra.get("mirror_to_1password", False)),
                ci=str(extra.get("ci") or "forbidden"),
                store_key=str(extra.get("store_key") or ""),
            )
        )
    if overrides:
        by_name = {f.field: idx for idx, f in enumerate(fields)}
        if unknown := sorted(set(overrides) - set(by_name)):
            raise ValueError(f"overrides name unknown settings fields: {unknown}")
        for name, override in overrides.items():
            changes = dict(override)
            if str(changes.get("source", "")) in _COMPOSE_SOURCES:
                changes.setdefault("injected", True)
            fields[by_name[name]] = replace(fields[by_name[name]], **changes)
    return EnvironmentManifest(
        source=source or f"{model.__module__}.{model.__qualname__}", fields=tuple(fields)
    )


def validate_environment(
    manifest: EnvironmentManifest, environ: Mapping[str, str], *, require_injected: bool = False
) -> _EnvironmentValidation:
    missing: list[str] = []
    resolved: dict[str, str] = {}
    for f in manifest.fields:
        present = [
            (cand, val if f.sensitive else val.strip())
            for cand in (f.env, *f.aliases)
            if isinstance(val := environ.get(cand), str) and val.strip()
        ]
        if len({v for _, v in present}) > 1:
            raise EnvironmentConflictError(
                f"conflicting environment variables for {f.env}: {', '.join(n for n, _ in present)}"
            )
        if present:
            resolved[f.env] = present[0][0]
        elif f.required or (require_injected and f.injected):
            missing.append(f.env)
    return _EnvironmentValidation(tuple(sorted(missing)), resolved)


def _load_manifest(path: str | Path, *, root: Path | None = None) -> EnvironmentManifest:
    target = Path(path) if root is None else root / path
    return EnvironmentManifest.from_dict(json.loads(target.read_text(encoding="utf-8")))


def _validate_env(
    manifest: EnvironmentManifest | str | Path,
    environ: Mapping[str, str] | None = None,
    *,
    root: Path | None = None,
    require_injected: bool = True,
) -> list[str]:
    loaded = (
        manifest
        if isinstance(manifest, EnvironmentManifest)
        else _load_manifest(manifest, root=root)
    )
    env = os.environ if environ is None else environ
    return list(validate_environment(loaded, env, require_injected=require_injected).missing)


def assert_environment(
    manifest_path: str | Path, *, environ: Mapping[str, str] | None = None, require_injected: bool
) -> None:
    path = Path(manifest_path)
    if not path.is_file():
        raise RuntimeError(
            f"environment manifest {path} is not in the image; boot validation cannot run (#759)"
        )
    missing = _validate_env(path, environ, require_injected=require_injected)
    if missing:
        raise RuntimeError(
            f"environment lacks {len(missing)} value(s) {path} declares: {', '.join(missing)} "
            "-- names only; the deployment must inject them (#759)"
        )


def manifest_config_fingerprint(manifest: EnvironmentManifest, values: Mapping[str, str]) -> str:
    lines = [
        f"{f.env}={hashlib.sha256(values[f.env].encode()).hexdigest()}"
        for f in sorted(manifest.fields, key=lambda item: item.env)
        if f.env in values
    ]
    return _canonical_sha256(lines)


# Keys that decide how a variable is produced and rendered. Two manifests that declare one
# variable must agree on them (infra2 registry.merged_manifest, #1320).
_RENDER_KEYS = ("source", "provided_by", "composed_from", "store_key", "empty_ok", "scope")


def merge_manifests(
    manifests: Sequence[tuple[str, EnvironmentManifest]],
    *,
    source: str,
    exclude: Collection[str] = (),
) -> EnvironmentManifest:
    """Union of several manifests of one stack, in order.

    ``manifests`` holds ``(label, manifest)`` pairs; the label names the manifest in an
    error. Two manifests may declare the same variable (both truealpha apps read
    ``DATABASE_URL``): they must agree on how it is produced and rendered (``source``,
    ``provided_by``, ``composed_from``, ``store_key``, ``empty_ok``, ``scope``), else
    ``ValueError``. The first declaration wins for the rest (requiredness, description).
    Variables in ``exclude`` are dropped.
    """
    excluded = set(exclude)
    fields: dict[str, EnvironmentField] = {}
    for label, manifest in manifests:
        for entry in manifest.fields:
            if entry.env in excluded:
                continue
            existing = fields.get(entry.env)
            if existing is None:
                fields[entry.env] = entry
                continue
            for key in _RENDER_KEYS:
                if getattr(existing, key) != getattr(entry, key):
                    raise ValueError(f"{source}: {entry.env}.{key} declared differently in {label}")
    return EnvironmentManifest(source=source, fields=tuple(fields.values()))


def render_env_markdown(manifest: EnvironmentManifest, *, title: str = "") -> str:
    """Render one Markdown reference of the manifest: one table for each group.

    The output is deterministic: groups and rows keep the field order of the settings
    model. A sensitive field shows no default and no example value.
    """
    groups: dict[str, list[EnvironmentField]] = {}
    for field in manifest.fields:
        groups.setdefault(field.group, []).append(field)
    lines = [f"# {title or 'Environment reference'}", "", f"Source: `{manifest.source}`", ""]
    for group, fields in groups.items():
        lines += [f"## {group}", "", "| Variable | Required | Sensitive | Source | Description |"]
        lines.append("|---|---|---|---|---|")
        for field in fields:
            name = f"`{field.env}`"
            if field.aliases:
                name += " (aliases: " + ", ".join(f"`{a}`" for a in field.aliases) + ")"
            description = " ".join(field.description.split()).replace("|", "\\|")
            lines.append(
                f"| {name} | {'yes' if field.required else 'no'} | "
                f"{'yes' if field.sensitive else 'no'} | {field.source} | {description} |"
            )
        lines.append("")
    return "\n".join(lines)


def _load_settings_class(target: str) -> type:
    import importlib

    module_name, _, attr = target.partition(":")
    if not module_name or not attr:
        raise ValueError(f"settings target must be 'module:Class', not {target!r}")
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    if not isinstance(obj, type):
        raise ValueError(f"{target} is not a class")
    return obj


def _main(argv: list[str] | None = None) -> int:
    """``python -m infra2_sdk.runtime.config_schema MODULE:CLASS [--json P] [--markdown P]``.

    It writes the manifest JSON and the Markdown reference of a Pydantic settings class.
    With ``--check`` it writes nothing and exits 1 when a file differs from the output.
    """
    import argparse
    import difflib
    import sys

    parser = argparse.ArgumentParser(prog="python -m infra2_sdk.runtime.config_schema")
    parser.add_argument("settings", help="settings class as module:Class")
    parser.add_argument("--source", default=None, help="manifest source label")
    parser.add_argument("--json", type=Path, default=None, help="manifest JSON path")
    parser.add_argument("--markdown", type=Path, default=None, help="Markdown reference path")
    parser.add_argument("--title", default="", help="Markdown title")
    parser.add_argument("--check", action="store_true", help="compare, do not write")
    args = parser.parse_args(argv)
    if args.json is None and args.markdown is None:
        parser.error("name at least one of --json and --markdown")
    manifest = environment_manifest_from_model(
        _load_settings_class(args.settings), source=args.source
    )
    outputs: list[tuple[Path, str]] = []
    if args.json is not None:
        text = json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n"
        outputs.append((args.json, text))
    if args.markdown is not None:
        outputs.append((args.markdown, render_env_markdown(manifest, title=args.title)))
    drift = 0
    for path, text in outputs:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current == text:
            continue
        if args.check:
            drift += 1
            sys.stdout.writelines(
                difflib.unified_diff(
                    current.splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    fromfile=str(path),
                    tofile=f"{path} (generated)",
                )
            )
        else:
            path.write_text(text, encoding="utf-8")
    if drift:
        print(f"{drift} generated file(s) are stale; run without --check to update")
    return 1 if drift else 0


def _validation_aliases(alias: Any) -> tuple[str, ...]:
    if isinstance(alias, str):
        return (alias,)
    return tuple(str(c) for c in getattr(alias, "choices", ()) if isinstance(c, str))


def _is_secret(annotation: Any) -> bool:
    if annotation is None:
        return False
    name = getattr(annotation, "__name__", "")
    if isinstance(name, str) and "secret" in name.lower():
        return True
    return any(_is_secret(arg) for arg in get_args(annotation))


__all__ = [
    "EnvironmentField",
    "EnvironmentManifest",
    "assert_environment",
    "environment_manifest_from_model",
    "manifest_config_fingerprint",
    "merge_manifests",
    "render_env_markdown",
]


if __name__ == "__main__":
    raise SystemExit(_main())
