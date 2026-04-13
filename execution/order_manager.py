"""
Order Manager.

Handles the lifecycle of a single trade:
  open → monitor → partial close at TP1 → trail / close at TP2/SL

Integrates with TelegramBot for real-time notifications.
"""
from __future__ import annotations
import uuid
import pandas as pd
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from core.risk.executor import TradeOrder
from core.risk.portfolio import Portfolio, ClosedTrade
from core.risk.take_profit import trailing_stop
from core.risk.fibo_trailing import update_fibo_trail
from core.risk.lot_sizing import round_number_offset

from core.structure.smc_logic import SMCLogic
from execution.mt5_client import MT5Client
from database.repository import TradeRepository
from utils.logger import get_logger
from utils.chart_renderer import ChartRenderer
from config.trading_config import TRADING_CONFIG, TradeMode
from config.risk_config import RISK_CONFIG

if TYPE_CHECKING:
    from telegram.bot import TelegramBot

logger = get_logger(__name__)

# Breakeven activation: move SL to entry once profit >= 1R
_BE_ACTIVATION_R = 1.0


class OrderManager:
    def __init__(
        self,
        mt5:       MT5Client,
        portfolio: Portfolio,
        repo:      Optional[TradeRepository] = None,
        telegram:  Optional["TelegramBot"]   = None,
    ):
        self.mt5            = mt5
        self.portfolio      = portfolio
        self.repo           = repo
        self.telegram       = telegram
        self.smc            = SMCLogic()
        self.chart_renderer = ChartRenderer()

    def execute(self, order: TradeOrder, signal_meta: dict | None = None,
                df_m15: Optional[pd.DataFrame] = None,
                df_m5:  Optional[pd.DataFrame] = None) -> Optional[str]:
        """
        Execute an approved TradeOrder via MT5.
        Returns the internal trade_id or None on failure.
        signal_meta: optional dict with score, session, ai_confidence, ai_reason
        df_m15, df_m5: optional OHLCV data for chart rendering after entry
        """
        if not order.approved:
            logger.warning(f"Attempted to execute rejected order: {order.rejection_reason}")
            return None

        # PULSE trades have no structural tp_levels, use signal TP directly
        if order.take_profit_levels is not None:
            tp2 = order.take_profit_levels.tp2
        else:
            tp2 = order.take_profit  # Fallback for PULSE (uses signal.take_profit)

        result = self.mt5.place_market_order(
            symbol     = order.symbol,
            direction  = order.direction,
            lot_size   = order.lot_size,
            stop_loss  = order.stop_loss,
            take_profit= tp2,
            comment    = f"sig:{order.signal_id}",
        )

        if result is None or result.get("error"):
            err_msg = result.get("comment", "Unknown Error") if result else "Connection failed"
            retcode = result.get("retcode", 0) if result else 0
            logger.error(f"Execution failed for {order.signal_id}: {err_msg} ({retcode})")
            
            if self.telegram:
                if retcode == 10019:  # TRADE_RETCODE_NO_MONEY
                    self.telegram.send_message(f"⚠️ *OVER CAPACITY*: Margin tidak cukup untuk buka posisi baru\\!\nReason: `{err_msg}`")
                elif retcode == 10022: # TRADE_RETCODE_LIMIT_ORDERS
                    self.telegram.send_message(f"⚠️ *OVER CAPACITY*: Limit total posisi broker tercapai\\!\nReason: `{err_msg}`")
                
            return None

        # Use signal_id for PULSE trades so guards can identify them
        if "PULSE" in order.signal_id:
            trade_id = order.signal_id
        else:
            trade_id = str(uuid.uuid4())[:12]
        mt5_ticket   = result.get("order", 0)
        actual_entry = result.get("price", order.entry_price)

        trade_info = {
            "mt5_ticket":   mt5_ticket,
            "signal_id":    order.signal_id,
            "symbol":       order.symbol,
            "direction":    order.direction,
            "entry_price":  actual_entry,
            "lot_size":     order.lot_size,
            "stop_loss":    order.stop_loss,
            "take_profit":  tp2,
            "tp_levels":    order.take_profit_levels,
            "risk_amount":  order.risk_amount,
            "risk_pct":     order.risk_pct,
            "opened_at":    datetime.now(timezone.utc),
            "be_activated": False,
            # ── Strategy / engine label ──
            "strategy":     (signal_meta or {}).get("strategy", ""),
            # ── Fibo Staged Trailing fields ──
            "fibo_levels":  getattr(order, "fibo_levels", None),
            "fibo_stage":   0,
            "original_lot": order.lot_size,  # Track original for partial close %
        }
        self.portfolio.open_trade(trade_id, trade_info)

        if self.repo:
            acc_info = self.mt5.get_account_info()
            acc_id = str(acc_info["login"]) if acc_info else None
            self.repo.save_trade_open(trade_id, order, actual_entry, account_id=acc_id)

        # ── Telegram: entry notification ─────────────────────────────────────
        if self.telegram:
            meta = signal_meta or {}
            self.telegram.notify_trade_opened(
                trade_id      = trade_id,
                direction     = order.direction,
                entry_price   = actual_entry,
                stop_loss     = order.stop_loss,
                take_profit   = tp2,
                lot_size      = order.lot_size,
                risk_amount   = order.risk_amount,
                risk_pct      = order.risk_pct,
                signal_score  = meta.get("score", 0),
                session       = meta.get("session", ""),
                ai_confidence = meta.get("ai_confidence", 0.0),
                ai_reason     = meta.get("ai_reason", ""),
                opened_at     = trade_info["opened_at"],
                strategy      = meta.get("strategy", ""),
            )

        logger.info(
            f"✅ Trade opened: {trade_id} | MT5#{mt5_ticket} | "
            f"{order.direction.upper()} {order.lot_size}L @ {actual_entry:.2f}"
        )
        
        # ── Global Basket SL Sync ──────────────────────────────────────────────
        self.sync_basket_sl(order.symbol, order.direction, order.stop_loss)

        # ── Chart Capture → Telegram (all trades) ─────────────────────────────
        if self.telegram and df_m15 is not None:
            try:
                fibo_lvls = trade_info.get("fibo_levels")
                tp_lvls   = order.take_profit_levels
                strategy  = trade_info.get("strategy", "")

                # Resolve TPs: Fibo uses its own levels, others use tp_levels or order.take_profit
                if fibo_lvls:
                    _tp1, _tp2, _tp3 = None, None, None  # Fibo renderer draws its own
                elif tp_lvls:
                    _tp1 = tp_lvls.tp1
                    _tp2 = tp_lvls.tp2
                    _tp3 = tp_lvls.tp3
                else:
                    _tp1, _tp2, _tp3 = tp2, None, None  # Pulse/simple: just main TP

                img_bytes = self.chart_renderer.render_chart(
                    df_m15      = df_m15,
                    df_m5       = df_m5,
                    entry_price = actual_entry,
                    stop_loss   = order.stop_loss,
                    direction   = order.direction,
                    trade_id    = trade_id,
                    fibo_levels = fibo_lvls,
                    tp1 = _tp1,
                    tp2 = _tp2,
                    tp3 = _tp3,
                    symbol      = order.symbol,
                    strategy    = strategy,
                )
                if img_bytes:
                    strat_label = f" | {strategy}" if strategy else ""
                    caption = (
                        f"📈 *{order.direction.upper()}{strat_label}*\n"
                        f"Entry: `{actual_entry:.2f}` │ SL: `{order.stop_loss:.2f}` │ TP: `{tp2:.2f}`"
                    )
                    self.telegram.notifier.send_photo(img_bytes, caption)
                    logger.info(f"📸 Chart sent to Telegram for {trade_id}")
            except Exception as e:
                logger.warning(f"Chart render failed (non-critical): {e}")
        
        return trade_id

    def sync_basket_sl(self, symbol: str, direction: str, new_sl: float) -> None:
        """
        Synchronizes SL for all open positions in the same direction.
        Ensures the entire 'basket' is protected by the latest structural SL.
        """
        open_trades = self.portfolio.open_trades
        synced_count = 0
        
        for tid, trade in open_trades.items():
            if trade["symbol"] == symbol and trade["direction"] == direction:
                # If buying, we want the HIGHEST SL. If selling, we want the LOWEST SL.
                is_safer = (direction == "buy" and new_sl > trade["stop_loss"]) or \
                           (direction == "sell" and new_sl < trade["stop_loss"] or trade["stop_loss"] == 0)
                
                if is_safer:
                    trade["stop_loss"] = new_sl
                    # In live mode, update MT5 as well
                    if trade.get("mt5_ticket"):
                        res = self.mt5.modify_position(trade["mt5_ticket"], sl=new_sl)
                        if isinstance(res, dict) and res.get("error"):
                            logger.warning(f"Basket SL Sync logic fallback: MT5 rejected SL {new_sl} for #{trade['mt5_ticket']}")
                    synced_count += 1
        
        if synced_count > 0:
            logger.info(f"🔄 Basket SL Sync: Updated {synced_count} positions to SL {new_sl:.2f}")

    def check_weekend_shutdown(self) -> bool:
        """
        Hard-closes all positions on Friday 21:00 UTC to avoid Monday gaps.
        Returns True if shutdown is active.
        """
        now = datetime.now(timezone.utc)
        # Friday is 4
        if now.weekday() == 4 and now.hour >= 21:
            if self.portfolio.open_trade_count > 0:
                logger.warning("🏁 Weekend Protection Active: Closing all positions before market close.")
                self.close_all_positions("weekend_shutdown")
                if self.telegram:
                    self.telegram.send_message("🏁 *Weekend Protection Active*: Semua posisi ditutup otomatis untuk menghindari Gap Senin.")
            return True
        return False

    def check_eod_shutdown(self) -> bool:
        """
        Closes all positions at 23:45 UTC/MT5 time to avoid overnight swap and spreads.
        """
        if not RISK_CONFIG.auto_close_eod:
            return False
            
        now = datetime.now(timezone.utc)
        if now.hour == 23 and now.minute >= 45:
            if self.portfolio.open_trade_count > 0:
                logger.warning("🌙 End of Day Protection: Closing all positions before rollover.")
                self.close_all_positions("daily_eod")
                if self.telegram:
                    self.telegram.send_message("🌙 *End of Day Protection Active*: Semua posisi ditutup otomatis untuk menghindari Swap/Spread malam.")
            return True
        return False

    def close_all_positions(self, reason: str = "manual") -> None:
        """Emergency or scheduled closure of the entire portfolio."""
        for tid in list(self.portfolio.open_trades.keys()):
            trade = self.portfolio.open_trades[tid]
            ticket = trade.get("mt5_ticket")
            if ticket:
                self.close_by_ticket(ticket, reason)

    def check_smart_auto_close(self, trade_id: str, trade: dict, current_price: float, df_m15: Optional[pd.DataFrame]) -> bool:
        """
        Evaluates stagnation and reversal cut-loss conditions. 
        Returns True if the trade was closed, so the loop can skip further processing.
        """
        ticket = trade.get("mt5_ticket")
        opened_at = trade.get("opened_at")
        if not ticket or not opened_at:
            return False

        # 1. ── Stagnation Check (Time-Stop) ──
        hours_open = (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600.0
        if hours_open >= RISK_CONFIG.auto_close_stagnant_hours:
            current_pnl = trade.get("current_pnl", 0.0)
            # If the trade is stagnant and slightly red or barely green (momentum died)
            if current_pnl <= 5.0:  # Adjust threshold if needed
                logger.warning(f"⏳ Stagnation Auto-Close: {trade_id} open for {hours_open:.1f}h (PNL: ${current_pnl:.2f})")
                self.close_by_ticket(ticket, "stagnation_stop")
                return True

        # 2. ── Reversal Guard (Cut-Loss) ──
        if RISK_CONFIG.auto_close_reversal_guard and df_m15 is not None and not df_m15.empty:
            direction = trade["direction"]
            # Look at the last closed M15 candle
            last_candle = df_m15.iloc[-2] if len(df_m15) >= 2 else df_m15.iloc[-1]
            body_size = abs(last_candle['close'] - last_candle['open'])
            total_size = last_candle['high'] - last_candle['low']
            
            # Require significant size to count as valid reversal signal (e.g. >15 pips)
            if total_size > 1.5:
                # Strong Bearish reversal against a BUY
                is_bearish_engulfing = last_candle['close'] < last_candle['open'] and (body_size / total_size) > 0.6
                if direction == "buy" and is_bearish_engulfing and current_price < trade["entry_price"]:
                    logger.warning(f"📉 Reversal Guard: Strong Bearish M15 against BUY {trade_id}. Cutting loss.")
                    self.close_by_ticket(ticket, "reversal_cut_loss")
                    return True
                    
                # Strong Bullish reversal against a SELL
                is_bullish_engulfing = last_candle['close'] > last_candle['open'] and (body_size / total_size) > 0.6
                if direction == "sell" and is_bullish_engulfing and current_price > trade["entry_price"]:
                    logger.warning(f"📈 Reversal Guard: Strong Bullish M15 against SELL {trade_id}. Cutting loss.")
                    self.close_by_ticket(ticket, "reversal_cut_loss")
                    return True

        return False

    def monitor_and_manage(self, current_price: float, df_m15: Optional[pd.DataFrame] = None) -> None:
        """
        Called on every new tick/bar.
        Applies trailing stop, breakeven, and SL/TP exit logic.
        """
        # ── Weekend Check ─────────────────────────────────────────────────────
        if self.check_weekend_shutdown():
            return
            
        # ── EOD Check ─────────────────────────────────────────────────────────
        if self.check_eod_shutdown():
            return

        for trade_id, trade in list(self.portfolio.open_trades.items()):
            direction  = trade["direction"]
            entry      = trade["entry_price"]
            sl         = trade["stop_loss"]
            lot        = trade["lot_size"]
            tp         = trade["take_profit"]
            be_active  = trade.get("be_activated", False)

            # ── Real-time floating PnL update ─────────────────────────────────
            current_pnl = self._calc_pnl(direction, entry, current_price, lot)
            trade["current_pnl"] = current_pnl

            # ── Smart Auto-Close Checks ───────────────────────────────────────
            if self.check_smart_auto_close(trade_id, trade, current_price, df_m15):
                continue

            # ── Fibo Staged Trailing SL (overrides normal trailing for fibo trades) ──
            if trade.get("fibo_levels") is not None:
                stage_result = update_fibo_trail(trade, current_price, df_h1=df_m15)
                if stage_result is not None:
                    new_sl    = stage_result.new_sl
                    close_pct = stage_result.close_pct
                    new_tp    = stage_result.new_tp

                    # ── Partial close if required ──────────────────────────────
                    if close_pct > 0:
                        orig_lot = trade.get("original_lot", lot)
                        close_vol = round(
                            (orig_lot * close_pct) / RISK_CONFIG.lot_step
                        ) * RISK_CONFIG.lot_step
                        close_vol = max(RISK_CONFIG.min_lot_size, close_vol)

                        if close_vol < lot:
                            ticket = trade.get("mt5_ticket")
                            if ticket:
                                close_ok = self.mt5.close_position(
                                    ticket, symbol=trade["symbol"], volume=close_vol
                                )
                            else:
                                close_ok = True  # paper mode

                            if close_ok or not trade.get("mt5_ticket"):
                                pnl = self._calc_pnl(direction, entry, current_price, close_vol)
                                rem_lot = round(max(lot - close_vol, 0.0), 2)
                                trade["lot_size"] = rem_lot

                                ct = ClosedTrade(
                                    trade_id   = f"{trade_id}_S{stage_result.stage}",
                                    symbol     = trade["symbol"],
                                    direction  = direction,
                                    entry_price= entry,
                                    exit_price = current_price,
                                    lot_size   = close_vol,
                                    pnl        = pnl,
                                    pnl_pips   = pnl / (close_vol * 10.0) if close_vol > 0 else 0,
                                    opened_at  = trade["opened_at"],
                                    closed_at  = datetime.now(timezone.utc),
                                    reason     = f"fibo_tp{stage_result.stage}",
                                )
                                self.portfolio.partial_close_trade(ct, rem_lot)
                                if self.repo:
                                    self.repo.save_trade_close(ct)
                                logger.info(
                                    f"Fibo partial close: {trade_id} stage={stage_result.stage} "
                                    f"closed={close_vol}L remaining={rem_lot}L"
                                )

                    # ── Update SL in MT5 ──────────────────────────────────────
                    trade["stop_loss"] = new_sl
                    ticket = trade.get("mt5_ticket")
                    if ticket:
                        res = self.mt5.modify_position(ticket, sl=new_sl, tp=new_tp)
                        if isinstance(res, dict) and res.get("error"):
                            logger.warning(
                                f"⚠️ Fibo SL modify failed for #{ticket}: "
                                f"{res.get('comment', 'Unknown')}"
                            )

                    # ── Notify Telegram ───────────────────────────────────────
                    if self.telegram:
                        self.telegram.notifier.send(stage_result.msg)

                continue  # Skip normal trailing for this tick

            # ── Breakeven check ───────────────────────────────────────────────────
            if sl is None or sl == 0:
                continue   # No SL set — skip management for this trade

            if not be_active:
                # ── Dynamic BE Activation ──
                is_extreme = trade.get("is_extreme", False)
                is_pulse   = "PULSE" in trade_id

                # Pulse: ultra-fast protection at 0.7R (tight SL, fast rebound)
                # Extreme: moderate protection at 1.2R
                # Standard: activate at 1R (default)
                if is_pulse:
                    be_threshold_r = 0.7
                elif is_extreme:
                    be_threshold_r = 1.2
                else:
                    be_threshold_r = _BE_ACTIVATION_R

                risk = abs(entry - sl)
                if risk > 0:
                    profit = (current_price - entry) if direction == "buy" else (entry - current_price)
                    if profit >= risk * be_threshold_r:
                        # Move SL to entry + small buffer to cover spread/commission
                        # Use slightly wider buffer. 0.03 is often rejected by XAUUSD stop_levels
                        buffer = 0.08  # ~8 pips
                        new_sl = entry + buffer if direction == "buy" else entry - buffer

                        trade["stop_loss"]    = new_sl
                        trade["be_activated"] = True

                        # Push SL change to MT5
                        ticket = trade.get("mt5_ticket")
                        if ticket:
                            res = self.mt5.modify_position(ticket, sl=new_sl)
                            # res can be a dict if updated mt5_client is used
                            if isinstance(res, dict) and not res.get("error"):
                                logger.info(
                                    f"⚖️ BE activated: {trade_id} MT5#{ticket} "
                                    f"SL→{new_sl:.2f} (threshold={be_threshold_r}R)"
                                )
                            elif isinstance(res, dict) and res.get("error"):
                                err = res.get("comment", "Unknown")
                                retcode = res.get("retcode", 0)
                                logger.warning(
                                    f"⚠️ BE: MT5 modify FAILED for #{ticket} | Err: {err} ({retcode}). "
                                    f"Virtual Local SL enforced at {new_sl:.2f}"
                                )
                            elif res is True:
                                # Fallback if older bool return
                                logger.info(f"⚖️ BE activated: {trade_id} MT5#{ticket} SL→{new_sl:.2f}")
                        else:
                            logger.warning(f"⚠️ BE: no mt5_ticket for {trade_id}, local virtual only")

                        if self.telegram:
                            self.telegram.notify_breakeven(trade_id, direction, entry, new_sl)

                        # ── PYRAMID LOGIC: Only for Aggressive modes ──
                        mode = TRADING_CONFIG.current_mode
                        if trade.get("is_extreme") and not trade.get("pyramided") and \
                           mode in (TradeMode.AGGRESSIVE, TradeMode.VERY_AGGRESSIVE, TradeMode.ULTRA_SCALPER):
                            trade["pyramided"] = True
                            pyramid_lot = max(0.01, round(lot / 3.0, 2))
                            res = self.mt5.place_market_order(
                                symbol=trade["symbol"],
                                direction=direction,
                                lot_size=float(pyramid_lot),
                                stop_loss=new_sl,
                                take_profit=tp,
                                comment="Pyr1/3"
                            )
                            if res and not res.get("error"):
                                logger.info(f"🔼 PYRAMID EXECUTED! Added 1/3 lot ({pyramid_lot}) at {res.get('price')} for {trade_id}")
                            else:
                                err = res.get("comment", "Unknown") if res else "None"
                                code = res.get("retcode", 0) if res else 0
                                logger.warning(f"⚠️ Pyramid MT5 execution failed for {trade_id} | Err: {err} ({code})")
                                if code == 10019 and self.telegram:
                                    self.telegram.send_message("⚠️ *Pyramid Failed*: Over Capacity (Margin tidak cukup untuk nambah layer)\\.")
                                
                        continue

            # ── Partial Close at TP1 (1:1 RR) ─────────────────────────────────
            tp_levels = trade.get("tp_levels")
            if TRADING_CONFIG.enable_partial_close and tp_levels and not trade.get("partial_closed"):
                tp1 = tp_levels.tp1
                tp1_hit = (
                    (direction == "buy" and current_price >= tp1) or
                    (direction == "sell" and current_price <= tp1)
                )
                if tp1_hit:
                    from config.risk_config import RISK_CONFIG
                    # Calculate 50% lot, rounded to broker step
                    partial_vol = round((lot / 2.0) / RISK_CONFIG.lot_step) * RISK_CONFIG.lot_step
                    partial_vol = max(RISK_CONFIG.min_lot_size, partial_vol)
                    
                    if partial_vol < lot:
                        ticket = trade.get("mt5_ticket")
                        close_ok = False
                        if ticket:
                            close_ok = self.mt5.close_position(ticket, symbol=trade["symbol"], volume=partial_vol)
                        
                        # Process if MT5 successful or if simulated
                        if close_ok or not ticket:
                            trade["partial_closed"] = True
                            pnl = self._calc_pnl(direction, entry, tp1, partial_vol)
                            
                            ct = ClosedTrade(
                                trade_id=f"{trade_id}_P",
                                symbol=trade["symbol"],
                                direction=direction,
                                entry_price=entry,
                                exit_price=tp1,
                                lot_size=partial_vol,
                                pnl=pnl,
                                pnl_pips=pnl / (partial_vol * 10.0) if partial_vol > 0 else 0,
                                opened_at=trade["opened_at"],
                                closed_at=datetime.now(timezone.utc),
                                reason="tp1",
                            )
                            
                            # Reduce lot size for remaining trade
                            rem_lot = round((lot - partial_vol) / 0.01) * 0.01
                            trade["lot_size"] = rem_lot
                            
                            self.portfolio.partial_close_trade(ct, rem_lot)
                            if self.repo:
                                self.repo.save_trade_close(ct)
                            
                            if self.telegram:
                                self.telegram.notify_trade_closed(ct)
                                
                            # Force breakeven immediately after partial take
                            if not be_active:
                                new_sl = entry + 0.01 if direction == "buy" else entry - 0.01
                                trade["stop_loss"] = new_sl
                                trade["be_activated"] = True
                                if ticket:
                                    res = self.mt5.modify_position(ticket, sl=new_sl)
                                    if isinstance(res, dict) and res.get("error"):
                                        logger.warning(f"⚠️ Partial Close BE: MT5 rejected SL {new_sl} for #{ticket}. Virtual enforced.")
                                if self.telegram:
                                    self.telegram.notify_breakeven(trade_id, direction, entry, new_sl)
                            
                            logger.info(f"✨ Partial Close executed for {trade_id} @ {tp1:.2f} (50% lot. Rem: {rem_lot})")
                            continue  # Skip trailing for this tick

            # ── Trailing stop (TP+) ───────────────────────────────────────────
            # Re-activated per user request. To avoid premature hits ("kena sebelum TP"), 
            # we trail with a wider buffer (40 pips / 4.0 points on XAUUSD) and only when deeper in profit.
            if be_active:
                new_sl = trailing_stop(
                    entry_price  = entry,
                    current_price= current_price,
                    current_sl   = sl,
                    direction    = direction,
                    trail_pips   = 40.0, # 4.0 gold points is usually safe against M5/M15 pullbacks
                )
                
                # Only physically update MT5 if the SL moved at least 0.5 points (to avoid spamming MT5 API)
                if abs(new_sl - sl) > 0.5:
                    trade["stop_loss"] = new_sl
                    # ── Push Trailing SL to MT5 ──────────────────────────────────
                    ticket = trade.get("mt5_ticket")
                    if ticket:
                        self.mt5.modify_position(ticket, sl=new_sl)
                        logger.info(f"🛡️ Trailing SL (TP+) moved: {trade_id} MT5#{ticket} SL→{new_sl:.2f}")

            # ── SL hit ────────────────────────────────────────────────────────
            if trade.get("stop_loss") is not None and trade["stop_loss"] > 0:
                sl_hit = (
                    (direction == "buy"  and current_price <= trade["stop_loss"]) or
                    (direction == "sell" and current_price >= trade["stop_loss"])
                )
                if sl_hit:
                    # ── Structural Hold (Soft SL) ─────────────────────────────
                    # REMOVED: This was causing the bot to ignore tight M1 SLs if price was still inside 
                    # a larger M15 zone, leading to massive unintended floating losses. Cutloss must be hard.
                    pnl = self._calc_pnl(direction, entry, trade["stop_loss"], lot)
                    ct  = self._close_trade(trade_id, trade, trade["stop_loss"], pnl, "sl")
                    if self.telegram:
                        self.telegram.notify_trade_closed(ct)
                    continue

            # ── TP hit ────────────────────────────────────────────────────────
            if trade.get("take_profit") is not None and trade["take_profit"] > 0:
                tp_hit = (
                    (direction == "buy"  and current_price >= tp) or
                    (direction == "sell" and current_price <= tp)
                )
                if tp_hit:
                    pnl = self._calc_pnl(direction, entry, tp, lot)
                    ct  = self._close_trade(trade_id, trade, tp, pnl, "tp2")
                    if self.telegram:
                        self.telegram.notify_trade_closed(ct)

    def close_by_ticket(self, ticket_id: int, reason: str = "manual_tg") -> bool:
        """Manual close triggered via Telegram or external signal."""
        open_trades = self.portfolio.open_trades
        target_id = None
        for tid, t in open_trades.items():
            if t.get("mt5_ticket") == ticket_id:
                target_id = tid
                break
        
        if not target_id:
            logger.warning(f"Close request failed: Ticket #{ticket_id} not found in portfolio")
            return False
            
        trade = open_trades[target_id]
        # In live mode, we actually close on MT5 first
        price = self.mt5.get_symbol_tick(trade["symbol"])["bid" if trade["direction"] == "buy" else "ask"]
        
        # Real MT5 close
        if self.mt5.close_position(ticket_id):
            pnl = self._calc_pnl(trade["direction"], trade["entry_price"], price, trade["lot_size"])
            self._close_trade(target_id, trade, price, pnl, reason)
            return True
        return False

    def _close_trade(
        self, trade_id: str, trade: dict,
        exit_price: float, pnl: float, reason: str
    ) -> ClosedTrade:
        ct = ClosedTrade(
            trade_id   = trade_id,
            symbol     = trade["symbol"],
            direction  = trade["direction"],
            entry_price= trade["entry_price"],
            exit_price = exit_price,
            lot_size   = trade["lot_size"],
            pnl        = pnl,
            pnl_pips   = pnl / (trade["lot_size"] * 10.0) if trade["lot_size"] > 0 else 0,
            opened_at  = trade["opened_at"],
            closed_at  = datetime.now(timezone.utc),
            reason     = reason,
        )
        self.portfolio.close_trade(ct)
        
        # Notify if Pulse just got suspended (fires exactly once at the 3rd loss)
        if "PULSE" in trade_id and self.telegram:
            if self.portfolio.pulse_suspended_today and self.portfolio.pulse_consecutive_losses == 3:
                self.telegram.notify_system_message(
                    "🔌 *Pulse Scalper Suspended*\n"
                    "Hit 3 consecutive StopLosses\\. "
                    "Suspended for today, auto\\-reset tomorrow\\."
                )
                
        if self.repo:
            self.repo.save_trade_close(ct)
        return ct

    @staticmethod
    def _calc_pnl(direction: str, entry: float, exit_: float, lot: float) -> float:
        pips = (exit_ - entry) / 0.1 if direction == "buy" else (entry - exit_) / 0.1
        return round(pips * lot * 10.0, 2)
