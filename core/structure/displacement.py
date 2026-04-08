"""
Displacement detection.

Displacement = a strong, impulsive candle (or series of candles) that:
  1. Has a large body relative to its range (body_ratio >= threshold)
  2. Moves at least N pips in one direction
  3. Closes near its extreme (minimal wick on the body side)

This indicates genuine institutional participation / order flow.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional
import pandas as pd
import numpy as np

from config.trading_config import TRADING_CONFIG


@dataclass
class DisplacementEvent:
    index: int
    timestamp: pd.Timestamp
    direction: str       # "bullish" | "bearish"
    body_ratio: float    # body / (high - low)
    move_pips: float
    open_price: float
    close_price: float


def detect_displacement(
    df: pd.DataFrame,
    min_body_ratio: float | None = None,
    min_pips: float | None = None,
) -> List[DisplacementEvent]:
    """
    Scan every candle for displacement criteria.
    """
    br_thresh = min_body_ratio or TRADING_CONFIG.displacement_min_body_ratio
    pip_thresh = min_pips or TRADING_CONFIG.displacement_min_pips
    price_thresh = pip_thresh * 0.1   # pips → price units for gold

    events: List[DisplacementEvent] = []
    n = len(df)

    for i in range(n):
        row = df.iloc[i]
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        candle_range = h - l
        if candle_range < 1e-8:
            continue

        body = abs(c - o)
        body_ratio = body / candle_range
        move_pips = body / 0.1  # convert price to pips for gold

        if body_ratio >= br_thresh and body >= price_thresh:
            direction = "bullish" if c > o else "bearish"
            events.append(DisplacementEvent(
                index=i,
                timestamp=df.index[i],
                direction=direction,
                body_ratio=body_ratio,
                move_pips=move_pips,
                open_price=o,
                close_price=c,
            ))

    return events


def get_recent_displacement(
    df: pd.DataFrame, lookback_bars: int = 5
) -> Optional[DisplacementEvent]:
    """Return the most recent displacement within the last N bars."""
    events = detect_displacement(df)
    recent = [e for e in events if e.index >= len(df) - lookback_bars]
    return recent[-1] if recent else None


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range helper."""
    high, low, prev_close = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()
