"""Internal primitives shared across the six public infra2_sdk modules."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, Self
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

_UNDERSCORE_PREVIEW_RE = re.compile(
    r"\A(?P<prefix>branch|pr|commit|tag)_(?P<rest>[a-z0-9][a-z0-9._-]*)\Z"
)


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


_SECRET_ENV_RE = re.compile(
    r"(?:^|_)(?:SECRET|TOKEN|PASSWORD|PASSWD|KEY|AUTH|CREDENTIAL|WEBHOOK|DSN)(?:_URL)?$"
    r"|^(?:DATABASE|REDIS)_URL$",
    re.I,
)
_EMAIL_RE = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_SECRET_VAL_RE = re.compile(
    r"Bearer\s+[A-Za-z0-9._~+/=-]+|eyJ[\w-]{8,}\.[\w-]+\.[\w-]*|(?:gh[ps]_|sk-|op_)[\w-]{8,}|(?<=:)[^:@/\s]+(?=@)"
)
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


def redact_url_credentials(detail: str, url: str | None, *, placeholder: str) -> str:
    """Redact a full URL string and embedded passwords without clobbering usernames."""
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
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        redacted = redacted.replace(secret, "<redacted>")
    return redacted
