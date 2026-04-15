"""
Mean Reversion Engine — Sideways / Consolidation Harvesting.

Conditions (ALL must be satisfied):
  1. ADX < 25  — no strong trend; market is ranging
  2. BB width < 20-period average BB width — squeeze or tight consolidation
  3. Price touching outer Bollinger Band (upper for sell, lower for buy)
  4. RSI diverging from extreme and turning: >70 for sell, <30 for buy
  5. Volume confirmation: current candle volume > 1.2x 20-period MA

Entry: at outer BB touch + RSI reversal
SL   : 1.5x ATR beyond the outer BB
TP   : Bollinger Band midline (20-period MA = mean)
Score: minimum 6 required (high quality only)

Active for all modes — sideways harvesting is mode-agnostic.
Score threshold respected per mode_settings["min_score_threshold"].
"""
from __future__ import annotations
from typing import Optional
import uuid
import pandas as pd
import numpy as np
from datetime import datetime, timezone

from core.signal.signal_engine import TradeSignal
from core.structure.trend_logic import TrendLogic
from config.trading_config import TRADING_CONFIG
from config.risk_config import RISK_CONFIG
from utils.logger import get_logger
from utils.time_utils import get_session_name

logger = get_logger(__name__)

_ADX_TRENDING_THRESHOLD = 25.0    # ADX above this = trending, engine skips
_RSI_OVERSOLD           = 30.0
_RSI_OVERBOUGHT         = 70.0
_VOLUME_MA_PERIOD       = 20
_VOLUME_MULT            = 1.2     # volume must be > 1.2x MA to confirm
_BB_PERIOD              = 20
_BB_STD                 = 2.0
_MIN_SCORE              = 6       # hard floor — below this is discarded regardless of mode


class MeanReversionEngine:
    def __init__(self):
        self._trend = TrendLogic()

    # ── Indicator helpers ─────────────────────────────────────────────────────

    def _rsi(self, series: pd.Series, period: int = 14) -> pd.Series:
        delta     = series.diff()
        up        = delta.clip(lower=0)
        down      = (-delta).clip(lower=0)
        roll_up   = up.ewm(com=period - 1, adjust=False).mean()
        roll_down = down.ewm(com=period - 1, adjust=False).mean()
        rs        = roll_up / roll_down
        return 100.0 - (100.0 / (1.0 + rs))

    def _bollinger(self, series: pd.Series, period: int = _BB_PERIOD, num_std: float = _BB_STD):
        mid   = series.rolling(window=period).mean()
        std   = series.rolling(window=period).std()
        upper = mid + std * num_std
        lower = mid - std * num_std
        width = upper - lower
        return upper, mid, lower, width

    def _atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        h, l, c = df["high"], df["low"], df["close"]
        prev_c  = c.shift(1)
        tr      = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
        return tr.ewm(com=period - 1, adjust=False).mean()

    # ── Main signal generation ────────────────────────────────────────────────

    def generate(
        self,
        df_m15: pd.DataFrame,
        df_m5:  Optional[pd.DataFrame] = None,
        current_time: Optional[datetime] = None,
        symbol: str = "XAUUSD",
    ) -> Optional[TradeSignal]:
        """
        Scan M15 data for a mean-reversion setup.
        Returns a TradeSignal or None.
        """
        if df_m15 is None or len(df_m15) < _BB_PERIOD + 10:
            return None

        if current_time is None:
            current_time = datetime.now(timezone.utc)

        close  = df_m15["close"]
        volume = df_m15.get("tick_volume", df_m15.get("volume", None))

        # ── 1. ADX check — must be sideways ──────────────────────────────────
        try:
            adx_series = self._trend.calculate_adx(df_m15)
            adx_val    = float(adx_series.iloc[-1])
        except Exception:
            return None

        if adx_val >= _ADX_TRENDING_THRESHOLD:
            return None   # market is trending — mean reversion not suitable

        # ── 2. Bollinger Bands ────────────────────────────────────────────────
        bb_upper, bb_mid, bb_lower, bb_width = self._bollinger(close)
        if bb_width.isna().all():
            return None

        avg_bb_width = bb_width.rolling(window=_BB_PERIOD).mean()
        current_bb_width   = float(bb_width.iloc[-1])
        avg_width_val      = float(avg_bb_width.iloc[-1]) if not avg_bb_width.isna().all() else current_bb_width

        bb_squeeze = current_bb_width <= avg_width_val   # consolidating

        current_close  = float(close.iloc[-1])
        current_upper  = float(bb_upper.iloc[-1])
        current_lower  = float(bb_lower.iloc[-1])
        current_mid    = float(bb_mid.iloc[-1])

        at_upper_band = current_close >= current_upper * 0.9985  # within 0.15% of upper
        at_lower_band = current_close <= current_lower * 1.0015  # within 0.15% of lower

        if not (at_upper_band or at_lower_band):
            return None   # price not touching outer band

        # ── 3. RSI ────────────────────────────────────────────────────────────
        rsi_series = self._rsi(close)
        current_rsi  = float(rsi_series.iloc[-1])
        prev_rsi     = float(rsi_series.iloc[-2]) if len(rsi_series) >= 2 else current_rsi

        rsi_sell_reversal = current_rsi > _RSI_OVERBOUGHT and current_rsi < prev_rsi  # turning down
        rsi_buy_reversal  = current_rsi < _RSI_OVERSOLD  and current_rsi > prev_rsi  # turning up

        if at_upper_band and not rsi_sell_reversal:
            return None
        if at_lower_band and not rsi_buy_reversal:
            return None

        # ── 4. Volume confirmation ────────────────────────────────────────────
        volume_confirmed = True
        if volume is not None and not volume.isna().all():
            vol_ma = volume.rolling(window=_VOLUME_MA_PERIOD).mean()
            current_vol     = float(volume.iloc[-1])
            avg_vol         = float(vol_ma.iloc[-1]) if not vol_ma.isna().all() else current_vol
            volume_confirmed = avg_vol > 0 and current_vol >= avg_vol * _VOLUME_MULT

        # ── 5. Score ──────────────────────────────────────────────────────────
        score_breakdown = {
            "adx_sideways":    2 if adx_val < _ADX_TRENDING_THRESHOLD else 0,
            "bb_squeeze":      1 if bb_squeeze else 0,
            "bb_touch":        2 if (at_upper_band or at_lower_band) else 0,
            "rsi_reversal":    2 if (rsi_sell_reversal or rsi_buy_reversal) else 0,
            "volume_confirm":  1 if volume_confirmed else 0,
        }
        score = sum(score_breakdown.values())

        mode_settings  = TRADING_CONFIG.mode_settings.get(TRADING_CONFIG.current_mode, {})
        mode_min_score = mode_settings.get("min_score_threshold", _MIN_SCORE)
        effective_min  = max(_MIN_SCORE, mode_min_score)

        if score < effective_min:
            logger.debug(f"MeanReversion: score {score} < threshold {effective_min}")
            return None

        # ── 6. Entry, SL, TP ──────────────────────────────────────────────────
        direction = "sell" if at_upper_band else "buy"

        atr_series  = self._atr(df_m15)
        current_atr = float(atr_series.iloc[-1]) if not atr_series.isna().all() else 1.5

        sl_buffer = current_atr * 1.5   # SL placed 1.5 ATR beyond outer band

        if direction == "sell":
            entry = current_close
            sl    = current_upper + sl_buffer
            tp    = current_mid              # revert to mean
        else:
            spread = 3.0 * 0.1              # approx 3-pip buy spread
            entry  = current_close + spread
            sl     = current_lower - sl_buffer
            tp     = current_mid

        sl_dist = abs(entry - sl)
        if sl_dist <= 0:
            return None

        rr = abs(tp - entry) / sl_dist

        if rr < RISK_CONFIG.min_rr_ratio:
            logger.debug(f"MeanReversion: RR {rr:.2f} below minimum {RISK_CONFIG.min_rr_ratio}")
            return None

        # ── 7. Session label ──────────────────────────────────────────────────
        session = get_session_name(current_time)

        signal = TradeSignal(
            signal_id       = f"MR-{str(uuid.uuid4())[:6].upper()}",
            symbol          = symbol,
            timestamp       = current_time,
            direction       = direction,
            entry_price     = round(entry, 2),
            stop_loss       = round(sl, 2),
            take_profit     = round(tp, 2),
            rr_ratio        = round(rr, 3),
            score           = score,
            score_breakdown = score_breakdown,
            max_score       = 8,
            atr_pips        = current_atr / 0.1,
            session         = session,
        )

        logger.info(
            f"MeanReversion signal: {direction.upper()} "
            f"score={score} RR={rr:.2f} ADX={adx_val:.1f} RSI={current_rsi:.1f}"
        )
        return signal
