"""
AI Scorer.

Wraps the multi-provider AI client to score trade signals.
In backtest mode (AI disabled), falls back to rule-based confidence scoring.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

from core.signal.signal_engine import TradeSignal
from ai.prompt_builder import SYSTEM_PROMPT, build_evaluation_prompt
from ai.client import call_ai
from ai.providers.registry import get_active_provider_name
from config.ai_config import AI_CONFIG
from config.risk_config import RISK_CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class AIEvaluation:
    confidence: float
    decision:   str        # "TAKE" | "SKIP"
    reason:     str
    source:     str        # "ai" | "rule_based"
    provider:   str = ""   # "deepseek" | "anthropic" | "gemini" | "rule_based"


class AIScorer:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def evaluate(self, signal: TradeSignal) -> AIEvaluation:
        """
        Evaluate a trade signal.
        If AI is disabled (backtest mode), uses rule-based fallback.
        """
        if not self.enabled:
            return self._rule_based_pass(signal)

        prompt  = build_evaluation_prompt(signal)
        result  = call_ai(system=SYSTEM_PROMPT, user=prompt)

        if result is None:
            logger.warning("AI evaluation failed — falling back to rule-based")
            return self._rule_based(signal)

        confidence = float(result.get("confidence", 0.0))
        decision   = str(result.get("decision", "SKIP")).upper()
        reason     = str(result.get("reason", "No reason provided"))
        provider   = get_active_provider_name()

        # Ensure decision is consistent with confidence
        if confidence >= AI_CONFIG.confidence_threshold and decision == "SKIP":
            decision = "TAKE"
        elif confidence < AI_CONFIG.confidence_threshold and decision == "TAKE":
            decision = "SKIP"

        eval_ = AIEvaluation(
            confidence = round(confidence, 3),
            decision   = decision,
            reason     = reason,
            source     = "ai",
            provider   = provider,
        )
        logger.info(
            f"AI eval [{signal.signal_id}] via {provider}: "
            f"{decision} conf={confidence:.2f} | {reason[:80]}"
        )
        return eval_

    @staticmethod
    def _rule_based_pass(signal: TradeSignal) -> AIEvaluation:
        """
        When USE_AI=false: always TAKE if signal passed engine scoring.
        The signal engine threshold is the single gate — no double-filtering.
        """
        score    = signal.score
        max_score = signal.max_score or 10
        conf     = round(min(1.0, score / max_score), 3)
        return AIEvaluation(
            confidence = conf,
            decision   = "TAKE",
            reason     = f"AI disabled — auto-TAKE (score={score}/{max_score})",
            source     = "rule_based",
            provider   = "rule_based",
        )

    @staticmethod
    def _rule_based(signal: TradeSignal) -> AIEvaluation:
        """
        Deterministic fallback scorer — used when API call fails.
        """
        score     = signal.score
        max_score = signal.max_score or 10
        raw_conf  = score / max_score

        if signal.sweep and signal.displacement:
            raw_conf = min(1.0, raw_conf + 0.10)

        if signal.session not in ("London", "New York", "Overlap", "Asian"):
            raw_conf *= 0.70

        if signal.htf_bias and signal.htf_bias.confidence >= 0.80:
            raw_conf = min(1.0, raw_conf + 0.05)

        decision = "TAKE" if raw_conf >= RISK_CONFIG.min_ai_confidence else "SKIP"

        return AIEvaluation(
            confidence = round(raw_conf, 3),
            decision   = decision,
            reason     = f"Rule-based: score={score}/{max_score} session={signal.session}",
            source     = "rule_based",
            provider   = "rule_based",
        )
