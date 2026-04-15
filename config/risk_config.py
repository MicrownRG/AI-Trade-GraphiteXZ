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
    min_rr_ratio: float = 1.5             # minimum reward-to-risk ratio
    max_lot_size: float = 5.0             # hard cap on single trade lots
    min_lot_size: float = 0.01
    lot_step: float = 0.01                # MT5 lot step for XAUUSD

    # ── Session Multipliers (Lower risk for low-liquidity sessions) ──────────
    risk_multiplier_prime: float = 1.0    # London / NY
    risk_multiplier_low:   float = 0.5    # Asian / Late NY

    # ── Daily & drawdown limits ───────────────────────────────────────────────
    # Hard cutloss: max REALIZED (closed) daily loss before halting trading.
    # Floating losses are ignored — only closed P&L counts toward this limit.
    # Auto-capped at 5% regardless of what is set here.
    hard_cutloss_daily_pct: float = 5.0   # max realized daily loss — hard cap (≤ 5%)
    max_daily_loss_pct: float = 5.0       # legacy filter alias — kept in sync with hard_cutloss
    max_concurrent_trades: int = 4        # capped to 4 to prevent over-exposure
    multi_entry_delay_sec: int = 120      # 2 min gap between positions (scalper-friendly but safe)

    # ── Smart Auto-Close Conditions ───────────────────────────────────────────
    auto_close_stagnant_hours: int = 4    # close if floating 4h without hitting BE/TP
    auto_close_reversal_guard: bool = True# cut loss early on strong M15 reversal against trade
    auto_close_eod: bool = False          # close all trades at 23:45 daily to avoid overnight swap

    # ── Anti-Liquidation & Margin Safety ──────────────────────────────────────
    min_margin_level_pct: float = 200.0   # scale down if below this
    hard_liq_protection_pct: float = 100.0# emergency close if below this

    # ── News / Event Risk ─────────────────────────────────────────────────────
    news_risk_multiplier: float = 0.5     # reduce risk by 50% during news

    # ── Cooldown & loss streak ────────────────────────────────────────────────
    revenge_cooldown_min: float = 15.0    # base cool-down after any loss (mode-scaled in filters.py)
    consecutive_loss_limit: int = 5       # pause if 5 losses in a row

    # ── Spread / slippage tolerances ─────────────────────────────────────────
    max_allowed_spread_pips: float = 30.0 # refuse trade if spread > this
    max_slippage_pips: float = 5.0

    # ── Trade scoring gate ────────────────────────────────────────────────────
    min_signal_score: int = 4             # discard signals with score < this
    min_ai_confidence: float = 0.50       # discard if AI confidence < this

    def __post_init__(self):
        assert 0 < self.risk_per_trade_pct <= 20, "Risk per trade cap at 20%"
        assert self.min_rr_ratio >= 1.0, "Minimum RR must be >= 1.0 for scalping"
        assert self.hard_cutloss_daily_pct > 0
        # Enforce 5% ceiling — no user input can raise this above 5%
        if self.hard_cutloss_daily_pct > 5.0:
            import logging
            logging.getLogger(__name__).warning(
                f"hard_cutloss_daily_pct={self.hard_cutloss_daily_pct} exceeds max 5% — "
                f"reset to 5.0 (use object.__setattr__ pattern to override in tests only)"
            )
            object.__setattr__(self, "hard_cutloss_daily_pct", 5.0)
            object.__setattr__(self, "max_daily_loss_pct", 5.0)


# Singleton used throughout the system
RISK_CONFIG = RiskConfig()
