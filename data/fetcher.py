"""
Data Fetcher.

Fetches OHLCV data from MT5 (live) or CSV/Parquet files (backtest).
Always returns a normalised pandas DataFrame with DatetimeIndex.
"""
from __future__ import annotations
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from config.settings import SYMBOL
from utils.logger import get_logger

logger = get_logger(__name__)

REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume"}


def fetch_from_mt5(
    mt5_client,
    symbol: str = SYMBOL,
    timeframe: str = "H4",
    count: int = 500,
) -> pd.DataFrame:
    """Fetch bars from live MT5 and return normalised DataFrame."""
    rates = mt5_client.get_ohlcv(symbol, timeframe, count)
    if rates is None or len(rates) == 0:
        logger.error(f"No data returned from MT5: {symbol} {timeframe}")
        return pd.DataFrame()

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df.sort_index(inplace=True)

    logger.debug(f"Fetched {len(df)} bars: {symbol} {timeframe}")
    return df


def fetch_batch_mt5(
    mt5_client,
    timeframes: List[str],
    symbol: str = SYMBOL,
    count: int = 500,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch multiple timeframes in parallel using ThreadPoolExecutor.
    Returns a dictionary mapping timeframe name to its DataFrame.
    """
    results = {}
    with ThreadPoolExecutor(max_workers=min(len(timeframes), 10)) as executor:
        # Create a mapping of future to TF string
        future_to_tf = {
            executor.submit(fetch_from_mt5, mt5_client, symbol, tf, count): tf
            for tf in timeframes
        }
        
        for future in as_completed(future_to_tf):
            tf = future_to_tf[future]
            try:
                df = future.result()
                if not df.empty:
                    results[tf.lower()] = df
            except Exception as e:
                logger.error(f"Parallel fetch failed for {tf}: {e}")
                
    return results


def fetch_from_file(
    path: str | Path,
    timeframe_label: str = "",
) -> pd.DataFrame:
    """
    Load historical data from CSV or Parquet.
    Expected columns: time/datetime, open, high, low, close, volume
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Data file not found: {p}")

    if p.suffix in (".parquet", ".pq"):
        df = pd.read_parquet(p)
    elif p.suffix == ".csv":
        df = pd.read_csv(p)
    else:
        raise ValueError(f"Unsupported format: {p.suffix}")

    # Normalise time index
    time_col = next((c for c in df.columns if c.lower() in ("time", "datetime", "date")), None)
    if time_col:
        df[time_col] = pd.to_datetime(df[time_col], utc=True, errors="coerce")
        df.set_index(time_col, inplace=True)
    elif not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("Cannot identify datetime column")

    df.columns = [c.lower() for c in df.columns]
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {missing}")

    df = df[list(REQUIRED_COLUMNS)].astype(float)
    df.sort_index(inplace=True)
    df.dropna(inplace=True)

    logger.info(f"Loaded {len(df)} bars from {p.name} [{timeframe_label}]")
    return df


def slice_window(df: pd.DataFrame, end_idx: int, window: int = 200) -> pd.DataFrame:
    """Return a rolling window slice up to (and including) end_idx."""
    start = max(0, end_idx - window + 1)
    return df.iloc[start : end_idx + 1]


def resample_to_htf(df_ltf: pd.DataFrame, rule: str) -> pd.DataFrame:
    """
    Resample a lower-timeframe DataFrame to a higher timeframe.
    rule: pandas resample rule e.g. '1H', '4H'
    """
    resampled = df_ltf.resample(rule).agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna()
    return resampled
