"""
Equal Highs / Equal Lows detection (liquidity pools).

Price repeatedly touching the same level indicates clustered stop-losses —
a high-probability sweep target for institutional players.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List
import pandas as pd

from core.structure.swing import SwingPoint, detect_swings
from config.trading_config import TRADING_CONFIG


@dataclass
class EqualLevel:
    kind: str            # "equal_highs" | "equal_lows"
    price: float         # average price of the cluster
    touches: int         # how many swings cluster here
    first_index: int
    last_index: int


def detect_equal_levels(
    df: pd.DataFrame,
    swings: List[SwingPoint] | None = None,
    tolerance_pips: float | None = None,
) -> List[EqualLevel]:
    """
    Group swing highs/lows that are within `tolerance_pips` of each other.
    Groups with 2+ members are equal levels (liquidity pools).
    """
    if swings is None:
        swings = detect_swings(df)
    tol = (tolerance_pips or TRADING_CONFIG.equal_level_tolerance_pips) * 0.1  # pips → price

    results: List[EqualLevel] = []

    for kind, swing_type in [("equal_highs", "high"), ("equal_lows", "low")]:
        filtered = [s for s in swings if s.kind == swing_type]
        filtered.sort(key=lambda s: s.price)

        clusters: List[List[SwingPoint]] = []
        for s in filtered:
            placed = False
            for cluster in clusters:
                if abs(s.price - cluster[0].price) <= tol:
                    cluster.append(s)
                    placed = True
                    break
            if not placed:
                clusters.append([s])

        for cluster in clusters:
            if len(cluster) >= 2:
                results.append(EqualLevel(
                    kind=kind,
                    price=sum(s.price for s in cluster) / len(cluster),
                    touches=len(cluster),
                    first_index=min(s.index for s in cluster),
                    last_index=max(s.index for s in cluster),
                ))

    return results


def nearest_equal_level(
    price: float, levels: List[EqualLevel], within_pips: float = 50.0
) -> EqualLevel | None:
    """Return the closest equal level within the pip threshold."""
    threshold = within_pips * 0.1
    candidates = [lv for lv in levels if abs(lv.price - price) <= threshold]
    if not candidates:
        return None
    return min(candidates, key=lambda lv: abs(lv.price - price))
