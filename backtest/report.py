"""
Backtest Report — multi-mode comparison output.
Produces a Markdown summary table and a detailed CSV trade log.
"""
from __future__ import annotations
import csv
import io
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict

from backtest.engine import BacktestResult
from backtest.metrics import trade_log_to_df
from utils.logger import get_logger

logger = get_logger(__name__)


def generate_md(results: Dict[str, BacktestResult], initial_balance: float = 10_000.0) -> str:
    """
    Markdown table comparing strategy performance across modes.
    Columns: Mode | Trades | WR% | PF | MaxDD% | Sharpe | Total PnL | Return%
    """
    lines = [
        "# Backtest Summary",
        "",
        f"**Balance:** ${initial_balance:,.0f}  |  "
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "| Mode | Trades | WR% | Profit Factor | Max DD% | Sharpe | Total PnL | Return% |",
        "|------|--------|-----|---------------|---------|--------|-----------|---------|",
    ]
    for mode, r in results.items():
        return_pct = r.total_pnl / initial_balance * 100 if initial_balance > 0 else 0.0
        lines.append(
            f"| {mode} "
            f"| {r.total_trades} "
            f"| {r.win_rate:.1f}% "
            f"| {r.profit_factor:.2f} "
            f"| {r.max_drawdown_pct:.2f}% "
            f"| {r.sharpe_ratio:.2f} "
            f"| ${r.total_pnl:,.2f} "
            f"| {return_pct:+.1f}% |"
        )
    lines += ["", "---", ""]
    return "\n".join(lines)


def generate_csv(results: Dict[str, BacktestResult]) -> str:
    """
    CSV of all trades across all modes.
    Columns: mode, trade_id, direction, entry, exit, lot, pnl, pnl_pct,
             opened_at, closed_at, reason, session
    """
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "mode", "trade_id", "direction", "entry", "exit", "lot",
        "pnl", "pnl_pips", "opened_at", "closed_at", "reason",
    ])
    writer.writeheader()
    for mode, r in results.items():
        for t in r.trades:
            writer.writerow({
                "mode":       mode,
                "trade_id":   t.trade_id,
                "direction":  t.direction,
                "entry":      round(t.entry_price, 2),
                "exit":       round(t.exit_price, 2),
                "lot":        t.lot_size,
                "pnl":        round(t.pnl, 2),
                "pnl_pips":   round(t.pnl_pips, 1),
                "opened_at":  t.opened_at.strftime("%Y-%m-%d %H:%M:%S") if t.opened_at else "",
                "closed_at":  t.closed_at.strftime("%Y-%m-%d %H:%M:%S") if t.closed_at else "",
                "reason":     t.reason,
            })
    return output.getvalue()


def save_reports(
    results: Dict[str, BacktestResult],
    initial_balance: float = 10_000.0,
    output_dir: str = "backtest_results",
) -> tuple[str, str]:
    """
    Write summary.md and trades.csv to output_dir.
    Returns (md_path, csv_path).
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    ts      = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    md_path  = out / f"backtest_summary_{ts}.md"
    csv_path = out / f"backtest_trades_{ts}.csv"

    md_path.write_text(generate_md(results, initial_balance), encoding="utf-8")
    csv_path.write_text(generate_csv(results), encoding="utf-8")

    logger.info(f"Backtest report: {md_path}")
    logger.info(f"Backtest trades: {csv_path}")
    return str(md_path), str(csv_path)
