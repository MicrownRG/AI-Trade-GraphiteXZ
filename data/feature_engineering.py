"""
Feature Engineering.

Computes indicators used by filters and scoring (ATR, spread simulation, etc.)
Does NOT include ML features — this is a structural/rules system.
"""
from __future__ import annotations
import pandas as pd
import numpy as np


def add_atr(df: pd.DataFrame, period: int = 14, col: str = "atr") -> pd.DataFrame:
    df = df.copy()
    high, low, prev_c = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([high - low, (high - prev_c).abs(), (low - prev_c).abs()], axis=1).max(axis=1)
    df[col] = tr.rolling(period).mean()
    return df


def add_atr_pips(df: pd.DataFrame, period: int = 14, pip_size: float = 0.1) -> pd.DataFrame:
    df = add_atr(df, period)
    df["atr_pips"] = df["atr"] / pip_size
    return df


def add_body_ratio(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    body  = (df["close"] - df["open"]).abs()
    range_ = df["high"] - df["low"]
    df["body_ratio"] = body / range_.replace(0, np.nan)
    return df


def add_candle_direction(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["bullish"] = (df["close"] > df["open"]).astype(int)
    return df


def simulate_spread(df: pd.DataFrame, base_spread: float = 0.03) -> pd.DataFrame:
    """
    Add simulated spread column (dynamic — higher during off-hours).
    For gold: typical spread 0.02–0.05 during London/NY, up to 0.20 otherwise.
    """
    df = df.copy()
    hours = df.index.hour
    # Higher spread during Asian session (off-hours)
    spread = np.where(
        ((hours >= 7) & (hours < 21)),  # London/NY
        base_spread,
        base_spread * 3.0,
    )
    df["spread"] = spread
    df["spread_pips"] = df["spread"] / 0.01  # convert to pips
    return df


def add_session_flag(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    hours = df.index.hour
    df["london"] = ((hours >= 7) & (hours < 16)).astype(int)
    df["ny"]     = ((hours >= 12) & (hours < 21)).astype(int)
    df["overlap"] = (df["london"] & df["ny"]).astype(int)
    df["in_session"] = ((df["london"] == 1) | (df["ny"] == 1)).astype(int)
    return df


def prepare_backtest_data(df: pd.DataFrame) -> pd.DataFrame:
    """One-shot feature enrichment for backtesting."""
    df = add_atr_pips(df)
    df = add_body_ratio(df)
    df = add_candle_direction(df)
    df = simulate_spread(df)
    df = add_session_flag(df)
    return df
