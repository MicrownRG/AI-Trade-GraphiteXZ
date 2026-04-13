"""
Higher Timeframe (HTF) Bias Determination.

The HTF bias is a directional filter — we ONLY take trades aligned with it.
Bias is determined from H4 / H1 structure using BOS and CHOCH.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import pandas as pd

from core.structure.swing import detect_swings
from core.structure.bos import detect_bos, BOSEvent
from core.structure.choch import detect_choch, CHOCHEvent


@dataclass
class HTFBias:
    direction: str          # "bullish" | "bearish" | "neutral"
    confidence: float       # 0.0 – 1.0
    last_bos: Optional[BOSEvent]
    last_choch: Optional[CHOCHEvent]
    trend_str: str          # human-readable summary
    swing_structure: str    # "hh_hl" | "lh_ll" | "mixed"


def calculate_htf_bias(df_h4: pd.DataFrame, df_h1: pd.DataFrame) -> HTFBias:
    """
    Compute HTF bias from H4 primary and H1 secondary confirmation.

    Rules:
    - If H4 BOS is bullish AND H1 BOS is bullish → strong bullish bias
    - If H4 CHOCH is bullish (coming from bearish trend) → potential reversal, weak bullish
    - Vice versa for bearish
    - Conflicting signals → neutral
    """
    # ── H4 analysis ──────────────────────────────────────────────────────────
    h4_swings  = detect_swings(df_h4)
    h4_bos     = detect_bos(df_h4, h4_swings)
    h4_choch   = detect_choch(df_h4, h4_swings)
    h4_swing_str = _swing_structure(h4_swings)

    latest_h4_bos   = h4_bos[-1]   if h4_bos   else None
    latest_h4_choch = h4_choch[-1] if h4_choch else None

    # ── H1 analysis ──────────────────────────────────────────────────────────
    h1_swings = detect_swings(df_h1)
    h1_bos    = detect_bos(df_h1, h1_swings)
    latest_h1_bos = h1_bos[-1] if h1_bos else None

    # ── Determine bias ────────────────────────────────────────────────────────
    h4_dir = latest_h4_bos.direction if latest_h4_bos else None
    h1_dir = latest_h1_bos.direction if latest_h1_bos else None

    if h4_dir == "bullish" and h1_dir == "bullish":
        direction   = "bullish"
        confidence  = 0.85
        trend_str   = "Strong bullish: H4 + H1 BOS align"
    elif h4_dir == "bearish" and h1_dir == "bearish":
        direction   = "bearish"
        confidence  = 0.85
        trend_str   = "Strong bearish: H4 + H1 BOS align"
    elif h4_dir == "bullish" and h4_swing_str == "hh_hl":
        direction   = "bullish"
        confidence  = 0.65
        trend_str   = "Bullish: H4 BOS + higher structure"
    elif h4_dir == "bearish" and h4_swing_str == "lh_ll":
        direction   = "bearish"
        confidence  = 0.65
        trend_str   = "Bearish: H4 BOS + lower structure"
    elif latest_h4_choch and latest_h4_choch.direction == "bullish":
        direction   = "bullish"
        confidence  = 0.45
        trend_str   = "Weak bullish: H4 CHOCH only (early reversal signal)"
    elif latest_h4_choch and latest_h4_choch.direction == "bearish":
        direction   = "bearish"
        confidence  = 0.45
        trend_str   = "Weak bearish: H4 CHOCH only (early reversal signal)"
    elif h1_dir == "bullish":
        direction   = "bullish"
        confidence  = 0.50
        trend_str   = "Mild bullish: H1 BOS only (H4 neutral)"
    elif h1_dir == "bearish":
        direction   = "bearish"
        confidence  = 0.50
        trend_str   = "Mild bearish: H1 BOS only (H4 neutral)"
    else:
        direction  = "neutral"
        confidence = 0.30
        trend_str  = "No clear HTF bias — ranging market"

    return HTFBias(
        direction=direction,
        confidence=confidence,
        last_bos=latest_h4_bos,
        last_choch=latest_h4_choch,
        trend_str=trend_str,
        swing_structure=h4_swing_str,
    )


def _swing_structure(swings) -> str:
    highs = [s for s in swings if s.kind == "high"][-4:]
    lows  = [s for s in swings if s.kind == "low"][-4:]
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1].price > highs[-2].price
        hl = lows[-1].price  > lows[-2].price
        if hh and hl:
            return "hh_hl"
        if not hh and not hl:
            return "lh_ll"
    return "mixed"
