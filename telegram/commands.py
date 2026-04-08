"""
Telegram Command Handlers.

Processes incoming Telegram messages and routes commands.
Runs in a background polling thread — does NOT block the trading loop.

Supported commands:
  /today              — trades & P&L today
  /trades [DD/MM/YYYY]— trades on a specific date
  /status             — bot state, balance, equity, drawdown
  /balance            — quick balance check
  /pnl                — P&L summary this week
  /pause [minutes]    — pause trading (default 60 min)
  /resume             — resume immediately
  /kill               — activate kill switch (admin)
  /reset              — reset kill switch (admin)
  /help               — command list

Security: only whitelisted TELEGRAM_CHAT_IDs are accepted.
"""
from __future__ import annotations
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, List

from telegram.notifier import TelegramNotifier
from telegram.formatter import (
    fmt_status, fmt_trades_list, fmt_daily_summary,
    fmt_bot_paused, fmt_bot_resumed, fmt_kill_switch_triggered,
    fmt_balance_quick, fmt_help, fmt_unknown_command, fmt_unauthorized,
    _wib,
)
from telegram.pause_manager import pause_manager, BotState
from core.risk.kill_switch import kill_switch
from core.risk.portfolio import Portfolio
from ai.providers.registry import get_active_provider_name
from utils.logger import get_logger

logger = get_logger(__name__)

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

_TELEGRAM_UPDATES = "https://api.telegram.org/bot{token}/getUpdates"
_TELEGRAM_SEND    = "https://api.telegram.org/bot{token}/sendMessage"


class CommandHandler:
    """
    Long-polling Telegram command processor.
    Runs in a daemon thread — call start() to begin.
    """

    def __init__(
        self,
        notifier:    TelegramNotifier,
        portfolio:   Portfolio,
        db_repo,                            # TradeRepository
        trades_today_ref: Callable[[], int],
        allowed_chat_ids: Optional[List[str]] = None,
    ):
        self.notifier          = notifier
        self.portfolio         = portfolio
        self.repo              = db_repo
        self.get_trades_today  = trades_today_ref
        self.token             = notifier.token

        # Whitelist: if empty, only the primary TELEGRAM_CHAT_ID is accepted
        primary = os.getenv("TELEGRAM_CHAT_ID", "")
        extra   = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
        self.allowed = set(
            filter(None, [primary] + (allowed_chat_ids or []) + extra)
        )

        self._offset    = 0
        self._running   = False
        self._thread: Optional[threading.Thread] = None

        # Wire auto-resume callback to send notification
        pause_manager.on_resume_callback = self._on_auto_resume

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not _HTTPX_AVAILABLE:
            logger.warning("httpx not installed — Telegram commands unavailable")
            return
        if not self.token:
            logger.warning("TELEGRAM_BOT_TOKEN not set — commands disabled")
            return

        self._running = True
        self._thread  = threading.Thread(
            target=self._poll_loop, daemon=True, name="telegram_cmd"
        )
        self._thread.start()
        logger.info("Telegram command handler started (long polling)")

    def stop(self) -> None:
        self._running = False
        logger.info("Telegram command handler stopped")

    # ── Polling loop ──────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while self._running:
            try:
                updates = self._get_updates()
                for update in updates:
                    self._offset = update["update_id"] + 1
                    if "message" in update:
                        self._dispatch(update["message"])
            except Exception as e:
                logger.error(f"Telegram poll error: {e}")
                time.sleep(5)
            time.sleep(1)

    def _get_updates(self) -> list:
        url    = _TELEGRAM_UPDATES.format(token=self.token)
        params = {"offset": self._offset, "timeout": 30, "allowed_updates": ["message"]}
        try:
            with httpx.Client(timeout=35.0) as c:
                r = c.get(url, params=params)
                if r.status_code == 200:
                    return r.json().get("result", [])
        except Exception:
            pass
        return []

    # ── Dispatcher ────────────────────────────────────────────────────────────

    def _dispatch(self, message: dict) -> None:
        chat_id = str(message.get("chat", {}).get("id", ""))
        text    = message.get("text", "").strip()

        if not text.startswith("/"):
            return

        if chat_id not in self.allowed:
            self._reply(chat_id, fmt_unauthorized())
            logger.warning(f"Unauthorized Telegram access from chat_id={chat_id}")
            return

        parts = text.split()
        cmd   = parts[0].lower().split("@")[0]   # strip @botname if present
        args  = parts[1:]

        logger.info(f"Telegram cmd: {cmd} {args} from {chat_id[:6]}...")

        handlers = {
            "/today":   self._cmd_today,
            "/trades":  self._cmd_trades,
            "/status":  self._cmd_status,
            "/balance": self._cmd_balance,
            "/pnl":     self._cmd_pnl,
            "/pause":   self._cmd_pause,
            "/resume":  self._cmd_resume,
            "/kill":    self._cmd_kill,
            "/reset":   self._cmd_reset,
            "/help":    self._cmd_help,
            "/start":   self._cmd_help,
        }

        fn = handlers.get(cmd)
        if fn:
            try:
                fn(chat_id, args)
            except Exception as e:
                logger.error(f"Command handler error [{cmd}]: {e}")
                self._reply(chat_id, f"⚠️ Error processing `{cmd}`\\: {str(e)[:80]}")
        else:
            self._reply(chat_id, fmt_unknown_command(cmd))

    # ── Command implementations ───────────────────────────────────────────────

    def _cmd_today(self, chat_id: str, args: list) -> None:
        today    = datetime.now(timezone.utc).date()
        trades   = self._get_trades_for_date(today)
        date_str = today.strftime("%d/%m/%Y")
        self._reply(chat_id, fmt_trades_list(trades, date_str))

    def _cmd_trades(self, chat_id: str, args: list) -> None:
        if args:
            try:
                date = datetime.strptime(args[0], "%d/%m/%Y").date()
            except ValueError:
                self._reply(chat_id, "⚠️ Format tanggal salah\\. Gunakan: `/trades DD/MM/YYYY`")
                return
        else:
            date = datetime.now(timezone.utc).date()

        trades   = self._get_trades_for_date(date)
        date_str = date.strftime("%d/%m/%Y")
        self._reply(chat_id, fmt_trades_list(trades, date_str))

    def _cmd_status(self, chat_id: str, args: list) -> None:
        pf  = self.portfolio
        ks  = kill_switch
        pm  = pause_manager

        msg = fmt_status(
            is_running         = pm.state == BotState.RUNNING,
            is_paused          = pm.state == BotState.PAUSED,
            pause_reason       = pm.reason,
            resume_at          = pm.resume_at,
            kill_switch_active = ks.is_active,
            kill_switch_reason = ks.reason or "",
            balance            = pf.balance,
            equity             = pf.equity,
            drawdown_pct       = pf.drawdown_pct,
            daily_pnl          = pf.daily_pnl,
            open_trades        = pf.open_trade_count,
            trades_today       = self.get_trades_today(),
            active_provider    = get_active_provider_name(),
            checked_at         = datetime.now(timezone.utc),
        )
        self._reply(chat_id, msg)

    def _cmd_balance(self, chat_id: str, args: list) -> None:
        pf = self.portfolio
        self._reply(chat_id, fmt_balance_quick(pf.balance, pf.equity, pf.daily_pnl))

    def _cmd_pnl(self, chat_id: str, args: list) -> None:
        # Weekly P&L from closed trades
        now      = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)
        trades   = [
            t for t in self.portfolio.closed_trades
            if t.closed_at and t.closed_at.replace(tzinfo=timezone.utc if t.closed_at.tzinfo is None else None) >= week_ago
        ]

        if not trades:
            self._reply(chat_id, "📭 *No closed trades in the last 7 days*")
            return

        total = sum(t.pnl for t in trades)
        wins  = sum(1 for t in trades if t.pnl > 0)
        losses= len(trades) - wins
        sign  = "\\+" if total >= 0 else "\\-"

        msg = (
            f"📊 *P&L — Last 7 Days*\n"
            f"{'─'*30}\n"
            f"Trades: `{len(trades)}`  \\({wins}W / {losses}L\\)\n"
            f"Total:  *{sign}${abs(total):.2f}*"
        )
        self._reply(chat_id, msg)

    def _cmd_pause(self, chat_id: str, args: list) -> None:
        minutes = 60
        if args:
            try:
                minutes = int(args[0])
                if minutes <= 0 or minutes > 1440:
                    raise ValueError
            except ValueError:
                self._reply(chat_id, "⚠️ Masukkan durasi valid \\(1–1440 menit\\)\\. Contoh: `/pause 120`")
                return

        resume_at = pause_manager.pause(
            reason  = f"Manual pause via Telegram",
            minutes = minutes,
        )
        self._reply(chat_id, fmt_bot_paused(
            reason="Manual pause via Telegram",
            pause_minutes=minutes,
            resume_at=resume_at,
        ))

    def _cmd_resume(self, chat_id: str, args: list) -> None:
        success = pause_manager.resume(resumed_by=f"telegram:{chat_id[:6]}")
        if success:
            self._reply(chat_id, fmt_bot_resumed(
                resumed_by=f"Telegram ({chat_id[:6]})",
                resumed_at=datetime.now(timezone.utc),
            ))
        else:
            self._reply(
                chat_id,
                "🔴 *Cannot resume* — Kill Switch is active\\.\n"
                "Use /reset to deactivate kill switch first\\."
            )

    def _cmd_kill(self, chat_id: str, args: list) -> None:
        reason = " ".join(args) if args else "Manual kill via Telegram"
        kill_switch.trigger(reason)
        pause_manager.halt(reason)
        self._reply(chat_id, fmt_kill_switch_triggered(
            reason=reason,
            triggered_at=datetime.now(timezone.utc),
        ))

    def _cmd_reset(self, chat_id: str, args: list) -> None:
        kill_switch.reset(authorized_by=f"telegram:{chat_id[:6]}")
        pause_manager.reset_halt(authorized_by=f"telegram:{chat_id[:6]}")
        self._reply(chat_id, (
            "🟢 *Kill switch reset*\\.\n"
            "Bot is now RUNNING\\.\n"
            "Send /status to confirm\\."
        ))

    def _cmd_help(self, chat_id: str, args: list) -> None:
        self._reply(chat_id, fmt_help())

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _reply(self, chat_id: str, text: str) -> None:
        url     = _TELEGRAM_SEND.format(token=self.token)
        payload = {
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }
        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.post(url, json=payload)
                if r.status_code != 200:
                    # Fallback: plain text
                    payload["parse_mode"] = ""
                    payload["text"]       = text.replace("\\", "").replace("*","").replace("`","").replace("_","")
                    c.post(url, json=payload)
        except Exception as e:
            logger.error(f"Telegram reply error: {e}")

    def _get_trades_for_date(self, date) -> list:
        """Pull closed trades for a given date from in-memory portfolio."""
        result = []
        for t in self.portfolio.closed_trades:
            if t.closed_at is None:
                continue
            closed = t.closed_at
            if closed.tzinfo is None:
                closed = closed.replace(tzinfo=timezone.utc)
            if closed.date() == date:
                result.append({
                    "trade_id":    t.trade_id,
                    "direction":   t.direction,
                    "entry_price": t.entry_price,
                    "exit_price":  t.exit_price,
                    "lot_size":    t.lot_size,
                    "pnl":         t.pnl,
                    "pnl_pips":    t.pnl_pips,
                    "close_reason":t.reason,
                    "opened_at":   t.opened_at,
                    "closed_at":   t.closed_at,
                })
        return result

    def _on_auto_resume(self, resumed_by: str) -> None:
        """Called by PauseManager when auto-timer fires."""
        self.notifier.send(fmt_bot_resumed(
            resumed_by="Auto-timer",
            resumed_at=datetime.now(timezone.utc),
        ))
