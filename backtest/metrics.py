"""
Backtest Metrics — detailed performance analytics.
"""
from __future__ import annotations
from typing import List, Dict
import numpy as np
import pandas as pd
from datetime import datetime

from core.risk.portfolio import ClosedTrade
from backtest.engine import BacktestResult
from utils.logger import get_logger

logger = get_logger(__name__)


def print_report(result: BacktestResult) -> None:
    """Print a formatted performance report to stdout."""
    sep = "─" * 55
    print(f"\n{'═'*55}")
    print(f"  📊 BACKTEST PERFORMANCE REPORT")
    print(f"{'═'*55}")
    print(f"  Total Trades      : {result.total_trades}")
    print(f"  Win Rate          : {result.win_rate:.2f}%")
    print(f"  Profit Factor     : {result.profit_factor:.3f}")
    print(f"  Total PnL         : ${result.total_pnl:,.2f}")
    print(f"  Net balance Δ     : ${result.net_balance_change:,.2f}")
    print(f"  Max Drawdown      : {result.max_drawdown_pct:.2f}%")
    print(f"  Sharpe Ratio      : {result.sharpe_ratio:.3f}")
    print(sep)

    trades = result.trades
    if trades:
        wins   = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        avg_w  = np.mean([t.pnl for t in wins])   if wins   else 0
        avg_l  = np.mean([t.pnl for t in losses]) if losses else 0
        avg_hold = np.mean(
            [(t.closed_at - t.opened_at).total_seconds() / 3600 for t in trades]
        )
        print(f"  Avg Win           : ${avg_w:,.2f}")
        print(f"  Avg Loss          : ${avg_l:,.2f}")
        print(f"  Avg Hold (hrs)    : {avg_hold:.1f}")
        print(f"  Wins / Losses     : {len(wins)} / {len(losses)}")
        print(sep)

    if result.monthly_breakdown:
        print(f"\n  📅 Monthly P&L:")
        for month, pnl in sorted(result.monthly_breakdown.items()):
            sign = "+" if pnl >= 0 else ""
            print(f"    {month}  {sign}${pnl:,.2f}")
    print(f"{'═'*55}\n")


def equity_curve_to_df(equity_curve: List[float]) -> pd.DataFrame:
    """Convert equity list to DataFrame for plotting."""
    return pd.DataFrame({"equity": equity_curve})


def trade_log_to_df(trades: List[ClosedTrade]) -> pd.DataFrame:
    """Convert trade list to DataFrame for analysis."""
    if not trades:
        return pd.DataFrame()
    rows = []
    for t in trades:
        rows.append({
            "trade_id":   t.trade_id,
            "symbol":     t.symbol,
            "direction":  t.direction,
            "entry":      t.entry_price,
            "exit":       t.exit_price,
            "lot":        t.lot_size,
            "pnl":        t.pnl,
            "pnl_pips":   t.pnl_pips,
            "opened_at":  t.opened_at,
            "closed_at":  t.closed_at,
            "reason":     t.reason,
        })
    df = pd.DataFrame(rows)
    df["cumulative_pnl"] = df["pnl"].cumsum()
    return df


def consecutive_losses(trades: List[ClosedTrade]) -> int:
    """Maximum consecutive losing streak."""
    max_streak = cur = 0
    for t in trades:
        if t.pnl <= 0:
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            cur = 0
    return max_streak


def expectancy(trades: List[ClosedTrade]) -> float:
    """Mathematical expectancy per trade in USD."""
    if not trades:
        return 0.0
    wins   = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    n = len(trades)
    wr = len(wins) / n
    avg_w = np.mean(wins)   if wins   else 0
    avg_l = np.mean(losses) if losses else 0
    return round(wr * avg_w + (1 - wr) * avg_l, 2)
