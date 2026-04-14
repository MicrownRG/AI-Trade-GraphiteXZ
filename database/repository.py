"""
Trade Repository — all database read/write operations.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import List, Optional

from database.models import SignalModel, TradeModel, TradeNote, PerformanceMetrics
from database.connection import get_session
from sqlalchemy.orm import joinedload
from core.signal.signal_engine import TradeSignal
from core.risk.executor import TradeOrder
from core.risk.portfolio import ClosedTrade, Portfolio
from utils.logger import get_logger

logger = get_logger(__name__)


class TradeRepository:

    # ── Signals ───────────────────────────────────────────────────────────────

    def save_signal(self, signal: TradeSignal) -> Optional[str]:
        try:
            with get_session() as session:
                m = SignalModel(
                    signal_id      = signal.signal_id,
                    symbol         = signal.symbol,
                    timestamp      = signal.timestamp,
                    direction      = signal.direction,
                    entry_price    = float(signal.entry_price),
                    stop_loss      = float(signal.stop_loss),
                    take_profit    = float(signal.take_profit),
                    rr_ratio       = float(signal.rr_ratio),
                    score          = int(signal.score),
                    max_score      = int(signal.max_score),
                    score_breakdown= signal.score_breakdown,
                    htf_direction  = signal.htf_bias.direction if signal.htf_bias else None,
                    htf_confidence = float(signal.htf_bias.confidence) if signal.htf_bias and signal.htf_bias.confidence is not None else None,
                    session        = signal.session,
                    atr_pips       = float(signal.atr_pips),
                    ai_confidence  = float(signal.ai_confidence),
                    ai_decision    = signal.ai_decision,
                    ai_reason      = signal.ai_reason,
                )
                session.add(m)
                return m.id
        except Exception as e:
            logger.error(f"save_signal error: {e}")
            return None

    # ── Trades ────────────────────────────────────────────────────────────────

    def save_trade_open(
        self, trade_id: str, order: TradeOrder, actual_entry: float, account_id: str | None = None
    ) -> None:
        try:
            with get_session() as session:
                m = TradeModel(
                    trade_id     = trade_id,
                    symbol       = order.symbol,
                    direction    = order.direction,
                    lot_size     = float(order.lot_size),
                    entry_price  = float(actual_entry),
                    stop_loss    = float(order.stop_loss),
                    take_profit  = float(order.take_profit_levels.tp2),
                    risk_pct     = float(order.risk_pct),
                    risk_amount  = float(order.risk_amount),
                    opened_at    = order.timestamp or datetime.now(timezone.utc),
                    is_open      = True,
                    account_id   = account_id,
                )
                session.add(m)
        except Exception as e:
            logger.error(f"save_trade_open error: {e}")

    def save_trade_close(self, ct: ClosedTrade) -> None:
        try:
            with get_session() as session:
                m = session.query(TradeModel).filter_by(trade_id=ct.trade_id).first()
                if m:
                    m.exit_price   = float(ct.exit_price)
                    m.pnl          = float(ct.pnl)
                    m.pnl_pips     = float(ct.pnl_pips)
                    m.close_reason = ct.reason
                    m.closed_at    = ct.closed_at
                    m.is_open      = False
        except Exception as e:
            logger.error(f"save_trade_close error: {e}")
    def get_recent_closed_trades(self, limit: int = 20) -> List[TradeModel]:
        try:
            with get_session() as session:
                return (
                    session.query(TradeModel)
                    .filter_by(is_open=False)
                    .order_by(TradeModel.closed_at.desc())
                    .limit(limit)
                    .all()
                )
        except Exception as e:
            logger.error(f"get_recent_closed_trades error: {e}")
            return []

    def get_detailed_trade_history(self, limit: int = 20) -> List[dict]:
        """Fetches closed trades joined with their originating signal context."""
        try:
            with get_session() as session:
                rows = (
                    session.query(TradeModel)
                    .options(joinedload(TradeModel.signal))
                    .filter_by(is_open=False)
                    .order_by(TradeModel.closed_at.desc())
                    .limit(limit)
                    .all()
                )
                
                results = []
                for r in rows:
                    t_data = {
                        "trade_id": r.trade_id,
                        "symbol": r.symbol,
                        "direction": r.direction,
                        "pnl": r.pnl,
                        "pips": r.pnl_pips,
                        "entry": r.entry_price,
                        "exit": r.exit_price,
                        "reason": r.close_reason,
                        "opened_at": r.opened_at.isoformat() if r.opened_at else "",
                        "signal_context": None
                    }
                    if r.signal:
                        t_data["signal_context"] = {
                            "score": r.signal.score,
                            "breakdown": r.signal.score_breakdown,
                            "ai_reason": r.signal.ai_reason,
                            "session": r.signal.session,
                            "htf_bias": r.signal.htf_direction
                        }
                    results.append(t_data)
                return results
        except Exception as e:
            logger.error(f"get_detailed_trade_history error: {e}")
            return []

    def get_untriggered_signals(self, limit: int = 20) -> List[dict]:
        """Fetches signals that never resulted in a trade (potential missed opportunities)."""
        try:
            with get_session() as session:
                # Signals that don't have an associated trade
                rows = (
                    session.query(SignalModel)
                    .outerjoin(TradeModel)
                    .filter(TradeModel.id == None)
                    .order_by(SignalModel.created_at.desc())
                    .limit(limit)
                    .all()
                )
                
                return [
                    {
                        "signal_id": s.signal_id,
                        "direction": s.direction,
                        "score": s.score,
                        "session": s.session,
                        "reason": s.ai_reason,
                        "created_at": s.created_at.isoformat()
                    }
                    for s in rows
                ]
        except Exception as e:
            logger.error(f"get_untriggered_signals error: {e}")
            return []

    def get_trades_by_date(self, date_obj) -> list:
        """Retrieves all trades closed on a specific date from the DB."""
        try:
            from sqlalchemy import func
            with get_session() as session:
                rows = (
                    session.query(TradeModel)
                    .filter(func.date(TradeModel.closed_at) == date_obj)
                    .filter_by(is_open=False)
                    .all()
                )
                return [
                    {
                        "trade_id":    r.trade_id,
                        "direction":   r.direction,
                        "entry_price": r.entry_price,
                        "exit_price":  r.exit_price,
                        "lot_size":    r.lot_size,
                        "pnl":         r.pnl,
                        "pnl_pips":    r.pnl_pips,
                        "close_reason":r.close_reason,
                        "opened_at":   r.opened_at,
                        "closed_at":   r.closed_at,
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"get_trades_by_date error: {e}")
            return []

    def get_open_trades(self) -> list:
        """Returns open trades as plain dicts (safe to use outside session)."""
        try:
            with get_session() as session:
                rows = session.query(TradeModel).filter_by(is_open=True).all()
                # Convert to dicts inside the session to avoid DetachedInstanceError
                return [
                    {
                        "trade_id":    r.trade_id,
                        "symbol":      r.symbol,
                        "direction":   r.direction,
                        "lot_size":    r.lot_size,
                        "entry_price": r.entry_price,
                        "stop_loss":   r.stop_loss,
                        "take_profit": r.take_profit,
                        "risk_pct":    r.risk_pct,
                        "opened_at":   r.opened_at,
                        "is_open":     r.is_open,
                        "mt5_ticket":  getattr(r, "mt5_ticket", None),
                        "account_id":  r.account_id,
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"get_open_trades error: {e}")
            return []

    def save_trade_note(self, trade_id: str, note: str, note_type: str = "general") -> None:
        """
        Persist an event note for a trade (contra-exit, SL+ TP recalibration, etc.).
        Called by order_manager when structural exits or trailing-stop events occur.
        """
        try:
            with get_session() as session:
                m = TradeNote(
                    trade_id  = trade_id,
                    note_type = note_type,
                    note      = note,
                )
                session.add(m)
        except Exception as e:
            logger.error(f"save_trade_note error: {e}")

    def get_trade_notes(self, trade_id: str) -> list:
        """Return all notes for a given trade (most recent first)."""
        try:
            with get_session() as session:
                rows = (
                    session.query(TradeNote)
                    .filter_by(trade_id=trade_id)
                    .order_by(TradeNote.created_at.desc())
                    .all()
                )
                return [
                    {"note_type": r.note_type, "note": r.note, "created_at": r.created_at.isoformat()}
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"get_trade_notes error: {e}")
            return []

    # ── Performance ───────────────────────────────────────────────────────────

    def save_performance_snapshot(self, portfolio: Portfolio, daily_trades: int, account_id: str | None = None) -> None:
        try:
            with get_session() as session:
                m = PerformanceMetrics(
                    date          = datetime.now(timezone.utc),
                    balance       = float(portfolio.balance),
                    equity        = float(portfolio.equity),
                    drawdown_pct  = float(portfolio.drawdown_pct),
                    daily_pnl     = float(portfolio.daily_pnl),
                    daily_trades  = int(daily_trades),
                    win_rate      = float(portfolio.win_rate),
                    total_trades  = int(portfolio.total_trades),
                    account_id    = account_id,
                )
                session.add(m)
        except Exception as e:
            logger.error(f"save_performance_snapshot error: {e}")
