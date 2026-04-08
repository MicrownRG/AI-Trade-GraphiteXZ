"""
Pre-trade filters.  Each filter returns (passed: bool, reason: str).
A trade is executed only if ALL filters pass.
"""
from __future__ import annotations
from typing import Tuple
from datetime import datetime, time as dtime

from config.risk_config import RISK_CONFIG
from config.trading_config import TRADING_CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)

FilterResult = Tuple[bool, str]


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
    if in_london or in_ny:
        return True, "OK"
    return False, f"Outside trading sessions (hour={hour} UTC)"


def filter_daily_loss(current_daily_pnl: float, account_balance: float) -> FilterResult:
    pct = abs(current_daily_pnl) / account_balance * 100 if account_balance > 0 else 0
    limit = RISK_CONFIG.max_daily_loss_pct
    if current_daily_pnl < 0 and pct >= limit:
        return False, f"Daily loss {pct:.2f}% reached limit {limit}%"
    return True, "OK"


def filter_daily_trade_count(trades_today: int) -> FilterResult:
    limit = RISK_CONFIG.max_daily_trades
    if trades_today >= limit:
        return False, f"Daily trade limit {limit} reached"
    return True, "OK"


def filter_concurrent_positions(open_positions: int) -> FilterResult:
    limit = RISK_CONFIG.max_concurrent_trades
    if open_positions >= limit:
        return False, f"Max concurrent positions {limit} reached"
    return True, "OK"


def filter_volatility(atr_pips: float) -> FilterResult:
    if atr_pips < TRADING_CONFIG.min_atr_pips:
        return False, f"ATR {atr_pips:.1f} pips too low (dead market)"
    if atr_pips > TRADING_CONFIG.max_atr_pips:
        return False, f"ATR {atr_pips:.1f} pips too high (extreme volatility)"
    return True, "OK"


def filter_rr_ratio(rr: float) -> FilterResult:
    minimum = RISK_CONFIG.min_rr_ratio
    if rr < minimum:
        return False, f"RR {rr:.2f} below minimum {minimum}"
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
) -> Tuple[bool, list]:
    """
    Run all filters and return (all_passed, list_of_failures).
    """
    checks = [
        filter_spread(spread_pips),
        filter_session(dt),
        filter_daily_loss(daily_pnl, balance),
        filter_daily_trade_count(trades_today),
        filter_concurrent_positions(open_positions),
        filter_volatility(atr_pips),
        filter_rr_ratio(rr),
    ]
    failures = [reason for passed, reason in checks if not passed]
    return len(failures) == 0, failures
