"""Large language model runtime configuration contracts."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from infra2_sdk._wire import validate_http_url

_ModelRole = Literal["large", "small", "multimodal"]
_ROLES: tuple[str, ...] = ("large", "small", "multimodal")


def _coerce_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, Iterable):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


@dataclass(frozen=True)
class LlmSettings:
    """Settings contract for large language model providers.

    The SDK defines default models through three roles: ``large``, ``small`` and
    ``multimodal``. Each role pairs ``glm-5.3-flash`` with a reasoning effort
    (``high``, ``low`` and ``low``). ``glm-5.3-flash`` accepts image, video and
    file input on the GLM Coding Plan; ``glm-5.3-flashx`` is not on the plan.
    """

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
    # The model roles are keyword-only (README: new runtime dataclass fields), so
    # positional construction of the fields above keeps its meaning.
    large_model: str = field(default="glm-5.3-flash", kw_only=True)
    small_model: str = field(default="glm-5.3-flash", kw_only=True)
    multimodal_model: str = field(default="glm-5.3-flash", kw_only=True)
    large_reasoning_effort: str = field(default="high", kw_only=True)
    small_reasoning_effort: str = field(default="low", kw_only=True)
    multimodal_reasoning_effort: str = field(default="low", kw_only=True)

    def __post_init__(self) -> None:
        for role in _ROLES:
            for suffix in ("model", "reasoning_effort"):
                role_field = f"{role}_{suffix}"
                value = getattr(self, role_field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{role_field} must be non-empty")
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
        """Return the model id of one role: ``large``, ``small`` or ``multimodal``."""
        return self._role_attr(role, "model")

    def role_reasoning_effort(self, role: _ModelRole) -> str:
        """Return the reasoning effort of one role: ``large``, ``small`` or ``multimodal``."""
        return self._role_attr(role, "reasoning_effort")


__all__ = [
    "LlmSettings",
]
