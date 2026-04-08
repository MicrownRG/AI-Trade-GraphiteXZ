"""
AI Provider Registry & Factory.

Reads AI_PROVIDER from environment (or .env) and returns the
correct provider instance.  Default: deepseek.

Supported values for AI_PROVIDER:
  deepseek    → DeepSeekProvider   (default)
  anthropic   → AnthropicProvider
  gemini      → GeminiProvider

Usage
─────
    from ai.providers.registry import get_provider

    provider = get_provider()          # uses AI_PROVIDER env var
    result   = provider.safe_call(system_prompt, user_prompt)

You can also force a specific provider:
    provider = get_provider("anthropic")
    provider = get_provider("gemini")

Configuration (in .env or environment)
───────────────────────────────────────
    # Which provider to use
    AI_PROVIDER=deepseek          # deepseek | anthropic | gemini

    # DeepSeek
    DEEPSEEK_API_KEY=sk-...
    DEEPSEEK_MODEL=deepseek-chat  # deepseek-chat | deepseek-reasoner

    # Anthropic
    ANTHROPIC_API_KEY=sk-ant-...
    ANTHROPIC_MODEL=claude-opus-4-5

    # Gemini
    GEMINI_API_KEY=AIza...
    GEMINI_MODEL=gemini-2.0-flash
"""
from __future__ import annotations
import os
from typing import Optional

from ai.providers.base import BaseAIProvider
from utils.logger import get_logger

logger = get_logger(__name__)

# ── Supported providers ────────────────────────────────────────────────────────
_PROVIDER_MAP: dict[str, str] = {
    "deepseek":  "ai.providers.deepseek_provider.DeepSeekProvider",
    "anthropic": "ai.providers.anthropic_provider.AnthropicProvider",
    "gemini":    "ai.providers.gemini_provider.GeminiProvider",
}

_DEFAULT_PROVIDER = "deepseek"

# Singleton cache — one instance per provider name
_instances: dict[str, BaseAIProvider] = {}


def get_provider(name: Optional[str] = None) -> BaseAIProvider:
    """
    Return a (cached) provider instance.

    Args:
        name: Provider name. If None, reads AI_PROVIDER env var.
              Falls back to "deepseek" if not set.

    Raises:
        ValueError:  unknown provider name
        ImportError: required SDK not installed
    """
    provider_name = (name or os.getenv("AI_PROVIDER", _DEFAULT_PROVIDER)).lower().strip()

    if provider_name not in _PROVIDER_MAP:
        available = ", ".join(_PROVIDER_MAP.keys())
        raise ValueError(
            f"Unknown AI provider: '{provider_name}'. "
            f"Available: {available}"
        )

    if provider_name in _instances:
        return _instances[provider_name]

    # Lazy import to avoid loading all SDKs on startup
    module_path, class_name = _PROVIDER_MAP[provider_name].rsplit(".", 1)
    import importlib
    module   = importlib.import_module(module_path)
    cls      = getattr(module, class_name)
    instance = cls()

    _instances[provider_name] = instance
    logger.info(f"AI provider loaded: {provider_name}")
    return instance


def get_active_provider_name() -> str:
    """Return the name of the currently configured provider."""
    return os.getenv("AI_PROVIDER", _DEFAULT_PROVIDER).lower().strip()


def list_providers() -> list[str]:
    """Return list of all supported provider names."""
    return list(_PROVIDER_MAP.keys())


def reset_instances() -> None:
    """Clear cached instances (useful for testing)."""
    _instances.clear()
