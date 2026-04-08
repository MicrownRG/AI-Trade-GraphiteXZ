"""
Telegram Notifier.

Handles sending messages to Telegram using the Bot API.
Uses httpx (async) with a sync wrapper so it works from any context.

No AI involved — pure notification delivery.

Env vars:
    TELEGRAM_BOT_TOKEN   — from @BotFather
    TELEGRAM_CHAT_ID     — your personal chat ID (or group ID)
"""
from __future__ import annotations
import os
import asyncio
import threading
from datetime import datetime, timezone
from typing import Optional

from utils.logger import get_logger

logger = get_logger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    httpx = None          # type: ignore
    _HTTPX_AVAILABLE = False


class TelegramNotifier:
    """
    Thread-safe, fire-and-forget Telegram message sender.

    All sends run in a background thread so they never block
    the trading loop — even if Telegram is slow or unreachable.
    """

    def __init__(
        self,
        token:   Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: bool = True,
    ):
        self.token   = token   or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = enabled and bool(self.token) and bool(self.chat_id)

        if not _HTTPX_AVAILABLE:
            logger.warning("httpx not installed — Telegram notifications disabled. Run: pip install httpx")
            self.enabled = False

        if self.enabled:
            logger.info(f"TelegramNotifier ready (chat_id={self.chat_id[:6]}...)")
        else:
            logger.warning("TelegramNotifier disabled (missing token/chat_id or httpx)")

    # ── Public interface ──────────────────────────────────────────────────────

    def send(self, text: str, parse_mode: str = "MarkdownV2") -> None:
        """
        Non-blocking send — fires in background thread.
        Safe to call from the trading loop.
        """
        if not self.enabled:
            return
        t = threading.Thread(
            target=self._send_sync,
            args=(text, parse_mode),
            daemon=True,
        )
        t.start()

    def send_blocking(self, text: str, parse_mode: str = "MarkdownV2") -> bool:
        """Blocking send — use only outside the trading loop (e.g. startup message)."""
        if not self.enabled:
            return False
        return self._send_sync(text, parse_mode)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _send_sync(self, text: str, parse_mode: str) -> bool:
        url     = _TELEGRAM_API.format(token=self.token)
        payload = {
            "chat_id":    self.chat_id,
            "text":       text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            with httpx.Client(timeout=10.0) as client:
                r = client.post(url, json=payload)
                if r.status_code == 200:
                    return True
                # Telegram returns 400 for bad MarkdownV2 — fall back to plain text
                if r.status_code == 400:
                    logger.warning(f"Telegram 400 — retrying as plain text: {r.text[:120]}")
                    payload["parse_mode"] = ""
                    payload["text"]       = _strip_markdown(text)
                    r2 = client.post(url, json=payload)
                    return r2.status_code == 200
                logger.warning(f"Telegram send failed: {r.status_code} {r.text[:80]}")
                return False
        except Exception as e:
            logger.error(f"Telegram send exception: {e}")
            return False


def _strip_markdown(text: str) -> str:
    """Remove MarkdownV2 escape chars for plain-text fallback."""
    return text.replace("\\", "").replace("*", "").replace("`", "").replace("_", "")
