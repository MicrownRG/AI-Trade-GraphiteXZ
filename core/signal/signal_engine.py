"""
Signal Engine.

Aggregates all structural analysis into a scored trade signal.
Only signals with score >= MIN_SIGNAL_SCORE are forwarded to risk management.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
import pandas as pd
from datetime import datetime

from core.structure import (
    calculate_htf_bias, HTFBias,
    get_latest_sweep, LiquiditySweep,
    get_recent_displacement, DisplacementEvent,
    get_latest_choch, CHOCHEvent,
    atr,
)
from config.trading_config import TRADING_CONFIG
from config.risk_config import RISK_CONFIG
from utils.logger import get_logger
from utils.time_utils import is_valid_session, get_session_name

logger = get_logger(__name__)


@dataclass
class TradeSignal:
    # ── Identity ──────────────────────────────────────────────────────────────
    signal_id: str
    symbol: str
    timestamp: datetime
    direction: str            # "buy" | "sell"

    # ── Prices ────────────────────────────────────────────────────────────────
    entry_price: float
    stop_loss: float
    take_profit: float
    rr_ratio: float

    # ── Scoring ───────────────────────────────────────────────────────────────
    score: int
    score_breakdown: Dict[str, int] = field(default_factory=dict)
    max_score: int = 10

    # ── Context ───────────────────────────────────────────────────────────────
    htf_bias: Optional[HTFBias] = None
    sweep: Optional[LiquiditySweep] = None
    displacement: Optional[DisplacementEvent] = None
    choch: Optional[CHOCHEvent] = None
    atr_pips: float = 0.0
    session: str = ""

    # ── AI layer (filled later) ───────────────────────────────────────────────
    ai_confidence: float = 0.0
    ai_decision: str = "PENDING"
    ai_reason: str = ""

    # ── Extreme & Reentry Logic ───────────────────────────────────────────────
    is_extreme: bool = False
    is_reentry: bool = False
    
    # ── Stalking / Pantau Logic ──────────────────────────────────────────────
    is_stalking: bool = False
    stalking_reason: str = ""

    # ── Size Modulation (thin-retrace, tiered entries) ───────────────────────
    # 1.0 = full size, <1.0 = scaled-down entry (still valid setup, reduced risk)
    lot_multiplier: float = 1.0
    entry_tier: str = "ZONE"  # ZONE | PROXIMITY | THIN_RETRACE | MOMENTUM

    @property
    def is_valid(self) -> bool:
        return (
            self.score >= RISK_CONFIG.min_signal_score
            and self.rr_ratio >= TRADING_CONFIG.score_weights.get("min_rr", 2.0)
        )


class SignalEngine:
    def __init__(self):
        self.weights = TRADING_CONFIG.score_weights

    def generate(
        self,
        df_h4: pd.DataFrame,
        df_h1: pd.DataFrame,
        df_ltf: pd.DataFrame,   # M15 entry timeframe (fallback)
        current_time: datetime,
        symbol: str = "XAUUSD",
        entry_dfs: dict | None = None,  # mapping timeframe -> DataFrame for small TFs
    ) -> Optional[TradeSignal]:
        """
        Full pipeline: structure → scoring → signal creation.
        Returns None if no valid setup found.
        """
        # ── 1. HTF Bias ───────────────────────────────────────────────────────
        htf_bias = calculate_htf_bias(df_h4, df_h1)
        if htf_bias.direction == "neutral":
            logger.debug("HTF bias neutral — no directional signal possible")
            return None  # Exit early if no clear direction


        # ── 2. Session filter ─────────────────────────────────────────────────
        session = get_session_name(current_time)
        session_valid = session in ("LONDON", "NY", "ASIA")

        # ── 3. ATR / Volatility ───────────────────────────────────────────────
        atr_series = atr(df_ltf, TRADING_CONFIG.atr_period)
        current_atr = atr_series.iloc[-1] if not atr_series.empty else 0.0
        atr_pips = current_atr / 0.1
        volatility_ok = (
            TRADING_CONFIG.min_atr_pips <= atr_pips <= TRADING_CONFIG.max_atr_pips
        )

        # ── 4. Liquidity sweep on LTF ─────────────────────────────────────────
        sweep = get_latest_sweep(df_ltf)
        sweep_valid = sweep is not None and _sweep_aligns_bias(sweep, htf_bias)

        # ── 5. Displacement ───────────────────────────────────────────────────
        displacement = get_recent_displacement(df_ltf, lookback_bars=5)
        displacement_valid = (
            displacement is not None
            and _displacement_aligns_bias(displacement, htf_bias)
        )

        # ── 6. CHOCH / Structure shift ────────────────────────────────────────
        choch = get_latest_choch(df_ltf)
        structure_shift = choch is not None and _choch_aligns_bias(choch, htf_bias)

        # ── 7. Score ──────────────────────────────────────────────────────────
        breakdown = {
            "htf_alignment":   self.weights["htf_alignment"]    if (htf_bias.direction != "neutral" and htf_bias.confidence >= 0.60) else 0,
            "liquidity_sweep": self.weights["liquidity_sweep"]  if sweep_valid             else 0,
            "displacement":    self.weights["displacement"]     if displacement_valid      else 0,
            "session_valid":   self.weights["session_valid"]    if session_valid           else 0,
            "volatility_ok":   self.weights["volatility_ok"]    if volatility_ok           else 0,
            "structure_shift": self.weights["structure_shift"]  if structure_shift         else 0,
            "choch_confirmed": self.weights["choch_confirmed"]  if (choch is not None and not structure_shift) else 0,
        }
        score = sum(breakdown.values())

        # ── 7b. Apply adaptive weight offsets from LearningEngine ────────────
        # Only non-zero components receive an offset to avoid biasing absent signals.
        try:
            from ai.learning_engine import LearningEngine
            from core.data.news_redis import news_client
            _htf_bias_str = htf_bias.direction.upper() if htf_bias else "NEUTRAL"
            _news_active  = news_client.is_news_active()
            _learning     = LearningEngine(None)   # no repo needed for get_score_offsets
            offsets = _learning.get_score_offsets(session, _htf_bias_str, _news_active)
            for component, offset in offsets.items():
                if component in breakdown and breakdown[component] != 0:
                    adjusted = breakdown[component] + int(offset)
                    breakdown[component] = max(0, adjusted)
                    score = sum(breakdown.values())
        except Exception:
            pass  # adaptive layer is best-effort; never block a signal

        # ── 7c. News sentiment bias ──────────────────────────────────────────
        # XAU sentiment from Go scraper: align → +1 score, oppose → −2 score.
        # Stronger penalty than bonus because trading into a sentiment wall
        # has historically been the costlier error.
        try:
            from core.data.news_redis import news_client as _nc_bias
            _sentiment = _nc_bias.get_dominant_sentiment()
            _dir_bull  = (htf_bias and htf_bias.direction == "bullish")
            if _sentiment == "bullish_xau":
                score += 1 if _dir_bull else -2
            elif _sentiment == "bearish_xau":
                score += -2 if _dir_bull else 1
            score = max(0, score)
        except Exception:
            pass

        if score < RISK_CONFIG.min_signal_score:
            logger.debug(f"Signal score {score} below threshold {RISK_CONFIG.min_signal_score}")
            return None

        # ── 8. Entry / SL / TP calculation ───────────────────────────────────
        direction  = "buy" if htf_bias.direction == "bullish" else "sell"
        last_close = df_ltf["close"].iloc[-1]
        sl_distance = max(current_atr * 1.5, 10 * 0.1)  # at least 10 pips

        # Dynamic RR: if score is exceptionally high, we can accept a slightly lower RR
        target_rr = TRADING_CONFIG.score_weights.get("min_rr", 1.5)
        if score >= 8:
            target_rr = max(1.2, target_rr * 0.8)  # Allow down to 1.2 if setup is "Perfect"

        # Adaptive TP multiplier from LearningEngine (scales target_rr up/down)
        try:
            from ai.learning_engine import LearningEngine
            _htf_str  = htf_bias.direction.upper() if htf_bias else "NEUTRAL"
            _news_act = False
            try:
                from core.data.news_redis import news_client as _nc
                _news_act = _nc.is_news_active()
            except Exception:
                pass
            _tp_mult = LearningEngine(None).get_tp_multiplier(session, _htf_str, _news_act)
            target_rr = round(target_rr * _tp_mult, 3)
        except Exception:
            pass

        # Apply spread for accurate RR calc and actual entry
        spread_price = 3.0 * 0.1  # approx 3 pips
        if direction == "buy":
            entry = last_close + spread_price
            sl    = entry - sl_distance
            tp    = entry + sl_distance * target_rr
        else:
            entry = last_close
            sl    = entry + sl_distance
            tp    = entry - sl_distance * target_rr

        rr = abs(tp - entry) / abs(sl - entry) if abs(sl - entry) > 0 else 0

        import uuid
        signal = TradeSignal(
            signal_id=str(uuid.uuid4())[:8],
            symbol=symbol,
            timestamp=current_time,
            direction=direction,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            rr_ratio=rr,
            score=score,
            score_breakdown=breakdown,
            max_score=sum(self.weights.values()),
            htf_bias=htf_bias,
            sweep=sweep,
            displacement=displacement,
            choch=choch,
            atr_pips=atr_pips,
            session=session,
        )
        logger.info(f"Signal generated: {direction.upper()} score={score} RR={rr:.2f}")
        return signal


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sweep_aligns_bias(sweep: LiquiditySweep, bias: HTFBias) -> bool:
    # Sell-side sweep (swept lows) → bullish continuation expected
    # Buy-side sweep (swept highs) → bearish continuation expected
    return (
        (bias.direction == "bullish" and sweep.direction == "sell_side") or
        (bias.direction == "bearish" and sweep.direction == "buy_side")
    )


def _displacement_aligns_bias(disp: DisplacementEvent, bias: HTFBias) -> bool:
    return (
        (bias.direction == "bullish" and disp.direction == "bullish") or
        (bias.direction == "bearish" and disp.direction == "bearish")
    )


def _choch_aligns_bias(choch: CHOCHEvent, bias: HTFBias) -> bool:
    return choch.direction == bias.direction



