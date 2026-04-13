"""
Trading logic configuration: timeframes, sessions, scoring weights.
"""
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


class TradeMode(Enum):
    CONSERVATIVE = "CONSERVATIVE"
    MODERATE = "MODERATE"
    AGGRESSIVE = "AGGRESSIVE"
    VERY_AGGRESSIVE = "VERY_AGGRESSIVE"
    ULTRA_SCALPER = "ULTRA_SCALPER"


@dataclass
class TradingConfig:
    enable_asian_session: bool = True
    enable_partial_close: bool = True
    enable_multi_tf: bool = True
    trend_update_interval_minutes: int = 5  # Recurring reminder frequency
    # ── Timeframes ────────────────────────────────────────────────────────────
    htf_timeframes: Tuple[str, ...] = ("H4", "H1")    # bias determination
    ltf_timeframes: Tuple[str, ...] = ("M15", "M5")   # entry refinement
    primary_htf: str = "H4"
    primary_ltf: str = "M15"

    # ── Candle lookback ───────────────────────────────────────────────────────
    swing_lookback: int = 20          # candles for swing detection
    structure_lookback: int = 100     # candles for BOS/CHOCH history
    displacement_min_body_ratio: float = 0.6   # body/range > this = displacement
    displacement_min_pips: float = 15.0        # min displacement size in pips

    # ── Equal levels tolerance ────────────────────────────────────────────────
    equal_level_tolerance_pips: float = 5.0

    # ── Liquidity sweep ───────────────────────────────────────────────────────
    sweep_wick_ratio: float = 0.4   # wick must be > 40% of candle range

    # ── Sessions (UTC hours) ──────────────────────────────────────────────────
    london_session: Tuple[int, int] = (7, 16)
    ny_session: Tuple[int, int] = (12, 21)
    asian_session: Tuple[int, int] = (0, 9)
    entry_timeframes: Tuple[str, ...] = ("1m", "5m", "15m")  # small TFs for entry detection
    # The session filter will now allow any of these three sessions.

    # ── Scoring weights ───────────────────────────────────────────────────────
    score_weights: Dict[str, int] = field(default_factory=lambda: {
        "htf_alignment":      2,
        "liquidity_sweep":    2,
        "displacement":       2,
        "session_valid":      1,
        "volatility_ok":      1,
        "structure_shift":    1,
        "choch_confirmed":    1,
    })

    # ── ATR-based filters ─────────────────────────────────────────────────────
    atr_period: int = 14
    min_atr_pips: float = 6.0      # lowered from 8.0 to allow Asian session entries
    max_atr_pips: float = 120.0    # avoid extreme volatility (news)

    # ── News / event blackout (minutes before/after) ──────────────────────────
    news_blackout_minutes: int = 30

    # ── Trade Mode Specifics ──────────────────────────────────────────────────
    current_mode: TradeMode = TradeMode.VERY_AGGRESSIVE
    mode_settings: Dict[TradeMode, Dict] = field(default_factory=lambda: {
        TradeMode.CONSERVATIVE: {
            "risk_per_trade": 0.005,      # 0.5%
            "min_score_threshold": 8,
            "use_recovery": False,
            "partial_close": True,
            "auto_be": True,
            "pulse_scalping": False
        },
        TradeMode.MODERATE: {
            "risk_per_trade": 0.01,       # 1.0%
            "min_score_threshold": 6,
            "use_recovery": False,
            "partial_close": True,
            "auto_be": True,
            "pulse_scalping": False
        },
        TradeMode.AGGRESSIVE: {
            "risk_per_trade": 0.02,       # 2.0%
            "min_score_threshold": 5,
            "use_recovery": True,
            "recovery_multiplier": 1.5,
            "partial_close": True,
            "auto_be": True,
            "pulse_scalping": False
        },
        TradeMode.VERY_AGGRESSIVE: {
            "risk_per_trade": 0.03,       # 3.0% (Massive reduction from 15% to prevent account blowout)
            "min_score_threshold": 4,     # Raised threshold slightly to avoid 'asal open'
            "use_recovery": True,
            "recovery_multiplier": 1.5,
            "partial_close": True,
            "auto_be": False,
            "pulse_scalping": True,
            "pulse_compound": False
        },
        TradeMode.ULTRA_SCALPER: {
            "risk_per_trade": 0.10,       # Scaled differently for pulse
            "min_score_threshold": 3,
            "use_recovery": False,        # We don't martingale, we use 3x Guard
            "partial_close": False,
            "auto_be": False,
            "pulse_scalping": True,
            "pulse_compound": True       # Enable granular lot compounding
        }
    })


TRADING_CONFIG = TradingConfig()
