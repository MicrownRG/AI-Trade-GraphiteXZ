"""
Google Gemini AI provider.

Uses the official `google-generativeai` SDK.

Supported models:
  gemini-2.0-flash          — fast, cheap, default
  gemini-2.0-flash-thinking — reasoning model (slower)
  gemini-1.5-pro            — high quality
  gemini-1.5-flash          — fast / balanced

Install:  pip install google-generativeai
Env vars: GEMINI_API_KEY
          GEMINI_MODEL  (optional, default: gemini-2.0-flash)

Note: Gemini does not accept a separate `system` role in the same way;
we prepend it as a user instruction in the first turn.
"""
from __future__ import annotations
import os
import time
from typing import Optional

from ai.providers.base import BaseAIProvider
from utils.logger import get_logger

logger = get_logger(__name__)

try:
    import google.generativeai as _genai
    from google.api_core.exceptions import GoogleAPIError as _GoogleAPIError
    _AVAILABLE = True
except ImportError:
    _genai          = None   # type: ignore
    _GoogleAPIError = Exception
    _AVAILABLE      = False


class GeminiProvider(BaseAIProvider):
    name = "gemini"

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
                "google-generativeai not installed. "
                "Run: pip install google-generativeai"
            )
        key = api_key or os.getenv("GEMINI_API_KEY", "")
        _genai.configure(api_key=key)

        self.model_name  = model or os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        self.max_tokens  = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        self._model = _genai.GenerativeModel(
            model_name        = self.model_name,
            generation_config = _genai.types.GenerationConfig(
                max_output_tokens = self.max_tokens,
                temperature       = self.temperature,
            ),
        )
        logger.info(f"[gemini] Initialised — model={self.model_name}")

    def call(self, system: str, user: str) -> Optional[dict]:
        # Gemini: merge system + user into a single prompt
        full_prompt = (
            f"{system}\n\n"
            f"---\n\n"
            f"{user}"
        )

        for attempt in range(self.max_retries):
            try:
                response = self._model.generate_content(full_prompt)
                raw      = response.text or ""
                result   = self.parse_json(raw)
                if result:
                    logger.debug(f"[gemini] OK attempt={attempt+1}")
                    return result

            except _GoogleAPIError as e:
                logger.warning(f"[gemini] API error (attempt {attempt+1}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (attempt + 1))
            except Exception as e:
                logger.error(f"[gemini] Unexpected error: {e}")
                break

        logger.error("[gemini] All retries exhausted")
        return None
