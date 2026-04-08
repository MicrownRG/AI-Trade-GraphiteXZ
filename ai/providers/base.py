"""
Base interface for all AI provider clients.

Every provider must implement `call(system, user) -> dict | None`.
The returned dict MUST have shape:
  {
    "confidence": float,   # 0.0 – 1.0
    "decision":   str,     # "TAKE" | "SKIP"
    "reason":     str
  }
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional
import json

from utils.logger import get_logger

logger = get_logger(__name__)


class BaseAIProvider(ABC):
    """Abstract base for every AI provider."""

    name: str = "base"

    @abstractmethod
    def call(self, system: str, user: str) -> Optional[dict]:
        """
        Call the underlying LLM API.

        Returns a parsed dict on success, None on failure.
        Implementations handle their own retry logic.
        """

    # ── Shared helpers ────────────────────────────────────────────────────────

    @staticmethod
    def parse_json(raw: str) -> Optional[dict]:
        """
        Safely parse a JSON string that may be wrapped in markdown fences.
        Returns None if parsing fails.
        """
        text = raw.strip()

        # Strip ```json ... ``` or ``` ... ``` fences
        if text.startswith("```"):
            lines = text.splitlines()
            # Drop first and last fence lines
            inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
            text  = "\n".join(inner).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"[{__name__}] JSON parse failed: {e} | raw={raw[:120]!r}")
            return None

    @staticmethod
    def validate_response(data: dict) -> bool:
        """Check that required keys exist and values are in range."""
        if not isinstance(data, dict):
            return False
        if "confidence" not in data or "decision" not in data:
            return False
        if not isinstance(data["confidence"], (int, float)):
            return False
        if data["decision"] not in ("TAKE", "SKIP"):
            return False
        return True

    def safe_call(self, system: str, user: str) -> Optional[dict]:
        """
        Wraps call() with validation.
        Returns None if result is malformed.
        """
        result = self.call(system, user)
        if result is None:
            return None
        if not self.validate_response(result):
            logger.warning(f"[{self.name}] Response failed validation: {result}")
            return None
        # Clamp confidence to [0, 1]
        result["confidence"] = max(0.0, min(1.0, float(result["confidence"])))
        return result
