"""General helper functions."""
from __future__ import annotations
import hashlib
import json
from typing import Any
from datetime import datetime


def round_price(price: float, tick_size: float = 0.01) -> float:
    """Round price to nearest tick size."""
    return round(round(price / tick_size) * tick_size, 10)


def pips_to_price(pips: float, pip_size: float = 0.1) -> float:
    return pips * pip_size


def price_to_pips(price: float, pip_size: float = 0.1) -> float:
    return price / pip_size


def dict_fingerprint(d: dict) -> str:
    """Deterministic hash of a dict (useful for deduplication)."""
    serialised = json.dumps(d, sort_keys=True, default=str)
    return hashlib.md5(serialised.encode()).hexdigest()[:12]


def clamp(value: float, min_val: float, max_val: float) -> float:
    return max(min_val, min(value, max_val))


def format_pnl(pnl: float) -> str:
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.2f}"


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b != 0 else default
