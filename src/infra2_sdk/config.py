"""Environment tiers, standard runtime keys, configuration manifests, and startup checks."""

from __future__ import annotations

import argparse
import difflib
import functools
import hashlib
import importlib
import json
import os
import re
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from enum import StrEnum
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Any, Final, get_args

import yaml
from pydantic import AliasChoices, BaseModel, SecretStr, field_validator, model_validator

from infra2_sdk._core import ContractModel, canonical_sha256, require_contract_version

MANIFEST_CONTRACT_VERSION: Final[int] = 2


class EnvironmentTier(StrEnum):
    LOCAL_DEV = "local_dev"
    LOCAL_TEST = "local_test"
    GITHUB_CI = "github_ci"
    PREVIEW = "preview"
    STAGING = "staging"
    PRODUCTION = "production"


PLATFORM_OWNED_TIERS: Final[frozenset[EnvironmentTier]] = frozenset(
    {EnvironmentTier.PREVIEW, EnvironmentTier.STAGING, EnvironmentTier.PRODUCTION}
)


class RuntimeEnvKey(StrEnum):
    ENVIRONMENT = "ENVIRONMENT"
    SERVICE_NAME = "OTEL_SERVICE_NAME"
    SERVICE_VERSION = "SERVICE_VERSION"
    GIT_COMMIT_SHA = "GIT_COMMIT_SHA"
    IMAGE_DIGEST = "IMAGE_DIGEST"
    CONFIGURATION_SHA256 = "CONFIGURATION_SHA256"
    RELEASE_ID = "RELEASE_ID"
    OTEL_EXPORTER_OTLP_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
    OTEL_RESOURCE_ATTRIBUTES = "OTEL_RESOURCE_ATTRIBUTES"


class FieldSource(StrEnum):
    BOOTSTRAP = "bootstrap"
    HUMAN = "human"
    RUNTIME = "runtime"
    RELEASE = "release"
    DECISION = "decision"
    CODE = "code"


class EnvironmentConflictError(ValueError):
    """Raised when conflicting values exist across a field's env names."""


_STORE_BACKED_SOURCES = frozenset({FieldSource.HUMAN, FieldSource.RUNTIME})
_COMPOSE_SOURCES = frozenset({FieldSource.RELEASE, FieldSource.DECISION})
_FIELD_SCOPES = frozenset({"env", "project"})
_CI_POLICIES = frozenset({"forbidden", "allowed"})
_PROVIDED_BY_RE = re.compile(r"\A[a-z][a-z0-9_]*/[a-z][a-z0-9_-]*:[A-Za-z_][A-Za-z0-9_]*\Z")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_PLACEHOLDER_RE = re.compile(r"\{env:([A-Z][A-Z0-9_]*)\}")
_IDENT_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
_RENDER_KEYS = ("source", "provided_by", "composed_from", "store_key", "empty_ok", "scope")

_CANARY_SLOT = "canary-preview"
_LOCAL_DEV_VALUES = frozenset({"dev", "development", "local", "local_dev"})
_LOCAL_TEST_VALUES = frozenset({"test", "testing", "ci", "local_ci", "local_test"})
_PRODUCTION_VALUES = frozenset({"prod", "production"})
_PREVIEW_RE = re.compile(
    r"\A(?:branch-[a-z0-9][a-z0-9_-]*|pr-[1-9][0-9]*|commit-[0-9a-f]{7,40}|tag-v[0-9]+-[0-9]+-[0-9]+)\Z"
)
_UNDERSCORE_PREVIEW_RE = re.compile(
    r"\A(?P<prefix>branch|pr|commit|tag)_(?P<rest>[a-z0-9][a-z0-9._-]*)\Z"
)


class RuntimeEnvironment(ContractModel):
    name: str
    tier: EnvironmentTier


def _canonicalize_preview_name(cleaned: str) -> str:
    if cleaned == "canary_preview":
        return _CANARY_SLOT
    match = _UNDERSCORE_PREVIEW_RE.match(cleaned)
    if match is not None:
        rest = match["rest"].replace("_", "-") if match["prefix"] == "tag" else match["rest"]
        return f"{match['prefix']}-{rest}"
    return cleaned


def is_preview_alias(value: str) -> bool:
    """Return True when ``value`` names a preview slot pattern."""
    alias = _canonicalize_preview_name(value.strip().lower())
    return alias == _CANARY_SLOT or bool(_PREVIEW_RE.match(alias))


def resolve_tier(value: str | EnvironmentTier, *, github_actions: bool = False) -> EnvironmentTier:
    """Resolve a raw environment string or tier enum into a canonical EnvironmentTier."""
    if isinstance(value, EnvironmentTier):
        return value
    if not isinstance(value, str):
        raise TypeError("environment must be a string or EnvironmentTier")
    cleaned = value.strip().lower()
    if cleaned in ("pr", "canary") or cleaned.startswith("preview/"):
        return EnvironmentTier.PREVIEW
    normalized = cleaned.replace("-", "_")
    if normalized in _LOCAL_DEV_VALUES:
        return EnvironmentTier.LOCAL_DEV
    if normalized in _LOCAL_TEST_VALUES:
        return EnvironmentTier.GITHUB_CI if github_actions else EnvironmentTier.LOCAL_TEST
    if normalized == EnvironmentTier.GITHUB_CI.value:
        return EnvironmentTier.GITHUB_CI
    alias = _canonicalize_preview_name(cleaned)
    if (
        normalized == EnvironmentTier.PREVIEW.value
        or alias == _CANARY_SLOT
        or _PREVIEW_RE.match(alias)
    ):
        return EnvironmentTier.PREVIEW
    if normalized == EnvironmentTier.STAGING.value:
        return EnvironmentTier.STAGING
    if normalized in _PRODUCTION_VALUES:
        return EnvironmentTier.PRODUCTION
    raise ValueError(f"unknown environment: {value!r}")


def read_runtime_env(
    key: RuntimeEnvKey,
    environ: Mapping[str, str] | None = None,
    *,
    default: str = "",
    required: bool = False,
) -> str:
    """Read one canonical runtime variable from ``environ``."""
    canonical = RuntimeEnvKey(key)
    values = os.environ if environ is None else environ
    raw = values.get(canonical.value)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if required:
        raise ValueError(f"{canonical.value} is required")
    return default


def resolve_environment(
    environ: Mapping[str, str] | None = None, *, required: bool = False
) -> RuntimeEnvironment:
    """Resolve the active RuntimeEnvironment from ``ENVIRONMENT``."""
    values = os.environ if environ is None else environ
    raw = read_runtime_env(
        RuntimeEnvKey.ENVIRONMENT,
        values,
        default="" if required else EnvironmentTier.LOCAL_DEV.value,
        required=required,
    )
    github_actions = str(values.get("GITHUB_ACTIONS", "")).strip().lower() == "true"
    tier = resolve_tier(raw, github_actions=github_actions)
    canonical_name = _canonicalize_preview_name(raw.strip().lower())
    name = (
        canonical_name
        if tier is EnvironmentTier.PREVIEW and canonical_name != "preview"
        else tier.value
    )
    return RuntimeEnvironment(name=name, tier=tier)


@functools.cache
def _cached_shared_env_contract() -> dict[str, Any]:
    source = _resource_files("infra2_sdk").joinpath("data/shared_env.yaml")
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
        raise ValueError("shared_env.yaml: 'groups' must be a mapping")
    return data


def shared_env_contract() -> Mapping[str, Any]:
    """Return the cross-repository shared environment variable table."""
    return _cached_shared_env_contract()


def check_shared_env(names: Iterable[str]) -> list[str]:
    """Validate declared shared env names against ``shared_env.yaml``."""
    contract = shared_env_contract()
    all_known: dict[str, bool] = {}
    for entries in contract["groups"].values():
        for entry in entries:
            all_known[str(entry["name"])] = bool(entry.get("sensitive", False))
    issues: list[str] = []
    for raw in sorted({str(item).strip() for item in names if str(item).strip()}):
        if raw.lower() in {"env", "app_env", "image_tag"}:
            issues.append(f"{raw} is not a canonical shared environment variable")
    return issues


class EnvironmentField(ContractModel):
    """One environment variable declared by an application settings model."""

    field: str
    env: str
    aliases: tuple[str, ...] = ()
    required: bool = False
    injected: bool = False
    has_default: bool = True
    sensitive: bool = False
    group: str = "Application"
    description: str = ""
    source: FieldSource = FieldSource.CODE
    empty_ok: bool = False
    scope: str = "env"
    provided_by: str = ""
    composed_from: str = ""
    store_key: str = ""
    ci: str = "forbidden"

    @model_validator(mode="after")
    def _validate_field(self) -> EnvironmentField:
        if not self.field or not self.env:
            raise ValueError("field and env must be non-empty")
        names = (self.env, *self.aliases)
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate environment alias for {self.field}")
        if self.scope not in _FIELD_SCOPES:
            raise ValueError(f"{self.field}: scope must be one of {sorted(_FIELD_SCOPES)}")
        if self.ci not in _CI_POLICIES:
            raise ValueError(f"{self.field}: ci must be one of {sorted(_CI_POLICIES)}")
        if self.provided_by and not _PROVIDED_BY_RE.match(self.provided_by):
            raise ValueError(f"{self.field}: provided_by must look like project/service:KEY")
        if self.required and self.empty_ok:
            raise ValueError(f"{self.field}: a required field cannot be empty_ok")
        if self.store_key and not _IDENT_RE.fullmatch(self.store_key):
            raise ValueError(f"{self.field}: store_key must be an identifier")
        if self.composed_from and not (
            _PLACEHOLDER_RE.search(self.composed_from)
            or _ENV_PLACEHOLDER_RE.search(self.composed_from)
        ):
            raise ValueError(
                f"{self.field}: composed_from needs a {{KEY}} or {{env:NAME}} placeholder"
            )
        return self

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


def _validation_aliases(alias: Any) -> tuple[str, ...]:
    if isinstance(alias, str):
        return (alias,)
    if isinstance(alias, AliasChoices):
        return tuple(str(c) for c in alias.choices if isinstance(c, str))
    return tuple(str(c) for c in getattr(alias, "choices", ()) if isinstance(c, str))


def _is_secret_annotation(annotation: Any) -> bool:
    if annotation is None:
        return False
    if annotation is SecretStr or (
        isinstance(annotation, type) and issubclass(annotation, SecretStr)
    ):
        return True
    name = getattr(annotation, "__name__", "")
    if isinstance(name, str) and "secret" in name.lower():
        return True
    return any(_is_secret_annotation(arg) for arg in get_args(annotation))


class EnvironmentManifest(ContractModel):
    """Versioned application configuration contract."""

    source: str
    fields: tuple[EnvironmentField, ...]
    contract_version: int = MANIFEST_CONTRACT_VERSION

    @field_validator("contract_version", mode="before")
    @classmethod
    def _check_version(cls, value: Any) -> int:
        return require_contract_version(
            value, MANIFEST_CONTRACT_VERSION, label="environment manifest version"
        )

    @model_validator(mode="after")
    def _validate_manifest(self) -> EnvironmentManifest:
        if not self.source:
            raise ValueError("source is required")
        owners: dict[str, str] = {}
        for fld in self.fields:
            for env_name in (fld.env, *fld.aliases):
                if (owner := owners.get(env_name)) is not None:
                    raise ValueError(
                        f"environment variable {env_name!r} is shared "
                        f"by {owner!r} and {fld.field!r}"
                    )
                owners[env_name] = fld.field
        return self

    def by_source(self, *sources: FieldSource | str) -> tuple[EnvironmentField, ...]:
        wanted = {FieldSource(s) for s in sources}
        return tuple(f for f in self.fields if f.source in wanted)

    def store_backed(self) -> tuple[EnvironmentField, ...]:
        return tuple(f for f in self.fields if f.store_backed)

    def validate_offline(self) -> list[str]:
        """Return contract violations in this manifest without external infrastructure."""
        errors: list[str] = []
        declared = {f.env for f in self.fields}
        for f in self.fields:
            settled = f.empty_ok or f.required or f.injected
            if f.store_backed and f.has_default and not settled:
                errors.append(
                    f"{f.env}: {f.source.value} value must be required, injected or empty_ok"
                )
            if f.source in _COMPOSE_SOURCES and not f.injected:
                errors.append(f"{f.env}: {f.source.value} value must be injected by the deployment")
            if f.sensitive and f.source is FieldSource.CODE and not f.provided_by:
                errors.append(f"{f.env}: a sensitive value cannot be a code default")
            if f.source is FieldSource.BOOTSTRAP:
                errors.append(f"{f.env}: bootstrap values never enter an application manifest")
            provider_key = f.provided_by.split(":", 1)[1] if f.provided_by else ""
            own_key = f.store_key if f.store_key and f.store_backed and not f.empty_ok else ""
            for key in f.composed_keys:
                if key not in declared and key not in (provider_key, own_key):
                    errors.append(f"{f.env}: composed_from references undeclared {key}")
        return errors

    def fingerprint(self, environ: Mapping[str, str]) -> str:
        lines = [
            f"{f.env}={hashlib.sha256(environ[f.env].encode()).hexdigest()}"
            for f in sorted(self.fields, key=lambda item: item.env)
            if f.env in environ
        ]
        return canonical_sha256(lines)

    def render_markdown(self, *, title: str = "") -> str:
        groups: dict[str, list[EnvironmentField]] = {}
        for fld in self.fields:
            groups.setdefault(fld.group, []).append(fld)
        lines = [f"# {title or 'Environment reference'}", "", f"Source: `{self.source}`", ""]
        for group, flds in groups.items():
            lines += [
                f"## {group}",
                "",
                "| Variable | Required | Sensitive | Source | Description |",
            ]
            lines.append("|---|---|---|---|---|")
            for fld in flds:
                name = f"`{fld.env}`"
                if fld.aliases:
                    name += " (aliases: " + ", ".join(f"`{a}`" for a in fld.aliases) + ")"
                desc = " ".join(fld.description.split()).replace("|", "\\|")
                lines.append(
                    f"| {name} | {'yes' if fld.required else 'no'} | "
                    f"{'yes' if fld.sensitive else 'no'} | {fld.source.value} | {desc} |"
                )
            lines.append("")
        return "\n".join(lines)

    @classmethod
    def from_file(cls, path: str | Path) -> EnvironmentManifest:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_model(
        cls,
        model: type[Any],
        *,
        source: str | None = None,
        overrides: Mapping[str, Mapping[str, object]] | None = None,
    ) -> EnvironmentManifest:
        """Extract an EnvironmentManifest from a Pydantic v2 BaseModel."""
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise TypeError("model must be a Pydantic BaseModel subclass")
        cfg = model.model_config
        prefix = cfg.get("env_prefix", "")
        prefix = prefix if isinstance(prefix, str) else ""
        fields: list[EnvironmentField] = []
        for name, info in model.model_fields.items():
            val_aliases = list(_validation_aliases(info.validation_alias))
            canonical = val_aliases[0] if val_aliases else f"{prefix}{info.alias or name}".upper()
            aliases = tuple(a for a in val_aliases[1:] if a != canonical)
            extra = info.json_schema_extra if isinstance(info.json_schema_extra, Mapping) else {}
            f_source = FieldSource(str(extra.get("source") or FieldSource.CODE.value))
            required = bool(info.is_required())
            fields.append(
                EnvironmentField(
                    field=str(name),
                    env=str(canonical),
                    aliases=aliases,
                    required=required,
                    injected=bool(extra.get("injected") or f_source in _COMPOSE_SOURCES),
                    has_default=not required,
                    sensitive=(
                        bool(extra.get("sensitive")) or _is_secret_annotation(info.annotation)
                    ),
                    group=str(extra.get("group", "Application")),
                    description=str(info.description or ""),
                    source=f_source,
                    empty_ok=bool(extra.get("empty_ok", False)),
                    scope=str(extra.get("scope") or "env"),
                    provided_by=str(extra.get("provided_by") or ""),
                    composed_from=str(extra.get("composed_from") or ""),
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
                if str(changes.get("source", "")) in {s.value for s in _COMPOSE_SOURCES}:
                    changes.setdefault("injected", True)
                merged_field = {**fields[by_name[name]].model_dump(), **changes}
                fields[by_name[name]] = EnvironmentField.model_validate(merged_field)
        return cls(
            source=source or f"{model.__module__}.{model.__qualname__}",
            fields=tuple(fields),
        )

    @classmethod
    def merge(
        cls,
        manifests: Sequence[tuple[str, EnvironmentManifest]],
        *,
        source: str,
        exclude: Collection[str] = (),
    ) -> EnvironmentManifest:
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
                        raise ValueError(
                            f"{source}: {entry.env}.{key} declared differently in {label}"
                        )
        return cls(source=source, fields=tuple(fields.values()))


class EnvironmentValidation(ContractModel):
    missing: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    invalid: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not (self.missing or self.conflicts or self.invalid)

    def format_error(self, *, source: str = "") -> str:
        prefix = f"{source}: " if source else ""
        parts: list[str] = []
        if self.missing:
            parts.append(f"missing {len(self.missing)} variable(s): {', '.join(self.missing)}")
        if self.conflicts:
            parts.append(f"conflicting variable(s): {', '.join(self.conflicts)}")
        if self.invalid:
            parts.append(f"invalid variable(s): {', '.join(self.invalid)}")
        return prefix + "; ".join(parts) if parts else "environment ok"


def validate_env(
    manifest: EnvironmentManifest,
    environ: Mapping[str, str],
    *,
    require_injected: bool = False,
) -> EnvironmentValidation:
    """Validate ``environ`` against ``manifest`` without raising."""
    missing: list[str] = []
    conflicts: list[str] = []
    for fld in manifest.fields:
        present = [
            (cand, val if fld.sensitive else val.strip())
            for cand in (fld.env, *fld.aliases)
            if isinstance(val := environ.get(cand), str) and (fld.empty_ok or val.strip())
        ]
        if len({v for _, v in present}) > 1:
            conflicts.append(f"{fld.env} ({', '.join(n for n, _ in present)})")
        elif not present and (fld.required or (require_injected and fld.injected)):
            missing.append(fld.env)
    return EnvironmentValidation(missing=tuple(sorted(missing)), conflicts=tuple(sorted(conflicts)))


def assert_env(
    manifest: EnvironmentManifest | str | Path,
    environ: Mapping[str, str] | None = None,
    *,
    require_injected: bool = False,
) -> None:
    """Fail closed when ``environ`` misses required/injected fields or has conflicts."""
    if isinstance(manifest, EnvironmentManifest):
        loaded = manifest
        label = loaded.source
    else:
        path = Path(manifest)
        if not path.is_file():
            raise RuntimeError(f"environment manifest {path} is missing")
        loaded = EnvironmentManifest.from_file(path)
        label = str(path)
    env = os.environ if environ is None else environ
    result = validate_env(loaded, env, require_injected=require_injected)
    if result.conflicts:
        raise EnvironmentConflictError(result.format_error(source=label))
    if not result.ok:
        raise RuntimeError(result.format_error(source=label))


def _load_settings_class(target: str) -> type[Any]:
    module_name, _, attr = target.partition(":")
    if not module_name or not attr:
        raise ValueError(f"settings target must be 'module:Class', not {target!r}")
    if "" not in sys.path:
        sys.path.insert(0, "")
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    if not isinstance(obj, type):
        raise ValueError(f"{target} is not a class")
    return obj


def _handle_export_or_check(args: argparse.Namespace, *, check_only: bool) -> int:
    if args.json is None and args.markdown is None:
        raise SystemExit("pass at least one of --json or --markdown")
    manifest = EnvironmentManifest.from_model(
        _load_settings_class(args.settings), source=args.source
    )
    if offline_errors := manifest.validate_offline():
        for err in offline_errors:
            print(err, file=sys.stderr)
        return 1
    outputs: list[tuple[Path, str]] = []
    if args.json is not None:
        outputs.append((args.json, manifest.to_json()))
    if args.markdown is not None:
        outputs.append((args.markdown, manifest.render_markdown(title=args.title)))
    drift = 0
    for path, text in outputs:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current == text:
            continue
        if check_only:
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
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    if drift:
        print(f"{drift} generated file(s) are stale; run `export` to update")
    return 1 if drift else 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: ``python -m infra2_sdk.config {export|check|validate} ...``."""
    parser = argparse.ArgumentParser(prog="python -m infra2_sdk.config")
    sub = parser.add_subparsers(dest="command", required=True)

    for cmd_name in ("export", "check"):
        sp = sub.add_parser(cmd_name)
        sp.add_argument("settings", help="settings class as module:Class")
        sp.add_argument("--source", default=None, help="manifest source label")
        sp.add_argument("--json", type=Path, default=None, help="manifest JSON path")
        sp.add_argument("--markdown", type=Path, default=None, help="Markdown reference path")
        sp.add_argument("--title", default="", help="Markdown title")

    vp = sub.add_parser("validate")
    vp.add_argument("manifest", type=Path, help="env.manifest.json path")
    vp.add_argument("--no-require-injected", action="store_true")

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command in ("export", "check"):
        return _handle_export_or_check(args, check_only=(args.command == "check"))

    try:
        assert_env(args.manifest, require_injected=not args.no_require_injected)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("environment ok")
    return 0


__all__ = (
    "MANIFEST_CONTRACT_VERSION",
    "EnvironmentTier",
    "PLATFORM_OWNED_TIERS",
    "RuntimeEnvKey",
    "FieldSource",
    "EnvironmentConflictError",
    "RuntimeEnvironment",
    "EnvironmentField",
    "EnvironmentManifest",
    "EnvironmentValidation",
    "is_preview_alias",
    "resolve_tier",
    "resolve_environment",
    "read_runtime_env",
    "shared_env_contract",
    "check_shared_env",
    "validate_env",
    "assert_env",
    "main",
)

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
