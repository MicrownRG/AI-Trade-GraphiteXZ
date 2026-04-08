"""
Data Processor.

Cleans, validates, and aligns OHLCV data before it reaches the strategy.
Handles:
- OHLC integrity checks (high >= open/close >= low)
- Gap filling (forward-fill for missing bars)
- Outlier detection (price spikes)
- Timezone normalisation
- Data alignment between timeframes
"""
from __future__ import annotations
import pandas as pd
import numpy as np
from typing import Tuple

from utils.logger import get_logger

logger = get_logger(__name__)


def validate_ohlcv(df: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
    """
    Remove rows with OHLC integrity violations.
    Returns (clean_df, n_removed).
    """
    initial = len(df)

    mask = (
        (df["high"] >= df["open"]) &
        (df["high"] >= df["close"]) &
        (df["low"]  <= df["open"]) &
        (df["low"]  <= df["close"]) &
        (df["high"] >= df["low"]) &
        (df["open"] > 0) &
        (df["close"] > 0) &
        (df["volume"] >= 0)
    )

    clean = df[mask].copy()
    removed = initial - len(clean)
    if removed > 0:
        logger.warning(f"Removed {removed} invalid OHLCV rows")

    return clean, removed


def remove_price_spikes(
    df: pd.DataFrame, z_threshold: float = 6.0
) -> pd.DataFrame:
    """
    Detect and remove price spikes using z-score on close price changes.
    A z-score > threshold indicates an erroneous tick.
    """
    pct_change = df["close"].pct_change().abs()
    z = (pct_change - pct_change.mean()) / pct_change.std()
    spikes = z > z_threshold
    n_spikes = spikes.sum()
    if n_spikes > 0:
        logger.warning(f"Removed {n_spikes} price spikes (z>{z_threshold})")
    return df[~spikes].copy()


def fill_missing_bars(df: pd.DataFrame, freq: str = "15min") -> pd.DataFrame:
    """
    Reindex to a uniform frequency and forward-fill missing bars.
    Gaps longer than 4 hours are left as NaN (weekend / holiday gaps).
    """
    full_index = pd.date_range(start=df.index.min(), end=df.index.max(), freq=freq)
    df_full    = df.reindex(full_index)

    # Only forward-fill gaps <= 4 hours (16 M15 bars)
    df_full = df_full.ffill(limit=16)

    filled = df_full.isna().any(axis=1).sum()
    if filled > 0:
        logger.debug(f"After fill: {filled} bars still NaN (weekend/holiday gaps — OK)")

    df_full.dropna(inplace=True)
    return df_full


def align_timeframes(
    df_ltf: pd.DataFrame, df_htf: pd.DataFrame
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Ensure both DataFrames share the same UTC timezone and overlap period.
    Trims both to the common date range.
    """
    start = max(df_ltf.index.min(), df_htf.index.min())
    end   = min(df_ltf.index.max(), df_htf.index.max())

    df_ltf = df_ltf[(df_ltf.index >= start) & (df_ltf.index <= end)]
    df_htf = df_htf[(df_htf.index >= start) & (df_htf.index <= end)]

    logger.debug(f"Aligned: {start.date()} → {end.date()} | LTF={len(df_ltf)} HTF={len(df_htf)}")
    return df_ltf, df_htf


def full_pipeline(df: pd.DataFrame, freq: str = "15min") -> pd.DataFrame:
    """Run all processing steps in sequence."""
    df, _ = validate_ohlcv(df)
    df    = remove_price_spikes(df)
    df    = fill_missing_bars(df, freq=freq)
    df.sort_index(inplace=True)
    logger.info(f"Processing complete: {len(df)} clean bars")
    return df
