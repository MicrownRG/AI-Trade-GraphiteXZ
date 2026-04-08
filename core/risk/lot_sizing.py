"""
Dynamic lot sizing based on risk percentage of account balance.

Formula:
  risk_amount = balance * (risk_pct / 100)
  pip_value   = lot_size * pip_value_per_lot
  lot_size    = risk_amount / (sl_pips * pip_value_per_lot)
"""
from __future__ import annotations
import math
from config.risk_config import RISK_CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)

# Gold (XAUUSD): 1 standard lot = 100 oz
# At $1 per pip per 0.01 lot (approximately)
# Pip value per standard lot (1.0 lot): $10 per pip (1 pip = $0.10 movement on 100 oz = $10)
# This is exchange/broker specific — use 10.0 as approximation
PIP_VALUE_PER_STD_LOT = 10.0   # USD per pip for 1.0 lot XAUUSD


def calculate_lot_size(
    account_balance: float,
    stop_loss_pips: float,
    risk_pct: float | None = None,
    pip_value_per_lot: float = PIP_VALUE_PER_STD_LOT,
) -> float:
    """
    Calculate position size based on risk percentage.

    Args:
        account_balance: current equity in USD
        stop_loss_pips:  distance from entry to SL in pips
        risk_pct:        fraction of balance to risk (overrides config if set)
        pip_value_per_lot: USD value per pip per 1.0 lot

    Returns:
        Lot size rounded to the nearest lot_step.
    """
    if stop_loss_pips <= 0:
        raise ValueError(f"Stop loss pips must be > 0, got {stop_loss_pips}")

    rp  = risk_pct or RISK_CONFIG.risk_per_trade_pct
    risk_amount = account_balance * (rp / 100.0)
    raw_lot     = risk_amount / (stop_loss_pips * pip_value_per_lot)

    # Clamp to allowed range
    clamped = max(RISK_CONFIG.min_lot_size, min(raw_lot, RISK_CONFIG.max_lot_size))

    # Round down to nearest step
    step     = RISK_CONFIG.lot_step
    lot_size = math.floor(clamped / step) * step
    lot_size = round(lot_size, 2)

    logger.debug(
        f"LotSize: balance={account_balance:.2f} risk={rp}% "
        f"SL={stop_loss_pips:.1f}pips → raw={raw_lot:.3f} final={lot_size}"
    )
    return lot_size


def price_to_pips(price_distance: float, pip_size: float = 0.1) -> float:
    """Convert price distance to pips (gold: 1 pip = $0.10)."""
    return price_distance / pip_size


def pips_to_price(pips: float, pip_size: float = 0.1) -> float:
    return pips * pip_size
