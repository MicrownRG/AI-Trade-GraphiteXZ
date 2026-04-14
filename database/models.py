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
