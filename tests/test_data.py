"""Tests for data fetcher, processor, and feature engineering."""
import pytest
import pandas as pd
import numpy as np
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.fetcher import slice_window, resample_to_htf
from data.processor import validate_ohlcv, remove_price_spikes, full_pipeline
from data.feature_engineering import (
    add_atr, add_atr_pips, simulate_spread,
    add_session_flag, prepare_backtest_data,
)


class TestFetcher:
    def test_slice_window_size(self, full_dataset):
        sliced = slice_window(full_dataset, end_idx=150, window=50)
        assert len(sliced) == 50

    def test_slice_window_at_start(self, full_dataset):
        sliced = slice_window(full_dataset, end_idx=10, window=50)
        assert len(sliced) == 11   # can't be more than what exists

    def test_resample_reduces_rows(self, full_dataset):
        h4 = resample_to_htf(full_dataset, "4h")
        assert len(h4) < len(full_dataset)

    def test_resample_ohlc_correct(self, full_dataset):
        h4 = resample_to_htf(full_dataset, "4h")
        assert set(h4.columns) >= {"open", "high", "low", "close", "volume"}
        # High must be >= Low always
        assert (h4["high"] >= h4["low"]).all()


class TestProcessor:
    def test_validate_removes_bad_rows(self, full_dataset):
        # Inject a bad row
        bad = full_dataset.copy()
        bad.iloc[5, bad.columns.get_loc("high")] = bad.iloc[5]["low"] - 1  # high < low
        cleaned, removed = validate_ohlcv(bad)
        assert removed >= 1

    def test_valid_data_passes(self, full_dataset):
        cleaned, removed = validate_ohlcv(full_dataset)
        assert removed == 0

    def test_spike_removal(self, full_dataset):
        spiked = full_dataset.copy()
        spiked.iloc[100, spiked.columns.get_loc("close")] *= 10  # massive spike
        cleaned = remove_price_spikes(spiked, z_threshold=5.0)
        assert len(cleaned) < len(spiked)

    def test_full_pipeline_returns_df(self, full_dataset):
        result = full_pipeline(full_dataset)
        assert isinstance(result, pd.DataFrame)
        assert len(result) > 0


class TestFeatureEngineering:
    def test_atr_no_nan_after_period(self, full_dataset):
        df = add_atr(full_dataset, period=14)
        assert "atr" in df.columns
        assert df["atr"].iloc[14:].isna().sum() == 0

    def test_atr_pips_positive(self, full_dataset):
        df = add_atr_pips(full_dataset, period=14)
        assert (df["atr_pips"].dropna() > 0).all()

    def test_spread_simulation_higher_at_night(self, full_dataset):
        df = simulate_spread(full_dataset)
        day_spread   = df[df.index.hour == 10]["spread"].mean()
        night_spread = df[df.index.hour == 3]["spread"].mean()
        assert night_spread > day_spread

    def test_session_flags_binary(self, full_dataset):
        df = add_session_flag(full_dataset)
        assert set(df["london"].unique()).issubset({0, 1})
        assert set(df["ny"].unique()).issubset({0, 1})

    def test_prepare_backtest_data_adds_all_features(self, full_dataset):
        df = prepare_backtest_data(full_dataset)
        for col in ["atr", "atr_pips", "body_ratio", "spread", "spread_pips", "in_session"]:
            assert col in df.columns, f"Missing column: {col}"
