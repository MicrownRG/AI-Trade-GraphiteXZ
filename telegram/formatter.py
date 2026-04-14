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

def _bias_emoji(bias: str) -> str:
    b = str(bias).upper()
    if "BULLISH" in b or b == "BUY":  return "🟢"
    if "BEARISH" in b or b == "SELL": return "🔴"
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
    ai_confidence: Optional[float] = None,
    ai_reason: Optional[str] = None,
    opened_at: datetime = None,
    strategy: str = "",
) -> str:
    sl_pips  = abs(entry_price - stop_loss) / 0.1
    tp_pips  = abs(take_profit - entry_price) / 0.1
    rr       = tp_pips / sl_pips if sl_pips > 0 else 0

    strategy_label = f"⚙️ Strategy  `{_escape(strategy)}`\n" if strategy else ""

    msg = (
        f"🚀 *TRADE ENTERED*\n"
        f"{'─' * 15}\n"
        f"*{_dir_emoji(direction)}*  `{entry_price:.2f}`\n\n"
        f"{strategy_label}"
        f"🎯 TP    `{take_profit:.2f}`  \\(\\+{tp_pips:.0f} pips\\)\n"
        f"🛑 SL    `{stop_loss:.2f}`  \\(\\-{sl_pips:.0f} pips\\)\n"
        f"📐 RR    `1:{rr:.2f}`\n\n"
        f"📦 Lot       `{lot_size}`\n"
        f"💸 Risk      `${risk_amount:.2f}`  \\({risk_pct:.2f}%\\)\n"
        f"📊 Score     `{signal_score}/10`\n"
    )

    if ai_confidence is not None:
        msg += f"🤖 AI Conf   `{ai_confidence:.0%}`\n"
    if ai_reason:
        msg += f"💬 _{_escape(ai_reason[:120])}_\n"

    msg += f"\n🕐 {_wib(opened_at or datetime.now(timezone.utc))}\n"
    msg += f"🆔 `{trade_id}`"
    return msg


def fmt_stalking_signal(
    direction: str,
    score: int,
    max_score: int,
    reason: str,
    price: float,
    sl: float,
    tp: float,
    mode: str,
) -> str:
    """Format a stalking / watching radar notification."""
    rr = abs(tp - price) / abs(sl - price) if abs(sl - price) > 0 else 0
    return (
        f"📡 *RADAR: WATCHING SETUP*\n"
        f"{'─' * 18}\n"
        f"*{_dir_emoji(direction)}*  `{price:.2f}`\n\n"
        f"📊 Score:   `{score}/{max_score}`\n"
        f"🛡 Mode:    `{_escape(mode)}`\n"
        f"📐 Pot. RR: `1:{rr:.2f}`\n\n"
        f"📝 *Status:*\n"
        f"_{_escape(reason)}_\n\n"
        f"🕐 {_wib(datetime.now(timezone.utc))}\n"
        f"⚠️ _Pending entry, waiting for extreme retracement\\._"
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
        f"{'─' * 15}\n"
        f"{_dir_emoji(direction)}  `{entry_price:.2f}` → `{exit_price:.2f}`\n\n"
        f"💰 PnL      `\\+${pnl:.2f}`  \\(`\\+{pnl_pips:.1f}` pips\\)\n"
        f"📦 Lot      `{lot_size}`\n"
        f"⏱ Duration `{duration_minutes} min`\n\n"
        f"🕐 `{_wib(closed_at)}`\n"
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
        f"{'─' * 15}\n"
        f"{_dir_emoji(direction)}  `{entry_price:.2f}` → `{exit_price:.2f}`\n\n"
        f"💸 PnL      `\\-${abs(pnl):.2f}`  \\(`\\-{abs(pnl_pips):.1f}` pips\\)\n"
        f"📦 Lot      `{lot_size}`\n"
        f"⏱ Duration `{duration_minutes} min`\n\n"
        f"🕐 `{_wib(closed_at)}`\n"
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
        f"{'─' * 15}\n"
        f"{_dir_emoji(direction)}  Entry `{entry_price:.2f}`\n"
        f"🛑 SL moved to `{new_sl:.2f}` \\(breakeven\\)\n"
        f"🕐 {_wib_short(closed_at)}\n"
        f"🆔 `{trade_id}`"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Pause notifications
# ═══════════════════════════════════════════════════════════════════════════════
def fmt_bot_paused(reason: str, pause_minutes: int, resume_at: datetime) -> str:
    return (
        f"⏸ *BOT PAUSED*\n"
        f"{'─' * 15}\n"
        f"📋 Reason: _{_escape(reason)}_\n"
        f"⏱ Duration: `{pause_minutes} minutes`\n"
        f"▶️ Auto\\-resume at: `{_wib(resume_at)}`\n\n"
        f"To resume early: /resume"
    )


def fmt_bot_resumed(resumed_by: str, resumed_at: datetime) -> str:
    return (
        f"▶️ *BOT RESUMED*\n"
        f"{'─' * 15}\n"
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
        f"{'─' * 15}\n"
        f"❌ Consecutive losses: `{consecutive_losses}`\n"
        f"💸 Loss today:   `\\-${abs(total_loss_today):.2f}` \\(`{loss_pct:.2f}%`\\)\n\n"
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
        f"{'─' * 15}\n"
        f"{pnl_emoji} PnL:        *{'\\+' if total_pnl >= 0 else '\\-'}${abs(total_pnl):.2f}*\n"
        f"🏦 Balance:   `${ending_balance:,.2f}`\n"
        f"📉 Max DD:    `{max_drawdown_pct:.2f}%`\n\n"
        f"📈 Trades:    `{total_trades}`  \\({wins}W / {losses}L\\)\n"
        f"🎯 Win Rate:  `{win_rate:.1f}%`\n\n"
        f"🥇 Best:    `\\+${best_trade_pnl:.2f}`\n"
        f"💀 Worst:   `\\-${abs(worst_trade_pnl):.2f}`"
    )


def fmt_risk_rejection(
    symbol: str,
    direction: str,
    reason: str,
    signal_id: str,
) -> str:
    return (
        f"⚠️ *TRADE REJECTED \\— RISK*\n"
        f"{'─' * 15}\n"
        f"*{symbol}*  {_dir_emoji(direction)}\n"
        f"🚫 Reason: _{_escape(reason)}_\n\n"
        f"🆔 `{signal_id}`"
    )


def fmt_ai_rejection(
    symbol: str,
    direction: str,
    confidence: float,
    reason: str,
    signal_id: str,
) -> str:
    return (
        f"🤖 *TRADE REJECTED \\— AI*\n"
        f"{'─' * 15}\n"
        f"*{symbol}*  {_dir_emoji(direction)}\n"
        f"🤖 AI Conf:  `{confidence:.0%}`\n"
        f"🚫 Reason:   _{_escape(reason)}_\n\n"
        f"🆔 `{signal_id}`"
    )


def fmt_signal_scan(
    symbol: str,
    direction: str,
    score: int,
    threshold: int,
    mode: str,
    bias: str,
    score_breakdown: dict,
    signal_id: str,
    scanned_at: datetime = None,
) -> str:
    """
    Signal scan summary sent when a valid signal is detected.
    Shows score breakdown so user can understand why bot fired.
    score_breakdown: dict of {component_name: points_awarded}
    """
    dir_em = _dir_emoji(direction)
    bar_filled = "█" * score
    bar_empty  = "░" * max(0, threshold - score)
    bar = f"{bar_filled}{bar_empty}"

    lines = [
        f"🔍 *SIGNAL DETECTED*",
        f"{'─' * 15}",
        f"*{_escape(symbol)}*  {dir_em}",
        f"",
        f"📊 Score  `{score}/{threshold}` \\({_escape(mode)}\\)",
        f"📈 Bias   `{_escape(bias)}`",
        f"📉 Dir    `{_escape(direction.upper())}`",
        f"",
        f"*Score Breakdown:*",
    ]

    if score_breakdown:
        for component, pts in score_breakdown.items():
            icon = "✅" if pts > 0 else "⬜"
            lines.append(f"{icon} {_escape(component)}: `\\+{pts}`")
    else:
        lines.append(f"_No breakdown available_")

    lines += [
        f"",
        f"🕐 {_wib(scanned_at or datetime.now(timezone.utc))}",
        f"🆔 `{signal_id}`",
    ]
    return "\n".join(lines)


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
        reason = t.get("comment", "?").upper()
        sign   = "\\+" if pnl >= 0 else "\\-"

        # Use backticks for numeric formatting to ensure 100% MarkdownV2 safety
        lines.append(
            f"\n*{i}\\. {d_emoji} {t.get('direction','?').upper()}*  `{t.get('entry_price',0):.2f}` → `{t.get('exit_price',0):.2f}`\n"
            f"   {emoji} `{sign}${abs(pnl):.2f}`  \\| {_escape(reason)}  \\| `{t.get('lot_size',0)} lot`\n"
            f"   🕐 `{_wib_short(t.get('closed_at', datetime.now(timezone.utc)))}`"
        )
        total_pnl += pnl

    sign = "\\+" if total_pnl >= 0 else "\\-"
    lines.append(
        f"\n{'─' * 30}\n"
        f"{_pnl_emoji(total_pnl)} *Total:*  `{sign}${abs(total_pnl):.2f}`"
    )
    return "\n".join(lines)


def fmt_status(
    is_running: bool,
    is_paused: bool,
    pause_reason: str,
    resume_at: Optional[datetime],
    balance: float,
    equity: float,
    drawdown_pct: float,
    realized_pnl: float,
    floating_pnl: float,
    total_pnl: float,
    open_trades: int,
    trades_today: int,
    active_provider: str,
    checked_at: datetime,
    current_mode: str = "UNKNOWN",
    ai_enabled: bool = True,
    pulse_suspended: bool = False,
    cutloss_triggered: bool = False,
    realized_loss_pct: float = 0.0,
) -> str:
    if is_paused:
        resume_str = f"\\| resume {_wib(resume_at)}" if resume_at else ""
        state = f"⏸ *PAUSED* {_escape(pause_reason)} {resume_str}"
    elif is_running:
        state = "🟢 *RUNNING*"
    else:
        state = "⚫ *STOPPED*"

    realized_sign = "\\+" if realized_pnl >= 0 else "\\-"
    floating_sign = "\\+" if floating_pnl >= 0 else "\\-"
    total_sign = "\\+" if total_pnl >= 0 else "\\-"

    pulse_status_str = f"\n🔌 Pulse Scalp: `SUSPENDED` \\(3x Loss Guard\\)" if pulse_suspended else ""
    cutloss_str = (
        f"\n🔴 *HARD CUTLOSS ACTIVE* — realized loss `{realized_loss_pct:.1f}%`\\. Trading halted\\."
        if cutloss_triggered else ""
    )

    return (
        f"🤖 *BOT STATUS*\n"
        f"{'─' * 15}\n"
        f"State:       {state}\n"
        f"🎯 Mode:        *{_escape(current_mode)}*\n\n"
        f"💰 Balance:  `${balance:.2f}`\n"
        f"📊 Equity:   `${equity:.2f}`\n"
        f"📉 Drawdown: `{drawdown_pct:.2f}%`\n\n"
        f"✅ *Today Realized PnL*: `{realized_sign}${abs(realized_pnl):.2f}`\n"
        f"📈 *Floating*:           `{floating_sign}${abs(floating_pnl):.2f}`\n"
        f"📊 *Total Today PnL*:    `{total_sign}${abs(total_pnl):.2f}`\n\n"
        f"📂 Open:     `{open_trades}` positions\n"
        f"🔢 Today:    `{trades_today}` trades\n"
        f"🤖 AI:       `{_escape(active_provider)}` \\({'ON' if ai_enabled else 'OFF'}\\){pulse_status_str}{cutloss_str}\n\n"
        f"🕐 {_wib(checked_at)}"
    )


def fmt_help() -> str:
    return (
        "🤖 *TRADING BOT COMMANDS*\n"
        f"{'─' * 15}\n\n"
        "*📊 Data & AI*\n"
        "/today — trades & P\\&L today\n"
        "/trades \\[DD/MM/YYYY\\] — trades on a date\n"
        "/pnl — P\\&L summary this week\n"
        "/status — bot state, balance, drawdown\n"
        "/positions — list all active positions\n"
        "/eod — generate AI Daily Retrospective\n\n"
        "*⚙️ Config & Trade*\n"
        "/mode — change risk level \\(Ultra, Moderate, etc\\)\n"
        "/mode\\_info — details on mode mechanics\n"
        "/asian — toggle Asian session trading\n"
        "/partial — toggle partial closures\n"
        "/trend — toggle trend change notifications\n"
        "/notify — toggle detailed AI/Risk alerts\n"
        "/buy — manual buy override\n"
        "/sell — manual sell override\n\n"
        "*⏸ Control & Emergency*\n"
        # "/cutloss — panic close ALL positions now\n"
        "/pause \\[minutes\\] — pause bot \\(default 60 min\\)\n"
        "/resume — resume immediately\n"
        "/reset — cooldown management menu\n"
        "/pulse — manually reset pulse scalper guard\n"
        "/adapter — toggle AI adaptive learning engine\n\n"
        "*ℹ️ Info*\n"
        "/balance — quick balance check\n"
        "/help — show this menu"
    )

def fmt_trend_alert(
    bias: str,
    trend_strength: str,
    adx: float,
    ema_cross_bullish: bool,
    price: float,
    ema_50: float,
    ema_200: float,
    timeframe: str = "H1",
    checked_at: datetime = None,
    tf_confluence: dict = None,
    is_update: bool = False,
) -> str:
    """Format a trend change notification with full technical breakdown."""
    if checked_at is None:
        checked_at = datetime.now(timezone.utc)
    
    if is_update:
        title = "📡 MARKET SCANNER"
        emoji = "🔭"
    elif bias == "BULLISH":
        title = "🚨 TREND SHIFT: BULLISH"
        emoji = "📈"
    elif bias == "BEARISH":
        title = "🚨 TREND SHIFT: BEARISH"
        emoji = "📉"
    else:
        title = "⚖️ TREND SHIFT: NEUTRAL"
        emoji = "↔️"
    
    strength_emoji = "💪" if trend_strength == "STRONG" else "🤏"
    ema_status = "EMA9 \\> EMA21 \\(Bull Cross\\)" if ema_cross_bullish else "EMA9 \\< EMA21 \\(Bear Cross\\)"
    
    price_vs_200 = "ABOVE" if price > ema_200 else "BELOW"
    price_vs_50 = "ABOVE" if price > ema_50 else "BELOW"

    msg = (
        f"{emoji} *{title}*\n"
        f"{'─' * 15}\n\n"
        f"⏱ Master TF: `{_escape(timeframe)}`\n"
        f"💲 Price:     `${price:.2f}`\n\n"
    )

    if tf_confluence:
        msg += f"*Multi-TF Analysis:*\n"
        # Sort by weight (HTF first)
        for tf in ["h4", "h1", "m30", "m15", "m5", "m1"]:
            if tf in tf_confluence:
                b_obj = tf_confluence[tf]
                b_str = getattr(b_obj, "bias", str(b_obj))
                msg += f"  • {tf.upper():<3}: {_bias_emoji(b_str)} `{_escape(b_str)}`"
                # Check for zones
                if hasattr(b_obj, "at_zone") and b_obj.at_zone != "NONE":
                    msg += f" 🛡️"
                msg += "\n"
        msg += "\n"

    msg += (
        f"*Technical Detail:*\n"
        f"  • ADX: `{adx:.1f}` {strength_emoji} {_escape(trend_strength)}\n"
        f"  • {_escape(ema_status)}\n"
        f"  • Price vs EMA50: `{_escape(price_vs_50)}` \\(`${ema_50:.2f}`\\)\n"
        f"  • Price vs EMA200: `{_escape(price_vs_200)}` \\(`${ema_200:.2f}`\\)\n\n"
        f"🕐 {_wib(checked_at)}"
    )
    return msg


def fmt_balance_quick(balance: float, equity: float, daily_pnl: float) -> str:
    sign  = "\\+" if daily_pnl >= 0 else "\\-"
    emoji = _pnl_emoji(daily_pnl)
    return (
        f"💰 *BALANCE*\n"
        f"Balance: `${balance:.2f}`\n"
        f"Equity:  `${equity:.2f}`\n"
        f"{emoji} Today:   `{sign}${abs(daily_pnl):.2f}`"
    )


def fmt_positions_list(
    open_trades: dict,
    mt5_pnl: dict,
) -> str:
    """Format the list of open positions for the /positions command."""
    if not open_trades:
        return "📭 *No open positions currently\\.*"

    lines = ["💼 *OPEN POSITIONS*", f"{'─' * 20}"]
    total_floating = 0.0

    for tid, t in open_trades.items():
        ticket = t.get("mt5_ticket", "?")
        pnl = mt5_pnl.get(str(ticket), t.get("current_pnl", 0.0))
        total_floating += pnl
        
        emoji = _pnl_emoji(pnl)
        d_emoji = "📈" if t.get("direction") == "buy" else "📉"
        lot = t.get("lot_size", 0)
        entry = t.get("entry_price", 0)
        
        # Identify trade type from strategy field, fallback to signal_id heuristic
        strat = t.get("strategy", "")
        if strat:
            type_label = strat
        elif "PULSE" in tid:
            type_label = "⚡ Pulse Scalper"
        elif "FIBO" in str(t.get("signal_id", "")):
            type_label = "📐 Fibo MTF"
        elif "REV" in str(t.get("signal_id", "")):
            type_label = "🔄 Reversal"
        else:
            type_label = "🛡️ ICT Standard"
        
        sign = "\\+" if pnl >= 0 else "\\-"
        
        lines.append(
            f"\n{emoji} *{t.get('symbol','XAUUSD')}* {d_emoji} `{lot}`L\n"
            f"   `{_escape(type_label)}` \\| Entry: `{entry:.2f}`\n"
            f"   PnL: *{sign}${abs(pnl):.2f}* \\| Ticket: `#{ticket}`"
        )

    # Footer summary
    p_sign = "\\+" if total_floating >= 0 else "\\-"
    lines.append(
        f"\n{'─' * 20}\n"
        f"💰 *Total Floating:*  *{p_sign}${abs(total_floating):.2f}*\n"
        f"🕐 {_wib_short(datetime.now(timezone.utc))}"
    )

    return "\n".join(lines)


def fmt_unknown_command(cmd: str) -> str:
    return f"❓ Unknown command: `{_escape(cmd)}`\n\nSend /help for available commands\\."


def fmt_unauthorized() -> str:
    return "🚫 *Unauthorized\\.*\nYour chat ID is not whitelisted\\."

