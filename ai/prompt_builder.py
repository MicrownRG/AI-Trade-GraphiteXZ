"""
AI Prompt Builder.

Constructs structured prompts for the LLM scorer.
The AI evaluates trade context — it does NOT generate signals.
It acts as an independent second opinion to filter weak setups.
"""
from __future__ import annotations
from typing import Optional
import json

from core.signal.signal_engine import TradeSignal
from core.structure.htf_bias import HTFBias


SYSTEM_PROMPT = """You are a senior institutional trading analyst specialising in XAUUSD (Gold).
Your role is to evaluate trade setups — not generate them.

Given a trade context, you must output a JSON object with EXACTLY this structure:
{
  "confidence": <float 0.0 to 1.0>,
  "decision": "<TAKE or SKIP>",
  "reason": "<concise 1-2 sentence explanation>"
}

Evaluation criteria:
- HTF bias must be clear and recent (within last 10 candles)
- Liquidity sweep must have significant wick ratio (>= 0.5 preferred)
- Displacement must align with bias direction
- Session must be London or New York
- Avoid setups with conflicting multi-timeframe signals
- Avoid extremely high volatility (news events)
- Risk/reward must justify trade execution

Be strict. Confidence >= 0.75 = high quality. 0.60–0.75 = marginal. < 0.60 = skip.
Respond with JSON only. No markdown, no preamble."""


def build_evaluation_prompt(signal: TradeSignal) -> str:
    """Build the user-turn message for AI evaluation."""

    sweep_info = "None detected"
    if signal.sweep:
        sweep_info = (
            f"direction={signal.sweep.direction} "
            f"swept_level={signal.sweep.swept_level:.2f} "
            f"wick_ratio={signal.sweep.wick_ratio:.2f}"
        )

    displacement_info = "None detected"
    if signal.displacement:
        displacement_info = (
            f"direction={signal.displacement.direction} "
            f"body_ratio={signal.displacement.body_ratio:.2f} "
            f"move_pips={signal.displacement.move_pips:.1f}"
        )

    choch_info = "None"
    if signal.choch:
        choch_info = f"direction={signal.choch.direction} level={signal.choch.broken_level:.2f}"

    htf = signal.htf_bias
    htf_info = "Unknown"
    if htf:
        htf_info = (
            f"direction={htf.direction} "
            f"confidence={htf.confidence:.2f} "
            f"swing_structure={htf.swing_structure} "
            f"summary='{htf.trend_str}'"
        )

    context = {
        "symbol":        signal.symbol,
        "timestamp":     signal.timestamp.isoformat(),
        "direction":     signal.direction,
        "session":       signal.session,
        "entry":         signal.entry_price,
        "stop_loss":     signal.stop_loss,
        "take_profit":   signal.take_profit,
        "rr_ratio":      round(signal.rr_ratio, 2),
        "signal_score":  f"{signal.score}/{signal.max_score}",
        "score_detail":  signal.score_breakdown,
        "htf_bias":      htf_info,
        "liquidity_sweep": sweep_info,
        "displacement":  displacement_info,
        "choch":         choch_info,
        "atr_pips":      round(signal.atr_pips, 1),
    }

    return (
        "Evaluate the following XAUUSD trade setup:\n\n"
        + json.dumps(context, indent=2)
        + "\n\nRespond with JSON only."
    )
