"""
News Redis Client.

Reads news events and sentiment data written by the Go news_scraper service.
Falls back to the CalendarAPI HTTP feed if Redis is unavailable.

Redis key schema (mirrors store/redis_store.go):
  news:events          — JSON array of NewsEvent objects
  news:active          — "1" / "0" — news active within 30-min window
  news:last_scrape     — RFC3339 timestamp of last successful scrape
  news:sentiment:<slug>— JSON SentimentResult per event
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# Lazy import — redis is optional; fall back to CalendarAPI if not installed
try:
    import redis as _redis_lib
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False

_REDIS_ADDR     = os.getenv("REDIS_ADDR", "localhost:6379")
_REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
_REDIS_DB       = int(os.getenv("REDIS_DB", "0"))

KEY_EVENTS      = "news:events"
KEY_ACTIVE      = "news:active"
KEY_LAST_SCRAPE = "news:last_scrape"
KEY_SENTIMENT   = "news:sentiment:{slug}"


class NewsRedisClient:
    """
    Reads scraped news events from Redis.
    Thread-safe — uses a single persistent connection pool.
    """

    def __init__(self, host: str = "localhost", port: int = 6379,
                 password: str = "", db: int = 0):
        self._client = None
        self._last_fallback_warn: float = 0.0
        if _REDIS_AVAILABLE:
            try:
                host_str, port_str = (host, str(port))
                self._client = _redis_lib.Redis(
                    host=host_str, port=int(port_str),
                    password=password or None, db=db,
                    decode_responses=True,
                    socket_connect_timeout=3,
                    socket_timeout=3,
                )
                self._client.ping()
                logger.info(f"NewsRedisClient connected to Redis @ {host}:{port}")
            except Exception as e:
                logger.warning(f"NewsRedisClient: Redis unavailable ({e}), will use HTTP fallback")
                self._client = None

    # ── Public API ────────────────────────────────────────────────────────────

    def is_news_active(self) -> bool:
        """
        Returns True if any 3-star USD event is within ±30 minutes of now.
        Reads the pre-computed 'news:active' key set by the Go scraper.
        Falls back to CalendarAPI on Redis miss.
        """
        if self._client is not None:
            try:
                val = self._client.get(KEY_ACTIVE)
                if val is not None:
                    return val == "1"
                # Key expired — recompute from events list
                return self._check_active_from_events()
            except Exception as e:
                self._warn_fallback(e)

        return self._fallback_is_active()

    def get_events(self) -> List[Dict]:
        """Return the current list of scraped 3-star events (empty list on miss)."""
        if self._client is not None:
            try:
                raw = self._client.get(KEY_EVENTS)
                if raw:
                    return json.loads(raw)
            except Exception as e:
                self._warn_fallback(e)
        return []

    def get_sentiment(self, title: str) -> Optional[Dict]:
        """
        Return the sentiment dict for a specific event title.
        Returns None if not found.
        """
        if self._client is None:
            return None
        try:
            slug = _make_slug(title)
            raw  = self._client.get(KEY_SENTIMENT.format(slug=slug))
            if raw:
                return json.loads(raw)
        except Exception:
            pass
        return None

    def get_dominant_sentiment(self) -> Optional[str]:
        """
        Aggregate sentiment across all active events.
        Returns "bullish_xau" | "bearish_xau" | "neutral" | None (no events).
        """
        events = self.get_events()
        if not events:
            return None

        bull = 0.0
        bear = 0.0
        for ev in events:
            s = self.get_sentiment(ev.get("title", ""))
            if not s:
                continue
            score = float(s.get("score", 0.0))
            if score > 0:
                bull += score
            elif score < 0:
                bear += abs(score)

        if bull == 0 and bear == 0:
            return "neutral"
        if bull > bear * 1.3:
            return "bullish_xau"
        if bear > bull * 1.3:
            return "bearish_xau"
        return "neutral"

    def last_scrape_age_minutes(self) -> Optional[float]:
        """Returns how many minutes ago the Go scraper last ran. None if unknown."""
        if self._client is None:
            return None
        try:
            ts = self._client.get(KEY_LAST_SCRAPE)
            if ts:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                diff = (datetime.now(timezone.utc) - dt).total_seconds() / 60
                return round(diff, 1)
        except Exception:
            pass
        return None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _check_active_from_events(self) -> bool:
        """Re-derive active flag from raw events list (when KEY_ACTIVE has expired)."""
        events = self.get_events()
        if not events:
            return False
        now = datetime.now(timezone.utc)
        for ev in events:
            try:
                ts = ev.get("event_time")
                if not ts:
                    continue
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                diff_min = abs((dt - now).total_seconds()) / 60
                if diff_min <= 30:
                    return True
            except Exception:
                continue
        return False

    def _fallback_is_active(self) -> bool:
        """HTTP fallback using the existing CalendarAPI."""
        try:
            from core.data.calendar_api import CalendarAPI
            api    = CalendarAPI()
            events = api.get_high_impact_events()
            return api.is_news_active(events)
        except Exception:
            return False

    def _warn_fallback(self, exc: Exception) -> None:
        now = time.time()
        if now - self._last_fallback_warn > 300:  # warn at most once per 5min
            logger.warning(f"NewsRedisClient: Redis error ({exc}), using HTTP fallback")
            self._last_fallback_warn = now


# ── Module-level singleton ────────────────────────────────────────────────────

def _parse_redis_addr(addr: str):
    """Parse 'host:port' string."""
    parts = addr.rsplit(":", 1)
    host  = parts[0] if len(parts) == 2 else addr
    port  = int(parts[1]) if len(parts) == 2 else 6379
    return host, port


_host, _port = _parse_redis_addr(_REDIS_ADDR)
news_client = NewsRedisClient(
    host=_host, port=_port,
    password=_REDIS_PASSWORD, db=_REDIS_DB,
)


def _make_slug(title: str) -> str:
    import re
    s = title.lower().replace(" ", "_")
    s = re.sub(r"[^a-z0-9_]", "", s)
    return s[:50]
