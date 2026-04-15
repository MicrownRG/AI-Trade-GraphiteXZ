"""
Pulse Scalping Engine — "Extreme Momentum" Mode.

Captures deep M1 extreme entries (e.g. RSI extreme + Order Block confluence)
during calm, low-volatility sessions.

Rules:
  - BLOCKED during US session hours (NY / London-NY Overlap): spike risk is too
    high for tight 1-2 pip SL.  SL floors in the executor handle wider SL for
    regular entries during those sessions.
  - ALLOWED during Asian session, Pre-London, and Late US (calm windows).
  - Requires RSI extreme + Bollinger Band breach on M1.
  - SL placed behind nearest M1 Order Block (or FVG fallback).
  - TP set using conservative RR: 1.5x for Asian, 2.5x for other calm sessions.
  - Enabled for AGGRESSIVE, VERY_AGGRESSIVE, and ULTRA_SCALPER modes.
  - Can operate in sideways / ranging markets (Asian session is typically ranging).
  - Max 2 concurrent Pulse positions (enforced in RiskExecutor).
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

# Volatile session windows (UTC hours) — Pulse is BLOCKED during these
_VOLATILE_SESSIONS = {
    "london_open": (7, 10),   # First 3 hours of London: violent opening moves
    "ny_open":     (13, 16),  # NY open spike window
    "overlap":     (12, 16),  # London-NY overlap: highest volatility
    "us_close":    (19, 21),  # US close rush: stop-hunt activity
}

# Calm session windows — Pulse is ALLOWED during these
_CALM_SESSIONS = {
    "asian":      (0, 9),    # Asian session: low volatility, often ranging
    "pre_london": (5, 7),    # Pre-London drift: quiet accumulation
    "late_us":    (21, 24),  # Late US / Early Asian transition
}


def _is_calm_session(utc_hour: int) -> bool:
    """Returns True if current hour is outside all volatile session windows."""
    for _name, (start, end) in _VOLATILE_SESSIONS.items():
        if start <= utc_hour < end:
            return False
    return True


class PulseEngine:
    def __init__(self):
        self.trend = TrendLogic()
        self.smc   = SMCLogic()

    def _calculate_rsi(self, series: pd.Series, period: int = 14) -> pd.Series:
        delta     = series.diff()
        up        = delta.clip(lower=0)
        down      = (-delta).clip(lower=0)
        roll_up   = up.ewm(com=period - 1, adjust=False).mean()
        roll_down = down.ewm(com=period - 1, adjust=False).mean()
        rs        = roll_up / roll_down
        return 100.0 - (100.0 / (1.0 + rs))

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
        mtf_direction: Optional[str] = None,  # "buy" | "sell" | None from MultiTFAnalyzer
        **kwargs,
    ) -> Optional[TradeSignal]:
        """
        Fire a Pulse signal only during calm sessions when a deep M1 extreme occurs
        inside an Order Block, with tight SL and conservative RR target.

        Volatile US session hours block Pulse automatically — the executor's
        session-aware SL floor still applies to regular entries during those hours.
        """

        # 1. Session gate — block Pulse during volatile US hours
        now_utc    = utc_now()
        utc_hour   = now_utc.hour
        is_overlap = (12 <= utc_hour < 16)
        is_ny_open = (13 <= utc_hour < 21)

        if is_overlap:
            logger.debug(
                f"Pulse: blocked during London-NY Overlap ({utc_hour}:xx UTC) "
                f"— spike risk too high for tight SL."
            )
            return None
        if is_ny_open:
            logger.debug(
                f"Pulse: blocked during NY session ({utc_hour}:xx UTC) "
                f"— use standard entry engine for US hours."
            )
            return None

        # 2. Spread gate — calm sessions have tight spreads; reject if too wide
        if current_spread_pips > 3.5:
            logger.debug(
                f"Pulse: spread {current_spread_pips:.1f} pips too wide for tight SL scalp."
            )
            return None

        # 3. Minimum balance gate
        if balance < 100:
            return None

        # 4. Mode gate — pulse_scalping must be enabled in mode settings
        mode     = TRADING_CONFIG.current_mode
        settings = TRADING_CONFIG.mode_settings.get(mode, {})
        if not settings.get("pulse_scalping", False):
            logger.debug(f"Pulse: disabled for mode {mode.value}.")
            return None

        # 5. Direction from MultiTFAnalyzer (bidirectional), fallback to H1 trend
        if mtf_direction:
            signal_dir = mtf_direction
            logger.debug(f"Pulse: using MTF direction {signal_dir.upper()}")
        else:
            trend_h1  = self.trend.analyze_trend(df_h1)
            direction = trend_h1["bias"]
            if direction == "NEUTRAL":
                logger.debug("Pulse: H1 bias NEUTRAL and no MTF direction — skip.")
                return None
            signal_dir = "buy" if direction == "BULLISH" else "sell"

        # 6. M1 RSI + Bollinger Band extreme check
        if len(df_m1) < 30:
            return None

        rsi_m1               = self._calculate_rsi(df_m1["close"], 14)
        bb_upper, _, bb_lower = self._calculate_bb(df_m1["close"], 20, 2.0)

        curr_close = df_m1["close"].iloc[-1]
        curr_rsi   = rsi_m1.iloc[-1]
        curr_lower = bb_lower.iloc[-1]
        curr_upper = bb_upper.iloc[-1]

        # Asian session is often ranging — use relaxed RSI thresholds (40/60)
        # Other calm sessions use tighter thresholds (35/65)
        is_asian = (0 <= utc_hour < 9)
        rsi_bot  = 40 if is_asian else 35
        rsi_top  = 60 if is_asian else 65

        is_extreme_buy  = (curr_close <= curr_lower) and (curr_rsi < rsi_bot)
        is_extreme_sell = (curr_close >= curr_upper) and (curr_rsi > rsi_top)

        if signal_dir == "buy" and not is_extreme_buy:
            logger.debug(
                f"Pulse: BUY not at extreme "
                f"(RSI={curr_rsi:.1f}, price={curr_close:.2f}, BB_lower={curr_lower:.2f})"
            )
            return None
        if signal_dir == "sell" and not is_extreme_sell:
            logger.debug(
                f"Pulse: SELL not at extreme "
                f"(RSI={curr_rsi:.1f}, price={curr_close:.2f}, BB_upper={curr_upper:.2f})"
            )
            return None

        # 7. SL placement — behind nearest M1 Order Block or FVG
        obs_m1  = self.smc.get_order_blocks(df_m1)
        fvgs_m1 = self.smc.get_fvgs(df_m1)

        atr_m1    = (df_m1["high"] - df_m1["low"]).tail(20).mean()
        # SL buffer: slightly larger for ranging Asian market to avoid noise sweeps
        sl_buffer = max(1.5, min(2.5, atr_m1 * 1.0))

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

        # Final fallback: recent M1 swing extreme (last 15 candles = ~15 min)
        if sl is None:
            if signal_dir == "buy":
                sl = df_m1["low"].tail(15).min() - sl_buffer
            else:
                sl = df_m1["high"].tail(15).max() + sl_buffer

        # Sanity check: SL distance must be meaningful
        risk_dist = abs(curr_close - sl)
        if risk_dist < 0.5:
            logger.debug("Pulse: SL distance < 0.5 points — skip.")
            return None

        # 8. TP calculation — Fibo extension (preferred), session RR fallback
        rr_multiplier = 1.5 if is_asian else 2.5
        tp_rr = (curr_close + risk_dist * rr_multiplier) if signal_dir == "buy" \
                else (curr_close - risk_dist * rr_multiplier)

        # Derive Fibo TP from M5 swing (Pulse operates on M1 extreme → M5 context)
        tp = tp_rr
        try:
            from core.structure.swing import detect_swings
            sw = detect_swings(df_m5.tail(80))
            highs = [s.price for s in sw if s.kind == 'high']
            lows  = [s.price for s in sw if s.kind == 'low']
            if highs and lows:
                sh, sl_pt = highs[-1], lows[-1]
                if sh > sl_pt:
                    rng = sh - sl_pt
                    if rng >= 0.5:
                        if signal_dir == "buy":
                            cand = sh + rng * 0.272
                            fibo_tp = cand if cand > curr_close else (sh + rng * 0.618)
                        else:
                            cand = sl_pt - rng * 0.272
                            fibo_tp = cand if cand < curr_close else (sl_pt - rng * 0.618)
                        fibo_rr = abs(fibo_tp - curr_close) / risk_dist if risk_dist > 0 else 0
                        # Use Fibo TP if RR ∈ [1.2, 3.5] — keep Pulse conservative
                        if 1.2 <= fibo_rr <= 3.5:
                            tp = fibo_tp
                            logger.info(
                                f"📐 PULSE Fibo TP @ {fibo_tp:.2f} (RR {fibo_rr:.2f}) — "
                                f"overrides session {rr_multiplier}x"
                            )
        except Exception:
            pass

        # TP synchronisation: align with existing trades if anchor is further
        anchor_tp = kwargs.get("anchor_tp")
        if anchor_tp:
            is_further = (
                (signal_dir == "buy"  and anchor_tp > tp) or
                (signal_dir == "sell" and anchor_tp < tp)
            )
            if is_further:
                logger.info(f"Pulse TP sync: aligning with anchor trade @ {anchor_tp:.2f}")
                tp = anchor_tp

        rr_actual = abs(tp - curr_close) / risk_dist if risk_dist > 0 else rr_multiplier

        signal = TradeSignal(
            signal_id     = f"PULSE_{str(uuid.uuid4())[:6]}",
            symbol        = "XAUUSD",
            timestamp     = now_utc,
            direction     = signal_dir,
            entry_price   = curr_close,
            stop_loss     = sl,
            take_profit   = tp,
            rr_ratio      = round(rr_actual, 1),
            score         = 10,
            max_score     = 10,
            session       = "PULSE",
            ai_decision   = "TAKE",
            ai_reason     = (
                f"Extreme RSI={curr_rsi:.0f} | OB SL={sl:.2f} | "
                f"TP={tp:.2f} | RR 1:{rr_actual:.0f}"
            ),
            ai_confidence = 0.9,
            is_extreme    = True,  # enables SL+ and pyramid on rebound
        )

        logger.info(
            f"PULSE EXTREME: {signal_dir.upper()} @ {curr_close:.2f} | "
            f"SL={sl:.2f} (dist={risk_dist:.2f}pt) | TP={tp:.2f} | "
            f"RR=1:{rr_actual:.0f} | RSI={curr_rsi:.1f} | UTC={utc_hour}h"
        )
        return signal
