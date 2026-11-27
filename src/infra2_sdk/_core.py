"""Internal primitives shared across the six public infra2_sdk modules."""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import math
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, Self
from urllib.parse import parse_qsl, unquote, unquote_to_bytes, urlencode, urlsplit, urlunsplit
from warnings import warn

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_UNDERSCORE_PREVIEW_RE = re.compile(
    r"\A(?P<prefix>branch|pr|commit|tag)_(?P<rest>[a-z0-9][a-z0-9._-]*)\Z"
)

_SECRET_ENV_RE = re.compile(
    r"(?:^|_)(?:SECRET|TOKEN|PASSWORD|PASSWD|KEY|AUTH|CREDENTIAL|WEBHOOK|DSN)(?:_URL)?$"
    r"|^(?:DATABASE|REDIS)_URL$",
    re.I,
)
_EMAIL_RE = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_SECRET_VAL_RE = re.compile(
    r"Bearer\s+[A-Za-z0-9._~+/=-]+|eyJ[\w-]{8,}\.[\w-]+\.[\w-]*|(?:gh[ps]_|sk-|op_)[\w-]{8,}|(?<=:)[^:@/\s]+(?=@)"
)
_INLINE_HTTP_URL_RE = re.compile(r"https?://[^\s'\"<>()]+", re.I)
_SENSITIVE_QUERY_RE = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|apikey|awsaccesskeyid|key|token|access[_-]?token|secret|password|auth|code|sig|signature|credential)s?$",
    re.I,
)
_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-goog-api-key",
        "x-auth-token",
        "openai-organization",
        "openai-project",
    }
)
_SENSITIVE_HEADER_RE = re.compile(r"(?:secret|token|key|password|auth|cookie|credential)", re.I)

SECRET_LOG_KEY_RE = re.compile(
    r"^(?:token|.*_(?:secret|password|passwd|api_key|access_key|secret_key|private_key|"
    r"auth_token|access_token|refresh_token|id_token|csrf_token|session_token|credential|"
    r"dsn|database_url|redis_url))$|"
    r"^(?:secret|password|passwd|authorization|cookie|set_cookie|set-cookie|"
    r"api_key|api-key|x-api-key|x-auth-token|raw_prompt|raw_response|response_body|error_body)$",
    re.I,
)

PROBE_PATHS: frozenset[str] = frozenset({"/livez", "/readyz", "/health", "/api/health"})
_REQUIRED_IDENTITY_ATTRIBUTES: tuple[str, ...] = (
    "deployment.environment.name",
    "infra.service.id",
    "service.version",
    "infra.iac.ref",
)
_INTERVAL_MESSAGE = "export_interval_millis must be positive"
_INVALID_PERCENT_RE = re.compile(r"%(?![0-9a-fA-F]{2})")
_OTLP_KEY_RE = re.compile(r"(?:infra\.llm\.|code\.|request_id$)")
_REQUEST_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9._:/@-]{1,128}$")
_RATIO_SAMPLERS = frozenset({"traceidratio", "parentbased_traceidratio"})
_TRACES_SAMPLER_NAMES = (
    "always_on",
    "always_off",
    "traceidratio",
    "parentbased_always_on",
    "parentbased_always_off",
    "parentbased_traceidratio",
)
_DEFAULT_SAMPLER = "parentbased_always_on"

REQUEST_ID_VAR: ContextVar[str | None] = ContextVar("infra2_request_id", default=None)
LOG_CONTEXT_VAR: ContextVar[Mapping[str, Any] | None] = ContextVar("infra2_log_ctx", default=None)

_STD_LOG_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "otelTraceID",
    "otelSpanID",
    "otelTraceSampled",
    "otelServiceName",
}

_GLOBAL_LOCK = threading.Lock()
_ACTIVE_GLOBAL: tuple[Any, OtelSettings] | None = None


def canonicalize_preview_name(cleaned: str) -> str:
    """Normalize a lowercased preview slot alias into canonical hyphenated form."""
    stripped = cleaned.removeprefix("preview/")
    if stripped == "canary_preview":
        return "canary-preview"
    match = _UNDERSCORE_PREVIEW_RE.match(stripped)
    if match is not None:
        rest = match["rest"].replace("_", "-") if match["prefix"] == "tag" else match["rest"]
        return f"{match['prefix']}-{rest}"
    return stripped


class ContractModel(BaseModel):
    """Strict, frozen base class for versioned SDK contracts and settings."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Self:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{cls.__name__} requires a mapping")
        return cls.model_validate(raw)


def require_optional(module: str, *, extra: str) -> ModuleType:
    """Import an optional dependency or raise a clear installation hint."""
    try:
        return importlib.import_module(module)
    except ImportError:
        raise RuntimeError(f"{module} is required; install infra2-sdk[{extra}]") from None


def load_dotted_symbol(spec: str, *, require_type: bool = False) -> Any:
    """Import ``module:attr.path`` dynamically and optionally verify it names a class."""
    module_name, sep, attr_path = spec.partition(":")
    if not sep or not module_name or not attr_path:
        raise ValueError(f"target must be 'module:attr', not {spec!r}")
    if "" not in sys.path:
        sys.path.insert(0, "")
    obj: Any = importlib.import_module(module_name)
    for part in attr_path.split("."):
        obj = getattr(obj, part)
    if require_type and not isinstance(obj, type):
        raise ValueError(f"{spec} is not a class")
    return obj


def canonical_sha256(payload: Any) -> str:
    """Return lowercase hex SHA-256 of canonical UTF-8 JSON bytes."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def require_contract_version(value: Any, expected: int, *, label: str) -> int:
    """Reject non-integer (including bool) or mismatched contract_version values."""
    if type(value) is not int:
        raise ValueError("contract_version must be an integer")
    if value != expected:
        raise ValueError(f"unsupported {label} {value}")
    return value


def parse_iso_utc(value: object) -> datetime:
    """Parse an ISO-8601 timezone-aware timestamp or datetime and normalize to UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise ValueError("expected non-empty ISO 8601 string or datetime")
    if parsed.tzinfo is None:
        raise ValueError("timestamp must have a time zone")
    return parsed.astimezone(UTC)


def validate_http_url(value: str, *, label: str, allow_fragment: bool = True) -> None:
    """Validate an HTTP(S) URL including port bounds and no embedded credentials."""
    try:
        ep = urlsplit(value)
        host = ep.hostname
        _ = ep.port
    except ValueError:
        raise ValueError(f"{label} must be a valid HTTP URL") from None
    if (
        ep.scheme not in {"http", "https"}
        or not ep.netloc
        or not host
        or any(c.isspace() for c in ep.netloc)
    ):
        raise ValueError(f"{label} must use http:// or https:// with a host")
    if ep.username is not None or ep.password is not None:
        raise ValueError(f"{label} must not contain credentials")
    if not allow_fragment and ep.fragment:
        raise ValueError(f"{label} must not contain a fragment")


def redact_secrets(text: str, environ: Mapping[str, str] | None = None) -> str:
    """Scrub secret environment values, tokens, embedded passwords, and emails from text."""
    env = os.environ if environ is None else environ
    scrubbed = _SECRET_VAL_RE.sub("[REDACTED]", text)
    vals = {
        v.strip()
        for k, v in env.items()
        if _SECRET_ENV_RE.search(k) and isinstance(v, str) and len(v.strip()) >= 4
    }
    for val in sorted(vals, key=len, reverse=True):
        scrubbed = scrubbed.replace(val, "***")
    return _EMAIL_RE.sub("[EMAIL]", scrubbed)


def sanitize_headers(headers: Mapping[str, Any]) -> dict[str, Any]:
    """Copy HTTP headers preserving list values while redacting credential-bearing headers."""
    out: dict[str, Any] = {}
    for k, v in headers.items():
        key = str(k)
        low = key.lower()
        if low in _SENSITIVE_HEADER_NAMES or _SENSITIVE_HEADER_RE.search(low):
            out[key] = ["REDACTED"] if isinstance(v, list) else "REDACTED"
        else:
            out[key] = list(v) if isinstance(v, list) else str(v)
    return out


def redact_url(url: str) -> str:
    """Strip URL userinfo and redact sensitive query parameter values without raising."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return _SECRET_VAL_RE.sub("[REDACTED]", url)
    netloc = parts.netloc
    if host:
        fmt_host = f"[{host}]" if ":" in host else host
        netloc = f"{fmt_host}:{port}" if port is not None else fmt_host
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        scrubbed = [(k, "REDACTED" if _SENSITIVE_QUERY_RE.search(k) else v) for k, v in pairs]
        query = urlencode(scrubbed, doseq=True)
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact_text(
    text: str,
    *,
    extra_secrets: Collection[str] = (),
    environ: Mapping[str, str] | None = None,
) -> str:
    """Scrub secrets, extra tokens, and any inline HTTP(S) URLs without mutating exception names."""
    scrubbed = _INLINE_HTTP_URL_RE.sub(lambda m: redact_url(m.group(0)), text)
    scrubbed = redact_secrets(scrubbed, environ=environ)
    for secret in sorted({s for s in extra_secrets if s}, key=len, reverse=True):
        scrubbed = scrubbed.replace(secret, "[REDACTED]")
    return scrubbed


def redact_url_credentials(detail: str, url: str | None, *, placeholder: str) -> str:
    """Redact a full URL string and embedded passwords without clobbering schemes."""
    redacted = detail
    if not url:
        return redacted
    cleaned = url.strip()
    if cleaned:
        redacted = redacted.replace(cleaned, placeholder)
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return redacted
    base = parsed.scheme.split("+", 1)[0].lower()
    if base in {"postgres", "postgresql"}:
        pg_norm = urlunsplit(
            ("postgresql", parsed.netloc, parsed.path, parsed.query, parsed.fragment)
        )
        redacted = redacted.replace(pg_norm, placeholder)
    secrets: set[str] = set()
    if parsed.password:
        secrets.update((parsed.password, unquote(parsed.password)))
    if parsed.query:
        for k, v in parse_qsl(parsed.query, keep_blank_values=True):
            if k.lower() == "password" and v:
                secrets.update((v, unquote(v)))
        for part in parsed.query.split("&"):
            rk, sep, rv = part.partition("=")
            if sep and unquote(rk).lower() == "password" and rv:
                secrets.update((rv, unquote(rv)))
    if not secrets:
        return redacted
    protected = re.compile(rf"({re.escape(placeholder)}|postgresql://|postgres://|rediss?://)")
    chunks = protected.split(redacted)
    sorted_secrets = sorted((s for s in secrets if s), key=len, reverse=True)
    for idx in range(0, len(chunks), 2):
        part = chunks[idx]
        for secret in sorted_secrets:
            part = part.replace(secret, "<redacted>")
        chunks[idx] = part
    return "".join(chunks)


# ---------------------------------------------------------------------------
# Shared internal config, contextvar, logging, and OTel bootstrap plumbing
# ---------------------------------------------------------------------------

_CANARY_SLOT = "canary-preview"
_PREVIEW_RE = re.compile(
    r"\A(?:canary|branch-[a-z0-9][a-z0-9._-]*|pr-[1-9][0-9]*|commit-[0-9a-f]{7,40}|tag-v[0-9]+-[0-9]+-[0-9]+)\Z"
)


def is_preview_alias(value: str) -> bool:
    """Return True when ``value`` names a canonical or shorthand preview slot."""
    cleaned = value.strip().lower()
    if cleaned in ("pr", "canary") or cleaned.startswith("preview/"):
        return True
    alias = canonicalize_preview_name(cleaned)
    return alias == _CANARY_SLOT or bool(_PREVIEW_RE.match(alias))


@dataclass(frozen=True, slots=True)
class RuntimeEnvironment:
    name: str
    tier: Any


def platform_owned_tiers() -> frozenset[Any]:
    from infra2_sdk.config import EnvironmentTier

    return frozenset({EnvironmentTier.PREVIEW, EnvironmentTier.STAGING, EnvironmentTier.PRODUCTION})


def read_runtime_env(
    key: Any,
    environ: Mapping[str, str] | None = None,
    *,
    default: str = "",
    required: bool = False,
) -> str:
    """Read one canonical runtime variable from ``environ``."""
    from infra2_sdk.config import RuntimeEnvKey

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
    from infra2_sdk.config import EnvironmentTier, RuntimeEnvKey, resolve_tier

    values = os.environ if environ is None else environ
    raw = read_runtime_env(
        RuntimeEnvKey.ENVIRONMENT,
        values,
        default="" if required else EnvironmentTier.LOCAL_DEV.value,
        required=required,
    )
    github_actions = str(values.get("GITHUB_ACTIONS", "")).strip().lower() == "true"
    tier = resolve_tier(raw, github_actions=github_actions)
    canonical_name = canonicalize_preview_name(raw.strip().lower())
    name = (
        canonical_name
        if tier is EnvironmentTier.PREVIEW and canonical_name != "preview"
        else tier.value
    )
    return RuntimeEnvironment(name=name, tier=tier)


def _structlog_cvars() -> Any:
    return getattr(sys.modules.get("structlog"), "contextvars", None)


def current_request_id() -> str | None:
    return REQUEST_ID_VAR.get()


@contextmanager
def bind_request_id(request_id: str | None = None) -> Iterator[str]:
    cleaned = (request_id or "").strip()
    rid = cleaned if cleaned and _REQUEST_ID_SAFE_RE.fullmatch(cleaned) else str(uuid.uuid4())
    sl = _structlog_cvars()
    prev_sl = sl.get_contextvars().get("request_id") if sl is not None else None
    if sl is not None:
        sl.bind_contextvars(request_id=rid)
    tok = REQUEST_ID_VAR.set(rid)
    try:
        yield rid
    finally:
        if sl is not None:
            if prev_sl is None:
                sl.unbind_contextvars("request_id")
            else:
                sl.bind_contextvars(request_id=prev_sl)
        try:
            REQUEST_ID_VAR.reset(tok)
        except ValueError:
            REQUEST_ID_VAR.set(None)


@contextmanager
def bind_log_context(**fields: Any) -> Iterator[dict[str, Any]]:
    clean = {k: v for k, v in fields.items() if v is not None}
    if not clean:
        yield dict(LOG_CONTEXT_VAR.get() or {})
        return
    prev = dict(LOG_CONTEXT_VAR.get() or {})
    merged = {**prev, **clean}
    tok = LOG_CONTEXT_VAR.set(merged)
    sl = _structlog_cvars()
    prev_sl: dict[str, Any] = dict(sl.get_contextvars() or {}) if sl is not None else {}
    if sl is not None:
        sl.bind_contextvars(**clean)
    try:
        yield merged
    finally:
        if sl is not None:
            to_unbind = [k for k in clean if k not in prev_sl]
            to_restore = {k: prev_sl[k] for k in clean if k in prev_sl}
            if to_unbind:
                sl.unbind_contextvars(*to_unbind)
            if to_restore:
                sl.bind_contextvars(**to_restore)
        try:
            LOG_CONTEXT_VAR.reset(tok)
        except ValueError:
            LOG_CONTEXT_VAR.set(prev or None)


def scrub_log_value(val: Any, env: Mapping[str, str], key: str | None = None) -> Any:
    if key and SECRET_LOG_KEY_RE.fullmatch(key):
        return "[REDACTED]"
    if val is None or isinstance(val, (bool, int, float)):
        return val
    if isinstance(val, str):
        return redact_secrets(val, env)
    if isinstance(val, Mapping):
        return {k: scrub_log_value(v, env, str(k)) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return type(val)(scrub_log_value(x, env) for x in val)
    return redact_secrets(str(val), env)


class JsonLogFormatter(logging.Formatter):
    """Compact single-line JSON formatter with OTel trace ID and secret scrubbing."""

    def __init__(self, identity: Mapping[str, str] | None = None) -> None:
        super().__init__()
        self._identity = dict(identity or {})

    def format(self, record: logging.LogRecord) -> str:
        env = os.environ
        sl = _structlog_cvars()
        sl_ctx = dict(sl.get_contextvars() or {}) if sl else {}
        ctx = dict(LOG_CONTEXT_VAR.get() or {}) | sl_ctx

        tid, sid = record.__dict__.get("otelTraceID"), record.__dict__.get("otelSpanID")
        if tid and set(str(tid)) != {"0"}:
            tid_s: str | None = str(tid)
            sid_s: str | None = str(sid) if sid else None
        elif (ot := sys.modules.get("opentelemetry.trace")) and (
            sc := ot.get_current_span().get_span_context()
        ).is_valid:
            tid_s, sid_s = format(sc.trace_id, "032x"), format(sc.span_id, "016x")
        else:
            tid_s = sid_s = None

        out: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub_log_value(record.getMessage(), env),
            "trace_id": tid_s,
            "span_id": sid_s,
            "request_id": current_request_id() or ctx.get("request_id") or None,
            **self._identity,
        }
        for k, v in (record.__dict__ | ctx).items():
            if k not in _STD_LOG_ATTRS and k not in out:
                out[k] = scrub_log_value(v, env, str(k))
        if record.exc_info:
            out["exception"] = scrub_log_value(self.formatException(record.exc_info), env)
        return json.dumps(out, separators=(",", ":"), default=str)


def configure_logging(
    *,
    level: int | None = logging.INFO,
    service_name: str = "",
    environment: str = "local_dev",
    service_version: str = "unknown",
    probe_paths: Collection[str] = PROBE_PATHS,
    suppress_probes: bool = True,
) -> None:
    if level is not None:
        root = logging.getLogger()
        has_json_stream = any(isinstance(h.formatter, JsonLogFormatter) for h in root.handlers)
        if not has_json_stream:
            for h in list(root.handlers):
                if type(h) is logging.StreamHandler and h.formatter is None:
                    root.removeHandler(h)
            handler = logging.StreamHandler(sys.stdout)
            ident = {
                "service.name": service_name or "unknown_service",
                "deployment.environment.name": environment,
                "service.version": service_version,
            }
            handler.setFormatter(JsonLogFormatter(ident))
            root.addHandler(handler)
        root.setLevel(level)
    if suppress_probes:
        access = logging.getLogger("uvicorn.access")
        if not any(getattr(f, "_infra2_probe_filter", False) for f in access.filters):
            paths = frozenset(probe_paths)

            class _AccessFilter(logging.Filter):
                _infra2_probe_filter = True

                def filter(self, record: logging.LogRecord) -> bool:
                    if isinstance(record.args, tuple) and len(record.args) >= 3:
                        raw_path = str(record.args[2]).split("?", 1)[0]
                        if raw_path in paths:
                            return False
                    msg = record.getMessage()
                    return not any(f" {p} " in msg or f' "{p} ' in msg for p in paths)

            access.addFilter(_AccessFilter())


def _parse_resource_attributes(value: str) -> dict[str, str]:
    if not value.strip():
        return {}
    attributes: dict[str, str] = {}
    for item in value.split(","):
        if "=" not in item:
            raise ValueError("OTEL_RESOURCE_ATTRIBUTES entries must use key=value")
        ek, raw = (part.strip() for part in item.split("=", 1))
        if _INVALID_PERCENT_RE.search(ek) or _INVALID_PERCENT_RE.search(raw):
            raise ValueError("OTEL_RESOURCE_ATTRIBUTES contains an invalid percent escape")
        try:
            key = unquote_to_bytes(ek).decode("utf-8")
            decoded = unquote_to_bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("OTEL_RESOURCE_ATTRIBUTES contains invalid UTF-8") from None
        if not key or not decoded:
            raise ValueError("OTEL_RESOURCE_ATTRIBUTES entries must be non-empty")
        if key in attributes:
            raise ValueError(f"OTEL_RESOURCE_ATTRIBUTES repeats {key!r}")
        attributes[key] = decoded
    return attributes


def _parse_otel_boolean(value: str | None, *, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    norm = value.strip().lower()
    if norm == "true":
        return True
    if norm == "false":
        return False
    raise ValueError("OpenTelemetry boolean environment variables must be true or false")


def _parse_traces_sampler(name: str | None, arg: str | None = None) -> tuple[str, float | None]:
    norm = (name or "").strip().lower()
    if not norm:
        return _DEFAULT_SAMPLER, None
    if norm not in _TRACES_SAMPLER_NAMES:
        expected = ", ".join(_TRACES_SAMPLER_NAMES)
        raise ValueError(f"unsupported OTEL_TRACES_SAMPLER {norm!r}; expected one of {expected}")
    if norm not in _RATIO_SAMPLERS:
        return norm, None
    raw = (arg or "").strip()
    if not raw:
        return norm, 1.0
    try:
        ratio = float(raw)
    except ValueError:
        raise ValueError("OTEL_TRACES_SAMPLER_ARG must be a number between 0 and 1") from None
    if not math.isfinite(ratio) or not 0.0 <= ratio <= 1.0:
        raise ValueError("OTEL_TRACES_SAMPLER_ARG must be a number between 0 and 1")
    return norm, ratio


def _normalize_deployment_env(value: str, tier: Any) -> str:
    from infra2_sdk.config import EnvironmentTier, resolve_tier

    resolved = resolve_tier(tier)
    cleaned = value.strip().lower()
    if not cleaned:
        return resolved.value
    if resolved is EnvironmentTier.PREVIEW:
        return canonicalize_preview_name(cleaned)
    if resolve_tier(cleaned) is not resolved:
        raise ValueError("deployment_environment disagrees with environment tier")
    return resolved.value


def _strict_or_warn(fn: Any, *, fallback: Any, strict: bool, prefix: str) -> Any:
    try:
        return fn()
    except ValueError as exc:
        if strict:
            raise ValueError(f"{prefix}: {exc}" if prefix else str(exc)) from exc
        warn(f"{prefix}: {exc}" if prefix else str(exc), RuntimeWarning, stacklevel=3)
        return fallback


class OtelSettings(ContractModel):
    """Explicit OpenTelemetry exporter, resource identity, and sampling configuration."""

    service_name: str
    endpoint: str | None = None
    service_version: str = "unknown"
    environment: str = "local_dev"
    deployment_environment: str = ""
    instance_id: str = ""
    resource_attributes: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    export_interval_millis: int = 60_000
    traces_sampler: str = ""
    traces_sampler_arg: str = ""
    sdk_disabled: bool = False
    suppress_probe_telemetry: bool = False

    @field_validator("export_interval_millis")
    @classmethod
    def _positive_interval(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(_INTERVAL_MESSAGE)
        return v

    @model_validator(mode="after")
    def _normalize_settings(self) -> OtelSettings:
        from infra2_sdk.config import resolve_tier

        if not self.service_name.strip():
            raise ValueError("service_name is required")
        tier = resolve_tier(self.environment)
        object.__setattr__(self, "environment", tier.value)
        dep_env = _normalize_deployment_env(self.deployment_environment, tier)
        object.__setattr__(self, "deployment_environment", dep_env)
        if self.enabled and not self.endpoint:
            raise ValueError("enabled telemetry requires an OTLP HTTP endpoint")
        if self.endpoint:
            validate_http_url(self.endpoint, label="OTLP endpoint", allow_fragment=False)
        sampler, _ = _parse_traces_sampler(self.traces_sampler, self.traces_sampler_arg)
        object.__setattr__(self, "traces_sampler", sampler if self.traces_sampler.strip() else "")
        object.__setattr__(self, "traces_sampler_arg", self.traces_sampler_arg.strip())
        if any(not k or not v for k, v in self.resource_attributes.items()):
            raise ValueError("resource attributes must have non-empty string keys and values")
        return self

    def resolved_resource_attributes(self) -> dict[str, str]:
        attrs = {
            **self.resource_attributes,
            "service.name": self.service_name,
            "service.version": self.service_version,
            "deployment.environment.name": self.deployment_environment,
        }
        if self.instance_id:
            attrs["service.instance.id"] = self.instance_id
        return attrs

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        default_service_name: str | None = None,
        strict: bool = True,
        require_issued_identity: bool = False,
        suppress_probe_telemetry: bool = False,
    ) -> OtelSettings:
        from infra2_sdk.config import EnvironmentTier, RuntimeEnvKey, resolve_tier

        values = os.environ if environ is None else environ
        if require_issued_identity:
            rt = RuntimeEnvironment(
                name=EnvironmentTier.LOCAL_DEV.value, tier=EnvironmentTier.LOCAL_DEV
            )
            env_is_set = False
        else:
            rt = resolve_environment(values, required=strict)
            env_is_set = bool(read_runtime_env(RuntimeEnvKey.ENVIRONMENT, values))

        raw_attrs = read_runtime_env(RuntimeEnvKey.OTEL_RESOURCE_ATTRIBUTES, values)
        attrs = _strict_or_warn(
            lambda: _parse_resource_attributes(raw_attrs),
            fallback={},
            strict=strict,
            prefix="invalid OTEL_RESOURCE_ATTRIBUTES",
        )
        for k in ("service.name", "service.version", "service.instance.id"):
            if k in attrs:
                if attrs[k].strip():
                    attrs[k] = attrs[k].strip()
                else:
                    del attrs[k]
        dep_env = attrs.get("deployment.environment.name", "").strip() or None

        service_name = (
            read_runtime_env(RuntimeEnvKey.SERVICE_NAME, values)
            or attrs.get("service.name")
            or default_service_name
            or "unknown_service"
        )
        service_version = (
            read_runtime_env(RuntimeEnvKey.SERVICE_VERSION, values)
            or attrs.get("service.version")
            or "unknown"
        )
        instance_id = attrs.get("service.instance.id", "")

        endpoint = read_runtime_env(RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT, values) or None
        disabled = _strict_or_warn(
            lambda: _parse_otel_boolean(values.get("OTEL_SDK_DISABLED")),
            fallback=False,
            strict=strict,
            prefix="invalid OTEL_SDK_DISABLED",
        )
        eff_ep = None if disabled else endpoint

        if require_issued_identity and eff_ep:
            missing = [
                *(
                    [RuntimeEnvKey.SERVICE_NAME.value]
                    if not read_runtime_env(RuntimeEnvKey.SERVICE_NAME, values)
                    else []
                ),
                *(a for a in _REQUIRED_IDENTITY_ATTRIBUTES if not attrs.get(a, "").strip()),
            ]
            if missing:
                raise ValueError(
                    "telemetry requires issued identity attributes when OTLP endpoint is set; "
                    f"missing: {', '.join(missing)}"
                )

        if dep_env and (not env_is_set or require_issued_identity):
            try:
                inferred = resolve_tier(dep_env)
            except ValueError as exc:
                raise ValueError(
                    f"invalid deployment.environment.name {dep_env!r}: {exc}"
                ) from None
            dep_env = _normalize_deployment_env(dep_env, inferred)
            rt = RuntimeEnvironment(name=dep_env, tier=inferred)
        elif dep_env:
            dep_env = _normalize_deployment_env(dep_env, rt.tier)

        s_name = str(values.get("OTEL_TRACES_SAMPLER") or "").strip()
        s_arg = str(values.get("OTEL_TRACES_SAMPLER_ARG") or "").strip()
        s_name, s_arg = _strict_or_warn(
            lambda: (_parse_traces_sampler(s_name, s_arg), (s_name, s_arg))[1],
            fallback=("", ""),
            strict=strict,
            prefix="invalid OTEL_TRACES_SAMPLER",
        )

        raw_interval = str(values.get("OTEL_METRIC_EXPORT_INTERVAL") or "").strip()
        interval = 60_000
        if raw_interval:

            def _parse_int() -> int:
                val = int(raw_interval)
                if val <= 0:
                    raise ValueError(_INTERVAL_MESSAGE)
                return val

            interval = _strict_or_warn(
                _parse_int,
                fallback=60_000,
                strict=strict,
                prefix="invalid OTEL_METRIC_EXPORT_INTERVAL",
            )

        return cls(
            service_name=service_name,
            endpoint=eff_ep,
            service_version=service_version,
            environment=rt.tier.value,
            deployment_environment=dep_env or rt.name,
            instance_id=instance_id,
            resource_attributes=attrs,
            enabled=bool(eff_ep),
            export_interval_millis=interval,
            traces_sampler=s_name,
            traces_sampler_arg=s_arg,
            sdk_disabled=disabled,
            suppress_probe_telemetry=suppress_probe_telemetry,
        )


def clear_active_global(providers: Any) -> None:
    global _ACTIVE_GLOBAL
    with _GLOBAL_LOCK:
        if _ACTIVE_GLOBAL is not None and _ACTIVE_GLOBAL[0] is providers:
            _ACTIVE_GLOBAL = None


class _RedactingLogRecordProcessor:
    def on_emit(self, record: Any) -> None:
        rec, env = getattr(record, "log_record", record), os.environ
        if rec.body is not None:
            rec.body = scrub_log_value(rec.body, env)
        sl = _structlog_cvars()
        sl_ctx = dict(sl.get_contextvars() or {}) if sl else {}
        log_ctx = dict(LOG_CONTEXT_VAR.get() or {}) | {
            k: v for k, v in sl_ctx.items() if _OTLP_KEY_RE.match(k)
        }
        rid = current_request_id()
        if rid and "request_id" not in log_ctx:
            log_ctx["request_id"] = rid
        if rec.attributes or log_ctx:
            merged = dict(rec.attributes or {}) | log_ctx
            rec.attributes = {k: scrub_log_value(v, env, str(k)) for k, v in merged.items()}

    def enabled(self, *_: Any, **__: Any) -> bool:
        return True

    def shutdown(self) -> None:
        pass

    def force_flush(self, _t: int = 30_000) -> bool:
        return True


def _ot(module: str) -> Any:
    return require_optional(f"opentelemetry.{module}", extra="telemetry")


def signal_endpoint(base_endpoint: str | None, signal: str) -> str:
    """Derive the OTLP/HTTP per-signal URL while preserving query parameters."""
    if not isinstance(base_endpoint, str) or not base_endpoint.strip():
        raise ValueError("OTLP endpoint is required")
    validate_http_url(base_endpoint.strip(), label="OTLP endpoint", allow_fragment=False)
    ep = urlsplit(base_endpoint.strip())
    cleaned = ep.path.rstrip("/")
    for known in ("traces", "metrics", "logs"):
        if cleaned.endswith(suffix := f"/v1/{known}"):
            cleaned = cleaned[: -len(suffix)]
            break
    return urlunsplit((ep.scheme, ep.netloc, f"{cleaned}/v1/{signal}", ep.query, ""))


def _is_probe_span(name: str, attributes: Mapping[str, Any] | None) -> bool:
    if any(name == p or name.endswith(f" {p}") or name.startswith(f"{p} ") for p in PROBE_PATHS):
        return True
    return bool(
        attributes
        and any(
            isinstance(v := attributes.get(k), str) and v.split("?", 1)[0] in PROBE_PATHS
            for k in ("url.path", "http.target", "http.route")
        )
    )


def _build_sampler(sampling: Any, settings: OtelSettings) -> Any:
    name, ratio = _parse_traces_sampler(settings.traces_sampler, settings.traces_sampler_arg)
    root = {"always_on": sampling.ALWAYS_ON, "always_off": sampling.ALWAYS_OFF}.get(
        name.removeprefix("parentbased_")
    ) or sampling.TraceIdRatioBased(ratio)
    delegate = sampling.ParentBased(root) if name.startswith("parentbased_") else root
    if not settings.suppress_probe_telemetry:
        return delegate

    class _DropProbeSampler(sampling.Sampler):  # type: ignore[misc]
        def should_sample(
            self,
            parent_context: Any,
            trace_id: int,
            span_name: str,
            kind: Any = None,
            attributes: Any = None,
            links: Any = None,
            trace_state: Any = None,
        ) -> Any:
            if _is_probe_span(span_name, attributes):
                return sampling.SamplingResult(sampling.Decision.DROP, {}, trace_state)
            return delegate.should_sample(
                parent_context, trace_id, span_name, kind, attributes, links, trace_state
            )

        def get_description(self) -> str:
            return f"DropProbeSampler({delegate.get_description()})"

    return _DropProbeSampler()


def _build_providers(
    settings: OtelSettings, *, set_global: bool, capture_logs: bool
) -> tuple[Any, list[str]]:
    from infra2_sdk.telemetry import TelemetryProviders

    trace_api, metrics_api, logs_api = _ot("trace"), _ot("metrics"), _ot("_logs")
    res = _ot("sdk.resources").Resource.create(settings.resolved_resource_attributes())
    tp = _ot("sdk.trace").TracerProvider(
        resource=res, sampler=_build_sampler(_ot("sdk.trace.sampling"), settings)
    )
    tp.add_span_processor(
        _ot("sdk.trace.export").BatchSpanProcessor(
            _ot("exporter.otlp.proto.http.trace_exporter").OTLPSpanExporter(
                endpoint=signal_endpoint(settings.endpoint, "traces")
            )
        )
    )
    mp = _ot("sdk.metrics").MeterProvider(
        resource=res,
        metric_readers=[
            _ot("sdk.metrics.export").PeriodicExportingMetricReader(
                _ot("exporter.otlp.proto.http.metric_exporter").OTLPMetricExporter(
                    endpoint=signal_endpoint(settings.endpoint, "metrics")
                ),
                export_interval_millis=settings.export_interval_millis,
            )
        ],
    )
    started = time.monotonic()
    mp.get_meter("infra2_sdk").create_observable_gauge(
        "process.uptime",
        callbacks=[lambda _: (metrics_api.Observation(time.monotonic() - started),)],
        unit="s",
    )
    lp = _ot("sdk._logs").LoggerProvider(resource=res)
    lp.add_log_record_processor(_RedactingLogRecordProcessor())
    lp.add_log_record_processor(
        _ot("sdk._logs.export").BatchLogRecordProcessor(
            _ot("exporter.otlp.proto.http._log_exporter").OTLPLogExporter(
                endpoint=signal_endpoint(settings.endpoint, "logs")
            )
        )
    )
    providers = TelemetryProviders(tp, mp, lp)
    if not set_global:
        return providers, []
    trace_api.set_tracer_provider(tp)
    metrics_api.set_meter_provider(mp)
    logs_api.set_logger_provider(lp)
    not_installed = [
        sig
        for sig, ok in (
            ("tracer", trace_api.get_tracer_provider() is tp),
            ("meter", metrics_api.get_meter_provider() is mp),
            ("logger", logs_api.get_logger_provider() is lp),
        )
        if not ok
    ]
    inst = _ot("instrumentation.logging").LoggingInstrumentor()
    inst.instrument(
        tracer_provider=tp, inject_trace_context=True, enable_log_auto_instrumentation=False
    )
    providers.logging_instrumentor = inst
    if capture_logs:
        handler_cls = _ot("instrumentation.logging.handler").LoggingHandler
        handler = handler_cls(level=logging.NOTSET, logger_provider=lp)
        logging.getLogger().addHandler(handler)
        providers.logging_handler = handler
    return providers, not_installed


def configure_telemetry(
    settings: OtelSettings,
    *,
    set_global: bool = False,
    capture_logs: bool = True,
) -> Any:
    """Create (and optionally install) OpenTelemetry trace, metric, and log providers."""
    from infra2_sdk.telemetry import TelemetryProviders

    global _ACTIVE_GLOBAL
    if not settings.enabled:
        return TelemetryProviders()
    with _GLOBAL_LOCK:
        if set_global and _ACTIVE_GLOBAL is not None:
            active, active_settings = _ACTIVE_GLOBAL
            if active_settings != settings:
                warn(
                    "telemetry is already configured globally; ignoring the new settings",
                    RuntimeWarning,
                    stacklevel=2,
                )
            return active
        providers, not_installed = _build_providers(
            settings, set_global=set_global, capture_logs=capture_logs
        )
        if set_global:
            _ACTIVE_GLOBAL = (providers, settings)
    if not_installed:
        warn(
            f"the global {', '.join(not_installed)} provider was already set and stays in "
            "place; this call's provider is not global",
            RuntimeWarning,
            stacklevel=2,
        )
    return providers


def bootstrap_telemetry(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
    enable_otel: bool = True,
    strict_otel: bool = True,
    require_issued_identity: bool = False,
    suppress_probes: bool = False,
    log_level: int | None = logging.INFO,
    logger: logging.Logger | None = None,
    strict_hint: str = "",
) -> tuple[OtelSettings | None, OtelSettings | None, str, dict[str, str]]:
    """Parse OtelSettings, configure logging, and return settings/release metadata."""
    from infra2_sdk.config import EnvironmentTier, RuntimeEnvKey

    values = os.environ if environ is None else environ
    parsed_settings: OtelSettings | None = None
    active_settings: OtelSettings | None = None
    tier = EnvironmentTier.LOCAL_DEV.value

    if enable_otel:
        try:
            parsed_settings = OtelSettings.from_env(
                values,
                strict=strict_otel,
                default_service_name=name,
                require_issued_identity=require_issued_identity,
                suppress_probe_telemetry=suppress_probes,
            )
            tier = parsed_settings.environment
            if parsed_settings.endpoint and not parsed_settings.sdk_disabled:
                active_settings = parsed_settings
            elif logger is not None:
                deployed = EnvironmentTier(tier) in platform_owned_tiers()
                logger.log(
                    logging.WARNING if deployed else logging.DEBUG,
                    "%s is not set or disabled; OpenTelemetry export is off",
                    RuntimeEnvKey.OTEL_EXPORTER_OTLP_ENDPOINT.value,
                )
        except Exception as exc:
            if strict_otel:
                if strict_hint and isinstance(exc, ValueError):
                    raise type(exc)(f"{exc}; {strict_hint}") from exc
                raise
            if logger is not None:
                logger.warning("Could not initialize OpenTelemetry: %s", exc)
            else:
                warn(str(exc), RuntimeWarning, stacklevel=2)
    else:
        tier = resolve_environment(values).tier.value

    sha = read_runtime_env(RuntimeEnvKey.GIT_COMMIT_SHA, values) or "unknown"
    ver = (
        parsed_settings.service_version
        if parsed_settings is not None
        else (read_runtime_env(RuntimeEnvKey.SERVICE_VERSION, values) or "unknown")
    )
    release_ver = ver if ver and ver != "unknown" else sha
    configure_logging(
        level=log_level,
        service_name=parsed_settings.service_name if parsed_settings else name,
        environment=parsed_settings.deployment_environment if parsed_settings else tier,
        service_version=release_ver,
        suppress_probes=suppress_probes,
    )
    return parsed_settings, active_settings, tier, {"version": release_ver, "git_sha": sha}
