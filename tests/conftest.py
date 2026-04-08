"""
Shared pytest fixtures for the trading system test suite.
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta


def _make_trending_ohlcv(
    n: int = 300,
    start_price: float = 1950.0,
    trend: float = 0.05,   # price change per bar
    volatility: float = 2.0,
    freq: str = "15min",
    start: str = "2024-01-02 08:00:00",
) -> pd.DataFrame:
    """Generate synthetic OHLCV data with a configurable trend."""
    np.random.seed(42)
    idx = pd.date_range(start=start, periods=n, freq=freq, tz="UTC")
    closes = np.cumsum(np.random.randn(n) * volatility + trend) + start_price

    opens  = np.roll(closes, 1)
    opens[0] = closes[0]

    half_range = np.abs(np.random.randn(n)) * volatility * 0.5 + 1.0
    highs = np.maximum(opens, closes) + half_range
    lows  = np.minimum(opens, closes) - half_range

    volume = np.random.randint(100, 5000, n).astype(float)

    return pd.DataFrame({
        "open":   opens,
        "high":   highs,
        "low":    lows,
        "close":  closes,
        "volume": volume,
    }, index=idx)


@pytest.fixture
def bullish_df():
    return _make_trending_ohlcv(n=300, trend=0.08, volatility=1.5)


@pytest.fixture
def bearish_df():
    return _make_trending_ohlcv(n=300, trend=-0.08, volatility=1.5)


@pytest.fixture
def ranging_df():
    return _make_trending_ohlcv(n=300, trend=0.0, volatility=3.0)


@pytest.fixture
def full_dataset():
    """600-bar M15 dataset used by backtest tests."""
    return _make_trending_ohlcv(n=600, trend=0.05, volatility=2.0)


@pytest.fixture
def h1_df(full_dataset):
    from data.fetcher import resample_to_htf
    return resample_to_htf(full_dataset, "1h")


@pytest.fixture
def h4_df(full_dataset):
    from data.fetcher import resample_to_htf
    return resample_to_htf(full_dataset, "4h")
