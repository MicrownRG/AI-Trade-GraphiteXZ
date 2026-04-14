"""
Trend Logic — accurate multi-indicator trend bias analysis.

Bias determination uses a three-layer approach:
  1. EMA stack position AND slope  (primary signal)
  2. ADX strength                  (trend quality filter)
  3. Candlestick rejection wicks   (additive note, NOT a bias override)

Fixes vs previous version:
  - ADX now uses Wilder's smoothing (RMA / exponential) — industry standard
  - EMA slope is checked alongside EMA level to filter ranging markets
  - Rejection candles no longer override the EMA-based bias; they only add
    a supplementary flag so signal scoring can use them as a bonus
  - Bias is NEUTRAL when EMA slopes conflict or ADX is weak AND price is
    between the two key EMAs (indecision zone)
"""
from __future__ import annotations
import pandas as pd
import numpy as np
from typing import Dict, Optional


class TrendLogic:
    def __init__(
        self,
        ema_periods: list[int] = [9, 21, 50, 200],
        adx_period:  int = 14,
        slope_lookback: int = 3,   # candles used to measure EMA slope
    ):
        self.ema_periods    = ema_periods
        self.adx_period     = adx_period
        self.slope_lookback = slope_lookback

    # ── EMA calculation ───────────────────────────────────────────────────────

    def calculate_emas(self, df: pd.DataFrame) -> pd.DataFrame:
        emas = pd.DataFrame(index=df.index)
        for p in self.ema_periods:
            emas[f"ema_{p}"] = df["close"].ewm(span=p, adjust=False).mean()
        return emas

    # ── ADX (Wilder's smoothing) ───────────────────────────────────────────────

    def calculate_adx(self, df: pd.DataFrame) -> pd.Series:
        """
        Industry-standard ADX using Wilder's Smoothed Moving Average (RMA).
        alpha = 1 / period  (equivalent to EMA with span = 2*period - 1)
        """
        n  = self.adx_period
        df = df.copy()

        # True Range
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"]  - prev_close).abs(),
        ], axis=1).max(axis=1)

        # Directional Movement
        up_move   = df["high"] - df["high"].shift(1)
        down_move = df["low"].shift(1)  - df["low"]

        plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move,  0.0)
        minus_dm = np.where((down_move > up_move)  & (down_move > 0), down_move, 0.0)

        # Wilder's smoothing (RMA)
        alpha = 1.0 / n

        def _rma(series: pd.Series) -> pd.Series:
            result = series.copy().astype(float)
            for i in range(1, len(result)):
                result.iloc[i] = (1 - alpha) * result.iloc[i - 1] + alpha * result.iloc[i]
            return result

        tr_rma   = _rma(tr)
        pdm_rma  = _rma(pd.Series(plus_dm,  index=df.index))
        ndm_rma  = _rma(pd.Series(minus_dm, index=df.index))

        plus_di  = 100 * pdm_rma  / tr_rma.replace(0, np.nan)
        minus_di = 100 * ndm_rma  / tr_rma.replace(0, np.nan)

        dx_denom = (plus_di + minus_di).replace(0, np.nan)
        dx       = 100 * (plus_di - minus_di).abs() / dx_denom
        adx      = _rma(dx.fillna(0))

        return adx

    # ── EMA slope helper ──────────────────────────────────────────────────────

    def _ema_slope(self, ema_series: pd.Series) -> float:
        """
        Returns the normalised slope of the EMA over the last slope_lookback candles.
        Positive = rising, negative = falling, near-zero = flat/ranging.
        Normalised by price level to make it comparable across instruments.
        """
        n = self.slope_lookback + 1
        if len(ema_series) < n:
            return 0.0
        segment = ema_series.iloc[-n:]
        rise    = segment.iloc[-1] - segment.iloc[0]
        ref     = segment.iloc[0]
        return float(rise / ref) if ref != 0 else 0.0

    # ── Main trend analysis ───────────────────────────────────────────────────

    def analyze_trend(self, df: pd.DataFrame) -> Dict:
        """
        Determine trend bias using EMA stack, EMA slope, and ADX.

        Bias rules (in priority order):
          BULLISH: price > EMA50 > EMA200 AND both EMAs sloping upward
          BEARISH: price < EMA50 < EMA200 AND both EMAs sloping downward
          NEUTRAL: EMAs flat/conflicting, or price between key EMAs

        Rejection candles (hammer / shooting star) are reported separately as
        'rejection' and are NOT used to override the EMA bias.  They act as a
        bonus signal in scoring layers above this function.
        """
        if len(df) < max(self.ema_periods) + self.adx_period:
            return {
                "bias": "NEUTRAL", "rejection": "NONE",
                "adx": 0.0, "trend_strength": "WEAK", "ema_cross": False,
            }

        emas     = self.calculate_emas(df)
        adx_ser  = self.calculate_adx(df)

        curr_price = float(df["close"].iloc[-1])
        ema_50     = float(emas["ema_50"].iloc[-1])
        ema_200    = float(emas["ema_200"].iloc[-1])
        ema_9      = float(emas["ema_9"].iloc[-1])
        ema_21     = float(emas["ema_21"].iloc[-1])
        curr_adx   = float(adx_ser.iloc[-1]) if not adx_ser.empty else 20.0

        # EMA slopes (normalised)
        slope_50  = self._ema_slope(emas["ema_50"])
        slope_200 = self._ema_slope(emas["ema_200"])

        # Slope threshold: < 0.01% per candle is considered flat/ranging
        _FLAT = 0.0001

        ema_stack_bull = (curr_price > ema_50 > ema_200)
        ema_stack_bear = (curr_price < ema_50 < ema_200)
        slopes_bull    = (slope_50 > _FLAT and slope_200 > -_FLAT * 2)
        slopes_bear    = (slope_50 < -_FLAT and slope_200 < _FLAT * 2)

        if ema_stack_bull and slopes_bull:
            bias = "BULLISH"
        elif ema_stack_bear and slopes_bear:
            bias = "BEARISH"
        elif ema_stack_bull and curr_adx > 30:
            # Strong trend even if slope is still catching up (e.g. early breakout)
            bias = "BULLISH"
        elif ema_stack_bear and curr_adx > 30:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

        # Rejection candle detection (supplementary — does NOT override bias)
        # Only flag on the most recent fully closed candle (iloc[-2])
        rejection = "NONE"
        if len(df) >= 2:
            c      = df.iloc[-2]
            body   = max(1e-6, abs(float(c["close"]) - float(c["open"])))
            rng    = max(1e-6, float(c["high"]) - float(c["low"]))
            upper  = float(c["high"]) - max(float(c["close"]), float(c["open"]))
            lower  = min(float(c["close"]), float(c["open"])) - float(c["low"])

            if upper >= body * 2.0 and upper > rng * 0.40:
                rejection = "BEARISH"   # Shooting star / upper wick
            elif lower >= body * 2.0 and lower > rng * 0.40:
                rejection = "BULLISH"   # Hammer / lower wick

        trend_strength = "STRONG" if curr_adx > 25 else ("MODERATE" if curr_adx > 18 else "WEAK")

        return {
            "bias":           bias,
            "rejection":      rejection,
            "adx":            round(curr_adx, 2),
            "trend_strength": trend_strength,
            "ema_cross":      ema_9 > ema_21,
            "ema_50":         ema_50,
            "ema_200":        ema_200,
            "slope_50":       round(slope_50 * 10000, 4),   # in basis-points per candle
            "slope_200":      round(slope_200 * 10000, 4),
        }
