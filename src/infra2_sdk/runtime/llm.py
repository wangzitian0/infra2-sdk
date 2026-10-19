"""Large language model runtime configuration contracts."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


def _coerce_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, Iterable):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


@dataclass(frozen=True)
class LlmSettings:
    """Settings contract for large language model providers."""

    provider: str = "zai"
    base_url: str = "https://api.z.ai/api/coding/paas/v4"
    api_key: str | None = field(default=None, repr=False)
    primary_model: str = "glm-5.3-flash"
    ocr_model: str | None = "glm-5.3-flashx"
    vision_model: str | None = "glm-5.3-flashx"
    fallback_models: tuple[str, ...] = ()
    vision_fallback_models: tuple[str, ...] = ()
    chat_completions_path: str = "/chat/completions"
    layout_parsing_path: str = "/layout_parsing"
    json_timeout_seconds: float = 360.0
    json_max_tokens: int = 8192
    json_disable_thinking: bool = True
    json_seed: int | None = None
    extract_max_attempts: int = 2
    encryption_keys: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fallback_models", _coerce_tuple(self.fallback_models))
        object.__setattr__(
            self,
            "vision_fallback_models",
            _coerce_tuple(self.vision_fallback_models),
        )
        object.__setattr__(self, "encryption_keys", _coerce_tuple(self.encryption_keys))

        if not self.provider.strip():
            raise ValueError("provider must be non-empty")
        if not self.primary_model.strip():
            raise ValueError("primary_model must be non-empty")

        base = urlsplit(self.base_url)
        if (
            base.scheme not in {"http", "https"}
            or not base.netloc
            or not base.hostname
            or any(char.isspace() for char in base.netloc)
        ):
            raise ValueError("base_url must use http:// or https:// with a host")
        if base.username is not None or base.password is not None:
            raise ValueError("base_url must not contain credentials")

        if not self.chat_completions_path.startswith("/"):
            raise ValueError("chat_completions_path must start with '/'")
        if not self.layout_parsing_path.startswith("/"):
            raise ValueError("layout_parsing_path must start with '/'")

        if self.json_timeout_seconds <= 0:
            raise ValueError("json_timeout_seconds must be positive")
        if self.json_max_tokens < 1:
            raise ValueError("json_max_tokens must be at least 1")
        if self.extract_max_attempts < 1:
            raise ValueError("extract_max_attempts must be at least 1")

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.chat_completions_path}"

    @property
    def layout_parsing_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.layout_parsing_path}"

    def model_chain(self, *, vision: bool = False) -> tuple[str, ...]:
        if vision:
            initial = (self.vision_model or self.primary_model, *self.vision_fallback_models)
        else:
            initial = (self.primary_model, *self.fallback_models)
        return tuple(dict.fromkeys(initial))


__all__ = [
    "LlmSettings",
]
