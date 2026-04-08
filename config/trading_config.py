"""
Trading logic configuration: timeframes, sessions, scoring weights.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class TradingConfig:
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
    # Asian session filtered out (low-quality gold moves)

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
    min_atr_pips: float = 8.0      # avoid dead market
    max_atr_pips: float = 120.0    # avoid extreme volatility (news)

    # ── News / event blackout (minutes before/after) ──────────────────────────
    news_blackout_minutes: int = 30


TRADING_CONFIG = TradingConfig()
