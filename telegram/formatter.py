"""
Telegram Message Formatter.

Pure formatting functions — no AI, no API calls.
Converts trade/system data into human-readable Telegram messages (Markdown V2).

All money values in USD, prices in XAUUSD format (2 decimal places).
Time displayed in WIB (UTC+7) for Indonesian users.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import List, Optional

# ── Timezone ──────────────────────────────────────────────────────────────────
WIB = timezone(timedelta(hours=7))

def _wib(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(WIB).strftime("%d/%m/%Y %H:%M WIB")

def _wib_short(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(WIB).strftime("%H:%M WIB")

def _pnl_emoji(pnl: float) -> str:
    if pnl > 0:   return "🟢"
    if pnl < 0:   return "🔴"
    return "⚪"

def _dir_emoji(direction: str) -> str:
    return "📈 BUY" if direction == "buy" else "📉 SELL"

def _escape(text: str) -> str:
    """Escape special chars for Telegram MarkdownV2."""
    specials = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in specials else c for c in str(text))


# ═══════════════════════════════════════════════════════════════════════════════
# Trade lifecycle notifications
# ═══════════════════════════════════════════════════════════════════════════════

def fmt_trade_entry(
    trade_id: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    lot_size: float,
    risk_amount: float,
    risk_pct: float,
    signal_score: int,
    session: str,
    ai_confidence: float,
    ai_reason: str,
    opened_at: datetime,
) -> str:
    sl_pips  = abs(entry_price - stop_loss) / 0.1
    tp_pips  = abs(take_profit - entry_price) / 0.1
    rr       = tp_pips / sl_pips if sl_pips > 0 else 0

    return (
        f"🚀 *TRADE ENTERED*\n"
        f"{'─' * 30}\n"
        f"*{_dir_emoji(direction)}*  `{entry_price:.2f}`\n\n"
        f"🎯 TP    `{take_profit:.2f}`  \\(\\+{tp_pips:.0f} pips\\)\n"
        f"🛑 SL    `{stop_loss:.2f}`  \\(\\-{sl_pips:.0f} pips\\)\n"
        f"📐 RR    `1:{rr:.2f}`\n\n"
        f"📦 Lot       `{lot_size}`\n"
        f"💸 Risk      `${risk_amount:.2f}`  \\({risk_pct:.2f}%\\)\n"
        f"📊 Score     `{signal_score}/10`\n"
        f"🤖 AI Conf   `{ai_confidence:.0%}`\n"
        f"💬 _{_escape(ai_reason[:120])}_\n\n"
        f"🕐 {_wib(opened_at)}\n"
        f"🆔 `{trade_id}`"
    )


def fmt_trade_tp(
    trade_id: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    lot_size: float,
    pnl: float,
    pnl_pips: float,
    tp_level: str,        # "TP1" | "TP2" | "TP3"
    duration_minutes: int,
    closed_at: datetime,
) -> str:
    return (
        f"✅ *TAKE PROFIT HIT \\— {_escape(tp_level)}*\n"
        f"{'─' * 30}\n"
        f"{_dir_emoji(direction)}  `{entry_price:.2f}` → `{exit_price:.2f}`\n\n"
        f"💰 PnL      *\\+${pnl:.2f}*  \\(\\+{pnl_pips:.1f} pips\\)\n"
        f"📦 Lot      `{lot_size}`\n"
        f"⏱ Duration `{duration_minutes} min`\n\n"
        f"🕐 {_wib(closed_at)}\n"
        f"🆔 `{trade_id}`"
    )


def fmt_trade_sl(
    trade_id: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    lot_size: float,
    pnl: float,
    pnl_pips: float,
    duration_minutes: int,
    closed_at: datetime,
) -> str:
    return (
        f"❌ *STOP LOSS HIT*\n"
        f"{'─' * 30}\n"
        f"{_dir_emoji(direction)}  `{entry_price:.2f}` → `{exit_price:.2f}`\n\n"
        f"💸 PnL      *\\-${abs(pnl):.2f}*  \\(\\-{abs(pnl_pips):.1f} pips\\)\n"
        f"📦 Lot      `{lot_size}`\n"
        f"⏱ Duration `{duration_minutes} min`\n\n"
        f"🕐 {_wib(closed_at)}\n"
        f"🆔 `{trade_id}`"
    )


def fmt_trade_breakeven(
    trade_id: str,
    direction: str,
    entry_price: float,
    new_sl: float,
    closed_at: datetime,
) -> str:
    return (
        f"⚖️ *BREAKEVEN ACTIVATED*\n"
        f"{'─' * 30}\n"
        f"{_dir_emoji(direction)}  Entry `{entry_price:.2f}`\n"
        f"🛑 SL moved to `{new_sl:.2f}` \\(breakeven\\)\n"
        f"🕐 {_wib_short(closed_at)}\n"
        f"🆔 `{trade_id}`"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Kill switch & pause notifications
# ═══════════════════════════════════════════════════════════════════════════════

def fmt_kill_switch_triggered(reason: str, triggered_at: datetime) -> str:
    return (
        f"🔴 *KILL SWITCH ACTIVATED*\n"
        f"{'─' * 30}\n"
        f"⚠️ Trading has been *halted*\\.\n\n"
        f"📋 Reason:\n_{_escape(reason)}_\n\n"
        f"🕐 {_wib(triggered_at)}\n\n"
        f"To resume, send: /resume"
    )


def fmt_bot_paused(reason: str, pause_minutes: int, resume_at: datetime) -> str:
    return (
        f"⏸ *BOT PAUSED*\n"
        f"{'─' * 30}\n"
        f"📋 Reason: _{_escape(reason)}_\n"
        f"⏱ Duration: `{pause_minutes} minutes`\n"
        f"▶️ Auto\\-resume at: `{_wib(resume_at)}`\n\n"
        f"To resume early: /resume"
    )


def fmt_bot_resumed(resumed_by: str, resumed_at: datetime) -> str:
    return (
        f"▶️ *BOT RESUMED*\n"
        f"{'─' * 30}\n"
        f"Trading is now *active*\\.\n"
        f"Resumed by: `{_escape(resumed_by)}`\n"
        f"🕐 {_wib(resumed_at)}"
    )


def fmt_loss_alert(
    consecutive_losses: int,
    total_loss_today: float,
    loss_pct: float,
    pause_minutes: int,
    resume_at: datetime,
) -> str:
    return (
        f"⚠️ *LOSS ALERT — AUTO PAUSE*\n"
        f"{'─' * 30}\n"
        f"❌ Consecutive losses: `{consecutive_losses}`\n"
        f"💸 Loss today:   `\\-${abs(total_loss_today):.2f}` \\({loss_pct:.2f}%\\)\n\n"
        f"🤖 Bot paused for `{pause_minutes} min`\n"
        f"▶️ Auto\\-resume: `{_wib(resume_at)}`\n\n"
        f"Commands:\n"
        f"• /resume — resume immediately\n"
        f"• /pause 60 — extend pause\n"
        f"• /status — view current state"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Daily summary
# ═══════════════════════════════════════════════════════════════════════════════

def fmt_daily_summary(
    date_str: str,
    total_trades: int,
    wins: int,
    losses: int,
    total_pnl: float,
    best_trade_pnl: float,
    worst_trade_pnl: float,
    ending_balance: float,
    max_drawdown_pct: float,
) -> str:
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    pnl_emoji = _pnl_emoji(total_pnl)

    return (
        f"📊 *DAILY SUMMARY — {_escape(date_str)}*\n"
        f"{'─' * 30}\n"
        f"{pnl_emoji} PnL:        *{'\\+' if total_pnl >= 0 else '\\-'}${abs(total_pnl):.2f}*\n"
        f"🏦 Balance:   `${ending_balance:.2f}`\n"
        f"📉 Max DD:    `{max_drawdown_pct:.2f}%`\n\n"
        f"📈 Trades:    `{total_trades}`  \\({wins}W / {losses}L\\)\n"
        f"🎯 Win Rate:  `{win_rate:.1f}%`\n\n"
        f"🥇 Best:    `\\+${best_trade_pnl:.2f}`\n"
        f"💀 Worst:   `\\-${abs(worst_trade_pnl):.2f}`"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Command responses
# ═══════════════════════════════════════════════════════════════════════════════

def fmt_trades_list(
    trades: list,
    date_label: str,
) -> str:
    if not trades:
        return f"📭 *No trades on {_escape(date_label)}*"

    lines = [f"📋 *TRADES — {_escape(date_label)}*\n{'─' * 30}"]
    total_pnl = 0.0

    for i, t in enumerate(trades, 1):
        pnl    = t.get("pnl", 0.0)
        emoji  = _pnl_emoji(pnl)
        d_emoji = "📈" if t.get("direction") == "buy" else "📉"
        reason = t.get("close_reason", "?").upper()
        sign   = "\\+" if pnl >= 0 else "\\-"

        lines.append(
            f"\n*{i}\\. {d_emoji} {t.get('direction','?').upper()}*  `{t.get('entry_price',0):.2f}` → `{t.get('exit_price',0):.2f}`\n"
            f"   {emoji} `{sign}${abs(pnl):.2f}`  \\| {_escape(reason)}  \\| `{t.get('lot_size',0)} lot`\n"
            f"   🕐 {_wib_short(t['opened_at']) if 'opened_at' in t else '?'}"
        )
        total_pnl += pnl

    sign = "\\+" if total_pnl >= 0 else "\\-"
    lines.append(
        f"\n{'─' * 30}\n"
        f"{_pnl_emoji(total_pnl)} *Total: {sign}${abs(total_pnl):.2f}*"
    )
    return "\n".join(lines)


def fmt_status(
    is_running: bool,
    is_paused: bool,
    pause_reason: str,
    resume_at: Optional[datetime],
    kill_switch_active: bool,
    kill_switch_reason: str,
    balance: float,
    equity: float,
    drawdown_pct: float,
    daily_pnl: float,
    open_trades: int,
    trades_today: int,
    active_provider: str,
    checked_at: datetime,
) -> str:
    if kill_switch_active:
        state = f"🔴 *HALTED* — Kill Switch\n   _{_escape(kill_switch_reason)}_"
    elif is_paused:
        resume_str = f"\\| resume {_wib(resume_at)}" if resume_at else ""
        state = f"⏸ *PAUSED* {_escape(pause_reason)} {resume_str}"
    elif is_running:
        state = "🟢 *RUNNING*"
    else:
        state = "⚫ *STOPPED*"

    daily_sign = "\\+" if daily_pnl >= 0 else "\\-"

    return (
        f"🤖 *BOT STATUS*\n"
        f"{'─' * 30}\n"
        f"State:       {state}\n\n"
        f"💰 Balance:  `${balance:.2f}`\n"
        f"📊 Equity:   `${equity:.2f}`\n"
        f"📉 Drawdown: `{drawdown_pct:.2f}%`\n"
        f"📅 Today PnL: `{daily_sign}${abs(daily_pnl):.2f}`\n\n"
        f"📂 Open:     `{open_trades}` positions\n"
        f"🔢 Today:    `{trades_today}` trades\n"
        f"🤖 AI:       `{_escape(active_provider)}`\n\n"
        f"🕐 {_wib(checked_at)}"
    )


def fmt_help() -> str:
    return (
        "🤖 *TRADING BOT COMMANDS*\n"
        "{'─' * 30}\n\n"
        "*📊 Data*\n"
        "/today — trades & P\\&L today\n"
        "/trades \\[DD/MM/YYYY\\] — trades on a date\n"
        "/status — bot state, balance, drawdown\n\n"
        "*⏸ Control*\n"
        "/pause \\[minutes\\] — pause bot \\(default 60 min\\)\n"
        "/resume — resume immediately\n"
        "/kill — activate kill switch\n"
        "/reset — reset kill switch \\(admin only\\)\n\n"
        "*ℹ️ Info*\n"
        "/help — show this menu\n"
        "/balance — quick balance check\n"
        "/pnl — P\\&L summary this week"
    )


def fmt_balance_quick(balance: float, equity: float, daily_pnl: float) -> str:
    sign  = "\\+" if daily_pnl >= 0 else "\\-"
    emoji = _pnl_emoji(daily_pnl)
    return (
        f"💰 *BALANCE*\n"
        f"Balance: `${balance:.2f}`\n"
        f"Equity:  `${equity:.2f}`\n"
        f"{emoji} Today:   `{sign}${abs(daily_pnl):.2f}`"
    )


def fmt_unknown_command(cmd: str) -> str:
    return f"❓ Unknown command: `{_escape(cmd)}`\n\nSend /help for available commands\\."


def fmt_unauthorized() -> str:
    return "🚫 *Unauthorized\\.*\nYour chat ID is not whitelisted\\."
