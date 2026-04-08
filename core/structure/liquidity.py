"""
Liquidity Sweep detection.

A liquidity sweep occurs when price wicks BEYOND a prior swing high/low
(taking out stop-losses clustered there) and then CLOSES BACK inside the range.

This is the "stop hunt" pattern used by institutional traders before reversals.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional
import pandas as pd

from core.structure.swing import SwingPoint, detect_swings
from config.trading_config import TRADING_CONFIG


@dataclass
class LiquiditySweep:
    index: int
    timestamp: pd.Timestamp
    direction: str         # "buy_side" (swept highs) | "sell_side" (swept lows)
    swept_level: float
    wick_high: float
    wick_low: float
    close_price: float
    wick_ratio: float      # wick size / total candle range


def detect_liquidity_sweeps(
    df: pd.DataFrame,
    swings: List[SwingPoint] | None = None,
    min_wick_ratio: float | None = None,
) -> List[LiquiditySweep]:
    """
    Detect liquidity sweeps candle by candle.

    Sweep criteria:
      1. Candle wick exceeds a prior swing level
      2. Candle CLOSES back inside (does not close beyond the level)
      3. Wick-to-range ratio exceeds threshold (filters minor wicks)
    """
    if swings is None:
        swings = detect_swings(df)
    wr_threshold = min_wick_ratio or TRADING_CONFIG.sweep_wick_ratio

    sweeps: List[LiquiditySweep] = []
    n = len(df)

    for i in range(1, n):
        row   = df.iloc[i]
        ts    = df.index[i]
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        candle_range = h - l
        if candle_range < 1e-8:
            continue

        prior = [s for s in swings if s.index < i]

        # ── Buy-side liquidity sweep (wick above prior swing HIGH, closes below) ─
        prior_highs = [s for s in prior if s.kind == "high"]
        if prior_highs:
            level = prior_highs[-1].price
            if h > level and c < level:
                wick = h - max(o, c)
                if wick / candle_range >= wr_threshold:
                    sweeps.append(LiquiditySweep(
                        index=i, timestamp=ts,
                        direction="buy_side",
                        swept_level=level,
                        wick_high=h, wick_low=l,
                        close_price=c,
                        wick_ratio=wick / candle_range,
                    ))

        # ── Sell-side liquidity sweep (wick below prior swing LOW, closes above) ─
        prior_lows = [s for s in prior if s.kind == "low"]
        if prior_lows:
            level = prior_lows[-1].price
            if l < level and c > level:
                wick = min(o, c) - l
                if wick / candle_range >= wr_threshold:
                    sweeps.append(LiquiditySweep(
                        index=i, timestamp=ts,
                        direction="sell_side",
                        swept_level=level,
                        wick_high=h, wick_low=l,
                        close_price=c,
                        wick_ratio=wick / candle_range,
                    ))

    return sweeps


def get_latest_sweep(df: pd.DataFrame) -> Optional[LiquiditySweep]:
    sweeps = detect_liquidity_sweeps(df)
    return sweeps[-1] if sweeps else None
