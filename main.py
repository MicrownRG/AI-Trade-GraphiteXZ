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
from zoneinfo import ZoneInfo

from config.settings import RUN_MODE, SYMBOL
from utils.logger import get_logger

logger = get_logger("main")


# ─────────────────────────────────────────────────────────────────────────────
def run_backtest(data_path: str, initial_balance: float = 10_000.0, allowed_strategies: list[str] = None, use_mt5: bool = False) -> None:
    if allowed_strategies is None:
        allowed_strategies = ["all"]
    from data.fetcher import fetch_from_file, fetch_from_mt5
    from backtest.engine import BacktestEngine
    from backtest.metrics import print_report, trade_log_to_df, equity_curve_to_df

    if use_mt5:
        logger.info(f"🔄 Fetching latest 55,000 bars from MT5 logic (M15)")
        from execution.mt5_client import MT5Client
        mt5_tmp = MT5Client()
        mt5_tmp.connect()
        df = fetch_from_mt5(mt5_tmp, "XAUUSD", "M15", count=55000)
        mt5_tmp.disconnect()
    else:
        logger.info(f"🔄 Loading data: {data_path}")
        df = fetch_from_file(data_path, timeframe_label="M15")

    logger.info(f"🚀 Starting backtest: {len(df)} bars | balance=${initial_balance:,.2f} | strategies={allowed_strategies}")
    engine = BacktestEngine(initial_balance=initial_balance, use_ai=False, allowed_strategies=allowed_strategies)
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
def run_live(paper: bool = False, allowed_strategies: list[str] = None) -> None:
    if allowed_strategies is None:
        allowed_strategies = ["all"]
        
    from execution.mt5_client  import MT5Client
    from execution.order_manager import OrderManager
    from execution.position_manager import PositionManager
    from core.risk.portfolio   import Portfolio
    from core.risk.executor    import RiskExecutor

    from core.signal.advanced_signal_engine import AdvancedSignalEngine
    from core.signal.reversal_engine import ImpulseRetestEngine
    from config.trading_config import TRADING_CONFIG, TradeMode
    from core.structure.swing  import detect_swings
    from ai.scorer             import AIScorer
    from ai.learning_engine    import LearningEngine
    from data.fetcher          import fetch_batch_mt5
    from database.connection   import init_db
    from database.repository   import TradeRepository
    from telegram.bot          import TelegramBot
    from telegram.pause_manager import pause_manager
    from core.data.calendar_api import CalendarAPI
    from core.data.news_redis   import news_client as _news_client
    from core.risk.management  import ManagementLogic
    from utils.report_formatter import format_eod_report
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
    if not account:
        logger.critical("Failed to retrieve account info from MT5 on startup")
        sys.exit(1)

    balance    = account["balance"]
    account_id = str(account["login"])
    logger.info(f"Account: #{account_id}  balance={balance:.2f}")

    # ── Load per-account config (DB → .env → default) ─────────────────────────
    try:
        from config.config_loader import ConfigLoader
        ConfigLoader.load(account_id)
    except Exception as _cfg_err:
        logger.warning(f"Config load failed — using defaults: {_cfg_err}")

    # ── Core components ───────────────────────────────────────────────────────
    portfolio     = Portfolio(balance)

    # ── State repository (daily cooldown persistence) ─────────────────────────
    from database.state_repo import StateRepository
    state_repo = StateRepository(account_id)

    # ── Initial Sync (Before Telegram/Scanning) ──────────────────────────────
    if not paper:
        # Sync current account state (equity, floating)
        portfolio.sync_actual_account(account["balance"], account["equity"])

        # Calculate daily start reference point to handle restarts accurately
        daily_realized = mt5.get_daily_realized_pnl()
        day_start_ref = account["balance"] - daily_realized
        portfolio.record_day_start(custom_balance=day_start_ref)

        # Restore today's cooldown state (protections survive bot restarts)
        _saved_state = state_repo.load()
        if _saved_state:
            import core.risk.filters as _filters
            if _saved_state.get("daily_cutloss_triggered"):
                portfolio.daily_cutloss_triggered = True
                logger.warning("Restored: daily_cutloss_triggered=True from DB — trading halted")
            else:
                # Cutloss was reset (DB=False). Offset current MT5 losses so they
                # don't immediately re-trigger on this restart.
                portfolio._cutloss_pnl_offset = daily_realized
                logger.info(
                    f"Cutloss DB=False: applying PnL offset {daily_realized:.2f} "
                    f"to prevent re-trigger of pre-reset losses"
                )
            if _saved_state.get("pulse_suspended_today"):
                portfolio.pulse_suspended_today = True
                logger.info("Restored: pulse_suspended_today=True from DB")
            if _saved_state.get("pulse_consecutive_losses"):
                portfolio.pulse_consecutive_losses = _saved_state["pulse_consecutive_losses"]
            if _saved_state.get("revenge_cooldown_cleared_at"):
                _filters.GLOBAL_COOLDOWN_CLEARED_AT = _saved_state["revenge_cooldown_cleared_at"]
                logger.info(f"Restored: GLOBAL_COOLDOWN_CLEARED_AT={_filters.GLOBAL_COOLDOWN_CLEARED_AT}")

        # Count trades since 00:00 (via MT5Client — Linux-safe)
        deals = mt5.get_daily_deals_today()
        if deals:
            trades_today = len(deals)
        else:
            trades_today = 0
    else:
        portfolio.record_day_start()
        trades_today = 0

    signal_engine   = AdvancedSignalEngine()
    reversal_engine = ImpulseRetestEngine()
    
    from core.signal.pulse_engine import PulseEngine
    pulse_engine = PulseEngine()

    from core.signal.reversal_engine import ImpulseRetestEngine
    reversal_engine = ImpulseRetestEngine()

    from core.signal.fibo_engine import FiboEngine
    fibo_engine = FiboEngine()

    from core.signal.velocity_engine import VelocityEngine
    velo_engine = VelocityEngine()

    from core.signal.mean_reversion_engine import MeanReversionEngine
    mr_engine = MeanReversionEngine()
    
    from core.structure.multi_tf_analyzer import MultiTFAnalyzer
    multi_tf = MultiTFAnalyzer()
    
    risk_executor = RiskExecutor(portfolio)
    from config.settings import USE_AI
    ai_scorer     = AIScorer(enabled=USE_AI and not paper)
    calendar      = CalendarAPI()
    learning_engine = LearningEngine(repo)

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram = TelegramBot(
        portfolio              = portfolio,
        db_repo                = repo,
        trades_today_ref       = lambda: trades_today,
        multi_tf               = multi_tf,
        loss_pause_minutes     = 60,
        consecutive_loss_trigger = 3,
        daily_loss_pct_trigger = 2.0,
    )
    # Start Telegram
    telegram.start()

    # ── Order manager (with telegram wired in) ────────────────────────────────
    order_manager = OrderManager(mt5, portfolio, repo, telegram=telegram)
    telegram.order_manager = order_manager
    telegram.account_id    = account_id   # expose for /config command
    telegram.state_repo    = state_repo   # expose for /reset cooldown persistence

    # ── Reconcile positions on startup ────────────────────────────────────────
    pos_manager = PositionManager(mt5, portfolio, repo)
    pos_manager.sync_on_startup()

    # ── Loop state ────────────────────────────────────────────────────────────
    last_total_trades = portfolio.total_trades
    _last_summary_date = None
    _last_full_scan_time = 0
    _last_htf_fetch_time = 0
    _last_learning_date  = None
    recovery_multiplier = 1.0
    _last_evaluated_closed_time = portfolio.last_trade_closed_at
    df_h4        = None
    df_h1        = None
    df_m30       = None
    df_m15_cache = None
    df_m5        = None
    df_m1        = None
    _last_performance_time = 0
    _last_trend_update_time = time.time()  # Initialize timing for recurring updates

    # News/sentiment cache — updated in full-scan block and consumed on every
    # tick by monitor_and_manage() so profit-lock / sentiment exit can act even
    # between full scans.
    _cached_news_active:    bool           = False
    _cached_news_sentiment: Optional[str]  = None
    
    # Cache account_id for snapshots
    acc_info = mt5.get_account_info()
    cached_acc_id = str(acc_info["login"]) if acc_info else None
    
    logger.info("🚀 System ready. Monitoring market...")
    logger.info(f"✅ System ready — entering {mode_label} main loop")

    try:
        while True:
            now = datetime.now(timezone.utc)
            
            # Update recovery multiplier strictly based on CLOSED trades
            if portfolio.last_trade_closed_at != _last_evaluated_closed_time:
                _last_evaluated_closed_time = portfolio.last_trade_closed_at
                mode_cfg = TRADING_CONFIG.mode_settings.get(TRADING_CONFIG.current_mode, {})
                if mode_cfg.get("use_recovery", False):
                    if portfolio.last_trade_result_pnl < 0:
                        recovery_multiplier = min(recovery_multiplier * mode_cfg.get("recovery_multiplier", 1.5), 4.0)
                        logger.info(f"MARTINGALE: Previous trade lost. Multiplier increased to {recovery_multiplier}x")
                    else:
                        recovery_multiplier = 1.0
                        logger.info("MARTINGALE: Previous trade won. Multiplier reset to 1.0x")
                else:
                    recovery_multiplier = 1.0

            # ── Fast Loop (Every ~2s) ─────────────────────────────────────────
            # Monitoring Open Positions & Real-time Sync
            tick = mt5.get_symbol_tick(SYMBOL)
            if tick:
                price = (tick["bid"] + tick["ask"]) / 2
                
                # ── Auto-sync manual MT5 positions so the bot can protect them ──
                if not paper:
                    try:
                        active_pos = mt5.get_raw_positions(SYMBOL)  # Linux-safe
                        active_tickets = {str(p.ticket) for p in active_pos} if active_pos else set()
                        
                        # Detect externally closed positions and resolve them
                        for t_id, t_data in list(portfolio.open_trades.items()):
                            mt5_ticket = str(t_data.get("mt5_ticket", ""))
                            if mt5_ticket and mt5_ticket not in active_tickets:
                                logger.warning(f"🔄 Trade {t_id} (ticket #{mt5_ticket}) closed externally. Resolving in portfolio...")
                                from core.risk.portfolio import ClosedTrade
                                ct = ClosedTrade(
                                    trade_id   = t_id,
                                    symbol     = t_data.get("symbol", SYMBOL),
                                    direction  = t_data.get("direction", "buy"),
                                    entry_price= t_data.get("entry_price", price),
                                    exit_price = price, 
                                    lot_size   = t_data.get("lot_size", 0.0),
                                    pnl        = t_data.get("current_pnl", 0.0),
                                    pnl_pips   = 0.0,
                                    opened_at  = t_data.get("opened_at") or datetime.now(timezone.utc),
                                    closed_at  = datetime.now(timezone.utc),
                                    reason     = "external_close",
                                )
                                portfolio.close_trade(ct)
                                order_manager.repo.save_trade_close(ct)

                        if active_pos:
                            for p in active_pos:
                                t_id = str(p.ticket)
                                comment = p.comment.upper()
                                # Only sync trades NOT opened by the bot (avoid ALPHA, PULSE, SIG prefixes)
                                if t_id not in portfolio.open_trades and "ALPHA" not in comment and "PULSE" not in comment and not comment.startswith("SIG:"):
                                    logger.info(f"🔄 Sync'ing Custom/Manual MT5 Trade #{t_id} into Bot Management.")
                                    
                                    actual_sl = p.sl
                                    if p.sl == 0.0:
                                        sl_dist = max(price * 0.002, 1.5)  # fallback 15 pips XAUUSD
                                        if df_m15_cache is not None and not df_m15_cache.empty and len(df_m15_cache) >= 15:
                                            high = df_m15_cache["high"].iloc[-14:].values
                                            low = df_m15_cache["low"].iloc[-14:].values
                                            close = df_m15_cache["close"].iloc[-15:-1].values
                                            if len(close) < 14: close = df_m15_cache["close"].iloc[-14:].values
                                            hl = high - low
                                            hc = [abs(h - c) for h, c in zip(high, close)]
                                            lc = [abs(l - c) for l, c in zip(low, close)]
                                            tr = [max(a, b, c) for a, b, c in zip(hl, hc, lc)]
                                            atr_rough = sum(tr) / len(tr) if tr else 0.0
                                            sl_dist = max(atr_rough * 2.5, sl_dist)
                                        auto_sl = p.price_open - sl_dist if p.type == 0 else p.price_open + sl_dist
                                        try:
                                            res = mt5.modify_position(p.ticket, sl=auto_sl)
                                            if not res.get("error"):
                                                actual_sl = auto_sl
                                                logger.info(f"🛡️ Auto-assigned SL {auto_sl:.2f} for manual trade #{t_id}")
                                        except Exception as e:
                                            logger.error(f"Failed to auto-assign SL for manual trade: {e}")

                                    portfolio.open_trade(t_id, {
                                        "symbol": p.symbol,
                                        "direction": "buy" if p.type == 0 else "sell",
                                        "entry_price": p.price_open,
                                        "stop_loss": actual_sl,
                                        "take_profit": p.tp,
                                        "lot_size": p.volume,
                                        "mt5_ticket": p.ticket,
                                        "opened_at": datetime.now(timezone.utc),
                                        "is_extreme": True,
                                    })
                                else:
                                    # Already synced: monitor for manual SL/TP changes
                                    t_data = portfolio.open_trades.get(t_id)
                                    if t_data:
                                        try:
                                            p_node = mt5.get_raw_position_by_ticket(int(t_id))  # Linux-safe
                                            if p_node:
                                                if p_node.sl != 0 and abs(p_node.sl - t_data["stop_loss"]) > price * 0.0001:
                                                    logger.info(f"🔄 Manual SL Change detected for #{t_id}")
                                                    portfolio.update_trade_sl_tp(t_id, sl=p_node.sl)
                                                if p_node.tp != 0 and abs(p_node.tp - t_data["take_profit"]) > price * 0.0001:
                                                    logger.info(f"🔄 Manual TP Change detected for #{t_id}")
                                                    portfolio.update_trade_sl_tp(t_id, tp=p_node.tp)
                                        except:
                                            pass
                    except Exception as e:
                        pass
                
                # Pass current HTF bias so contra-exit logic can evaluate it
                _cached_mtf_for_monitor = multi_tf.last_result
                _htf_bias_for_monitor   = _cached_mtf_for_monitor.master_bias if _cached_mtf_for_monitor else None
                order_manager.monitor_and_manage(
                    price,
                    df_m15         = df_m15_cache,
                    htf_bias       = _htf_bias_for_monitor,
                    news_active    = _cached_news_active,
                    news_sentiment = _cached_news_sentiment,
                )

                # Update account info real-time for true equity tracking
                account = mt5.get_account_info()
                if account:
                    if not paper:
                        portfolio.sync_actual_account(account["balance"], account["equity"])
                        # Read true realized PnL exactly from MT5 deal sum
                        true_pnl = mt5.get_daily_realized_pnl()
                        portfolio.sync_true_pnl(true_pnl)

                        # Hard cutloss check — close all + halt if realized loss >= 5%
                        if order_manager.check_hard_cutloss():
                            _dd_pct  = portfolio.drawdown_pct
                            _rl_pct  = portfolio.realized_daily_loss_pct
                            _msg = (
                                f"🔴 HARD CUTLOSS TRIGGERED\n"
                                f"Realized daily loss: {_rl_pct:.1f}%\n"
                                f"Equity drawdown: {_dd_pct:.1f}%\n"
                                f"All positions closed. Trading halted for today."
                            )
                            telegram.notifier.send(_msg, parse_mode="")
                            # Persist cutloss state so it survives a bot restart today
                            state_repo.save_from_portfolio(portfolio)
                    else:
                        portfolio.update_equity(account["equity"] - account["balance"])
            


            # ── Throttled Full Scan (Every ~3s for M1 Stream) ───────────────────
            if time.time() - _last_full_scan_time < 3:
                time.sleep(1)
                continue
            
            _last_full_scan_time = time.time()

            # ── Pause guard & Data Fetch ─────────────────────────────────────
            if not telegram.is_trading_allowed:
                continue

            # ── Parallel Data Fetch (All TFs at once) ─────────────────────────
            tfs_to_fetch = ["H4", "H1", "M30", "M15", "M5", "M1"]
            batch_data = fetch_batch_mt5(mt5, tfs_to_fetch, SYMBOL, count=300)
            
            df_h4  = batch_data.get("h4")
            df_h1  = batch_data.get("h1")
            df_m30 = batch_data.get("m30")
            df_m15 = batch_data.get("m15")
            df_m5  = batch_data.get("m5")
            df_m1  = batch_data.get("m1")
            
            # Ensure critical data exists before proceeding
            if df_h1 is None or df_m15 is None or df_m1 is None:
                logger.warning("Main: Parallel fetch returned missing critical timeframes (H1/M15/M1). Retrying...")
                time.sleep(1)
                continue
                
            df_m15_cache = df_m15

            # ── Trend analysis (change-detect + periodic update) ──────────────
            # Runs on every loop iteration that has HTF data available.
            # Change-detect: only notifies when master bias flips.
            # Periodic: forces a status report every N minutes regardless.
            if df_h4 is not None and df_h1 is not None:
                try:
                    _trend_logic  = multi_tf.trend  # Reuse cached instance — no need to re-create every cycle
                    _analysis     = multi_tf.analyze(
                        df_h4=df_h4, df_h1=df_h1, df_m30=df_m30,
                        df_m15=df_m15_cache, df_m5=df_m5, df_m1=df_m1
                    )
                    _master_bias  = _analysis.master_bias
                    _master_tf    = _analysis.tfs.get("h4") or _analysis.tfs.get("h1")

                    if _master_tf:
                        _df_ref = df_h4 if _master_tf.tf == "h4" else df_h1
                        _res    = _trend_logic.analyze_trend(_df_ref)
                        _emas   = _trend_logic.calculate_emas(_df_ref)

                        _trend_params = dict(
                            bias              = _master_bias,
                            trend_strength    = _res.get("trend_strength", "NEUTRAL"),
                            adx               = _master_tf.adx,
                            ema_cross_bullish = _master_tf.ema_cross,
                            price             = _df_ref["close"].iloc[-1],
                            ema_50            = _emas["ema_50"].iloc[-1],
                            ema_200           = _emas["ema_200"].iloc[-1],
                            timeframe         = _master_tf.tf.upper(),
                            tf_confluence     = _analysis.tfs,
                        )

                        # 1. Change-detect notification (fires when bias flips)
                        telegram.notify_trend_change(**_trend_params)

                        # 2. Periodic status report (fires every N minutes)
                        interval_mins = TRADING_CONFIG.trend_update_interval_minutes
                        if interval_mins > 0 and (time.time() - _last_trend_update_time > interval_mins * 60):
                            _last_trend_update_time = time.time()
                            logger.info(f"📡 Periodic Market Structure Report ({interval_mins}m interval)")
                            telegram.notify_trend_change(**_trend_params, is_update=True)

                except Exception as e:
                    logger.debug(f"Trend analysis skipped: {e}")

            # MT5 Account sync
            
            account = mt5.get_account_info()
            if not account:
                time.sleep(2)
                continue
                
            margin_level = account.get("margin_level", 1000.0)
            # Prefer Redis (Go scraper) for news; fall back to CalendarAPI HTTP feed.
            # Sentiment is ALWAYS computed (independent of news_active) so that
            # quiet-period bias from recent events still informs exit decisions.
            news_active   = _news_client.is_news_active()
            dom_sentiment = _news_client.get_dominant_sentiment()
            # Legacy fallback when Redis scraper has not run yet
            if _news_client.last_scrape_age_minutes() is None:
                events      = calendar.get_high_impact_events()
                news_active = calendar.is_news_active(events)

            # Update tick-level cache consumed by monitor_and_manage()
            _cached_news_active    = news_active
            _cached_news_sentiment = dom_sentiment
            
            # ── Contra-Flip (after structural contra-exit) ───────────────────
            # If the previous monitor cycle cut a losing trade because HTF bias
            # flipped, execute an immediate entry in the new bias direction.
            # This flip bypasses the anti-revenge cooldown (it follows the market).
            if not paper:
                _flip = order_manager.pop_contra_flip()
                if _flip is not None:
                    _flip_age = (utc_now() - _flip["queued_at"]).total_seconds()
                    if _flip_age <= 120 and df_m1 is not None:  # only valid for 2 minutes
                        from core.signal.signal_engine import TradeSignal as _TS
                        from utils.time_utils import get_session_name as _gsn
                        from core.structure.swing import detect_swings as _ds
                        _fd        = _flip["direction"]
                        _fp        = price
                        _atr_m1    = (df_m1["high"] - df_m1["low"]).tail(20).mean()

                        # Structural SL: nearest M5 swing extreme (fallback ATR×2)
                        _flip_sl = None
                        try:
                            _fsw = _ds(df_m5.tail(80)) if df_m5 is not None else []
                            if _fd == "buy":
                                _lows = [s.price for s in _fsw if s.kind == 'low']
                                if _lows:
                                    _flip_sl = min(_lows[-1], _fp - _atr_m1 * 1.2) - _atr_m1 * 0.3
                            else:
                                _highs = [s.price for s in _fsw if s.kind == 'high']
                                if _highs:
                                    _flip_sl = max(_highs[-1], _fp + _atr_m1 * 1.2) + _atr_m1 * 0.3
                        except Exception:
                            pass
                        if _flip_sl is None:
                            _flip_sl = (_fp - _atr_m1 * 2.0) if _fd == "buy" else (_fp + _atr_m1 * 2.0)
                        _flip_risk = abs(_fp - _flip_sl)

                        # Fibo TP: 127.2% ext from nearest H1/M15 swing, fallback RR 2.0
                        _flip_tp = None
                        try:
                            for _df_tf in (df_h1, df_m15_cache):
                                if _df_tf is None or len(_df_tf) < 50:
                                    continue
                                _sw = _ds(_df_tf.tail(120))
                                _hs = [s.price for s in _sw if s.kind == 'high']
                                _ls = [s.price for s in _sw if s.kind == 'low']
                                if not (_hs and _ls):
                                    continue
                                _sh, _sl = _hs[-1], _ls[-1]
                                if _sh <= _sl:
                                    continue
                                _rng = _sh - _sl
                                if _fd == "buy":
                                    _c127 = _sh + _rng * 0.272
                                    _cand = _c127 if _c127 > _fp else (_sh + _rng * 0.618)
                                else:
                                    _c127 = _sl - _rng * 0.272
                                    _cand = _c127 if _c127 < _fp else (_sl - _rng * 0.618)
                                _cand_rr = abs(_cand - _fp) / _flip_risk if _flip_risk > 0 else 0
                                if 1.5 <= _cand_rr <= 5.0:
                                    _flip_tp = _cand
                                    logger.info(f"↩️ FLIP Fibo TP @ {_cand:.2f} (RR {_cand_rr:.2f})")
                                    break
                        except Exception:
                            pass
                        if _flip_tp is None:
                            _flip_tp = (_fp + _flip_risk * 2.0) if _fd == "buy" else (_fp - _flip_risk * 2.0)
                        _flip_sig  = _TS(
                            signal_id       = f"FLIP_{str(__import__('uuid').uuid4())[:6]}",
                            symbol          = SYMBOL,
                            timestamp       = utc_now(),
                            direction       = _fd,
                            entry_price     = _fp,
                            stop_loss       = _flip_sl,
                            take_profit     = _flip_tp,
                            rr_ratio        = round(abs(_flip_tp - _fp) / _flip_risk, 2) if _flip_risk > 0 else 2.0,
                            score           = 8,
                            max_score       = 15,
                            atr_pips        = _atr_m1 / 0.1,
                            session         = _gsn(utc_now()),
                            is_extreme      = False,
                            is_stalking     = False,
                            stalking_reason = "",
                            ai_decision     = "TAKE",
                            ai_confidence   = 0.75,
                            ai_reason       = f"Contra-flip: HTF bias is now {_flip['htf_bias']} post contra-exit",
                        )
                        _flip_spread = (tick["spread"] / 10.0) if tick and "spread" in tick else 3.0
                        _flip_order = risk_executor.evaluate(
                            signal=_flip_sig, df_m15=df_m15_cache, df_m5=df_m5, df_m1=df_m1,
                            swings=[], spread_pips=_flip_spread,
                            current_time=now.replace(tzinfo=None),
                            trades_today=trades_today, margin_level=margin_level, news_active=news_active,
                        )
                        if _flip_order and _flip_order.approved:
                            tid = order_manager.execute(_flip_order, signal_meta={
                                "score": 8, "session": _flip_sig.session,
                                "strategy": f"↩️ Contra-Flip ({_fd.upper()})",
                            }, df_m15=df_m15_cache, df_m5=df_m5)
                            if tid:
                                trades_today += 1
                                logger.info(f"↩️ Contra-flip executed: {_fd.upper()} after structural contra-exit")
                        else:
                            logger.info(f"↩️ Contra-flip rejected: {_flip_order.rejection_reason if _flip_order else 'no order'}")
                    elif _flip_age > 120:
                        logger.debug("Contra-flip expired (>2 min) — discarded")

            # ── Post-TP Re-entry ─────────────────────────────────────────
            # After a trade hits TP, check if price continues (same dir) or
            # flips (opposite) — immediate re-entry opportunity.
            if not paper and price:
                _tp_re = order_manager.consume_tp_reentry(
                    current_price=price,
                    htf_bias=_htf_bias_for_monitor or "",
                )
                if _tp_re is not None:
                    from core.signal.signal_engine import TradeSignal as _TS
                    from utils.time_utils import get_session_name as _gsn
                    _re_risk = abs(_tp_re["entry_price"] - _tp_re["stop_loss"])
                    _re_rr = abs(_tp_re["take_profit"] - _tp_re["entry_price"]) / _re_risk if _re_risk > 0 else 2.0
                    _re_sig = _TS(
                        signal_id       = f"RETP_{str(__import__('uuid').uuid4())[:6]}",
                        symbol          = _tp_re["symbol"],
                        timestamp       = utc_now(),
                        direction       = _tp_re["direction"],
                        entry_price     = _tp_re["entry_price"],
                        stop_loss       = _tp_re["stop_loss"],
                        take_profit     = _tp_re["take_profit"],
                        rr_ratio        = round(_re_rr, 2),
                        score           = 7,
                        max_score       = 15,
                        atr_pips        = _re_risk / 0.1,
                        session         = _gsn(utc_now()),
                        is_extreme      = False,
                        is_stalking     = False,
                        stalking_reason = "",
                        ai_decision     = "TAKE",
                        ai_confidence   = 0.70,
                        ai_reason       = _tp_re["reason"],
                    )
                    _re_spread = (tick["spread"] / 10.0) if tick and "spread" in tick else 3.0
                    _re_order = risk_executor.evaluate(
                        signal=_re_sig, df_m15=df_m15_cache, df_m5=df_m5, df_m1=df_m1,
                        swings=[], spread_pips=_re_spread,
                        current_time=now.replace(tzinfo=None),
                        trades_today=trades_today, margin_level=margin_level, news_active=news_active,
                    )
                    if _re_order and _re_order.approved:
                        tid = order_manager.execute(_re_order, signal_meta={
                            "score": 7, "session": _re_sig.session,
                            "strategy": f"⚡ Post-TP {_tp_re['reason']}",
                        }, df_m15=df_m15_cache, df_m5=df_m5)
                        if tid:
                            trades_today += 1
                            logger.info(f"⚡ Post-TP re-entry executed: {_tp_re['direction'].upper()} ({_tp_re['reason']})")
                    else:
                        logger.info(f"⚡ Post-TP re-entry rejected: {_re_order.rejection_reason if _re_order else 'no order'}")

            # ── Pulse Scalping ───────────────────
            if ("all" in allowed_strategies or "pulse" in allowed_strategies) and tick and not paper:
                current_spread_pips = (tick["spread"] / 10.0) if "spread" in tick else 3.0
                anchor_tp_buy  = portfolio.get_anchor_tp("buy")
                anchor_tp_sell = portfolio.get_anchor_tp("sell")

                # Re-use cached analysis (avoid double-compute, _ANALYZE_TTL_SECONDS handles freshness)
                _cached_mtf    = multi_tf.last_result
                _pulse_direction = _cached_mtf.direction if _cached_mtf else None

                pulse_signal = pulse_engine.generate(
                    df_h1=df_h1, df_m5=df_m5, df_m1=df_m1,
                    current_spread_pips=current_spread_pips,
                    balance=portfolio.balance,
                    is_suspended=portfolio.pulse_suspended_today,
                    daily_pnl=portfolio.daily_pnl,
                    mtf_direction=_pulse_direction,
                    anchor_tp=anchor_tp_buy if _pulse_direction == "buy" else anchor_tp_sell
                )
                
                if pulse_signal:
                    pulse_order = risk_executor.evaluate(
                        signal=pulse_signal, df_m15=df_m15_cache, df_m5=df_m5, df_m1=df_m1,
                        swings=[], spread_pips=current_spread_pips,
                        current_time=now.replace(tzinfo=None),
                        trades_today=trades_today, margin_level=margin_level, news_active=news_active
                    )
                    if pulse_order.approved:
                        tid = order_manager.execute(pulse_order, signal_meta={
                            "score": pulse_signal.score, "session": pulse_signal.session,
                            "strategy": "⚡ Pulse Scalper",
                        }, df_m15=df_m15_cache, df_m5=df_m5)
                        if tid: trades_today += 1

            # ── Day summary ──────────────────────
            if _last_summary_date != now.date():
                if _last_summary_date is not None:
                    telegram.send_daily_summary()
                _last_summary_date = now.date()
                portfolio.record_day_start()
                trades_today = 0
                
            # ── Reversal Engine (Spike/Retest/Exhaustion) ────────────────────
            if "all" in allowed_strategies or "rev" in allowed_strategies:
                reversal_signal = reversal_engine.generate(
                    df_m1=df_m1, df_m5=df_m5, df_h1=df_h1,
                    current_spread_pips=(tick["spread"] / 10.0) if tick and "spread" in tick else 3.0,
                )
                if reversal_signal and not paper:
                    rev_ai = ai_scorer.evaluate(reversal_signal)
                    reversal_signal.ai_confidence = rev_ai.confidence
                    reversal_signal.ai_reason     = rev_ai.reason
                    if rev_ai.decision != "TAKE":
                        logger.info(
                            f"🔄 Reversal {reversal_signal.signal_id} rejected by AI: "
                            f"conf={rev_ai.confidence:.2f} decision={rev_ai.decision} | {rev_ai.reason[:80]}"
                        )
                    else:
                        rev_swings = detect_swings(df_m15_cache) if df_m15_cache is not None else []
                        rev_spread = (tick["spread"] / 10.0) if tick and "spread" in tick else 3.0
                        rev_order = risk_executor.evaluate(
                            signal=reversal_signal, df_m15=df_m15_cache, df_m5=df_m5, df_m1=df_m1,
                            swings=rev_swings, spread_pips=rev_spread,
                            current_time=now.replace(tzinfo=None), trades_today=trades_today,
                            margin_level=margin_level, news_active=news_active,
                            recovery_multiplier=recovery_multiplier,
                            risk_pct_override=telegram.get_current_risk_pct()
                        )
                        if rev_order and rev_order.approved:
                            tid = order_manager.execute(rev_order, signal_meta={
                                "score": reversal_signal.score, "session": reversal_signal.session,
                                "ai_confidence": reversal_signal.ai_confidence,
                                "ai_reason": reversal_signal.ai_reason,
                                "strategy": "🔄 Reversal (MACD/Retest)",
                            }, df_m15=df_m15_cache, df_m5=df_m5)
                            if tid:
                                trades_today += 1
                                logger.info(f"⚡ Reversal Engine executed: {reversal_signal.signal_id}")
                        else:
                            logger.info(
                                f"🔄 Reversal {reversal_signal.signal_id} blocked by risk executor: "
                                f"{rev_order.rejection_reason if rev_order else 'no order returned'}"
                            )

            # ── Fibonacci MTF Engine ─────────────────────────────────────────
            if ("all" in allowed_strategies or "fibo" in allowed_strategies) and df_m15_cache is not None and df_m5 is not None and df_m1 is not None and not paper:
                try:
                    fibo_signal = fibo_engine.generate(
                        df_m15=df_m15_cache, df_m5=df_m5, df_m1=df_m1, symbol=SYMBOL
                    )
                    if fibo_signal:
                        spread_pips = (tick["spread"] / 10.0) if tick and "spread" in tick else 3.0
                        fibo_order = risk_executor.evaluate(
                            signal=fibo_signal, df_m15=df_m15_cache, df_m5=df_m5, df_m1=df_m1,
                            swings=[], spread_pips=spread_pips,
                            current_time=now.replace(tzinfo=None), trades_today=trades_today,
                            margin_level=margin_level, news_active=news_active,
                            recovery_multiplier=recovery_multiplier,
                            risk_pct_override=telegram.get_current_risk_pct(),
                        )
                        # Apply Tier B half-size
                        if fibo_order and fibo_order.approved:
                            if fibo_signal.lot_multiplier < 1.0:
                                fibo_order.lot_size = round(
                                    fibo_order.lot_size * fibo_signal.lot_multiplier, 2
                                )
                            # Pass fibo_levels onto the order for order_manager to track
                            fibo_order.fibo_levels = fibo_signal
                            tid = order_manager.execute(
                                fibo_order,
                                signal_meta={
                                    "score": fibo_signal.score, "session": fibo_signal.session,
                                    "strategy": f"📐 Fibo MTF (Tier {fibo_signal.signal_tier})",
                                },
                                df_m15=df_m15_cache,
                                df_m5=df_m5,
                            )
                            if tid:
                                trades_today += 1
                                logger.info(f"🎯 Fibo engine executed: {fibo_signal.signal_id} Tier={fibo_signal.signal_tier}")
                except Exception as e:
                    logger.debug(f"FiboEngine scan error (non-critical): {e}")

            # ── Velocity Burst Engine 🔥 ──────────────────────────────────────────
            if "all" in allowed_strategies or "velo" in allowed_strategies:
                try:
                    velo_signal = velo_engine.generate(
                        df_m1=df_m1,
                        current_spread_pips=(tick["spread"] / 10.0) if tick and "spread" in tick else 3.0,
                        balance=portfolio.balance
                    )
                    if velo_signal and not paper:
                        velo_swings = detect_swings(df_m15_cache) if df_m15_cache is not None else []
                        velo_spread = (tick["spread"] / 10.0) if tick and "spread" in tick else 3.0
                        velo_order = risk_executor.evaluate(
                            signal=velo_signal, df_m15=df_m15_cache, df_m5=df_m5, df_m1=df_m1,
                            swings=velo_swings, spread_pips=velo_spread,
                            current_time=now.replace(tzinfo=None), trades_today=trades_today,
                            margin_level=margin_level, news_active=news_active,
                            recovery_multiplier=recovery_multiplier,
                            risk_pct_override=telegram.get_current_risk_pct()
                        )
                        if velo_order and velo_order.approved:
                            tid = order_manager.execute(velo_order, signal_meta={
                                "score": velo_signal.score, "session": velo_signal.session,
                                "strategy": "🔥 Velocity Breakout",
                            }, df_m15=df_m15_cache, df_m5=df_m5)
                            if tid:
                                trades_today += 1
                                logger.info(f"💥 Velocity Engine executed: {velo_signal.signal_id}")
                except Exception as e:
                    logger.debug(f"Velocity Engine scan error (non-critical): {e}")



            # ── Mean Reversion Engine (Sideways harvest) ─────────────────────
            if ("all" in allowed_strategies or "mr" in allowed_strategies) and df_m15_cache is not None and not paper:
                try:
                    mr_signal = mr_engine.generate(
                        df_m15=df_m15_cache, df_m5=df_m5, current_time=now.replace(tzinfo=None), symbol=SYMBOL
                    )
                    if mr_signal:
                        mr_spread = (tick["spread"] / 10.0) if tick and "spread" in tick else 3.0
                        mr_order = risk_executor.evaluate(
                            signal=mr_signal, df_m15=df_m15_cache, df_m5=df_m5, df_m1=df_m1,
                            swings=[], spread_pips=mr_spread,
                            current_time=now.replace(tzinfo=None), trades_today=trades_today,
                            margin_level=margin_level, news_active=news_active,
                            recovery_multiplier=recovery_multiplier,
                            risk_pct_override=telegram.get_current_risk_pct(),
                        )
                        if mr_order and mr_order.approved:
                            tid = order_manager.execute(mr_order, signal_meta={
                                "score": mr_signal.score, "session": mr_signal.session,
                                "strategy": "〰️ Mean Reversion",
                            }, df_m15=df_m15_cache, df_m5=df_m5)
                            if tid:
                                trades_today += 1
                                logger.info(f"〰️ Mean Reversion executed: {mr_signal.signal_id}")
                except Exception as e:
                    logger.debug(f"MeanReversion scan error (non-critical): {e}")

            # ── Generate signal (Standard) ───────
            if "all" in allowed_strategies or "smc" in allowed_strategies:
                signal = signal_engine.generate(
                    df_h4=df_h4, df_h1=df_h1, df_m30=df_m30,
                    df_m15=df_m15_cache, df_m5=df_m5, df_m1=df_m1,
                    current_time=now.replace(tzinfo=None), symbol=SYMBOL
                )

                if signal:
                    repo.save_signal(signal)
                    if signal.is_stalking:
                        telegram.notify_stalking_setup(signal, mode_label=TRADING_CONFIG.current_mode.value)
                        if time.time() - _last_performance_time > 900:
                            repo.save_performance_snapshot(portfolio, trades_today, account_id=cached_acc_id)
                            _last_performance_time = time.time()
                        continue

                    ai_eval = ai_scorer.evaluate(signal)
                    signal.ai_confidence = ai_eval.confidence
                    signal.ai_reason     = ai_eval.reason

                    if ai_eval.decision == "TAKE":
                        swings = detect_swings(df_m15_cache)
                        spread_pips = (tick["spread"] / 10.0) if tick and "spread" in tick else 3.0
                        order = risk_executor.evaluate(
                            signal=signal, df_m15=df_m15_cache, df_m5=df_m5, df_m1=df_m1,
                            swings=swings, spread_pips=spread_pips,
                            current_time=now.replace(tzinfo=None), trades_today=trades_today,
                            margin_level=margin_level, news_active=news_active,
                            recovery_multiplier=recovery_multiplier,
                            risk_pct_override=telegram.get_current_risk_pct()
                        )

                        if order and not order.approved:
                            logger.warning(
                                f"🚫 SMC REJECTED: {order.rejection_reason} "
                                f"| signal={signal.signal_id} score={signal.score}"
                            )
                        if order and order.approved:
                            # Apply tier-based lot multiplier (THIN_RETRACE = 0.5x)
                            if getattr(signal, "lot_multiplier", 1.0) < 1.0:
                                order.lot_size = round(
                                    order.lot_size * signal.lot_multiplier, 2
                                )
                                logger.info(
                                    f"📉 SMC tier {getattr(signal, 'entry_tier', 'ZONE')}: "
                                    f"lot scaled x{signal.lot_multiplier} → {order.lot_size}"
                                )
                            tid = order_manager.execute(order, signal_meta={
                                "score": signal.score, "session": signal.session,
                                "ai_confidence": signal.ai_confidence, "ai_reason": signal.ai_reason,
                                "strategy": f"🛡️ SMC Standard ({getattr(signal, 'entry_tier', 'ZONE')})",
                            }, df_m15=df_m15_cache, df_m5=df_m5)
                            if tid:
                                trades_today += 1

            # ── Performance snapshot (15m) ──────
            if time.time() - _last_performance_time > 900:
                repo.save_performance_snapshot(portfolio, trades_today, account_id=cached_acc_id)
                _last_performance_time = time.time()

            # ── EOD AI Retrospective (23:00 Local) ──
            local_now = datetime.now(ZoneInfo("Asia/Jakarta"))
            if local_now.hour == 23 and local_now.minute < 3 and _last_learning_date != local_now.date():
                logger.info("🧠 Triggering Automated EOD AI Retrospective Analysis...")
                try:
                    # Analyze slightly more trades for the EOD report
                    analysis = learning_engine.run_retrospective(trade_limit=30)
                    report = format_eod_report(analysis)
                    telegram.notifier.send(report)
                    _last_learning_date = local_now.date()
                except Exception as e:
                    logger.error(f"EOD Auto-trigger failed: {e}")

            logger.info(
                f"Cycle OK | equity={portfolio.equity:.2f} "
                f"dd={portfolio.drawdown_pct:.2f}% "
                f"open_risk=${portfolio.total_open_risk_usd:.2f} "
                f"daily_pnl=${portfolio.daily_pnl:.2f} "
                f"open={portfolio.open_trade_count}"
            )

    except KeyboardInterrupt:
        logger.info("🛑 Shutdown triggered")
    finally:
        telegram.stop()
        mt5.disconnect()
        logger.info("Shutdown complete")


# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="AI XAUUSD Trading System")
    parser.add_argument("--mode",    default=RUN_MODE, choices=["backtest", "live", "paper"])
    parser.add_argument("--mt5", action="store_true", help="Fetch history directly from MT5 (default for backtest)")
    parser.add_argument("--balance", type=float, default=10_000.0)
    parser.add_argument("--strategy", type=str, default="all", help="Comma-separated list of strategies to run: smc,fibo,rev,pulse. Default 'all'.")
    args = parser.parse_args()

    allowed_strats = args.strategy.lower().split(",") if args.strategy else ["all"]

    if args.mode == "live":
        run_live(paper=False, allowed_strategies=allowed_strats)
    elif args.mode == "paper":
        run_live(paper=True, allowed_strategies=allowed_strats)
    else:
        run_backtest(data_path=None, initial_balance=args.balance, allowed_strategies=allowed_strats, use_mt5=True)

if __name__ == "__main__":
    main()
