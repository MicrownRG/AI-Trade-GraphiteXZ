"""
AI Adaptive Learning Engine.

Acts as a "machine-learning trader" that:
  1. After each session, analyses closed trades + missed signals with AI.
  2. Derives adaptive score-weight offsets per context bucket
     (session × HTF bias × news_active).
  3. Writes offsets to knowledge/adaptive_weights.json so the signal
     engine can bias its scoring each cycle.
  4. Suggests TP multiplier adjustments based on historical SL+ hit rate
     (if SL+ is hit more often than TP2, TP may be too ambitious).

Toggle:
  TRADING_CONFIG.ai_adapter_enabled = True | False
  /adapter command in Telegram calls toggle_adapter().

Knowledge file schema (knowledge/adaptive_weights.json):
{
  "last_updated": "2026-04-14T10:00:00",
  "overall_grade": "B",
  "summary": "...",
  "score_offsets": {
    "<session>_<htf_bias>_<news>": {
        "htf_alignment":   +1,
        "liquidity_sweep": -1,
        ...
    }
  },
  "tp_multiplier": {
    "<session>_<htf_bias>_<news>": 1.15
  },
  "sl_plus_tp_recal_count": 12,
  "patterns": [...]
}
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from database.repository import TradeRepository
from ai.client import call_ai_raw
from config.trading_config import TRADING_CONFIG

logger = logging.getLogger(__name__)

LEARNINGS_FILE = Path("knowledge/ai_learnings.json")
WEIGHTS_FILE   = Path("knowledge/adaptive_weights.json")

# ── AI system prompt ───────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """
You are the Lead Performance AI for an institutional XAUUSD trading bot.
Your role is to analyse past trade data and provide ACTIONABLE adjustments
that improve the win-rate (WR) and reduce unnecessary losses.

Input datasets:
  1. closed_trades  — list of recent closed trades with PnL, signal context, session, HTF bias
  2. missed_signals — high-score signals that never became trades
  3. trade_notes    — structural contra-exit and SL+ TP recalibration records
  4. current_weights— existing adaptive score-weight offsets (may be empty)

Analysis goals:
  A. Identify CONTEXT BUCKETS (session × HTF bias × news) with poor WR.
  B. Recommend score-weight OFFSETS for each component in bad buckets.
     Offsets are integers, range -3 to +3.
  C. Recommend TP MULTIPLIER adjustments (0.8–1.5) if SL+ is triggered
     frequently before TP — indicating TP targets are too ambitious.
  D. Flag missed breakouts where "extreme retracement" gate prevented entry.
  E. Provide an overall performance grade (A/B/C/D/F) and 3-sentence summary.

Output MUST be valid JSON with this exact structure:
{
  "overall_grade": "B",
  "summary": "...",
  "score_offsets": {
    "NY_BEARISH_news_on":  {"htf_alignment": 1, "liquidity_sweep": -1},
    "ASIA_BULLISH_news_off": {"displacement": 1}
  },
  "tp_multiplier": {
    "NY_BEARISH_news_on": 0.85,
    "OVERLAP_NEUTRAL_news_on": 0.80
  },
  "patterns": [
    {
      "type": "LOSING",
      "insight": "NY session BEARISH setups have 35% WR — HTF against entry",
      "recommendation": "Increase htf_alignment weight +1 in NY bearish context",
      "score_impact": -1
    }
  ]
}
"""


class LearningEngine:
    """
    Retrospective + adaptive learning engine.

    Usage:
      engine = LearningEngine(repo)
      result = engine.run_retrospective()      # runs AI analysis, updates weights
      offsets = engine.get_score_offsets(ctx)  # returns weight offsets for current context
      tp_mult = engine.get_tp_multiplier(ctx)  # returns TP multiplier suggestion
    """

    def __init__(self, repo: TradeRepository):
        self.repo = repo
        LEARNINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        WEIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # ── Retrospective analysis ────────────────────────────────────────────────

    def run_retrospective(self, trade_limit: int = 30) -> Dict:
        """
        Fetch trade history, run AI analysis, persist adaptive weights.
        Called once per day (or on /eod command).
        """
        if not TRADING_CONFIG.ai_adapter_enabled:
            logger.info("AI Adapter disabled — skipping retrospective")
            return {"summary": "AI Adapter is OFF.", "patterns": []}

        closed_trades  = self.repo.get_detailed_trade_history(limit=trade_limit)
        missed_signals = self.repo.get_untriggered_signals(limit=15)

        # Gather SL+ TP recalibration notes
        trade_notes = []
        for t in closed_trades:
            tid = t.get("trade_id", "")
            if tid:
                notes = self.repo.get_trade_notes(tid)
                trade_notes.extend(notes)

        if not closed_trades and not missed_signals:
            return {"summary": "No data available for analysis yet.", "patterns": []}

        current_weights = self._load_weights()

        payload = {
            "closed_trades":   closed_trades,
            "missed_signals":  missed_signals,
            "trade_notes":     trade_notes,
            "current_weights": current_weights.get("score_offsets", {}),
            "metadata": {
                "timestamp": datetime.now().isoformat(),
                "symbol":    "XAUUSD",
            },
        }

        user_prompt = f"Analyse this session data and return adaptive adjustments:\n{json.dumps(payload)}"
        logger.info(
            f"Running AI Retrospective: {len(closed_trades)} trades, "
            f"{len(missed_signals)} missed, {len(trade_notes)} notes"
        )

        try:
            result = call_ai_raw(system=_SYSTEM_PROMPT, user=user_prompt)
            if isinstance(result, str):
                clean  = result.replace("```json", "").replace("```", "").strip()
                result = json.loads(clean)

            self._merge_and_save(result)
            return result

        except Exception as e:
            logger.error(f"AI Retrospective failed: {e}")
            return {"summary": f"AI analysis error: {e}", "patterns": []}

    # ── Runtime accessors (used by signal engine each cycle) ─────────────────

    def get_score_offsets(self, session: str, htf_bias: str, news_active: bool) -> Dict[str, int]:
        """
        Return score-weight offsets for the current context bucket.
        Returns empty dict if AI Adapter is disabled or no data.

        Context key format: "<SESSION>_<HTF_BIAS>_<news_on|news_off>"
        """
        if not TRADING_CONFIG.ai_adapter_enabled:
            return {}
        weights = self._load_weights()
        key     = _ctx_key(session, htf_bias, news_active)
        return dict(weights.get("score_offsets", {}).get(key, {}))

    def get_tp_multiplier(self, session: str, htf_bias: str, news_active: bool) -> float:
        """
        Return TP multiplier suggestion for the current context.
        Returns 1.0 (no change) when adapter is off or key not found.
        """
        if not TRADING_CONFIG.ai_adapter_enabled:
            return 1.0
        weights = self._load_weights()
        key     = _ctx_key(session, htf_bias, news_active)
        return float(weights.get("tp_multiplier", {}).get(key, 1.0))

    def get_learnings(self) -> Dict:
        """Load the last AI analysis (for /eod Telegram display)."""
        if not LEARNINGS_FILE.exists():
            return {"patterns": [], "summary": "", "last_updated": ""}
        try:
            with open(LEARNINGS_FILE) as f:
                return json.load(f)
        except Exception:
            return {"patterns": [], "summary": "", "last_updated": ""}

    # ── Toggle ────────────────────────────────────────────────────────────────

    @staticmethod
    def toggle_adapter() -> bool:
        """Toggle ai_adapter_enabled on the global config. Returns the new state."""
        TRADING_CONFIG.ai_adapter_enabled = not TRADING_CONFIG.ai_adapter_enabled
        state = TRADING_CONFIG.ai_adapter_enabled
        logger.info(f"AI Adapter toggled {'ON' if state else 'OFF'}")
        return state

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load_weights(self) -> Dict:
        if not WEIGHTS_FILE.exists():
            return {}
        try:
            with open(WEIGHTS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def _merge_and_save(self, analysis: Dict) -> None:
        """
        Merge new AI analysis into the persistent weights file.
        Score offsets are averaged with existing values (exponential smoothing α=0.4)
        so a single bad session does not swing weights wildly.
        """
        existing = self._load_weights()
        alpha = 0.4   # weight for new observation

        # Merge score_offsets
        new_offsets  = analysis.get("score_offsets", {})
        merged_off   = dict(existing.get("score_offsets", {}))
        for ctx, offsets in new_offsets.items():
            if ctx not in merged_off:
                merged_off[ctx] = {}
            for component, val in offsets.items():
                old_val = merged_off[ctx].get(component, 0)
                merged_off[ctx][component] = round(
                    (1 - alpha) * old_val + alpha * val
                )

        # Merge tp_multiplier
        new_tp  = analysis.get("tp_multiplier", {})
        merged_tp = dict(existing.get("tp_multiplier", {}))
        for ctx, mult in new_tp.items():
            old_mult = merged_tp.get(ctx, 1.0)
            merged_tp[ctx] = round((1 - alpha) * old_mult + alpha * float(mult), 3)

        updated = {
            "last_updated":  datetime.now().isoformat(),
            "overall_grade": analysis.get("overall_grade", "?"),
            "summary":       analysis.get("summary", ""),
            "score_offsets": merged_off,
            "tp_multiplier": merged_tp,
            "patterns":      analysis.get("patterns", []),
        }

        with open(WEIGHTS_FILE, "w") as f:
            json.dump(updated, f, indent=2)
        logger.info(f"Adaptive weights saved to {WEIGHTS_FILE}")

        # Also save full learnings for /eod display
        with open(LEARNINGS_FILE, "w") as f:
            json.dump(updated, f, indent=2)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ctx_key(session: str, htf_bias: str, news_active: bool) -> str:
    """Build the context bucket key used in the weights dict."""
    sess = (session or "DEFAULT").upper()
    bias = (htf_bias or "NEUTRAL").upper()
    news = "news_on" if news_active else "news_off"
    return f"{sess}_{bias}_{news}"
