"""
TelegramBot — top-level orchestrator.

Ties together:
  - TelegramNotifier  (outbound messages)
  - CommandHandler    (inbound /commands)
  - PauseManager      (pause/resume state)
  - LossGuard         (auto-pause on consecutive losses / daily loss %)

Usage (in main.py live loop):
    from telegram.bot import TelegramBot

    tg = TelegramBot(portfolio=portfolio, db_repo=repo)
    tg.start()

    # Inside order_manager (after a trade closes):
    tg.notify_trade_opened(order, actual_entry, signal)
    tg.notify_trade_closed(closed_trade)

    # Check before sending a new order:
    if not tg.is_trading_allowed:
        continue
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
from typing import Optional, Callable

from telegram.notifier    import TelegramNotifier
from telegram.commands    import CommandHandler
from telegram.pause_manager import pause_manager, BotState
from telegram.formatter   import (
    fmt_trade_entry, fmt_trade_tp, fmt_trade_sl,
    fmt_trade_breakeven,
    fmt_bot_paused, fmt_loss_alert, fmt_daily_summary,
    fmt_risk_rejection, fmt_ai_rejection, fmt_trend_alert,
)
from core.risk.portfolio  import Portfolio, ClosedTrade

from utils.logger import get_logger

logger = get_logger(__name__)


class TelegramBot:
    def __init__(
        self,
        portfolio:            Portfolio,
        db_repo               = None,
        trades_today_ref:     Optional[Callable[[], int]] = None,
        multi_tf              = None,

        # Loss guard settings
        loss_pause_minutes:       int   = 60,
        consecutive_loss_trigger: int   = 3,
        daily_loss_pct_trigger:   float = float(os.getenv("DAILY_LOSS_PCT_TRIGGER", "3.0")),
        global_drawdown_pct_trigger: float = float(os.getenv("GLOBAL_DRAWDOWN_PCT_TRIGGER", "20.0")),
        risk_pct_default:         float = float(os.getenv("RISK_PCT_DEFAULT", "1.0")),
    ):
        self.portfolio   = portfolio
        self.multi_tf    = multi_tf
        self.notifier    = TelegramNotifier()
        self.cmd_handler = CommandHandler(
            notifier         = self.notifier,
            portfolio        = portfolio,
            db_repo          = db_repo,
            trades_today_ref = trades_today_ref or (lambda: 0),
        )
        self.cmd_handler.bot_ref = self # Allow cmd_handler to update settings

        # Per-user alert preferences (default OFF)
        # {chat_id: bool} where True = Full (with AI/Risk), False = Basic
        self.user_prefs: dict[str, bool] = {}

        # Loss guard
        self._loss_pause_minutes       = loss_pause_minutes
        self._consecutive_loss_trigger = consecutive_loss_trigger
        self._daily_loss_pct_trigger   = daily_loss_pct_trigger
        self._global_drawdown_pct_trigger = global_drawdown_pct_trigger
        self._risk_pct_default         = risk_pct_default
        self._consecutive_losses       = 0

        # Trend notification (per-user opt-in, default from .env)
        self._trend_notify_default = os.getenv("TREND_NOTIFY_ENABLED", "false").lower() == "true"
        self.trend_notify_users: dict[str, bool] = {}  # {chat_id: True/False}
        self._last_known_bias: Optional[str] = None
        
        # Stalking / Pantau Cooldowns: {setup_id: last_notified_time}
        # setup_id = direction (e.g., "buy" or "sell"). 
        # We only notify for the primary setups to avoid spam.
        self._stalking_cooldowns: dict[str, datetime] = {}

    def get_current_risk_pct(self) -> float:
        """Adaptive risk %: reduced after consecutive losses.
          * 0-1 losses: full base risk
          * 2 losses:   75% of base
          * 3+ losses:  50% of base
        """
        base = self._risk_pct_default
        if self._consecutive_losses >= 3:
            return base * 0.5
        if self._consecutive_losses >= 2:
            return base * 0.75
        return base

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start command polling thread and send startup notification."""
        self.cmd_handler.start()
        
        # Format metrics
        pf = self.portfolio
        msg = (
            f"🤖 *Trading Bot Started*\n"
            f"{chr(8212) * 15}\n"
            f"🏦 Balance:   `${pf.balance:,.2f}`\n"
            f"📊 Equity:    `${pf.equity:,.2f}`\n"
            f"📉 Drawdown:  `{pf.drawdown_pct:.2f}%`\n"
            f"🌊 Floating:  `${pf.current_floating_pnl:,.2f}`\n"
            f"📅 Today PnL: `${pf.daily_pnl:,.2f}`\n\n"
            f"Mode: LIVE\n"
            f"Send /help for commands\\."
        )
        self.notifier.send_blocking(msg)
        logger.info("TelegramBot started")

    def stop(self) -> None:
        self.cmd_handler.stop()
        try:
            self.notifier.send_blocking("⏹ *Trading Bot Stopped*")
        except:
            pass
        logger.info("TelegramBot stopped")

    # ── Trading gate ──────────────────────────────────────────────────────────

    @property
    def is_trading_allowed(self) -> bool:
        """Check before every signal → execution cycle."""
        return pause_manager.is_trading_allowed

    # ── Trade notifications ───────────────────────────────────────────────────

    def notify_trade_opened(
        self,
        trade_id:     str,
        direction:    str,
        entry_price:  float,
        stop_loss:    float,
        take_profit:  float,
        lot_size:     float,
        risk_amount:  float,
        risk_pct:     float,
        signal_score: int,
        session:      str,
        ai_confidence:float,
        ai_reason:    str,
        opened_at:    Optional[datetime] = None,
        strategy:     str = "",
        signal_id:    str = "",
        max_score:    int = 10,
        score_breakdown: Optional[dict] = None,
        mtf_summary:  str = "",
        htf_bias:     str = "",
        tp_source:    str = "",
        sl_source:    str = "",
    ) -> None:
        # Loop over all allowed users to send tailored message
        for chat_id in self.cmd_handler.allowed:
            prefers_full = self.user_prefs.get(chat_id, False)

            self.notifier.send(
                fmt_trade_entry(
                    trade_id        = trade_id,
                    direction       = direction,
                    entry_price     = entry_price,
                    stop_loss       = stop_loss,
                    take_profit     = take_profit,
                    lot_size        = lot_size,
                    risk_amount     = risk_amount,
                    risk_pct        = risk_pct,
                    signal_score    = signal_score,
                    session         = session,
                    ai_confidence   = ai_confidence if prefers_full else None,
                    ai_reason       = ai_reason if prefers_full else None,
                    opened_at       = opened_at or datetime.now(timezone.utc),
                    strategy        = strategy,
                    signal_id       = signal_id,
                    max_score       = max_score,
                    score_breakdown = score_breakdown if prefers_full else None,
                    mtf_summary     = mtf_summary,
                    htf_bias        = htf_bias,
                    tp_source       = tp_source,
                    sl_source       = sl_source,
                ),
                chat_id = chat_id
            )

    def notify_risk_rejection(
        self,
        symbol: str,
        direction: str,
        reason: str,
        signal_id: str,
    ) -> None:
        for chat_id in self.cmd_handler.allowed:
            if self.user_prefs.get(chat_id, False):
                self.notifier.send(
                    fmt_risk_rejection(symbol, direction, reason, signal_id),
                    chat_id = chat_id
                )

    def notify_ai_rejection(
        self,
        symbol: str,
        direction: str,
        confidence: float,
        reason: str,
        signal_id: str,
    ) -> None:
        for chat_id in self.cmd_handler.allowed:
            if self.user_prefs.get(chat_id, False):
                self.notifier.send(
                    fmt_ai_rejection(symbol, direction, confidence, reason, signal_id),
                    chat_id = chat_id
                )

    def notify_trade_closed(self, ct: ClosedTrade) -> None:
        """
        Route to TP or SL formatter based on close reason.
        Also runs the loss guard check.
        """
        opened_at  = ct.opened_at
        closed_at  = ct.closed_at or datetime.now(timezone.utc)
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=timezone.utc)
        duration   = int((closed_at - opened_at).total_seconds() / 60)

        if ct.reason in ("tp1", "tp2", "tp3"):
            self.notifier.send(fmt_trade_tp(
                trade_id        = ct.trade_id,
                direction       = ct.direction,
                entry_price     = ct.entry_price,
                exit_price      = ct.exit_price,
                lot_size        = ct.lot_size,
                pnl             = ct.pnl,
                pnl_pips        = ct.pnl_pips,
                tp_level        = ct.reason.upper(),
                duration_minutes= duration,
                closed_at       = closed_at,
            ))
            self._consecutive_losses = 0   # reset on win

        else:   # sl, manual, end_of_backtest
            self.notifier.send(fmt_trade_sl(
                trade_id        = ct.trade_id,
                direction       = ct.direction,
                entry_price     = ct.entry_price,
                exit_price      = ct.exit_price,
                lot_size        = ct.lot_size,
                pnl             = ct.pnl,
                pnl_pips        = ct.pnl_pips,
                duration_minutes= duration,
                closed_at       = closed_at,
            ))
            self._consecutive_losses += 1
            self._check_loss_guard()

    def notify_breakeven(
        self,
        trade_id:    str,
        direction:   str,
        entry_price: float,
        new_sl:      float,
    ) -> None:
        self.notifier.send(fmt_trade_breakeven(
            trade_id    = trade_id,
            direction   = direction,
            entry_price = entry_price,
            new_sl      = new_sl,
            closed_at   = datetime.now(timezone.utc),
        ))

    def notify_system_message(self, message: str) -> None:
        """Broadcasts a generic system alert to all allowed chats."""
        for chat_id in self.cmd_handler.allowed:
            self.notifier.send(message, chat_id=chat_id)

    def send_message(self, message: str, parse_mode: Optional[str] = None) -> None:
        """
        Alias used by order_manager / core modules.
        Defaults to plain text (empty parse_mode) so callers don't need to
        escape MarkdownV2 specials. Callers that WANT formatting should
        pass parse_mode="MarkdownV2" explicitly AND pre-escape.
        """
        pm = parse_mode if parse_mode else ""   # "" disables Telegram parsing
        for chat_id in self.cmd_handler.allowed:
            self.notifier.send(message, parse_mode=pm, chat_id=chat_id)

    def notify_stalking_setup(self, signal: TradeSignal, mode_label: str) -> None:
        """Sends a radar 'Pantau' alert with a 30-minute cooldown."""
        setup_id = f"{signal.direction}"
        now = datetime.now(timezone.utc)
        
        last_time = self._stalking_cooldowns.get(setup_id)
        if last_time and (now - last_time).total_seconds() < 1800: # 30 mins
            return # Cool cooling down...
        
        self._stalking_cooldowns[setup_id] = now
        
        from telegram.formatter import fmt_stalking_signal
        msg = fmt_stalking_signal(
            direction=signal.direction,
            score=signal.score,
            max_score=signal.max_score,
            reason=signal.stalking_reason,
            price=signal.entry_price,
            sl=signal.stop_loss,
            tp=signal.take_profit,
            mode=mode_label,
        )
        for chat_id in self.cmd_handler.allowed:
            self.notifier.send(msg, chat_id=chat_id)

    def notify_trend_change(
        self,
        bias: str,
        trend_strength: str,
        adx: float,
        ema_cross_bullish: bool,
        price: float,
        ema_50: float,
        ema_200: float,
        timeframe: str = "H1",
        tf_confluence: Optional[dict] = None,
        is_update: bool = False,
    ) -> None:
        """
        Send trend notification. 
        If is_update is False, only sends on bias/authority change.
        If is_update is True, sends as a periodic status update.
        """
        state_id = f"{timeframe}_{bias}"
        
        # Guard against spamming the same status if it's NOT a periodic update
        if not is_update and state_id == self._last_known_bias:
            return
        
        self._last_known_bias = state_id

        msg = fmt_trend_alert(
            bias=bias,
            trend_strength=trend_strength,
            adx=adx,
            ema_cross_bullish=ema_cross_bullish,
            price=price,
            ema_50=ema_50,
            ema_200=ema_200,
            timeframe=timeframe,
            tf_confluence=tf_confluence,
            is_update=is_update,
        )

        kb = {
            "inline_keyboard": [[
                {"text": "📊 Refresh", "callback_data": "trend:refresh"},
                {"text": "⚙️ Settings", "callback_data": "menu:mode"}
            ]]
        }

        for chat_id in self.cmd_handler.allowed:
            enabled = self.trend_notify_users.get(chat_id, self._trend_notify_default)
            if enabled:
                self.notifier.send(msg, chat_id=chat_id, markup=kb)

    # ── Daily summary ─────────────────────────────────────────────────────────

    def send_daily_summary(self) -> None:
        """Call once at end of trading day (e.g. 21:00 UTC / NY close)."""
        from datetime import datetime, timezone
        today  = datetime.now(timezone.utc).date()
        trades = [
            t for t in self.portfolio.closed_trades
            if t.closed_at and t.closed_at.date() == today
        ]
        if not trades:
            self.notifier.send(f"📊 *Daily Summary*\n_No trades today\\._")
            return

        pnls       = [t.pnl for t in trades]
        wins       = [p for p in pnls if p > 0]
        losses_    = [p for p in pnls if p <= 0]
        total_pnl  = sum(pnls)

        self.notifier.send(fmt_daily_summary(
            date_str        = today.strftime("%d/%m/%Y"),
            total_trades    = len(trades),
            wins            = len(wins),
            losses          = len(losses_),
            total_pnl       = total_pnl,
            best_trade_pnl  = max(pnls),
            worst_trade_pnl = min(pnls),
            ending_balance  = self.portfolio.balance,
            max_drawdown_pct= self.portfolio.drawdown_pct,
        ))

    # ── Loss guard ────────────────────────────────────────────────────────────

    def _check_loss_guard(self) -> None:
        """
        Auto-pause when:
          A) consecutive_losses >= trigger
          B) daily_loss_pct >= trigger
        """
        pf  = self.portfolio
        now = datetime.now(timezone.utc)

        triggered   = False
        description = ""

        # Check A: consecutive losses
        if self._consecutive_losses >= self._consecutive_loss_trigger:
            triggered   = True
            description = f"{self._consecutive_losses} consecutive losses"

        # Check B: daily loss %
        daily_loss_pct = abs(pf.daily_pnl_pct) if pf.daily_pnl < 0 else 0
        if daily_loss_pct >= self._daily_loss_pct_trigger:
            triggered   = True
            description = f"Daily loss {daily_loss_pct:.2f}% >= {self._daily_loss_pct_trigger}%"

        # Check C: global drawdown %
        if pf.drawdown_pct >= self._global_drawdown_pct_trigger:
            triggered   = True
            description = f"Global drawdown {pf.drawdown_pct:.2f}% >= {self._global_drawdown_pct_trigger}%"

        if triggered and pause_manager.state == BotState.RUNNING:
            resume_at = pause_manager.pause(
                reason  = description,
                minutes = self._loss_pause_minutes,
            )
            self.notifier.send(fmt_loss_alert(
                consecutive_losses = self._consecutive_losses,
                total_loss_today   = pf.daily_pnl,
                loss_pct           = abs(pf.daily_pnl_pct),
                pause_minutes      = self._loss_pause_minutes,
                resume_at          = resume_at,
            ))
            logger.warning(f"Loss guard triggered: {description} — paused {self._loss_pause_minutes}min")
