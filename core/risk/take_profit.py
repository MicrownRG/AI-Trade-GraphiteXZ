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

    tp1 = entry_price + risk * 1.0 if direction == "buy" else entry_price - risk * 1.0
    tp2 = entry_price + risk * rr  if direction == "buy" else entry_price - risk * rr

    # TP3: next swing structure target
    if direction == "buy":
        candidates = [s for s in swings if s.kind == "high" and s.price > tp2]
        tp3 = candidates[0].price if candidates else tp2 * 1.005
    else:
        candidates = [s for s in swings if s.kind == "low" and s.price < tp2]
        tp3 = candidates[0].price if candidates else tp2 * 0.995

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
