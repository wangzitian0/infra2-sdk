"""PydanticAI model factory, guarded wrapper, OTel 4-stream attribution, and VCR."""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import inspect
import json
import logging
import math
import os
import re
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal
from weakref import WeakKeyDictionary

from pydantic import Field, field_validator, model_validator

from infra2_sdk._core import (
    ContractModel,
    canonical_sha256,
    redact_secrets,
    redact_url,
    require_optional,
    sanitize_headers,
    validate_http_url,
)
from infra2_sdk.config import shared_env_contract
from infra2_sdk.telemetry import bind_log_context, get_meter

_LOGGER = logging.getLogger("infra2_sdk.llm")

ModelRole = Literal["large", "small", "multimodal"]
ThinkingLevel = bool | Literal["minimal", "low", "medium", "high", "xhigh"]

_DEFAULT_ZAI_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
_DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_ROLES = ("large", "small", "multimodal")

_CALL_SITE_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
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
_PASSTHROUGH_PA_EXC_NAMES = (
    "UsageLimitExceeded",
    "ConcurrencyLimitExceeded",
    "UserError",
    "UnexpectedModelBehavior",
    "RunCancelled",
    "SuspendedResponseExpired",
    "ModelRetry",
    "RetryModelRequest",
    "ApprovalRequired",
    "CallDeferred",
    "SkipModelRequest",
    "SkipToolExecution",
    "SkipToolValidation",
    "ToolFailed",
    "ToolFailedError",
    "ToolRetryError",
    "FallbackExceptionGroup",
)

_LLM_CONTEXT: ContextVar[Mapping[str, Any] | None] = ContextVar("infra2_llm_ctx", default=None)
_PENDING_SYNC_CALLER: ContextVar[tuple[int, tuple[dict[str, str | int], str]] | None] = ContextVar(
    "infra2_llm_pending_caller", default=None
)

_GUARDED_MODEL_CLS: type[Any] | None = None
_HIST_LOCK = threading.Lock()
_DURATION_HISTOGRAMS: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()


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


def _normalize_hash_input(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes_sha256__": hashlib.sha256(bytes(value)).hexdigest()}
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
            {"code.file.path": "unknown", "code.function.name": "unknown", "code.line.number": 0},
            "unknown.call",
        )
    finally:
        del frame


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
    with _HIST_LOCK:
        hist = _DURATION_HISTOGRAMS.get(provider)
        if hist is None:
            hist = get_meter("infra2_sdk.llm", meter_provider=provider).create_histogram(
                "gen_ai.client.operation.duration",
                unit="s",
                description="Duration of LLM client operations",
            )
            with contextlib.suppress(TypeError):
                _DURATION_HISTOGRAMS[provider] = hist
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


def _is_passthrough_exception(exc: Exception, pa_exc: Any) -> bool:
    if type(exc).__module__.startswith("vcr."):
        return True
    if isinstance(exc, RuntimeError) and "model requests are not allowed" in str(exc).lower():
        return True
    for name in _PASSTHROUGH_PA_EXC_NAMES:
        cls = getattr(pa_exc, name, None)
        if cls is not None and isinstance(exc, cls):
            return True
    return False


def _no_stream_usage_model(
    base_cls: type[Any], omit_sentinel: Any, *args: Any, **kwargs: Any
) -> Any:
    class _NoUsageModel(base_cls):  # type: ignore[misc]
        def _get_stream_options(self, _ms: Any) -> Any:
            return omit_sentinel

    return _NoUsageModel(*args, **kwargs)


def _build_provider_model(
    *,
    provider_name: str,
    base_url: str,
    api_key: str | None,
    model_name: str,
    model_settings: Mapping[str, Any] | None,
    include_stream_usage: bool,
) -> Any:
    if not api_key or not api_key.strip():
        raise ValueError(f"LlmSettings.api_key is required for provider {provider_name!r}")
    prov = provider_name.strip().lower()
    raw_ms = dict(model_settings) if model_settings else None
    openai_mod = require_optional("openai", extra="llm")

    if prov == "zai":
        zai_mod = require_optional("pydantic_ai.models.zai", extra="llm")
        zai_prov = require_optional("pydantic_ai.providers.zai", extra="llm")
        client = openai_mod.AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        provider: Any = zai_prov.ZaiProvider(openai_client=client)
        model_cls: Any = zai_mod.ZaiModel
    elif prov in {"openai", "openai-compatible"}:
        oai_mod = require_optional("pydantic_ai.models.openai", extra="llm")
        oai_prov = require_optional("pydantic_ai.providers.openai", extra="llm")
        eff_base = (
            base_url if (prov == "openai-compatible" or base_url != _DEFAULT_ZAI_BASE_URL) else None
        )
        client = openai_mod.AsyncOpenAI(api_key=api_key, base_url=eff_base, max_retries=0)
        provider = oai_prov.OpenAIProvider(openai_client=client)
        model_cls = oai_mod.OpenAIChatModel
    elif prov == "openrouter":
        or_mod = require_optional("pydantic_ai.models.openrouter", extra="llm")
        or_prov = require_optional("pydantic_ai.providers.openrouter", extra="llm")
        eff_base = base_url if base_url != _DEFAULT_ZAI_BASE_URL else _DEFAULT_OPENROUTER_BASE_URL
        client = openai_mod.AsyncOpenAI(api_key=api_key, base_url=eff_base, max_retries=0)
        provider = or_prov.OpenRouterProvider(openai_client=client)
        model_cls = or_mod.OpenRouterModel
    else:
        raise ValueError(f"unsupported LLM provider: {provider_name!r}")

    if include_stream_usage:
        return model_cls(model_name, provider=provider, settings=raw_ms)
    return _no_stream_usage_model(
        model_cls, openai_mod.omit, model_name, provider=provider, settings=raw_ms
    )


def _resolve_instrumentation_settings(
    *,
    tracer_provider: Any | None,
    meter_provider: Any | None,
) -> Any:
    inst_mod = require_optional("pydantic_ai.models.instrumented", extra="llm")
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


class LlmSettings(ContractModel):
    """Typed LLM provider, role model names, and transport configuration."""

    provider: str = "zai"
    base_url: str = _DEFAULT_ZAI_BASE_URL
    api_key: str | None = Field(default=None, repr=False)
    large_model: str = "glm-5.3-flash"
    small_model: str = "glm-5.3-flash"
    multimodal_model: str = "glm-5.3-flash"

    @model_validator(mode="after")
    def _validate_settings(self) -> LlmSettings:
        for role in _ROLES:
            rf = f"{role}_model"
            if not isinstance(v := getattr(self, rf), str) or not v.strip():
                raise ValueError(f"{rf} must be non-empty")
        if not self.provider.strip():
            raise ValueError("provider must be non-empty")
        validate_http_url(self.base_url, label="base_url")
        return self

    def role_model(self, role: ModelRole) -> str:
        """Return configured model identifier for the given role."""
        if role not in _ROLES:
            raise ValueError(f"unknown model role: {role!r}")
        return str(getattr(self, f"{role}_model"))

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> LlmSettings:
        """Build LlmSettings strictly from canonical shared_env.yaml LLM variables."""
        env = os.environ if environ is None else environ
        llm_names = {str(entry["name"]) for entry in shared_env_contract()["groups"]["llm"]}
        values: dict[str, Any] = {}
        for attr, key, sensitive in (
            ("provider", "LLM_PROVIDER", False),
            ("base_url", "LLM_BASE_URL", False),
            ("api_key", "LLM_API_KEY", True),
            ("large_model", "LLM_LARGE_MODEL", False),
            ("small_model", "LLM_SMALL_MODEL", False),
            ("multimodal_model", "LLM_MULTIMODAL_MODEL", False),
        ):
            if key not in llm_names:  # pragma: no cover
                raise KeyError(key)
            if attr in overrides and overrides[attr] is not None:
                continue
            raw = env.get(key)
            if isinstance(raw, str) and raw.strip():
                values[attr] = raw if sensitive else raw.strip()
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)


class DecodingSpec(ContractModel):
    """Versioned decoding parameters mapped onto PydanticAI ModelSettings."""

    decoding_version: str = "v1"
    thinking: ThinkingLevel | None = None
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None
    max_tokens: int | None = None
    timeout: float | None = None

    @field_validator("seed", "max_tokens", "temperature", "top_p", "timeout", mode="before")
    @classmethod
    def _reject_bool_numbers(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("boolean values are not valid numeric decoding settings")
        return value

    @model_validator(mode="after")
    def _validate_decoding(self) -> DecodingSpec:
        if not self.decoding_version.strip():
            raise ValueError("decoding_version must be non-empty")
        if self.max_tokens is not None and (
            type(self.max_tokens) is not int or self.max_tokens < 1
        ):
            raise ValueError("max_tokens must be at least 1")
        if self.seed is not None and type(self.seed) is not int:
            raise ValueError("seed must be an integer")
        if self.timeout is not None and (
            isinstance(self.timeout, bool)
            or not math.isfinite(float(self.timeout))
            or self.timeout <= 0
        ):
            raise ValueError("timeout must be finite and positive")
        if self.temperature is not None and (
            isinstance(self.temperature, bool)
            or not math.isfinite(float(self.temperature))
            or self.temperature < 0
        ):
            raise ValueError("temperature must be finite and non-negative")
        if self.top_p is not None and (
            isinstance(self.top_p, bool)
            or not math.isfinite(float(self.top_p))
            or not 0.0 <= float(self.top_p) <= 1.0
        ):
            raise ValueError("top_p must be between 0.0 and 1.0")
        return self

    def to_model_settings(self) -> dict[str, Any]:
        """Convert non-None fields into a PydanticAI ModelSettings dictionary."""
        keys = ("thinking", "temperature", "top_p", "seed", "max_tokens", "timeout")
        return {k: v for k in keys if (v := getattr(self, k)) is not None}


def prompt_sha256(prompt: Any) -> str:
    """Return canonical lowercase SHA-256 hex digest of a prompt or message sequence."""
    return canonical_sha256(_normalize_hash_input(prompt))


def schema_sha256(schema: Any) -> str:
    """Return canonical lowercase SHA-256 hex digest of an output schema or Pydantic model."""
    if schema is None:
        raise ValueError("schema is required")
    return canonical_sha256(_normalize_hash_input(schema))


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
    return canonical_sha256(
        {
            "model": model.strip(),
            "prompt": _normalize_hash_input(prompt),
            "schema": _normalize_hash_input(schema),
            "decoding": _normalize_hash_input(decoding),
            "extra": _normalize_hash_input(extra or {}),
        }
    )


def redact_llm_error(exc: BaseException) -> Exception:
    """Scrub prompt and credential text while preserving PydanticAI ModelAPIError subclasses."""
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


@contextmanager
def llm_call_context(
    *,
    call_site: str | None = None,
    decoding_version: str | None = None,
    domain_run_id: str | None = None,
    prompt_sha256: str | None = None,
    artifact_dependency_digest: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Bind dotted LLM call attributes across OTel spans, metrics, JSON logs, and OTLP logs."""
    updates: dict[str, Any] = {}
    if call_site is not None:
        updates["infra.llm.call_site"] = _validate_call_site(call_site)
    for attr_key, attr_val in (
        ("infra.llm.decoding_version", decoding_version),
        ("infra.llm.domain_run_id", domain_run_id),
        ("infra.llm.prompt_sha256", prompt_sha256),
        ("infra.llm.artifact_dependency_digest", artifact_dependency_digest),
    ):
        if attr_val is not None:
            updates[attr_key] = attr_val
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
        with bind_log_context(**log_fields):
            yield public
    finally:
        _reset_llm_context(tok, prev)


def _guarded_model_class() -> type[Any]:
    global _GUARDED_MODEL_CLS
    if _GUARDED_MODEL_CLS is not None:
        return _GUARDED_MODEL_CLS
    wrapper_mod = require_optional("pydantic_ai.models.wrapper", extra="llm")
    pa_exc = require_optional("pydantic_ai.exceptions", extra="llm")

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
        async def _guarded_span(
            self,
            model_box: list[str],
            caller_active: list[bool] | None = None,
        ) -> AsyncIterator[None]:
            attrs, site, dec_ver, code_attrs, should_warn = _resolve_call_attrs(
                self._call_site, self._decoding_version
            )
            _stamp_current_span(attrs)
            started = time.perf_counter()
            error_type: str | None = None
            with bind_log_context(**attrs):
                if should_warn:
                    _LOGGER.warning(
                        "LLM call missing explicit call_site at %s:%s; falling back to %s",
                        code_attrs["code.file.path"],
                        code_attrs["code.line.number"],
                        site,
                    )
                try:
                    yield
                except BaseException as exc:
                    if caller_active and caller_active[0]:
                        error_type = type(exc).__name__
                        raise
                    if not isinstance(exc, Exception):
                        error_type = type(exc).__name__
                        raise
                    if _is_passthrough_exception(exc, pa_exc):
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
            caller_active = [False]
            async with (
                self._guarded_span(model_box, caller_active),
                super().request_stream(
                    messages, model_settings, model_request_parameters, run_context
                ) as stream,
            ):
                caller_active[0] = True
                try:
                    yield stream
                    caller_active[0] = False
                finally:
                    model_box[0] = str(getattr(stream, "model_name", None) or model_box[0])

    _GuardedLlmModel.__name__ = "GuardedLlmModel"
    _GuardedLlmModel.__qualname__ = "GuardedLlmModel"
    _GUARDED_MODEL_CLS = _GuardedLlmModel
    return _GUARDED_MODEL_CLS


class GuardedLlmModel:
    """WrapperModel enforcing call-site OTel correlation, log binding, and error scrubbing."""

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


def _disable_banner() -> Any:
    pydantic_ai = require_optional("pydantic_ai", extra="llm")
    pydantic_ai.BANNER_ENABLED = False
    return pydantic_ai


def _ensure_guarded(
    model: Any,
    fb_mod: Any,
    *,
    call_site: str | None,
    decoding_version: str | None,
    meter_provider: Any | None,
) -> Any:
    if isinstance(model, fb_mod.FallbackModel):
        children = [
            _ensure_guarded(
                child,
                fb_mod,
                call_site=call_site,
                decoding_version=decoding_version,
                meter_provider=meter_provider,
            )
            for child in model.models
        ]
        if all(a is b for a, b in zip(children, model.models, strict=True)):
            return model
        cloned_fb = copy.copy(model)
        cloned_fb.models = children
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
    role: ModelRole,
    merged_settings: Mapping[str, Any] | None,
    call_site: str | None,
    decoding_version: str | None,
    include_stream_usage: bool,
    guard: bool,
    meter_provider: Any | None,
) -> tuple[Any, Any | None]:
    inst_mod = require_optional("pydantic_ai.models.instrumented", extra="llm")
    fb_mod = require_optional("pydantic_ai.models.fallback", extra="llm")
    chosen = settings.role_model(role) if spec is None else spec
    extracted_is = None
    if isinstance(chosen, str):
        base = _build_provider_model(
            provider_name=settings.provider,
            base_url=settings.base_url,
            api_key=settings.api_key,
            model_name=chosen,
            model_settings=merged_settings,
            include_stream_usage=include_stream_usage,
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


def _merge_model_settings(
    decoding: DecodingSpec | None,
    model_settings: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    return {
        **(decoding.to_model_settings() if decoding is not None else {}),
        **(dict(model_settings) if model_settings is not None else {}),
    } or None


def _wrap_model_suite(
    cfg: LlmSettings,
    *,
    role: ModelRole,
    model: Any | None,
    fallback_models: Sequence[Any],
    merged_settings: Mapping[str, Any] | None,
    call_site: str | None,
    decoding_version: str | None,
    include_stream_usage: bool,
    guard: bool,
    tracer_provider: Any | None,
    meter_provider: Any | None,
) -> tuple[Any, list[Any], Any]:
    primary, extracted_is = _wrap_guarded(
        cfg,
        model,
        role=role,
        merged_settings=merged_settings,
        call_site=call_site,
        decoding_version=decoding_version,
        include_stream_usage=include_stream_usage,
        guard=guard,
        meter_provider=meter_provider,
    )
    fbs: list[Any] = []
    for fb in fallback_models:
        wrapped_fb, fb_is = _wrap_guarded(
            cfg,
            fb,
            role=role,
            merged_settings=merged_settings,
            call_site=call_site,
            decoding_version=decoding_version,
            include_stream_usage=include_stream_usage,
            guard=guard,
            meter_provider=meter_provider,
        )
        if extracted_is is None and fb_is is not None:
            extracted_is = fb_is
        fbs.append(wrapped_fb)

    eff_tp = tracer_provider or getattr(extracted_is, "tracer_provider", None)
    eff_mp = meter_provider or getattr(extracted_is, "meter_provider", None)
    if guard and meter_provider is None and eff_mp is not None:
        for wrapped_m in (primary, *fbs):
            if hasattr(wrapped_m, "_meter_provider") and wrapped_m._meter_provider is None:
                wrapped_m._meter_provider = eff_mp
    isettings = _resolve_instrumentation_settings(tracer_provider=eff_tp, meter_provider=eff_mp)
    return primary, fbs, isettings


def create_llm_model(
    settings: LlmSettings | None = None,
    *,
    role: ModelRole = "small",
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
    """Build a guarded, OTel-instrumented PydanticAI Model with transport retries disabled."""
    _disable_banner()
    if call_site is not None:
        _validate_call_site(call_site)
    cfg = settings or LlmSettings()
    merged = _merge_model_settings(decoding, model_settings)
    eff_dec = decoding_version or (decoding.decoding_version if decoding is not None else None)
    primary, fbs, isettings = _wrap_model_suite(
        cfg,
        role=role,
        model=model,
        fallback_models=fallback_models,
        merged_settings=merged,
        call_site=call_site,
        decoding_version=eff_dec,
        include_stream_usage=include_stream_usage,
        guard=guard,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )
    target = primary
    if fbs:
        fb_mod = require_optional("pydantic_ai.models.fallback", extra="llm")
        target = fb_mod.FallbackModel(primary, *fbs)
    if instrument:
        inst_mod = require_optional("pydantic_ai.models.instrumented", extra="llm")
        return inst_mod.InstrumentedModel(target, isettings)
    return target


def create_llm_agent(
    settings: LlmSettings | None = None,
    *,
    role: ModelRole = "small",
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
    merged = _merge_model_settings(decoding, model_settings)
    eff_dec = decoding.decoding_version if decoding is not None else None
    primary, guarded_fbs, isettings = _wrap_model_suite(
        cfg,
        role=role,
        model=model,
        fallback_models=fallback_models,
        merged_settings=merged,
        call_site=call_site,
        decoding_version=eff_dec,
        include_stream_usage=include_stream_usage,
        guard=True,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )

    agent_mod = require_optional("pydantic_ai", extra="llm")
    cap_mod = require_optional("pydantic_ai.capabilities", extra="llm")

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

    if guarded_fbs:
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
            with bind_log_context(**log_attrs):
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


def _scrub_vcr_body(body: Any) -> Any:
    if isinstance(body, dict) and "string" in body and isinstance(body["string"], str):
        return {**body, "string": redact_secrets(body["string"], environ={})}
    if isinstance(body, str):
        return redact_secrets(body, environ={})
    return body


def vcr_llm_config(
    *,
    cassette_library_dir: str | Path | None = None,
    record_mode: str | None = None,
    filter_headers: Sequence[str] = (),
    filter_query_parameters: Sequence[str] = (),
    decode_compressed_response: bool = True,
) -> dict[str, Any]:
    """Return a shared pytest-recording / vcrpy config with secret & URI redaction hooks."""
    default_hdrs = (
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "x-goog-api-key",
        "cookie",
        "set-cookie",
    )
    default_qs = (
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
    seen_headers: dict[str, str] = {h.lower(): h for h in default_hdrs}
    for h in filter_headers:
        seen_headers.setdefault(h.lower(), h)

    seen_query: dict[str, str] = {q.lower(): q for q in default_qs}
    for q in filter_query_parameters:
        seen_query.setdefault(q.lower(), q)

    def _before_record_request(request: Any) -> Any:
        if getattr(request, "uri", None):
            request.uri = redact_url(str(request.uri))
        if getattr(request, "headers", None):
            request.headers = sanitize_headers(dict(request.headers))
        if getattr(request, "body", None) is not None:
            request.body = _scrub_vcr_body(request.body)
        return request

    def _before_record_response(response: dict[str, Any]) -> dict[str, Any]:
        hdrs = response.get("headers")
        if isinstance(hdrs, Mapping):
            response["headers"] = sanitize_headers(hdrs)
        if "body" in response:
            response["body"] = _scrub_vcr_body(response["body"])
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
    """Temporarily allow model dispatch inside @pytest.mark.vcr while vcrpy blocks sockets."""
    models_mod = require_optional("pydantic_ai.models", extra="llm")
    with models_mod.override_allow_model_requests(True):
        yield


__all__ = (
    "DecodingSpec",
    "GuardedLlmModel",
    "LlmSettings",
    "ModelRole",
    "ThinkingLevel",
    "allow_vcr_model_requests",
    "create_llm_agent",
    "create_llm_model",
    "llm_call_context",
    "prompt_sha256",
    "redact_llm_error",
    "request_sha256",
    "schema_sha256",
    "vcr_llm_config",
)
