"""
Stop Loss calculation based on market structure.

Priority order:
1. Place SL beyond the nearest swing high/low that invalidates the setup
2. Minimum buffer of 0.5 × ATR beyond the swing
3. Never less than MIN_SL_PIPS from entry
"""
from __future__ import annotations
from typing import List, Optional
import pandas as pd

from core.structure.swing import SwingPoint
from core.structure.displacement import atr as calc_atr
from config.trading_config import TRADING_CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)

MIN_SL_PIPS = 10.0
ATR_BUFFER  = 0.5   # multiplier of ATR added beyond swing


def calculate_stop_loss(
    direction: str,
    entry_price: float,
    swings: List[SwingPoint],
    df_ltf: pd.DataFrame,
) -> float:
    """
    Calculate SL based on the last swing that would invalidate the trade.

    For BUY: SL = below the last swing LOW
    For SELL: SL = above the last swing HIGH
    """
    atr_val = calc_atr(df_ltf, TRADING_CONFIG.atr_period).iloc[-1]
    buffer  = atr_val * ATR_BUFFER

    if direction == "buy":
        relevant = [s for s in swings if s.kind == "low" and s.price < entry_price]
        if relevant:
            swing_level = relevant[-1].price
            sl = swing_level - buffer
        else:
            # Fallback: ATR-based SL
            sl = entry_price - atr_val * 2.0
    else:
        relevant = [s for s in swings if s.kind == "high" and s.price > entry_price]
        if relevant:
            swing_level = relevant[-1].price
            sl = swing_level + buffer
        else:
            sl = entry_price + atr_val * 2.0

    # Enforce minimum SL distance
    min_distance = MIN_SL_PIPS * 0.1
    if direction == "buy" and entry_price - sl < min_distance:
        sl = entry_price - min_distance
    elif direction == "sell" and sl - entry_price < min_distance:
        sl = entry_price + min_distance

    logger.debug(f"SL calculated: direction={direction} entry={entry_price:.2f} sl={sl:.2f}")
    return round(sl, 2)


def adjust_sl_to_breakeven(entry_price: float, current_price: float, direction: str,
                            activation_rr: float = 1.0, original_sl: float = 0.0) -> float:
    """
    Move SL to breakeven when trade reaches activation_rr in profit.
    Returns new SL (or original if not activated yet).
    """
    risk = abs(entry_price - original_sl)
    if risk == 0:
        return original_sl

    if direction == "buy":
        profit_ratio = (current_price - entry_price) / risk
        if profit_ratio >= activation_rr:
            return entry_price + 0.01   # just above entry
    else:
        profit_ratio = (entry_price - current_price) / risk
        if profit_ratio >= activation_rr:
            return entry_price - 0.01

    return original_sl
