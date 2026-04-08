"""
Anthropic (Claude) AI provider.

Supported models: claude-opus-4-5, claude-sonnet-4-5, claude-haiku-4-5, etc.

Install:  pip install anthropic
Env vars: ANTHROPIC_API_KEY
          ANTHROPIC_MODEL  (optional, default: claude-opus-4-5)
"""
from __future__ import annotations
import os
import time
from typing import Optional

from ai.providers.base import BaseAIProvider
from utils.logger import get_logger

logger = get_logger(__name__)

try:
    import anthropic as _anthropic
    _AVAILABLE = True
except ImportError:
    _anthropic  = None   # type: ignore
    _AVAILABLE  = False


class AnthropicProvider(BaseAIProvider):
    name = "anthropic"

    def __init__(
        self,
        api_key:     Optional[str] = None,
        model:       Optional[str] = None,
        max_tokens:  int   = 512,
        temperature: float = 0.0,
        max_retries: int   = 3,
        retry_delay: float = 2.0,
    ):
        if not _AVAILABLE:
            raise ImportError(
                "anthropic package not installed. Run: pip install anthropic"
            )
        self._client     = _anthropic.Anthropic(
            api_key=api_key or os.getenv("ANTHROPIC_API_KEY", "")
        )
        self.model       = model or os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")
        self.max_tokens  = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        logger.info(f"[anthropic] Initialised — model={self.model}")

    def call(self, system: str, user: str) -> Optional[dict]:
        for attempt in range(self.max_retries):
            try:
                response = self._client.messages.create(
                    model      = self.model,
                    max_tokens = self.max_tokens,
                    temperature= self.temperature,
                    system     = system,
                    messages   = [{"role": "user", "content": user}],
                )
                raw    = response.content[0].text
                result = self.parse_json(raw)
                if result:
                    logger.debug(f"[anthropic] OK attempt={attempt+1}")
                    return result

            except _anthropic.APIError as e:
                logger.warning(f"[anthropic] API error (attempt {attempt+1}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (attempt + 1))
            except Exception as e:
                logger.error(f"[anthropic] Unexpected error: {e}")
                break

        logger.error("[anthropic] All retries exhausted")
        return None
