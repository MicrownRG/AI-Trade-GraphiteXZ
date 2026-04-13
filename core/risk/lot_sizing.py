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
from config.trading_config import TRADING_CONFIG
from utils.logger import get_logger
from core.risk.safety import SafetyManager

logger = get_logger(__name__)

# Gold (XAUUSD): 1 standard lot = 100 oz
# At $1 per pip per 0.01 lot (approximately)
# Pip value per standard lot (1.0 lot): $10 per pip (1 pip = $0.10 movement on 100 oz = $10)
# This is exchange/broker specific — use 10.0 as approximation
PIP_VALUE_PER_STD_LOT = 10.0   # USD per pip for 1.0 lot XAUUSD

def get_equity_lot_caps(equity: float, strategy: str = "") -> tuple[float, float]:
    """
    Returns (total_limit, single_entry_limit) based on ULTRA-SAFE granular equity tiers.
    Can be customized per strategy (SMC, FIBO, REV, PULSE).
    """

    # ── 1. Fast Numeric Tiers (if/elif is fastest for range checks) ──────────
    if   equity < 300:   base_limit = (0.10, 0.02)
    elif equity < 500:   base_limit = (0.15, 0.03)
    elif equity < 700:   base_limit = (0.20, 0.04)
    elif equity < 1000:  base_limit = (0.25, 0.05)
    elif equity < 1500:  base_limit = (0.30, 0.06)
    elif equity < 2500:  base_limit = (0.40, 0.08)
    elif equity < 4000:  base_limit = (0.50, 0.10)
    elif equity < 5000:  base_limit = (0.60, 0.12)
    elif equity < 6000:  base_limit = (0.70, 0.15)
    elif equity < 8000:  base_limit = (0.85, 0.18)
    elif equity < 10000: base_limit = (1.00, 0.20)
    else:                base_limit = (1.50, 0.30)

    # ── 2. High-Performance Strategy Matching (Python 3.10+) ─────────────────
    # Memisahkan prefix secara langsung (misal "SMC_12345" diarahkan ke "smc")
    # match-case jauh lebih kencang dibanding 4x pengecekan "boolean in string"
    if not strategy:
        return base_limit
        
    strat_key = strategy.split('_')[0].split('-')[0].lower() 
    
    match strat_key:
        case "smc" | "alpha":
            return base_limit
        case "fibo":
            return base_limit
        case "rev":
            return base_limit
        case "pulse":
            return base_limit
        case "velo":
            return base_limit
        case _:
            return base_limit


def synthetic_vix_multiplier(current_atr: float, avg_atr: float) -> float:
    """
    No.13 — Synthetic VIX Toggle.
    Compares current ATR to historical average ATR.
    Returns a lot multiplier (0.5x to 1.0x) based on volatility state.

    High volatility → smaller lot (protect account during spikes).
    Normal/low volatility → full lot (enter with confidence).

    Multipliers:
      atr_ratio >= 2.5  → 0.40x  (extreme volatile, very small lot)
      atr_ratio >= 2.0  → 0.55x  (very volatile)
      atr_ratio >= 1.5  → 0.75x  (moderately volatile)
      else              → 1.00x  (normal / calm)
    """
    if avg_atr <= 0:
        return 1.0
    atr_ratio = current_atr / avg_atr
    if atr_ratio >= 2.5:
        logger.info(f"🔥 Synthetic VIX: EXTREME volatile (ATR ratio={atr_ratio:.2f}) → lot x0.40")
        return 0.40
    if atr_ratio >= 2.0:
        logger.info(f"⚠️ Synthetic VIX: Very volatile (ATR ratio={atr_ratio:.2f}) → lot x0.55")
        return 0.55
    if atr_ratio >= 1.5:
        logger.debug(f"Synthetic VIX: Moderately volatile (ATR ratio={atr_ratio:.2f}) → lot x0.75")
        return 0.75
    return 1.0


def round_number_offset(price: float, direction: str, is_tp: bool) -> float:
    """
    No.18 — Round Number Overshoot Offset.
    Move TP/SL slightly away from round numbers (.00) to avoid stop-hunt.

    For TP:
      BUY  TP → target .98 instead of .00 (take profit before round number resistance)
      SELL TP → target .02 instead of .00 (take profit before round number support)

    For SL:
      BUY  SL → place at .02 instead of .00 (avoid being stopped by MM sweep of .00)
      SELL SL → place at .98 instead of .00

    Only adjusts if within 3 pips of a round number.
    """
    # Find nearest round number
    rounded = round(price)
    dist = abs(price - rounded)

    # Only apply if within 3 pips of round number
    if dist > 0.3:
        return price

    offset = 0.02  # 2 pips away from the round
    if is_tp:
        # TP: take profit BEFORE hitting the round number
        if direction == "buy":
            return rounded - offset   # e.g., 2350.00 → 2349.98
        else:
            return rounded + offset   # e.g., 2350.00 → 2350.02
    else:
        # SL: place BEYOND the round number to avoid MM stop hunt
        if direction == "buy":
            return rounded + offset   # e.g., 2340.00 → 2340.02 (below entry, buy SL)
        else:
            return rounded - offset   # e.g., 2340.00 → 2339.98


def calculate_lot_size(
    account_balance: float,
    stop_loss_pips: float,
    risk_pct: float | None = None,
    pip_value_per_lot: float = PIP_VALUE_PER_STD_LOT,
    news_active: bool = False,
    signal_score: float = 10.0,
    recovery_multiplier: float = 1.0,
    current_atr: float = 0.0,    # No.13: current ATR for VIX calc
    avg_atr: float = 0.0,        # No.13: average ATR (20-period)
    strategy: str = "",
) -> float:
    """
    Calculate position size based on risk percentage and safety filters.
    Includes Synthetic VIX scaling (No.13) to reduce lot during high volatility.
    """
    if stop_loss_pips <= 0:
        raise ValueError(f"Stop loss pips must be > 0, got {stop_loss_pips}")

    rp  = risk_pct or RISK_CONFIG.risk_per_trade_pct

    # ── News Guard (Defensive Mode) ───────────────────────────────────────────
    if news_active:
        logger.warning(f"High-Impact News Active! Forcing 0.01 lot for safety.")
        return RISK_CONFIG.min_lot_size

    # ── Signal Quality Filter (Dynamic Scaling) ──────────────────────────────────
    mode_cfg = TRADING_CONFIG.mode_settings.get(TRADING_CONFIG.current_mode, {})
    min_req = mode_cfg.get("min_score_threshold", 8)

    if signal_score < min_req:
        logger.info(f"Rejected by lot sizer: Score {signal_score} < min req {min_req}")
        return RISK_CONFIG.min_lot_size

    # ── No.13 Synthetic VIX Multiplier ────────────────────────────────────────
    vix_mult = synthetic_vix_multiplier(current_atr, avg_atr) if current_atr > 0 and avg_atr > 0 else 1.0
    rp *= vix_mult

    # ── Standard Risk Calc ───────────────────────────────────────────────────
    rp *= recovery_multiplier
    risk_amount = account_balance * (rp / 100.0)
    raw_lot     = risk_amount / (stop_loss_pips * pip_value_per_lot)

    # Round down to nearest step
    step     = RISK_CONFIG.lot_step
    lot_size = math.floor(raw_lot / step) * step

    # ── Granting Equity Caps & Quality Damping ───────────────────────────────
    _, single_cap = get_equity_lot_caps(account_balance, strategy=strategy)

    # Sinyal Moderate: Gunakan 50% dari plafon entry jika pas batas minimal
    if signal_score <= (min_req + 2):
        effective_cap = max(RISK_CONFIG.min_lot_size, single_cap * 0.5)
        logger.info(f"Moderate Setup (Score {signal_score}). Capping at 50%: {effective_cap}")
        single_cap = effective_cap

    # Apply the dynamic equity tier cap
    if lot_size > single_cap:
        logger.info(f"Lot size {lot_size} capped to {single_cap} (Dynamic Cap)")
        lot_size = single_cap

    # ── Absolute mode-based hard risk cap (Backstop Layer) ───────────────────
    # This is the final safety net: the lot CANNOT imply more risk than the
    # mode's risk_per_trade setting, regardless of any other calculation above.
    mode_risk_frac  = mode_cfg.get("risk_per_trade", 0.03)   # fraction (0.03 = 3%)
    max_risk_amount = account_balance * mode_risk_frac
    max_lot_for_mode = math.floor((max_risk_amount / (stop_loss_pips * pip_value_per_lot)) / step) * step
    max_lot_for_mode = max(RISK_CONFIG.min_lot_size, max_lot_for_mode)
    if lot_size > max_lot_for_mode:
        logger.info(f"Lot {lot_size} → {max_lot_for_mode} (Mode hard-cap {mode_risk_frac*100:.1f}% risk)")
        lot_size = max_lot_for_mode

    # Clamp to global min/max
    lot_size = max(RISK_CONFIG.min_lot_size, min(lot_size, RISK_CONFIG.max_lot_size))
    lot_size = round(lot_size, 2)

    logger.debug(
        f"LotSize: balance={account_balance:.2f} score={signal_score} "
        f"vix_mult={vix_mult:.2f} final={lot_size}"
    )
    return lot_size



def price_to_pips(price_distance: float, pip_size: float = 0.1) -> float:
    """Convert price distance to pips (gold: 1 pip = $0.10)."""
    return price_distance / pip_size


def pips_to_price(pips: float, pip_size: float = 0.1) -> float:
    return pips * pip_size
