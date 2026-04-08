"""
Kill Switch — halts all trading when risk thresholds are breached.

Conditions that trigger the kill switch:
  1. Drawdown exceeds kill_switch_drawdown_pct
  2. Daily loss exceeds kill_switch_daily_loss_pct
  3. Consecutive loss streak (configurable)
  4. Manual trigger via API/CLI

Once triggered, only a manual reset can re-enable trading.
"""
from __future__ import annotations
import threading
from datetime import datetime
from typing import Optional

from config.risk_config import RISK_CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)


class KillSwitch:
    def __init__(self):
        self._lock       = threading.Lock()
        self._triggered  = False
        self._reason: Optional[str] = None
        self._triggered_at: Optional[datetime] = None
        self._consecutive_losses = 0
        self._max_consecutive_losses = 5

    # ── State ─────────────────────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        return self._triggered

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    # ── Trigger ───────────────────────────────────────────────────────────────

    def trigger(self, reason: str) -> None:
        with self._lock:
            if not self._triggered:
                self._triggered    = True
                self._reason       = reason
                self._triggered_at = datetime.utcnow()
                logger.critical(f"🔴 KILL SWITCH ACTIVATED: {reason}")

    def reset(self, authorized_by: str = "manual") -> None:
        with self._lock:
            self._triggered          = False
            self._reason             = None
            self._triggered_at       = None
            self._consecutive_losses = 0
            logger.warning(f"🟢 Kill switch reset by: {authorized_by}")

    # ── Auto-check hooks ──────────────────────────────────────────────────────

    def check_drawdown(self, drawdown_pct: float) -> None:
        limit = RISK_CONFIG.kill_switch_drawdown_pct
        if drawdown_pct >= limit:
            self.trigger(f"Drawdown {drawdown_pct:.2f}% >= limit {limit}%")

    def check_daily_loss(self, daily_loss_pct: float) -> None:
        limit = RISK_CONFIG.kill_switch_daily_loss_pct
        if daily_loss_pct <= -limit:
            self.trigger(f"Daily loss {daily_loss_pct:.2f}% >= limit {limit}%")

    def record_trade_result(self, pnl: float) -> None:
        if pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self._max_consecutive_losses:
                self.trigger(
                    f"Consecutive loss streak: {self._consecutive_losses} trades"
                )
        else:
            self._consecutive_losses = 0

    def check_all(self, drawdown_pct: float, daily_loss_pct: float) -> bool:
        """Run all checks. Returns True if trading should continue."""
        if self._triggered:
            return False
        self.check_drawdown(drawdown_pct)
        self.check_daily_loss(daily_loss_pct)
        return not self._triggered

    def status(self) -> dict:
        return {
            "active":           self._triggered,
            "reason":           self._reason,
            "triggered_at":     self._triggered_at.isoformat() if self._triggered_at else None,
            "consecutive_loss": self._consecutive_losses,
        }


# Global singleton
kill_switch = KillSwitch()
