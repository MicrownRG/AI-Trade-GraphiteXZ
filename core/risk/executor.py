"""
Risk Executor.

Orchestrates the full risk pipeline for a given signal:
  1. Run all pre-trade filters
  2. Calculate lot size
  3. Confirm stop loss / take profit
  4. Check kill switch
  5. Return an approved (or rejected) TradeOrder
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List
from datetime import datetime
import pandas as pd

from core.signal.signal_engine import TradeSignal
from core.structure.swing import SwingPoint
from core.risk.lot_sizing import calculate_lot_size, price_to_pips
from core.risk.stop_loss import calculate_stop_loss
from core.risk.take_profit import calculate_take_profit, TakeProfitLevels
from core.risk.filters import run_all_filters
from core.risk.kill_switch import kill_switch
from core.risk.portfolio import Portfolio
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TradeOrder:
    signal_id: str
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit_levels: TakeProfitLevels
    lot_size: float
    risk_amount: float
    risk_pct: float
    approved: bool
    rejection_reason: str = ""
    timestamp: Optional[datetime] = None


class RiskExecutor:
    def __init__(self, portfolio: Portfolio):
        self.portfolio = portfolio

    def evaluate(
        self,
        signal: TradeSignal,
        df_ltf: pd.DataFrame,
        swings: List[SwingPoint],
        spread_pips: float,
        current_time: datetime,
        trades_today: int,
    ) -> TradeOrder:
        """
        Full risk evaluation pipeline for a signal.
        Returns an approved or rejected TradeOrder.
        """
        pf = self.portfolio

        # ── Kill switch check ─────────────────────────────────────────────────
        if kill_switch.is_active:
            return self._reject(signal, f"Kill switch active: {kill_switch.reason}")

        if not kill_switch.check_all(pf.drawdown_pct, pf.daily_pnl_pct):
            return self._reject(signal, kill_switch.reason or "Kill switch triggered")

        # ── Calculate SL from structure ────────────────────────────────────────
        sl = calculate_stop_loss(
            direction=signal.direction,
            entry_price=signal.entry_price,
            swings=swings,
            df_ltf=df_ltf,
        )

        sl_pips = price_to_pips(abs(signal.entry_price - sl))

        # ── Lot size ──────────────────────────────────────────────────────────
        lot_size = calculate_lot_size(
            account_balance=pf.balance,
            stop_loss_pips=sl_pips,
        )
        risk_amount = lot_size * sl_pips * 10.0   # approx USD
        risk_pct    = risk_amount / pf.balance * 100 if pf.balance > 0 else 0

        # ── Take profit ───────────────────────────────────────────────────────
        tp_levels = calculate_take_profit(
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_loss=sl,
            swings=swings,
        )

        # ── Filters ───────────────────────────────────────────────────────────
        passed, failures = run_all_filters(
            spread_pips=spread_pips,
            dt=current_time,
            daily_pnl=pf.daily_pnl,
            balance=pf.balance,
            trades_today=trades_today,
            open_positions=pf.open_trade_count,
            atr_pips=signal.atr_pips,
            rr=tp_levels.rr_at_tp2,
        )

        if not passed:
            return self._reject(signal, "; ".join(failures))

        logger.info(
            f"✅ Trade approved: {signal.direction.upper()} {signal.symbol} "
            f"lot={lot_size} SL={sl:.2f} TP2={tp_levels.tp2:.2f} "
            f"RR={tp_levels.rr_at_tp2:.2f} risk={risk_pct:.2f}%"
        )

        return TradeOrder(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_loss=sl,
            take_profit_levels=tp_levels,
            lot_size=lot_size,
            risk_amount=round(risk_amount, 2),
            risk_pct=round(risk_pct, 2),
            approved=True,
            timestamp=current_time,
        )

    @staticmethod
    def _reject(signal: TradeSignal, reason: str) -> TradeOrder:
        logger.warning(f"❌ Trade rejected [{signal.signal_id}]: {reason}")
        return TradeOrder(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_loss=0.0,
            take_profit_levels=None,  # type: ignore
            lot_size=0.0,
            risk_amount=0.0,
            risk_pct=0.0,
            approved=False,
            rejection_reason=reason,
        )
