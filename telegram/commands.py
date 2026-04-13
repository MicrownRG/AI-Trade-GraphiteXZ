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
from config.trading_config import TRADING_CONFIG, TradeMode
from config.settings import USE_AI, AI_PROVIDER
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, List

from telegram.notifier import TelegramNotifier
from telegram.formatter import (
    fmt_status, fmt_trades_list, fmt_daily_summary,
    fmt_bot_paused, fmt_bot_resumed,
    fmt_balance_quick, fmt_help, fmt_unknown_command, fmt_unauthorized,
    fmt_positions_list,
    _wib,
)
from telegram.pause_manager import pause_manager, BotState

from core.risk.portfolio import Portfolio
from ai.providers.registry import get_active_provider_name
from ai.learning_engine import LearningEngine
from utils.report_formatter import format_eod_report
from telegram.calendar_ui import create_calendar
from utils.logger import get_logger

logger = get_logger(__name__)

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

_TELEGRAM_UPDATES = "https://api.telegram.org/bot{token}/getUpdates"
_TELEGRAM_SEND    = "https://api.telegram.org/bot{token}/sendMessage"
_TELEGRAM_SET_CMDS = "https://api.telegram.org/bot{token}/setMyCommands"


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
        self.user_prefs        = {}

        # Whitelist: if empty, only the primary TELEGRAM_CHAT_ID is accepted
        primary = os.getenv("TELEGRAM_CHAT_ID", "")
        extra   = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
        self.allowed = set(
            filter(None, [primary] + (allowed_chat_ids or []) + extra)
        )

        self._offset    = 0
        self._running   = False
        self._thread: Optional[threading.Thread] = None
        self._executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="tg_worker")

        # Shared HTTP client for performance
        self.client = httpx.Client(timeout=10.0, limits=httpx.Limits(max_keepalive_connections=5, max_connections=10))

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
        self._set_bot_commands()
        logger.info(f"Telegram command handler started — whitelisted: {list(self.allowed)}")

    def stop(self) -> None:
        self._running = False
        try:
            self._executor.shutdown(wait=False)
            self.client.close()
        except:
            pass
        logger.info("Telegram command handler stopped")

    # ── Polling loop ──────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while self._running:
            try:
                updates = self._get_updates()
                for update in updates:
                    self._offset = update["update_id"] + 1
                    
                    # Offload to thread pool for parallel processing
                    self._executor.submit(self._process_update, update)
                        
            except Exception as e:
                logger.error(f"Telegram poll error: {e}")
                time.sleep(5)
            time.sleep(1)

    def _process_update(self, update: dict) -> None:
        """Entry point for each update thread."""
        try:
            if "message" in update:
                self._dispatch_message(update["message"])
            elif "callback_query" in update:
                cq = update["callback_query"]
                logger.debug(f"TG Callback: {cq.get('data')} from {cq.get('from',{}).get('username')}")
                self._dispatch_callback(cq)
        except Exception as e:
            logger.error(f"Update processing error: {e}")

    def _get_updates(self) -> list:
        url    = _TELEGRAM_UPDATES.format(token=self.token)
        params = {
            "offset": self._offset, 
            "timeout": 30, 
            "allowed_updates": ["message", "callback_query"]
        }
        try:
            with httpx.Client(timeout=35.0) as c:
                r = c.get(url, params=params)
                if r.status_code == 200:
                    return r.json().get("result", [])
        except Exception:
            pass
        return []

    # ── Dispatcher ────────────────────────────────────────────────────────────

    def _dispatch_message(self, message: dict) -> None:
        chat_id  = str(message.get("chat", {}).get("id", ""))
        from_id  = str(message.get("from", {}).get("id", ""))
        from_usr = message.get("from", {}).get("username", "unknown")
        is_bot   = message.get("from", {}).get("is_bot", False)
        text     = message.get("text", "").strip()

        if not text.startswith("/") or is_bot:
            return

        parts = text.split()
        if not parts:
            return
        cmd   = parts[0].lower().split("@")[0]
        args  = parts[1:]

        is_authorized = chat_id in self.allowed or from_id in self.allowed

        # /help and /start are public but show unauthorized status
        if cmd in ["/help", "/start"]:
            self._cmd_help(chat_id, args)
            if not is_authorized:
                self._reply(chat_id, fmt_unauthorized())
            return

        if not is_authorized:
            self._reply(chat_id, fmt_unauthorized())
            logger.warning(f"Unauthorized access: {cmd} from @{from_usr}")
            return

        logger.info(f"Telegram cmd: {cmd} from @{from_usr}")

        handlers = {
            "/today":   self._cmd_today,
            "/trades":  self._cmd_trades,
            "/status":  self._cmd_status,
            "/balance": self._cmd_balance,
            "/pnl":     self._cmd_pnl,
            "/pause":   self._cmd_pause,
            "/resume":  self._cmd_resume,
            "/reset":   self._cmd_reset,
            "/asian":   self._cmd_asian,
            "/partial": self._cmd_partial,
            "/pulse":   self._cmd_pulse,
            "/notify":  lambda c, a: self._cmd_notify(c, from_id, a),
            "/trend":   lambda c, a: self._cmd_trend(c, from_id, a),
            "/mode":    self._cmd_mode,
            "/mode_info": self._cmd_mode_info,
            "/positions": self._cmd_positions,
            "/buy":       lambda c, a: self._cmd_manual_entry(c, a, "buy"),
            "/sell":      lambda c, a: self._cmd_manual_entry(c, a, "sell"),
            "/eod":       self._cmd_eod,
            "/cutloss":   self._cmd_cutloss,
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

    def _dispatch_callback(self, cq: dict) -> None:
        """Handle inline button clicks (Callback Queries)."""
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        cq_id   = cq.get("id")
        data    = cq.get("data", "")
        from_id = str(cq.get("from", {}).get("id", ""))
        
        # Security check
        if from_id not in self.allowed:
            self._answer_callback(cq_id, "🚫 Unauthorized")
            return

        # Handle calendar selection: cal:set:YYYY:MM:DD
        if data.startswith("cal:set:"):
            self._answer_callback(cq_id)
            _, _, y, m, d = data.split(":")
            date = datetime(int(y), int(m), int(d)).date()
            trades = self._get_trades_for_date(date)
            date_str = date.strftime("%d/%m/%Y")
            
            # Edit original message to remove keyboard and show data
            self._edit_message(
                chat_id, 
                cq.get("message", {}).get("message_id"),
                fmt_trades_list(trades, date_str)
            )

        # Handle calendar navigation: cal:nav:YYYY:MM
        elif data.startswith("cal:nav:"):
            self._answer_callback(cq_id)
            _, _, y, m = data.split(":")
            kb = create_calendar(int(y), int(m))
            self._edit_reply_markup(
                chat_id,
                cq.get("message", {}).get("message_id"),
                kb
            )

        elif data == "notify:toggle":
            is_on = self.bot_ref.user_prefs.get(from_id, False)
            new_val = not is_on
            self.bot_ref.user_prefs[from_id] = new_val
            
            # Send immediate toast feedback
            toast = "✅ Alerts: ENABLED" if new_val else "🔕 Alerts: DISABLED"
            self._answer_callback(cq_id, toast)
            
            status = r"ENABLED \(AI \+ Risk\)" if new_val else r"DISABLED \(Basic only\)"
            btn_text = "🔕 Turn OFF Full Alerts" if new_val else "🔔 Turn ON Full Alerts"
            
            kb = {"inline_keyboard": [[{"text": btn_text, "callback_data": "notify:toggle"}]]}
            self._edit_message(chat_id, cq.get("message", {}).get("message_id"), 
                              f"⚡ *Personal Notifications*\n{'─' * 15}\nState: *{status}*\n\nWhen ON, you will see AI confidence and Risk rejection reasons\\. When OFF, you only see basic trade entries\\.", kb)

        elif data == "trend:toggle":
            is_on = self.bot_ref.trend_notify_users.get(from_id, self.bot_ref._trend_notify_default)
            new_val = not is_on
            self.bot_ref.trend_notify_users[from_id] = new_val
            
            toast = "📊 Trend Alerts: ON" if new_val else "🔕 Trend Alerts: OFF"
            self._answer_callback(cq_id, toast)
            
            # Adaptive logic for the dashboard update
            mtf_on = getattr(TRADING_CONFIG, "enable_multi_tf", True)
            title = "🛡️ GLOBAL DASHBOARD: SAFE" if (new_val and mtf_on) else "⚡ GLOBAL DASHBOARD: AGRESSIVE"
            
            self._cmd_trend(chat_id, from_id, [])

        elif data.startswith("trend:interval:set:"):
            self._answer_callback(cq_id)
            minutes = int(data.split(":")[-1])
            TRADING_CONFIG.trend_update_interval_minutes = minutes
            
            toast = f"⏲️ Interval: {minutes}m" if minutes > 0 else "⏲️ Interval: OFF"
            self._answer_callback(cq_id, toast)
            self._cmd_trend(chat_id, from_id, [])

        elif data == "trend:refresh":
            self._answer_callback(cq_id, "🔄 Refreshing analysis...")
            self._cmd_trend(chat_id, from_id, [])

        elif data == "multitf:toggle":
            current = getattr(TRADING_CONFIG, "enable_multi_tf", True)
            new_val = not current
            TRADING_CONFIG.enable_multi_tf = new_val
            
            toast = "⚙️ Multi-TF: ON" if new_val else "⚠️ Multi-TF: OFF (Agresif)"
            self._answer_callback(cq_id, toast)
            
            # Adaptive logic for the dashboard update
            notif_on = self.bot_ref.trend_notify_users.get(from_id, self.bot_ref._trend_notify_default)
            title = "🛡️ GLOBAL DASHBOARD: SAFE" if (notif_on and new_val) else "⚡ GLOBAL DASHBOARD: AGRESSIVE"
            
            notif_status = "AKTIF (📢)" if notif_on else "MATI (🔕)"
            notif_btn = "🔕 Turn OFF Alerts" if notif_on else "🔔 Turn ON Alerts"
            
            mtf_status = "AKTIF (Konfluensi)" if new_val else "OFF (Trend H1)"
            mtf_btn = "🔴 Disable Multi-TF" if new_val else "🟢 Enable Multi-TF"
            
            kb = {
                "inline_keyboard": [
                    [{"text": notif_btn, "callback_data": "trend:toggle"}],
                    [{"text": mtf_btn, "callback_data": "multitf:toggle"}]
                ]
            }
            msg = (
                f"📊 *{title}*\n"
                f"{'─' * 20}\n"
                f"📢 *Notifikasi Trend*: _{notif_status}_\n"
                f"⚙️ *Multi-TF Engine*: _{mtf_status}_\n\n"
                f"• *Safe*: Konfluensi multi-timeframe (Lebih Aman)\n"
                f"• *Agress*: Hanya tren H1 (Fokus Peluang)\n\n"
                f"_Update: Sistem Adaptif Sesi aktif_"
            )
            self._edit_message(chat_id, cq.get("message", {}).get("message_id"), msg, kb)

        elif data == "partial:toggle":
            current = getattr(TRADING_CONFIG, "enable_partial_close", True)
            new_val = not current
            TRADING_CONFIG.enable_partial_close = new_val
            
            toast = "💰 Partial: ON" if new_val else "🔕 Partial: OFF"
            self._answer_callback(cq_id, toast)
            
            status = "ENABLED" if new_val else "DISABLED"
            btn_text = "❌ Turn OFF Partial" if new_val else "✅ Turn ON Partial"
            
            kb = {"inline_keyboard": [[{"text": btn_text, "callback_data": "partial:toggle"}]]}
            msg = (
                f"💰 *Partial Close*\n"
                f"{'─' * 15}\n"
                f"State: *{status}*\n\n"
                f"When ON, bot takes 50% profit at RR 1:1\\. When OFF, bot holds full lot to final TP\\."
            )
            self._edit_message(chat_id, cq.get("message", {}).get("message_id"), msg, kb)

        elif data == "asian:toggle":
            current = getattr(TRADING_CONFIG, "enable_asian_session", True)
            new_val = not current
            TRADING_CONFIG.enable_asian_session = new_val
            
            toast = "🌏 Asian: ON" if new_val else "🔕 Asian: OFF"
            self._answer_callback(cq_id, toast)
            
            status = "ENABLED" if new_val else "DISABLED"
            btn_text = "❌ Turn OFF Asian" if new_val else "✅ Turn ON Asian"
            
            kb = {"inline_keyboard": [[{"text": btn_text, "callback_data": "asian:toggle"}]]}
            msg = (
                f"🌏 *Asian Session*\n"
                f"{'─' * 15}\n"
                f"State: *{status}*\n\n"
                f"When ON, bot will trade selama sesi Asia (00:00\\-08:00 WIB)\\. Saat OFF, bot akan standby\\."
            )
            self._edit_message(chat_id, cq.get("message", {}).get("message_id"), msg, kb)

        elif data.startswith("mode:set:"):
            mode_str = data.split(":")[-1]
            try:
                new_mode = TradeMode[mode_str]
                TRADING_CONFIG.current_mode = new_mode
                
                # Toast feedback (Top of screen) - only call THIS once per cq_id
                self._answer_callback(cq_id, f"✅ Mode: {new_mode.value}")
                
                # Update the original menu message to show the new Current mode, keep buttons!
                curr = TRADING_CONFIG.current_mode
                msg = (
                    f"🛠️ *Select Trade Mode*\n"
                    f"{'─' * 20}\n"
                    f"Current: *{curr.value}*\n\n"
                    f"Choose a mode below to immediately switch risk levels and logic engines\\."
                )
                
                # Re-build the same keyboard
                kb = {
                    "inline_keyboard": [
                        [
                            {"text": "🛡️ Con", "callback_data": "mode:set:CONSERVATIVE"},
                            {"text": "⚖️ Mod", "callback_data": "mode:set:MODERATE"}
                        ],
                        [
                            {"text": "⚡ Agr", "callback_data": "mode:set:AGGRESSIVE"},
                            {"text": "🏎️ V-Agr", "callback_data": "mode:set:VERY_AGGRESSIVE"}
                        ],
                        [
                            {"text": "🚀 Ultra Scalper", "callback_data": "mode:set:ULTRA_SCALPER"}
                        ]
                    ]
                }
                
                self._edit_message(chat_id, cq.get("message", {}).get("message_id"), msg, kb)
                
                # Send explicit notification independently
                notice = (
                    f"🎯 *Trade Mode Updated*\n"
                    f"Mesin bot sekarang menggunakan parameter: `{new_mode.value}`\\."
                )
                self._reply(chat_id, notice)
                
                logger.info(f"Trade mode changed to {new_mode.value} via Telegram")
            except Exception as e:
                self._answer_callback(cq_id, "⚠️ Error setting mode")
                logger.error(f"Mode change error: {e}")

        elif data.startswith("close:init:"):
            self._answer_callback(cq_id)
            ticket = data.split(":")[-1]
            msg = f"❓ *Confirm Close*\n\nAre you sure you want to close position *#{ticket}*?"
            kb = {
                "inline_keyboard": [[
                    {"text": "✅ Yes, Close", "callback_data": f"close:confirm:{ticket}"},
                    {"text": "🚫 Cancel", "callback_data": "close:cancel"}
                ]]
            }
            self._edit_message(chat_id, cq.get("message", {}).get("message_id"), msg, kb)

        elif data.startswith("close:confirm:"):
            ticket = int(data.split(":")[-1])
            success = self.bot_ref.order_manager.close_by_ticket(ticket)
            if success:
                self._answer_callback(cq_id, "✅ Position closed")
                self._edit_message(chat_id, cq.get("message", {}).get("message_id"), f"✅ Position *#{ticket}* has been closed successfully\\.")
            else:
                self._answer_callback(cq_id, "⚠️ Failed to close")
                self._edit_message(chat_id, cq.get("message", {}).get("message_id"), f"⚠️ Could not close *#{ticket}*\\. (Ticket not found or MT5 error)")

        elif data == "close:cancel":
            self._answer_callback(cq_id, "Cancelled")
            # Clear buttons or go back to positions list
            self._cmd_positions(chat_id, [])
            
        elif data.startswith("reset:"):
            action = data.split(":")[1]
            if action == "all":
                import core.risk.filters as filters
                filters.GLOBAL_COOLDOWN_CLEARED_AT = datetime.now(timezone.utc)
                self.bot_ref.portfolio.pulse_suspended_today = False
                self.bot_ref.portfolio.pulse_consecutive_losses = 0
                if hasattr(self.bot_ref, "telegram") and hasattr(self.bot_ref.telegram, "_stalking_cooldowns"):
                    self.bot_ref.telegram._stalking_cooldowns.clear()
                self._answer_callback(cq_id, "✅ ALL cooldowns reset")
                self._edit_message(chat_id, cq.get("message", {}).get("message_id"), "✅ *All Cooldowns Cleared*\\.\nBot is fully armed\\.")
                
            elif action == "revenge":
                import core.risk.filters as filters
                filters.GLOBAL_COOLDOWN_CLEARED_AT = datetime.now(timezone.utc)
                self._answer_callback(cq_id, "✅ Revenge SL timer cleared")
                self._edit_message(chat_id, cq.get("message", {}).get("message_id"), "⏱️ *Revenge Cooldown Reset*\\.\nBot can enter immediately\\.")
                
            elif action == "pulse":
                self.bot_ref.portfolio.pulse_suspended_today = False
                self.bot_ref.portfolio.pulse_consecutive_losses = 0
                self._answer_callback(cq_id, "✅ Pulse Guard cleared")
                self._edit_message(chat_id, cq.get("message", {}).get("message_id"), "⚡ *Pulse Scalper Guard Cleared*\\.\nReady for Asian burst\\.")

            elif action == "stalk":
                if hasattr(self.bot_ref, "telegram") and hasattr(self.bot_ref.telegram, "_stalking_cooldowns"):
                    self.bot_ref.telegram._stalking_cooldowns.clear()
                self._answer_callback(cq_id, "✅ Stalking logs cleared")
                self._edit_message(chat_id, cq.get("message", {}).get("message_id"), "👀 *Pantau Spam Cooldown Cleared*\\.")

        elif data == "ignore":
            self._answer_callback(cq_id)

        else:
            # Fallback for any other buttons
            self._answer_callback(cq_id)

    # ── Command implementations ───────────────────────────────────────────────

    def _cmd_today(self, chat_id: str, args: list) -> None:
        if not hasattr(self, "bot_ref") or not self.bot_ref:
            self._reply(chat_id, "⚠️ Bot offline.")
            return
            
        today_local = datetime.now().date()
        start_query = datetime.now() - timedelta(days=2)
        end_query   = datetime.now() + timedelta(days=1)
        
        all_trades = self.bot_ref.order_manager.mt5.get_history_deals(start_query.replace(tzinfo=None), end_query.replace(tzinfo=None))
        
        # Filter explicitly to match local day
        trades = []
        for t in all_trades:
            # Convert UTC datetime to local machine's naive day
            if t["closed_at"].astimezone().date() == today_local:
                trades.append(t)
                
        date_str = today_local.strftime("%d/%m/%Y")
        self._reply(chat_id, fmt_trades_list(trades, date_str))

    def _cmd_eod(self, chat_id: str, args: List[str]) -> None:
        """Triggers AI EOD Retrospective."""
        self._reply(chat_id, "🧠 *Analyzing session data...* This may take a few seconds\\.")
        
        try:
            engine = LearningEngine(self.repo)
            limit = int(args[0]) if args else 20
            analysis = engine.run_retrospective(trade_limit=limit)
            
            report = format_eod_report(analysis)
            self._reply(chat_id, report)
        except Exception as e:
            logger.error(f"EOD command error: {e}")
            self._reply(chat_id, "⚠️ Failed to generate AI analysis report.")

    def _cmd_trades(self, chat_id: str, args: List[str]) -> None:
        if not hasattr(self, "bot_ref") or not self.bot_ref:
            self._reply(chat_id, "⚠️ Bot offline.")
            return
            
        if args:
            try:
                date = datetime.strptime(args[0], "%d/%m/%Y")
            except ValueError:
                self._reply(chat_id, "⚠️ Format tanggal salah\\. Gunakan: `/trades DD/MM/YYYY`")
                return
        else:
            date = datetime.now()

        start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        
        start_query = start - timedelta(days=2)
        end_query   = start + timedelta(days=2)
        
        all_trades = self.bot_ref.order_manager.mt5.get_history_deals(start_query.replace(tzinfo=None), end_query.replace(tzinfo=None))
        
        trades = []
        for t in all_trades:
            if t["closed_at"].astimezone().date() == date.date():
                trades.append(t)
                
        date_str = date.strftime("%d/%m/%Y")
        self._reply(chat_id, fmt_trades_list(trades, date_str))

    def _cmd_trades_v2(self, chat_id: str, args: list) -> None:
        """Enhanced /trades command with calendar picker."""
        if args:
            # Fallback to manual date if provided
            try:
                d = datetime.strptime(args[0], "%d/%m/%Y")
                start = d.replace(hour=0, minute=0, second=0, microsecond=0)
                
                start_query = start - timedelta(days=2)
                end_query   = start + timedelta(days=2)
                all_trades = self.bot_ref.order_manager.mt5.get_history_deals(start_query.replace(tzinfo=None), end_query.replace(tzinfo=None))
                
                trades = []
                for t in all_trades:
                    if t["closed_at"].astimezone().date() == d.date():
                        trades.append(t)
                        
                self._reply(chat_id, fmt_trades_list(trades, d.strftime("%d/%m/%Y")))
            except ValueError:
                self._reply(chat_id, "⚠️ Format tanggal salah\\. Gunakan: `/trades DD/MM/YYYY` atau panggil `/trades` saja untuk kalender\\.")
            return

        # No args? Show calendar
        kb = create_calendar()
        self._reply(chat_id, "📅 *Pilih Tanggal:*", kb)

    # Alias for /trades
    _cmd_trades = _cmd_trades_v2

    def _cmd_status(self, chat_id: str, args: list) -> None:
        pf  = self.portfolio
        pm  = pause_manager

        msg = fmt_status(
            is_running         = pm.state == BotState.RUNNING,
            is_paused          = pm.state == BotState.PAUSED,
            pause_reason       = pm.reason,
            resume_at          = pm.resume_at,
            balance            = pf.balance,
            equity             = pf.equity,
            drawdown_pct       = pf.drawdown_pct,
            realized_pnl       = pf.realized_pnl,
            floating_pnl       = pf.current_floating_pnl,
            total_pnl          = pf.daily_pnl,
            open_trades        = pf.open_trade_count,
            trades_today       = self.get_trades_today(),
            active_provider    = get_active_provider_name(),
            checked_at         = datetime.now(timezone.utc),
            current_mode       = TRADING_CONFIG.current_mode.value,
            ai_enabled         = USE_AI,
            pulse_suspended    = pf.pulse_suspended_today,
        )
        self._reply(chat_id, msg)

    def _cmd_balance(self, chat_id: str, args: list) -> None:
        pf = self.portfolio
        self._reply(chat_id, fmt_balance_quick(pf.balance, pf.equity, pf.daily_pnl))

    def _cmd_pnl(self, chat_id: str, args: list) -> None:
        if not hasattr(self, "bot_ref") or not self.bot_ref:
            self._reply(chat_id, "⚠️ Bot offline.")
            return
            
        now = datetime.now()
        week_ago = now - timedelta(days=7)
        week_ago_date = week_ago.date()
        
        # Query MT5 with padding
        start_query = week_ago - timedelta(days=2)
        end_query   = now + timedelta(days=1)
        
        all_trades = self.bot_ref.order_manager.mt5.get_history_deals(start_query.replace(tzinfo=None), end_query.replace(tzinfo=None))
        
        trades = []
        for t in all_trades:
            if t["closed_at"].astimezone().date() >= week_ago_date:
                trades.append(t)

        if not trades:
            self._reply(chat_id, "📭 *No closed trades in the last 7 days*")
            return

        total = sum(t.get("pnl", 0) for t in trades)
        wins  = sum(1 for t in trades if t.get("pnl", 0) > 0)
        losses= len(trades) - wins
        sign  = "\\+" if total >= 0 else "\\-"

        # Enforced backticks for unescaped floats to fix Telegram 400 Bad Request
        msg = (
            f"📊 *P\\&L \\— Last 7 Days*\n"
            f"{'─'*20}\n"
            f"Trades: `{len(trades)}`  \\({wins}W / {losses}L\\)\n"
            f"Total:  `{sign}${abs(total):.2f}`"
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

    def _cmd_cutloss(self, chat_id: str, args: list) -> None:
        """Close all positions (Panic Close). Does not halt the bot."""
        if not hasattr(self, "bot_ref") or not self.bot_ref:
            self._reply(chat_id, "⚠️ Bot reference is missing\\. Cannot close positions\\.")
            return

        closed_count = self.bot_ref.order_manager.close_all_positions(reason="manual_cutloss")
        
        self._reply(chat_id, (
            f"🚨 *GLOBAL CUT LOSS ACTIVATED* 🚨\n"
            f"{'─'*20}\n"
            f"✅ Sent close/cover signal to *{closed_count}* MT5 positions\\.\n"
            f"🟢 Bot remains *RUNNING* and will hunt for new signals\\.\n"
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
                "⚠️ *Bot is not paused* — nothing to resume\\."
            )


    def _cmd_reset(self, chat_id: str, args: list) -> None:
        msg = "🛠️ *Cooldown Manager*\n\nPilih batasan yang ingin dihapus paksa:"
        kb = {
            "inline_keyboard": [
                [{"text": "🔄 Reset ALL Cooldowns", "callback_data": "reset:all"}],
                [{"text": "⏱️ Reset Revenge Timer", "callback_data": "reset:revenge"}],
                [{"text": "⚡ Reset Pulse Guard", "callback_data": "reset:pulse"}],
                [{"text": "👀 Reset Pantau Limiter", "callback_data": "reset:stalk"}]
            ]
        }
        self._reply(chat_id, msg, kb)

    def _cmd_help(self, chat_id: str, args: list) -> None:
        self._reply(chat_id, fmt_help())

    def _cmd_notify(self, chat_id: str, user_id: str, args: list) -> None:
        """Toggle detailed alerts (AI/Risk) per-user."""
        if not hasattr(self, "bot_ref") or not self.bot_ref:
            self._reply(chat_id, "⚠️ Bot settings not available\\.")
            return

        is_on = self.bot_ref.user_prefs.get(user_id, False)
        status = r"ENABLED \(AI \+ Risk\)" if is_on else r"DISABLED \(Basic only\)"
        btn_text = "🔕 Turn OFF Full Alerts" if is_on else "🔔 Turn ON Full Alerts"
        
        msg = (
            f"⚡ *Personal Notifications*\n"
            f"{'─' * 15}\n"
            f"State: *{status}*\n\n"
            f"When ON, you will see AI confidence and Risk rejection reasons\\. When OFF, you only see basic trade entries\\."
        )
        
        kb = {
            "inline_keyboard": [[
                {"text": btn_text, "callback_data": "notify:toggle"}
            ]]
        }
        self._reply(chat_id, msg, kb)

    def _cmd_mode(self, chat_id: str, args: list) -> None:
        """Show mode selection keyboard."""
        curr = TRADING_CONFIG.current_mode
        msg = (
            f"🛠️ *Select Trade Mode*\n"
            f"{'─' * 20}\n"
            f"Current: *{curr.value}*\n\n"
            f"Choose a mode below to immediately switch risk levels and logic engines\\."
        )
        
        kb = {
            "inline_keyboard": [
                [
                    {"text": "🛡️ Con", "callback_data": "mode:set:CONSERVATIVE"},
                    {"text": "⚖️ Mod", "callback_data": "mode:set:MODERATE"}
                ],
                [
                    {"text": "⚡ Agr", "callback_data": "mode:set:AGGRESSIVE"},
                    {"text": "🏎️ V-Agr", "callback_data": "mode:set:VERY_AGGRESSIVE"}
                ],
                [
                    {"text": "🚀 Ultra Scalper", "callback_data": "mode:set:ULTRA_SCALPER"}
                ]
            ]
        }
        self._reply(chat_id, msg, kb)

    def _cmd_mode_info(self, chat_id: str, args: list) -> None:
        """Display detailed risk/config info for all modes."""
        msg = "📑 *Trade Modes Cheat Sheet*\n\n"
        
        for mode, s in TRADING_CONFIG.mode_settings.items():
            recovery   = "✅ Yes" if s.get("use_recovery") else "❌ No"
            auto_be    = "✅" if s.get("auto_be", False) else "❌"
            partial_cl = "✅" if s.get("partial_close", False) else "❌"
            risk_pct   = s.get("risk_per_trade", 0) * 100
            min_score  = s.get("min_score_threshold", "?")

            logic_desc = "ICT \\+ Alpha Filter"
            if mode.name in ("AGGRESSIVE", "MODERATE"):   logic_desc = "SMC Hybrid \\+ Alpha"
            if mode.name == "VERY_AGGRESSIVE":             logic_desc = "Max Momentum \\+ Alpha"

            msg += (
                f"▪️ *{mode.value}*\n"
                f"   • Logic: {logic_desc}\n"
                f"   • Risk: `{risk_pct:.1f}%` / trade\n"
                f"   • Min Score: `{min_score}`\n"
                f"   • Recovery: {recovery}\n"
                f"   • Auto\\-BE: {auto_be}\n"
                f"   • Partial Close: {partial_cl}\n\n"
            )
        
        msg += (
            f"\U0001f6e1\ufe0f *GainzAlgo v2 Alpha Active*\n"
            f"\u2022 Structure: BOS/CHoCH Alpha Validation\n"
            "\u2022 Filter: Expansion \\> `0\\.5\\*ATR` required\n"
            "\u2022 Safety Check: Margin \\& News active\n"
            f"\u2022 AI Provider: `{AI_PROVIDER.upper()}`\n"
            f"\u2022 AI Validation: `{'ACTIVE' if USE_AI else 'OFF'}`"
        )
        self._reply(chat_id, msg)

    def _cmd_asian(self, chat_id: str, args: list) -> None:
        """Toggle Asian session filter on/off via Telegram command."""
        current = getattr(TRADING_CONFIG, "enable_asian_session", True)
        status = "ENABLED" if current else "DISABLED"
        btn_text = "❌ Turn OFF Asian" if current else "✅ Turn ON Asian"
        
        msg = (
            f"🌏 *Asian Session*\n"
            f"{'─' * 15}\n"
            f"State: *{status}*\n\n"
            f"When ON, bot will trade during 00:00\\-08:00 WIB\\. When OFF, this session is skipped\\."
        )
        
        kb = {"inline_keyboard": [[{"text": btn_text, "callback_data": "asian:toggle"}]]}
        self._reply(chat_id, msg, kb)

    def _cmd_partial(self, chat_id: str, args: list) -> None:
        """Toggle Partial Close feature via Telegram command."""
        current = getattr(TRADING_CONFIG, "enable_partial_close", True)
        status = "ENABLED" if current else "DISABLED"
        btn_text = "❌ Turn OFF Partial" if current else "✅ Turn ON Partial"
        
        msg = (
            f"💰 *Partial Close*\n"
            f"{'─' * 15}\n"
            f"State: *{status}*\n\n"
            f"When ON, bot takes 50% profit at RR 1:1\\. When OFF, bot holds full lot to final TP\\."
        )
        
        kb = {"inline_keyboard": [[{"text": btn_text, "callback_data": "partial:toggle"}]]}
        self._reply(chat_id, msg, kb)

    def _cmd_pulse(self, chat_id: str, args: list) -> None:
        """Manually un-suspend or toggle Pulse Scalping for the bot."""
        pf = self.portfolio
        
        # If it was suspended, user wants to resume it
        if pf.pulse_suspended_today:
            pf.pulse_suspended_today = False
            pf.pulse_consecutive_losses = 0
            self._reply(chat_id, (
                f"🔌 *Pulse Scalper Reset*\n"
                f"{chr(8212)*20}\n"
                f"Status: *RESUMED*\n\n"
                f"The 3x Loss Guard has been manually cleared. Pulse scalping is allowed again."
            ))
            return
            
        settings = TRADING_CONFIG.mode_settings.get(TRADING_CONFIG.current_mode, {})
        pulse_enabled_in_mode = settings.get("pulse_scalping", False)
        
        if not pulse_enabled_in_mode:
            self._reply(chat_id, "⚠️ Pulse Scalping is DISABLED in the current Trade Mode. Switch to VERY_AGGRESSIVE to use it.")
        else:
            self._reply(chat_id, "ℹ️ Pulse Scalping is actively running and has not hit the 3x Loss Guard limit yet.")

    def _cmd_trend(self, chat_id: str, from_id: str, args: list) -> None:
        """Dashboard for Trend Alerts and Multi-TF Engine control."""
        if not hasattr(self, 'bot_ref') or not self.bot_ref:
            self._reply(chat_id, "⚠️ Bot reference not available\\.")
            return
        
        # 1. Trend Notif State
        notif_on = self.bot_ref.trend_notify_users.get(from_id, self.bot_ref._trend_notify_default)
        notif_status = "ENABLED" if notif_on else "DISABLED"
        notif_btn = "🔕 Turn OFF Alerts" if notif_on else "🔔 Turn ON Alerts"
        
        # 2. Multi-TF Logic State
        mtf_on = getattr(TRADING_CONFIG, "enable_multi_tf", True)
        mtf_status = "ACTIVE (Confluence)" if mtf_on else "OFF (Trend Only)"
        mtf_btn = "🔴 Disable Multi-TF" if mtf_on else "🟢 Enable Multi-TF"
        
        # 3. Live TF Breakdown
        tf_list = ""
        analysis = None
        if hasattr(self.bot_ref, "multi_tf") and self.bot_ref.multi_tf:
            analysis = self.bot_ref.multi_tf.last_result
        
        if analysis:
            tf_list = "\n*Live Multi-TF Status:*\n"
            from telegram.formatter import _bias_emoji, _escape
            # Sort order
            for tf_key in ["h4", "h1", "m30", "m15", "m5", "m1"]:
                if tf_key in analysis.tfs:
                    t = analysis.tfs[tf_key]
                    emoji = _bias_emoji(t.bias)
                    zone_icon = " 🛡️" if t.at_zone != "NONE" else ""
                    tf_list += f"  • {tf_key.upper():<3}: {emoji} `{_escape(t.bias)}`{zone_icon}\n"
            
            tf_list += f"\n🏆 Confluence Bias: *{_escape(analysis.master_bias)}*\n"
            if analysis.direction:
                tf_list += f"🎯 Setup Bias: *{_escape(analysis.direction.upper())}*\n"
        else:
            tf_list = "\n_⏳ Menunggu analisa TF pertama..._\n"

        # Adaptive Dashboard Title
        title = "🛡️ GLOBAL DASHBOARD: SAFE" if (notif_on and mtf_on) else "⚡ GLOBAL DASHBOARD: AGRESSIVE"
        interval = TRADING_CONFIG.trend_update_interval_minutes
        
        msg = (
            f"📊 *{title}*\n"
            f"{'─' * 20}\n"
            f"📢 Alerts: *{notif_status}*\n"
            f"⚙️ Multi-TF: *{mtf_status}*\n"
            f"⏲️ Interval: *{interval} minutes*\n"
            f"{tf_list}\n"
            f"Sistem saat ini sudah **Adaptif Sesi Market**\\. SL/RR menyesuaikan otomatis antara Asia, London, dan US\\."
        )
        
        kb = {
            "inline_keyboard": [
                [{"text": notif_btn, "callback_data": "trend:toggle"}],
                [{"text": mtf_btn, "callback_data": "multitf:toggle"}],
                [
                    {"text": "⏲️ 5m", "callback_data": "trend:interval:set:5"},
                    {"text": "⏲️ 15m", "callback_data": "trend:interval:set:15"},
                    {"text": "⏲️ 1h", "callback_data": "trend:interval:set:60"},
                    {"text": "❌ OFF", "callback_data": "trend:interval:set:0"}
                ],
                [{"text": "🔄 Refresh Now", "callback_data": "trend:refresh"}]
            ]
        }
        self._reply(chat_id, msg, kb)

    def _cmd_manual_entry(self, chat_id: str, args: list, direction: str = "buy") -> None:
        """
        Handle /buy and /sell manual entry commands.
        Usage: /buy [lot] [sl=price] [tp=price]
        Example: /buy 0.1 sl=2320 tp=2350
        """
        if not hasattr(self, 'bot_ref') or not self.bot_ref:
            self._reply(chat_id, "\u26a0\ufe0f Bot reference not available\\.")
            return

        if not args:
            self._reply(chat_id,
                f"\u26a0\ufe0f Usage: `/{direction} [lot] sl=[price] tp=[price]`\n"
                f"Example: `/{direction} 0\\.1 sl=2320 tp=2350`"
            )
            return

        try:
            lot = float(args[0])
            if lot <= 0 or lot > 5.0:
                self._reply(chat_id, "\u26a0\ufe0f Lot size harus antara 0\\.01 \\- 5\\.0")
                return

            params = {"sl": None, "tp": None}
            for arg in args[1:]:
                if "=" in arg:
                    k, v = arg.split("=", 1)
                    k = k.strip().lower()
                    if k in params:
                        params[k] = float(v)

            # Get current price
            om = self.bot_ref.order_manager
            tick = om.mt5.get_symbol_tick("XAUUSD")
            if not tick:
                self._reply(chat_id, "\u274c Tidak bisa ambil harga terkini dari MT5\\.")
                return

            curr_price = (tick["bid"] + tick["ask"]) / 2
            atr_default = 3.0  # ~30 pips default SL if not specified

            sl = params["sl"] or (curr_price - atr_default if direction == "buy" else curr_price + atr_default)
            tp = params["tp"] or (curr_price + atr_default * 2 if direction == "buy" else curr_price - atr_default * 2)

            self._reply(chat_id, f"🔄 Processing manual *{direction.upper()}* `{lot}`L....")

            from core.risk.executor import TradeOrder
            from core.risk.take_profit import TakeProfitLevels
            order = TradeOrder(
                signal_id          = f"MANUAL_{int(time.time())}",
                symbol             = "XAUUSD",
                direction          = direction,
                entry_price        = curr_price,
                stop_loss          = sl,
                take_profit_levels = TakeProfitLevels(tp1=tp, tp2=tp, tp3=tp),
                lot_size           = lot,
                approved           = True,
                risk_amount        = 0.0,
                risk_pct           = 0.0,
            )

            tid = om.execute(order, {"score": 99, "session": "MANUAL", "ai_confidence": 1.0, "ai_reason": "Manual entry"})
            if tid:
                self._reply(chat_id,
                    f"\u2705 *Manual Entry OK*\n"
                    f"ID: `{tid}`\n"
                    f"Dir: *{direction.upper()}* `{lot}`L\n"
                    f"Entry: `{curr_price:.2f}` SL: `{sl:.2f}` TP: `{tp:.2f}`"
                )
            else:
                self._reply(chat_id, "\u274c Manual entry gagal\\. Cek log untuk detail\\.")

        except ValueError as e:
            self._reply(chat_id, f"\u26a0\ufe0f Parameter tidak valid: `{str(e)[:60]}`")
        except Exception as e:
            logger.error(f"Manual entry error: {e}")
            self._reply(chat_id, f"\u26a0\ufe0f Error: `{str(e)[:80]}`")

    def _cmd_positions(self, chat_id: str, args: list) -> None:
        """List all open portfolio positions with interactive close buttons."""
        open_trades = self.portfolio.open_trades
        if not open_trades:
            self._reply(chat_id, "📭 *No open positions currently\\.*")
            return

        # Get live floating PnL from MT5
        mt5_pnl = {}
        if hasattr(self, 'bot_ref') and self.bot_ref and hasattr(self.bot_ref, 'order_manager'):
            try:
                positions = self.bot_ref.order_manager.mt5.get_open_positions()
                mt5_pnl = {str(p.get("ticket", "")): p.get("profit", 0.0) for p in positions}
            except Exception:
                pass

        msg = fmt_positions_list(open_trades, mt5_pnl)
        
        kb_rows = []
        for tid, t in open_trades.items():
            ticket = t.get("mt5_ticket")
            if ticket:
                kb_rows.append([{"text": f"❌ Close #{ticket}", "callback_data": f"close:init:{ticket}"}])

        self._reply(chat_id, msg, {"inline_keyboard": kb_rows} if kb_rows else None)

    def _set_bot_commands(self) -> None:
        """Register commands with Telegram to show them in the autocomplete/menu list."""
        url = _TELEGRAM_SET_CMDS.format(token=self.token)
        commands = [
            {"command": "help",    "description": "Show command list and menu"},
            {"command": "status",  "description": "Bot state, balance, and drawdown"},
            {"command": "today",   "description": "Trades and P&L today"},
            {"command": "balance", "description": "Quick balance check"},
            {"command": "pnl",     "description": "P&L summary this week"},
            {"command": "notify",  "description": "Toggle alerts (ai/risk)"},
            {"command": "trend",   "description": "Toggle H1/H4 master trend alerts"},
            {"command": "pause",   "description": "Pause trading (default 60 min)"},
            {"command": "resume",  "description": "Resume trading immediately"},
            {"command": "trades",  "description": "Trades on a date [DD/MM/YYYY]"},
            {"command": "cutloss", "description": "Panic close all positions"},
            {"command": "reset",   "description": "Cooldown management menu"},
            {"command": "asian",   "description": "Toggle Asian session on/off"},
            {"command": "partial", "description": "Toggle Partial Close feature (50% TP1)"},
            {"command": "mode",    "description": "Switch trade mode (Con/Mod/Agr/V-Agr)"},
            {"command": "mode_info", "description": "Show risk/config for each mode"},
            {"command": "eod",       "description": "Trigger AI Session Retrospective"},
        ]
        try:
            r = self.client.post(url, json={"commands": commands})
            if r.status_code == 200:
                logger.info("Telegram bot commands menu (autocomplete) updated")
            else:
                logger.error(f"Failed to set Telegram commands: {r.text}")
        except Exception as e:
            logger.debug(f"Could not set bot commands: {e}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _reply(self, chat_id: str, text: str, reply_markup: dict = None) -> None:
        url     = _TELEGRAM_SEND.format(token=self.token)
        payload = {
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            r = self.client.post(url, json=payload)
            if r.status_code != 200:
                logger.debug(f"Reply fail (code={r.status_code}): {r.text}")
                # Fallback: plain text
                payload["parse_mode"] = ""
                payload["text"]       = text.replace("\\", "").replace("*","").replace("`","").replace("_","")
                self.client.post(url, json=payload)
        except Exception as e:
            logger.error(f"Telegram reply error: {e}")

    def _answer_callback(self, cq_id: str, text: str = None) -> None:
        """ACK callback query to stop loading spinner in UI."""
        url = f"https://api.telegram.org/bot{self.token}/answerCallbackQuery"
        payload = {"callback_query_id": cq_id}
        if text: payload["text"] = text
        try:
            self.client.post(url, json=payload)
        except: pass

    def _edit_message(self, chat_id: str, msg_id: int, text: str, markup: dict = None) -> None:
        """Change message text (and optionally buttons)."""
        url = f"https://api.telegram.org/bot{self.token}/editMessageText"
        payload = {
            "chat_id":    chat_id,
            "message_id": msg_id,
            "text":       text,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }
        if markup:
            payload["reply_markup"] = markup
        try:
            self.client.post(url, json=payload)
        except Exception as e:
            logger.error(f"Telegram edit error: {e}")

    def _edit_reply_markup(self, chat_id: str, msg_id: int, markup: dict) -> None:
        """Update inline keyboard only."""
        url = f"https://api.telegram.org/bot{self.token}/editMessageReplyMarkup"
        payload = {
            "chat_id":    chat_id,
            "message_id": msg_id,
            "reply_markup": markup
        }
        try:
            self.client.post(url, json=payload)
        except Exception as e:
            logger.error(f"Telegram edit markup error: {e}")

    def _get_trades_for_date(self, date) -> list:
        """
        Pull closed trades for a given date.
        Combines in-memory (latest session) and database (previous sessions).
        """
        seen_ids = set()
        result = []
        
        # 1. From database (Primary source for past session persistence)
        if self.repo:
            db_trades = self.repo.get_trades_by_date(date)
            for t in db_trades:
                result.append(t)
                seen_ids.add(t["trade_id"])

        # 2. From in-memory portfolio (Hot cache for current session)
        for t in self.portfolio.closed_trades:
            if t.trade_id in seen_ids:
                continue
            
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
                seen_ids.add(t.trade_id)
                
        # Sort by closed_at (newest first)
        result.sort(key=lambda x: x["closed_at"], reverse=True)
        return result

    def _on_auto_resume(self, resumed_by: str) -> None:
        """Called by PauseManager when auto-timer fires."""
        self.notifier.send(fmt_bot_resumed(
            resumed_by="Auto-timer",
            resumed_at=datetime.now(timezone.utc),
        ))
