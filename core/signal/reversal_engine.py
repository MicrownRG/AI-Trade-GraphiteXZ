"""
Reversal & Spike-Retest Engine.

Two specific reversal scenarios:
  1. SPIKE-DOWN -> wait -> RETEST (Wyckoff Spring / Post-Impulse Retest)
  2. SPIKE-DOWN -> stabilise -> SLOW RECOVERY -> buy entry (Exhaustion Recovery)

Conditions:
  - Detect "Impulse Bar" (candle body > 2x ATR)
  - Enter WAIT mode for N candles after the impulse
  - Entry when price retests 38–61.8% Fibonacci zone OR after N reversal candles

Components:
  - ImpulseRetestEngine: state machine managing WATCHING -> WAITING -> READY
  - Consecutive Candle Fade: 7+ same-direction candles -> reversal setup

Strategy references:
  - Plan No.11 Consecutive Candle Fade
  - Plan No.33 Wyckoff Spring Detection (adapted)
  - Plan No.35 Z-Score MA Revert
  - Plan No.65 MACD Histogram Acceleration
"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import pandas as pd
import numpy as np

from core.signal.signal_engine import TradeSignal
from config.trading_config import TRADING_CONFIG, TradeMode
from utils.logger import get_logger
from utils.time_utils import utc_now

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
IMPULSE_ATR_MULTIPLIER   = 2.0   # candle body > 2x ATR → impulse bar
RETEST_FIBO_LOW          = 0.382 # retest entry zone: 38.2% retracement
RETEST_FIBO_HIGH         = 0.618 # retest entry zone: 61.8% retracement
WAIT_CANDLES_AFTER_SPIKE = 3     # candles to wait before checking retest
CONFIRM_CANDLES_REVERSAL = 2     # candles closing in reversal direction needed
MIN_IMPULSE_PIPS         = 8.0   # minimum impulse size (pips) to qualify
CONSECUTIVE_FADE_THRESH  = 7     # No.11: N candles same direction triggers fade
MACH_SHRINK_COUNT        = 3     # No.65: N bars of MACD hist shrinking triggers
MACH_SHRINK_COUNT        = 3     # keep alias
MACD_SHRINK_COUNT        = 3
RECOVERY_ATR_RATIO       = 1.2   # Scenario 2: ATR must cool below this ratio for entry
RECOVERY_MIN_BARS        = 3     # minimum recovery bars required (slow ascending close series)

# ── Session-specific overrides ────────────────────────────────────────────────
_SESSION_PARAMS = {
    # (confirm_candles, rr_mult_retest, rr_mult_fade, sl_atr_factor, fade_thresh)
    "OVERLAP": (4, 3.5, 2.5, 0.6, 9),   # 12-16 UTC: most volatile — requires 4 confirmations
    "NY":      (3, 3.0, 2.0, 0.5, 8),   # 13-21 UTC: volatile — 3 confirmations
    "LONDON":  (2, 2.5, 1.8, 0.4, 7),   # 07-16 UTC: active but structured
    "ASIA":    (2, 2.0, 1.5, 0.3, 6),   # 00-09 UTC: quiet — more aggressive entries OK
    "DEFAULT": (2, 2.5, 1.7, 0.4, 7),   # transition / late US
}


@dataclass
class ImpulseEvent:
    """Records a detected impulse bar event."""
    direction: str          # "down" or "up" (impulse direction)
    impulse_high: float     # candle's high
    impulse_low: float      # candle's low
    impulse_body: float     # candle body size
    detected_at_idx: int    # bar index when detected
    retest_zone_low: float  # 38.2% from bottom
    retest_zone_high: float # 61.8% from bottom
    atr_at_detection: float


class ImpulseRetestEngine:
    """
    State machine that detects large impulse spikes and manages
    entry timing for retest or exhaustion-recovery setups.

    State: WATCHING → WAITING → READY_TO_ENTER → RESET
    """

    def __init__(self):
        self._last_impulse: Optional[ImpulseEvent] = None
        self._wait_counter: int = 0
        self._state: str = "WATCHING"  # WATCHING | WAITING | READY
        self._confirm_count: int = 0

    def reset(self) -> None:
        self._last_impulse = None
        self._wait_counter = 0
        self._state = "WATCHING"
        self._confirm_count = 0

    def _get_session_params(self) -> tuple:
        """
        Returns (confirm_candles, rr_retest, rr_fade, sl_atr_factor, fade_thresh)
        based on current UTC session.
        """
        hour = utc_now().hour
        if 12 <= hour < 16:   return _SESSION_PARAMS["OVERLAP"]  # most brutal session
        if 13 <= hour < 21:   return _SESSION_PARAMS["NY"]
        if 7  <= hour < 16:   return _SESSION_PARAMS["LONDON"]
        if 0  <= hour < 9:    return _SESSION_PARAMS["ASIA"]
        return _SESSION_PARAMS["DEFAULT"]

    # ── Core method ───────────────────────────────────────────────────────────

    def generate(
        self,
        df_m1: pd.DataFrame,
        df_m5: pd.DataFrame,
        df_h1: pd.DataFrame,
        current_spread_pips: float = 2.0,
    ) -> Optional[TradeSignal]:
        """
        Analyze recent bars for:
          1. Impulse-Retest Entry
          2. Exhaustion Recovery Entry
          3. Consecutive Candle Fade
          4. MACD Histogram Acceleration Reversal

        Returns a TradeSignal or None.
        """
        mode = TRADING_CONFIG.current_mode
        settings = TRADING_CONFIG.mode_settings.get(mode, {})

        # Get session-specific parameters
        confirm_candles, rr_retest, rr_fade, sl_atr_factor, fade_thresh = self._get_session_params()
        hour = utc_now().hour
        is_ny_session = (13 <= hour < 21)
        is_overlap    = (12 <= hour < 16)

        # Only fire for aggressive modes (includes reversals by design)
        if mode not in (
            TradeMode.ULTRA_SCALPER, TradeMode.VERY_AGGRESSIVE,
            TradeMode.AGGRESSIVE, TradeMode.MODERATE,
        ):
            return None

        if len(df_m1) < 50 or len(df_m5) < 30:
            return None

        # Spread guard — tighter during NY/Overlap where spread widens
        max_spread = 4.0 if (is_ny_session or is_overlap) else 3.0
        if current_spread_pips > max_spread:
            return None

        # Try each reversal strategy in order of aggressiveness
        signal = (
            self._check_impulse_retest(df_m1, df_m5, rr_retest, sl_atr_factor) or
            self._check_exhaustion_recovery(df_m1, df_m5, confirm_candles) or
            self._check_consecutive_fade(df_m1, rr_fade, fade_thresh) or
            self._check_macd_reversal(df_m5, df_h1, rr_fade)
        )

        return signal

    # ── Strategy 1: Impulse-Retest ────────────────────────────────────────────

    def _check_impulse_retest(
        self,
        df_m1: pd.DataFrame,
        df_m5: pd.DataFrame,
        rr_mult: float = 2.5,
        sl_atr_factor: float = 0.3,
    ) -> Optional[TradeSignal]:
        """
        Detect a large impulse candle (>2x ATR), wait, then enter when
        price retests the 38–61.8% Fibonacci zone of the impulse range.
        SL: beyond the spike extreme.
        """
        closes = df_m1["close"]
        highs  = df_m1["high"]
        lows   = df_m1["low"]
        opens  = df_m1["open"]

        # ATR M1 (20 period)
        atr = (highs - lows).tail(20).mean()
        if atr <= 0:
            return None

        # Look for impulse in last 5 bars (excluding current)
        look_back = min(8, len(df_m1) - 2)
        impulse: Optional[ImpulseEvent] = None

        for i in range(2, look_back + 1):
            idx = -i
            body = abs(closes.iloc[idx] - opens.iloc[idx])
            candle_dir = "down" if closes.iloc[idx] < opens.iloc[idx] else "up"

            if body < atr * IMPULSE_ATR_MULTIPLIER:
                continue

            # Qualify: minimum pip size
            pips = body / 0.1
            if pips < MIN_IMPULSE_PIPS:
                continue

            c_high = highs.iloc[idx]
            c_low  = lows.iloc[idx]
            c_range = c_high - c_low

            # Fibonacci retest zone
            if candle_dir == "down":
                # Spike down → expect buy retest
                # Zone: 38.2–61.8% from LOW back toward HIGH
                zone_low  = c_low + c_range * RETEST_FIBO_LOW
                zone_high = c_low + c_range * RETEST_FIBO_HIGH
            else:
                # Spike up → expect sell retest
                zone_high = c_high - c_range * RETEST_FIBO_LOW
                zone_low  = c_high - c_range * RETEST_FIBO_HIGH

            impulse = ImpulseEvent(
                direction     = candle_dir,
                impulse_high  = c_high,
                impulse_low   = c_low,
                impulse_body  = body,
                detected_at_idx = len(df_m1) + idx,
                retest_zone_low  = zone_low,
                retest_zone_high = zone_high,
                atr_at_detection = atr,
            )
            break  # Take the most recent impulse

        if impulse is None:
            return None

        # Current price
        current = closes.iloc[-1]
        bars_since = abs(impulse.detected_at_idx - len(df_m1))

        # Must wait at least WAIT_CANDLES before taking retest entry
        if bars_since < WAIT_CANDLES_AFTER_SPIKE:
            logger.debug(
                f"ImpulseRetest: {impulse.direction.upper()} spike detected, "
                f"waiting ({bars_since}/{WAIT_CANDLES_AFTER_SPIKE} bars)..."
            )
            return None

        # Check if current price is in retest zone
        in_retest = impulse.retest_zone_low <= current <= impulse.retest_zone_high

        if not in_retest:
            return None

        # Entry direction is OPPOSITE of spike direction
        entry_dir = "buy" if impulse.direction == "down" else "sell"

        # SL: beyond the extreme of the impulse candle (+ session-aware buffer)
        sl_buffer = max(0.5, atr * sl_atr_factor)
        if entry_dir == "buy":
            sl = impulse.impulse_low - sl_buffer
        else:
            sl = impulse.impulse_high + sl_buffer

        risk_dist = abs(current - sl)
        if risk_dist < 0.5:
            return None

        # TP: session-aware RR
        tp = (current + risk_dist * rr_mult) if entry_dir == "buy" \
             else (current - risk_dist * rr_mult)

        logger.info(
            f"⚡ IMPULSE-RETEST: {entry_dir.upper()} @ {current:.2f} | "
            f"Spike {impulse.direction.upper()} {impulse.impulse_body/0.1:.0f}pip | "
            f"Retest zone [{impulse.retest_zone_low:.2f}-{impulse.retest_zone_high:.2f}] | "
            f"SL={sl:.2f} TP={tp:.2f} RR=1:{rr_mult}"
        )

        return self._make_signal(
            direction=entry_dir,
            entry_price=current,
            sl=sl,
            tp=tp,
            rr=rr_mult,
            reason=f"ImpulseRetest: {impulse.direction.upper()} spike {impulse.impulse_body/0.1:.0f}pip → retest {RETEST_FIBO_LOW*100:.0f}-{RETEST_FIBO_HIGH*100:.0f}% zone",
            score_bonus=3,
        )

    # ── Strategy 2: Exhaustion Recovery (Slow Climb) ─────────────────────────

    def _check_exhaustion_recovery(
        self,
        df_m1: pd.DataFrame,
        df_m5: pd.DataFrame,
        confirm_candles: int = RECOVERY_MIN_BARS,
    ) -> Optional[TradeSignal]:
        """
        Scenario 2: After a sharp spike down, volatility cools (ATR drops),
        then price climbs slowly for RECOVERY_MIN_BARS candles → buy entry.

        Conditions:
          - Impulse-down candle within the last 10 bars
          - M1 ATR is now < 1.2x baseline ATR (volatility cooled)
          - Last N closes form an ascending series
        """
        closes = df_m1["close"]
        lows   = df_m1["low"]
        highs  = df_m1["high"]
        opens  = df_m1["open"]

        # ATR comparison: recent vs historical
        atr_recent = (highs - lows).tail(5).mean()
        atr_normal = (highs - lows).tail(20).mean()

        if atr_normal <= 0:
            return None

        atr_ratio = atr_recent / atr_normal

        # Volatility must have cooled
        if atr_ratio > RECOVERY_ATR_RATIO:
            # Still too volatile
            return None

        # Check for prior spike down in last 10 bars
        had_spike_down = False
        for i in range(3, 11):
            if i >= len(df_m1):
                break
            body = abs(closes.iloc[-i] - opens.iloc[-i])
            atr_at = (highs - lows).iloc[-i-5:-i].mean() if i + 5 < len(df_m1) else atr_normal
            if atr_at > 0 and body > atr_at * IMPULSE_ATR_MULTIPLIER:
                down_candle = closes.iloc[-i] < opens.iloc[-i]
                if down_candle:
                    had_spike_down = True
                    break

        if not had_spike_down:
            return None

        # Check last N closes are ascending (recovery pattern)
        # confirm_candles is session-aware: NY=3, Asia=2
        recent_closes = closes.iloc[-confirm_candles:]
        is_ascending = all(
            recent_closes.iloc[j] > recent_closes.iloc[j - 1]
            for j in range(1, len(recent_closes))
        )

        if not is_ascending:
            return None

        current = closes.iloc[-1]
        atr_m1  = atr_normal

        # SL: below the recent swing low
        sl_candidate = lows.tail(RECOVERY_MIN_BARS + 2).min()
        sl_buffer    = max(0.3, atr_m1 * 0.2)
        sl = sl_candidate - sl_buffer

        risk_dist = abs(current - sl)
        if risk_dist < 0.5:
            return None

        rr_mult = 2.0
        tp = current + risk_dist * rr_mult

        logger.info(
            f"🔄 EXHAUSTION RECOVERY: BUY @ {current:.2f} | "
            f"ATR ratio={atr_ratio:.2f} (cooled) | "
            f"{RECOVERY_MIN_BARS} ascending bars | SL={sl:.2f} TP={tp:.2f}"
        )

        return self._make_signal(
            direction="buy",
            entry_price=current,
            sl=sl,
            tp=tp,
            rr=rr_mult,
            reason=f"ExhaustionRecovery: ATR cooled ({atr_ratio:.1f}x) + {RECOVERY_MIN_BARS} ascending closes post-spike",
            score_bonus=2,
        )

    # ── Strategy 3: Consecutive Candle Fade (No.11) ───────────────────────────

    def _check_consecutive_fade(
        self,
        df_m1: pd.DataFrame,
        rr_mult: float = 1.5,
        fade_thresh: int = CONSECUTIVE_FADE_THRESH,
    ) -> Optional[TradeSignal]:
        """
        No.11: When N consecutive candles run in the same direction (valid body, no doji),
        take a counter-entry with a tight SL just beyond the streak's extreme.
        Logic: momentum exhaustion — price tends to correct after a long directional run.
        """
        closes = df_m1["close"]
        opens  = df_m1["open"]
        highs  = df_m1["high"]
        lows   = df_m1["low"]

        atr = (highs - lows).tail(20).mean()
        min_body = atr * 0.3  # candle must have meaningful body (not doji)

        # Count consecutive same-direction closes
        streak_dir = None
        count = 0
        for i in range(1, fade_thresh + 3):
            if i >= len(df_m1):
                break
            body = closes.iloc[-i] - opens.iloc[-i]
            if abs(body) < min_body:
                break  # Doji breaks the streak
            candle_dir = "up" if body > 0 else "down"
            if streak_dir is None:
                streak_dir = candle_dir
            if candle_dir == streak_dir:
                count += 1
            else:
                break

        if count < fade_thresh or streak_dir is None:
            return None

        # Entry direction is OPPOSITE of streak
        entry_dir = "sell" if streak_dir == "up" else "buy"
        current   = closes.iloc[-1]

        # SL: beyond the extreme of the streak
        sl_buffer = max(0.3, atr * 0.2)
        if entry_dir == "sell":
            sl = highs.iloc[-count:].max() + sl_buffer
        else:
            sl = lows.iloc[-count:].min() - sl_buffer

        risk_dist = abs(current - sl)
        if risk_dist < 0.5:
            return None

        # TP: session-aware RR (passed in)
        tp = (current - risk_dist * rr_mult) if entry_dir == "sell" \
             else (current + risk_dist * rr_mult)

        logger.info(
            f"🔁 CONSECUTIVE FADE (No.11): {entry_dir.upper()} @ {current:.2f} | "
            f"{count} consecutive {streak_dir.upper()} candles | "
            f"SL={sl:.2f} TP={tp:.2f} RR=1:{rr_mult}"
        )

        return self._make_signal(
            direction=entry_dir,
            entry_price=current,
            sl=sl,
            tp=tp,
            rr=rr_mult,
            reason=f"ConsecutiveFade: {count}x {streak_dir.upper()} candles exhaustion → {entry_dir.upper()} counter",
            score_bonus=1,
        )

    # ── Strategy 4: MACD Histogram Acceleration (No.65) ──────────────────────

    def _check_macd_reversal(
        self,
        df_m5: pd.DataFrame,
        df_h1: pd.DataFrame,
        rr_mult: float = 1.8,
    ) -> Optional[TradeSignal]:
        """
        No.65: MACD histogram shrinks N consecutive bars →
        momentum weakening → enter in the reversal direction.

        Only fires when H1 bias does not strongly oppose the entry direction.
        """
        if len(df_m5) < 40:
            return None

        closes = df_m5["close"]
        highs  = df_m5["high"]
        lows   = df_m5["low"]

        # MACD calculation (12,26,9)
        ema12 = closes.ewm(span=12, adjust=False).mean()
        ema26 = closes.ewm(span=26, adjust=False).mean()
        macd  = ema12 - ema26
        signal_line = macd.ewm(span=9, adjust=False).mean()
        hist  = macd - signal_line

        # Check last N bars of histogram shrinking
        recent_hist = hist.iloc[-(MACD_SHRINK_COUNT + 2):]

        # Detect shrinking: absolute value of hist must decrease consecutively
        shrink_count = 0
        prev_abs = abs(recent_hist.iloc[-MACD_SHRINK_COUNT - 1])
        shrink_dir = None  # which side the hist was on before shrinking

        for i in range(MACD_SHRINK_COUNT, 0, -1):
            curr_abs = abs(recent_hist.iloc[-i])
            if curr_abs < prev_abs:
                shrink_count += 1
                if shrink_dir is None:
                    shrink_dir = "positive" if recent_hist.iloc[-i] > 0 else "negative"
            else:
                shrink_count = 0  # Reset if not monotonically shrinking
            prev_abs = curr_abs

        if shrink_count < MACD_SHRINK_COUNT or shrink_dir is None:
            return None

        # Entry direction OPPOSITE to the fading momentum
        # If positive hist was shrinking → bullish momentum fading → SELL
        # If negative hist was shrinking → bearish momentum fading → BUY
        entry_dir = "sell" if shrink_dir == "positive" else "buy"

        current = closes.iloc[-1]
        atr = (highs - lows).tail(20).mean()

        # Basic SL based on recent swing
        sl_buffer = max(0.5, atr * 0.4)
        if entry_dir == "sell":
            sl = highs.tail(5).max() + sl_buffer
        else:
            sl = lows.tail(5).min() - sl_buffer

        risk_dist = abs(current - sl)
        if risk_dist < 0.5:
            return None

        rr_mult = 1.8
        tp = (current - risk_dist * rr_mult) if entry_dir == "sell" \
             else (current + risk_dist * rr_mult)

        logger.info(
            f"📉 MACD HIST ACCEL (No.65): {entry_dir.upper()} @ {current:.2f} | "
            f"{MACD_SHRINK_COUNT}x consecutive hist shrink ({shrink_dir}) | "
            f"SL={sl:.2f} TP={tp:.2f} RR=1:{rr_mult}"
        )

        return self._make_signal(
            direction=entry_dir,
            entry_price=current,
            sl=sl,
            tp=tp,
            rr=rr_mult,
            reason=f"MACDHistAccel: {MACD_SHRINK_COUNT}x {shrink_dir} hist shrinking → {entry_dir.upper()} momentum exhaustion",
            score_bonus=2,
        )

    # ── Factory ───────────────────────────────────────────────────────────────

    @staticmethod
    def _make_signal(
        direction: str,
        entry_price: float,
        sl: float,
        tp: float,
        rr: float,
        reason: str,
        score_bonus: int = 0,
    ) -> TradeSignal:
        """Create a standardized TradeSignal for reversal entries."""
        from utils.time_utils import utc_now, get_session_name
        now = utc_now()
        return TradeSignal(
            signal_id       = f"REV_{str(uuid.uuid4())[:6]}",
            symbol          = "XAUUSD",
            timestamp       = now,
            direction       = direction,
            entry_price     = entry_price,
            stop_loss       = sl,
            take_profit     = tp,
            rr_ratio        = rr,
            score           = 8 + score_bonus,   # Base 8 + strategy-specific bonus
            max_score       = 12,
            atr_pips        = abs(entry_price - sl) / 0.1,
            session         = get_session_name(now),
            is_extreme      = False,
            is_stalking     = False,
            stalking_reason = "",
            ai_decision     = "TAKE",
            ai_confidence   = 0.75,
            ai_reason       = reason,
        )
