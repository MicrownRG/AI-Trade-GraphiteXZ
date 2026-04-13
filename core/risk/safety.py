from __future__ import annotations
from typing import List, Dict
from config.risk_config import RISK_CONFIG
from config.trading_config import TRADING_CONFIG

class SafetyManager:
    @staticmethod
    def check_margin_level(current_margin_level: float) -> bool:
        """Returns True if margin level is above safety threshold."""
        return current_margin_level >= RISK_CONFIG.min_margin_level_pct

    @staticmethod
    def check_hard_liquidation(current_margin_level: float) -> bool:
        """Returns True if in emergency liquidation territory."""
        return current_margin_level < RISK_CONFIG.hard_liq_protection_pct

    @staticmethod
    def get_news_risk_multiplier(active_high_impact_news: bool) -> float:
        """Returns a risk multiplier based on high-impact news status."""
        if active_high_impact_news:
            return RISK_CONFIG.news_risk_multiplier
        return 1.0
