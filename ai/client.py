"""
Unified AI client — public interface for the rest of the system.

All code that previously imported `call_claude` can now import `call_ai`
(or keep using `call_claude` via the alias below — backwards-compatible).

Which provider is used is controlled by the AI_PROVIDER env var:
  AI_PROVIDER=deepseek     → DeepSeek   (default)
  AI_PROVIDER=anthropic    → Anthropic Claude
  AI_PROVIDER=gemini       → Google Gemini

See ai/providers/registry.py for full configuration reference.
"""
from __future__ import annotations
from typing import Optional

from ai.providers.registry import get_provider, get_active_provider_name
from utils.logger import get_logger

logger = get_logger(__name__)


def call_ai(system: str, user: str) -> Optional[dict]:
    """
    Call the active AI provider.

    Returns a validated dict:
        {
            "confidence": float,   # 0.0 - 1.0
            "decision":   str,     # "TAKE" | "SKIP"
            "reason":     str
        }
    or None on failure.
    """
    provider = get_provider()
    logger.debug(f"call_ai -> provider={get_active_provider_name()}")
    return provider.safe_call(system, user)


# Backwards-compatible alias — scorer.py still imports call_claude
call_claude = call_ai
