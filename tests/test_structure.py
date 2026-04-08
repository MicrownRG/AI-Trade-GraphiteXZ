"""Tests for market structure detection modules."""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.structure.swing import detect_swings, get_recent_swings
from core.structure.bos import detect_bos, get_latest_bos
from core.structure.choch import detect_choch, get_latest_choch
from core.structure.liquidity import detect_liquidity_sweeps
from core.structure.displacement import detect_displacement, atr
from core.structure.equal_levels import detect_equal_levels
from core.structure.htf_bias import calculate_htf_bias


# ── Swing detection ───────────────────────────────────────────────────────────

class TestSwingDetection:
    def test_returns_list(self, bullish_df):
        swings = detect_swings(bullish_df)
        assert isinstance(swings, list)

    def test_has_highs_and_lows(self, bullish_df):
        swings = detect_swings(bullish_df)
        kinds = {s.kind for s in swings}
        assert "high" in kinds
        assert "low" in kinds

    def test_swing_high_is_local_max(self, bullish_df):
        swings = detect_swings(bullish_df, lookback=5)
        highs = [s for s in swings if s.kind == "high"]
        for h in highs:
            window = bullish_df["high"].iloc[max(0, h.index-5): h.index+6]
            assert h.price == window.max(), f"Swing high {h.price} is not local max"

    def test_swing_low_is_local_min(self, bullish_df):
        swings = detect_swings(bullish_df, lookback=5)
        lows = [s for s in swings if s.kind == "low"]
        for l in lows:
            window = bullish_df["low"].iloc[max(0, l.index-5): l.index+6]
            assert l.price == window.min()

    def test_get_recent_swings(self, bullish_df):
        recent = get_recent_swings(bullish_df, n_swings=3)
        assert len(recent["highs"]) <= 3
        assert len(recent["lows"])  <= 3

    def test_no_crash_on_short_df(self):
        tiny = pd.DataFrame({
            "open": [1950]*15, "high": [1952]*15,
            "low": [1948]*15, "close": [1951]*15, "volume": [100]*15,
        }, index=pd.date_range("2024-01-01", periods=15, freq="15min", tz="UTC"))
        swings = detect_swings(tiny, lookback=5)
        assert isinstance(swings, list)


# ── BOS detection ─────────────────────────────────────────────────────────────

class TestBOSDetection:
    def test_bullish_bos_in_uptrend(self, bullish_df):
        events = detect_bos(bullish_df)
        bullish_events = [e for e in events if e.direction == "bullish"]
        assert len(bullish_events) > 0, "Expected bullish BOS in uptrend data"

    def test_bearish_bos_in_downtrend(self, bearish_df):
        events = detect_bos(bearish_df)
        bearish_events = [e for e in events if e.direction == "bearish"]
        assert len(bearish_events) > 0, "Expected bearish BOS in downtrend data"

    def test_bos_close_beyond_level(self, bullish_df):
        events = detect_bos(bullish_df)
        for e in events:
            if e.direction == "bullish":
                assert e.close_price > e.broken_level
            else:
                assert e.close_price < e.broken_level

    def test_get_latest_bos(self, bullish_df):
        latest = get_latest_bos(bullish_df)
        assert latest is not None


# ── CHOCH detection ───────────────────────────────────────────────────────────

class TestCHOCHDetection:
    def test_returns_list(self, bearish_df):
        events = detect_choch(bearish_df)
        assert isinstance(events, list)

    def test_choch_has_valid_direction(self, bullish_df):
        events = detect_choch(bullish_df)
        for e in events:
            assert e.direction in ("bullish", "bearish")


# ── Liquidity sweep ───────────────────────────────────────────────────────────

class TestLiquiditySweep:
    def test_sweep_close_back_inside(self, bullish_df):
        sweeps = detect_liquidity_sweeps(bullish_df)
        for s in sweeps:
            if s.direction == "buy_side":
                # Wick above level, close below
                assert s.wick_high > s.swept_level
                assert s.close_price < s.swept_level
            else:
                assert s.wick_low < s.swept_level
                assert s.close_price > s.swept_level

    def test_wick_ratio_above_threshold(self, bullish_df):
        sweeps = detect_liquidity_sweeps(bullish_df, min_wick_ratio=0.3)
        for s in sweeps:
            assert s.wick_ratio >= 0.3


# ── Displacement ──────────────────────────────────────────────────────────────

class TestDisplacement:
    def test_body_ratio_above_threshold(self, bullish_df):
        events = detect_displacement(bullish_df, min_body_ratio=0.6)
        for e in events:
            assert e.body_ratio >= 0.6

    def test_direction_matches_candle(self, bullish_df):
        events = detect_displacement(bullish_df)
        for e in events:
            bar = bullish_df.iloc[e.index]
            if e.direction == "bullish":
                assert bar["close"] > bar["open"]
            else:
                assert bar["close"] < bar["open"]

    def test_atr_returns_series(self, bullish_df):
        a = atr(bullish_df, period=14)
        assert len(a) == len(bullish_df)
        assert a.iloc[-1] > 0


# ── Equal levels ──────────────────────────────────────────────────────────────

class TestEqualLevels:
    def test_touches_at_least_two(self, bullish_df):
        levels = detect_equal_levels(bullish_df)
        for lv in levels:
            assert lv.touches >= 2

    def test_kind_is_valid(self, bullish_df):
        levels = detect_equal_levels(bullish_df)
        for lv in levels:
            assert lv.kind in ("equal_highs", "equal_lows")


# ── HTF Bias ──────────────────────────────────────────────────────────────────

class TestHTFBias:
    def test_returns_htf_bias(self, h4_df, h1_df):
        bias = calculate_htf_bias(h4_df, h1_df)
        assert bias.direction in ("bullish", "bearish", "neutral")
        assert 0.0 <= bias.confidence <= 1.0

    def test_bullish_trend_gives_bullish_bias(self, bullish_df):
        from data.fetcher import resample_to_htf
        h4 = resample_to_htf(bullish_df, "4h")
        h1 = resample_to_htf(bullish_df, "1h")
        bias = calculate_htf_bias(h4, h1)
        # Should be bullish or neutral (never strongly bearish in clean uptrend)
        assert bias.direction in ("bullish", "neutral")
