"""
Walk-forward time-series splits for backtesting.

Each fold: train = all bars before the test window; test = the next ``test_size``
bars. The next fold advances by one full test block (expanding train).
"""
from __future__ import annotations

from typing import Iterator, List, Tuple

import pandas as pd


def walk_forward_splits(
    df: pd.DataFrame,
    n_splits: int = 4,
    test_size: int | None = None,
    test_frac: float | None = None,
    min_train_bars: int = 150,
) -> List[Tuple[pd.DataFrame, pd.DataFrame]]:
    """
    Expanding-window walk-forward: train is always ``df.iloc[:test_start]``,
    test is ``df.iloc[test_start : test_start + test_size]``.

    Parameters
    ----------
    df : DataFrame with time index (only row count is used).
    n_splits : maximum number of (train, test) pairs.
    test_size : bars per test window; if None, derived from ``test_frac``.
    test_frac : fraction of total length for test window when ``test_size`` is None.
    min_train_bars : first test window starts at this index.
    """
    n = len(df)
    if n_splits < 1 or n < min_train_bars + 10:
        return []

    if test_size is None:
        if test_frac is None or not (0 < test_frac < 1):
            test_frac = 0.15
        test_size = max(50, int(n * test_frac))

    out: List[Tuple[pd.DataFrame, pd.DataFrame]] = []
    test_start = min_train_bars
    while test_start + test_size <= n and len(out) < n_splits:
        train = df.iloc[:test_start]
        test = df.iloc[test_start : test_start + test_size]
        if len(train) >= min_train_bars and len(test) > 0:
            out.append((train, test))
        test_start += test_size
    return out


def iter_walk_forward(
    df: pd.DataFrame,
    n_splits: int = 4,
    test_size: int | None = None,
    test_frac: float | None = None,
    min_train_bars: int = 150,
) -> Iterator[Tuple[pd.DataFrame, pd.DataFrame]]:
    yield from walk_forward_splits(
        df,
        n_splits=n_splits,
        test_size=test_size,
        test_frac=test_frac,
        min_train_bars=min_train_bars,
    )
