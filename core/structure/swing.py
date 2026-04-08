"""
Swing High / Low detection using fractal-style logic.
A swing high is a candle whose high is the highest among the N candles
on each side.  Same logic inverted for swing low.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import List

from config.trading_config import TRADING_CONFIG


@dataclass
class SwingPoint:
    index: int          # positional index in the DataFrame
    timestamp: pd.Timestamp
    price: float
    kind: str           # "high" | "low"


def detect_swings(df: pd.DataFrame, lookback: int | None = None) -> List[SwingPoint]:
    """
    Detect swing highs and lows.

    Args:
        df: OHLCV DataFrame with DatetimeIndex
        lookback: candles on each side to confirm swing (default from config)

    Returns:
        List of SwingPoint objects sorted by index.
    """
    lb = lookback or TRADING_CONFIG.swing_lookback
    swings: List[SwingPoint] = []

    highs = df["high"].values
    lows  = df["low"].values
    n     = len(df)

    for i in range(lb, n - lb):
        # Swing High: local maximum
        if highs[i] == np.max(highs[i - lb : i + lb + 1]):
            swings.append(SwingPoint(
                index=i,
                timestamp=df.index[i],
                price=highs[i],
                kind="high",
            ))
        # Swing Low: local minimum
        if lows[i] == np.min(lows[i - lb : i + lb + 1]):
            swings.append(SwingPoint(
                index=i,
                timestamp=df.index[i],
                price=lows[i],
                kind="low",
            ))

    # Deduplicate overlapping swings (keep highest/lowest per cluster)
    return _deduplicate(swings)


def _deduplicate(swings: List[SwingPoint]) -> List[SwingPoint]:
    """Remove duplicate swings at the same price within proximity."""
    if not swings:
        return swings
    swings.sort(key=lambda s: s.index)
    deduped: List[SwingPoint] = [swings[0]]
    for s in swings[1:]:
        prev = deduped[-1]
        if s.kind == prev.kind and abs(s.index - prev.index) < 3:
            # Keep the more extreme one
            if s.kind == "high" and s.price > prev.price:
                deduped[-1] = s
            elif s.kind == "low" and s.price < prev.price:
                deduped[-1] = s
        else:
            deduped.append(s)
    return deduped


def get_recent_swings(
    df: pd.DataFrame, n_swings: int = 6, lookback: int | None = None
) -> dict[str, List[SwingPoint]]:
    """Return the N most recent swing highs and swing lows separately."""
    all_swings = detect_swings(df, lookback)
    highs = [s for s in all_swings if s.kind == "high"][-n_swings:]
    lows  = [s for s in all_swings if s.kind == "low"][-n_swings:]
    return {"highs": highs, "lows": lows}
