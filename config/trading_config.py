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
    trend_update_interval_minutes: int = 5  # Periodic market structure report interval

    # ── Timeframes ────────────────────────────────────────────────────────────
    htf_timeframes: Tuple[str, ...] = ("H4", "H1")    # bias determination
    ltf_timeframes: Tuple[str, ...] = ("M15", "M5")   # entry refinement
    primary_htf: str = "H4"
    primary_ltf: str = "M15"

    # ── Candle lookback ───────────────────────────────────────────────────────
    swing_lookback: int = 20          # candles for swing detection
    structure_lookback: int = 100     # candles for BOS/CHOCH history
    displacement_min_body_ratio: float = 0.6   # body/range > this = displacement candle
    displacement_min_pips: float = 15.0        # minimum displacement size in pips

    # ── Equal levels tolerance ────────────────────────────────────────────────
    equal_level_tolerance_pips: float = 5.0

    # ── Liquidity sweep ───────────────────────────────────────────────────────
    sweep_wick_ratio: float = 0.4   # wick must be > 40% of candle range to count as sweep

    # ── Sessions (UTC hours) ──────────────────────────────────────────────────
    london_session: Tuple[int, int] = (7, 16)
    ny_session: Tuple[int, int] = (12, 21)
    asian_session: Tuple[int, int] = (0, 9)
    entry_timeframes: Tuple[str, ...] = ("1m", "5m", "15m")  # small TFs for entry detection

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
    min_atr_pips: float = 6.0      # lowered to allow Asian session entries
    max_atr_pips: float = 120.0    # reject extreme volatility (e.g. news spikes)

    # ── News / event blackout (minutes before/after high-impact event) ────────
    news_blackout_minutes: int = 30

    # ── AI Adapter toggle ─────────────────────────────────────────────────────
    # When True, the LearningEngine applies adaptive score-weight offsets and
    # TP multiplier suggestions derived from past trade analysis.
    # Can be toggled at runtime via /adapter command in Telegram.
    ai_adapter_enabled: bool = True

    # ── Active trade mode ─────────────────────────────────────────────────────
    current_mode: TradeMode = TradeMode.VERY_AGGRESSIVE

    # ── Per-mode settings ─────────────────────────────────────────────────────
    # trailing_pips     : SL trailing distance once breakeven is active (SL+)
    # be_threshold_r    : profit in R-multiples required to activate breakeven
    # pulse_scalping    : whether the Pulse M1 extreme engine is enabled
    # pulse_compound    : whether Pulse lots compound (only Ultra)
    # use_recovery      : martingale multiplier on losing trades
    # recovery_multiplier: lot multiplier applied after a loss
    # partial_close     : close 50% at TP1 and move SL to breakeven
    # auto_be           : automatically move SL to breakeven at be_threshold_r
    # -------------------------------------------------------------------------
    # Conservative / Moderate:
    #   - Minimum risk, high score requirement, SL placed on M1 structure
    #   - Entries only a few times per day when ALL conditions align
    #   - Pulse disabled — too volatile for tight-SL scalping in trending sessions
    #   - SL floors applied by session (see executor.py) to avoid liquidity sweeps
    #
    # Aggressive / Very Aggressive / Ultra:
    #   - Frequent entries on small timeframes (M1/M5)
    #   - Pulse always enabled — can trade sideways Asian / calm sessions
    #   - Choppiness and weekly-open filters bypassed (can trade ranging markets)
    #   - SL+ (trailing) activates early and uses tighter trail distance
    #   - Recovery multiplier applied on losing trades
    #   - During high-volatility US sessions, Pulse is auto-blocked by session gate
    #     and SL floors widen automatically to avoid liquidity sweeps
    # -------------------------------------------------------------------------
    mode_settings: Dict[TradeMode, Dict] = field(default_factory=lambda: {
        TradeMode.CONSERVATIVE: {
            "risk_per_trade":       0.005,   # 0.5% — minimum risk
            "min_score_threshold":  8,        # very selective: only high-confluence setups
            "use_recovery":         False,
            "partial_close":        True,
            "auto_be":              True,
            "be_threshold_r":       1.0,      # move SL to BE once 1R in profit
            "trailing_pips":        40.0,     # wide trail to survive normal M15 pullbacks
            "pulse_scalping":       False,    # disabled — conservative avoids M1 extremes
            "max_daily_trades":     5,        # guard: at most 5 trades per day
        },
        TradeMode.MODERATE: {
            "risk_per_trade":       0.01,    # 1.0%
            "min_score_threshold":  6,        # selective but slightly more active
            "use_recovery":         False,
            "partial_close":        True,
            "auto_be":              True,
            "be_threshold_r":       1.0,      # move SL to BE at 1R profit
            "trailing_pips":        35.0,     # slightly tighter trail
            "pulse_scalping":       False,    # disabled for moderate risk profile
            "max_daily_trades":     10,
        },
        TradeMode.AGGRESSIVE: {
            "risk_per_trade":       0.02,    # 2.0%
            "min_score_threshold":  5,
            "use_recovery":         True,
            "recovery_multiplier":  1.5,
            "partial_close":        True,
            "auto_be":              True,
            "be_threshold_r":       0.8,      # activate BE earlier (0.8R) to lock profit faster
            "trailing_pips":        25.0,     # tighter trail — small TF entries
            "pulse_scalping":       True,     # enabled — frequent M1 entries in calm sessions
            "max_daily_trades":     0,        # 0 = unlimited
        },
        TradeMode.VERY_AGGRESSIVE: {
            "risk_per_trade":       0.025,   # 2.5% — 2 losses before 5% hard cutloss
            "min_score_threshold":  4,        # low threshold — active scanning
            "use_recovery":         True,
            "recovery_multiplier":  1.5,
            "partial_close":        True,
            "auto_be":              True,
            "be_threshold_r":       0.7,      # very early BE — protect profits on fast M1 moves
            "trailing_pips":        20.0,     # tight trail matching M1/M5 structure
            "pulse_scalping":       True,     # always active (blocked automatically in US/Overlap)
            "pulse_compound":       False,
            "max_daily_trades":     0,        # unlimited
        },
        TradeMode.ULTRA_SCALPER: {
            "risk_per_trade":       0.05,    # 5.0% — 1 loss before cutloss; compound handles growth
            "min_score_threshold":  3,
            "use_recovery":         False,   # no martingale — uses 3x equity guard instead
            "partial_close":        False,
            "auto_be":              False,
            "be_threshold_r":       0.5,      # ultra-fast protection at half-R
            "trailing_pips":        15.0,     # very tight trail for M1 scalping
            "pulse_scalping":       True,     # always active
            "pulse_compound":       True,     # granular lot compounding enabled
            "max_daily_trades":     0,
        },
    })


TRADING_CONFIG = TradingConfig()
