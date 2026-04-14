"""
Multi-Timeframe Trend Confluence Analyzer.

Analyzes trend direction across all timeframes (M1, M5, M15, M30, H1, H4)
and returns the best trade direction with a confidence score.

Strategy:
  - Each TF votes BULLISH, BEARISH, or NEUTRAL
  - Votes are weighted by TF importance (higher TF = more weight)
  - Confluence score determines direction confidence
  - Entry always executed on M1 (execution timeframe)

TF Weights:
  H4: 8  (master trend — anchor)
  H1: 6  (intermediate trend — confirmation)
  M30: 4 (sub-trend)
  M15: 3 (zone reference)
  M5:  2 (trigger momentum)
  M1:  1 (execution timing)

Accuracy improvements vs previous version:
  - Rejection candle bonus is capped to weight * 0.5 (was 1.5) to prevent a
    single wick on H4 from dominating the score
  - Zone bonus unchanged at weight * 0.5
  - master_bias requires slope confirmation from TrendLogic (not just EMA level)
  - Cache TTL differentiated per TF: H4/H1 cache 90s, LTFs 5s/realtime
"""
from __future__ import annotations
import time
import pandas as pd
import numpy as np
from typing import Dict, Optional, Tuple

from core.structure.trend_logic import TrendLogic
from utils.logger import get_logger

logger = get_logger(__name__)

# Full re-analysis TTL (seconds) — avoid recomputing every tick
_ANALYZE_TTL_SECONDS = 25

# Timeframe weights
TF_WEIGHTS = {
    "m1":  1,
    "m5":  2,
    "m15": 3,
    "m30": 4,
    "h1":  6,
    "h4":  8,
}

MIN_CONFLUENCE_SCORE = 6    # minimum points to consider a direction valid
MIN_STRONG_SCORE     = 12   # threshold for "strong" setups

# Per-TF cache TTL (seconds)
# Higher TFs change slowly — cache longer.
# M1 must be real-time; M5 can tolerate a short burst cache.
TF_TTL = {
    "h4":  90,   # H4 bar = 4h, changes very slowly
    "h1":  90,   # H1 same reasoning
    "m30": 45,
    "m15": 30,
    "m5":  5,
    "m1":  0,    # Always recalculate M1
}


class TFTrend:
    """Trend result for a single timeframe."""
    __slots__ = ("tf", "bias", "adx", "ema_cross", "weight", "score", "at_zone", "rejection")

    def __init__(
        self,
        tf: str,
        bias: str,
        adx: float,
        ema_cross: bool,
        at_zone: str = "NONE",
        rejection: str = "NONE",
    ):
        self.tf        = tf
        self.bias      = bias       # "BULLISH" | "BEARISH" | "NEUTRAL"
        self.adx       = adx
        self.ema_cross = ema_cross
        self.at_zone   = at_zone    # "BULLISH_OB" | "BEARISH_OB" | "NONE"
        self.rejection = rejection  # "BULLISH" | "BEARISH" | "NONE"
        self.weight    = TF_WEIGHTS.get(tf.lower(), 1)

        # Base score: weight * strength multiplier
        adx_mult    = 1.5 if adx > 25 else (1.2 if adx > 18 else 1.0)
        self.score  = self.weight * adx_mult if bias != "NEUTRAL" else 0.0

        # Rejection wick bonus — capped at weight * 0.5 to prevent single-candle
        # dominance (previously was weight * 1.5 which over-weighted one wick on H4)
        if rejection != "NONE" and rejection == bias:
            self.score += self.weight * 0.5

        # Zone hit bonus (price at HTF OB/FVG)
        if at_zone != "NONE":
            self.score += self.weight * 0.5

    def __repr__(self) -> str:
        return f"{self.tf.upper()}:{self.bias}(adx={self.adx:.0f})"


class MultiTFAnalysis:
    """Full multi-TF confluence result."""

    def __init__(
        self,
        tfs: Dict[str, TFTrend],
        bull_score: float,
        bear_score: float,
    ):
        self.tfs        = tfs
        self.bull_score = bull_score
        self.bear_score = bear_score

    @property
    def master_bias(self) -> str:
        """
        Higher-timeframe consensus bias (H4 / H1 / M30 fallback).
        Requires at least two HTFs to agree, or one clear trending HTF.
        """
        h4  = self.tfs.get("h4")
        h1  = self.tfs.get("h1")
        m30 = self.tfs.get("m30")

        # Strong consensus: H4 and H1 agree
        if h4 and h1 and h4.bias == h1.bias and h4.bias != "NEUTRAL":
            return h4.bias

        # Single HTF with strong ADX — trust it
        if h4 and h4.bias != "NEUTRAL" and h4.adx > 20:
            return h4.bias
        if h1 and h1.bias != "NEUTRAL" and h1.adx > 20:
            return h1.bias

        # Fallback: M30 (sub-trend)
        if m30 and m30.bias != "NEUTRAL" and m30.adx > 18:
            return m30.bias

        return "NEUTRAL"

    @property
    def direction(self) -> Optional[str]:
        """
        Primary trade direction.

        Priority:
          1. HTF zone hit with strong LTF confirmation (counter-trend reversal)
          2. HTF-aligned trend following
          3. High-confluence override when master bias is unclear
        """
        master = self.master_bias
        h4 = self.tfs.get("h4")
        h1 = self.tfs.get("h1")

        # Zone hit override — only if LTF score clearly dominates
        hitting_resis = (
            (h4 and h4.at_zone == "BEARISH_OB") or
            (h1 and h1.at_zone == "BEARISH_OB")
        )
        if (hitting_resis and
                self.bear_score >= MIN_STRONG_SCORE and
                self.bear_score > self.bull_score * 1.5):
            return "sell"

        hitting_supp = (
            (h4 and h4.at_zone == "BULLISH_OB") or
            (h1 and h1.at_zone == "BULLISH_OB")
        )
        if (hitting_supp and
                self.bull_score >= MIN_STRONG_SCORE and
                self.bull_score > self.bear_score * 1.5):
            return "buy"

        # Standard trend following
        if master == "BULLISH" and self.bull_score > self.bear_score:
            return "buy"
        if master == "BEARISH" and self.bear_score > self.bull_score:
            return "sell"

        # High-confluence override: score dominant AND 2x the opposing score
        threshold = MIN_CONFLUENCE_SCORE + 4  # 10 pts
        if self.bull_score >= threshold and self.bull_score > self.bear_score * 2.0:
            return "buy"
        if self.bear_score >= threshold and self.bear_score > self.bull_score * 2.0:
            return "sell"

        return None

    @property
    def confidence(self) -> float:
        """0.0 – 1.0 confidence based on dominant score ratio."""
        total = self.bull_score + self.bear_score
        if total == 0:
            return 0.0
        dominant = max(self.bull_score, self.bear_score)
        return round(dominant / total, 2)

    @property
    def is_strong(self) -> bool:
        return max(self.bull_score, self.bear_score) >= MIN_STRONG_SCORE

    @property
    def htf_aligned(self) -> bool:
        """H4 and H1 agree on the same non-neutral direction."""
        h4 = self.tfs.get("h4")
        h1 = self.tfs.get("h1")
        if h4 and h1:
            return h4.bias == h1.bias and h4.bias != "NEUTRAL"
        return False

    @property
    def reversal_detected(self) -> bool:
        """
        LTF (M1/M5) is trending OPPOSITE to HTF — possible pullback or flip.
        Used as a scoring bonus in advanced_signal_engine for reversal setups.
        """
        htf_bias = self.tfs.get("h4") or self.tfs.get("h1")
        m1 = self.tfs.get("m1")
        m5 = self.tfs.get("m5")

        if not htf_bias or htf_bias.bias == "NEUTRAL":
            return False

        ltf_votes = [t for t in [m1, m5] if t and t.bias != "NEUTRAL"]
        if not ltf_votes:
            return False

        return any(t.bias != htf_bias.bias for t in ltf_votes)

    def summary(self) -> str:
        def _short(bias: str) -> str:
            if "BULLISH" in bias.upper(): return "BUL"
            if "BEARISH" in bias.upper(): return "BEA"
            return "NEU"

        order = ["h4", "h1", "m30", "m15", "m5", "m1"]
        parts = []
        for k in order:
            v = self.tfs.get(k)
            if v is None:
                continue
            item = f"{k.upper()}:{_short(v.bias)}"
            if v.rejection != "NONE":
                item += "!"   # visual indicator for rejection wick
            if v.at_zone != "NONE":
                item += "*"   # visual indicator for zone hit
            parts.append(item)

        return (
            " | ".join(parts) +
            f" -> {self.direction or 'NEUTRAL'} "
            f"[conf={self.confidence:.0%} bull={self.bull_score:.1f} bear={self.bear_score:.1f}]"
        )


class MultiTFAnalyzer:
    """Analyzes all timeframes and declares the best trade direction."""

    def __init__(self):
        self.trend        = TrendLogic()
        self.last_result: Optional[MultiTFAnalysis] = None
        self._last_time:  float = 0.0
        self._cache: Dict[str, Tuple[float, TFTrend]] = {}  # tf -> (timestamp, TFTrend)
        from core.structure.smc_logic import SMCLogic
        self._smc = SMCLogic()

    def _analyze_tf(self, df: Optional[pd.DataFrame], tf_name: str) -> Optional[TFTrend]:
        if df is None or len(df) < 50:
            return None
        try:
            result = self.trend.analyze_trend(df)
            price  = float(df["close"].iloc[-1])

            # Detect HTF order-block zone hit
            obs     = self._smc.get_order_blocks(df)
            at_zone = "NONE"
            if obs:
                for ob in obs[-3:]:
                    lo, hi = ob["low"], ob["high"]
                    # 0.2 pt tolerance to catch prices touching the edge of a zone
                    if (lo - 0.2) <= price <= (hi + 0.2):
                        at_zone = "BULLISH_OB" if ob["type"] == "BULLISH" else "BEARISH_OB"
                        break

            return TFTrend(
                tf        = tf_name,
                bias      = result.get("bias", "NEUTRAL"),
                adx       = float(result.get("adx", 20.0)),
                ema_cross = bool(result.get("ema_cross", False)),
                at_zone   = at_zone,
                rejection = result.get("rejection", "NONE"),
            )
        except Exception as e:
            logger.debug(f"MultiTF: failed to analyze {tf_name}: {e}")
            return None

    def analyze(
        self,
        df_h4:  pd.DataFrame,
        df_h1:  pd.DataFrame,
        df_m30: Optional[pd.DataFrame],
        df_m15: pd.DataFrame,
        df_m5:  pd.DataFrame,
        df_m1:  pd.DataFrame,
    ) -> MultiTFAnalysis:
        """
        Analyze all TFs (with per-TF TTL caching) and return the confluence result.
        """
        now     = time.time()
        tf_data = {
            "h4": df_h4, "h1": df_h1, "m30": df_m30,
            "m15": df_m15, "m5": df_m5, "m1": df_m1,
        }

        tfs: Dict[str, TFTrend] = {}
        for tf_name, df in tf_data.items():
            if df is None:
                continue

            ttl                         = TF_TTL.get(tf_name, 0)
            cached_time, cached_obj     = self._cache.get(tf_name, (0.0, None))

            if cached_obj is not None and (now - cached_time) < ttl:
                tfs[tf_name] = cached_obj
            else:
                trend_obj = self._analyze_tf(df, tf_name)
                if trend_obj is not None:
                    tfs[tf_name]             = trend_obj
                    self._cache[tf_name]     = (now, trend_obj)

        bull_score = sum(t.score for t in tfs.values() if t.bias == "BULLISH")
        bear_score = sum(t.score for t in tfs.values() if t.bias == "BEARISH")

        analysis          = MultiTFAnalysis(tfs, float(bull_score), float(bear_score))
        self.last_result  = analysis
        self._last_time   = now

        logger.info(
            f"Multi-TF: {analysis.summary()} "
            f"strong={analysis.is_strong} htf_aligned={analysis.htf_aligned}"
        )

        return analysis
