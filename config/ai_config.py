"""AI layer configuration."""
from dataclasses import dataclass


@dataclass(frozen=True)
class AIConfig:
    model: str = "claude-opus-4-5"
    max_tokens: int = 512
    temperature: float = 0.0          # deterministic for scoring
    timeout_seconds: float = 10.0
    max_retries: int = 3
    retry_delay_seconds: float = 2.0
    # Confidence gate — trades below this are skipped
    confidence_threshold: float = 0.50
    # Disable AI in backtest to save API calls; use rule-based scoring only
    enabled_in_backtest: bool = False


AI_CONFIG = AIConfig()
