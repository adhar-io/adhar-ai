"""LLM backends. `load_provider` maps the `adhar-ai-llm` secret to one of them."""

from __future__ import annotations

from ...config import LLMConfig
from .base import LLMProvider, ProviderResult


def load_provider(cfg: LLMConfig) -> LLMProvider:
    if cfg.provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(cfg)
    if cfg.provider == "ollama":
        from .ollama import OllamaProvider

        return OllamaProvider(cfg)
    from .openai_compatible import AzureOpenAIProvider, OpenAICompatibleProvider

    if cfg.provider == "azure":
        return AzureOpenAIProvider(cfg)
    return OpenAICompatibleProvider(cfg)


__all__ = ["LLMProvider", "ProviderResult", "load_provider"]
