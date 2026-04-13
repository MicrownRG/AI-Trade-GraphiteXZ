"""
Pre-trade filters.  Each filter returns (passed: bool, reason: str).
A trade is executed only if ALL filters pass.

Smart filters (non-blocking, execution-supporting):
  - filter_weekly_open    (No.77): larang trade berlawanan weekly trend
  - filter_daily_exhaustion (No.98): Gold lari >330 pip searah → expect reversal
  - filter_choppiness     (No.23): tolak jika market terlalu choppy (rendahnya ATR ratio)
"""
from __future__ import annotations
from typing import Tuple, Optional
import pandas as pd
from datetime import datetime, time as dtime, timezone

from config.risk_config import RISK_CONFIG
from config.trading_config import TRADING_CONFIG
from utils.logger import get_logger
from core.risk.safety import SafetyManager

logger = get_logger(__name__)

FilterResult = Tuple[bool, str]

# Daily exhaustion threshold (pips): if gold ran this far in one direction today,
# consider mean reversion likely (No.98)
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


def filter_daily_loss(current_daily_pnl: float, account_balance: float) -> FilterResult:
    pct = abs(current_daily_pnl) / account_balance * 100 if account_balance > 0 else 0
    limit = RISK_CONFIG.max_daily_loss_pct
    if current_daily_pnl < 0 and pct >= limit:
        return False, f"Daily loss {pct:.2f}% reached limit {limit}%"
    return True, "OK"


def filter_daily_trade_count(trades_today: int) -> FilterResult:
    # Unlimited daily trades — no cap enforced
    return True, "OK"


def filter_concurrent_positions(open_positions: int) -> FilterResult:
    limit = RISK_CONFIG.max_concurrent_trades
    if open_positions >= limit:
        return False, f"Max concurrent positions {limit} reached"
    return True, "OK"


def filter_volatility(atr_pips: float, session: str = "") -> FilterResult:
    min_limit = TRADING_CONFIG.min_atr_pips
    
    # Relax min ATR for high-volume sessions (Overlap/London)
    if session in ("Overlap", "London"):
        min_limit *= 0.7  # 30% reduction in requirements
        
    if atr_pips < min_limit:
        return False, f"ATR {atr_pips:.1f} pips too low (limit {min_limit:.1f})"
    if atr_pips > TRADING_CONFIG.max_atr_pips:
        return False, f"ATR {atr_pips:.1f} pips too high (extreme volatility)"
    return True, "OK"


def filter_rr_ratio(rr: float) -> FilterResult:
    minimum = RISK_CONFIG.min_rr_ratio
    if rr < minimum:
        return False, f"RR {rr:.2f} below minimum {minimum}"
    return True, "OK"


def filter_max_risk(calculated_risk_pct: float) -> FilterResult:
    # Mode-based max risk is already handled in lot_sizing, 
    # but we keep a hard safety cap at 20% for Extreme modes.
    limit = 20.0 
    if calculated_risk_pct > limit:
        return False, f"Risk {calculated_risk_pct:.1f}% exceeds absolute safety cap {limit}%"
    return True, "OK"


def filter_margin_level(margin_level: float) -> FilterResult:
    if not SafetyManager.check_margin_level(margin_level):
        return False, f"Margin level {margin_level:.1f}% below safety {RISK_CONFIG.min_margin_level_pct}%"
    return True, "OK"


def filter_news(news_active: bool) -> FilterResult:
    if news_active and TRADING_CONFIG.current_mode == "CONSERVATIVE":
         return False, "High-impact news blackout active"
    return True, "OK"


# Global variable to bypass cooldowns when user resets via Telegram
GLOBAL_COOLDOWN_CLEARED_AT: Optional[datetime] = None

def filter_cooldown(last_closed_at: Optional[datetime], last_pnl: float) -> FilterResult:
    if last_closed_at is None or last_pnl >= 0:
        return True, "OK"
        
    global GLOBAL_COOLDOWN_CLEARED_AT
    if GLOBAL_COOLDOWN_CLEARED_AT and last_closed_at < GLOBAL_COOLDOWN_CLEARED_AT:
        return True, "OK"
    
    elapsed = (datetime.now(timezone.utc) - last_closed_at).total_seconds() / 60.0
    limit = RISK_CONFIG.revenge_cooldown_min
    if elapsed < limit:
        return False, f"Anti-Revenge: Cooldown active ({int(limit - elapsed)}m remaining)"
    return True, "OK"


def filter_entry_delay(last_opened_at: Optional[datetime], atr_pips: Optional[float] = None) -> FilterResult:
    if last_opened_at is None:
        return True, "OK"
    
    elapsed = (datetime.now(timezone.utc) - last_opened_at).total_seconds()
    
    # ── ADAPTIVE TIME LOGIC ──
    mode = TRADING_CONFIG.current_mode.value
    if mode in ("ULTRA_SCALPER", "VERY_AGGRESSIVE"):
        base_limit = 120  # 2 mins
    elif mode == "AGGRESSIVE":
        base_limit = 180  # 3 mins
    elif mode == "MODERATE":
        base_limit = 240  # 4 mins
    else:
        base_limit = 300  # 5 mins
        
    # Volatility Check: Mengerem jika candle terlalu liar
    # (M1 ATR > 25 pips berarti 1 candle gerak $2.5, sangat volatil!)
    if atr_pips and atr_pips >= 25.0:
        base_limit += 60  # Tambah ekstra 1 menit untuk cooling down whipsaw
        
    if elapsed < base_limit:
        return False, f"Multi-Entry: Jeda {int(base_limit - elapsed)}s lagi (Adaptive {mode})"
    return True, "OK"


def filter_profit_lock(open_positions: int, floating_pnl: float) -> FilterResult:
    # Only block adding MORE positions (>= 2) while existing ones are in loss
    if open_positions >= 2 and floating_pnl < -10.0:
        return False, f"Profit-Lock: {open_positions} posisi sedang Loss (${floating_pnl:.2f})"
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
    Jika harga di bawah weekly open → larang buy.
    Jika harga di atas weekly open → larang sell.
    Hanya berlaku untuk Conservative & Moderate (agresif boleh lawan).
    """
    mode = TRADING_CONFIG.current_mode
    from config.trading_config import TradeMode
    if mode in (TradeMode.ULTRA_SCALPER, TradeMode.VERY_AGGRESSIVE):
        return True, "OK"  # Aggressive modes skip this filter

    if weekly_open is None or weekly_open <= 0:
        return True, "OK"  # No data, skip

    if direction == "buy" and current_price < weekly_open:
        diff = (weekly_open - current_price) / 0.1
        if diff > 50:  # Only reject if meaningfully below (> 50 pips)
            return False, f"Weekly Open Filter: Price {current_price:.2f} below weekly open {weekly_open:.2f} ({diff:.0f} pip)"
    elif direction == "sell" and current_price > weekly_open:
        diff = (current_price - weekly_open) / 0.1
        if diff > 50:
            return False, f"Weekly Open Filter: Price {current_price:.2f} above weekly open {weekly_open:.2f} ({diff:.0f} pip)"

    return True, "OK"


def filter_daily_exhaustion(
    direction: str,
    daily_high: Optional[float],
    daily_low: Optional[float],
    current_price: float,
) -> FilterResult:
    """
    No.98 — Daily Limit Capacity Exhaustion.
    Jika Gold sudah lari > 330 pips searah dalam satu hari,
    kemungkinan besar akan mean revert → LARANG entry searah tren, dorong ke reversal.
    """
    if daily_high is None or daily_low is None:
        return True, "OK"

    daily_range_pips = (daily_high - daily_low) / 0.1

    if daily_range_pips < _DAILY_EXHAUSTION_PIPS:
        return True, "OK"  # Range hasn't exhausted yet

    # Determine dominant daily direction
    # If price is near the high → daily went up → warn against buy
    range_size = daily_high - daily_low
    if range_size <= 0:
        return True, "OK"

    price_pct_in_range = (current_price - daily_low) / range_size

    if direction == "buy" and price_pct_in_range > 0.75:
        return False, (
            f"Daily Exhaustion (No.98): Gold ran {daily_range_pips:.0f} pips today, "
            f"price at {price_pct_in_range*100:.0f}% of daily range — expect mean reversion"
        )
    elif direction == "sell" and price_pct_in_range < 0.25:
        return False, (
            f"Daily Exhaustion (No.98): Gold ran {daily_range_pips:.0f} pips today, "
            f"price at {price_pct_in_range*100:.0f}% of daily range — expect mean reversion"
        )

    return True, "OK"


def filter_choppiness(
    daily_range_pips: float,
    atr_pips: float,
) -> FilterResult:
    """
    No.23 — Choppiness Index Filter (simplified).
    Market choppy jika daily range / ATR ratio < 1.5 (bergerak kurang dari 1.5x ATR).
    Hanya untuk Conservative & Moderate agar tidak over-filter Aggressive modes.
    """
    mode = TRADING_CONFIG.current_mode
    from config.trading_config import TradeMode
    if mode in (TradeMode.ULTRA_SCALPER, TradeMode.VERY_AGGRESSIVE, TradeMode.AGGRESSIVE):
        return True, "OK"

    if atr_pips <= 0:
        return True, "OK"

    choppiness_ratio = daily_range_pips / atr_pips if atr_pips > 0 else 5.0

    if choppiness_ratio < 1.5:
        return False, f"Choppiness Filter (No.23): Market choppy (daily range/ATR = {choppiness_ratio:.1f} < 1.5)"

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
    # Smart context params (Phase 2)
    direction: str = "",
    weekly_open: Optional[float] = None,
    current_price: float = 0.0,
    daily_high: Optional[float] = None,
    daily_low: Optional[float] = None,
) -> tuple[bool, list]:
    """
    Run all filters and return (all_passed, list_of_failures).
    """
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

    # Pulse scalper intentionally bypasses these specific limiters for rapid re-entry
    if signal_type != "PULSE":
        checks.extend([
            filter_concurrent_positions(open_positions),
            filter_cooldown(last_trade_closed_at, last_trade_result_pnl),
            filter_entry_delay(last_trade_opened_at, atr_pips=atr_pips),
            filter_profit_lock(open_positions, current_floating_pnl),
        ])

        # Smart Context Filters (Phase 2 additions) — REGULAR signals only
        if direction and current_price > 0:
            checks.append(filter_weekly_open(direction, weekly_open, current_price))
            checks.append(filter_daily_exhaustion(direction, daily_high, daily_low, current_price))

            # Choppiness: compute daily range pips from high/low if available
            if daily_high is not None and daily_low is not None and atr_pips > 0:
                daily_range_pips = (daily_high - daily_low) / 0.1
                checks.append(filter_choppiness(daily_range_pips, atr_pips))

    failures = [reason for passed, reason in checks if not passed]
    if failures:
        logger.debug(f"Filter failures: {failures}")
    return len(failures) == 0, failures

