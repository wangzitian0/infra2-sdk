"""PydanticAI model factory, guarded wrapper, OTel 4-stream attribution, and VCR."""

from __future__ import annotations

import copy
import dataclasses
import logging
import os
import sys
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from infra2_sdk._core import (
    ContractModel,
    canonical_sha256,
    redact_url,
    require_optional,
    sanitize_headers,
    validate_http_url,
)
from infra2_sdk._llm_runtime import (
    DEFAULT_VCR_FILTER_HEADERS,
    DEFAULT_VCR_FILTER_QUERY,
    DEFAULT_ZAI_BASE_URL,
    LLM_CONTEXT,
    PENDING_SYNC_CALLER,
    ROLES,
    build_llm_context_attrs,
    build_provider_model,
    caller_frame_info,
    extract_provider_code,
    is_internal_agent_reentry,
    is_passthrough_local_error,
    normalize_hash_input,
    record_llm_duration,
    reset_llm_context,
    resolve_call_attrs,
    resolve_instrumentation_settings,
    safe_token,
    scrub_response_headers,
    stamp_current_span,
    strip_instrumented,
    validate_call_site,
)
from infra2_sdk.config import shared_env_contract
from infra2_sdk.telemetry import bind_log_context

_LOGGER = logging.getLogger("infra2_sdk.llm")

ModelRole = Literal["large", "small", "multimodal"]
ThinkingLevel = bool | Literal["minimal", "low", "medium", "high", "xhigh"]

_GUARDED_MODEL_CLS: type[Any] | None = None


class LlmSettings(ContractModel):
    """Typed LLM provider, role model names, and transport configuration."""

    provider: str = "zai"
    base_url: str = DEFAULT_ZAI_BASE_URL
    api_key: str | None = Field(default=None, repr=False)
    large_model: str = "glm-5.3-flash"
    small_model: str = "glm-5.3-flash"
    multimodal_model: str = "glm-5.3-flash"
    large_reasoning_effort: str = "high"
    small_reasoning_effort: str = "low"
    multimodal_reasoning_effort: str = "low"

    @model_validator(mode="after")
    def _validate_settings(self) -> LlmSettings:
        for role in ROLES:
            for suffix in ("model", "reasoning_effort"):
                rf = f"{role}_{suffix}"
                if not isinstance(v := getattr(self, rf), str) or not v.strip():
                    raise ValueError(f"{rf} must be non-empty")
        if not self.provider.strip():
            raise ValueError("provider must be non-empty")
        validate_http_url(self.base_url, label="base_url")
        return self

    def _role_attr(self, role: ModelRole, suffix: str) -> str:
        if role not in ROLES:
            raise ValueError(f"unknown model role: {role!r}")
        return getattr(self, f"{role}_{suffix}")

    def role_model(self, role: ModelRole) -> str:
        """Return configured model identifier for the given role."""
        return self._role_attr(role, "model")

    def role_reasoning_effort(self, role: ModelRole) -> str:
        """Return configured reasoning effort label for the given role."""
        return self._role_attr(role, "reasoning_effort")

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

    @model_validator(mode="after")
    def _validate_decoding(self) -> DecodingSpec:
        if not self.decoding_version.strip():
            raise ValueError("decoding_version must be non-empty")
        if self.max_tokens is not None and (
            type(self.max_tokens) is not int or self.max_tokens < 1
        ):
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
        return self

    def to_model_settings(self) -> dict[str, Any]:
        """Convert non-None fields into a PydanticAI ModelSettings dictionary."""
        keys = ("thinking", "temperature", "top_p", "seed", "max_tokens", "timeout")
        return {k: v for k in keys if (v := getattr(self, k)) is not None}


def prompt_sha256(prompt: Any) -> str:
    """Return canonical lowercase SHA-256 hex digest of a prompt or message sequence."""
    return canonical_sha256(normalize_hash_input(prompt))


def schema_sha256(schema: Any) -> str:
    """Return canonical lowercase SHA-256 hex digest of an output schema or Pydantic model."""
    if schema is None:
        raise ValueError("schema is required")
    return canonical_sha256(normalize_hash_input(schema))


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
            "prompt": normalize_hash_input(prompt),
            "schema": normalize_hash_input(schema),
            "decoding": normalize_hash_input(decoding),
            "extra": normalize_hash_input(extra or {}),
        }
    )


def redact_llm_error(exc: BaseException) -> Exception:
    """Scrub prompt and credential text while preserving PydanticAI ModelAPIError subclasses."""
    raw_status = getattr(exc, "status_code", None)
    status_code = raw_status if type(raw_status) is int else None
    provider_code = extract_provider_code(exc)
    cls_label = type(exc).__name__.removeprefix("_HTTP")
    summary = (
        f"{cls_label} (http={status_code if status_code is not None else 'none'}, "
        f"provider_code={provider_code or 'none'})"
    )
    pa_exc = sys.modules.get("pydantic_ai.exceptions")
    model_api_err = getattr(pa_exc, "ModelAPIError", None) if pa_exc is not None else None
    if model_api_err is not None and isinstance(exc, model_api_err):
        safe_type = safe_token(getattr(exc, "provider_error_type", None))
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
    updates = build_llm_context_attrs(
        call_site=call_site,
        decoding_version=decoding_version,
        domain_run_id=domain_run_id,
        prompt_hash=prompt_sha256,
        artifact_dependency_digest=artifact_dependency_digest,
    )
    prev = dict(LLM_CONTEXT.get() or {})
    merged = {**prev, **updates}
    if "infra.llm.call_site" in updates:
        merged["_infra2_ctx_call_site"] = updates["infra.llm.call_site"]
    if decoding_version is not None:
        merged["_infra2_ctx_decoding_version"] = decoding_version
    code_attrs, _ = caller_frame_info()
    public = {k: v for k, v in merged.items() if not k.startswith("_infra2_")}
    log_fields = {**code_attrs, **public}
    tok = LLM_CONTEXT.set(merged)
    try:
        with bind_log_context(**log_fields):
            yield public
    finally:
        reset_llm_context(tok, prev)


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
                validate_call_site(call_site)
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
            record_llm_duration(
                self._meter_provider,
                max(time.perf_counter() - started, 0.0),
                metric_attrs,
            )

        @asynccontextmanager
        async def _guarded_span(self, model_box: list[str]) -> AsyncIterator[None]:
            attrs, site, dec_ver, code_attrs, should_warn = resolve_call_attrs(
                self._call_site, self._decoding_version
            )
            stamp_current_span(attrs)
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
                except (pa_exc.UsageLimitExceeded, pa_exc.UserError) as exc:
                    error_type = type(exc).__name__
                    raise
                except Exception as exc:
                    if is_passthrough_local_error(exc):
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
        base = build_provider_model(
            provider_name=settings.provider,
            base_url=settings.base_url,
            api_key=settings.api_key,
            model_name=chosen,
            model_settings=merged_settings,
            include_stream_usage=include_stream_usage,
        )
    else:
        base, extracted_is = strip_instrumented(chosen, inst_mod, fb_mod)
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
        validate_call_site(call_site)
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
        fb_mod = require_optional("pydantic_ai.models.fallback", extra="llm")
        fbs = []
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

    inst_mod = require_optional("pydantic_ai.models.instrumented", extra="llm")
    if instrument:
        eff_tp = tracer_provider or getattr(extracted_is, "tracer_provider", None)
        eff_mp = meter_provider or getattr(extracted_is, "meter_provider", None)
        isettings = resolve_instrumentation_settings(
            tracer_provider=eff_tp,
            meter_provider=eff_mp,
        )
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
        validate_call_site(call_site)
    cfg = settings or LlmSettings()
    merged = {
        **(decoding.to_model_settings() if decoding is not None else {}),
        **(dict(model_settings) if model_settings is not None else {}),
    } or None
    eff_dec = decoding.decoding_version if decoding is not None else None

    inst_mod = require_optional("pydantic_ai.models.instrumented", extra="llm")
    fb_mod = require_optional("pydantic_ai.models.fallback", extra="llm")
    if model is not None and not isinstance(model, str):
        raw_model, extracted_is = strip_instrumented(model, inst_mod, fb_mod)
    else:
        raw_model, extracted_is = model, None

    stripped_fbs: list[Any] = []
    for fb in fallback_models:
        if fb is not None and not isinstance(fb, str):
            stripped_fb, fb_is = strip_instrumented(fb, inst_mod, fb_mod)
            if extracted_is is None and fb_is is not None:
                extracted_is = fb_is
            stripped_fbs.append(stripped_fb)
        else:
            stripped_fbs.append(fb)

    eff_tp = tracer_provider or getattr(extracted_is, "tracer_provider", None)
    eff_mp = meter_provider or getattr(extracted_is, "meter_provider", None)
    isettings = resolve_instrumentation_settings(
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

    agent_mod = require_optional("pydantic_ai", extra="llm")
    cap_mod = require_optional("pydantic_ai.capabilities", extra="llm")

    class _RunContextCapability(cap_mod.AbstractCapability):  # type: ignore[misc]
        async def wrap_run(self, _ctx: Any, *, handler: Any) -> Any:
            attrs, resolved_site, _dec, _code, _warn = resolve_call_attrs(call_site, eff_dec)
            stamp_current_span(attrs, agent_call_site=resolved_site)
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
            prev = dict(LLM_CONTEXT.get() or {})
            if caller is None:
                pending = PENDING_SYNC_CALLER.get()
                if pending is not None and pending[0] == id(self):
                    caller = pending[1]
                    PENDING_SYNC_CALLER.set(None)
                elif is_internal_agent_reentry(id(self), prev):
                    yield
                    return
            code_attrs, fallback_site = caller if caller is not None else caller_frame_info()
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
                tok = LLM_CONTEXT.set(bound_ctx)
                try:
                    yield
                finally:
                    reset_llm_context(tok, prev)

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
            caller = caller_frame_info()
            tok = PENDING_SYNC_CALLER.set((id(self), caller))
            try:
                return super().run_stream_sync(*args, usage_limits=eff_limits, **kwargs)
            finally:
                try:
                    PENDING_SYNC_CALLER.reset(tok)
                except ValueError:
                    PENDING_SYNC_CALLER.set(None)

        def run_stream_events(
            self, *args: Any, usage_limits: Any | None = None, **kwargs: Any
        ) -> Any:
            eff_limits = self._default_usage_limits if usage_limits is None else usage_limits
            caller = caller_frame_info()
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
    for h in (*DEFAULT_VCR_FILTER_HEADERS, *filter_headers):
        seen_headers.setdefault(h.lower(), h)

    seen_query: dict[str, str] = {}
    for q in (*DEFAULT_VCR_FILTER_QUERY, *filter_query_parameters):
        seen_query.setdefault(q.lower(), q)

    def _before_record_request(request: Any) -> Any:
        if getattr(request, "uri", None):
            request.uri = redact_url(str(request.uri))
        if getattr(request, "headers", None):
            request.headers = sanitize_headers(dict(request.headers))
        return request

    cfg: dict[str, Any] = {
        "filter_headers": [(h, "REDACTED") for h in seen_headers.values()],
        "filter_query_parameters": [(q, "REDACTED") for q in seen_query.values()],
        "decode_compressed_response": decode_compressed_response,
        "match_on": ("method", "scheme", "host", "port", "path", "query"),
        "before_record_request": _before_record_request,
        "before_record_response": scrub_response_headers,
    }
    if cassette_library_dir is not None:
        cfg["cassette_library_dir"] = str(cassette_library_dir)
    if record_mode is not None:
        cfg["record_mode"] = record_mode
    return cfg


@contextmanager
def _allow_vcr_cm() -> Iterator[None]:
    models_mod = require_optional("pydantic_ai.models", extra="llm")
    with models_mod.override_allow_model_requests(True):
        yield


def allow_vcr_model_requests() -> AbstractContextManager[None]:
    """Temporarily allow model dispatch inside @pytest.mark.vcr while vcrpy blocks sockets."""
    return _allow_vcr_cm()


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
