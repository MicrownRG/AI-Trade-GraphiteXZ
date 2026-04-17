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
        # ── Profit-lock fields (added) ────────────────────────────────────────
        # micro_profit_lock_r   : R-multiple at which to ratchet SL to entry+buffer
        #                         (guarantees a tiny profit — staircase equity curve)
        # micro_lock_buffer_pips: buffer above entry applied at the micro-lock step
        # partial_close_fraction: fraction of lot closed at TP1 (rest runs to TP2)
        # news_aligned_tp_mult  : TP multiplier applied when news sentiment agrees with
        #                         signal direction AND HTF is aligned (momentum boost)
        # quick_harvest_on_adverse_news:
        #                         close any in-profit trade immediately when news
        #                         sentiment flips against it
        # ---------------------------------------------------------------------
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
            # Profit-lock: small lock early, then loose trail → swing-panjang profit besar
            "micro_profit_lock_r":       0.30,
            "micro_lock_buffer_pips":    2.0,
            "sl_plus_delay_sec":         0,
            "partial_close_fraction":    0.30,   # only close 30% at TP1, let 70% run
            "news_aligned_tp_mult":      1.80,
            "quick_harvest_on_adverse_news": True,
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
            "micro_profit_lock_r":       0.25,
            "micro_lock_buffer_pips":    1.5,
            "sl_plus_delay_sec":         0,
            "partial_close_fraction":    0.35,
            "news_aligned_tp_mult":      1.60,
            "quick_harvest_on_adverse_news": True,
        },
        TradeMode.AGGRESSIVE: {
            "risk_per_trade":       0.02,    # 2.0%
            "min_score_threshold":  5,
            "use_recovery":         True,
            "recovery_multiplier":  1.5,
            "partial_close":        True,
            "auto_be":              True,
            "be_threshold_r":       0.5,      # fast BE at 0.5R — lock profit early
            "trailing_pips":        18.0,     # tighter trail — catch profit on M5 moves
            "pulse_scalping":       True,
            "pulse_compound":       True,     # compound enabled — equity growth
            "max_daily_trades":     0,
            "micro_profit_lock_r":       0.12,  # micro-lock at 12% of risk (≈3 pips)
            "micro_lock_buffer_pips":    5.0,   # 0.50 pts — covers spread
            "sl_plus_delay_sec":         90,    # wait 90s before SL+ to avoid premature lock
            "partial_close_fraction":    0.50,
            "news_aligned_tp_mult":      1.40,
            "quick_harvest_on_adverse_news": True,
            "momentum_fade_exit":        True,  # close when profit fades from peak
        },
        TradeMode.VERY_AGGRESSIVE: {
            "risk_per_trade":       0.025,   # 2.5%
            "min_score_threshold":  4,
            "use_recovery":         True,
            "recovery_multiplier":  1.5,
            "partial_close":        True,
            "auto_be":              True,
            "be_threshold_r":       0.4,      # very early BE — 100% WR target
            "trailing_pips":        12.0,     # tight trail on M1/M5
            "pulse_scalping":       True,
            "pulse_compound":       True,     # compound enabled — equity growth
            "max_daily_trades":     0,
            "micro_profit_lock_r":       0.08,  # lock at 8% of risk (≈2 pips) → near-instant
            "micro_lock_buffer_pips":    5.0,   # 0.50 pts — covers spread
            "sl_plus_delay_sec":         60,    # wait 60s before SL+ to avoid premature lock
            "partial_close_fraction":    0.60,
            "news_aligned_tp_mult":      1.30,
            "quick_harvest_on_adverse_news": True,
            "momentum_fade_exit":        True,
        },
        TradeMode.ULTRA_SCALPER: {
            "risk_per_trade":       0.05,    # 5.0%
            "min_score_threshold":  3,
            "use_recovery":         False,
            "partial_close":        True,     # partial close enabled — lock profit fast
            "auto_be":              True,     # auto BE enabled
            "be_threshold_r":       0.3,      # ultra-fast at 0.3R
            "trailing_pips":        8.0,      # very tight — M1 scalping
            "pulse_scalping":       True,
            "pulse_compound":       True,     # compound enabled
            "max_daily_trades":     0,
            "micro_profit_lock_r":       0.05,  # lock at 5% of risk (≈1-2 pips)
            "micro_lock_buffer_pips":    5.0,   # 0.50 pts — covers spread
            "sl_plus_delay_sec":         45,    # wait 45s before SL+ — ultra needs speed but not instant
            "partial_close_fraction":    0.70,
            "news_aligned_tp_mult":      1.20,
            "quick_harvest_on_adverse_news": True,
            "momentum_fade_exit":        True,
        },
    })


TRADING_CONFIG = TradingConfig()
