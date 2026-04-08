"""
Risk management parameters — tweak these carefully.
All values validated at startup; invalid config raises immediately.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class RiskConfig:
    # ── Per-trade risk ────────────────────────────────────────────────────────
    risk_per_trade_pct: float = 1.0       # % of account balance risked per trade
    min_rr_ratio: float = 2.0             # minimum reward-to-risk ratio
    max_lot_size: float = 5.0             # hard cap on single trade lots
    min_lot_size: float = 0.01
    lot_step: float = 0.01                # MT5 lot step for XAUUSD

    # ── Daily & drawdown limits ───────────────────────────────────────────────
    max_daily_loss_pct: float = 3.0       # daily loss as % of starting balance
    max_drawdown_pct: float = 8.0         # max peak-to-trough drawdown %
    max_daily_trades: int = 5             # hard cap on trade count per day
    max_concurrent_trades: int = 2

    # ── Kill-switch thresholds ────────────────────────────────────────────────
    kill_switch_drawdown_pct: float = 6.0
    kill_switch_daily_loss_pct: float = 2.5

    # ── Spread / slippage tolerances ─────────────────────────────────────────
    max_allowed_spread_pips: float = 30.0   # refuse trade if spread > this
    max_slippage_pips: float = 5.0

    # ── Trade scoring gate ────────────────────────────────────────────────────
    min_signal_score: int = 5              # discard signals with score < this
    min_ai_confidence: float = 0.60        # discard if AI confidence < this

    def __post_init__(self):
        assert 0 < self.risk_per_trade_pct <= 5, "Risk per trade must be 0–5%"
        assert self.min_rr_ratio >= 1.5, "Minimum RR must be >= 1.5"
        assert self.max_daily_loss_pct > 0
        assert self.max_drawdown_pct > self.kill_switch_drawdown_pct


# Singleton used throughout the system
RISK_CONFIG = RiskConfig()
