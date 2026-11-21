"""Large language model runtime configuration, pydantic-ai factory, OTel guards, and VCR helpers."""

from __future__ import annotations

import copy
import dataclasses
import inspect
import json
import logging
import re
import sys
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from infra2_sdk._wire import (
    canonical_sha256 as _canonical_sha256,
)
from infra2_sdk._wire import (
    validate_http_url as _validate_http_url,
)
from infra2_sdk.runtime._optional import require as _require
from infra2_sdk.runtime.otel import _bind_log_context

_LOGGER = logging.getLogger("infra2_sdk.runtime.llm")
_ModelRole = Literal["large", "small", "multimodal"]
_ThinkingLevel = bool | Literal["minimal", "low", "medium", "high", "xhigh"]
_ROLES = ("large", "small", "multimodal")
_DEFAULT_ZAI_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
_DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_JSON_FENCE_RE = re.compile(r"\A```(?:json)?\s*(.*?)\s*```\Z", re.S | re.I)
_CALL_SITE_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
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
_DEFAULT_VCR_FILTER_HEADERS = (
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "x-goog-api-key",
    "cookie",
    "set-cookie",
)
_DEFAULT_VCR_FILTER_QUERY = (
    "key",
    "api_key",
    "apikey",
    "token",
    "access_token",
    "secret",
    "password",
    "auth",
    "sig",
    "signature",
)
_FRAME_SKIP_PREFIXES = (
    "infra2_sdk.",
    "pydantic_ai",
    "pydantic_graph",
    "opentelemetry",
    "contextlib",
    "asyncio",
    "anyio",
    "concurrent",
    "threading",
    "importlib",
    "functools",
    "pluggy",
    "_pytest",
    "pytest",
)
_LLM_CONTEXT: ContextVar[Mapping[str, Any] | None] = ContextVar("infra2_llm_ctx", default=None)
_PENDING_SYNC_CALLER: ContextVar[tuple[int, tuple[dict[str, str | int], str]] | None] = ContextVar(
    "infra2_llm_pending_caller", default=None
)
_GUARDED_MODEL_CLS: type[Any] | None = None


def _reset_llm_context(tok: Any, prev: Mapping[str, Any]) -> None:
    try:
        _LLM_CONTEXT.reset(tok)
    except ValueError:
        _LLM_CONTEXT.set(dict(prev) if prev else None)


def _is_internal_agent_reentry(agent_id: int, prev: Mapping[str, Any]) -> bool:
    if prev.get("_infra2_agent_id") != agent_id:
        return False
    frame = inspect.currentframe()
    try:
        cur = frame.f_back if frame is not None else None
        while cur is not None:
            mod = str(cur.f_globals.get("__name__", ""))
            if not mod.startswith(("infra2_sdk.", "contextlib", "asyncio", "pydantic_graph")):
                return mod.startswith("pydantic_ai")
            cur = cur.f_back
        return False
    finally:
        del frame


def _coerce_tuple(value: Any) -> tuple[str, ...]:
    is_seq = isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray))
    items = value.split(",") if isinstance(value, str) else (value if is_seq else ())
    return tuple(s for item in items if (s := str(item).strip()))


@dataclass(frozen=True)
class LlmSettings:
    provider: str = "zai"
    base_url: str = _DEFAULT_ZAI_BASE_URL
    api_key: str | None = field(default=None, repr=False)
    large_model: str = field(default="glm-5.3-flash", kw_only=True)
    small_model: str = field(default="glm-5.3-flash", kw_only=True)
    multimodal_model: str = field(default="glm-5.3-flash", kw_only=True)
    large_reasoning_effort: str = field(default="high", kw_only=True)
    small_reasoning_effort: str = field(default="low", kw_only=True)
    multimodal_reasoning_effort: str = field(default="low", kw_only=True)

    def __post_init__(self) -> None:
        for role in _ROLES:
            for suffix in ("model", "reasoning_effort"):
                rf = f"{role}_{suffix}"
                if not isinstance(v := getattr(self, rf), str) or not v.strip():
                    raise ValueError(f"{rf} must be non-empty")
        if not self.provider.strip():
            raise ValueError("provider must be non-empty")
        _validate_http_url(self.base_url, label="base_url")

    def _role_attr(self, role: _ModelRole, suffix: str) -> str:
        if role not in _ROLES:
            raise ValueError(f"unknown model role: {role!r}")
        return getattr(self, f"{role}_{suffix}")

    def role_model(self, role: _ModelRole) -> str:
        return self._role_attr(role, "model")

    def role_reasoning_effort(self, role: _ModelRole) -> str:
        return self._role_attr(role, "reasoning_effort")

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, str], **overrides: Any) -> LlmSettings:
        """Build LlmSettings from a plain string mapping using shared_env.yaml names."""
        from infra2_sdk.runtime.environment import _resolve_env, shared_env_contract

        llm_specs = {str(entry["name"]): entry for entry in shared_env_contract()["groups"]["llm"]}

        def _pick(canonical: str, *, sensitive: bool = False) -> str | None:
            entry = llm_specs[canonical]
            aliases = tuple(str(a) for a in (entry.get("deprecated_aliases") or ()))
            return _resolve_env(mapping, canonical, aliases=aliases, sensitive=sensitive).value

        values: dict[str, Any] = {}
        for attr, key, sensitive in (
            ("provider", "LLM_PROVIDER", False),
            ("base_url", "LLM_BASE_URL", False),
            ("api_key", "LLM_API_KEY", True),
            ("large_model", "LLM_LARGE_MODEL", False),
            ("small_model", "LLM_SMALL_MODEL", False),
            ("multimodal_model", "LLM_MULTIMODAL_MODEL", False),
        ):
            if attr in overrides and overrides[attr] is not None:
                continue
            resolved = _pick(key, sensitive=sensitive)
            if resolved is not None:
                values[attr] = resolved
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)


@dataclass(frozen=True)
class DecodingSpec:
    """Versioned decoding knobs mapped onto pydantic-ai ModelSettings."""

    decoding_version: str = "v1"
    thinking: _ThinkingLevel | None = field(default=None, kw_only=True)
    temperature: float | None = field(default=None, kw_only=True)
    top_p: float | None = field(default=None, kw_only=True)
    seed: int | None = field(default=None, kw_only=True)
    max_tokens: int | None = field(default=None, kw_only=True)
    timeout: float | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.decoding_version, str) or not self.decoding_version.strip():
            raise ValueError("decoding_version must be non-empty")
        valid_toks = self.max_tokens is not None and (
            type(self.max_tokens) is not int or self.max_tokens < 1
        )
        if valid_toks:
            raise ValueError("max_tokens must be at least 1")
        if self.timeout is not None and (isinstance(self.timeout, bool) or self.timeout <= 0):
            raise ValueError("timeout must be positive")
        if self.temperature is not None and (
            isinstance(self.temperature, bool) or self.temperature < 0
        ):
            raise ValueError("temperature must be non-negative")
        if self.top_p is not None and (
            isinstance(self.top_p, bool) or not 0.0 <= float(self.top_p) <= 1.0
        ):
            raise ValueError("top_p must be between 0.0 and 1.0")

    def to_model_settings(self) -> dict[str, Any]:
        """Convert non-None fields to pydantic-ai ModelSettings keys."""
        keys = ("thinking", "temperature", "top_p", "seed", "max_tokens", "timeout")
        return {k: v for k in keys if (v := getattr(self, k)) is not None}


def _normalize_hash_input(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, type) and hasattr(value, "model_json_schema"):
        return _normalize_hash_input(value.model_json_schema())
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _normalize_hash_input(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _normalize_hash_input(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _normalize_hash_input(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        normalized = [_normalize_hash_input(x) for x in value]
        return sorted(normalized, key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=True))
    if isinstance(value, Sequence):
        return [_normalize_hash_input(x) for x in value]
    return str(value)


def prompt_sha256(prompt: Any) -> str:
    """Return canonical lowercase SHA-256 hex digest of a prompt or message sequence."""
    return _canonical_sha256(_normalize_hash_input(prompt))


def schema_sha256(schema: Any) -> str:
    """Return canonical lowercase SHA-256 hex digest of an output schema or Pydantic model."""
    if schema is None:
        raise ValueError("schema is required")
    return _canonical_sha256(_normalize_hash_input(schema))


def request_sha256(
    *,
    model: str,
    prompt: Any,
    schema: Any = None,
    decoding: DecodingSpec | Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """Return canonical lowercase SHA-256 hex digest for model, prompt, schema, and decoding."""
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be non-empty")
    payload = {
        "model": model.strip(),
        "prompt": _normalize_hash_input(prompt),
        "schema": _normalize_hash_input(schema),
        "decoding": _normalize_hash_input(decoding),
        "extra": _normalize_hash_input(extra or {}),
    }
    return _canonical_sha256(payload)


def _is_sensitive_header(name: str) -> bool:
    low = name.lower()
    return low in _SENSITIVE_HEADER_NAMES or bool(_SENSITIVE_HEADER_RE.search(low))


def sanitize_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    """Return a copy of HTTP headers with credential-bearing values replaced by 'REDACTED'."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        ks = str(k)
        out[ks] = "REDACTED" if _is_sensitive_header(ks) else str(v)
    return out


def redact_uri(uri: str) -> str:
    """Strip URL userinfo and redact sensitive query parameter values."""
    parts = urlsplit(uri)
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


def _safe_token(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return text if _SAFE_TOKEN_RE.fullmatch(text) else "redacted"


def _extract_provider_code(exc: BaseException) -> str | None:
    direct = getattr(exc, "provider_error_code", None) or getattr(exc, "code", None)
    if direct is not None:
        return _safe_token(direct)
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        if (code := body.get("code")) is not None:
            return _safe_token(code)
        err = body.get("error")
        if isinstance(err, Mapping) and (code := err.get("code")) is not None:
            return _safe_token(code)
    return None


def redact_llm_error(exc: BaseException) -> Exception:
    """Scrub prompt/credential text while preserving exact pydantic-ai ModelAPIError subclass."""
    raw_status = getattr(exc, "status_code", None)
    status_code = raw_status if type(raw_status) is int else None
    provider_code = _extract_provider_code(exc)
    cls_label = type(exc).__name__.removeprefix("_HTTP")
    summary = (
        f"{cls_label} (http={status_code if status_code is not None else 'none'}, "
        f"provider_code={provider_code or 'none'})"
    )
    pa_exc = sys.modules.get("pydantic_ai.exceptions")
    model_api_err = getattr(pa_exc, "ModelAPIError", None) if pa_exc is not None else None
    if model_api_err is not None and isinstance(exc, model_api_err):
        safe_type = _safe_token(getattr(exc, "provider_error_type", None))
        retry_after = getattr(exc, "retry_after", None)
        in_stream = bool(getattr(exc, "in_stream", False))
        if isinstance(exc, pa_exc.ModelHTTPError):
            raw_hdrs = getattr(exc, "headers", None)
            safe_hdrs = sanitize_headers(raw_hdrs) if isinstance(raw_hdrs, Mapping) else None
            return type(exc)(
                status_code=exc.status_code,
                model_name=exc.model_name,
                body=summary,
                headers=safe_hdrs,
                provider_error_code=provider_code,
                provider_error_type=safe_type,
                retry_after=retry_after,
                in_stream=in_stream,
            )
        if isinstance(exc, pa_exc.ModelRequestTimeout):
            return pa_exc.ModelRequestTimeout(exc.model_name, exc.timeout, setting=exc.setting)
        if isinstance(exc, pa_exc.ModelConnectionError):
            return type(exc)(
                exc.model_name,
                summary,
                phase=exc.phase,
                body=None,
                provider_error_code=provider_code,
                provider_error_type=safe_type,
                retry_after=retry_after,
                in_stream=in_stream,
            )
        return type(exc)(
            exc.model_name,
            summary,
            body=None,
            provider_error_code=provider_code,
            provider_error_type=safe_type,
            retry_after=retry_after,
            in_stream=in_stream,
        )
    return RuntimeError(summary)


def _validate_call_site(call_site: str) -> str:
    if not isinstance(call_site, str) or not _CALL_SITE_RE.fullmatch(call_site):
        raise ValueError("call_site must match ^[a-z][a-z0-9_.-]{1,63}$")
    return call_site


def _sanitize_code_path(filename: str) -> str:
    norm = filename.replace("\\", "/")
    if "/src/" in norm:
        return "src/" + norm.rsplit("/src/", 1)[1]
    if "/tests/" in norm:
        return "tests/" + norm.rsplit("/tests/", 1)[1]
    parts = [p for p in norm.split("/") if p]
    if norm.startswith("/") and len(parts) > 2:
        return "/".join(parts[-2:])
    return "/".join(parts[-2:]) if len(parts) >= 2 else (norm or "unknown")


def _sanitize_fallback_site(module_name: str, func_name: str) -> str:
    mod_leaf = (module_name or "").rsplit(".", 1)[-1].lower()
    raw_fn = (func_name or "call").strip("<>").lower()
    mod_clean = re.sub(r"[^a-z0-9_.-]+", "_", mod_leaf).strip("_.") or "app"
    fn_clean = re.sub(r"[^a-z0-9_.-]+", "_", raw_fn).strip("_.") or "call"
    site = f"{mod_clean}.{fn_clean}"
    if not site[0].isalpha():
        site = f"c_{site}"
    return site[:64].rstrip("_.-") or "unknown.call"


def _caller_frame_info() -> tuple[dict[str, str | int], str]:
    frame = inspect.currentframe()
    try:
        cur = frame.f_back if frame is not None else None
        while cur is not None:
            mod = str(cur.f_globals.get("__name__", ""))
            if not mod.startswith(_FRAME_SKIP_PREFIXES):
                clean_path = _sanitize_code_path(cur.f_code.co_filename)
                func_name = cur.f_code.co_name
                lineno = cur.f_lineno
                attrs: dict[str, str | int] = {
                    "code.file.path": clean_path,
                    "code.function.name": func_name,
                    "code.line.number": lineno,
                }
                return attrs, _sanitize_fallback_site(mod, func_name)
            cur = cur.f_back
        return (
            {
                "code.file.path": "unknown",
                "code.function.name": "unknown",
                "code.line.number": 0,
            },
            "unknown.call",
        )
    finally:
        del frame


def _build_llm_context_attrs(
    *,
    call_site: str | None = None,
    decoding_version: str | None = None,
    domain_run_id: str | None = None,
    prompt_hash: str | None = None,
    artifact_dependency_digest: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if call_site is not None:
        out["infra.llm.call_site"] = _validate_call_site(call_site)
    if decoding_version is not None:
        out["infra.llm.decoding_version"] = decoding_version
    if domain_run_id is not None:
        out["infra.llm.domain_run_id"] = domain_run_id
    if prompt_hash is not None:
        out["infra.llm.prompt_sha256"] = prompt_hash
    if artifact_dependency_digest is not None:
        out["infra.llm.artifact_dependency_digest"] = artifact_dependency_digest
    return out


@contextmanager
def llm_call_context(
    *,
    call_site: str | None = None,
    decoding_version: str | None = None,
    domain_run_id: str | None = None,
    prompt_sha256: str | None = None,
    artifact_dependency_digest: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Bind dotted LLM call attributes onto active spans, metrics, and JSON/OTLP logs."""
    updates = _build_llm_context_attrs(
        call_site=call_site,
        decoding_version=decoding_version,
        domain_run_id=domain_run_id,
        prompt_hash=prompt_sha256,
        artifact_dependency_digest=artifact_dependency_digest,
    )
    prev = dict(_LLM_CONTEXT.get() or {})
    merged = {**prev, **updates}
    if "infra.llm.call_site" in updates:
        merged["_infra2_ctx_call_site"] = updates["infra.llm.call_site"]
    if decoding_version is not None:
        merged["_infra2_ctx_decoding_version"] = decoding_version
    code_attrs, _ = _caller_frame_info()
    public = {k: v for k, v in merged.items() if not k.startswith("_infra2_")}
    log_fields = {**code_attrs, **public}
    tok = _LLM_CONTEXT.set(merged)
    try:
        with _bind_log_context(log_fields):
            yield public
    finally:
        _reset_llm_context(tok, prev)


def _stamp_current_span(attrs: Mapping[str, Any], *, agent_call_site: str | None = None) -> None:
    otel_trace = sys.modules.get("opentelemetry.trace")
    if otel_trace is None:
        try:
            import opentelemetry.trace as otel_trace  # type: ignore[no-redef]
        except ImportError:
            return
    span = otel_trace.get_current_span()
    if span is not None and span.is_recording():
        span.set_attributes(dict(attrs))
        if agent_call_site and getattr(span, "name", "").startswith("invoke_agent"):
            span.update_name(f"invoke_agent {agent_call_site}")


def _record_llm_duration(
    meter_provider: Any | None,
    duration_seconds: float,
    attrs: Mapping[str, str],
) -> None:
    otel_metrics = sys.modules.get("opentelemetry.metrics")
    if otel_metrics is None:
        try:
            import opentelemetry.metrics as otel_metrics  # type: ignore[no-redef]
        except ImportError:
            return
    provider = meter_provider or otel_metrics.get_meter_provider()
    hist = provider.get_meter("infra2_sdk.runtime.llm").create_histogram(
        "gen_ai.client.operation.duration",
        unit="s",
        description="Duration of LLM client operations",
    )
    hist.record(duration_seconds, attributes=dict(attrs))


def _resolve_call_attrs(
    explicit_call_site: str | None,
    explicit_decoding_version: str | None,
) -> tuple[dict[str, Any], str, str, dict[str, str | int], bool]:
    ctx = dict(_LLM_CONTEXT.get() or {})
    if "code.file.path" in ctx and "_infra2_fallback_site" in ctx:
        code_attrs: dict[str, str | int] = {
            "code.file.path": ctx["code.file.path"],
            "code.function.name": ctx["code.function.name"],
            "code.line.number": ctx["code.line.number"],
        }
        fallback_site = str(ctx["_infra2_fallback_site"])
    else:
        code_attrs, fallback_site = _caller_frame_info()
    resolved_site = explicit_call_site or ctx.get("infra.llm.call_site")
    missing_site = resolved_site is None
    if missing_site:
        resolved_site = fallback_site
    dec_ver = explicit_decoding_version or ctx.get("infra.llm.decoding_version") or "default"
    attrs: dict[str, Any] = {
        **code_attrs,
        "infra.llm.call_site": str(resolved_site),
        "infra.llm.decoding_version": str(dec_ver),
    }
    for extra_key in (
        "infra.llm.domain_run_id",
        "infra.llm.prompt_sha256",
        "infra.llm.artifact_dependency_digest",
    ):
        if extra_key in ctx:
            attrs[extra_key] = ctx[extra_key]
    should_warn = missing_site and not bool(ctx.get("_infra2_warned_missing_site"))
    return attrs, str(resolved_site), str(dec_ver), code_attrs, should_warn


def _guarded_model_class() -> type[Any]:
    global _GUARDED_MODEL_CLS
    if _GUARDED_MODEL_CLS is not None:
        return _GUARDED_MODEL_CLS
    wrapper_mod = _require("pydantic_ai.models.wrapper", extra="pydantic-ai")
    pa_exc = _require("pydantic_ai.exceptions", extra="pydantic-ai")

    class _GuardedLlmModel(wrapper_mod.WrapperModel, GuardedLlmModel):  # type: ignore[misc]
        def __new__(cls, *_args: Any, **_kwargs: Any) -> Any:
            return object.__new__(cls)

        def __init__(
            self,
            wrapped: Any,
            *,
            call_site: str | None = None,
            decoding_version: str | None = None,
            meter_provider: Any | None = None,
        ) -> None:
            if call_site is not None:
                _validate_call_site(call_site)
            super().__init__(wrapped)
            self._call_site = call_site
            self._decoding_version = decoding_version
            self._meter_provider = meter_provider

        def _finish_metric(
            self,
            started: float,
            resolved_site: str,
            resolved_decoding: str,
            response_model: str,
            error_type: str | None,
        ) -> None:
            metric_attrs: dict[str, str] = {
                "gen_ai.provider.name": str(self.wrapped.system),
                "gen_ai.request.model": str(self.wrapped.model_name),
                "gen_ai.response.model": response_model,
                "infra.llm.call_site": resolved_site,
                "infra.llm.decoding_version": resolved_decoding,
            }
            if error_type is not None:
                metric_attrs["error.type"] = error_type
            _record_llm_duration(
                self._meter_provider,
                max(time.perf_counter() - started, 0.0),
                metric_attrs,
            )

        @asynccontextmanager
        async def _guarded_span(self, model_box: list[str]) -> AsyncIterator[None]:
            attrs, site, dec_ver, code_attrs, should_warn = _resolve_call_attrs(
                self._call_site, self._decoding_version
            )
            _stamp_current_span(attrs)
            started = time.perf_counter()
            error_type: str | None = None
            with _bind_log_context(attrs):
                if should_warn:
                    _LOGGER.warning(
                        "LLM call missing explicit call_site at %s:%s; falling back to %s",
                        code_attrs["code.file.path"],
                        code_attrs["code.line.number"],
                        site,
                    )
                try:
                    yield
                except (pa_exc.UsageLimitExceeded, pa_exc.UserError) as exc:
                    error_type = type(exc).__name__
                    raise
                except Exception as exc:
                    if _is_passthrough_local_error(exc):
                        error_type = type(exc).__name__
                        raise
                    scrubbed = redact_llm_error(exc)
                    error_type = type(scrubbed).__name__.removeprefix("_HTTP")
                    raise scrubbed from None
                finally:
                    self._finish_metric(started, site, dec_ver, model_box[0], error_type)

        async def request(
            self,
            messages: list[Any],
            model_settings: Any | None,
            model_request_parameters: Any,
        ) -> Any:
            model_box = [str(self.wrapped.model_name)]
            async with self._guarded_span(model_box):
                response = await super().request(messages, model_settings, model_request_parameters)
                model_box[0] = str(getattr(response, "model_name", None) or model_box[0])
                return response

        @asynccontextmanager
        async def request_stream(
            self,
            messages: list[Any],
            model_settings: Any | None,
            model_request_parameters: Any,
            run_context: Any | None = None,
        ) -> AsyncIterator[Any]:
            model_box = [str(self.wrapped.model_name)]
            async with (
                self._guarded_span(model_box),
                super().request_stream(
                    messages, model_settings, model_request_parameters, run_context
                ) as stream,
            ):
                try:
                    yield stream
                finally:
                    model_box[0] = str(getattr(stream, "model_name", None) or model_box[0])

    _GuardedLlmModel.__name__ = "GuardedLlmModel"
    _GuardedLlmModel.__qualname__ = "GuardedLlmModel"
    _GUARDED_MODEL_CLS = _GuardedLlmModel
    return _GUARDED_MODEL_CLS


class GuardedLlmModel:
    """WrapperModel enforcing call-site telemetry, log context binding, and error redaction."""

    def __new__(
        cls,
        wrapped: Any,
        *,
        call_site: str | None = None,
        decoding_version: str | None = None,
        meter_provider: Any | None = None,
    ) -> Any:
        impl = _guarded_model_class()
        return impl(
            wrapped,
            call_site=call_site,
            decoding_version=decoding_version,
            meter_provider=meter_provider,
        )


def _is_passthrough_local_error(exc: Exception) -> bool:
    if type(exc).__module__.startswith("vcr."):
        return True
    return isinstance(exc, RuntimeError) and "model requests are not allowed" in str(exc).lower()


def _disable_banner() -> Any:
    pydantic_ai = _require("pydantic_ai", extra="pydantic-ai")
    pydantic_ai.BANNER_ENABLED = False
    return pydantic_ai


def _require_api_key(settings: LlmSettings) -> str:
    if not settings.api_key or not settings.api_key.strip():
        raise ValueError(f"LlmSettings.api_key is required for provider {settings.provider!r}")
    return settings.api_key


def _no_stream_usage_model(
    base_cls: type[Any], omit_sentinel: Any, *args: Any, **kwargs: Any
) -> Any:
    class _NoUsageModel(base_cls):  # type: ignore[misc]
        def _get_stream_options(self, _ms: Any) -> Any:
            return omit_sentinel

    return _NoUsageModel(*args, **kwargs)


def _build_provider_model(
    settings: LlmSettings,
    model_name: str,
    model_settings: Mapping[str, Any] | None,
    *,
    include_stream_usage: bool,
) -> Any:
    api_key = _require_api_key(settings)
    prov = settings.provider.strip().lower()
    raw_ms = dict(model_settings) if model_settings else None

    if prov == "zai":
        openai_mod = _require("openai", extra="pydantic-ai")
        zai_mod = _require("pydantic_ai.models.zai", extra="pydantic-ai")
        zai_prov = _require("pydantic_ai.providers.zai", extra="pydantic-ai")
        client = openai_mod.AsyncOpenAI(api_key=api_key, base_url=settings.base_url, max_retries=0)
        provider = zai_prov.ZaiProvider(openai_client=client)
        if include_stream_usage:
            return zai_mod.ZaiModel(model_name, provider=provider, settings=raw_ms)
        return _no_stream_usage_model(
            zai_mod.ZaiModel, openai_mod.omit, model_name, provider=provider, settings=raw_ms
        )

    if prov in {"openai", "openai-compatible"}:
        openai_mod = _require("openai", extra="pydantic-ai")
        oai_mod = _require("pydantic_ai.models.openai", extra="pydantic-ai")
        oai_prov = _require("pydantic_ai.providers.openai", extra="pydantic-ai")
        eff_base = (
            settings.base_url
            if (prov == "openai-compatible" or settings.base_url != _DEFAULT_ZAI_BASE_URL)
            else None
        )
        client = openai_mod.AsyncOpenAI(api_key=api_key, base_url=eff_base, max_retries=0)
        provider = oai_prov.OpenAIProvider(openai_client=client)
        if include_stream_usage:
            return oai_mod.OpenAIChatModel(model_name, provider=provider, settings=raw_ms)
        return _no_stream_usage_model(
            oai_mod.OpenAIChatModel,
            openai_mod.omit,
            model_name,
            provider=provider,
            settings=raw_ms,
        )

    if prov == "openrouter":
        openai_mod = _require("openai", extra="pydantic-ai")
        or_mod = _require("pydantic_ai.models.openrouter", extra="pydantic-ai")
        or_prov = _require("pydantic_ai.providers.openrouter", extra="pydantic-ai")
        eff_base = (
            settings.base_url
            if settings.base_url != _DEFAULT_ZAI_BASE_URL
            else _DEFAULT_OPENROUTER_BASE_URL
        )
        client = openai_mod.AsyncOpenAI(api_key=api_key, base_url=eff_base, max_retries=0)
        provider = or_prov.OpenRouterProvider(openai_client=client)
        if include_stream_usage:
            return or_mod.OpenRouterModel(model_name, provider=provider, settings=raw_ms)
        return _no_stream_usage_model(
            or_mod.OpenRouterModel,
            openai_mod.omit,
            model_name,
            provider=provider,
            settings=raw_ms,
        )

    if prov == "anthropic":
        anth_mod = _require("anthropic", extra="pydantic-ai")
        pa_anth = _require("pydantic_ai.models.anthropic", extra="pydantic-ai")
        pa_anth_prov = _require("pydantic_ai.providers.anthropic", extra="pydantic-ai")
        eff_base = settings.base_url if settings.base_url != _DEFAULT_ZAI_BASE_URL else None
        client = anth_mod.AsyncAnthropic(api_key=api_key, base_url=eff_base, max_retries=0)
        provider = pa_anth_prov.AnthropicProvider(anthropic_client=client)
        return pa_anth.AnthropicModel(model_name, provider=provider, settings=raw_ms)

    if prov in {"google", "gemini"}:
        genai_types = _require("google.genai.types", extra="pydantic-ai")
        pa_google = _require("pydantic_ai.models.google", extra="pydantic-ai")
        pa_google_prov = _require("pydantic_ai.providers.google", extra="pydantic-ai")
        eff_base = settings.base_url if settings.base_url != _DEFAULT_ZAI_BASE_URL else None
        provider = pa_google_prov.GoogleProvider(
            api_key=api_key,
            base_url=eff_base,
            retry_options=genai_types.HttpRetryOptions(attempts=1),
        )
        return pa_google.GoogleModel(model_name, provider=provider, settings=raw_ms)

    raise ValueError(f"unsupported LLM provider: {settings.provider!r}")


def _resolve_instrumentation_settings(
    *,
    tracer_provider: Any | None,
    meter_provider: Any | None,
) -> Any:
    inst_mod = _require("pydantic_ai.models.instrumented", extra="pydantic-ai")
    return inst_mod.InstrumentationSettings(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        include_content=False,
        include_binary_content=False,
        include_model_request_parameters=False,
        use_aggregated_usage_attribute_names=True,
    )


def _strip_instrumented(model: Any, inst_mod: Any, fb_mod: Any) -> tuple[Any, Any | None]:
    if isinstance(model, inst_mod.InstrumentedModel):
        inner, inner_is = _strip_instrumented(model.wrapped, inst_mod, fb_mod)
        return inner, model.instrumentation_settings or inner_is
    if isinstance(model, fb_mod.FallbackModel):
        found_is = None
        new_models: list[Any] = []
        changed = False
        for child in model.models:
            stripped, child_is = _strip_instrumented(child, inst_mod, fb_mod)
            if stripped is not child:
                changed = True
            if found_is is None and child_is is not None:
                found_is = child_is
            new_models.append(stripped)
        if not changed:
            return model, found_is
        cloned_fb = copy.copy(model)
        cloned_fb.models = new_models
        return cloned_fb, found_is
    if hasattr(model, "wrapped"):
        inner, inner_is = _strip_instrumented(model.wrapped, inst_mod, fb_mod)
        if inner is model.wrapped:
            return model, inner_is
        cloned = copy.copy(model)
        cloned.wrapped = inner
        return cloned, inner_is
    return model, None


def _ensure_guarded(
    model: Any,
    fb_mod: Any,
    *,
    call_site: str | None,
    decoding_version: str | None,
    meter_provider: Any | None,
) -> Any:
    if isinstance(model, fb_mod.FallbackModel):
        new_children = [
            _ensure_guarded(
                child,
                fb_mod,
                call_site=call_site,
                decoding_version=decoding_version,
                meter_provider=meter_provider,
            )
            for child in model.models
        ]
        if all(a is b for a, b in zip(new_children, model.models, strict=True)):
            return model
        cloned_fb = copy.copy(model)
        cloned_fb.models = new_children
        return cloned_fb
    if isinstance(model, GuardedLlmModel):
        return model
    return GuardedLlmModel(
        model,
        call_site=call_site,
        decoding_version=decoding_version,
        meter_provider=meter_provider,
    )


def _wrap_guarded(
    settings: LlmSettings,
    spec: Any,
    *,
    role: _ModelRole,
    merged_settings: Mapping[str, Any] | None,
    call_site: str | None,
    decoding_version: str | None,
    include_stream_usage: bool,
    guard: bool,
    meter_provider: Any | None,
) -> tuple[Any, Any | None]:
    inst_mod = _require("pydantic_ai.models.instrumented", extra="pydantic-ai")
    fb_mod = _require("pydantic_ai.models.fallback", extra="pydantic-ai")
    chosen = settings.role_model(role) if spec is None else spec
    extracted_is = None
    if isinstance(chosen, str):
        base = _build_provider_model(
            settings, chosen, merged_settings, include_stream_usage=include_stream_usage
        )
    else:
        base, extracted_is = _strip_instrumented(chosen, inst_mod, fb_mod)
    if not guard:
        return base, extracted_is
    guarded = _ensure_guarded(
        base,
        fb_mod,
        call_site=call_site,
        decoding_version=decoding_version,
        meter_provider=meter_provider,
    )
    return guarded, extracted_is


def create_llm_model(
    settings: LlmSettings | None = None,
    *,
    role: _ModelRole = "small",
    model: Any | None = None,
    decoding: DecodingSpec | None = None,
    model_settings: Mapping[str, Any] | None = None,
    fallback_models: Sequence[Any] = (),
    call_site: str | None = None,
    decoding_version: str | None = None,
    include_stream_usage: bool = True,
    instrument: bool = True,
    guard: bool = True,
    tracer_provider: Any | None = None,
    meter_provider: Any | None = None,
) -> Any:
    """Build a guarded, OpenTelemetry-instrumented pydantic-ai Model with max_retries=0."""
    _disable_banner()
    if call_site is not None:
        _validate_call_site(call_site)
    cfg = settings or LlmSettings()
    merged = {
        **(decoding.to_model_settings() if decoding is not None else {}),
        **(dict(model_settings) if model_settings is not None else {}),
    } or None
    eff_dec = decoding_version or (decoding.decoding_version if decoding is not None else None)

    primary, extracted_is = _wrap_guarded(
        cfg,
        model,
        role=role,
        merged_settings=merged,
        call_site=call_site,
        decoding_version=eff_dec,
        include_stream_usage=include_stream_usage,
        guard=guard,
        meter_provider=meter_provider,
    )
    target = primary
    if fallback_models:
        fb_mod = _require("pydantic_ai.models.fallback", extra="pydantic-ai")
        fbs: list[Any] = []
        for fb in fallback_models:
            wrapped_fb, fb_is = _wrap_guarded(
                cfg,
                fb,
                role=role,
                merged_settings=merged,
                call_site=call_site,
                decoding_version=eff_dec,
                include_stream_usage=include_stream_usage,
                guard=guard,
                meter_provider=meter_provider,
            )
            if extracted_is is None and fb_is is not None:
                extracted_is = fb_is
            fbs.append(wrapped_fb)
        target = fb_mod.FallbackModel(primary, *fbs)

    inst_mod = _require("pydantic_ai.models.instrumented", extra="pydantic-ai")
    if instrument:
        eff_tp = tracer_provider or getattr(extracted_is, "tracer_provider", None)
        eff_mp = meter_provider or getattr(extracted_is, "meter_provider", None)
        isettings = _resolve_instrumentation_settings(
            tracer_provider=eff_tp,
            meter_provider=eff_mp,
        )
        return inst_mod.InstrumentedModel(target, isettings)
    return target


def create_llm_agent(
    settings: LlmSettings | None = None,
    *,
    role: _ModelRole = "small",
    model: Any | None = None,
    call_site: str | None = None,
    decoding: DecodingSpec | None = None,
    output_type: Any = str,
    instructions: Any = None,
    system_prompt: str | Sequence[str] = (),
    deps_type: Any = object,
    tools: Sequence[Any] = (),
    toolsets: Sequence[Any] | None = None,
    fallback_models: Sequence[Any] = (),
    usage_limits: Any | None = None,
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    retry_on: Any | None = None,
    include_stream_usage: bool = True,
    tracer_provider: Any | None = None,
    meter_provider: Any | None = None,
    model_settings: Mapping[str, Any] | None = None,
    capabilities: Sequence[Any] = (),
    end_strategy: Any = "graceful",
    retries: int = 1,
    metadata: Mapping[str, Any] | None = None,
) -> Any:
    """Build the Agent entrypoint with OTel instrumentation, usage limits, and retries."""
    _disable_banner()
    if call_site is not None:
        _validate_call_site(call_site)
    cfg = settings or LlmSettings()
    merged = {
        **(decoding.to_model_settings() if decoding is not None else {}),
        **(dict(model_settings) if model_settings is not None else {}),
    } or None
    eff_dec = decoding.decoding_version if decoding is not None else None

    inst_mod = _require("pydantic_ai.models.instrumented", extra="pydantic-ai")
    fb_mod = _require("pydantic_ai.models.fallback", extra="pydantic-ai")
    if model is not None and not isinstance(model, str):
        raw_model, extracted_is = _strip_instrumented(model, inst_mod, fb_mod)
    else:
        raw_model, extracted_is = model, None

    stripped_fbs: list[Any] = []
    for fb in fallback_models:
        if fb is not None and not isinstance(fb, str):
            stripped_fb, fb_is = _strip_instrumented(fb, inst_mod, fb_mod)
            if extracted_is is None and fb_is is not None:
                extracted_is = fb_is
            stripped_fbs.append(stripped_fb)
        else:
            stripped_fbs.append(fb)

    eff_tp = tracer_provider or getattr(extracted_is, "tracer_provider", None)
    eff_mp = meter_provider or getattr(extracted_is, "meter_provider", None)
    isettings = _resolve_instrumentation_settings(
        tracer_provider=eff_tp,
        meter_provider=eff_mp,
    )

    primary = create_llm_model(
        cfg,
        role=role,
        model=raw_model,
        decoding=decoding,
        model_settings=model_settings,
        call_site=call_site,
        decoding_version=eff_dec,
        include_stream_usage=include_stream_usage,
        instrument=False,
        guard=True,
        meter_provider=eff_mp,
    )

    agent_mod = _require("pydantic_ai", extra="pydantic-ai")
    cap_mod = _require("pydantic_ai.capabilities", extra="pydantic-ai")

    class _RunContextCapability(cap_mod.AbstractCapability):  # type: ignore[misc]
        async def wrap_run(self, _ctx: Any, *, handler: Any) -> Any:
            attrs, resolved_site, _dec, _code, _warn = _resolve_call_attrs(call_site, eff_dec)
            _stamp_current_span(attrs, agent_call_site=resolved_site)
            return await handler()

    caps: list[Any] = [cap_mod.Instrumentation(isettings), _RunContextCapability()]
    retry_kwargs: dict[str, Any] = {
        "max_attempts": max_attempts,
        "initial_delay": initial_delay,
        "max_delay": max_delay,
    }
    if retry_on is not None:
        retry_kwargs["retry_on"] = retry_on
    caps.append(cap_mod.RetryOnModelErrors(**retry_kwargs))

    if stripped_fbs:
        guarded_fbs = [
            _wrap_guarded(
                cfg,
                fb,
                role=role,
                merged_settings=merged,
                call_site=call_site,
                decoding_version=eff_dec,
                include_stream_usage=include_stream_usage,
                guard=True,
                meter_provider=eff_mp,
            )[0]
            for fb in stripped_fbs
        ]
        caps.append(cap_mod.ModelFallback(*guarded_fbs))
    caps.extend(capabilities)

    @dataclasses.dataclass(init=False)
    class _LlmAgent(agent_mod.Agent):  # type: ignore[misc]
        _default_usage_limits: Any = dataclasses.field(default=None, init=False, repr=False)
        _call_site: str | None = dataclasses.field(default=None, init=False, repr=False)
        _decoding_version: str | None = dataclasses.field(default=None, init=False, repr=False)

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            default_limits = kwargs.pop("_default_usage_limits", None)
            site = kwargs.pop("_call_site", None)
            dec_v = kwargs.pop("_decoding_version", None)
            super().__init__(*args, **kwargs)
            self._default_usage_limits = default_limits
            self._call_site = site
            self._decoding_version = dec_v

        @contextmanager
        def _bind_invocation(
            self, *, caller: tuple[dict[str, str | int], str] | None = None
        ) -> Iterator[None]:
            prev = dict(_LLM_CONTEXT.get() or {})
            if caller is None:
                pending = _PENDING_SYNC_CALLER.get()
                if pending is not None and pending[0] == id(self):
                    caller = pending[1]
                    _PENDING_SYNC_CALLER.set(None)
                elif _is_internal_agent_reentry(id(self), prev):
                    yield
                    return
            code_attrs, fallback_site = caller if caller is not None else _caller_frame_info()
            resolved_site = self._call_site or prev.get("_infra2_ctx_call_site")
            eff_site = str(resolved_site or fallback_site)
            eff_dec_val = str(
                self._decoding_version or prev.get("_infra2_ctx_decoding_version") or "default"
            )
            bound_ctx = {
                **prev,
                **code_attrs,
                "infra.llm.call_site": eff_site,
                "infra.llm.decoding_version": eff_dec_val,
                "_infra2_fallback_site": fallback_site,
                "_infra2_agent_id": id(self),
                "_infra2_warned_missing_site": resolved_site is None,
            }
            log_attrs = {
                **code_attrs,
                "infra.llm.call_site": eff_site,
                "infra.llm.decoding_version": eff_dec_val,
                **{
                    k: bound_ctx[k]
                    for k in (
                        "infra.llm.domain_run_id",
                        "infra.llm.prompt_sha256",
                        "infra.llm.artifact_dependency_digest",
                    )
                    if k in bound_ctx
                },
            }
            with _bind_log_context(log_attrs):
                if resolved_site is None:
                    _LOGGER.warning(
                        "LLM call missing explicit call_site at %s:%s; falling back to %s",
                        code_attrs["code.file.path"],
                        code_attrs["code.line.number"],
                        fallback_site,
                    )
                tok = _LLM_CONTEXT.set(bound_ctx)
                try:
                    yield
                finally:
                    _reset_llm_context(tok, prev)

        def run_sync(self, *args: Any, usage_limits: Any | None = None, **kwargs: Any) -> Any:
            eff_limits = self._default_usage_limits if usage_limits is None else usage_limits
            with self._bind_invocation():
                return super().run_sync(*args, usage_limits=eff_limits, **kwargs)

        async def run(self, *args: Any, usage_limits: Any | None = None, **kwargs: Any) -> Any:
            eff_limits = self._default_usage_limits if usage_limits is None else usage_limits
            with self._bind_invocation():
                return await super().run(*args, usage_limits=eff_limits, **kwargs)

        @asynccontextmanager
        async def iter(
            self, *args: Any, usage_limits: Any | None = None, **kwargs: Any
        ) -> AsyncIterator[Any]:
            eff_limits = self._default_usage_limits if usage_limits is None else usage_limits
            with self._bind_invocation():
                async with super().iter(*args, usage_limits=eff_limits, **kwargs) as agent_run:
                    yield agent_run

        @asynccontextmanager
        async def run_stream(
            self, *args: Any, usage_limits: Any | None = None, **kwargs: Any
        ) -> AsyncIterator[Any]:
            eff_limits = self._default_usage_limits if usage_limits is None else usage_limits
            with self._bind_invocation():
                async with super().run_stream(
                    *args, usage_limits=eff_limits, **kwargs
                ) as stream_res:
                    yield stream_res

        def run_stream_sync(
            self, *args: Any, usage_limits: Any | None = None, **kwargs: Any
        ) -> Any:
            eff_limits = self._default_usage_limits if usage_limits is None else usage_limits
            caller = _caller_frame_info()
            tok = _PENDING_SYNC_CALLER.set((id(self), caller))
            try:
                return super().run_stream_sync(*args, usage_limits=eff_limits, **kwargs)
            finally:
                try:
                    _PENDING_SYNC_CALLER.reset(tok)
                except ValueError:
                    _PENDING_SYNC_CALLER.set(None)

        def run_stream_events(
            self, *args: Any, usage_limits: Any | None = None, **kwargs: Any
        ) -> Any:
            eff_limits = self._default_usage_limits if usage_limits is None else usage_limits
            caller = _caller_frame_info()
            inner_cm = super().run_stream_events(*args, usage_limits=eff_limits, **kwargs)

            @asynccontextmanager
            async def _bound_events() -> AsyncIterator[Any]:
                with self._bind_invocation(caller=caller):
                    async with inner_cm as events:
                        yield events

            return _bound_events()

    return _LlmAgent(
        primary,
        output_type=output_type,
        instructions=instructions,
        system_prompt=system_prompt,
        deps_type=deps_type,
        name=call_site,
        model_settings=merged,
        retries=retries,
        tools=tools,
        toolsets=toolsets,
        metadata=dict(metadata) if metadata is not None else None,
        end_strategy=end_strategy,
        capabilities=caps,
        _default_usage_limits=usage_limits,
        _call_site=call_site,
        _decoding_version=eff_dec,
    )


def vcr_llm_config(
    *,
    cassette_library_dir: str | Path | None = None,
    record_mode: str | None = None,
    filter_headers: Sequence[str] = (),
    filter_query_parameters: Sequence[str] = (),
    decode_compressed_response: bool = True,
) -> dict[str, Any]:
    """Return a shared pytest-recording / vcrpy config with secret & URI redaction hooks."""
    seen_headers: dict[str, str] = {}
    for h in (*_DEFAULT_VCR_FILTER_HEADERS, *filter_headers):
        seen_headers.setdefault(h.lower(), h)

    seen_query: dict[str, str] = {}
    for q in (*_DEFAULT_VCR_FILTER_QUERY, *filter_query_parameters):
        seen_query.setdefault(q.lower(), q)

    def _before_record_request(request: Any) -> Any:
        if getattr(request, "uri", None):
            request.uri = redact_uri(str(request.uri))
        if getattr(request, "headers", None):
            request.headers = sanitize_headers(dict(request.headers))
        return request

    def _before_record_response(response: dict[str, Any]) -> dict[str, Any]:
        hdrs = response.get("headers")
        if isinstance(hdrs, Mapping):
            response["headers"] = {
                str(k): (
                    ["REDACTED"]
                    if isinstance(v, list) and _is_sensitive_header(str(k))
                    else ("REDACTED" if _is_sensitive_header(str(k)) else v)
                )
                for k, v in hdrs.items()
            }
        return response

    cfg: dict[str, Any] = {
        "filter_headers": [(h, "REDACTED") for h in seen_headers.values()],
        "filter_query_parameters": [(q, "REDACTED") for q in seen_query.values()],
        "decode_compressed_response": decode_compressed_response,
        "match_on": ("method", "scheme", "host", "port", "path", "query"),
        "before_record_request": _before_record_request,
        "before_record_response": _before_record_response,
    }
    if cassette_library_dir is not None:
        cfg["cassette_library_dir"] = str(cassette_library_dir)
    if record_mode is not None:
        cfg["record_mode"] = record_mode
    return cfg


@contextmanager
def allow_vcr_model_requests() -> Iterator[None]:
    """Temporarily allow model dispatch inside @pytest.mark.vcr while vcrpy blocks live sockets."""
    models_mod = _require("pydantic_ai.models", extra="pydantic-ai")
    with models_mod.override_allow_model_requests(True):
        yield


__all__ = [
    "DecodingSpec",
    "GuardedLlmModel",
    "LlmSettings",
    "allow_vcr_model_requests",
    "create_llm_agent",
    "create_llm_model",
    "llm_call_context",
    "prompt_sha256",
    "redact_llm_error",
    "redact_uri",
    "request_sha256",
    "sanitize_headers",
    "schema_sha256",
    "vcr_llm_config",
]
