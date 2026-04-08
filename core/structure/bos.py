"""
Break of Structure (BOS) detection.

BOS = price closes BEYOND a prior swing high (bullish BOS) or
      swing low (bearish BOS) in the direction of the existing trend.
BOS CONFIRMS the existing trend continuation.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional
import pandas as pd

from core.structure.swing import SwingPoint, detect_swings


@dataclass
class BOSEvent:
    index: int
    timestamp: pd.Timestamp
    direction: str          # "bullish" | "bearish"
    broken_level: float     # the swing level that was broken
    close_price: float


def detect_bos(df: pd.DataFrame, swings: List[SwingPoint] | None = None) -> List[BOSEvent]:
    """
    Scan bar-by-bar and emit a BOSEvent whenever a candle closes beyond
    a prior confirmed swing high (bullish BOS) or swing low (bearish BOS).

    Only considers swings BEFORE the current candle (no look-ahead).
    """
    if swings is None:
        swings = detect_swings(df)

    events: List[BOSEvent] = []
    closes = df["close"].values
    n = len(df)

    for i in range(1, n):
        ts = df.index[i]
        c  = closes[i]

        # Bullish BOS: close above a prior swing HIGH
        prior_highs = [s for s in swings if s.kind == "high" and s.index < i]
        if prior_highs:
            last_high = prior_highs[-1]
            if c > last_high.price:
                events.append(BOSEvent(
                    index=i,
                    timestamp=ts,
                    direction="bullish",
                    broken_level=last_high.price,
                    close_price=c,
                ))

        # Bearish BOS: close below a prior swing LOW
        prior_lows = [s for s in swings if s.kind == "low" and s.index < i]
        if prior_lows:
            last_low = prior_lows[-1]
            if c < last_low.price:
                events.append(BOSEvent(
                    index=i,
                    timestamp=ts,
                    direction="bearish",
                    broken_level=last_low.price,
                    close_price=c,
                ))

    return _deduplicate_bos(events)


def get_latest_bos(df: pd.DataFrame) -> Optional[BOSEvent]:
    events = detect_bos(df)
    return events[-1] if events else None


def _deduplicate_bos(events: List[BOSEvent]) -> List[BOSEvent]:
    """Drop consecutive BOS events in the same direction at the same level."""
    if not events:
        return events
    deduped = [events[0]]
    for e in events[1:]:
        prev = deduped[-1]
        if e.direction == prev.direction and abs(e.broken_level - prev.broken_level) < 1e-6:
            deduped[-1] = e   # update to latest
        else:
            deduped.append(e)
    return deduped
