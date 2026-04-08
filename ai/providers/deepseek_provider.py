"""
DeepSeek AI provider.

DeepSeek exposes an OpenAI-compatible REST API, so we use the
`openai` SDK pointed at DeepSeek's base URL.

Supported models:
  deepseek-chat          (DeepSeek-V3)   — default, fast & cheap
  deepseek-reasoner      (DeepSeek-R1)   — slow chain-of-thought

Install:  pip install openai
Env vars: DEEPSEEK_API_KEY
          DEEPSEEK_MODEL   (optional, default: deepseek-chat)
          DEEPSEEK_BASE_URL (optional, default: https://api.deepseek.com/v1)
"""
from __future__ import annotations
import os
import time
from typing import Optional

from ai.providers.base import BaseAIProvider
from utils.logger import get_logger

logger = get_logger(__name__)

_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

try:
    from openai import OpenAI as _OpenAI, APIError as _APIError
    _AVAILABLE = True
except ImportError:
    _OpenAI   = None   # type: ignore
    _APIError = Exception
    _AVAILABLE = False


class DeepSeekProvider(BaseAIProvider):
    name = "deepseek"

    def __init__(
        self,
        api_key:     Optional[str] = None,
        model:       Optional[str] = None,
        base_url:    Optional[str] = None,
        max_tokens:  int   = 512,
        temperature: float = 0.0,
        max_retries: int   = 3,
        retry_delay: float = 2.0,
    ):
        if not _AVAILABLE:
            raise ImportError(
                "openai package not installed. Run: pip install openai"
            )
        self._client = _OpenAI(
            api_key  = api_key  or os.getenv("DEEPSEEK_API_KEY", ""),
            base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", _DEEPSEEK_BASE_URL),
        )
        self.model       = model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        self.max_tokens  = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        logger.info(f"[deepseek] Initialised — model={self.model}")

    def call(self, system: str, user: str) -> Optional[dict]:
        for attempt in range(self.max_retries):
            try:
                response = self._client.chat.completions.create(
                    model      = self.model,
                    max_tokens = self.max_tokens,
                    temperature= self.temperature,
                    messages   = [
                        {"role": "system",  "content": system},
                        {"role": "user",    "content": user},
                    ],
                )
                raw    = response.choices[0].message.content or ""
                result = self.parse_json(raw)
                if result:
                    logger.debug(f"[deepseek] OK attempt={attempt+1}")
                    return result

            except _APIError as e:
                logger.warning(f"[deepseek] API error (attempt {attempt+1}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (attempt + 1))
            except Exception as e:
                logger.error(f"[deepseek] Unexpected error: {e}")
                break

        logger.error("[deepseek] All retries exhausted")
        return None
