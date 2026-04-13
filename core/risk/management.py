from dataclasses import dataclass
from typing import Optional

@dataclass
class TradeManagementConfig:
    use_recovery: bool = False
    recovery_multiplier: float = 1.5
    max_recovery_steps: int = 4
    auto_be_at_r: Optional[float] = 1.0  # Move to BE at 1R
    partial_close_pct: Optional[float] = 0.5  # Close 50%
    partial_close_at_r: Optional[float] = 1.0

class ManagementLogic:
    def __init__(self, mode_settings: dict):
        self.settings = mode_settings
        
    def should_auto_be(self, current_r: float) -> bool:
        if not self.settings.get("auto_be"):
            return False
        return current_r >= 1.0

    def should_partial_close(self, current_r: float, already_partially_closed: bool) -> bool:
        if not self.settings.get("partial_close") or already_partially_closed:
            return False
        return current_r >= 1.0
