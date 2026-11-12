"""Large language model runtime configuration contracts and OpenAI-compatible client."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from infra2_sdk._wire import validate_http_url
from infra2_sdk.runtime.http import (
    HttpClientSettings,
    _build_client,
    arequest_with_retry,
    request_with_retry,
)

_ModelRole = Literal["large", "small", "multimodal"]
_ROLES = ("large", "small", "multimodal")
_JSON_FENCE_RE = re.compile(r"\A```(?:json)?\s*(.*?)\s*```\Z", re.S | re.I)


def _coerce_tuple(value: Any) -> tuple[str, ...]:
    items = (
        value.split(",")
        if isinstance(value, str)
        else (value if isinstance(value, Iterable) else ())
    )
    return tuple(s for item in items if (s := str(item).strip()))


@dataclass(frozen=True)
class LlmSettings:
    provider: str = "zai"
    base_url: str = "https://api.z.ai/api/coding/paas/v4"
    api_key: str | None = field(default=None, repr=False)
    chat_completions_path: str = "/chat/completions"
    json_timeout_seconds: float = 360.0
    json_max_tokens: int = 8192
    json_disable_thinking: bool = True
    json_seed: int | None = None
    extract_max_attempts: int = 2
    encryption_keys: tuple[str, ...] = field(default=(), repr=False)
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
        object.__setattr__(self, "encryption_keys", _coerce_tuple(self.encryption_keys))
        if not self.provider.strip():
            raise ValueError("provider must be non-empty")
        validate_http_url(self.base_url, label="base_url")
        if not self.chat_completions_path.startswith("/"):
            raise ValueError("chat_completions_path must start with '/'")
        if self.json_timeout_seconds <= 0:
            raise ValueError("json_timeout_seconds must be positive")
        if self.json_max_tokens < 1:
            raise ValueError("json_max_tokens must be at least 1")
        if self.extract_max_attempts < 1:
            raise ValueError("extract_max_attempts must be at least 1")

    def _role_attr(self, role: _ModelRole, suffix: str) -> str:
        if role not in _ROLES:
            raise ValueError(f"unknown model role: {role!r}")
        return getattr(self, f"{role}_{suffix}")

    def role_model(self, role: _ModelRole) -> str:
        return self._role_attr(role, "model")

    def role_reasoning_effort(self, role: _ModelRole) -> str:
        return self._role_attr(role, "reasoning_effort")


@dataclass(frozen=True)
class LlmResponse:
    content: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


def _extract_json(text: str) -> Any:
    cleaned = text.strip()
    if match := _JSON_FENCE_RE.match(cleaned):
        cleaned = match.group(1).strip()
    return json.loads(cleaned)


def _record_span_attrs(model: str, provider: str, in_tok: int, out_tok: int) -> None:
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover
        return
    span = trace.get_current_span()
    if span is not None and span.is_recording():
        for k, v in (
            ("gen_ai.system", provider),
            ("gen_ai.request.model", model),
            ("gen_ai.usage.input_tokens", in_tok),
            ("gen_ai.usage.output_tokens", out_tok),
        ):
            span.set_attribute(k, v)


def _parse_chat_payload(data: Any, fallback_model: str, provider: str) -> LlmResponse:
    choices = data.get("choices") if isinstance(data, Mapping) else None
    if not isinstance(choices, list) or not choices:
        raise ValueError("LLM response missing choices")
    content = (choices[0].get("message") or {}).get("content")
    text = (
        content if isinstance(content, str) else json.dumps(content) if content is not None else ""
    )
    usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
    in_tok, out_tok = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    model = str(data.get("model") or fallback_model)
    _record_span_attrs(model, provider, in_tok, out_tok)
    return LlmResponse(text, model, in_tok, out_tok, data)


def _parse_embeddings(payload: Any) -> list[list[float]]:
    rows = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("LLM embeddings response missing data")
    return [[float(x) for x in row["embedding"]] for row in rows]


class LlmClient:
    """OpenAI-compatible chat, structured JSON, and embeddings client."""

    def __init__(
        self,
        settings: LlmSettings | None = None,
        *,
        client: Any | None = None,
        async_client: Any | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        asleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.settings = settings or LlmSettings()
        self._client, self._async_client = client, async_client
        self._max_retries, self._sleep, self._asleep = max_retries, sleep, asleep

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.settings.api_key:
            h["Authorization"] = f"Bearer {self.settings.api_key}"
        return h

    def _url(self, path: str) -> str:
        return f"{self.settings.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _http_settings(self) -> HttpClientSettings:
        return HttpClientSettings(timeout_seconds=self.settings.json_timeout_seconds)

    def _chat_attempts(
        self,
        messages: Sequence[Mapping[str, Any]],
        role: _ModelRole,
        model: str | None,
        fallbacks: Sequence[str],
        json_mode: bool,
        max_tokens: int | None,
        extra_body: Mapping[str, Any] | None,
    ) -> list[tuple[str, dict[str, Any]]]:
        primary = model.strip() if model and model.strip() else self.settings.role_model(role)
        cands = list(dict.fromkeys(s for s in (primary, *(f.strip() for f in fallbacks)) if s))
        out: list[tuple[str, dict[str, Any]]] = []
        for cand in cands:
            body: dict[str, Any] = {
                "model": cand,
                "messages": [dict(m) for m in messages],
                "max_tokens": max_tokens or self.settings.json_max_tokens,
            }
            if json_mode:
                body["response_format"] = {"type": "json_object"}
                if self.settings.json_seed is not None:
                    body["seed"] = self.settings.json_seed
                if self.settings.json_disable_thinking and self.settings.provider == "zai":
                    body["thinking"] = {"type": "disabled"}
            else:
                body["reasoning_effort"] = self.settings.role_reasoning_effort(role)
            if extra_body:
                body.update(extra_body)
            out.append((cand, body))
        return out

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        role: _ModelRole = "small",
        model: str | None = None,
        fallback_models: Sequence[str] = (),
        json_mode: bool = False,
        max_tokens: int | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> LlmResponse:
        owned = self._client is None
        http = self._client or _build_client(self._http_settings(), headers=self._headers())
        url, hdrs, last_err = (
            self._url(self.settings.chat_completions_path),
            self._headers(),
            None,
        )
        try:
            for cand, body in self._chat_attempts(
                messages, role, model, fallback_models, json_mode, max_tokens, extra_body
            ):
                try:
                    r = request_with_retry(
                        http,
                        "POST",
                        url,
                        headers=hdrs,
                        json=body,
                        max_attempts=self._max_retries,
                        sleep=self._sleep,
                    )
                    if int(r.status_code) >= 400:
                        raise RuntimeError(f"LLM HTTP {r.status_code} for model {cand}")
                    return _parse_chat_payload(r.json(), cand, self.settings.provider)
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
            assert last_err is not None
            raise last_err
        finally:
            if owned:
                http.close()

    async def achat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        role: _ModelRole = "small",
        model: str | None = None,
        fallback_models: Sequence[str] = (),
        json_mode: bool = False,
        max_tokens: int | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> LlmResponse:
        owned = self._async_client is None
        http = self._async_client or _build_client(
            self._http_settings(), async_client=True, headers=self._headers()
        )
        url, hdrs, last_err = (
            self._url(self.settings.chat_completions_path),
            self._headers(),
            None,
        )
        try:
            for cand, body in self._chat_attempts(
                messages, role, model, fallback_models, json_mode, max_tokens, extra_body
            ):
                try:
                    r = await arequest_with_retry(
                        http,
                        "POST",
                        url,
                        headers=hdrs,
                        json=body,
                        max_attempts=self._max_retries,
                        asleep=self._asleep,
                    )
                    if int(r.status_code) >= 400:
                        raise RuntimeError(f"LLM HTTP {r.status_code} for model {cand}")
                    return _parse_chat_payload(r.json(), cand, self.settings.provider)
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
            assert last_err is not None
            raise last_err
        finally:
            if owned:
                await http.aclose()

    def chat_json(self, messages: Sequence[Mapping[str, Any]], **kwargs: Any) -> Any:
        last_err: Exception | None = None
        for _ in range(self.settings.extract_max_attempts):
            try:
                return _extract_json(self.chat(messages, **kwargs, json_mode=True).content)
            except ValueError as exc:
                last_err = exc
        assert last_err is not None
        raise last_err

    async def achat_json(self, messages: Sequence[Mapping[str, Any]], **kwargs: Any) -> Any:
        last_err: Exception | None = None
        for _ in range(self.settings.extract_max_attempts):
            try:
                return _extract_json((await self.achat(messages, **kwargs, json_mode=True)).content)
            except ValueError as exc:
                last_err = exc
        assert last_err is not None
        raise last_err

    def embed(
        self,
        inputs: str | Sequence[str],
        *,
        model: str = "embedding-3",
        embeddings_path: str = "/embeddings",
    ) -> list[list[float]]:
        owned = self._client is None
        http = self._client or _build_client(self._http_settings(), headers=self._headers())
        items = [inputs] if isinstance(inputs, str) else list(inputs)
        try:
            r = request_with_retry(
                http,
                "POST",
                self._url(embeddings_path),
                headers=self._headers(),
                json={"model": model, "input": items},
                max_attempts=self._max_retries,
                sleep=self._sleep,
            )
            if int(r.status_code) >= 400:
                raise RuntimeError(f"LLM embeddings HTTP {r.status_code}")
            return _parse_embeddings(r.json())
        finally:
            if owned:
                http.close()

    async def aembed(
        self,
        inputs: str | Sequence[str],
        *,
        model: str = "embedding-3",
        embeddings_path: str = "/embeddings",
    ) -> list[list[float]]:
        owned = self._async_client is None
        http = self._async_client or _build_client(
            self._http_settings(), async_client=True, headers=self._headers()
        )
        items = [inputs] if isinstance(inputs, str) else list(inputs)
        try:
            r = await arequest_with_retry(
                http,
                "POST",
                self._url(embeddings_path),
                headers=self._headers(),
                json={"model": model, "input": items},
                max_attempts=self._max_retries,
                asleep=self._asleep,
            )
            if int(r.status_code) >= 400:
                raise RuntimeError(f"LLM embeddings HTTP {r.status_code}")
            return _parse_embeddings(r.json())
        finally:
            if owned:
                await http.aclose()


__all__ = ["LlmClient", "LlmSettings"]
