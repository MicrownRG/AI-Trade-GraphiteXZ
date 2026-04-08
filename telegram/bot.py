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
    fmt_trade_breakeven, fmt_kill_switch_triggered,
    fmt_bot_paused, fmt_loss_alert, fmt_daily_summary,
)
from core.risk.portfolio  import Portfolio, ClosedTrade
from core.risk.kill_switch import kill_switch
from utils.logger import get_logger

logger = get_logger(__name__)


class TelegramBot:
    def __init__(
        self,
        portfolio:            Portfolio,
        db_repo               = None,
        trades_today_ref:     Optional[Callable[[], int]] = None,

        # Loss guard settings
        loss_pause_minutes:       int   = 60,
        consecutive_loss_trigger: int   = 3,
        daily_loss_pct_trigger:   float = 2.0,
    ):
        self.portfolio   = portfolio
        self.notifier    = TelegramNotifier()
        self.cmd_handler = CommandHandler(
            notifier         = self.notifier,
            portfolio        = portfolio,
            db_repo          = db_repo,
            trades_today_ref = trades_today_ref or (lambda: 0),
        )

        # Loss guard
        self._loss_pause_minutes       = loss_pause_minutes
        self._consecutive_loss_trigger = consecutive_loss_trigger
        self._daily_loss_pct_trigger   = daily_loss_pct_trigger
        self._consecutive_losses       = 0

        # Wire kill switch → auto-notify + halt
        kill_switch_original = kill_switch.trigger
        def _patched_trigger(reason: str):
            kill_switch_original(reason)
            pause_manager.halt(reason)
            self.notifier.send(fmt_kill_switch_triggered(
                reason=reason,
                triggered_at=datetime.now(timezone.utc),
            ))
        kill_switch.trigger = _patched_trigger  # type: ignore

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start command polling thread and send startup notification."""
        self.cmd_handler.start()
        self.notifier.send_blocking(
            f"🤖 *Trading Bot Started*\n"
            f"Balance: `${self.portfolio.balance:,.2f}`\n"
            f"Mode: LIVE\n"
            f"Send /help for commands\\."
        )
        logger.info("TelegramBot started")

    def stop(self) -> None:
        self.cmd_handler.stop()
        self.notifier.send_blocking("⏹ *Trading Bot Stopped*")
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
    ) -> None:
        self.notifier.send(fmt_trade_entry(
            trade_id     = trade_id,
            direction    = direction,
            entry_price  = entry_price,
            stop_loss    = stop_loss,
            take_profit  = take_profit,
            lot_size     = lot_size,
            risk_amount  = risk_amount,
            risk_pct     = risk_pct,
            signal_score = signal_score,
            session      = session,
            ai_confidence= ai_confidence,
            ai_reason    = ai_reason,
            opened_at    = opened_at or datetime.now(timezone.utc),
        ))

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

        else:   # sl, manual, kill_switch, end_of_backtest
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

    # ── Daily summary ─────────────────────────────────────────────────────────

    def send_daily_summary(self) -> None:
        """Call once at end of trading day (e.g. 21:00 UTC / NY close)."""
        from datetime import date
        today  = date.today()
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
