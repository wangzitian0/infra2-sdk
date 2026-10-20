"""Large language model runtime configuration contracts."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

_ModelRole = Literal["large", "small", "multimodal"]


class _RoleDefault:
    """Marks a model slot that takes its value from a model role."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<role default>"


_ROLE_DEFAULT: Any = _RoleDefault()


def _coerce_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, Iterable):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


@dataclass(frozen=True)
class LlmSettings:
    """Settings contract for large language model providers.

    The SDK owns the default models through three roles. Each role is a model and
    a reasoning effort: ``large`` and ``small`` both use glm-5.3-flash, with high
    and low reasoning, and ``multimodal`` uses glm-5.3-flashx. The task slots
    derive from the roles when they are not set: ``primary_model`` uses the small
    model, and ``ocr_model`` and ``vision_model`` use the multimodal model. An
    explicit slot value, ``None`` included, still wins. ``dataclasses.replace``
    passes the resolved slots back in, so it does not re-derive them from a
    changed role; construct a new instance to re-derive.
    """

    provider: str = "zai"
    base_url: str = "https://api.z.ai/api/coding/paas/v4"
    api_key: str | None = field(default=None, repr=False)
    primary_model: str = _ROLE_DEFAULT
    ocr_model: str | None = _ROLE_DEFAULT
    vision_model: str | None = _ROLE_DEFAULT
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
    # The model roles are keyword-only (README: new runtime dataclass fields), so
    # positional construction of the fields above keeps its meaning.
    large_model: str = field(default="glm-5.3-flash", kw_only=True)
    small_model: str = field(default="glm-5.3-flash", kw_only=True)
    multimodal_model: str = field(default="glm-5.3-flashx", kw_only=True)
    large_reasoning_effort: str = field(default="high", kw_only=True)
    small_reasoning_effort: str = field(default="low", kw_only=True)
    multimodal_reasoning_effort: str = field(default="low", kw_only=True)

    def __post_init__(self) -> None:
        for role_field in (
            "large_model",
            "small_model",
            "multimodal_model",
            "large_reasoning_effort",
            "small_reasoning_effort",
            "multimodal_reasoning_effort",
        ):
            value = getattr(self, role_field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{role_field} must be non-empty")
        if self.primary_model is _ROLE_DEFAULT:
            object.__setattr__(self, "primary_model", self.small_model)
        if self.ocr_model is _ROLE_DEFAULT:
            object.__setattr__(self, "ocr_model", self.multimodal_model)
        if self.vision_model is _ROLE_DEFAULT:
            object.__setattr__(self, "vision_model", self.multimodal_model)
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

    def role_model(self, role: _ModelRole) -> str:
        """Return the model id of one role: ``large``, ``small`` or ``multimodal``."""
        models = {
            "large": self.large_model,
            "small": self.small_model,
            "multimodal": self.multimodal_model,
        }
        if role not in models:
            raise ValueError(f"unknown model role: {role!r}")
        return models[role]

    def role_reasoning_effort(self, role: _ModelRole) -> str:
        """Return the reasoning effort of one role: ``large``, ``small`` or ``multimodal``."""
        efforts = {
            "large": self.large_reasoning_effort,
            "small": self.small_reasoning_effort,
            "multimodal": self.multimodal_reasoning_effort,
        }
        if role not in efforts:
            raise ValueError(f"unknown model role: {role!r}")
        return efforts[role]

    def model_chain(self, *, vision: bool = False) -> tuple[str, ...]:
        if vision:
            initial = (self.vision_model or self.primary_model, *self.vision_fallback_models)
        else:
            initial = (self.primary_model, *self.fallback_models)
        return tuple(dict.fromkeys(initial))


__all__ = [
    "LlmSettings",
]
