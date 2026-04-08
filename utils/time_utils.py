"""Time utility functions."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta

WIB_OFFSET = timedelta(hours=7)   # UTC+7, no pytz needed


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_wib(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(WIB_OFFSET))


def is_valid_session(dt: datetime) -> bool:
    """Returns True if current time is within London or NY session (UTC)."""
    hour = dt.hour if dt.tzinfo else dt.hour
    in_london = 7 <= hour < 16
    in_ny     = 12 <= hour < 21
    return in_london or in_ny


def format_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
