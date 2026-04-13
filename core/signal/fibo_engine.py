"""
Fibonacci Multi-Timeframe Strategy Engine.

Strategy:
  M15 → Auto-detect swing high/low → Calculate all Fibo levels (entry, SL, TP1/2/3, extension)
  M5  → RSI strict bounce gate (must touch zone, then bounce back)
  M1  → Tiered momentum trigger:
         Tier A: Engulfing + Strong Close (body > 60%) → full lot
         Tier B: Pin Bar only / weak engulfing           → half lot
         Reject: Doji / inside bar                       → skip

Partial Close:
  TP1 → Close 50%,  SL → BE (entry)
  TP2 → Close 25%,  SL → TP1
  TP3 → SL → TP2, recalc extension from H1 Fibo

All levels are mathematically derived from Fibo — no guessing.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional, Tuple
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from core.structure.swing import detect_swings
from config.trading_config import TRADING_CONFIG
from config.settings import SYMBOL
from utils.logger import get_logger

logger = get_logger(__name__)

# ── Fibo Ratios ──────────────────────────────────────────────────────────────
FIBO_RATIOS = {
    "r0":    0.000,
    "r236":  0.236,
    "r382":  0.382,
    "r500":  0.500,
    "r618":  0.618,
    "r786":  0.786,
    "r100":  1.000,
    "ext127": 1.272,
    "ext161": 1.618,
}

# ── Entry Zone: Price must be between these retracement levels ────────────────
ENTRY_ZONE_MIN = 0.50   # 50% retracement
ENTRY_ZONE_MAX = 0.786  # 78.6% retracement

# ── RSI Strict Gate Parameters ────────────────────────────────────────────────
RSI_PERIOD          = 14
RSI_OVERSOLD        = 30.0   # Must have TOUCHED below this to confirm oversold
RSI_OVERSOLD_EXIT   = 33.0   # Must bounce back ABOVE this (bounce confirmed for buy)
RSI_OVERBOUGHT      = 70.0   # Must have TOUCHED above this
RSI_OVERBOUGHT_EXIT = 67.0   # Must drop back BELOW this (bounce confirmed for sell)
RSI_LOOKBACK        = 20     # Candles to look back for the RSI touch

# ── SL Buffer ─────────────────────────────────────────────────────────────────
SL_BUFFER_PIPS = 3.0  # pips beyond swing_low/high for SL


@dataclass
class FiboLevels:
    """All Fibonacci-derived price levels for a setup."""
    signal_id:  str
    symbol:     str
    direction:  str       # "buy" | "sell"
    timestamp:  datetime

    # ── Anchor Swings ──────────────────────────────────────────────────────────
    swing_low:  float
    swing_high: float

    # ── Key Price Levels ──────────────────────────────────────────────────────
    entry:          float  # 61.8% retracement (golden ratio)
    stop_loss:      float  # beyond swing_low/high + buffer
    tp1:            float  # 38.2% level (50% close)
    tp2:            float  # 0% level = swing_high/low (25% close)
    tp3:            float  # 127.2% extension (ride remaining 25%)
    tp_extension:   float  # 161.8% extension (if TP3 exceeded, new H1 target)

    # ── All Retrace Lines (for chart) ─────────────────────────────────────────
    level_0:    float  # 0%
    level_236:  float  # 23.6%
    level_382:  float  # 38.2%
    level_500:  float  # 50%
    level_618:  float  # 61.8%
    level_786:  float  # 78.6%
    level_100:  float  # 100%

    # ── Signal Quality ────────────────────────────────────────────────────────
    signal_tier:    str    # "A" | "B"
    lot_multiplier: float  # Tier A = 1.0, Tier B = 0.5
    rsi_value:      float  # RSI at signal time (for chart display)
    score:          int    # signal quality score (for compatibility with RiskExecutor)

    # ── Compatibility fields (for TradeSignal-like usage) ─────────────────────
    entry_price:    float = field(init=False)
    take_profit:    float = field(init=False)
    rr_ratio:       float = field(init=False)
    atr_pips:       float = 0.0
    session:        str   = ""
    ai_confidence:  float = 0.0
    ai_reason:      str   = ""
    is_extreme:     bool  = False
    is_reentry:     bool  = False
    is_stalking:    bool  = False
    stalking_reason: str  = ""
    score_breakdown: dict = field(default_factory=dict)

    def __post_init__(self):
        self.entry_price = self.entry
        self.take_profit = self.tp2  # Default TP for compatibility
        risk = abs(self.entry - self.stop_loss)
        self.rr_ratio = abs(self.tp2 - self.entry) / risk if risk > 0 else 0


# ── RSI Calculation ───────────────────────────────────────────────────────────

def _calc_rsi(df: pd.DataFrame, period: int = RSI_PERIOD) -> pd.Series:
    """Compute RSI from OHLCV DataFrame."""
    close = df["close"]
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


# ── Swing Detection for Fibo ──────────────────────────────────────────────────

def detect_swing_for_fibo(
    df_m15: pd.DataFrame,
    min_range_pips: float = 20.0,
    max_range_pips: float = 300.0,
) -> Optional[Tuple[float, float, str]]:
    """
    Detect the most recent significant swing suitable for a Fibo setup.

    Looks for the last completed move (up or down) and checks if
    current price is in the retracement zone (50%-78.6%).

    Returns:
        (swing_low, swing_high, direction) or None
    """
    if df_m15 is None or len(df_m15) < 50:
        return None

    swings = detect_swings(df_m15, lookback=3)
    if len(swings) < 2:
        return None

    current_price = df_m15["close"].iloc[-1]
    pips_factor = 0.1  # 1 pip = 0.1 for XAUUSD

    # Check last few swing pairs
    highs = [s for s in swings if s.kind == "high"][-5:]
    lows  = [s for s in swings if s.kind == "low"][-5:]

    best_setup = None

    # ── BULLISH SETUP: recent swing_low → swing_high, price pulling back ──
    for sh in reversed(highs):
        # Find most recent swing low BEFORE this swing high
        prior_lows = [sl for sl in lows if sl.index < sh.index]
        if not prior_lows:
            continue
        sl = max(prior_lows, key=lambda x: x.index)

        swing_low  = sl.price
        swing_high = sh.price
        rng = swing_high - swing_low

        range_pips = rng / pips_factor
        if not (min_range_pips <= range_pips <= max_range_pips):
            continue

        # Check price is in retracement zone (50%–78.6% pullback from swing_high)
        retrace_val = (swing_high - current_price) / rng  # 0 = at high, 1 = at low
        if ENTRY_ZONE_MIN <= retrace_val <= ENTRY_ZONE_MAX:
            best_setup = (swing_low, swing_high, "buy")
            break  # Take the most recent valid setup

    if best_setup:
        return best_setup

    # ── BEARISH SETUP: recent swing_high → swing_low, price bouncing ──
    for sl_pt in reversed(lows):
        prior_highs = [sh for sh in highs if sh.index < sl_pt.index]
        if not prior_highs:
            continue
        sh = max(prior_highs, key=lambda x: x.index)

        swing_high = sh.price
        swing_low  = sl_pt.price
        rng = swing_high - swing_low

        range_pips = rng / pips_factor
        if not (min_range_pips <= range_pips <= max_range_pips):
            continue

        retrace_val = (current_price - swing_low) / rng  # 0 = at low, 1 = at high
        if ENTRY_ZONE_MIN <= retrace_val <= ENTRY_ZONE_MAX:
            return (swing_low, swing_high, "sell")

    return None


# ── Fibonacci Level Calculation ───────────────────────────────────────────────

def calculate_fibo_levels(
    swing_low:  float,
    swing_high: float,
    direction:  str,
    symbol:     str = SYMBOL,
) -> FiboLevels:
    """
    Calculate all Fibonacci-derived price levels from a swing pair.

    For BULLISH setup (price pulled back, we expect up move):
      entry  = swing_high - range * 0.618  (= swing_low + range * 0.382)
      tp1    = swing_high - range * 0.382  (= swing_low + range * 0.618)
      tp2    = swing_high                  (complete retrace reversal)
      tp3    = swing_high + range * 0.272  (127.2% extension)
      sl     = swing_low - buffer

    For BEARISH setup (price bounced, we expect down move):
      entry  = swing_low + range * 0.618   (= swing_high - range * 0.382)
      tp1    = swing_low + range * 0.382   (= swing_high - range * 0.618)
      tp2    = swing_low                   (complete retrace reversal)
      tp3    = swing_low - range * 0.272   (127.2% extension down)
      sl     = swing_high + buffer
    """
    rng = swing_high - swing_low
    buf = SL_BUFFER_PIPS * 0.1  # Convert pips to price

    # All retrace lines (measured from swing_low = 0%, swing_high = 100%)
    lvl = {k: swing_low + rng * v for k, v in FIBO_RATIOS.items() if k.startswith("r")}
    # Extension lines  
    lvl["ext127"] = swing_low + rng * FIBO_RATIOS["ext127"]  # 127.2% (beyond swing_high)
    lvl["ext161"] = swing_low + rng * FIBO_RATIOS["ext161"]  # 161.8%

    if direction == "buy":
        entry       = swing_low  + rng * 0.382   # = 61.8% pullback from swing_high
        stop_loss   = swing_low  - buf
        tp1         = swing_low  + rng * 0.618   # = 38.2% pullback
        tp2         = swing_high                  # = 0% pullback
        tp3         = swing_high + rng * 0.272   # 127.2% ext
        tp_ext      = swing_high + rng * 0.618   # 161.8% ext
    else:  # sell
        entry       = swing_high - rng * 0.382   # = 61.8% bounce from swing_low
        stop_loss   = swing_high + buf
        tp1         = swing_high - rng * 0.618   # = 38.2% bounce
        tp2         = swing_low                   # = 0% bounce
        tp3         = swing_low  - rng * 0.272   # 127.2% ext down
        tp_ext      = swing_low  - rng * 0.618   # 161.8% ext down

    return FiboLevels(
        signal_id=f"FIBO-{str(uuid.uuid4())[:6].upper()}",
        symbol=symbol,
        direction=direction,
        timestamp=datetime.now(timezone.utc),
        swing_low=round(swing_low, 2),
        swing_high=round(swing_high, 2),
        entry=round(entry, 2),
        stop_loss=round(stop_loss, 2),
        tp1=round(tp1, 2),
        tp2=round(tp2, 2),
        tp3=round(tp3, 2),
        tp_extension=round(tp_ext, 2),
        level_0=round(lvl["r0"], 2),
        level_236=round(lvl["r236"], 2),
        level_382=round(lvl["r382"], 2),
        level_500=round(lvl["r500"], 2),
        level_618=round(lvl["r618"], 2),
        level_786=round(lvl["r786"], 2),
        level_100=round(lvl["r100"], 2),
        signal_tier="A",      # Default; overridden below
        lot_multiplier=1.0,
        rsi_value=50.0,
        score=8,              # Default score; fibo signals are high quality
    )


# ── RSI Strict Gate ───────────────────────────────────────────────────────────

def check_rsi_strict_gate(df_m5: pd.DataFrame, direction: str) -> Tuple[bool, float]:
    """
    Strict RSI bounce confirmation gate.

    Buy:  RSI must have TOUCHED < 30 (oversold), then bounced back ABOVE 33.
    Sell: RSI must have TOUCHED > 70 (overbought), then dropped back BELOW 67.

    The touch must have happened within the last RSI_LOOKBACK candles.
    Current RSI must be on the bounce side (confirming the reversal).

    Returns:
        (passed: bool, current_rsi: float)
    """
    if df_m5 is None or len(df_m5) < RSI_PERIOD + RSI_LOOKBACK:
        return False, 50.0

    rsi = _calc_rsi(df_m5)
    recent_rsi    = rsi.iloc[-(RSI_LOOKBACK + 1):-1]  # Lookback window (excl. current)
    current_rsi   = rsi.iloc[-1]

    if direction == "buy":
        # Must have touched oversold zone
        touched_oversold = (recent_rsi < RSI_OVERSOLD).any()
        # Current RSI must be bouncing above exit threshold
        bounced = current_rsi > RSI_OVERSOLD_EXIT
        passed = touched_oversold and bounced
        if passed:
            min_rsi = recent_rsi.min()
            logger.debug(f"RSI Buy Gate PASS: min={min_rsi:.1f} current={current_rsi:.1f}")
        else:
            logger.debug(
                f"RSI Buy Gate FAIL: touched={touched_oversold} "
                f"bounced={bounced} current={current_rsi:.1f}"
            )
    else:  # sell
        touched_overbought = (recent_rsi > RSI_OVERBOUGHT).any()
        bounced = current_rsi < RSI_OVERBOUGHT_EXIT
        passed = touched_overbought and bounced
        if passed:
            max_rsi = recent_rsi.max()
            logger.debug(f"RSI Sell Gate PASS: max={max_rsi:.1f} current={current_rsi:.1f}")
        else:
            logger.debug(
                f"RSI Sell Gate FAIL: touched={touched_overbought} "
                f"bounced={bounced} current={current_rsi:.1f}"
            )

    return passed, round(float(current_rsi), 2)


# ── M1 Momentum Tier Detection ────────────────────────────────────────────────

def _is_engulfing(candle: pd.Series, prev: pd.Series, direction: str) -> bool:
    """Bullish/Bearish engulfing: current body fully contains previous body."""
    curr_open, curr_close = candle["open"], candle["close"]
    prev_open, prev_close = prev["open"], prev["close"]
    if direction == "buy":
        # Bullish engulfing: current closes higher, body engulfs previous red body
        return (curr_close > curr_open and                # current is bullish
                prev_close < prev_open and                # previous is bearish
                curr_close >= prev_open and               # engulfs previous top
                curr_open  <= prev_close)                 # engulfs previous bottom
    else:
        return (curr_close < curr_open and
                prev_close > prev_open and
                curr_close <= prev_open and
                curr_open  >= prev_close)


def _is_strong_close(candle: pd.Series, direction: str, body_ratio: float = 0.60) -> bool:
    """
    Strong close: candle body > 60% of total candle range.
    For buy: close in upper 60% of candle range.
    For sell: close in lower 60% of candle range.
    """
    total_range = candle["high"] - candle["low"]
    if total_range < 1e-8:
        return False
    body = abs(candle["close"] - candle["open"])
    body_pct = body / total_range

    if body_pct < body_ratio:
        return False

    if direction == "buy":
        return candle["close"] > candle["open"]  # bullish close
    else:
        return candle["close"] < candle["open"]  # bearish close


def _is_pin_bar(candle: pd.Series, direction: str) -> bool:
    """
    Pin Bar: long wick on the rejection side, small body on the other.
    Wick must be >= 2x body.
    """
    total_range = candle["high"] - candle["low"]
    if total_range < 1e-8:
        return False
    body = abs(candle["close"] - candle["open"])

    if direction == "buy":
        lower_wick = min(candle["open"], candle["close"]) - candle["low"]
        return lower_wick >= body * 2.0 and lower_wick >= total_range * 0.5
    else:
        upper_wick = candle["high"] - max(candle["open"], candle["close"])
        return upper_wick >= body * 2.0 and upper_wick >= total_range * 0.5


def detect_m1_momentum_tier(
    df_m1: pd.DataFrame,
    direction: str,
) -> Optional[str]:
    """
    Detect M1 momentum candle tier.

    Tier A: Engulfing + Strong Close (body > 60%) → full size
    Tier B: Pin Bar only / Weak Engulfing           → half size
    None:   Doji / inside bar / no confirmation     → skip

    Returns: "A", "B", or None
    """
    if df_m1 is None or len(df_m1) < 3:
        return None

    candle = df_m1.iloc[-2]   # Last CLOSED candle (not live)
    prev   = df_m1.iloc[-3]

    engulf       = _is_engulfing(candle, prev, direction)
    strong_close = _is_strong_close(candle, direction)
    pin_bar      = _is_pin_bar(candle, direction)

    # Doji check (body < 10% range = indecision)
    total_range = candle["high"] - candle["low"]
    body = abs(candle["close"] - candle["open"])
    is_doji = (body / total_range < 0.10) if total_range > 1e-8 else True

    # Inside bar (current high < prev high AND current low > prev low)
    is_inside = (candle["high"] < prev["high"] and candle["low"] > prev["low"])

    if is_doji or is_inside:
        logger.debug(f"M1 REJECT: doji={is_doji} inside={is_inside}")
        return None

    # Tier A: Engulfing + Strong Close (clear conviction)
    if engulf and strong_close:
        logger.debug("M1 Tier A: Engulfing + Strong Close")
        return "A"

    # Tier B: Single signal + some momentum
    if engulf or (pin_bar and strong_close):
        logger.debug(f"M1 Tier B: engulf={engulf} pin={pin_bar} strong={strong_close}")
        return "B"

    logger.debug(f"M1 REJECT: no valid pattern (engulf={engulf} pin={pin_bar} strong={strong_close})")
    return None


# ── Main Signal Generator ─────────────────────────────────────────────────────

class FiboEngine:
    """
    Main Fibonacci MTF Strategy Engine.

    Usage in main loop:
        fibo = FiboEngine()
        signal = fibo.generate(df_m15, df_m5, df_m1)
        if signal:
            order = risk_executor.evaluate(signal, ...)
            trade_id = order_manager.execute(order, df_m15=df_m15)
    """

    def generate(
        self,
        df_m15: pd.DataFrame,
        df_m5:  pd.DataFrame,
        df_m1:  pd.DataFrame,
        symbol: str = SYMBOL,
    ) -> Optional[FiboLevels]:
        """
        Full pipeline: Swing detect → Fibo levels → RSI gate → M1 trigger.

        Returns FiboLevels (which is Signal-compatible) or None.
        """
        # ── Step 1: Detect swing for Fibo setup ──────────────────────────────
        swing = detect_swing_for_fibo(df_m15)
        if swing is None:
            logger.debug("FiboEngine: No valid swing found in M15")
            return None

        swing_low, swing_high, direction = swing
        logger.debug(
            f"FiboEngine: Swing found {direction.upper()} "
            f"[{swing_low:.2f} → {swing_high:.2f}]"
        )

        # ── Step 2: RSI strict gate (M5) ─────────────────────────────────────
        rsi_ok, rsi_val = check_rsi_strict_gate(df_m5, direction)
        if not rsi_ok:
            logger.debug(f"FiboEngine: RSI gate FAILED (RSI={rsi_val:.1f})")
            return None

        # ── Step 3: M1 momentum tier ─────────────────────────────────────────
        tier = detect_m1_momentum_tier(df_m1, direction)
        if tier is None:
            logger.debug("FiboEngine: No M1 momentum trigger")
            return None

        # ── Step 4: Build FiboLevels ─────────────────────────────────────────
        fibo = calculate_fibo_levels(swing_low, swing_high, direction, symbol)
        fibo.signal_tier    = tier
        fibo.lot_multiplier = 1.0 if tier == "A" else 0.5
        fibo.rsi_value      = rsi_val
        fibo.score          = 9 if tier == "A" else 7  # High score for executor filter
        fibo.score_breakdown = {
            "fibo_setup": 3,
            "rsi_bounce": 3,
            "m1_momentum": 3 if tier == "A" else 1,
        }

        logger.info(
            f"🎯 Fibo Signal [{fibo.signal_id}]: {direction.upper()} "
            f"Tier={tier} entry={fibo.entry:.2f} sl={fibo.stop_loss:.2f} "
            f"tp1={fibo.tp1:.2f} tp2={fibo.tp2:.2f} tp3={fibo.tp3:.2f} "
            f"RSI={rsi_val:.1f}"
        )
        return fibo
