"""Provider selection."""

from __future__ import annotations

from ...config import settings
from .base import Provider, ProviderError

_cache: dict[str, Provider] = {}


def get_provider() -> Provider:
    key = f"{settings.provider}:{settings.model}"
    if key not in _cache:
        if settings.provider == "ollama":
            from .ollama import OllamaProvider

            _cache[key] = OllamaProvider(settings.model, num_ctx=settings.num_ctx)
        elif settings.provider == "anthropic":
            from .anthropic_provider import AnthropicProvider

            _cache[key] = AnthropicProvider(settings.model)
        else:
            raise ProviderError(
                f"Unknown provider '{settings.provider}'. Use 'ollama' or 'anthropic'."
            )
    return _cache[key]


__all__ = ["get_provider", "Provider", "ProviderError"]
