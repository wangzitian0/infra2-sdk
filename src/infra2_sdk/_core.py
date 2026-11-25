"""Internal primitives shared across the six public infra2_sdk modules."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, Self
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_SECRET_ENV_RE = re.compile(
    r"(?:^|_)(?:SECRET|TOKEN|PASSWORD|PASSWD|KEY|AUTH|CREDENTIAL|WEBHOOK|DSN)(?:_URL)?$"
    r"|^(?:DATABASE|REDIS)_URL$",
    re.I,
)
_EMAIL_RE = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_SECRET_VAL_RE = re.compile(
    r"Bearer\s+[\w.-]+|eyJ[\w-]{8,}\.[\w-]+\.[\w-]*|(?:gh[ps]_|sk-|op_)[\w-]{8,}|(?<=:)[^:@/\s]+(?=@)"
)
_SENSITIVE_QUERY_RE = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|apikey|key|token|access[_-]?token|secret|password|auth|sig|signature|credential)s?$",
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


def canonical_json_bytes(payload: Any) -> bytes:
    """Serialize payload into canonical UTF-8 JSON bytes."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def canonical_sha256(payload: Any) -> str:
    """Return lowercase hex SHA-256 of canonical UTF-8 JSON bytes."""
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def require_contract_version(value: Any, expected: int, *, label: str) -> int:
    """Reject non-integer (including bool) or mismatched contract_version values."""
    if type(value) is not int:
        raise ValueError("contract_version must be an integer")
    if value != expected:
        raise ValueError(f"unsupported {label} {value}")
    return value


def parse_iso_utc(value: object, *, allow_naive: bool = False) -> datetime:
    """Parse an ISO-8601 timestamp or datetime and normalize to UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise ValueError("expected non-empty ISO 8601 string or datetime")
    if parsed.tzinfo is None:
        if not allow_naive:
            raise ValueError("timestamp must have a time zone")
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def validate_http_url(
    value: str,
    *,
    label: str,
    allow_fragment: bool = True,
    validate_port: bool = False,
) -> None:
    """Validate an HTTP(S) URL without embedded credentials or whitespace."""
    try:
        ep = urlsplit(value)
        host = ep.hostname
        if validate_port:
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


def is_loopback_host(hostname: str) -> bool:
    """Return True when ``hostname`` names a local loopback address."""
    return hostname.strip("[]").lower() in _LOOPBACK_HOSTS


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


def is_sensitive_header(name: str) -> bool:
    low = name.lower()
    return low in _SENSITIVE_HEADER_NAMES or bool(_SENSITIVE_HEADER_RE.search(low))


def sanitize_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    """Copy HTTP headers with credential-bearing values replaced by 'REDACTED'."""
    return {
        str(k): "REDACTED" if is_sensitive_header(str(k)) else str(v) for k, v in headers.items()
    }


def redact_url(url: str) -> str:
    """Strip URL userinfo and redact sensitive query parameter values."""
    parts = urlsplit(url)
    netloc = parts.netloc
    if parts.hostname:
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        netloc = f"{host}:{parts.port}" if parts.port is not None else host
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        scrubbed = [(k, "REDACTED" if _SENSITIVE_QUERY_RE.search(k) else v) for k, v in pairs]
        query = urlencode(scrubbed, doseq=True)
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact_url_credentials(detail: str, url: str | None, *, placeholder: str) -> str:
    """Redact a full URL string and any embedded credentials from an error message."""
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
    secrets: set[str] = set()
    if parsed.password:
        secrets.update((parsed.password, unquote(parsed.password)))
    if parsed.username:
        secrets.update((parsed.username, unquote(parsed.username)))
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
