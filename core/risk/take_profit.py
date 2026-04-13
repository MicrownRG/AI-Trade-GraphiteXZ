"""
Take Profit calculation.

Supports:
1. Fixed RR ratio TP
2. Structure-based TP (next major swing level)
3. Partial TP with trailing stop
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Tuple
import pandas as pd

from core.structure.swing import SwingPoint
from config.risk_config import RISK_CONFIG
from config.trading_config import TRADING_CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TakeProfitLevels:
    tp1: float          # partial exit (1:1 RR) — take 50%
    tp2: float          # main target (1:2 RR)
    tp3: float          # extended target (structure-based)
    rr_at_tp2: float


def calculate_take_profit(
    direction: str,
    entry_price: float,
    stop_loss: float,
    swings: List[SwingPoint],
    min_rr: float | None = None,
) -> TakeProfitLevels:
    """
    Calculate tiered take profit levels.

    TP1 = 1:1 RR (partial)
    TP2 = 2:2 RR minimum (main target)
    TP3 = next significant structure level
    """
    rr = min_rr or RISK_CONFIG.min_rr_ratio
    risk = abs(entry_price - stop_loss)

    # Find immediate structure target (Apex/Nearest Resistance)
    inward_buffer = 3.0 * 0.1  # 3 pips buffer to exit before exact touch
    structure_tp = None

    if direction == "buy":
        candidates = sorted([s for s in swings if s.kind == "high" and s.price > entry_price], key=lambda s: s.price)
        if candidates:
            structure_tp = candidates[0].price - inward_buffer
    else:
        candidates = sorted([s for s in swings if s.kind == "low" and s.price < entry_price], key=lambda s: s.price, reverse=True)
        if candidates:
            structure_tp = candidates[0].price + inward_buffer

    # Calculate conservative TP1 (0.8R to 1.0R)
    base_tp1 = entry_price + risk * 1.0 if direction == "buy" else entry_price - risk * 1.0
    
    # Calculate ambitious mathematical TP2
    math_tp2 = entry_price + risk * rr if direction == "buy" else entry_price - risk * rr

    # ── OPTIMIZATION: Jangan maksa TP jauh! ──
    # Jika ada resistance/support terdekat yang jaraknya masuk akal (RR > 0.8), ambil itu sebagai TP Utama!
    if structure_tp is not None:
        struct_risk = abs(structure_tp - entry_price)
        struct_rr = struct_risk / risk if risk > 0 else 0
        
        if struct_rr >= 0.8:
            # Structure is far enough to be worth taking, cap TP2 at structure
            if direction == "buy":
                tp2 = min(math_tp2, structure_tp)
            else:
                tp2 = max(math_tp2, structure_tp)
        else:
            # Structure is too close (RR < 0.8), ignore it and trust mathematical RR 
            # (or the signal engine shouldn't have fired anyway)
            tp2 = math_tp2
    else:
        tp2 = math_tp2

    # TP1 is partial close at 1:1, but if TP2 is pulled closer than 1R, skip TP1
    if direction == "buy":
        tp1 = min(base_tp1, tp2 * 0.999) if abs(tp2 - entry_price) > risk else tp2
        tp3 = tp2 * 1.005 # Extrapolation
    else:
        tp1 = max(base_tp1, tp2 * 1.001) if abs(tp2 - entry_price) > risk else tp2
        tp3 = tp2 * 0.995 # Extrapolation

    rr_at_tp2 = abs(tp2 - entry_price) / risk if risk > 0 else rr

    levels = TakeProfitLevels(
        tp1=round(tp1, 2),
        tp2=round(tp2, 2),
        tp3=round(tp3, 2),
        rr_at_tp2=round(rr_at_tp2, 2),
    )
    logger.debug(f"TP levels: {levels}")
    return levels


def trailing_stop(
    entry_price: float,
    current_price: float,
    current_sl: float,
    direction: str,
    trail_pips: float = 15.0,
) -> float:
    """
    Trailing stop that moves SL as price advances.
    Only moves in profit direction — never backward.
    """
    trail_distance = trail_pips * 0.1

    if direction == "buy":
        new_sl = current_price - trail_distance
        return max(new_sl, current_sl)
    else:
        new_sl = current_price + trail_distance
        return min(new_sl, current_sl)
