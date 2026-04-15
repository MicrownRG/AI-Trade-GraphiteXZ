"""
Database models using SQLAlchemy 2.x declarative style.
"""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    String, Float, Integer, Boolean, DateTime, Text, JSON,
    ForeignKey,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


class SignalModel(Base):
    __tablename__ = "signals"

    id: Mapped[str]          = mapped_column(String(36), primary_key=True, default=_uuid)
    signal_id: Mapped[str]   = mapped_column(String(20), index=True)
    symbol: Mapped[str]      = mapped_column(String(10))
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    direction: Mapped[str]   = mapped_column(String(4))
    entry_price: Mapped[float]
    stop_loss: Mapped[float]
    take_profit: Mapped[float]
    rr_ratio: Mapped[float]
    score: Mapped[int]
    max_score: Mapped[int]
    score_breakdown: Mapped[dict] = mapped_column(JSON)
    htf_direction: Mapped[str | None] = mapped_column(String(10))
    htf_confidence: Mapped[float | None]
    session: Mapped[str | None] = mapped_column(String(20))
    atr_pips: Mapped[float | None]
    ai_confidence: Mapped[float | None]
    ai_decision: Mapped[str | None] = mapped_column(String(10))
    ai_reason: Mapped[str | None]   = mapped_column(Text)
    created_at: Mapped[datetime]    = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    trade: Mapped["TradeModel | None"] = relationship("TradeModel", back_populates="signal")


class TradeModel(Base):
    __tablename__ = "trades"

    id: Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    trade_id: Mapped[str]     = mapped_column(String(20), unique=True, index=True)
    signal_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("signals.id"))
    account_id: Mapped[str | None] = mapped_column(String(20), index=True)
    mt5_ticket: Mapped[int | None]
    symbol: Mapped[str]       = mapped_column(String(10))
    direction: Mapped[str]    = mapped_column(String(4))
    lot_size: Mapped[float]
    entry_price: Mapped[float]
    stop_loss: Mapped[float]
    take_profit: Mapped[float]
    exit_price: Mapped[float | None]
    pnl: Mapped[float | None]
    pnl_pips: Mapped[float | None]
    risk_pct: Mapped[float | None]
    risk_amount: Mapped[float | None]
    close_reason: Mapped[str | None] = mapped_column(String(30))
    opened_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]
    is_open: Mapped[bool]     = mapped_column(Boolean, default=True)

    signal: Mapped["SignalModel | None"] = relationship("SignalModel", back_populates="trade")


class TradeNote(Base):
    """Event-driven notes attached to a trade (contra-exit records, SL+ TP recalibration hints, etc.)."""
    __tablename__ = "trade_notes"

    id: Mapped[str]          = mapped_column(String(36), primary_key=True, default=_uuid)
    trade_id: Mapped[str]    = mapped_column(String(20), index=True)
    note_type: Mapped[str]   = mapped_column(String(40))   # e.g. "contra_exit" | "sl_plus_tp_recal"
    note: Mapped[str]        = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class AccountConfigModel(Base):
    """
    Per-account configuration persisted in the database.

    Load priority (highest → lowest):
        1. This table (keyed by MT5 account login)
        2. Environment variables (BOT_<FIELD> prefix)
        3. Hardcoded defaults in RiskConfig / TradingConfig

    NULL in any column means "not yet set" — ConfigLoader will populate it
    from .env / default on the very first startup and never overwrite again.
    """
    __tablename__ = "account_config"

    id: Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str]   = mapped_column(String(30), unique=True, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # ── Risk params ───────────────────────────────────────────────────────────
    risk_per_trade_pct:       Mapped[float | None]
    min_rr_ratio:             Mapped[float | None]
    max_lot_size:             Mapped[float | None]
    hard_cutloss_daily_pct:   Mapped[float | None]
    max_concurrent_trades:    Mapped[int   | None]
    multi_entry_delay_sec:    Mapped[int   | None]
    auto_close_stagnant_hours: Mapped[int  | None]
    auto_close_eod:           Mapped[bool  | None]
    min_margin_level_pct:     Mapped[float | None]
    news_risk_multiplier:     Mapped[float | None]
    revenge_cooldown_min:     Mapped[float | None]
    consecutive_loss_limit:   Mapped[int   | None]
    max_allowed_spread_pips:  Mapped[float | None]
    max_slippage_pips:        Mapped[float | None]
    min_signal_score:         Mapped[int   | None]
    min_ai_confidence:        Mapped[float | None]

    # ── Trading mode & behaviour ──────────────────────────────────────────────
    trade_mode:               Mapped[str   | None] = mapped_column(String(20), nullable=True)
    enable_asian_session:     Mapped[bool  | None]
    enable_partial_close:     Mapped[bool  | None]
    ai_adapter_enabled:       Mapped[bool  | None]
    news_blackout_minutes:    Mapped[int   | None]


class BotRuntimeStateModel(Base):
    """
    Per-account daily runtime state — persists cooldown flags across bot restarts.

    State is keyed by (account_id, state_date).  On load, only the record for
    TODAY is applied; a record from a previous day is treated as stale and ignored.
    This means protections (hard cutloss, pulse guard) survive a bot crash/restart
    within the same trading day.

    Reset via Telegram (/reset) writes False/0/NULL back to this record so the
    protection is cleared even if the bot is restarted again before midnight.
    """
    __tablename__ = "bot_runtime_state"

    id: Mapped[str]           = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str]   = mapped_column(String(30), nullable=False, index=True)
    state_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Hard cutloss
    daily_cutloss_triggered:   Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Pulse guard
    pulse_suspended_today:     Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pulse_consecutive_losses:  Mapped[int  | None] = mapped_column(Integer, nullable=True)

    # Anti-revenge cooldown (stores the datetime of the last manual clear)
    revenge_cooldown_cleared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PerformanceMetrics(Base):
    __tablename__ = "performance_metrics"

    id: Mapped[str]            = mapped_column(String(36), primary_key=True, default=_uuid)
    date: Mapped[datetime]     = mapped_column(DateTime, index=True)
    account_id: Mapped[str | None] = mapped_column(String(20), index=True)
    balance: Mapped[float]
    equity: Mapped[float]
    drawdown_pct: Mapped[float]
    daily_pnl: Mapped[float]
    daily_trades: Mapped[int]
    win_rate: Mapped[float]
    total_trades: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
