"""
Pause Manager.

Controls timed bot pauses with auto-resume.
Thread-safe singleton used by both the trading loop and Telegram commands.

States:
  RUNNING  — normal operation
  PAUSED   — trading suspended until resume_at OR manual /resume
  HALTED   — kill switch active, requires manual /reset
"""
from __future__ import annotations
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional
from enum import Enum

from utils.logger import get_logger

logger = get_logger(__name__)


class BotState(str, Enum):
    RUNNING = "RUNNING"
    PAUSED  = "PAUSED"
    HALTED  = "HALTED"    # kill switch — needs explicit reset


class PauseManager:
    def __init__(self):
        self._lock      = threading.Lock()
        self._state     = BotState.RUNNING
        self._reason    = ""
        self._resume_at: Optional[datetime] = None
        self._paused_at: Optional[datetime] = None
        self._auto_resume_thread: Optional[threading.Thread] = None

        # Callback called when auto-resume fires (set by TelegramBot)
        self.on_resume_callback: Optional[callable] = None

    # ── State accessors ───────────────────────────────────────────────────────

    @property
    def is_trading_allowed(self) -> bool:
        """Returns True only when state is RUNNING."""
        self._check_auto_resume()
        with self._lock:
            return self._state == BotState.RUNNING

    @property
    def state(self) -> BotState:
        self._check_auto_resume()
        return self._state

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def resume_at(self) -> Optional[datetime]:
        return self._resume_at

    # ── Pause ─────────────────────────────────────────────────────────────────

    def pause(self, reason: str, minutes: int) -> datetime:
        """
        Pause trading for `minutes` minutes.
        Returns the scheduled resume time (UTC).
        """
        now       = datetime.now(timezone.utc)
        resume_at = now + timedelta(minutes=minutes)

        with self._lock:
            self._state     = BotState.PAUSED
            self._reason    = reason
            self._resume_at = resume_at
            self._paused_at = now

        logger.warning(
            f"Bot PAUSED: {reason} | duration={minutes}min | "
            f"resume_at={resume_at.strftime('%H:%M UTC')}"
        )

        # Schedule auto-resume in background
        self._schedule_auto_resume(minutes)
        return resume_at

    def resume(self, resumed_by: str = "manual") -> bool:
        """
        Resume trading immediately.
        Returns False if state is HALTED (kill switch — use reset() instead).
        """
        with self._lock:
            if self._state == BotState.HALTED:
                logger.warning("Cannot resume: kill switch is active. Use reset_halt().")
                return False
            if self._state == BotState.RUNNING:
                return True   # already running

            self._state     = BotState.RUNNING
            self._reason    = ""
            self._resume_at = None

        logger.info(f"Bot RESUMED by: {resumed_by}")
        if self.on_resume_callback:
            try:
                self.on_resume_callback(resumed_by)
            except Exception as e:
                logger.error(f"on_resume_callback error: {e}")
        return True

    # ── Halt (kill switch integration) ────────────────────────────────────────

    def halt(self, reason: str) -> None:
        """Halt trading permanently (kill switch). Requires reset_halt() to clear."""
        with self._lock:
            self._state  = BotState.HALTED
            self._reason = reason
        logger.critical(f"Bot HALTED: {reason}")

    def reset_halt(self, authorized_by: str = "admin") -> None:
        """Reset a halted state (admin action)."""
        with self._lock:
            if self._state == BotState.HALTED:
                self._state  = BotState.RUNNING
                self._reason = ""
        logger.warning(f"Halt reset by: {authorized_by}")

    # ── Auto-resume ───────────────────────────────────────────────────────────

    def _schedule_auto_resume(self, minutes: int) -> None:
        """Spawn a daemon thread that resumes after delay."""
        if self._auto_resume_thread and self._auto_resume_thread.is_alive():
            # Cancel previous timer by marking state (thread checks on wake)
            pass

        def _auto():
            import time
            time.sleep(minutes * 60)
            # Only resume if still paused (not manually resumed or halted)
            with self._lock:
                if self._state == BotState.PAUSED:
                    self._state     = BotState.RUNNING
                    self._reason    = ""
                    self._resume_at = None
                    logger.info(f"Bot auto-resumed after {minutes} min pause")
                    if self.on_resume_callback:
                        try:
                            self.on_resume_callback("auto_timer")
                        except Exception as e:
                            logger.error(f"on_resume_callback error: {e}")

        t = threading.Thread(target=_auto, daemon=True, name="auto_resume")
        self._auto_resume_thread = t
        t.start()

    def _check_auto_resume(self) -> None:
        """Safety check — if resume_at is in the past, resume."""
        with self._lock:
            if (
                self._state == BotState.PAUSED
                and self._resume_at is not None
                and datetime.now(timezone.utc) >= self._resume_at
            ):
                self._state     = BotState.RUNNING
                self._reason    = ""
                self._resume_at = None
                logger.info("Bot auto-resumed (time check)")

    def status_dict(self) -> dict:
        self._check_auto_resume()
        return {
            "state":     self._state.value,
            "reason":    self._reason,
            "paused_at": self._paused_at.isoformat() if self._paused_at else None,
            "resume_at": self._resume_at.isoformat() if self._resume_at else None,
        }


# Global singleton
pause_manager = PauseManager()
