"""
Candlestick Pattern Detector.

Detects common reversal and continuation patterns on any timeframe DataFrame.
Returns a list of detected patterns with direction bias and strength.

Patterns detected:
  Reversal: Engulfing, Pin Bar (Hammer/Shooting Star), Morning/Evening Star,
            Harami, Tweezer Top/Bottom
  Continuation: Three White Soldiers / Three Black Crows, Marubozu
  Indecision: Doji, Inside Bar
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional
import pandas as pd

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CandlePattern:
    name: str           # "BULLISH_ENGULFING", "BEARISH_PIN_BAR", etc.
    direction: str      # "buy" or "sell"
    strength: int       # 1=weak, 2=moderate, 3=strong
    bar_index: int      # index in DataFrame where pattern completes


def detect_patterns(df: pd.DataFrame, lookback: int = 3) -> List[CandlePattern]:
    """
    Scan the last `lookback` closed candles for candlestick patterns.
    Uses iloc[-2] as the most recent CLOSED candle (iloc[-1] is live/forming).
    """
    if df is None or len(df) < 5:
        return []

    patterns: List[CandlePattern] = []
    idx = len(df) - 2  # last closed bar

    c = df.iloc[-2]   # current closed
    p = df.iloc[-3]   # previous
    pp = df.iloc[-4]  # 2 bars ago

    c_body = c["close"] - c["open"]
    c_abs_body = abs(c_body)
    c_range = c["high"] - c["low"]
    c_bullish = c_body > 0

    p_body = p["close"] - p["open"]
    p_abs_body = abs(p_body)
    p_range = p["high"] - p["low"]
    p_bullish = p_body > 0

    if c_range < 1e-8 or p_range < 1e-8:
        return patterns

    c_body_pct = c_abs_body / c_range
    p_body_pct = p_abs_body / p_range

    # ── Doji detection (body < 10% of range)
    c_is_doji = c_body_pct < 0.10
    p_is_doji = p_body_pct < 0.10

    # ── Engulfing ────────────────────────────────────────────────────
    if (c_bullish and not p_bullish and
            c["close"] >= p["open"] and c["open"] <= p["close"]):
        strength = 3 if c_body_pct > 0.60 else 2
        patterns.append(CandlePattern("BULLISH_ENGULFING", "buy", strength, idx))

    if (not c_bullish and p_bullish and
            c["close"] <= p["open"] and c["open"] >= p["close"]):
        strength = 3 if c_body_pct > 0.60 else 2
        patterns.append(CandlePattern("BEARISH_ENGULFING", "sell", strength, idx))

    # ── Pin Bar (Hammer / Shooting Star) ─────────────────────────────
    c_upper_wick = c["high"] - max(c["open"], c["close"])
    c_lower_wick = min(c["open"], c["close"]) - c["low"]

    # Hammer (bullish pin): long lower wick, small body at top
    if (c_lower_wick >= c_abs_body * 2.0 and
            c_lower_wick >= c_range * 0.50 and
            c_upper_wick < c_range * 0.20):
        patterns.append(CandlePattern("HAMMER", "buy", 2, idx))

    # Shooting Star (bearish pin): long upper wick, small body at bottom
    if (c_upper_wick >= c_abs_body * 2.0 and
            c_upper_wick >= c_range * 0.50 and
            c_lower_wick < c_range * 0.20):
        patterns.append(CandlePattern("SHOOTING_STAR", "sell", 2, idx))

    # ── Morning Star (3-candle bullish reversal) ─────────────────────
    # Bar -4: bearish, Bar -3: small body (indecision), Bar -2: bullish
    pp_body = pp["close"] - pp["open"]
    pp_range = pp["high"] - pp["low"]
    if pp_range > 1e-8:
        pp_body_pct = abs(pp_body) / pp_range
        if (pp_body < 0 and pp_body_pct > 0.40 and  # strong bearish
                p_body_pct < 0.30 and                 # small body (star)
                c_bullish and c_body_pct > 0.40 and   # strong bullish
                c["close"] > (pp["open"] + pp["close"]) / 2):  # closes above midpoint of first
            patterns.append(CandlePattern("MORNING_STAR", "buy", 3, idx))

    # ── Evening Star (3-candle bearish reversal) ─────────────────────
    if pp_range > 1e-8:
        pp_body_pct = abs(pp_body) / pp_range
        if (pp_body > 0 and pp_body_pct > 0.40 and  # strong bullish
                p_body_pct < 0.30 and                 # small body (star)
                not c_bullish and c_body_pct > 0.40 and  # strong bearish
                c["close"] < (pp["open"] + pp["close"]) / 2):
            patterns.append(CandlePattern("EVENING_STAR", "sell", 3, idx))

    # ── Harami (inside previous body) ────────────────────────────────
    if (not p_bullish and c_bullish and
            c["open"] >= p["close"] and c["close"] <= p["open"] and
            c_abs_body < p_abs_body * 0.5):
        patterns.append(CandlePattern("BULLISH_HARAMI", "buy", 1, idx))

    if (p_bullish and not c_bullish and
            c["open"] <= p["close"] and c["close"] >= p["open"] and
            c_abs_body < p_abs_body * 0.5):
        patterns.append(CandlePattern("BEARISH_HARAMI", "sell", 1, idx))

    # ── Tweezer Bottom (bullish) ─────────────────────────────────────
    low_diff = abs(c["low"] - p["low"])
    if (not p_bullish and c_bullish and
            low_diff < c_range * 0.05 and
            c_body_pct > 0.30 and p_body_pct > 0.30):
        patterns.append(CandlePattern("TWEEZER_BOTTOM", "buy", 2, idx))

    # ── Tweezer Top (bearish) ────────────────────────────────────────
    high_diff = abs(c["high"] - p["high"])
    if (p_bullish and not c_bullish and
            high_diff < c_range * 0.05 and
            c_body_pct > 0.30 and p_body_pct > 0.30):
        patterns.append(CandlePattern("TWEEZER_TOP", "sell", 2, idx))

    # ── Three White Soldiers (continuation bullish) ──────────────────
    if len(df) >= 5:
        b3 = df.iloc[-4]
        b2 = df.iloc[-3]
        b1 = df.iloc[-2]
        if (b3["close"] > b3["open"] and b2["close"] > b2["open"] and b1["close"] > b1["open"] and
                b2["close"] > b3["close"] and b1["close"] > b2["close"] and
                b2["open"] > b3["open"] and b1["open"] > b2["open"]):
            patterns.append(CandlePattern("THREE_WHITE_SOLDIERS", "buy", 3, idx))

    # ── Three Black Crows (continuation bearish) ─────────────────────
    if len(df) >= 5:
        b3 = df.iloc[-4]
        b2 = df.iloc[-3]
        b1 = df.iloc[-2]
        if (b3["close"] < b3["open"] and b2["close"] < b2["open"] and b1["close"] < b1["open"] and
                b2["close"] < b3["close"] and b1["close"] < b2["close"] and
                b2["open"] < b3["open"] and b1["open"] < b2["open"]):
            patterns.append(CandlePattern("THREE_BLACK_CROWS", "sell", 3, idx))

    # ── Marubozu (strong momentum, no wicks) ─────────────────────────
    if c_body_pct >= 0.85:
        if c_bullish:
            patterns.append(CandlePattern("BULLISH_MARUBOZU", "buy", 2, idx))
        else:
            patterns.append(CandlePattern("BEARISH_MARUBOZU", "sell", 2, idx))

    return patterns


def get_pattern_score(patterns: List[CandlePattern], direction: str) -> int:
    """
    Sum the score contribution from patterns aligned with the given direction.
    Opposing patterns subtract. Returns net score.
    """
    score = 0
    for p in patterns:
        if p.direction == direction:
            score += p.strength
        else:
            score -= 1
    return score


def get_pattern_names(patterns: List[CandlePattern], direction: str) -> str:
    """Return comma-separated names of patterns aligned with direction."""
    aligned = [p.name for p in patterns if p.direction == direction]
    return ", ".join(aligned) if aligned else ""
