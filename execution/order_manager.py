"""
Order Manager.

Handles the lifecycle of a single trade:
  open → monitor → partial close at TP1 → trail / close at TP2/SL

Integrates with TelegramBot for real-time notifications.
"""
from __future__ import annotations
import uuid
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from core.risk.executor import TradeOrder
from core.risk.portfolio import Portfolio, ClosedTrade
from core.risk.take_profit import trailing_stop
from core.risk.kill_switch import kill_switch
from execution.mt5_client import MT5Client
from database.repository import TradeRepository
from utils.logger import get_logger

if TYPE_CHECKING:
    from telegram.bot import TelegramBot

logger = get_logger(__name__)

# Breakeven activation: move SL to entry once profit >= 1R
_BE_ACTIVATION_R = 1.0


class OrderManager:
    def __init__(
        self,
        mt5:       MT5Client,
        portfolio: Portfolio,
        repo:      Optional[TradeRepository] = None,
        telegram:  Optional["TelegramBot"]   = None,
    ):
        self.mt5       = mt5
        self.portfolio = portfolio
        self.repo      = repo
        self.telegram  = telegram

    def execute(self, order: TradeOrder, signal_meta: dict | None = None) -> Optional[str]:
        """
        Execute an approved TradeOrder via MT5.
        Returns the internal trade_id or None on failure.
        signal_meta: optional dict with score, session, ai_confidence, ai_reason
        """
        if not order.approved:
            logger.warning(f"Attempted to execute rejected order: {order.rejection_reason}")
            return None

        tp2 = order.take_profit_levels.tp2

        result = self.mt5.place_market_order(
            symbol     = order.symbol,
            direction  = order.direction,
            lot_size   = order.lot_size,
            stop_loss  = order.stop_loss,
            take_profit= tp2,
            comment    = f"sig:{order.signal_id}",
        )

        if result is None:
            logger.error(f"Order execution failed for signal {order.signal_id}")
            return None

        trade_id     = str(uuid.uuid4())[:12]
        mt5_ticket   = result.get("order", 0)
        actual_entry = result.get("price", order.entry_price)

        trade_info = {
            "mt5_ticket":   mt5_ticket,
            "signal_id":    order.signal_id,
            "symbol":       order.symbol,
            "direction":    order.direction,
            "entry_price":  actual_entry,
            "lot_size":     order.lot_size,
            "stop_loss":    order.stop_loss,
            "take_profit":  tp2,
            "tp_levels":    order.take_profit_levels,
            "risk_amount":  order.risk_amount,
            "risk_pct":     order.risk_pct,
            "opened_at":    datetime.utcnow(),
            "be_activated": False,
        }
        self.portfolio.open_trade(trade_id, trade_info)

        if self.repo:
            self.repo.save_trade_open(trade_id, order, actual_entry)

        # ── Telegram: entry notification ─────────────────────────────────────
        if self.telegram:
            meta = signal_meta or {}
            self.telegram.notify_trade_opened(
                trade_id      = trade_id,
                direction     = order.direction,
                entry_price   = actual_entry,
                stop_loss     = order.stop_loss,
                take_profit   = tp2,
                lot_size      = order.lot_size,
                risk_amount   = order.risk_amount,
                risk_pct      = order.risk_pct,
                signal_score  = meta.get("score", 0),
                session       = meta.get("session", ""),
                ai_confidence = meta.get("ai_confidence", 0.0),
                ai_reason     = meta.get("ai_reason", ""),
                opened_at     = trade_info["opened_at"],
            )

        logger.info(
            f"✅ Trade opened: {trade_id} | MT5#{mt5_ticket} | "
            f"{order.direction.upper()} {order.lot_size}L @ {actual_entry:.2f}"
        )
        return trade_id

    def monitor_and_manage(self, current_price: float) -> None:
        """
        Called on every new tick/bar.
        Applies trailing stop, breakeven, and SL/TP exit logic.
        """
        for trade_id, trade in list(self.portfolio.open_trades.items()):
            direction  = trade["direction"]
            entry      = trade["entry_price"]
            sl         = trade["stop_loss"]
            lot        = trade["lot_size"]
            tp         = trade["take_profit"]
            be_active  = trade.get("be_activated", False)

            # ── Breakeven check ───────────────────────────────────────────────
            if not be_active:
                risk = abs(entry - sl)
                if risk > 0:
                    profit = (current_price - entry) if direction == "buy" else (entry - current_price)
                    if profit >= risk * _BE_ACTIVATION_R:
                        new_sl = entry + 0.01 if direction == "buy" else entry - 0.01
                        trade["stop_loss"]   = new_sl
                        trade["be_activated"]= True
                        if self.telegram:
                            self.telegram.notify_breakeven(trade_id, direction, entry, new_sl)
                        continue

            # ── Trailing stop ─────────────────────────────────────────────────
            new_sl = trailing_stop(
                entry_price  = entry,
                current_price= current_price,
                current_sl   = sl,
                direction    = direction,
                trail_pips   = 20.0,
            )
            if abs(new_sl - sl) > 0.01:
                trade["stop_loss"] = new_sl

            # ── SL hit ────────────────────────────────────────────────────────
            sl_hit = (
                (direction == "buy"  and current_price <= trade["stop_loss"]) or
                (direction == "sell" and current_price >= trade["stop_loss"])
            )
            if sl_hit:
                pnl = self._calc_pnl(direction, entry, trade["stop_loss"], lot)
                ct  = self._close_trade(trade_id, trade, trade["stop_loss"], pnl, "sl")
                kill_switch.record_trade_result(pnl)
                if self.telegram:
                    self.telegram.notify_trade_closed(ct)
                continue

            # ── TP hit ────────────────────────────────────────────────────────
            tp_hit = (
                (direction == "buy"  and current_price >= tp) or
                (direction == "sell" and current_price <= tp)
            )
            if tp_hit:
                pnl = self._calc_pnl(direction, entry, tp, lot)
                ct  = self._close_trade(trade_id, trade, tp, pnl, "tp2")
                kill_switch.record_trade_result(pnl)
                if self.telegram:
                    self.telegram.notify_trade_closed(ct)

    def _close_trade(
        self, trade_id: str, trade: dict,
        exit_price: float, pnl: float, reason: str
    ) -> ClosedTrade:
        ct = ClosedTrade(
            trade_id   = trade_id,
            symbol     = trade["symbol"],
            direction  = trade["direction"],
            entry_price= trade["entry_price"],
            exit_price = exit_price,
            lot_size   = trade["lot_size"],
            pnl        = pnl,
            pnl_pips   = pnl / (trade["lot_size"] * 10.0) if trade["lot_size"] > 0 else 0,
            opened_at  = trade["opened_at"],
            closed_at  = datetime.utcnow(),
            reason     = reason,
        )
        self.portfolio.close_trade(ct)
        if self.repo:
            self.repo.save_trade_close(ct)
        return ct

    @staticmethod
    def _calc_pnl(direction: str, entry: float, exit_: float, lot: float) -> float:
        pips = (exit_ - entry) / 0.1 if direction == "buy" else (entry - exit_) / 0.1
        return round(pips * lot * 10.0, 2)
