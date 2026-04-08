"""
Advanced Signal Engine.

Extends the base SignalEngine with:
- Fair Value Gap (FVG) detection
- Order Block identification
- Premium / Discount zone filtering
- Multi-LTF confluence (M15 + M5)
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, Tuple
import pandas as pd
import numpy as np

from core.signal.signal_engine import SignalEngine, TradeSignal
from core.structure import atr
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class FairValueGap:
    index: int
    direction: str    # "bullish" | "bearish"
    top: float
    bottom: float
    filled: bool = False


@dataclass
class OrderBlock:
    index: int
    direction: str    # "bullish" | "bearish"
    high: float
    low: float
    open_: float
    close_: float


def detect_fvg(df: pd.DataFrame) -> List[FairValueGap]:
    """
    Fair Value Gap: 3-candle pattern where gap exists between
    candle[i-2].high and candle[i].low (bullish FVG)
    or candle[i-2].low and candle[i].high (bearish FVG).
    """
    fvgs = []
    n = len(df)
    for i in range(2, n):
        c1 = df.iloc[i - 2]
        c3 = df.iloc[i]

        # Bullish FVG: c3.low > c1.high
        if c3["low"] > c1["high"]:
            fvgs.append(FairValueGap(
                index=i, direction="bullish",
                top=c3["low"], bottom=c1["high"],
            ))

        # Bearish FVG: c3.high < c1.low
        if c3["high"] < c1["low"]:
            fvgs.append(FairValueGap(
                index=i, direction="bearish",
                top=c1["low"], bottom=c3["high"],
            ))

    return fvgs


def detect_order_blocks(df: pd.DataFrame, lookback: int = 50) -> List[OrderBlock]:
    """
    Order Block: last bearish candle before a bullish impulse (bullish OB),
    or last bullish candle before a bearish impulse (bearish OB).
    """
    obs = []
    subset = df.iloc[-lookback:]
    n = len(subset)

    for i in range(1, n - 1):
        row  = subset.iloc[i]
        next_= subset.iloc[i + 1]
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        nc = next_["close"]

        # Bearish candle followed by strong bullish move → bullish OB
        if c < o and nc > h:
            obs.append(OrderBlock(
                index=subset.index[i].value,
                direction="bullish",
                high=h, low=l, open_=o, close_=c,
            ))

        # Bullish candle followed by strong bearish move → bearish OB
        if c > o and nc < l:
            obs.append(OrderBlock(
                index=subset.index[i].value,
                direction="bearish",
                high=h, low=l, open_=o, close_=c,
            ))

    return obs


def premium_discount_zone(df: pd.DataFrame) -> Tuple[float, float, str]:
    """
    Identify if current price is in Premium (above 50% of recent range) or
    Discount (below 50%) zone.  Buy in discount, sell in premium.
    """
    recent = df.tail(100)
    high  = recent["high"].max()
    low   = recent["low"].min()
    mid   = (high + low) / 2
    price = df["close"].iloc[-1]

    if price > mid:
        zone = "premium"
    elif price < mid:
        zone = "discount"
    else:
        zone = "equilibrium"

    return high, low, zone


class AdvancedSignalEngine(SignalEngine):
    """
    Inherits full base signal generation and overlays advanced confluence.
    Adds FVG / Order Block entry refinement and premium/discount filter.
    """

    def refine_entry(
        self,
        signal: TradeSignal,
        df_m5: pd.DataFrame,
    ) -> TradeSignal:
        """
        Attempt to refine entry using M5 FVG or Order Block as a tighter entry.
        Improves RR while keeping the structural stop-loss.
        """
        fvgs = detect_fvg(df_m5)
        obs  = detect_order_blocks(df_m5)
        _, _, zone = premium_discount_zone(df_m5)

        # Zone alignment check
        if signal.direction == "buy" and zone == "premium":
            logger.debug("Buy signal in premium zone — skipping refinement entry")
            return signal
        if signal.direction == "sell" and zone == "discount":
            logger.debug("Sell signal in discount zone — skipping refinement entry")
            return signal

        # Try to find aligned FVG for tighter entry
        aligned_fvgs = [
            f for f in fvgs[-5:]
            if f.direction == signal.direction
        ]
        if aligned_fvgs:
            fvg = aligned_fvgs[-1]
            if signal.direction == "buy":
                # Enter at FVG bottom (better price)
                refined_entry = fvg.bottom
            else:
                refined_entry = fvg.top

            new_rr = abs(signal.take_profit - refined_entry) / abs(signal.stop_loss - refined_entry)
            if new_rr > signal.rr_ratio:
                signal.entry_price = refined_entry
                signal.rr_ratio    = new_rr
                logger.info(f"Entry refined via FVG: new RR={new_rr:.2f}")

        return signal
