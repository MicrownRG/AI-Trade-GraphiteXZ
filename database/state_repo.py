"""
Bot Runtime State Repository.

Persists daily cooldown flags (cutloss, pulse guard, revenge timer) to the DB
so they survive bot restarts within the same trading day.

Load rule: only apply a record if state_date == today (UTC date).
           A stale record from yesterday is silently ignored.
Save rule: upsert on (account_id, state_date).
"""
from __future__ import annotations
from datetime import datetime, date, timezone
from typing import Optional

from database.connection import get_session
from database.models import BotRuntimeStateModel
from utils.logger import get_logger

logger = get_logger(__name__)


def _today_utc() -> datetime:
    """Return midnight UTC for today as a datetime (used as state_date key)."""
    d = date.today()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


class StateRepository:
    """Load and save daily bot runtime state for a given MT5 account."""

    def __init__(self, account_id: str):
        self.account_id = account_id

    # ── Public API ────────────────────────────────────────────────────────────

    def load(self) -> Optional[dict]:
        """
        Load today's runtime state from the DB.
        Returns a dict with keys matching BotRuntimeStateModel columns,
        or None if no record exists for today.
        """
        today = _today_utc()
        try:
            with get_session() as session:
                row = (
                    session.query(BotRuntimeStateModel)
                    .filter_by(account_id=self.account_id)
                    .filter(BotRuntimeStateModel.state_date == today)
                    .first()
                )
                if row is None:
                    return None
                return {
                    "daily_cutloss_triggered":   row.daily_cutloss_triggered,
                    "pulse_suspended_today":     row.pulse_suspended_today,
                    "pulse_consecutive_losses":  row.pulse_consecutive_losses,
                    "revenge_cooldown_cleared_at": row.revenge_cooldown_cleared_at,
                }
        except Exception as e:
            logger.warning(f"StateRepo.load failed: {e}")
            return None

    def save(
        self,
        *,
        daily_cutloss_triggered:   bool,
        pulse_suspended_today:     bool,
        pulse_consecutive_losses:  int,
        revenge_cooldown_cleared_at: Optional[datetime] = None,
    ) -> None:
        """Upsert today's runtime state into the DB."""
        today = _today_utc()
        try:
            with get_session() as session:
                row = (
                    session.query(BotRuntimeStateModel)
                    .filter_by(account_id=self.account_id)
                    .filter(BotRuntimeStateModel.state_date == today)
                    .first()
                )
                if row is None:
                    row = BotRuntimeStateModel(
                        account_id=self.account_id,
                        state_date=today,
                    )
                    session.add(row)

                row.daily_cutloss_triggered   = daily_cutloss_triggered
                row.pulse_suspended_today     = pulse_suspended_today
                row.pulse_consecutive_losses  = pulse_consecutive_losses
                row.revenge_cooldown_cleared_at = revenge_cooldown_cleared_at
                session.flush()
        except Exception as e:
            logger.warning(f"StateRepo.save failed: {e}")

    def save_from_portfolio(self, portfolio, revenge_cleared_at=None) -> None:
        """
        Convenience wrapper — pull values directly from a Portfolio instance.

        IMPORTANT: if caller passes revenge_cleared_at=None we PRESERVE the
        existing saved value instead of overwriting with NULL. This prevents
        an unrelated save (e.g. pulse-guard reset) from silently erasing the
        revenge-cooldown cleared timestamp. To explicitly clear it, callers
        must pass the literal value ``False`` (handled below).
        """
        final_revenge = revenge_cleared_at
        if revenge_cleared_at is None:
            # Preserve previously saved value
            existing = self.load()
            final_revenge = existing.get("revenge_cooldown_cleared_at") if existing else None
        elif revenge_cleared_at is False:
            # Explicit clear requested
            final_revenge = None

        self.save(
            daily_cutloss_triggered  = portfolio.daily_cutloss_triggered,
            pulse_suspended_today    = portfolio.pulse_suspended_today,
            pulse_consecutive_losses = portfolio.pulse_consecutive_losses,
            revenge_cooldown_cleared_at = final_revenge,
        )

    # ── Read-only snapshot for /status telemetry ──────────────────────────────

    def snapshot(self) -> dict:
        """
        Return the current saved cooldown state as a plain dict for display
        (e.g. Telegram /status). Falsy dict if no row exists today.
        """
        s = self.load() or {}
        return {
            "daily_cutloss_triggered":     bool(s.get("daily_cutloss_triggered")),
            "pulse_suspended_today":       bool(s.get("pulse_suspended_today")),
            "pulse_consecutive_losses":    int(s.get("pulse_consecutive_losses") or 0),
            "revenge_cooldown_cleared_at": s.get("revenge_cooldown_cleared_at"),
        }
