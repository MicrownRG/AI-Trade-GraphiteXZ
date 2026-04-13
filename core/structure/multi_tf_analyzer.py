"""
Multi-Timeframe Trend Confluence Analyzer.

Analyzes trend direction across all timeframes (M1, M5, M15, M30, H1, H4)
and returns the best trade direction with confidence score.

Strategy:
- Each TF votes BULLISH, BEARISH, or NEUTRAL
- Votes are weighted by TF importance (higher TF = more weight)
- Confluence score determines direction confidence
- Always executes entry on M1 (execution TF)

TF Weights:
  H4: 8  (Master Trend - Anchor)
  H1: 6  (Intermediate Trend - Confirmation)
  M30: 4 (Sub-trend)  
  M15: 3 (Zone reference)
  M5:  2 (Trigger momentum)
  M1:  1 (Execution timing)
"""
from __future__ import annotations
import time
import pandas as pd
import numpy as np
from typing import Dict, Optional, Tuple

from core.structure.trend_logic import TrendLogic
from utils.logger import get_logger

logger = get_logger(__name__)

# Result cache TTL: don't re-run full multi-TF analysis more often than this
_ANALYZE_TTL_SECONDS = 25

# Timeframe weights: higher TF gets more weight
TF_WEIGHTS = {
    "m1":  1,
    "m5":  2,
    "m15": 3,
    "m30": 4,
    "h1":  6,
    "h4":  8,
}

MIN_CONFLUENCE_SCORE = 6   # Out of max 21 possible points
MIN_STRONG_SCORE     = 12  # For "strong" confluences like 4743 type

# Tiered TTL Caching (seconds)
# HTF: rarely changes, cache more. LTF: high speed, cache less/none.
TF_TTL = {
    "h4":  60,
    "h1":  60,
    "m30": 30,
    "m15": 30,
    "m5":  3,  # Short burst cache
    "m1":  0   # Real-time
}


class TFTrend:
    """Trend result for a single timeframe."""
    __slots__ = ("tf", "bias", "adx", "ema_cross", "weight", "score", "at_zone", "rejection")

    def __init__(self, tf: str, bias: str, adx: float, ema_cross: bool, at_zone: str = "NONE", rejection: str = "NONE"):
        self.tf        = tf
        self.bias      = bias           # "BULLISH" | "BEARISH" | "NEUTRAL"
        self.adx       = adx
        self.ema_cross = ema_cross
        self.at_zone   = at_zone        # "BULLISH_OB" | "BEARISH_OB" | "NONE"
        self.rejection = rejection     # "BULLISH" | "BEARISH" | "NONE"
        self.weight    = TF_WEIGHTS.get(tf.lower(), 1)
        
        # Score this TF's vote
        strength = 1.5 if adx > 25 else 1.0
        self.score = self.weight * strength if bias != "NEUTRAL" else 0
        
        # Rejection Bonus (Ekor Panjang): Count for extra weight if it matches bias
        if rejection != "NONE" and rejection == bias:
            self.score += self.weight * 1.5 # Significant boost for wicks
        
        # Zone Bonus: If hitting a zone, it's worth more points for confluence
        if at_zone != "NONE":
            self.score += self.weight * 0.5

    def __repr__(self):
        return f"{self.tf.upper()}:{self.bias}(adx={self.adx:.0f})"


class MultiTFAnalysis:
    """Full multi-TF confluence result."""

    def __init__(
        self,
        tfs: Dict[str, TFTrend],  # key = "m1", "m5", etc.
        bull_score: float,
        bear_score: float,
    ):
        self.tfs        = tfs
        self.bull_score = bull_score
        self.bear_score = bear_score

    @property
    def master_bias(self) -> str:
        """The 'Patokan Besar' (H4/H1 consensus)."""
        h4 = self.tfs.get("h4")
        h1 = self.tfs.get("h1")
        m30 = self.tfs.get("m30")

        # 1. Consensus
        if h4 and h1 and h4.bias == h1.bias and h4.bias != "NEUTRAL":
            return h4.bias
            
        # 2. Priority of trending TF
        if h4 and h4.bias != "NEUTRAL": return h4.bias
        if h1 and h1.bias != "NEUTRAL": return h1.bias
        
        # 3. Fallback to M30 if HTF is dead
        if m30 and m30.bias != "NEUTRAL": return m30.bias
        
        return "NEUTRAL"

    @property
    def direction(self) -> Optional[str]:
        """Primary trade direction anchoring on HTF potential & Zone Hits."""
        master = self.master_bias
        
        # 1. Zone Hit Override (Counter-trend Support/Resistance)
        # If hitting a strong HTF Bearish OB while bullish, we might want to SHORT
        h4 = self.tfs.get("h4")
        h1 = self.tfs.get("h1")
        
        # Bearish Reversal Setup (Hit H4/H1 Resis + LTF Bearish)
        hitting_resis = (h4 and h4.at_zone == "BEARISH_OB") or (h1 and h1.at_zone == "BEARISH_OB")
        if hitting_resis and self.bear_score >= MIN_STRONG_SCORE and self.bear_score > self.bull_score:
            return "sell"
            
        # Bullish Reversal Setup (Hit H4/H1 Support + LTF Bullish)
        hitting_supp = (h4 and h4.at_zone == "BULLISH_OB") or (h1 and h1.at_zone == "BULLISH_OB")
        if hitting_supp and self.bull_score >= MIN_STRONG_SCORE and self.bull_score > self.bear_score:
            return "buy"
            
        # 2. Standard Trend Following
        if master == "BULLISH" and self.bull_score > self.bear_score:
            return "buy"
        if master == "BEARISH" and self.bear_score > self.bull_score:
            return "sell"
            
        # 3. High Confluence Override (Trade based on score even if master is unclear)
        # If Bull score is double Bear score and meets absolute threshold
        threshold = MIN_CONFLUENCE_SCORE + 4 # 10 pts
        if self.bull_score >= threshold and self.bull_score > self.bear_score * 2.5:
            return "buy"
        if self.bear_score >= threshold and self.bear_score > self.bull_score * 2.5:
            return "sell"
            
        return None

    @property
    def confidence(self) -> float:
        """0.0 – 1.0 confidence based on score ratio."""
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
        """H4 and H1 agree on same direction."""
        h4 = self.tfs.get("h4")
        h1 = self.tfs.get("h1")
        if h4 and h1:
            return h4.bias == h1.bias and h4.bias != "NEUTRAL"
        return False

    @property
    def reversal_detected(self) -> bool:
        """
        LTF trending opposite to HTF — potential reversal or strong retracement.
        Example: H4=BULLISH but M1/M5 going BEARISH → possible pullback or flip.
        """
        htf_bias = self.tfs.get("h4", self.tfs.get("h1"))
        m1 = self.tfs.get("m1")
        m5 = self.tfs.get("m5")
        if not htf_bias or htf_bias.bias == "NEUTRAL":
            return False
        ltf_votes = [t for t in [m1, m5] if t and t.bias != "NEUTRAL"]
        if not ltf_votes:
            return False
        # If any fast TF disagrees with HTF
        return any(t.bias != htf_bias.bias for t in ltf_votes)

    def summary(self) -> str:
        def _short(bias: str) -> str:
            if "BULLISH" in bias.upper(): return "BUL"
            if "BEARISH" in bias.upper(): return "BEA"
            return "NEU"
        
        parts = []
        for k, v in sorted(self.tfs.items(), key=lambda x: list(TF_WEIGHTS.keys()).index(x[0]), reverse=True):
            item = f"{k.upper()}:{_short(v.bias)}"
            if v.rejection != "NONE":
                item += "!" # Visual indicator for 'Ekor Panjang'
            parts.append(item)
            
        return " | ".join(parts) + f" \u2192 {self.direction or 'NEUTRAL'} conf={self.confidence:.0%}"


class MultiTFAnalyzer:
    """Analyzes all timeframes and declares best trade direction."""

    def __init__(self):
        self.trend       = TrendLogic()
        self.last_result: Optional[MultiTFAnalysis] = None
        self._last_analyze_time: float = 0.0
        self._cache: Dict[str, Tuple[float, TFTrend]] = {} # tf -> (timestamp, trend_obj)
        # Lazily import SMCLogic once and reuse
        from core.structure.smc_logic import SMCLogic
        self._smc = SMCLogic()

    def _analyze_tf(self, df: Optional[pd.DataFrame], tf_name: str) -> Optional[TFTrend]:
        if df is None or len(df) < 30:
            return None
        try:
            result = self.trend.analyze_trend(df)
            
            # Detect Zone Hit (Support/Resistance) using cached SMCLogic instance
            price = df['close'].iloc[-1]
            obs   = self._smc.get_order_blocks(df)
            
            at_zone = "NONE"
            if obs:
                for ob in obs[-3:]:
                    lo, hi = ob["low"], ob["high"]
                    if (lo - 0.2) <= price <= (hi + 0.2):
                        at_zone = "BULLISH_OB" if ob["type"] == "BULLISH" else "BEARISH_OB"
                        break

            return TFTrend(
                tf=tf_name,
                bias=result.get("bias", "NEUTRAL"),
                adx=float(result.get("adx", 20)),
                ema_cross=bool(result.get("ema_cross", False)),
                at_zone=at_zone,
                rejection=result.get("rejection", "NONE")
            )
        except Exception as e:
            logger.debug(f"MultiTF: Failed to analyze {tf_name}: {e}")
            return None

    def analyze(
        self,
        df_h4:  pd.DataFrame,
        df_h1:  pd.DataFrame,
        df_m30: Optional[pd.DataFrame],
        df_m15: pd.DataFrame,
        df_m5:  pd.DataFrame,
        df_m1:  pd.DataFrame
    ) -> MultiTFAnalysis:
        """Main entry point: analyzes all TFs (cached or new) and returns confluence."""
        now = time.time()
        
        tf_data = {
            "h4":  df_h4,
            "h1":  df_h1,
            "m30": df_m30,
            "m15": df_m15,
            "m5":  df_m5,
            "m1":  df_m1
        }
        
        tfs = {}
        for tf_name, df in tf_data.items():
            if df is None: continue
            
            # ── Tiered Cache Lookup ──
            ttl = TF_TTL.get(tf_name, 0)
            cached_time, cached_obj = self._cache.get(tf_name, (0.0, None))
            
            if cached_obj and (now - cached_time < ttl):
                tfs[tf_name] = cached_obj
            else:
                # Cache miss or expired: Recalculate
                trend_obj = self._analyze_tf(df, tf_name)
                if trend_obj:
                    tfs[tf_name] = trend_obj
                    self._cache[tf_name] = (now, trend_obj)
        
        bull_score = sum(t.score for t in tfs.values() if t.bias == "BULLISH")
        bear_score = sum(t.score for t in tfs.values() if t.bias == "BEARISH")

        analysis = MultiTFAnalysis(tfs, float(bull_score), float(bear_score))
        self.last_result = analysis
        self._last_analyze_time = now

        logger.info(
            f"📊 Multi-TF Confluence: {analysis.summary()} "
            f"[Bull={bull_score:.1f} Bear={bear_score:.1f} Strong={analysis.is_strong}]"
        )

        return analysis
