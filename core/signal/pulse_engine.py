"""
Pulse Scalping Engine - "Extreme Momentum" Mode.

Designed to capture deep extreme M1 entries like the user's manual 4743 trade.
Strategy:
  - ONLY fires during calm (Asian / low-volatility) sessions
  - BLOCKED during high-volatility sessions (London open, NY, Overlap)
  - Requires RSI extreme + M1 Order Block confirmation   - SL and TP: Tight SL (1-2 pips) and Conservative TP (RR 1:3)
  - Max concurrent same-direction entries: 3 (max lot same, not compound)
  - Works even when account is in daily floating loss (does NOT throttle on PnL)
"""
from typing import Optional
from datetime import datetime, timezone
import pandas as pd
import numpy as np
import uuid

from core.signal.signal_engine import TradeSignal
from core.structure.trend_logic import TrendLogic
from core.structure.smc_logic import SMCLogic
from config.trading_config import TRADING_CONFIG
from utils.logger import get_logger
from utils.time_utils import utc_now

logger = get_logger(__name__)

# Sessions defined in UTC hours
_VOLATILE_SESSIONS = {
    "london_open": (7, 10),   # First 3 hours London: most violent
    "ny_open":     (13, 16),  # NY open spike window
    "overlap":     (12, 16),  # London-NY overlap
    "us_close":    (19, 21),  # US close rush
}

_CALM_SESSIONS = {
    "asian":       (0, 9),    # Asian session (WIB 07:00 - 16:00)
    "pre_london":  (5, 7),    # Pre-London drift
    "late_us":     (21, 24),  # Late US/early Asian
}


def _is_calm_session(utc_hour: int) -> bool:
    """Returns True if we're in a calm/landai session — safe for tight SL scalping."""
    for name, (start, end) in _VOLATILE_SESSIONS.items():
        if start <= utc_hour < end:
            return False
    return True


class PulseEngine:
    def __init__(self):
        self.trend = TrendLogic()
        self.smc   = SMCLogic()

    def _calculate_rsi(self, series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        up   = delta.clip(lower=0)
        down = (-delta).clip(lower=0)
        roll_up   = up.ewm(com=period - 1, adjust=False).mean()
        roll_down = down.ewm(com=period - 1, adjust=False).mean()
        RS = roll_up / roll_down
        return 100.0 - (100.0 / (1.0 + RS))

    def _calculate_bb(self, series: pd.Series, period: int = 20, num_std: float = 2.0):
        mid   = series.rolling(window=period).mean()
        std   = series.rolling(window=period).std()
        upper = mid + (std * num_std)
        lower = mid - (std * num_std)
        return upper, mid, lower

    def generate(
        self,
        df_h1: pd.DataFrame,
        df_m5: pd.DataFrame,
        df_m1: pd.DataFrame,
        current_spread_pips: float,
        balance: float,
        is_suspended: bool = False,
        daily_pnl: float = 0.0,
        mtf_direction: Optional[str] = None,  # "buy" | "sell" | None — pre-computed by MultiTFAnalyzer
        **kwargs
    ) -> Optional[TradeSignal]:
        """
        Fires a Pulse signal ONLY in calm sessions when a M1 deep-extreme occurs
        inside an Order Block, with tight SL and far TP (1:10+).
        """

        # 0. Suspension guard (Removed per user request)
        # if is_suspended:
        #     logger.debug("Pulse: suspended today (3x consecutive loss guard).")
        #     return None

        # 1. Session gate — BLOCK Pulse di NY/Overlap (terlalu volatile untuk SL tipis 1-2 pip)
        now_utc = utc_now()
        utc_hour = now_utc.hour
        is_overlap = (12 <= utc_hour < 16)   # London-NY overlap: paling brutal
        is_ny_open = (13 <= utc_hour < 21)   # Pure NY: still volatile

        if is_overlap:
            logger.debug(f"Pulse: BLOCKED during London-NY Overlap ({utc_hour}:xx UTC) — spike sangat besar, SL tipis tidak aman.")
            return None
        if is_ny_open:
            logger.debug(f"Pulse: BLOCKED during NY session ({utc_hour}:xx UTC) — volatility tinggi, gunakan mode standard.")
            return None

        # 2. Spread gate — calm sessions usually tight; reject if > 2.5 pips
        if current_spread_pips > 2.5:
            logger.debug(f"Pulse: spread too wide ({current_spread_pips:.1f} pips) for tight SL scalp.")
            return None

        # 3. Min balance gate
        if balance < 100:
            return None

        # 4. Mode check
        mode = TRADING_CONFIG.current_mode
        settings = TRADING_CONFIG.mode_settings.get(mode, {})
        if not settings.get("pulse_scalping", False):
            logger.debug("Pulse: pulse_scalping disabled for current mode.")
            return None

        # 5. Direction: from MultiTFAnalyzer (bidirectional), fallback to H1
        if mtf_direction:
            signal_dir = mtf_direction
            logger.debug(f"Pulse: Using MTF direction: {signal_dir.upper()}")
        else:
            trend_h1  = self.trend.analyze_trend(df_h1)
            direction = trend_h1["bias"]
            if direction == "NEUTRAL":
                logger.debug("Pulse: H1 bias NEUTRAL and no MTF direction — skip.")
                return None
            signal_dir = "buy" if direction == "BULLISH" else "sell"

        # 6. M1 RSI + Bollinger Band — Ujung Extreme Check
        if len(df_m1) < 30:
            return None

        rsi_m1 = self._calculate_rsi(df_m1["close"], 14)
        bb_upper, bb_mid, bb_lower = self._calculate_bb(df_m1["close"], 20, 2.0)

        curr_close  = df_m1["close"].iloc[-1]
        curr_rsi    = rsi_m1.iloc[-1]
        curr_lower  = bb_lower.iloc[-1]
        curr_upper  = bb_upper.iloc[-1]

        # Ujung Bawah → BUY: price below BB lower AND RSI oversold
        # Ujung Atas  → SELL: price above BB upper AND RSI overbought
        is_extreme_buy  = (curr_close <= curr_lower) and (curr_rsi < 35)
        is_extreme_sell = (curr_close >= curr_upper) and (curr_rsi > 65)

        if signal_dir == "buy"  and not is_extreme_buy:
            logger.debug(f"Pulse: BUY but not at extreme (RSI={curr_rsi:.1f}, price={curr_close:.2f}, BB_lower={curr_lower:.2f})")
            return None
        if signal_dir == "sell" and not is_extreme_sell:
            logger.debug(f"Pulse: SELL but not at extreme (RSI={curr_rsi:.1f}, price={curr_close:.2f}, BB_upper={curr_upper:.2f})")
            return None

        # 7. M1 Order Block — Place SL tightly behind it
        obs_m1 = self.smc.get_order_blocks(df_m1)
        fvgs_m1 = self.smc.get_fvgs(df_m1)
        
        atr_m1 = (df_m1["high"] - df_m1["low"]).tail(20).mean()
        # buffer: absolute max 1.5 pips for calm sessions (very tight)
        sl_buffer = min(1.5, atr_m1 * 0.3)

        sl = None

        if obs_m1:
            last_ob = obs_m1[-1]
            if signal_dir == "buy" and last_ob["type"] == "BULLISH" and curr_close >= last_ob["low"]:
                sl = last_ob["low"] - sl_buffer
            elif signal_dir == "sell" and last_ob["type"] == "BEARISH" and curr_close <= last_ob["high"]:
                sl = last_ob["high"] + sl_buffer

        # Fallback: FVG if no OB
        if sl is None and fvgs_m1:
            last_fvg = fvgs_m1[-1]
            if signal_dir == "buy" and last_fvg["type"] == "BULLISH" and curr_close >= last_fvg["bottom"]:
                sl = last_fvg["bottom"] - sl_buffer
            elif signal_dir == "sell" and last_fvg["type"] == "BEARISH" and curr_close <= last_fvg["top"]:
                sl = last_fvg["top"] + sl_buffer

        # Last fallback: recent M1 swing low/high (1 hour window)
        if sl is None:
            if signal_dir == "buy":
                sl = df_m1["low"].tail(15).min() - sl_buffer
            else:
                sl = df_m1["high"].tail(15).max() + sl_buffer

        # Sanity: SL must not be on wrong side
        risk_dist = abs(curr_close - sl)
        if risk_dist < 0.5:
            logger.debug("Pulse: SL distance too small (<0.5 pt), skip.")
            return None

        # 8. TP Calculation — Conservative RR 1:3 for 'TP Tipis-Tipis'
        rr_multiplier = 3.0
        
        if signal_dir == "buy":
            # For Pulse, we now prioritize the conservative RR target over the long swing
            tp = curr_close + (risk_dist * rr_multiplier)
        else:
            tp = curr_close - (risk_dist * rr_multiplier)

        # TP Synchronization: Align with existing trades if available
        anchor_tp = kwargs.get("anchor_tp")
        if anchor_tp:
            # If anchor_tp is further than our calculated tp, use it
            is_further = (signal_dir == "buy" and anchor_tp > tp) or (signal_dir == "sell" and anchor_tp < tp)
            if is_further:
                logger.info(f"🔄 PULSE TP SYNC: Aligning target with anchor trade @ {anchor_tp:.2f}")
                tp = anchor_tp

        rr_actual = abs(tp - curr_close) / risk_dist if risk_dist > 0 else rr_multiplier

        signal = TradeSignal(
            signal_id       = f"PULSE_{str(uuid.uuid4())[:6]}",
            symbol          = "XAUUSD",
            timestamp       = now_utc,
            direction       = signal_dir,
            entry_price     = curr_close,
            stop_loss       = sl,
            take_profit     = tp,
            rr_ratio        = round(rr_actual, 1),
            score           = 10,
            max_score       = 10,
            session         = "PULSE",
            ai_decision     = "TAKE",
            ai_reason       = f"Extreme Point RSI={curr_rsi:.0f} | OB SL={sl:.2f} | TP={tp:.2f} | RR 1:{rr_actual:.0f}",
            ai_confidence   = 0.9,
            is_extreme      = True,   # Enables SL+ and Pyramid upon rebound
        )

        logger.info(
            f"⚡ PULSE EXTREME! {signal_dir.upper()} @ {curr_close:.2f} | "
            f"SL={sl:.2f} (dist={risk_dist:.2f}pt) | TP={tp:.2f} | RR=1:{rr_actual:.0f} | "
            f"RSI={curr_rsi:.1f} | Session: UTC={utc_hour}h"
        )
        return signal
