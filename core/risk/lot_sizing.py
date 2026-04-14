"""
Dynamic lot sizing based on risk percentage of account balance.

Formula:
  risk_amount = balance * (risk_pct / 100)
  lot_size    = risk_amount / (sl_pips * pip_value_per_lot)

Volatile session reduction (No.31):
  During US / London-NY overlap sessions or when Synthetic VIX is elevated,
  the maximum total and per-entry lot caps are scaled down to limit exposure
  and make recovery easier if trades turn against us.
"""
from __future__ import annotations
import math
from config.risk_config import RISK_CONFIG
from config.trading_config import TRADING_CONFIG
from utils.logger import get_logger
from core.risk.safety import SafetyManager

logger = get_logger(__name__)

# Gold (XAUUSD): approximately $10 per pip for 1.0 standard lot
PIP_VALUE_PER_STD_LOT = 10.0   # USD per pip for 1.0 lot XAUUSD


def get_equity_lot_caps(
    equity: float,
    strategy: str = "",
    session_name: str = "",
) -> tuple[float, float]:
    """
    Returns (total_portfolio_limit, single_entry_limit) based on equity tier.

    Volatile session reduction (No.31):
      During US session or London-NY Overlap, total cap is reduced by 40% and
      single-entry cap by 30% to limit drawdown in unpredictable conditions
      and make recovery easier.  This applies regardless of trade mode so that
      even aggressive modes are protected against session-driven spikes.
    """

    # Base tier caps — ultra-safe granular equity tiers
    if   equity < 300:    base_limit = (0.02, 0.01)
    elif equity < 500:    base_limit = (0.03, 0.01)
    elif equity < 700:    base_limit = (0.04, 0.01)
    elif equity < 1000:   base_limit = (0.05, 0.01)
    elif equity < 1500:   base_limit = (0.06, 0.02)
    elif equity < 2500:   base_limit = (0.10, 0.03)
    elif equity < 4000:   base_limit = (0.20, 0.05)
    elif equity < 5000:   base_limit = (0.30, 0.06)
    elif equity < 6000:   base_limit = (0.35, 0.07)
    elif equity < 8000:   base_limit = (0.50, 0.10)
    elif equity < 10000:  base_limit = (0.65, 0.12)
    else:                 base_limit = (1.00, 0.20)

    # No.31 — Volatile session cap reduction
    # During US / Overlap: reduce total cap 40%, single-entry cap 30%
    # During London (not overlap): reduce total cap 20%, single-entry 15%
    # Purpose: smaller lot during volatile hours -> smaller loss -> easier recovery
    session_upper = session_name.upper() if session_name else ""
    if session_upper in ("OVERLAP", "NY"):
        total_cap  = round(base_limit[0] * 0.60, 2)  # 40% reduction
        single_cap = round(base_limit[1] * 0.70, 2)  # 30% reduction
        base_limit = (max(total_cap, 0.01), max(single_cap, 0.01))
        logger.debug(
            f"No.31 Volatile session cap ({session_upper}): "
            f"total={base_limit[0]}, single={base_limit[1]}"
        )
    elif session_upper == "LONDON":
        total_cap  = round(base_limit[0] * 0.80, 2)  # 20% reduction
        single_cap = round(base_limit[1] * 0.85, 2)  # 15% reduction
        base_limit = (max(total_cap, 0.01), max(single_cap, 0.01))

    if not strategy:
        return base_limit

    # Strategy-specific adjustments
    strat_key = strategy.split("_")[0].split("-")[0].lower()

    match strat_key:
        case "pulse":
            # Pulse uses 50% of the already-reduced cap for extra protection
            total  = base_limit[0] * 0.5
            single = min(base_limit[1], 0.01)
            return (round(total, 2), round(single, 2))
        case "velo":
            return (round(base_limit[0] * 0.7, 2), base_limit[1])
        case "smc" | "alpha" | "fibo" | "rev":
            return base_limit
        case _:
            return base_limit


def synthetic_vix_multiplier(current_atr: float, avg_atr: float) -> float:
    """
    No.13 — Synthetic VIX Toggle.
    Compares current M1 ATR to 20-period average ATR.
    Returns a lot multiplier (0.40x – 1.00x) scaled by volatility state.

    High volatility -> smaller lot (protect account during spikes).
    Normal/calm     -> full lot (enter with confidence).

    Multipliers:
      atr_ratio >= 2.5  -> 0.40x  (extreme volatile, very small lot)
      atr_ratio >= 2.0  -> 0.55x  (very volatile)
      atr_ratio >= 1.5  -> 0.75x  (moderately volatile)
      else              -> 1.00x  (normal / calm)
    """
    if avg_atr <= 0:
        return 1.0
    atr_ratio = current_atr / avg_atr
    if atr_ratio >= 2.5:
        logger.info(f"Synthetic VIX (No.13): EXTREME volatility (ratio={atr_ratio:.2f}) -> x0.40")
        return 0.40
    if atr_ratio >= 2.0:
        logger.info(f"Synthetic VIX (No.13): Very volatile (ratio={atr_ratio:.2f}) -> x0.55")
        return 0.55
    if atr_ratio >= 1.5:
        logger.debug(f"Synthetic VIX (No.13): Moderate volatility (ratio={atr_ratio:.2f}) -> x0.75")
        return 0.75
    return 1.0


def round_number_offset(price: float, direction: str, is_tp: bool) -> float:
    """
    No.18 — Round Number Overshoot Offset.
    Shift TP/SL slightly away from round numbers (.00) to avoid stop-hunts.

    For TP:
      BUY  TP -> target .98 (exit before round number resistance)
      SELL TP -> target .02 (exit before round number support)

    For SL:
      BUY  SL -> place at .98 (below round number, beyond typical MM sweep)
      SELL SL -> place at .02 (above round number)

    Only adjusts if within 3 pips of a round number.
    """
    rounded = round(price)
    dist    = abs(price - rounded)

    if dist > 0.3:
        return price

    offset = 0.02  # 2 pips from the round number
    if is_tp:
        if direction == "buy":
            return rounded - offset   # e.g. 2350.00 -> 2349.98
        else:
            return rounded + offset   # e.g. 2350.00 -> 2350.02
    else:
        # SL: place beyond the round number to avoid MM stop-hunt at clean levels
        if direction == "buy":
            return rounded - offset   # e.g. 2340.00 -> 2339.98 (below entry, buy SL)
        else:
            return rounded + offset   # e.g. 2340.00 -> 2340.02


def calculate_lot_size(
    account_balance: float,
    stop_loss_pips: float,
    risk_pct: float | None = None,
    pip_value_per_lot: float = PIP_VALUE_PER_STD_LOT,
    news_active: bool = False,
    signal_score: float = 10.0,
    recovery_multiplier: float = 1.0,
    current_atr: float = 0.0,    # No.13: current M1 ATR
    avg_atr: float = 0.0,        # No.13: 20-period average M1 ATR
    strategy: str = "",
    session_name: str = "",      # No.31: session name for volatile cap
) -> float:
    """
    Calculate position size based on risk percentage and safety filters.

    Incorporates:
      - Signal quality scaling (low score -> min lot)
      - Synthetic VIX scaling (No.13): reduces lot during high volatility
      - News guard: force min lot during high-impact events
      - Mode hard-cap: lot cannot imply more risk than mode's risk_per_trade
      - Volatile session cap (No.31): US/Overlap sessions cap lot further
    """
    if stop_loss_pips <= 0:
        raise ValueError(f"Stop loss pips must be > 0, got {stop_loss_pips}")

    rp = risk_pct or RISK_CONFIG.risk_per_trade_pct

    # News guard — defensive minimum lot during high-impact news
    if news_active:
        logger.warning("High-impact news active: forcing min lot size for safety.")
        return RISK_CONFIG.min_lot_size

    # Signal quality filter — reject low-quality signals with minimum lot
    mode_cfg  = TRADING_CONFIG.mode_settings.get(TRADING_CONFIG.current_mode, {})
    min_req   = mode_cfg.get("min_score_threshold", 8)

    if signal_score < min_req:
        logger.info(
            f"Low-quality signal (score {signal_score} < min {min_req}): "
            f"using min_lot {RISK_CONFIG.min_lot_size}"
        )
        return RISK_CONFIG.min_lot_size

    # No.13 Synthetic VIX multiplier
    vix_mult = synthetic_vix_multiplier(current_atr, avg_atr) if current_atr > 0 and avg_atr > 0 else 1.0
    rp *= vix_mult

    # Recovery multiplier (martingale) applied here
    rp *= recovery_multiplier
    risk_amount = account_balance * (rp / 100.0)
    raw_lot     = risk_amount / (stop_loss_pips * pip_value_per_lot)

    # Round down to nearest lot step
    step     = RISK_CONFIG.lot_step
    lot_size = math.floor(raw_lot / step) * step

    # No.31 — Get equity-tier cap with volatile session reduction applied
    _, single_cap = get_equity_lot_caps(account_balance, strategy=strategy, session_name=session_name)

    # Borderline signal: use 50% of single cap
    if signal_score < min_req + 1:
        effective_cap = max(RISK_CONFIG.min_lot_size, single_cap * 0.5)
        logger.info(f"Borderline setup (score {signal_score}): cap at 50% -> {effective_cap}")
        single_cap = effective_cap

    if lot_size > single_cap:
        logger.info(f"Lot {lot_size} capped to {single_cap} (equity-tier cap)")
        lot_size = single_cap

    # Mode hard-cap backstop: lot cannot exceed mode's declared risk fraction
    mode_risk_frac  = mode_cfg.get("risk_per_trade", 0.03)
    max_risk_amount = account_balance * mode_risk_frac
    max_lot_for_mode = math.floor(
        (max_risk_amount / (stop_loss_pips * pip_value_per_lot)) / step
    ) * step
    max_lot_for_mode = max(RISK_CONFIG.min_lot_size, max_lot_for_mode)
    if lot_size > max_lot_for_mode:
        logger.info(
            f"Lot {lot_size} -> {max_lot_for_mode} "
            f"(mode hard-cap {mode_risk_frac*100:.1f}% risk)"
        )
        lot_size = max_lot_for_mode

    # Global min/max clamp
    lot_size = max(RISK_CONFIG.min_lot_size, min(lot_size, RISK_CONFIG.max_lot_size))
    lot_size = round(lot_size, 2)

    logger.debug(
        f"LotSize: balance={account_balance:.2f} score={signal_score} "
        f"session={session_name} vix={vix_mult:.2f} final={lot_size}"
    )
    return lot_size


def price_to_pips(price_distance: float, pip_size: float = 0.1) -> float:
    """Convert price distance to pips (gold: 1 pip = $0.10)."""
    return price_distance / pip_size


def pips_to_price(pips: float, pip_size: float = 0.1) -> float:
    """Convert pips to price distance (gold)."""
    return pips * pip_size
