"""
Fibonacci Staged Trailing Stop-Loss Manager.

Implements the tiered exit strategy:
  Stage 0 → Entry:  SL = fibo_sl (beyond swing)
  Stage 1 → TP1:    Close 50%,  SL → Entry (breakeven)
  Stage 2 → TP2:    Close 25%,  SL → TP1
  Stage 3 → TP3:    SL → TP2,  recalculate H1 extension TP (ride remaining 25%)
  Stage 4 → ExtTP:  SL → TP3,  position closes via MT5 TP or next recalculation

All stage transitions are idempotent — calling update_fibo_trail repeatedly
on the same stage is safe (returns None if no change needed).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

import pandas as pd

from utils.logger import get_logger

if TYPE_CHECKING:
    from core.signal.fibo_engine import FiboLevels

logger = get_logger(__name__)


@dataclass
class FiboStageResult:
    """Result returned when a stage transition is triggered."""
    stage:      int     # New stage number (1, 2, 3, or 4)
    new_sl:     float   # New stop loss price
    close_pct:  float   # Percentage of ORIGINAL lot to close (0.0 = don't close)
    new_tp:     Optional[float]  # New TP to set on MT5 (None = keep existing)
    msg:        str     # Human-readable message for Telegram


def update_fibo_trail(
    trade: dict,
    current_price: float,
    df_h1: Optional[pd.DataFrame] = None,
) -> Optional[FiboStageResult]:
    """
    Check if price has reached the next Fibo TP stage and return the
    required SL/close actions.

    Args:
        trade:         Trade dict from portfolio.open_trades (mutated in-place on stage update)
        current_price: Latest market price
        df_h1:         H1 OHLCV data for extension TP recalculation after Stage 3

    Returns:
        FiboStageResult if a transition occurred, None if no change.
    """
    fibo: Optional["FiboLevels"] = trade.get("fibo_levels")
    if fibo is None:
        return None  # Not a Fibo trade

    stage      = trade.get("fibo_stage", 0)
    direction  = trade["direction"]
    entry      = trade["entry_price"]

    # ── Stage 0 → 1: TP1 hit → Close 50%, SL → Entry ────────────────────────
    if stage == 0:
        tp1_hit = (
            (direction == "buy"  and current_price >= fibo.tp1) or
            (direction == "sell" and current_price <= fibo.tp1)
        )
        if tp1_hit:
            new_sl = entry + 0.08 if direction == "buy" else entry - 0.08  # +8 pips buffer
            new_sl = round(new_sl, 2)
            trade["fibo_stage"] = 1
            logger.info(
                f"🎯 Fibo Stage 1: TP1 hit @ {fibo.tp1:.2f} | "
                f"Close 50% | SL → {new_sl:.2f} (Breakeven)"
            )
            return FiboStageResult(
                stage=1,
                new_sl=new_sl,
                close_pct=0.50,
                new_tp=None,
                msg=(
                    f"🎯 *Fibo Stage 1* \\— TP1 Hit\\!\n"
                    f"Close 50% @ `{fibo.tp1:.2f}`\n"
                    f"SL → Breakeven `{new_sl:.2f}`"
                ),
            )

    # ── Stage 1 → 2: TP2 hit → Close 25%, SL → TP1 ─────────────────────────
    elif stage == 1:
        tp2_hit = (
            (direction == "buy"  and current_price >= fibo.tp2) or
            (direction == "sell" and current_price <= fibo.tp2)
        )
        if tp2_hit:
            new_sl = round(fibo.tp1, 2)
            trade["fibo_stage"] = 2
            logger.info(
                f"🎯 Fibo Stage 2: TP2 hit @ {fibo.tp2:.2f} | "
                f"Close 25% | SL → {new_sl:.2f} (TP1)"
            )
            return FiboStageResult(
                stage=2,
                new_sl=new_sl,
                close_pct=0.25,
                new_tp=None,
                msg=(
                    f"🎯 *Fibo Stage 2* \\— TP2 Hit\\!\n"
                    f"Close 25% @ `{fibo.tp2:.2f}`\n"
                    f"SL → TP1 `{new_sl:.2f}`"
                ),
            )

    # ── Stage 2 → 3: TP3 hit → Lock SL → TP2, recalc extension TP ──────────
    elif stage == 2:
        tp3_hit = (
            (direction == "buy"  and current_price >= fibo.tp3) or
            (direction == "sell" and current_price <= fibo.tp3)
        )
        if tp3_hit:
            new_sl = round(fibo.tp2, 2)
            # Try to get next TP from H1 fibo extension
            new_tp = get_next_fibo_tp_from_h1(df_h1, direction, fibo.tp3) or fibo.tp_extension
            trade["fibo_stage"] = 3
            # Update fibo_levels with new TP so next stage can track it
            fibo.tp_extension = new_tp
            logger.info(
                f"🎯 Fibo Stage 3: TP3 hit @ {fibo.tp3:.2f} | "
                f"SL → {new_sl:.2f} (TP2) | New TP → {new_tp:.2f} (H1 Ext)"
            )
            return FiboStageResult(
                stage=3,
                new_sl=new_sl,
                close_pct=0.0,   # No close at TP3 — ride the remaining 25%
                new_tp=new_tp,
                msg=(
                    f"🚀 *Fibo Stage 3* \\— TP3 Hit\\!\n"
                    f"Riding last 25% with extended target\\.\n"
                    f"SL → `{new_sl:.2f}` \\(TP2\\)\n"
                    f"New TP → `{new_tp:.2f}` \\(H1 Extension\\)"
                ),
            )

    # ── Stage 3 → 4: Extension TP hit → SL → TP3 ───────────────────────────
    elif stage == 3:
        ext_hit = (
            (direction == "buy"  and current_price >= fibo.tp_extension) or
            (direction == "sell" and current_price <= fibo.tp_extension)
        )
        if ext_hit:
            new_sl = round(fibo.tp3, 2)
            trade["fibo_stage"] = 4
            logger.info(
                f"🏆 Fibo Stage 4: Extension TP hit @ {fibo.tp_extension:.2f} | "
                f"SL → {new_sl:.2f} (TP3)"
            )
            return FiboStageResult(
                stage=4,
                new_sl=new_sl,
                close_pct=0.0,
                new_tp=None,
                msg=(
                    f"🏆 *Fibo Stage 4* \\— Extension Hit\\!\n"
                    f"Full run complete\\. SL → `{new_sl:.2f}` \\(TP3\\)"
                ),
            )

    return None


def get_next_fibo_tp_from_h1(
    df_h1:      Optional[pd.DataFrame],
    direction:  str,
    base_price: float,
    ext_ratio:  float = 0.618,
) -> Optional[float]:
    """
    Recalculate the next extension TP from H1 Fibonacci after TP3 is exceeded.

    Uses the most recent H1 swing pair to compute a fresh extension level.
    If no valid H1 swing is found, returns None (caller uses fibo.tp_extension as fallback).
    """
    if df_h1 is None or len(df_h1) < 50:
        return None

    try:
        from core.structure.swing import detect_swings
        swings = detect_swings(df_h1, lookback=5)
        if len(swings) < 2:
            return None

        highs = sorted([s for s in swings if s.kind == "high"], key=lambda x: x.index)
        lows  = sorted([s for s in swings if s.kind == "low"],  key=lambda x: x.index)

        if not highs or not lows:
            return None

        # Use the last completed H1 swing pair
        last_high = highs[-1]
        last_low  = lows[-1]

        swing_high = last_high.price
        swing_low  = last_low.price
        rng = swing_high - swing_low

        if rng < 5.0:  # Too small to be meaningful (< 5 pips on gold)
            return None

        if direction == "buy":
            ext_tp = swing_high + rng * ext_ratio
        else:
            ext_tp = swing_low - rng * ext_ratio

        logger.info(
            f"H1 Fibo Extension recalculated: {direction.upper()} TP → {ext_tp:.2f} "
            f"(H1 swing [{swing_low:.2f}–{swing_high:.2f}] × {ext_ratio})"
        )
        return round(ext_tp, 2)

    except Exception as e:
        logger.debug(f"H1 Fibo extension recalc failed: {e}")
        return None
