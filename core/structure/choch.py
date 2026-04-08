"""
Change of Character (CHOCH) detection.

CHOCH = price closes BEYOND a prior swing in the OPPOSITE direction of trend.
CHOCH signals a potential trend REVERSAL (not continuation like BOS).

Logic:
- In a downtrend (series of lower highs / lower lows):
    CHOCH = close above the most recent significant swing HIGH → bullish reversal signal
- In an uptrend (series of higher highs / higher lows):
    CHOCH = close below the most recent significant swing LOW → bearish reversal signal
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional
import pandas as pd

from core.structure.swing import SwingPoint, detect_swings


@dataclass
class CHOCHEvent:
    index: int
    timestamp: pd.Timestamp
    direction: str          # "bullish" (reversal up) | "bearish" (reversal down)
    broken_level: float
    close_price: float


def _infer_trend(swings: List[SwingPoint]) -> str:
    """
    Infer trend from the last 4 swing highs and lows.
    Returns "up", "down", or "ranging".
    """
    highs = [s for s in swings if s.kind == "high"][-4:]
    lows  = [s for s in swings if s.kind == "low"][-4:]

    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1].price > highs[-2].price
        hl = lows[-1].price  > lows[-2].price
        lh = highs[-1].price < highs[-2].price
        ll = lows[-1].price  < lows[-2].price

        if hh and hl:
            return "up"
        if lh and ll:
            return "down"

    return "ranging"


def detect_choch(df: pd.DataFrame, swings: List[SwingPoint] | None = None) -> List[CHOCHEvent]:
    """
    Detect CHOCH events bar-by-bar.
    """
    if swings is None:
        swings = detect_swings(df)

    events: List[CHOCHEvent] = []
    closes = df["close"].values
    n = len(df)

    for i in range(4, n):
        ts    = df.index[i]
        c     = closes[i]
        prior = [s for s in swings if s.index < i]

        trend = _infer_trend(prior)

        if trend == "down":
            # CHOCH bullish: in downtrend, break above last swing HIGH
            prior_highs = [s for s in prior if s.kind == "high"]
            if prior_highs and c > prior_highs[-1].price:
                events.append(CHOCHEvent(
                    index=i,
                    timestamp=ts,
                    direction="bullish",
                    broken_level=prior_highs[-1].price,
                    close_price=c,
                ))

        elif trend == "up":
            # CHOCH bearish: in uptrend, break below last swing LOW
            prior_lows = [s for s in prior if s.kind == "low"]
            if prior_lows and c < prior_lows[-1].price:
                events.append(CHOCHEvent(
                    index=i,
                    timestamp=ts,
                    direction="bearish",
                    broken_level=prior_lows[-1].price,
                    close_price=c,
                ))

    return events


def get_latest_choch(df: pd.DataFrame) -> Optional[CHOCHEvent]:
    events = detect_choch(df)
    return events[-1] if events else None
