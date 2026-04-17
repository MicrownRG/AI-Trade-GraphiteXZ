"""
Order Manager.

Handles the full lifecycle of a trade:
  open -> monitor -> partial close at TP1 -> trail (SL+) -> close at TP2 / SL

Key features:
  - Breakeven (BE) activation threshold is read from the mode settings
    (be_threshold_r), so aggressive modes lock profit earlier than conservative ones.
  - Trailing stop (SL+) distance is also mode-aware (trailing_pips from settings).
  - Structural contra-exit: if market structure / news flips against an open
    position WHILE the trade is in loss, the position is cut early to avoid a
    large drawdown.  If SL+ is hit before TP (trade already at BE), the event
    is recorded so the TP can be recalculated on the next signal.
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

# Default breakeven activation R-multiple (used when mode settings are absent)
_DEFAULT_BE_R = 1.0


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
        # Set by check_structural_contra_exit; consumed by main loop for flip entry
        self._pending_contra_flip: Optional[dict] = None
        # Post-TP re-entry: when a trade hits TP, queue context for immediate re-entry
        self._pending_tp_reentry: Optional[dict] = None

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def execute(
        self,
        order: TradeOrder,
        signal_meta: dict | None = None,
        df_m15: Optional[pd.DataFrame] = None,
        df_m5:  Optional[pd.DataFrame] = None,
    ) -> Optional[str]:
        """
        Execute an approved TradeOrder via MT5.
        Returns the internal trade_id, or None on failure.
        signal_meta: optional dict with score, session, ai_confidence, ai_reason, strategy.
        df_m15, df_m5: optional OHLCV data for chart rendering after entry.
        """
        if not order.approved:
            logger.warning(f"Attempted to execute rejected order: {order.rejection_reason}")
            return None

        # PULSE trades have no structural tp_levels; use signal take_profit directly
        if order.take_profit_levels is not None:
            tp2 = order.take_profit_levels.tp2
        else:
            tp2 = order.take_profit

        result = self.mt5.place_market_order(
            symbol      = order.symbol,
            direction   = order.direction,
            lot_size    = order.lot_size,
            stop_loss   = order.stop_loss,
            take_profit = tp2,
            comment     = f"sig:{order.signal_id}",
        )

        if result is None or result.get("error"):
            err_msg = result.get("comment", "Unknown Error") if result else "Connection failed"
            retcode = result.get("retcode", 0) if result else 0
            logger.error(f"Execution failed for {order.signal_id}: {err_msg} ({retcode})")

            if self.telegram:
                if retcode == 10019:  # TRADE_RETCODE_NO_MONEY
                    self.telegram.send_message(
                        f"⚠️ *OVER CAPACITY*: Insufficient margin to open a new position\\!\n"
                        f"Reason: `{err_msg}`"
                    )
                elif retcode == 10022:  # TRADE_RETCODE_LIMIT_ORDERS
                    self.telegram.send_message(
                        f"⚠️ *OVER CAPACITY*: Broker total position limit reached\\!\n"
                        f"Reason: `{err_msg}`"
                    )
            return None

        # Use signal_id for PULSE trades so position guards can identify them
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
            "strategy":     (signal_meta or {}).get("strategy", ""),
            # Fibo staged trailing fields
            "fibo_levels":  getattr(order, "fibo_levels", None),
            "fibo_stage":   0,
            "original_lot": order.lot_size,
            # Structural contra-exit tracking
            "contra_checked": False,  # whether structure flip has been evaluated
        }
        self.portfolio.open_trade(trade_id, trade_info)

        if self.repo:
            acc_info = self.mt5.get_account_info()
            acc_id = str(acc_info["login"]) if acc_info else None
            self.repo.save_trade_open(trade_id, order, actual_entry, account_id=acc_id)

        # Telegram: entry notification
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
            f"Trade opened: {trade_id} | MT5#{mt5_ticket} | "
            f"{order.direction.upper()} {order.lot_size}L @ {actual_entry:.2f}"
        )

        # Sync basket SL for all same-direction positions
        self.sync_basket_sl(order.symbol, order.direction, order.stop_loss)

        # Chart capture -> Telegram
        if self.telegram and df_m15 is not None:
            try:
                fibo_lvls = trade_info.get("fibo_levels")
                tp_lvls   = order.take_profit_levels
                strategy  = trade_info.get("strategy", "")

                if fibo_lvls:
                    _tp1, _tp2, _tp3 = None, None, None
                elif tp_lvls:
                    _tp1 = tp_lvls.tp1
                    _tp2 = tp_lvls.tp2
                    _tp3 = tp_lvls.tp3
                else:
                    _tp1, _tp2, _tp3 = tp2, None, None

                img_bytes = self.chart_renderer.render_chart(
                    df_m15      = df_m15,
                    df_m5       = df_m5,
                    entry_price = actual_entry,
                    stop_loss   = order.stop_loss,
                    direction   = order.direction,
                    trade_id    = trade_id,
                    fibo_levels = fibo_lvls,
                    tp1 = _tp1, tp2 = _tp2, tp3 = _tp3,
                    symbol      = order.symbol,
                    strategy    = strategy,
                )
                if img_bytes:
                    # Plain-text caption avoids MarkdownV2 `.` / `|` / `(` 400 errors.
                    strat_label = f" | {strategy}" if strategy else ""
                    caption = (
                        f"📈 {order.direction.upper()}{strat_label}\n"
                        f"Entry: {actual_entry:.2f} | SL: {order.stop_loss:.2f} | TP: {tp2:.2f}"
                    )
                    self.telegram.notifier.send_photo(img_bytes, caption, parse_mode="")
                    logger.info(f"Chart sent to Telegram for {trade_id}")
            except Exception as e:
                logger.warning(f"Chart render failed (non-critical): {e}")

        return trade_id

    def sync_basket_sl(self, symbol: str, direction: str, new_sl: float) -> None:
        """
        Synchronise SL for all open positions in the same direction.
        The entire position basket shares the latest structural SL.
        """
        open_trades = self.portfolio.open_trades
        synced_count = 0

        for tid, trade in open_trades.items():
            if trade["symbol"] == symbol and trade["direction"] == direction:
                is_safer = (
                    (direction == "buy"  and new_sl > trade["stop_loss"]) or
                    (direction == "sell" and (new_sl < trade["stop_loss"] or trade["stop_loss"] == 0))
                )
                if is_safer:
                    trade["stop_loss"] = new_sl
                    if trade.get("mt5_ticket"):
                        res = self.mt5.modify_position(trade["mt5_ticket"], sl=new_sl)
                        if isinstance(res, dict) and res.get("error"):
                            logger.warning(
                                f"Basket SL sync: MT5 rejected SL {new_sl} "
                                f"for #{trade['mt5_ticket']}"
                            )
                    synced_count += 1

        if synced_count > 0:
            logger.info(f"Basket SL sync: updated {synced_count} positions to SL {new_sl:.2f}")

    def check_weekend_shutdown(self) -> bool:
        """Hard-close all positions on Friday 21:00 UTC to avoid Monday gap risk."""
        now = datetime.now(timezone.utc)
        if now.weekday() == 4 and now.hour >= 21:
            if self.portfolio.open_trade_count > 0:
                logger.warning("Weekend protection: closing all positions before market close.")
                self.close_all_positions("weekend_shutdown")
                if self.telegram:
                    self.telegram.send_message(
                        "🏁 *Weekend Protection Active*: All positions closed automatically to avoid Monday gap risk\\."
                    )
            return True
        return False

    def check_eod_shutdown(self) -> bool:
        """Close all positions at 23:45 UTC to avoid overnight swap and wide spreads."""
        if not RISK_CONFIG.auto_close_eod:
            return False

        now = datetime.now(timezone.utc)
        if now.hour == 23 and now.minute >= 45:
            if self.portfolio.open_trade_count > 0:
                logger.warning("End-of-day protection: closing all positions before rollover.")
                self.close_all_positions("daily_eod")
                if self.telegram:
                    self.telegram.send_message(
                        "🌙 *End-of-Day Protection Active*: All positions closed automatically to avoid swap/spread at rollover\\."
                    )
            return True
        return False

    def check_hard_cutloss(self) -> bool:
        """
        Trigger cutloss on EITHER:
          (a) realized daily loss >= limit, OR
          (b) live equity drawdown from peak >= limit.
        Returns True if cutloss was triggered this call (first time only).
        Respects manual reset — won't re-trigger on pre-reset losses.
        """
        if self.portfolio.daily_cutloss_triggered:
            return False

        pct   = self.portfolio.realized_daily_loss_pct
        dd    = self.portfolio.drawdown_pct

        # Equity-aware cutloss: micro-accounts ($<500) get wider threshold.
        _base = RISK_CONFIG.hard_cutloss_daily_pct
        _eq   = max(self.portfolio.balance, 100)
        limit = max(_base, min(10.0, 3000.0 / _eq))

        trigger_reason = None
        if pct >= limit:
            trigger_reason = f"realized daily loss {pct:.2f}% >= {limit:.1f}%"
        elif dd >= limit:
            trigger_reason = f"equity drawdown {dd:.2f}% from peak >= {limit:.1f}% (floating)"

        if trigger_reason:
            self.portfolio.daily_cutloss_triggered = True
            logger.warning(f"HARD CUTLOSS TRIGGERED: {trigger_reason}")
            self.close_all_positions(reason="hard_cutloss")
            return True
        return False

    def close_all_positions(self, reason: str = "manual") -> None:
        """Emergency or scheduled closure of the entire portfolio."""
        for tid in list(self.portfolio.open_trades.keys()):
            trade = self.portfolio.open_trades[tid]
            ticket = trade.get("mt5_ticket")
            if ticket:
                self.close_by_ticket(ticket, reason)

    def check_smart_auto_close(
        self, trade_id: str, trade: dict, current_price: float,
        df_m15: Optional[pd.DataFrame],
    ) -> bool:
        """
        Evaluate time-stop (stagnation) and M15 reversal cut-loss conditions.
        Returns True if the trade was closed so the caller can skip further processing.
        """
        ticket    = trade.get("mt5_ticket")
        opened_at = trade.get("opened_at")
        if not ticket or not opened_at:
            return False

        # 1. Stagnation time-stop
        hours_open = (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600.0
        if hours_open >= RISK_CONFIG.auto_close_stagnant_hours:
            current_pnl = trade.get("current_pnl", 0.0)
            if current_pnl <= 5.0:
                logger.warning(
                    f"Stagnation auto-close: {trade_id} open {hours_open:.1f}h "
                    f"(PnL: ${current_pnl:.2f})"
                )
                self.close_by_ticket(ticket, "stagnation_stop")
                return True

        # 2. M15 reversal guard (cut-loss on strong M15 candle against direction)
        if RISK_CONFIG.auto_close_reversal_guard and df_m15 is not None and not df_m15.empty:
            direction   = trade["direction"]
            last_candle = df_m15.iloc[-2] if len(df_m15) >= 2 else df_m15.iloc[-1]
            body_size   = abs(last_candle["close"] - last_candle["open"])
            total_size  = last_candle["high"] - last_candle["low"]

            if total_size > 1.5:  # require at least 15 pips of range
                is_bearish = last_candle["close"] < last_candle["open"] and (body_size / total_size) > 0.6
                is_bullish = last_candle["close"] > last_candle["open"] and (body_size / total_size) > 0.6

                if direction == "buy" and is_bearish and current_price < trade["entry_price"]:
                    logger.warning(f"Reversal guard: strong bearish M15 against BUY {trade_id}. Cutting loss.")
                    self.close_by_ticket(ticket, "reversal_cut_loss")
                    return True

                if direction == "sell" and is_bullish and current_price > trade["entry_price"]:
                    logger.warning(f"Reversal guard: strong bullish M15 against SELL {trade_id}. Cutting loss.")
                    self.close_by_ticket(ticket, "reversal_cut_loss")
                    return True

        return False

    def check_structural_contra_exit(
        self,
        trade_id: str,
        trade: dict,
        current_price: float,
        htf_bias: Optional[str] = None,  # "BULLISH" | "BEARISH" | None
        news_active: bool = False,
    ) -> bool:
        """
        Structural contra-exit logic.

        If the higher-timeframe bias or news has CLEARLY flipped against an open
        position AND the trade is currently in loss, close the position early to
        cap the drawdown.

        Logic:
          - BUY position + bias is BEARISH (or news strongly bearish) + in loss -> cut
          - SELL position + bias is BULLISH (or news strongly bullish) + in loss -> cut

        If the trade is already at breakeven or better (be_activated=True) and hits
        SL+ before reaching TP, that event is recorded for TP recalibration on the
        next signal, but the position is NOT force-closed here (trailing stop will
        handle it naturally).

        Risk constraint: only close when loss is within the trade's initial risk
        budget (< 1.5R drawdown from entry) to avoid exiting on temporary noise.
        """
        if htf_bias is None:
            return False

        direction = trade["direction"]
        entry     = trade["entry_price"]
        sl        = trade.get("stop_loss", 0.0)
        be_active = trade.get("be_activated", False)

        # Determine if bias is against the trade
        bias_against = (
            (direction == "buy"  and htf_bias == "BEARISH") or
            (direction == "sell" and htf_bias == "BULLISH")
        )
        news_against = (
            news_active and (
                (direction == "buy"  and htf_bias in ("BEARISH", None)) or
                (direction == "sell" and htf_bias in ("BULLISH", None))
            )
        )

        if not (bias_against or news_against):
            # Diagnostic: log per-trade why flip skipped (rate-limited by set)
            if htf_bias in (None, "NEUTRAL") and not trade.get("_flip_diag_neutral"):
                trade["_flip_diag_neutral"] = True
                logger.debug(
                    f"Contra-flip skip {trade_id}: htf_bias={htf_bias} "
                    f"(NEUTRAL/None — waiting for clear bias)"
                )
            return False

        current_pnl = trade.get("current_pnl", 0.0)

        # If breakeven is already active (trade was in profit at some point):
        # trailing stop handles the exit — record the event for TP learning only.
        if be_active:
            if not trade.get("contra_checked"):
                trade["contra_checked"] = True
                logger.info(
                    f"Structural contra noted for {trade_id}: "
                    f"BE already active, trailing stop will handle exit. "
                    f"TP recalibration flagged."
                )
                if self.repo:
                    try:
                        self.repo.save_trade_note(
                            trade_id,
                            note=(
                                f"Structural contra-exit skipped (BE active): "
                                f"direction={direction}, bias={htf_bias}, "
                                f"entry={entry:.2f}, current={current_price:.2f}, "
                                f"SL+={sl:.2f}. TP may need recalibration."
                            ),
                        )
                    except Exception:
                        pass
            return False

        # Not at BE — only cut if actually in loss
        if current_pnl >= 0:
            return False

        # Safety: only cut if loss is within 1.5R of the original SL distance
        # This avoids exiting on normal price noise before the structural SL is hit.
        if sl > 0 and entry > 0:
            original_risk = abs(entry - sl)
            loss_distance = abs(entry - current_price)
            if loss_distance > original_risk * 1.5:
                # Loss is already beyond the planned SL — hard SL should have triggered.
                # Don't force-close here; let MT5 handle it.
                return False

        ticket = trade.get("mt5_ticket")
        if not ticket:
            return False

        # Record for TP recalibration before closing
        if self.repo:
            try:
                self.repo.save_trade_note(
                    trade_id,
                    note=(
                        f"Structural contra-exit: direction={direction}, "
                        f"bias={htf_bias}, news={news_active}, "
                        f"entry={entry:.2f}, current={current_price:.2f}, "
                        f"loss=${current_pnl:.2f}. TP should be recalibrated."
                    ),
                )
            except Exception:
                pass

        logger.warning(
            f"Structural contra-exit: {trade_id} {direction.upper()} in loss "
            f"(${current_pnl:.2f}) while HTF bias is {htf_bias}. Cutting loss."
        )
        self.close_by_ticket(ticket, "structural_contra_exit")

        # Queue a contra-flip so the main loop can immediately open in the opposite
        # direction.  The flip bypasses the anti-revenge cooldown because the new
        # entry follows the bias — it is not revenge trading.
        flip_dir = "buy" if direction == "sell" else "sell"
        self._pending_contra_flip = {
            "direction":  flip_dir,
            "htf_bias":   htf_bias,
            "ref_price":  current_price,
            "queued_at":  datetime.now(timezone.utc),
        }
        logger.info(f"Contra-flip queued: {flip_dir.upper()} following HTF {htf_bias}")

        if self.telegram:
            self.telegram.send_message(
                f"🔄 *Structural Contra-Exit*\n"
                f"Trade `{trade_id}` \\({direction.upper()}\\) cut early\\.\n"
                f"HTF bias flipped to *{htf_bias}* while in loss \\(${current_pnl:.2f}\\)\\.\n"
                f"↩️ Contra-flip *{flip_dir.upper()}* queued for next scan\\."
            )
        return True

    def pop_contra_flip(self) -> Optional[dict]:
        """
        Return and clear the pending contra-flip dict, or None if none is queued.
        Called by the main loop after each full data scan.
        """
        flip = self._pending_contra_flip
        self._pending_contra_flip = None
        return flip

    # ─────────────────────────────────────────────────────────────────────────
    # Main monitoring loop (called on every tick)
    # ─────────────────────────────────────────────────────────────────────────

    def monitor_and_manage(
        self,
        current_price: float,
        df_m15: Optional[pd.DataFrame] = None,
        htf_bias: Optional[str] = None,
        news_active: bool = False,
        news_sentiment: Optional[str] = None,
    ) -> None:
        """
        Called on every new tick.
        Applies trailing stop (SL+), breakeven, partial close, and exit logic.

        htf_bias:       latest higher-timeframe bias ("BULLISH" | "BEARISH" | "NEUTRAL")
                        passed in from the main loop so contra-exit can evaluate it.
        news_active:    True while any 3★ event is within ±30min.
        news_sentiment: aggregated XAU sentiment from scraped news:
                        "bullish_xau" | "bearish_xau" | "neutral" | None.
                        Drives quick-harvest on adverse news and HTF-flip exit
                        for winning trades (staircase equity protection).
        """
        if self.check_weekend_shutdown():
            return
        if self.check_eod_shutdown():
            return

        # Read mode settings once per monitor cycle
        mode     = TRADING_CONFIG.current_mode
        settings = TRADING_CONFIG.mode_settings.get(mode, {})
        be_r     = settings.get("be_threshold_r", _DEFAULT_BE_R)
        trail_p  = settings.get("trailing_pips",  40.0)

        # Profit-lock fields (per-mode, from trading_config.py)
        micro_lock_r       = settings.get("micro_profit_lock_r",       0.0)
        micro_lock_buf     = settings.get("micro_lock_buffer_pips",    1.0)
        quick_harvest_news = settings.get("quick_harvest_on_adverse_news", False)
        news_tp_mult       = settings.get("news_aligned_tp_mult",      1.0)
        sl_plus_delay      = settings.get("sl_plus_delay_sec",         0)

        # Tighten trailing when news is active (volatility spike protection)
        # News-aligned sentiment keeps normal trail; adverse sentiment tightens ×0.6.
        effective_trail_p = trail_p
        if news_active:
            effective_trail_p = trail_p * 0.7

        for trade_id, trade in list(self.portfolio.open_trades.items()):
            direction = trade["direction"]
            entry     = trade["entry_price"]
            sl        = trade["stop_loss"]
            lot       = trade["lot_size"]
            tp        = trade["take_profit"]
            be_active = trade.get("be_activated", False)

            # Real-time floating PnL update
            current_pnl = self._calc_pnl(direction, entry, current_price, lot)
            trade["current_pnl"] = current_pnl

            # Smart auto-close (stagnation + reversal guard)
            if self.check_smart_auto_close(trade_id, trade, current_price, df_m15):
                continue

            # Structural contra-exit (bias / news flipped against trade while in loss)
            if self.check_structural_contra_exit(
                trade_id, trade, current_price,
                htf_bias=htf_bias, news_active=news_active,
            ):
                continue

            # ── Fibo staged trailing (overrides normal trailing for Fibo trades) ──
            if trade.get("fibo_levels") is not None:
                stage_result = update_fibo_trail(trade, current_price, df_h1=df_m15)
                if stage_result is not None:
                    new_sl    = stage_result.new_sl
                    close_pct = stage_result.close_pct
                    new_tp    = stage_result.new_tp

                    if close_pct > 0:
                        orig_lot  = trade.get("original_lot", lot)
                        close_vol = round(
                            (orig_lot * close_pct) / RISK_CONFIG.lot_step
                        ) * RISK_CONFIG.lot_step
                        close_vol = max(RISK_CONFIG.min_lot_size, close_vol)

                        if close_vol < lot:
                            ticket   = trade.get("mt5_ticket")
                            close_ok = (
                                self.mt5.close_position(ticket, symbol=trade["symbol"], volume=close_vol)
                                if ticket else True
                            )

                            if close_ok or not trade.get("mt5_ticket"):
                                pnl      = self._calc_pnl(direction, entry, current_price, close_vol)
                                rem_lot  = round(max(lot - close_vol, 0.0), 2)
                                trade["lot_size"] = rem_lot

                                ct = ClosedTrade(
                                    trade_id    = f"{trade_id}_S{stage_result.stage}",
                                    symbol      = trade["symbol"],
                                    direction   = direction,
                                    entry_price = entry,
                                    exit_price  = current_price,
                                    lot_size    = close_vol,
                                    pnl         = pnl,
                                    pnl_pips    = pnl / (close_vol * 10.0) if close_vol > 0 else 0,
                                    opened_at   = trade["opened_at"],
                                    closed_at   = datetime.now(timezone.utc),
                                    reason      = f"fibo_tp{stage_result.stage}",
                                )
                                self.portfolio.partial_close_trade(ct, rem_lot)
                                if self.repo:
                                    self.repo.save_trade_close(ct)
                                logger.info(
                                    f"Fibo partial close: {trade_id} stage={stage_result.stage} "
                                    f"closed={close_vol}L remaining={rem_lot}L"
                                )

                    trade["stop_loss"] = new_sl
                    ticket = trade.get("mt5_ticket")
                    if ticket:
                        res = self.mt5.modify_position(ticket, sl=new_sl, tp=new_tp)
                        if isinstance(res, dict) and res.get("error"):
                            logger.warning(
                                f"Fibo SL modify failed for #{ticket}: "
                                f"{res.get('comment', 'Unknown')}"
                            )

                    if self.telegram:
                        self.telegram.notifier.send(stage_result.msg)

                continue  # Skip normal trailing this tick

            # ── SL guard: skip if no SL set ───────────────────────────────────
            if sl is None or sl == 0:
                continue

            # ── Quick-harvest on adverse news sentiment ───────────────────────
            # Close any in-profit trade immediately when XAU sentiment flips
            # against it.  This guarantees "profit meski kecil" — we never give
            # back a winner to an adverse news reaction.
            if quick_harvest_news and news_sentiment and current_pnl > 0:
                adverse = (
                    (direction == "buy"  and news_sentiment == "bearish_xau") or
                    (direction == "sell" and news_sentiment == "bullish_xau")
                )
                if adverse:
                    pnl = current_pnl
                    ct  = self._close_trade(trade_id, trade, current_price, pnl, "adverse_news_harvest")
                    logger.info(
                        f"Quick-harvest adverse news: {trade_id} {direction.upper()} "
                        f"closed at ${pnl:.2f} (sentiment={news_sentiment})"
                    )
                    if self.telegram:
                        self.telegram.notify_trade_closed(ct)
                    continue

            # ── HTF-flip quick-harvest (winning trade) ────────────────────────
            # Extends structural_contra_exit: if HTF bias flipped against an
            # already-profitable trade, lock the profit now rather than wait
            # for the trail to stop it out lower.
            if htf_bias and current_pnl > 0:
                against = (
                    (direction == "buy"  and htf_bias == "BEARISH") or
                    (direction == "sell" and htf_bias == "BULLISH")
                )
                # Only fire when profit is "meaningful" — at or past the
                # micro-lock threshold (so tiny blips don't close the trade).
                risk = abs(entry - sl)
                if against and risk > 0:
                    prof = (
                        (current_price - entry) if direction == "buy"
                        else (entry - current_price)
                    )
                    if prof >= risk * max(micro_lock_r, 0.15):
                        pnl = current_pnl
                        ct  = self._close_trade(trade_id, trade, current_price, pnl, "htf_flip_harvest")
                        logger.info(
                            f"HTF-flip harvest: {trade_id} {direction.upper()} "
                            f"closed at ${pnl:.2f} (HTF now {htf_bias})"
                        )
                        if self.telegram:
                            self.telegram.notify_trade_closed(ct)
                        continue

            # ── Momentum-fade quick exit (100% WR guard) ────────────────────
            # If trade reached a peak profit >= 0.5R but has now faded back to
            # <= 0.15R, close immediately at whatever tiny profit remains.
            # This prevents "winner turned loser". AGG+ modes only.
            momentum_fade_exit = settings.get("momentum_fade_exit", False)
            if momentum_fade_exit and not be_active:
                risk = abs(entry - sl)
                if risk > 0:
                    profit = (
                        (current_price - entry) if direction == "buy"
                        else (entry - current_price)
                    )
                    profit_r = profit / risk
                    # Track peak R per trade
                    peak_r = trade.get("_peak_profit_r", 0.0)
                    if profit_r > peak_r:
                        trade["_peak_profit_r"] = profit_r
                        peak_r = profit_r

                    # If we hit >= 0.5R but faded to <= 0.15R → exit to preserve tiny profit
                    if peak_r >= 0.5 and profit_r <= 0.15 and profit > 0:
                        ticket = trade.get("mt5_ticket")
                        if ticket:
                            self.close_by_ticket(ticket, reason="momentum_fade")
                        ct = ClosedTrade(
                            trade_id    = trade_id,
                            symbol      = trade["symbol"],
                            direction   = direction,
                            entry_price = entry,
                            exit_price  = current_price,
                            lot_size    = lot,
                            pnl         = self._calc_pnl(direction, entry, current_price, lot),
                            pnl_pips    = profit / 0.1 if profit else 0,
                            opened_at   = trade["opened_at"],
                            closed_at   = datetime.now(timezone.utc),
                            reason      = "momentum_fade",
                        )
                        self.portfolio.close_trade(ct)
                        if self.repo:
                            self.repo.save_trade_close(ct)
                        logger.info(
                            f"⚡ MOMENTUM FADE EXIT: {trade_id} peak={peak_r:.2f}R "
                            f"faded to {profit_r:.2f}R → closed at +{profit:.2f} pts"
                        )
                        if self.telegram:
                            self.telegram.notify_trade_closed(ct)
                        continue

            # ── Micro-profit-lock ratchet ─────────────────────────────────────
            # Before the heavier BE logic, guarantee that any trade which
            # reaches micro_profit_lock_r never returns to a losing SL.
            # SL → entry + tiny buffer (0.5–2.0 pips depending on mode).
            # This creates the staircase equity curve the strategy targets.
            if micro_lock_r > 0 and not trade.get("micro_locked") and not be_active:
                _age_sec = (datetime.now(timezone.utc) - trade["opened_at"]).total_seconds()
                if sl_plus_delay > 0 and _age_sec < sl_plus_delay:
                    pass  # too early — skip micro-lock this cycle
                elif (risk := abs(entry - sl)) > 0:
                    profit = (
                        (current_price - entry) if direction == "buy"
                        else (entry - current_price)
                    )
                    if profit >= risk * micro_lock_r:
                        # pip = 0.1 for XAU/USD. Min 0.40 pts to cover spread.
                        buf_price = max(0.40, micro_lock_buf * 0.1)
                        new_sl = (entry + buf_price) if direction == "buy" else (entry - buf_price)

                        # Only move SL if it actually improves (toward entry + buffer)
                        improves = (
                            (direction == "buy"  and new_sl > sl) or
                            (direction == "sell" and new_sl < sl)
                        )
                        if improves:
                            trade["stop_loss"]    = new_sl
                            trade["micro_locked"] = True
                            sl = new_sl  # update local for subsequent blocks

                            ticket = trade.get("mt5_ticket")
                            if ticket:
                                res = self.mt5.modify_position(ticket, sl=new_sl)
                                if isinstance(res, dict) and res.get("error"):
                                    logger.warning(
                                        f"Micro-lock SL modify failed for #{ticket}: "
                                        f"{res.get('comment')} — virtual SL enforced"
                                    )
                            logger.info(
                                f"Micro-profit-lock: {trade_id} SL -> {new_sl:.2f} "
                                f"(profit {profit:.2f} >= {micro_lock_r:.2f}R)"
                            )

            # ── News-aligned TP boost (one-shot per trade) ────────────────────
            # When sentiment + HTF both confirm the signal direction, extend the
            # TP so winners run further on momentum.
            if (
                news_tp_mult > 1.0 and
                not trade.get("tp_boosted") and
                news_sentiment and htf_bias and
                trade.get("take_profit")
            ):
                aligned = (
                    (direction == "buy"  and news_sentiment == "bullish_xau" and htf_bias == "BULLISH") or
                    (direction == "sell" and news_sentiment == "bearish_xau" and htf_bias == "BEARISH")
                )
                if aligned:
                    old_tp = trade["take_profit"]
                    risk   = abs(entry - sl) if sl else 0
                    if risk > 0:
                        # extend TP by (mult-1) * risk in the trade direction
                        extra = risk * (news_tp_mult - 1.0)
                        new_tp = (old_tp + extra) if direction == "buy" else (old_tp - extra)
                        trade["take_profit"] = new_tp
                        trade["tp_boosted"]  = True
                        ticket = trade.get("mt5_ticket")
                        if ticket:
                            self.mt5.modify_position(ticket, tp=new_tp)
                        logger.info(
                            f"News-aligned TP boost: {trade_id} TP {old_tp:.2f} -> {new_tp:.2f} "
                            f"(×{news_tp_mult:.2f}, sentiment={news_sentiment} HTF={htf_bias})"
                        )

            # ── Breakeven activation ──────────────────────────────────────────
            if not be_active:
                _age_sec_be = (datetime.now(timezone.utc) - trade["opened_at"]).total_seconds()
                _delay_ok = sl_plus_delay <= 0 or _age_sec_be >= sl_plus_delay

                is_pulse   = "PULSE" in trade_id
                is_extreme = trade.get("is_extreme", False)

                if is_pulse:
                    be_threshold = min(be_r, 0.7)
                elif is_extreme:
                    be_threshold = min(be_r, 1.2)
                else:
                    be_threshold = be_r

                risk = abs(entry - sl)
                if risk > 0 and _delay_ok:
                    profit = (
                        (current_price - entry) if direction == "buy"
                        else (entry - current_price)
                    )
                    if profit >= risk * be_threshold:
                        # Buffer must cover spread + commission on XAU (~3-5 pips).
                        # micro_lock_buf is in PIPS (×0.1 → pts). Minimum 0.50 pts.
                        _be_buf_pts = max(0.50, micro_lock_buf * 0.1 * 1.5)
                        new_sl = entry + _be_buf_pts if direction == "buy" else entry - _be_buf_pts

                        trade["stop_loss"]    = new_sl
                        trade["be_activated"] = True

                        ticket = trade.get("mt5_ticket")
                        if ticket:
                            res = self.mt5.modify_position(ticket, sl=new_sl)
                            if isinstance(res, dict) and not res.get("error"):
                                logger.info(
                                    f"BE activated: {trade_id} MT5#{ticket} "
                                    f"SL -> {new_sl:.2f} (threshold {be_threshold:.1f}R)"
                                )
                            elif isinstance(res, dict) and res.get("error"):
                                logger.warning(
                                    f"BE modify failed for #{ticket}: "
                                    f"{res.get('comment')} ({res.get('retcode', 0)}). "
                                    f"Virtual local SL enforced at {new_sl:.2f}"
                                )
                            elif res is True:
                                logger.info(f"BE activated: {trade_id} MT5#{ticket} SL -> {new_sl:.2f}")
                        else:
                            logger.warning(f"BE: no mt5_ticket for {trade_id}, local virtual only")

                        if self.telegram:
                            self.telegram.notify_breakeven(trade_id, direction, entry, new_sl)

                        # Pyramid entry: only for aggressive modes on extreme signals
                        if (
                            trade.get("is_extreme") and
                            not trade.get("pyramided") and
                            mode in (TradeMode.AGGRESSIVE, TradeMode.VERY_AGGRESSIVE, TradeMode.ULTRA_SCALPER)
                        ):
                            trade["pyramided"] = True
                            pyramid_lot = max(0.01, round(lot / 3.0, 2))
                            res = self.mt5.place_market_order(
                                symbol      = trade["symbol"],
                                direction   = direction,
                                lot_size    = float(pyramid_lot),
                                stop_loss   = new_sl,
                                take_profit = tp,
                                comment     = "Pyr1/3",
                            )
                            if res and not res.get("error"):
                                logger.info(
                                    f"Pyramid executed: added 1/3 lot ({pyramid_lot}) "
                                    f"at {res.get('price')} for {trade_id}"
                                )
                            else:
                                err  = res.get("comment", "Unknown") if res else "None"
                                code = res.get("retcode", 0) if res else 0
                                logger.warning(
                                    f"Pyramid MT5 execution failed for {trade_id}: "
                                    f"{err} ({code})"
                                )
                                if code == 10019 and self.telegram:
                                    self.telegram.send_message(
                                        "⚠️ *Pyramid Failed*: Insufficient margin to add pyramid layer\\."
                                    )
                        continue

            # ── Partial close at TP1 (1:1 RR) ────────────────────────────────
            tp_levels = trade.get("tp_levels")
            if TRADING_CONFIG.enable_partial_close and tp_levels and not trade.get("partial_closed"):
                tp1 = tp_levels.tp1
                tp1_hit = (
                    (direction == "buy"  and current_price >= tp1) or
                    (direction == "sell" and current_price <= tp1)
                )
                if tp1_hit:
                    # Profile-aware partial fraction (30% CONS → 70% ULTRA)
                    pc_frac = settings.get("partial_close_fraction", 0.5)
                    pc_frac = max(0.10, min(0.90, pc_frac))  # safety clamp
                    partial_vol = round((lot * pc_frac) / RISK_CONFIG.lot_step) * RISK_CONFIG.lot_step
                    partial_vol = max(RISK_CONFIG.min_lot_size, partial_vol)

                    if partial_vol < lot:
                        ticket   = trade.get("mt5_ticket")
                        close_ok = False
                        if ticket:
                            close_ok = self.mt5.close_position(
                                ticket, symbol=trade["symbol"], volume=partial_vol
                            )

                        if close_ok or not ticket:
                            trade["partial_closed"] = True
                            pnl = self._calc_pnl(direction, entry, tp1, partial_vol)

                            ct = ClosedTrade(
                                trade_id    = f"{trade_id}_P",
                                symbol      = trade["symbol"],
                                direction   = direction,
                                entry_price = entry,
                                exit_price  = tp1,
                                lot_size    = partial_vol,
                                pnl         = pnl,
                                pnl_pips    = pnl / (partial_vol * 10.0) if partial_vol > 0 else 0,
                                opened_at   = trade["opened_at"],
                                closed_at   = datetime.now(timezone.utc),
                                reason      = "tp1",
                            )

                            rem_lot = round((lot - partial_vol) / 0.01) * 0.01
                            trade["lot_size"] = rem_lot

                            self.portfolio.partial_close_trade(ct, rem_lot)
                            if self.repo:
                                self.repo.save_trade_close(ct)
                            if self.telegram:
                                self.telegram.notify_trade_closed(ct)

                            # Force breakeven immediately after partial take
                            if not be_active:
                                _pc_buf = max(0.40, micro_lock_buf * 0.1)
                                new_sl = entry + _pc_buf if direction == "buy" else entry - _pc_buf
                                trade["stop_loss"]    = new_sl
                                trade["be_activated"] = True
                                if ticket:
                                    res = self.mt5.modify_position(ticket, sl=new_sl)
                                    if isinstance(res, dict) and res.get("error"):
                                        logger.warning(
                                            f"Partial-close BE: MT5 rejected SL {new_sl} "
                                            f"for #{ticket}. Virtual enforced."
                                        )
                                if self.telegram:
                                    self.telegram.notify_breakeven(trade_id, direction, entry, new_sl)

                            logger.info(
                                f"Partial close: {trade_id} @ {tp1:.2f} "
                                f"(50% lot closed, remaining: {rem_lot}L)"
                            )
                            continue

            # ── Trailing stop (SL+) — active once BE is set ───────────────────
            # trail_pips comes from mode settings:
            #   Conservative: 40 pips | Moderate: 35 | Aggressive: 25
            #   Very Aggressive: 20 | Ultra Scalper: 15
            # This ensures aggressive modes lock profits faster on M1 moves.
            if be_active:
                new_sl = trailing_stop(
                    entry_price   = entry,
                    current_price = current_price,
                    current_sl    = sl,
                    direction     = direction,
                    trail_pips    = effective_trail_p,
                )

                # Only push to MT5 if SL moved at least 0.5 points (avoid API spam)
                if abs(new_sl - sl) > 0.5:
                    trade["stop_loss"] = new_sl
                    ticket = trade.get("mt5_ticket")
                    if ticket:
                        self.mt5.modify_position(ticket, sl=new_sl)
                        logger.info(
                            f"Trailing SL (SL+) moved: {trade_id} MT5#{ticket} "
                            f"SL -> {new_sl:.2f} (trail {effective_trail_p:.0f} pips)"
                        )

            # ── SL hit ────────────────────────────────────────────────────────
            if trade.get("stop_loss") is not None and trade["stop_loss"] > 0:
                sl_hit = (
                    (direction == "buy"  and current_price <= trade["stop_loss"]) or
                    (direction == "sell" and current_price >= trade["stop_loss"])
                )
                if sl_hit:
                    # Determine if this is an SL+ hit (already at breakeven)
                    reason = "sl_plus" if be_active else "sl"
                    if reason == "sl_plus":
                        logger.info(
                            f"SL+ triggered for {trade_id}: trade was at BE, "
                            f"trailing stop hit at {trade['stop_loss']:.2f} before TP {tp:.2f}. "
                            f"TP recalibration recommended."
                        )
                        if self.repo:
                            try:
                                self.repo.save_trade_note(
                                    trade_id,
                                    note=(
                                        f"SL+ hit before TP: direction={direction}, "
                                        f"entry={entry:.2f}, sl_plus={trade['stop_loss']:.2f}, "
                                        f"tp={tp:.2f}. Consider widening TP or reducing "
                                        f"trail_pips for next signal."
                                    ),
                                )
                            except Exception:
                                pass

                    pnl = self._calc_pnl(direction, entry, trade["stop_loss"], lot)
                    ct  = self._close_trade(trade_id, trade, trade["stop_loss"], pnl, reason)
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

                    # Queue post-TP re-entry for the main loop
                    self._pending_tp_reentry = {
                        "closed_direction": direction,
                        "tp_price":         tp,
                        "entry_price":      entry,
                        "symbol":           trade["symbol"],
                        "closed_at":        datetime.now(timezone.utc),
                        "lot_size":         lot,
                        "sl":               trade.get("stop_loss", 0),
                    }
                    logger.info(
                        f"Post-TP re-entry queued: closed {direction} @ TP {tp:.2f}, "
                        f"watching for continuation or flip"
                    )

    # ─────────────────────────────────────────────────────────────────────────
    # Post-TP re-entry
    # ─────────────────────────────────────────────────────────────────────────

    def consume_tp_reentry(self, current_price: float, htf_bias: str = "") -> Optional[dict]:
        """
        Check if a post-TP re-entry is queued and determine direction.
        Returns dict with direction/entry/sl/tp for the main loop to execute,
        or None if no re-entry is warranted.

        Logic:
          - If price continues past TP (momentum) → continuation entry (same dir)
          - If price reverses from TP → flip entry (opposite dir)
          - Must happen within 120s of TP hit
          - HTF bias used as tiebreaker
        """
        pending = self._pending_tp_reentry
        if pending is None:
            return None

        age = (datetime.now(timezone.utc) - pending["closed_at"]).total_seconds()
        if age > 120:
            self._pending_tp_reentry = None
            return None

        closed_dir = pending["closed_direction"]
        tp_price   = pending["tp_price"]
        symbol     = pending["symbol"]
        old_risk   = abs(pending["entry_price"] - pending["sl"])
        if old_risk < 0.5:
            old_risk = 1.0

        reentry_dir = None

        if closed_dir == "sell":
            # Closed a SELL at TP (price went down). Now:
            # - If price drops further below TP → continuation SELL
            # - If price bounces up above TP → flip BUY
            if current_price < tp_price - old_risk * 0.3:
                reentry_dir = "sell"
            elif current_price > tp_price + old_risk * 0.3:
                reentry_dir = "buy"
        else:
            # Closed a BUY at TP (price went up). Now:
            # - If price rises further above TP → continuation BUY
            # - If price drops below TP → flip SELL
            if current_price > tp_price + old_risk * 0.3:
                reentry_dir = "buy"
            elif current_price < tp_price - old_risk * 0.3:
                reentry_dir = "sell"

        if reentry_dir is None:
            return None

        # HTF bias veto: don't enter against strong HTF trend
        if htf_bias:
            bias_up = htf_bias.upper()
            if reentry_dir == "buy" and "BEAR" in bias_up:
                logger.debug(f"Post-TP re-entry BUY vetoed by HTF bias {htf_bias}")
                self._pending_tp_reentry = None
                return None
            if reentry_dir == "sell" and "BULL" in bias_up:
                logger.debug(f"Post-TP re-entry SELL vetoed by HTF bias {htf_bias}")
                self._pending_tp_reentry = None
                return None

        # Build re-entry params
        atr_est = old_risk * 0.8
        if reentry_dir == "buy":
            sl = current_price - atr_est
            tp = current_price + atr_est * 2.0
        else:
            sl = current_price + atr_est
            tp = current_price - atr_est * 2.0

        self._pending_tp_reentry = None

        logger.info(
            f"⚡ Post-TP re-entry: {reentry_dir.upper()} @ {current_price:.2f} "
            f"(prev {closed_dir} TP={tp_price:.2f}) SL={sl:.2f} TP={tp:.2f}"
        )

        return {
            "direction":   reentry_dir,
            "entry_price": current_price,
            "stop_loss":   sl,
            "take_profit": tp,
            "symbol":      symbol,
            "reason":      f"PostTP_{closed_dir}→{reentry_dir}",
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Close helpers
    # ─────────────────────────────────────────────────────────────────────────

    def close_by_ticket(self, ticket_id: int, reason: str = "manual_tg") -> bool:
        """Manual close triggered via Telegram or external signal."""
        open_trades = self.portfolio.open_trades
        target_id   = None

        for tid, t in open_trades.items():
            if t.get("mt5_ticket") == ticket_id:
                target_id = tid
                break

        if not target_id:
            logger.warning(f"Close request failed: ticket #{ticket_id} not found in portfolio")
            return False

        trade = open_trades[target_id]
        tick  = self.mt5.get_symbol_tick(trade["symbol"])
        price = tick["bid"] if trade["direction"] == "buy" else tick["ask"]

        if self.mt5.close_position(ticket_id):
            pnl = self._calc_pnl(trade["direction"], trade["entry_price"], price, trade["lot_size"])
            self._close_trade(target_id, trade, price, pnl, reason)
            return True
        return False

    def _close_trade(
        self, trade_id: str, trade: dict,
        exit_price: float, pnl: float, reason: str,
    ) -> ClosedTrade:
        ct = ClosedTrade(
            trade_id    = trade_id,
            symbol      = trade["symbol"],
            direction   = trade["direction"],
            entry_price = trade["entry_price"],
            exit_price  = exit_price,
            lot_size    = trade["lot_size"],
            pnl         = pnl,
            pnl_pips    = pnl / (trade["lot_size"] * 10.0) if trade["lot_size"] > 0 else 0,
            opened_at   = trade["opened_at"],
            closed_at   = datetime.now(timezone.utc),
            reason      = reason,
        )
        self.portfolio.close_trade(ct)
        if self.repo:
            self.repo.save_trade_close(ct)
        return ct

    @staticmethod
    def _calc_pnl(direction: str, entry: float, exit_: float, lot: float) -> float:
        pips = (exit_ - entry) / 0.1 if direction == "buy" else (entry - exit_) / 0.1
        return round(pips * lot * 10.0, 2)
