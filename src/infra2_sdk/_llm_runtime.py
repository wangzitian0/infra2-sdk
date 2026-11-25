"""Internal PydanticAI provider, 4-stream OTel attribution, and VCR helpers."""

from __future__ import annotations

import copy
import dataclasses
import importlib
import inspect
import json
import re
import sys
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from infra2_sdk._core import is_sensitive_header, require_optional

DEFAULT_ZAI_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
ROLES = ("large", "small", "multimodal")

CALL_SITE_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

DEFAULT_VCR_FILTER_HEADERS = (
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "x-goog-api-key",
    "cookie",
    "set-cookie",
)
DEFAULT_VCR_FILTER_QUERY = (
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

LLM_CONTEXT: ContextVar[Mapping[str, Any] | None] = ContextVar("infra2_llm_ctx", default=None)
PENDING_SYNC_CALLER: ContextVar[tuple[int, tuple[dict[str, str | int], str]] | None] = ContextVar(
    "infra2_llm_pending_caller", default=None
)


def reset_llm_context(tok: Any, prev: Mapping[str, Any]) -> None:
    """Safely reset contextvar across async task boundaries."""
    try:
        LLM_CONTEXT.reset(tok)
    except ValueError:
        LLM_CONTEXT.set(dict(prev) if prev else None)


def is_internal_agent_reentry(agent_id: int, prev: Mapping[str, Any]) -> bool:
    """Return True when pydantic_ai re-enters the same Agent internally."""
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


def normalize_hash_input(value: Any) -> Any:
    """Convert models, dataclasses, sets, and paths into canonical JSON-hashable values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, type) and hasattr(value, "model_json_schema"):
        return normalize_hash_input(value.model_json_schema())
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return normalize_hash_input(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return normalize_hash_input(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(k): normalize_hash_input(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        normalized = [normalize_hash_input(x) for x in value]
        return sorted(normalized, key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=True))
    if isinstance(value, Sequence):
        return [normalize_hash_input(x) for x in value]
    return str(value)


def safe_token(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return text if SAFE_TOKEN_RE.fullmatch(text) else "redacted"


def extract_provider_code(exc: BaseException) -> str | None:
    direct = getattr(exc, "provider_error_code", None) or getattr(exc, "code", None)
    if direct is not None:
        return safe_token(direct)
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        if (code := body.get("code")) is not None:
            return safe_token(code)
        err = body.get("error")
        if isinstance(err, Mapping) and (code := err.get("code")) is not None:
            return safe_token(code)
    return None


def validate_call_site(call_site: str) -> str:
    if not isinstance(call_site, str) or not CALL_SITE_RE.fullmatch(call_site):
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


def caller_frame_info() -> tuple[dict[str, str | int], str]:
    """Locate caller file/function/line attributes and derive fallback call_site."""
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


def build_llm_context_attrs(
    *,
    call_site: str | None = None,
    decoding_version: str | None = None,
    domain_run_id: str | None = None,
    prompt_hash: str | None = None,
    artifact_dependency_digest: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if call_site is not None:
        out["infra.llm.call_site"] = validate_call_site(call_site)
    if decoding_version is not None:
        out["infra.llm.decoding_version"] = decoding_version
    if domain_run_id is not None:
        out["infra.llm.domain_run_id"] = domain_run_id
    if prompt_hash is not None:
        out["infra.llm.prompt_sha256"] = prompt_hash
    if artifact_dependency_digest is not None:
        out["infra.llm.artifact_dependency_digest"] = artifact_dependency_digest
    return out


def stamp_current_span(attrs: Mapping[str, Any], *, agent_call_site: str | None = None) -> None:
    """Attach dotted LLM attributes and normalized agent span name onto active OTel span."""
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


def record_llm_duration(
    meter_provider: Any | None,
    duration_seconds: float,
    attrs: Mapping[str, str],
) -> None:
    """Record gen_ai.client.operation.duration histogram sample."""
    otel_metrics = sys.modules.get("opentelemetry.metrics")
    if otel_metrics is None:
        try:
            import opentelemetry.metrics as otel_metrics  # type: ignore[no-redef]
        except ImportError:
            return
    provider = meter_provider or otel_metrics.get_meter_provider()
    hist = provider.get_meter("infra2_sdk.llm").create_histogram(
        "gen_ai.client.operation.duration",
        unit="s",
        description="Duration of LLM client operations",
    )
    hist.record(duration_seconds, attributes=dict(attrs))


def resolve_call_attrs(
    explicit_call_site: str | None,
    explicit_decoding_version: str | None,
) -> tuple[dict[str, Any], str, str, dict[str, str | int], bool]:
    ctx = dict(LLM_CONTEXT.get() or {})
    if "code.file.path" in ctx and "_infra2_fallback_site" in ctx:
        code_attrs: dict[str, str | int] = {
            "code.file.path": ctx["code.file.path"],
            "code.function.name": ctx["code.function.name"],
            "code.line.number": ctx["code.line.number"],
        }
        fallback_site = str(ctx["_infra2_fallback_site"])
    else:
        code_attrs, fallback_site = caller_frame_info()
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


def is_passthrough_local_error(exc: Exception) -> bool:
    if type(exc).__module__.startswith("vcr."):
        return True
    return isinstance(exc, RuntimeError) and "model requests are not allowed" in str(exc).lower()


def _require_provider_package(module: str, package_hint: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError:
        raise RuntimeError(f"{module} is required; install {package_hint}") from None


def _no_stream_usage_model(
    base_cls: type[Any], omit_sentinel: Any, *args: Any, **kwargs: Any
) -> Any:
    class _NoUsageModel(base_cls):  # type: ignore[misc]
        def _get_stream_options(self, _ms: Any) -> Any:
            return omit_sentinel

    return _NoUsageModel(*args, **kwargs)


def build_provider_model(
    *,
    provider_name: str,
    base_url: str,
    api_key: str | None,
    model_name: str,
    model_settings: Mapping[str, Any] | None,
    include_stream_usage: bool,
) -> Any:
    """Build a provider-native PydanticAI model with max_retries=0."""
    if not api_key or not api_key.strip():
        raise ValueError(f"LlmSettings.api_key is required for provider {provider_name!r}")
    prov = provider_name.strip().lower()
    raw_ms = dict(model_settings) if model_settings else None

    if prov == "zai":
        openai_mod = require_optional("openai", extra="llm")
        zai_mod = require_optional("pydantic_ai.models.zai", extra="llm")
        zai_prov = require_optional("pydantic_ai.providers.zai", extra="llm")
        client = openai_mod.AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        provider = zai_prov.ZaiProvider(openai_client=client)
        if include_stream_usage:
            return zai_mod.ZaiModel(model_name, provider=provider, settings=raw_ms)
        return _no_stream_usage_model(
            zai_mod.ZaiModel, openai_mod.omit, model_name, provider=provider, settings=raw_ms
        )

    if prov in {"openai", "openai-compatible"}:
        openai_mod = require_optional("openai", extra="llm")
        oai_mod = require_optional("pydantic_ai.models.openai", extra="llm")
        oai_prov = require_optional("pydantic_ai.providers.openai", extra="llm")
        eff_base = (
            base_url if (prov == "openai-compatible" or base_url != DEFAULT_ZAI_BASE_URL) else None
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
        openai_mod = require_optional("openai", extra="llm")
        or_mod = require_optional("pydantic_ai.models.openrouter", extra="llm")
        or_prov = require_optional("pydantic_ai.providers.openrouter", extra="llm")
        eff_base = base_url if base_url != DEFAULT_ZAI_BASE_URL else DEFAULT_OPENROUTER_BASE_URL
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
        anth_mod = _require_provider_package("anthropic", "pydantic-ai-slim[anthropic]")
        pa_anth = _require_provider_package(
            "pydantic_ai.models.anthropic", "pydantic-ai-slim[anthropic]"
        )
        pa_anth_prov = _require_provider_package(
            "pydantic_ai.providers.anthropic", "pydantic-ai-slim[anthropic]"
        )
        eff_base = base_url if base_url != DEFAULT_ZAI_BASE_URL else None
        client = anth_mod.AsyncAnthropic(api_key=api_key, base_url=eff_base, max_retries=0)
        provider = pa_anth_prov.AnthropicProvider(anthropic_client=client)
        return pa_anth.AnthropicModel(model_name, provider=provider, settings=raw_ms)

    if prov in {"google", "gemini"}:
        genai_types = _require_provider_package("google.genai.types", "pydantic-ai-slim[google]")
        pa_google = _require_provider_package(
            "pydantic_ai.models.google", "pydantic-ai-slim[google]"
        )
        pa_google_prov = _require_provider_package(
            "pydantic_ai.providers.google", "pydantic-ai-slim[google]"
        )
        eff_base = base_url if base_url != DEFAULT_ZAI_BASE_URL else None
        provider = pa_google_prov.GoogleProvider(
            api_key=api_key,
            base_url=eff_base,
            retry_options=genai_types.HttpRetryOptions(attempts=1),
        )
        return pa_google.GoogleModel(model_name, provider=provider, settings=raw_ms)

    raise ValueError(f"unsupported LLM provider: {provider_name!r}")


def resolve_instrumentation_settings(
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


def strip_instrumented(model: Any, inst_mod: Any, fb_mod: Any) -> tuple[Any, Any | None]:
    if isinstance(model, inst_mod.InstrumentedModel):
        inner, inner_is = strip_instrumented(model.wrapped, inst_mod, fb_mod)
        return inner, model.instrumentation_settings or inner_is
    if isinstance(model, fb_mod.FallbackModel):
        found_is = None
        new_models: list[Any] = []
        changed = False
        for child in model.models:
            stripped, child_is = strip_instrumented(child, inst_mod, fb_mod)
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
        inner, inner_is = strip_instrumented(model.wrapped, inst_mod, fb_mod)
        if inner is model.wrapped:
            return model, inner_is
        cloned = copy.copy(model)
        cloned.wrapped = inner
        return cloned, inner_is
    return model, None


def scrub_response_headers(response: dict[str, Any]) -> dict[str, Any]:
    """Scrub credential-bearing response headers in VCR cassettes."""
    hdrs = response.get("headers")
    if isinstance(hdrs, Mapping):
        response["headers"] = {
            str(k): (
                ["REDACTED"]
                if isinstance(v, list) and is_sensitive_header(str(k))
                else ("REDACTED" if is_sensitive_header(str(k)) else v)
            )
            for k, v in hdrs.items()
        }
    return response
