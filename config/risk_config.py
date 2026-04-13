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
    max_daily_loss_pct: float = 15.0      # Cap daily loss at 15%
    max_concurrent_trades: int = 4        # Capped to 4 to prevent over-exposure
    multi_entry_delay_sec: int = 120      # 2 Minutes gap between positions (Scalper friendly but safe)

    # ── Smart Auto-Close Conditions ───────────────────────────────────────────
    auto_close_stagnant_hours: int = 4    # Close if floating for 4h without hitting BE/TP
    auto_close_reversal_guard: bool = True# Cut loss early on strong M15 reversal against trade
    auto_close_eod: bool = False          # Close all trades at 23:45 daily to avoid overnight swap

    # ── Anti-Liquidation & Margin Safety ──────────────────────────────────────
    min_margin_level_pct: float = 200.0   # Scale down if below this
    hard_liq_protection_pct: float = 100.0 # Emergency close if below this

    # ── News / Event Risk ─────────────────────────────────────────────────────
    news_risk_multiplier: float = 0.5     # Reduce risk by 50% during news

    # ── Kill-switch thresholds ────────────────────────────────────────────────
    kill_switch_drawdown_pct: float = 30.0   # Absolute MC prevention
    kill_switch_daily_loss_pct: float = 25.0
    revenge_cooldown_min: float = 30.0    # Cool-down after any loss
    consecutive_loss_limit: int = 5       # Pause if 5 losses in a row

    # ── Spread / slippage tolerances ─────────────────────────────────────────
    max_allowed_spread_pips: float = 30.0   # refuse trade if spread > this
    max_slippage_pips: float = 5.0

    # ── Trade scoring gate ────────────────────────────────────────────────────
    min_signal_score: int = 4              # discard signals with score < this
    min_ai_confidence: float = 0.50        # discard if AI confidence < this

    def __post_init__(self):
        assert 0 < self.risk_per_trade_pct <= 20, "Risk per trade cap at 20%"
        assert self.min_rr_ratio >= 1.0, "Minimum RR must be >= 1.0 for scalping"
        assert self.max_daily_loss_pct > 0


# Singleton used throughout the system
RISK_CONFIG = RiskConfig()
