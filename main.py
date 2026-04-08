"""
Main entry point.

Usage:
    python main.py --mode backtest --data data/XAUUSD_M15.csv
    python main.py --mode live
    python main.py --mode paper
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

from config.settings import RUN_MODE, SYMBOL
from utils.logger import get_logger

logger = get_logger("main")


# ─────────────────────────────────────────────────────────────────────────────
def run_backtest(data_path: str, initial_balance: float = 10_000.0) -> None:
    from data.fetcher import fetch_from_file
    from backtest.engine import BacktestEngine
    from backtest.metrics import print_report, trade_log_to_df, equity_curve_to_df

    logger.info(f"🔄 Loading data: {data_path}")
    df = fetch_from_file(data_path, timeframe_label="M15")

    logger.info(f"🚀 Starting backtest: {len(df)} bars | balance=${initial_balance:,.2f}")
    engine = BacktestEngine(initial_balance=initial_balance, use_ai=False)
    result = engine.run(df_ltf=df, warm_up_bars=100)

    print_report(result)

    output_dir = Path("results")
    output_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    trade_df = trade_log_to_df(result.trades)
    if not trade_df.empty:
        trade_df.to_csv(output_dir / f"trades_{ts}.csv", index=False)
        logger.info(f"Trade log saved: results/trades_{ts}.csv")

    eq_df = equity_curve_to_df(result.equity_curve)
    eq_df.to_csv(output_dir / f"equity_{ts}.csv", index=False)
    logger.info(f"Equity curve saved: results/equity_{ts}.csv")


# ─────────────────────────────────────────────────────────────────────────────
def run_live(paper: bool = False) -> None:
    from execution.mt5_client  import MT5Client
    from execution.order_manager import OrderManager
    from execution.position_manager import PositionManager
    from core.risk.portfolio   import Portfolio
    from core.risk.executor    import RiskExecutor
    from core.risk.kill_switch import kill_switch
    from core.signal.signal_engine import SignalEngine
    from core.structure.swing  import detect_swings
    from ai.scorer             import AIScorer
    from data.fetcher          import fetch_from_mt5
    from database.connection   import init_db
    from database.repository   import TradeRepository
    from telegram.bot          import TelegramBot
    from telegram.pause_manager import pause_manager
    from utils.time_utils      import utc_now

    mode_label = "PAPER" if paper else "LIVE"
    logger.info(f"🟢 Initialising {mode_label} trading system")

    # ── DB ────────────────────────────────────────────────────────────────────
    init_db()
    repo = TradeRepository()

    # ── MT5 ───────────────────────────────────────────────────────────────────
    mt5 = MT5Client()
    if not mt5.connect():
        logger.critical("Cannot connect to MT5 — aborting")
        sys.exit(1)

    account = mt5.get_account_info()
    balance = account["balance"]
    logger.info(f"Account: balance={balance:.2f}")

    # ── Core components ───────────────────────────────────────────────────────
    portfolio     = Portfolio(balance)
    portfolio.record_day_start()
    signal_engine = SignalEngine()
    risk_executor = RiskExecutor(portfolio)
    ai_scorer     = AIScorer(enabled=not paper)
    trades_today  = 0

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram = TelegramBot(
        portfolio              = portfolio,
        db_repo                = repo,
        trades_today_ref       = lambda: trades_today,
        loss_pause_minutes     = 60,
        consecutive_loss_trigger = 3,
        daily_loss_pct_trigger = 2.0,
    )
    telegram.start()

    # ── Order manager (with telegram wired in) ────────────────────────────────
    order_manager = OrderManager(mt5, portfolio, repo, telegram=telegram)

    # ── Reconcile positions on startup ────────────────────────────────────────
    pos_manager = PositionManager(mt5, portfolio, repo)
    pos_manager.sync_on_startup()

    # ── Daily summary scheduler ───────────────────────────────────────────────
    _last_summary_date = None

    logger.info(f"✅ System ready — entering {mode_label} main loop")

    try:
        while True:
            now = utc_now()

            # ── Kill switch check ─────────────────────────────────────────────
            if kill_switch.is_active:
                logger.critical(f"Kill switch active: {kill_switch.reason}. Halting.")
                break

            # ── Pause guard ───────────────────────────────────────────────────
            if not telegram.is_trading_allowed:
                state = pause_manager.status_dict()
                logger.info(f"Bot paused ({state['state']}): {state['reason']} — waiting...")
                time.sleep(30)
                continue

            # ── Fetch latest bars ─────────────────────────────────────────────
            df_h4  = fetch_from_mt5(mt5, SYMBOL, "H4",  count=200)
            df_h1  = fetch_from_mt5(mt5, SYMBOL, "H1",  count=300)
            df_ltf = fetch_from_mt5(mt5, SYMBOL, "M15", count=300)

            if df_h4.empty or df_h1.empty or df_ltf.empty:
                logger.warning("Empty data — skipping cycle")
                time.sleep(60)
                continue

            # ── Monitor open positions ────────────────────────────────────────
            tick = mt5.get_symbol_tick(SYMBOL)
            if tick:
                price = (tick["bid"] + tick["ask"]) / 2
                order_manager.monitor_and_manage(price)
                portfolio.update_equity(0)

            # ── Kill switch auto-check ────────────────────────────────────────
            kill_switch.check_all(portfolio.drawdown_pct, portfolio.daily_pnl_pct)
            if kill_switch.is_active:
                break

            # ── Day reset ─────────────────────────────────────────────────────
            if _last_summary_date != now.date():
                if _last_summary_date is not None:
                    telegram.send_daily_summary()     # send previous day summary
                portfolio.record_day_start()
                trades_today = 0
                _last_summary_date = now.date()

            # ── Generate signal ───────────────────────────────────────────────
            signal = signal_engine.generate(
                df_h4        = df_h4,
                df_h1        = df_h1,
                df_ltf       = df_ltf,
                current_time = now.replace(tzinfo=None),
                symbol       = SYMBOL,
            )

            if signal:
                repo.save_signal(signal)

                # AI evaluation
                ai_eval = ai_scorer.evaluate(signal)
                signal.ai_confidence = ai_eval.confidence
                signal.ai_decision   = ai_eval.decision
                signal.ai_reason     = ai_eval.reason

                if ai_eval.decision == "TAKE":
                    swings = detect_swings(df_ltf)
                    spread = tick["spread"] if tick else 30.0
                    order  = risk_executor.evaluate(
                        signal       = signal,
                        df_ltf       = df_ltf,
                        swings       = swings,
                        spread_pips  = spread,
                        current_time = now.replace(tzinfo=None),
                        trades_today = trades_today,
                    )
                    if order.approved and not paper:
                        signal_meta = {
                            "score":        signal.score,
                            "session":      signal.session,
                            "ai_confidence":signal.ai_confidence,
                            "ai_reason":    signal.ai_reason,
                        }
                        tid = order_manager.execute(order, signal_meta=signal_meta)
                        if tid:
                            trades_today += 1

            # ── Performance snapshot ──────────────────────────────────────────
            repo.save_performance_snapshot(portfolio, trades_today)

            logger.info(
                f"Cycle ✓ | equity={portfolio.equity:.2f} "
                f"dd={portfolio.drawdown_pct:.2f}% | sleep 60s"
            )
            time.sleep(60)

    except KeyboardInterrupt:
        logger.info("🛑 Keyboard interrupt — shutting down")
    finally:
        telegram.stop()
        mt5.disconnect()
        logger.info("System shutdown complete")


# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="AI XAUUSD Trading System")
    parser.add_argument("--mode",    default=RUN_MODE, choices=["backtest", "live", "paper"])
    parser.add_argument("--data",    default="data/XAUUSD_M15.csv")
    parser.add_argument("--balance", type=float, default=10_000.0)
    args = parser.parse_args()

    logger.info(f"Mode: {args.mode.upper()}")

    if args.mode == "backtest":
        run_backtest(args.data, args.balance)
    elif args.mode == "live":
        run_live(paper=False)
    elif args.mode == "paper":
        run_live(paper=True)
    else:
        logger.error(f"Unknown mode: {args.mode}")
        sys.exit(1)


if __name__ == "__main__":
    main()
