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
    __slots__ = ("tf", "bias", "adx", "ema_cross", "weight", "score",
                 "at_zone", "rejection", "fibo_golden", "fibo_retrace_pct",
                 "candle_pattern", "vol_ratio")

    def __init__(
        self,
        tf: str,
        bias: str,
        adx: float,
        ema_cross: bool,
        at_zone: str = "NONE",
        rejection: str = "NONE",
        fibo_golden: str = "NONE",      # "BULLISH" | "BEARISH" | "NONE"
        fibo_retrace_pct: float = 0.0,  # current retrace %
        candle_pattern: str = "",        # detected pattern name(s) aligned with bias
        vol_ratio: float = 1.0,         # current vol / avg vol
    ):
        self.tf        = tf
        self.bias      = bias       # "BULLISH" | "BEARISH" | "NEUTRAL"
        self.adx       = adx
        self.ema_cross = ema_cross
        self.at_zone   = at_zone    # "BULLISH_OB" | "BEARISH_OB" | "NONE"
        self.rejection = rejection  # "BULLISH" | "BEARISH" | "NONE"
        self.fibo_golden = fibo_golden
        self.fibo_retrace_pct = fibo_retrace_pct
        self.candle_pattern = candle_pattern
        self.vol_ratio = vol_ratio
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

        # Fibo golden pocket bonus (price at 61.8–78.6% retracement aligned to bias)
        if fibo_golden != "NONE" and fibo_golden == bias:
            self.score += self.weight * 0.6

        # Candle pattern bonus — HTF patterns (H4/H1) carry more conviction
        if candle_pattern:
            self.score += self.weight * 0.4

        # Volume confirmation bonus — high volume validates the TF signal
        if vol_ratio >= 1.5:
            self.score += self.weight * 0.3

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
                item += "!"   # rejection wick
            if v.at_zone != "NONE":
                item += "*"   # OB/FVG zone hit
            if v.fibo_golden != "NONE":
                item += "φ"   # Fibo golden pocket (61.8-78.6%)
            if v.candle_pattern:
                item += "🕯"  # candle pattern detected
            if v.vol_ratio >= 1.5:
                item += "V"   # high volume
            parts.append(item)

        return (
            " | ".join(parts) +
            f" -> {self.direction or 'NEUTRAL'} "
            f"[conf={self.confidence:.0%} bull={self.bull_score:.1f} bear={self.bear_score:.1f}]"
        )


    def find_multi_tf_sr(self, tf_dataframes: Dict[str, pd.DataFrame], price: float, tolerance_pts: float = 3.0) -> list:
        """
        Find S/R levels confirmed by multiple timeframes.
        Returns list of dicts: {price, kind, tf_count, tfs, distance}.
        Levels within tolerance_pts of each other are merged.
        """
        from core.structure.swing import detect_swings
        all_levels = []
        for tf_name, df in tf_dataframes.items():
            if df is None or len(df) < 30:
                continue
            try:
                swings = detect_swings(df.tail(120))
                for s in swings[-10:]:
                    all_levels.append({"price": s.price, "kind": s.kind, "tf": tf_name})
            except Exception:
                continue

        if not all_levels:
            return []

        all_levels.sort(key=lambda x: x["price"])
        clusters = []
        used = set()
        for i, lv in enumerate(all_levels):
            if i in used:
                continue
            cluster = [lv]
            used.add(i)
            for j in range(i + 1, len(all_levels)):
                if j in used:
                    continue
                if abs(all_levels[j]["price"] - lv["price"]) <= tolerance_pts:
                    cluster.append(all_levels[j])
                    used.add(j)
            if len(set(c["tf"] for c in cluster)) >= 2:
                avg_price = sum(c["price"] for c in cluster) / len(cluster)
                kind = "resistance" if sum(1 for c in cluster if c["kind"] == "high") > len(cluster) / 2 else "support"
                tfs = sorted(set(c["tf"] for c in cluster))
                clusters.append({
                    "price": round(avg_price, 2),
                    "kind": kind,
                    "tf_count": len(tfs),
                    "tfs": tfs,
                    "distance": round(abs(price - avg_price), 2),
                })
        clusters.sort(key=lambda x: x["distance"])
        return clusters[:5]


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
            bias_current = result.get("bias", "NEUTRAL")

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

            # Fibo golden pocket detection (61.8–78.6% retrace aligned with bias)
            fibo_golden = "NONE"
            fibo_retrace_pct = 0.0
            try:
                from core.structure.swing import detect_swings
                sw = detect_swings(df.tail(100))
                highs = [s for s in sw if s.kind == 'high']
                lows  = [s for s in sw if s.kind == 'low']
                if highs and lows:
                    sh = highs[-1]
                    sl_pt = lows[-1]
                    rng = sh.price - sl_pt.price
                    if rng > 0.5:
                        # Bullish setup: most recent low came AFTER high won't apply;
                        # we use last swing as the impulse; price retracing into
                        # golden pocket means continuation in bias direction.
                        if sh.index > sl_pt.index:
                            # Upswing: retrace measured downward from swing_high
                            retrace = (sh.price - price) / rng
                            if 0.618 <= retrace <= 0.786:
                                fibo_golden = "BULLISH"
                                fibo_retrace_pct = retrace
                        else:
                            # Downswing: retrace measured upward from swing_low
                            retrace = (price - sl_pt.price) / rng
                            if 0.618 <= retrace <= 0.786:
                                fibo_golden = "BEARISH"
                                fibo_retrace_pct = retrace
            except Exception:
                pass

            # Candle pattern detection per TF
            _cp_name = ""
            try:
                from core.signal.candle_patterns import detect_patterns, get_pattern_names
                _bias_dir = "buy" if bias_current == "BULLISH" else ("sell" if bias_current == "BEARISH" else "")
                if _bias_dir:
                    _pats = detect_patterns(df)
                    _cp_name = get_pattern_names(_pats, _bias_dir)
            except Exception:
                pass

            # Volume ratio (current bar vs 20-bar avg)
            _vol_ratio = 1.0
            try:
                _vol_col = "tick_volume" if "tick_volume" in df.columns else ("volume" if "volume" in df.columns else None)
                if _vol_col and len(df) >= 20:
                    _v_avg = df[_vol_col].tail(20).mean()
                    _v_cur = df[_vol_col].iloc[-2]  # last closed bar
                    _vol_ratio = _v_cur / _v_avg if _v_avg > 0 else 1.0
            except Exception:
                pass

            return TFTrend(
                tf        = tf_name,
                bias      = bias_current,
                adx       = float(result.get("adx", 20.0)),
                ema_cross = bool(result.get("ema_cross", False)),
                at_zone   = at_zone,
                rejection = result.get("rejection", "NONE"),
                fibo_golden = fibo_golden,
                fibo_retrace_pct = fibo_retrace_pct,
                candle_pattern = _cp_name,
                vol_ratio = _vol_ratio,
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
