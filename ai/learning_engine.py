"""
Learning Engine — AI Retrospective Analysis.

This module analyzes past closed trades to identify winning/losing patterns
and provide feedback to the signal engine.
"""
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional
import pandas as pd

from database.repository import TradeRepository
from ai.client import call_ai_raw
from utils.logger import get_logger

logger = get_logger(__name__)

LEARNINGS_FILE = Path("knowledge/ai_learnings.json")

SYSTEM_LEARNING_PROMPT = """
You are a Lead Trading Strategist and Performance AI for an Institutional SMC/ICT Gold (XAUUSD) bot.
Your task is to analyze two datasets from the recent session:
1. CLOSED TRADES: Actual entries with PnL and the signal logic that triggered them.
2. UNTRIGGERED SIGNALS: Missed opportunities where a signal was high-score but no trade happened.

CRITICAL ANALYSIS GOALS:
- ID toxic sessions: Are we losing consistently in Asia?
- ID score efficacy: Are high scores (12+) failing? Why? (e.g., "Hit SL before TP")
- ID missed breakouts: Are we missing big moves because we wait for an 'Extreme' retracement that never comes?
- ID structural failure: Is our M1/M5 OB detection reliable?

Output MUST be a valid JSON with this structure:
{
    "patterns": [
        {"type": "WINNING/LOSING/MISSED", "insight": "Concise detail", "recommendation": "Engine tweak", "score_impact": -2}
    ],
    "summary": "High-level EOD summary (max 3 sentences)",
    "overall_grade": "A/B/C/D/F"
}
"""

class LearningEngine:
    def __init__(self, repo: TradeRepository):
        self.repo = repo
        LEARNINGS_FILE.parent.mkdir(exist_ok=True)

    def run_retrospective(self, trade_limit: int = 20) -> Dict:
        """Fetch deep history, analyze with AI, and return structured insights."""
        # 1. Fetch Data
        closed_trades = self.repo.get_detailed_trade_history(limit=trade_limit)
        missed_signals = self.repo.get_untriggered_signals(limit=10)
        
        if not closed_trades and not missed_signals:
            return {"summary": "No data available for analysis yet.", "patterns": []}

        # 2. Build User Prompt
        data_payload = {
            "closed_trades": closed_trades,
            "missed_signals": missed_signals,
            "metadata": {
                "timestamp": datetime.now().isoformat(),
                "symbol": "XAUUSD"
            }
        }
        
        user_prompt = f"Analyze the following session data: {json.dumps(data_payload)}"
        
        logger.info(f"Starting AI Retrospective on {len(closed_trades)} trades and {len(missed_signals)} missed signals...")
        
        try:
            result = call_ai_raw(system=SYSTEM_LEARNING_PROMPT, user=user_prompt)
            if result:
                # Clean/Parse if result is string
                if isinstance(result, str):
                    # Strip markdown if AI wrapped it
                    clean = result.replace("```json", "").replace("```", "").strip()
                    result = json.loads(clean)
                
                self._save_learnings(result)
                return result
        except Exception as e:
            logger.error(f"EOD AI Analysis failed: {e}")
            
        return {"summary": "AI Analysis encountered an error.", "patterns": []}

    def _save_learnings(self, data: Dict):
        """Save AI insights to local file."""
        try:
            current_data = self.get_learnings()
            # Append or update patterns
            current_data["last_updated"] = datetime.now().isoformat()
            current_data["patterns"] = data.get("patterns", [])
            current_data["summary"]  = data.get("summary", "")
            
            with open(LEARNINGS_FILE, "w") as f:
                json.dump(current_data, f, indent=4)
            logger.info(f"AI Learnings saved to {LEARNINGS_FILE}")
        except Exception as e:
            logger.error(f"Error saving learnings: {e}")

    def get_learnings(self) -> Dict:
        """Load insights from local file."""
        if not LEARNINGS_FILE.exists():
            return {"patterns": [], "summary": "", "last_updated": ""}
        try:
            with open(LEARNINGS_FILE, "r") as f:
                return json.load(f)
        except:
            return {"patterns": [], "summary": "", "last_updated": ""}
