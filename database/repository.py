"""
Trade Repository — all database read/write operations.
"""
from __future__ import annotations
from datetime import datetime
from typing import List, Optional

from database.models import SignalModel, TradeModel, PerformanceMetrics
from database.connection import get_session
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
                    entry_price    = signal.entry_price,
                    stop_loss      = signal.stop_loss,
                    take_profit    = signal.take_profit,
                    rr_ratio       = signal.rr_ratio,
                    score          = signal.score,
                    max_score      = signal.max_score,
                    score_breakdown= signal.score_breakdown,
                    htf_direction  = signal.htf_bias.direction if signal.htf_bias else None,
                    htf_confidence = signal.htf_bias.confidence if signal.htf_bias else None,
                    session        = signal.session,
                    atr_pips       = signal.atr_pips,
                    ai_confidence  = signal.ai_confidence,
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
        self, trade_id: str, order: TradeOrder, actual_entry: float
    ) -> None:
        try:
            with get_session() as session:
                m = TradeModel(
                    trade_id     = trade_id,
                    symbol       = order.symbol,
                    direction    = order.direction,
                    lot_size     = order.lot_size,
                    entry_price  = actual_entry,
                    stop_loss    = order.stop_loss,
                    take_profit  = order.take_profit_levels.tp2,
                    risk_pct     = order.risk_pct,
                    risk_amount  = order.risk_amount,
                    opened_at    = order.timestamp or datetime.utcnow(),
                    is_open      = True,
                )
                session.add(m)
        except Exception as e:
            logger.error(f"save_trade_open error: {e}")

    def save_trade_close(self, ct: ClosedTrade) -> None:
        try:
            with get_session() as session:
                m = session.query(TradeModel).filter_by(trade_id=ct.trade_id).first()
                if m:
                    m.exit_price   = ct.exit_price
                    m.pnl          = ct.pnl
                    m.pnl_pips     = ct.pnl_pips
                    m.close_reason = ct.reason
                    m.closed_at    = ct.closed_at
                    m.is_open      = False
        except Exception as e:
            logger.error(f"save_trade_close error: {e}")

    def get_open_trades(self) -> List[TradeModel]:
        try:
            with get_session() as session:
                return session.query(TradeModel).filter_by(is_open=True).all()
        except Exception as e:
            logger.error(f"get_open_trades error: {e}")
            return []

    # ── Performance ───────────────────────────────────────────────────────────

    def save_performance_snapshot(self, portfolio: Portfolio, daily_trades: int) -> None:
        try:
            with get_session() as session:
                m = PerformanceMetrics(
                    date          = datetime.utcnow(),
                    balance       = portfolio.balance,
                    equity        = portfolio.equity,
                    drawdown_pct  = portfolio.drawdown_pct,
                    daily_pnl     = portfolio.daily_pnl,
                    daily_trades  = daily_trades,
                    win_rate      = portfolio.win_rate,
                    total_trades  = portfolio.total_trades,
                )
                session.add(m)
        except Exception as e:
            logger.error(f"save_performance_snapshot error: {e}")
