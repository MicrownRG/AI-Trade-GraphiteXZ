"""
Multi-Mode Backtest Runner CLI.

Usage:
    python -m backtest.runner
    python -m backtest.runner --years 2 --balance 10000 --mode AGGRESSIVE
    python -m backtest.runner --mode ALL --output backtest_results/

Options:
    --years N       Years of history to fetch (default: 2)
    --balance B     Starting balance in USD (default: 10000)
    --mode M        Single mode, comma-list, or ALL (default: ALL)
    --output DIR    Output directory (default: backtest_results)
    --mt5           Fetch from live MT5 (default: True)

Outputs:
    backtest_results/backtest_summary_<ts>.md   — Markdown comparison table
    backtest_results/backtest_trades_<ts>.csv   — Full trade detail CSV
"""
from __future__ import annotations
import argparse
import sys
import time
from typing import Dict

from config.trading_config import TRADING_CONFIG, TradeMode
from backtest.engine import BacktestEngine, BacktestResult
from backtest.report import save_reports
from utils.logger import get_logger

logger = get_logger("backtest.runner")

_ALL_MODES = [
    TradeMode.CONSERVATIVE,
    TradeMode.MODERATE,
    TradeMode.AGGRESSIVE,
    TradeMode.VERY_AGGRESSIVE,
]


def _fetch_data(years: int):
    """Fetch M15 XAUUSD data from MT5 (bars for N years ≈ bars_per_year * N)."""
    from execution.mt5_client import MT5Client
    from data.fetcher import fetch_from_mt5

    bars_per_year = 26280  # 4 bars/hr * 24 * 365 (M15 candles)
    count = bars_per_year * years + 500  # +500 warm-up buffer

    mt5 = MT5Client()
    if not mt5.connect():
        logger.critical("Cannot connect to MT5 — aborting backtest")
        sys.exit(1)

    logger.info(f"Fetching {count:,} M15 bars (~{years}yr) from MT5...")
    df = fetch_from_mt5(mt5, "XAUUSD", "M15", count=count)
    mt5.disconnect()

    if df is None or df.empty:
        logger.critical("No data returned from MT5")
        sys.exit(1)

    logger.info(f"Data fetched: {len(df):,} bars  "
                f"[{df.index[0]} → {df.index[-1]}]")
    return df


def run_backtest_for_mode(
    mode: TradeMode,
    df,
    initial_balance: float,
) -> BacktestResult:
    """Run backtest for a single mode. Mutates TRADING_CONFIG.current_mode."""
    TRADING_CONFIG.current_mode = mode

    logger.info(f"Running backtest: {mode.value}  balance=${initial_balance:,.0f}")
    t0 = time.time()

    engine = BacktestEngine(
        initial_balance=initial_balance,
        use_ai=False,
        allowed_strategies=["all"],
    )
    result = engine.run(df_ltf=df, warm_up_bars=200)
    elapsed = time.time() - t0

    logger.info(
        f"{mode.value}: {result.total_trades} trades  "
        f"WR={result.win_rate:.1f}%  PF={result.profit_factor:.2f}  "
        f"PnL=${result.total_pnl:,.2f}  MaxDD={result.max_drawdown_pct:.2f}%  "
        f"({elapsed:.0f}s)"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-Mode XAUUSD Backtest Runner")
    parser.add_argument("--years",   type=int,   default=2,                   help="Years of history (default: 2)")
    parser.add_argument("--balance", type=float, default=10_000.0,            help="Starting balance USD (default: 10000)")
    parser.add_argument("--mode",    type=str,   default="ALL",               help="Mode(s): ALL, or CONSERVATIVE,MODERATE,... (default: ALL)")
    parser.add_argument("--output",  type=str,   default="backtest_results",  help="Output directory (default: backtest_results)")
    args = parser.parse_args()

    # Resolve modes
    if args.mode.upper() == "ALL":
        modes = _ALL_MODES
    else:
        modes = []
        for m in args.mode.upper().split(","):
            try:
                modes.append(TradeMode[m.strip()])
            except KeyError:
                logger.error(f"Unknown mode: {m}  (valid: {[x.value for x in TradeMode]})")
                sys.exit(1)

    logger.info(
        f"Backtest: years={args.years}  balance=${args.balance:,.0f}  "
        f"modes={[m.value for m in modes]}"
    )

    df = _fetch_data(args.years)

    results: Dict[str, BacktestResult] = {}
    for mode in modes:
        try:
            results[mode.value] = run_backtest_for_mode(mode, df, args.balance)
        except Exception as e:
            logger.error(f"Backtest failed for {mode.value}: {e}", exc_info=True)

    if not results:
        logger.error("No backtest results produced — aborting")
        sys.exit(1)

    md_path, csv_path = save_reports(results, args.balance, args.output)
    print(f"\nReports written:")
    print(f"  Summary : {md_path}")
    print(f"  Trades  : {csv_path}")

    # Print quick markdown table to stdout as well
    from backtest.report import generate_md
    print("\n" + generate_md(results, args.balance))


if __name__ == "__main__":
    main()
