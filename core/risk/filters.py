"""
Pre-trade filters. Each filter returns (passed: bool, reason: str).
A trade is executed only if ALL filters pass.

Smart filters (non-blocking context filters):
  - filter_weekly_open    (No.77): block trades against weekly open trend
  - filter_daily_exhaustion (No.98): gold moved >330 pips today -> expect reversal
  - filter_choppiness     (No.23): reject if market is too choppy (low ATR ratio)
"""
from __future__ import annotations
from typing import Tuple, Optional
import pandas as pd
from datetime import datetime, time as dtime, timezone

from config.risk_config import RISK_CONFIG
from config.trading_config import TRADING_CONFIG, TradeMode
from utils.logger import get_logger
from core.risk.safety import SafetyManager

logger = get_logger(__name__)

FilterResult = Tuple[bool, str]

# Daily exhaustion threshold (pips): if gold ran this far in one direction today,
# mean reversion is likely — block same-direction entries (No.98)
_DAILY_EXHAUSTION_PIPS = 330.0


def filter_spread(current_spread_pips: float) -> FilterResult:
    limit = RISK_CONFIG.max_allowed_spread_pips
    if current_spread_pips > limit:
        return False, f"Spread {current_spread_pips:.1f} > max {limit}"
    return True, "OK"


def filter_session(dt: datetime) -> FilterResult:
    hour = dt.hour
    cfg  = TRADING_CONFIG
    in_london = cfg.london_session[0] <= hour < cfg.london_session[1]
    in_ny     = cfg.ny_session[0]     <= hour < cfg.ny_session[1]
    in_asia   = cfg.asian_session[0]  <= hour < cfg.asian_session[1]

    if in_london or in_ny or (in_asia and cfg.enable_asian_session):
        return True, "OK"
    return False, f"Outside trading sessions (hour={hour} UTC)"


def filter_daily_loss(realized_pnl: float, account_balance: float) -> FilterResult:
    """Gate on REALIZED (closed) daily losses only. Floating losses are ignored."""
    if realized_pnl >= 0 or account_balance <= 0:
        return True, "OK"
    pct = abs(realized_pnl) / account_balance * 100
    limit = RISK_CONFIG.hard_cutloss_daily_pct
    if pct >= limit:
        return False, f"Realized daily loss {pct:.2f}% reached hard cutloss limit {limit}%"
    return True, "OK"


def filter_daily_trade_count(trades_today: int) -> FilterResult:
    """
    For Conservative / Moderate modes, cap daily trade count to avoid over-trading.
    Aggressive modes have no daily cap (max_daily_trades = 0).
    """
    mode = TRADING_CONFIG.current_mode
    settings = TRADING_CONFIG.mode_settings.get(mode, {})
    max_trades = settings.get("max_daily_trades", 0)
    if max_trades > 0 and trades_today >= max_trades:
        return False, f"Daily trade cap reached ({trades_today}/{max_trades} for {mode.value})"
    return True, "OK"


def filter_concurrent_positions(open_positions: int) -> FilterResult:
    limit = RISK_CONFIG.max_concurrent_trades
    if open_positions >= limit:
        return False, f"Max concurrent positions {limit} reached"
    return True, "OK"


def filter_volatility(atr_pips: float, session: str = "") -> FilterResult:
    min_limit = TRADING_CONFIG.min_atr_pips

    # Relax minimum ATR requirement in high-volume sessions (London / NY incl. Overlap)
    if session in ("LONDON", "NY"):
        min_limit *= 0.7  # 30% reduction

    if atr_pips < min_limit:
        return False, f"ATR {atr_pips:.1f} pips too low (min {min_limit:.1f})"
    if atr_pips > TRADING_CONFIG.max_atr_pips:
        return False, f"ATR {atr_pips:.1f} pips too high (extreme volatility)"
    return True, "OK"


def filter_rr_ratio(rr: float) -> FilterResult:
    minimum = RISK_CONFIG.min_rr_ratio
    if rr < minimum:
        return False, f"RR {rr:.2f} below minimum {minimum}"
    return True, "OK"


def filter_max_risk(calculated_risk_pct: float) -> FilterResult:
    # Hard safety cap — mode-based caps are enforced earlier in lot_sizing
    limit = 20.0
    if calculated_risk_pct > limit:
        return False, f"Risk {calculated_risk_pct:.1f}% exceeds absolute safety cap {limit}%"
    return True, "OK"


def filter_margin_level(margin_level: float) -> FilterResult:
    if not SafetyManager.check_margin_level(margin_level):
        return False, f"Margin level {margin_level:.1f}% below safety {RISK_CONFIG.min_margin_level_pct}%"
    return True, "OK"


def filter_news(news_active: bool) -> FilterResult:
    """
    Conservative mode: fully block trading during high-impact news events.
    Moderate mode: allow trading but risk is already reduced 50% via lot sizing.
    Aggressive and above: news does not block entry.
    """
    if news_active and TRADING_CONFIG.current_mode == TradeMode.CONSERVATIVE:
        return False, "High-impact news blackout active (Conservative mode)"
    return True, "OK"


# Global variable: set when user manually resets cooldown via Telegram
GLOBAL_COOLDOWN_CLEARED_AT: Optional[datetime] = None


def _mode_revenge_limit_min() -> float:
    """
    Mode-aware revenge cooldown. Aggressive modes recover faster because entry
    frequency is the whole point. Defaults derived from RISK_CONFIG.revenge_cooldown_min
    but scaled per mode.
    """
    base = RISK_CONFIG.revenge_cooldown_min  # 15 default (was 30)
    mode = TRADING_CONFIG.current_mode.value
    mult = {
        "CONSERVATIVE":    2.0,   # 30 min
        "MODERATE":        1.3,   # ~20 min
        "AGGRESSIVE":      0.66,  # 10 min
        "VERY_AGGRESSIVE": 0.20,  # 5 min = 0.33, 3 min = 0.20
        "ULTRA_SCALPER":   0.20,  # 3 min
    }.get(mode, 1.0)
    return max(1.0, base * mult)


def filter_cooldown(last_closed_at: Optional[datetime], last_pnl: float) -> FilterResult:
    if last_closed_at is None or last_pnl >= 0:
        return True, "OK"

    global GLOBAL_COOLDOWN_CLEARED_AT
    _closed = last_closed_at if last_closed_at.tzinfo else last_closed_at.replace(tzinfo=timezone.utc)
    if GLOBAL_COOLDOWN_CLEARED_AT:
        _cleared = GLOBAL_COOLDOWN_CLEARED_AT
        if _cleared.tzinfo is None:
            _cleared = _cleared.replace(tzinfo=timezone.utc)
        if _closed < _cleared:
            return True, "OK"

    elapsed = (datetime.now(timezone.utc) - _closed).total_seconds() / 60.0
    limit = _mode_revenge_limit_min()
    if elapsed < limit:
        return False, f"Anti-revenge cooldown active ({int(limit - elapsed)}m remaining)"
    return True, "OK"


def filter_entry_delay(last_opened_at: Optional[datetime], atr_pips: Optional[float] = None) -> FilterResult:
    if last_opened_at is None:
        return True, "OK"

    # Respect manual Telegram cooldown reset — bypass entry-delay if the last
    # position was opened BEFORE the reset timestamp, or the reset happened
    # within the last 60s (user explicitly wants to trade now).
    global GLOBAL_COOLDOWN_CLEARED_AT
    if GLOBAL_COOLDOWN_CLEARED_AT:
        # Normalise tz — some callers may pass naive datetimes
        _cleared = GLOBAL_COOLDOWN_CLEARED_AT
        if _cleared.tzinfo is None:
            _cleared = _cleared.replace(tzinfo=timezone.utc)
        _opened = last_opened_at
        if _opened.tzinfo is None:
            _opened = _opened.replace(tzinfo=timezone.utc)
        if _opened < _cleared:
            return True, "OK"
        cleared_age = (datetime.now(timezone.utc) - _cleared).total_seconds()
        if cleared_age < 60:
            return True, "OK"

    elapsed = (datetime.now(timezone.utc) - last_opened_at).total_seconds()

    # Adaptive delay by mode — aggressive modes use shorter gaps for frequent entries
    mode = TRADING_CONFIG.current_mode.value
    if mode == "ULTRA_SCALPER":
        base_limit = 45    # 45s — near-instant re-entry
    elif mode == "VERY_AGGRESSIVE":
        base_limit = 75    # 1m15s
    elif mode == "AGGRESSIVE":
        base_limit = 120   # 2 min
    elif mode == "MODERATE":
        base_limit = 180   # 3 min
    else:
        base_limit = 240   # 4 min (Conservative)

    # Extra 60s cooldown if M1 ATR is very high (> 25 pips per candle = whipsaw risk)
    if atr_pips and atr_pips >= 25.0:
        base_limit += 60

    if elapsed < base_limit:
        return False, f"Entry delay: wait {int(base_limit - elapsed)}s more ({mode} adaptive)"
    return True, "OK"


def filter_profit_lock(open_positions: int, floating_pnl: float) -> FilterResult:
    """
    Prevent adding new positions while existing ones are in significant loss.

    Thresholds are mode-aware:
      Conservative / Moderate : block if >= 1 open trade and floating PnL < -$5
      Aggressive               : block if >= 3 open trades and floating PnL < -$20
      Very Aggressive / Ultra  : block if >= 4 open trades and floating PnL < -$50
                                 (risk already capped by equity-tier lot caps)

    Small floating losses (below threshold) are counted in total_risk monitoring
    but do NOT block new entries — consistent with 'always calculate risk even
    for small negatives' requirement.
    """
    mode = TRADING_CONFIG.current_mode

    if mode in (TradeMode.ULTRA_SCALPER, TradeMode.VERY_AGGRESSIVE):
        min_positions = 4
        loss_threshold = -50.0
    elif mode == TradeMode.AGGRESSIVE:
        min_positions = 3
        loss_threshold = -20.0
    else:  # CONSERVATIVE / MODERATE
        min_positions = 1
        loss_threshold = -5.0

    if open_positions >= min_positions and floating_pnl < loss_threshold:
        return False, (
            f"Profit-lock: {open_positions} open positions with "
            f"${floating_pnl:.2f} floating loss (threshold ${loss_threshold:.0f})"
        )
    return True, "OK"


# ─────────────────────────────────────────────────────────────────────────────
# Smart Context Filters (No.77, No.98, No.23)
# ─────────────────────────────────────────────────────────────────────────────

def filter_weekly_open(
    direction: str,
    weekly_open: Optional[float],
    current_price: float,
) -> FilterResult:
    """
    No.77 — Weekly Open Trend Filter.
    Price below weekly open -> block buy.
    Price above weekly open -> block sell.
    Only applied to Conservative & Moderate; aggressive modes bypass this filter.
    """
    mode = TRADING_CONFIG.current_mode
    if mode in (TradeMode.ULTRA_SCALPER, TradeMode.VERY_AGGRESSIVE, TradeMode.AGGRESSIVE):
        return True, "OK"  # Aggressive modes skip weekly filter

    if weekly_open is None or weekly_open <= 0:
        return True, "OK"  # No data available

    if direction == "buy" and current_price < weekly_open:
        diff = (weekly_open - current_price) / 0.1
        if diff > 50:  # Only reject if meaningfully below (> 50 pips)
            return False, (
                f"Weekly open filter (No.77): price {current_price:.2f} is "
                f"{diff:.0f} pips below weekly open {weekly_open:.2f}"
            )
    elif direction == "sell" and current_price > weekly_open:
        diff = (current_price - weekly_open) / 0.1
        if diff > 50:
            return False, (
                f"Weekly open filter (No.77): price {current_price:.2f} is "
                f"{diff:.0f} pips above weekly open {weekly_open:.2f}"
            )

    return True, "OK"


def filter_daily_exhaustion(
    direction: str,
    daily_high: Optional[float],
    daily_low: Optional[float],
    current_price: float,
) -> FilterResult:
    """
    No.98 — Daily Limit Capacity Exhaustion.
    If gold has moved > 330 pips in one direction today, mean reversion is
    likely — block same-direction entries and favour counter-trend.
    """
    if daily_high is None or daily_low is None:
        return True, "OK"

    daily_range_pips = (daily_high - daily_low) / 0.1

    if daily_range_pips < _DAILY_EXHAUSTION_PIPS:
        return True, "OK"

    range_size = daily_high - daily_low
    if range_size <= 0:
        return True, "OK"

    price_pct_in_range = (current_price - daily_low) / range_size

    if direction == "buy" and price_pct_in_range > 0.75:
        return False, (
            f"Daily exhaustion (No.98): gold ran {daily_range_pips:.0f} pips today, "
            f"price at {price_pct_in_range*100:.0f}% of range — mean reversion expected"
        )
    elif direction == "sell" and price_pct_in_range < 0.25:
        return False, (
            f"Daily exhaustion (No.98): gold ran {daily_range_pips:.0f} pips today, "
            f"price at {price_pct_in_range*100:.0f}% of range — mean reversion expected"
        )

    return True, "OK"


def filter_choppiness(
    daily_range_pips: float,
    atr_pips: float,
) -> FilterResult:
    """
    No.23 — Choppiness Index Filter (simplified).
    Market is choppy when daily range / ATR < 1.5 (moves less than 1.5x ATR).
    Only applied to Conservative & Moderate — aggressive modes can trade ranging markets.
    """
    mode = TRADING_CONFIG.current_mode
    if mode in (TradeMode.ULTRA_SCALPER, TradeMode.VERY_AGGRESSIVE, TradeMode.AGGRESSIVE):
        return True, "OK"  # Aggressive modes bypass — Pulse handles sideways

    if atr_pips <= 0:
        return True, "OK"

    choppiness_ratio = daily_range_pips / atr_pips if atr_pips > 0 else 5.0

    if choppiness_ratio < 1.5:
        return False, (
            f"Choppiness filter (No.23): market too choppy "
            f"(daily range/ATR = {choppiness_ratio:.1f} < 1.5)"
        )

    return True, "OK"


def run_all_filters(
    *,
    spread_pips: float,
    dt: datetime,
    daily_pnl: float,
    balance: float,
    trades_today: int,
    open_positions: int,
    atr_pips: float,
    rr: float,
    margin_level: float = 1000.0,
    news_active: bool = False,
    session: str = "",
    calculated_risk_pct: float = 0.0,
    last_trade_closed_at: Optional[datetime] = None,
    last_trade_result_pnl: float = 0.0,
    last_trade_opened_at: Optional[datetime] = None,
    current_floating_pnl: float = 0.0,
    signal_type: str = "REGULAR",
    cutloss_triggered: bool = False,
    # Smart context params
    direction: str = "",
    weekly_open: Optional[float] = None,
    current_price: float = 0.0,
    daily_high: Optional[float] = None,
    daily_low: Optional[float] = None,
) -> tuple[bool, list]:
    """
    Run all pre-trade filters and return (all_passed, list_of_failure_reasons).
    daily_pnl is kept for legacy callers; realized-loss check uses it as realized PnL.
    """
    # Hard cutloss gate — checked first, blocks all new entries for the rest of the day
    if cutloss_triggered:
        return False, ["Hard cutloss triggered — trading halted for today"]

    checks = [
        filter_spread(spread_pips),
        filter_session(dt),
        filter_daily_loss(daily_pnl, balance),
        filter_daily_trade_count(trades_today),
        filter_volatility(atr_pips, session),
        filter_rr_ratio(rr),
        filter_max_risk(calculated_risk_pct),
        filter_margin_level(margin_level),
        filter_news(news_active),
    ]

    # Pulse and contra-flip bypass rate-limit filters.
    # Contra-flip follows the new HTF bias after a structural exit — not revenge trading.
    if signal_type not in ("PULSE", "CONTRA_FLIP"):
        checks.extend([
            filter_concurrent_positions(open_positions),
            filter_cooldown(last_trade_closed_at, last_trade_result_pnl),
            filter_entry_delay(last_trade_opened_at, atr_pips=atr_pips),
            filter_profit_lock(open_positions, current_floating_pnl),
        ])

        # Smart context filters — regular signals only
        if direction and current_price > 0:
            checks.append(filter_weekly_open(direction, weekly_open, current_price))
            checks.append(filter_daily_exhaustion(direction, daily_high, daily_low, current_price))

            if daily_high is not None and daily_low is not None and atr_pips > 0:
                daily_range_pips = (daily_high - daily_low) / 0.1
                checks.append(filter_choppiness(daily_range_pips, atr_pips))

    failures = [reason for passed, reason in checks if not passed]
    if failures:
        logger.debug(f"Filter failures: {failures}")
    return len(failures) == 0, failures
