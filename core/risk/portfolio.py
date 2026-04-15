"""
Portfolio state tracker.
Tracks equity, daily P&L, drawdown, and open/closed trade history in memory.
Persisted to the database separately via TradeRepository.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime, date, timezone
import threading

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ClosedTrade:
    trade_id:    str
    symbol:      str
    direction:   str
    entry_price: float
    exit_price:  float
    lot_size:    float
    pnl:         float
    pnl_pips:    float
    opened_at:   datetime
    closed_at:   datetime
    reason:      str   # "tp1" | "tp2" | "sl" | "sl_plus" | "manual" | "kill_switch" | ...


class Portfolio:
    def __init__(self, initial_balance: float):
        self._lock               = threading.Lock()
        self.initial_balance     = initial_balance
        self.balance             = initial_balance
        self.equity              = initial_balance
        self.peak_equity         = initial_balance
        self.open_trades:  dict[str, dict] = {}
        self.closed_trades: List[ClosedTrade] = []
        self._day_start_balance_val: float = initial_balance
        self._day_start_date: date = date.today()

        # Timing and state used by pre-trade filters
        self.last_trade_closed_at:  Optional[datetime] = None
        self.last_trade_opened_at:  Optional[datetime] = None
        self.last_trade_result_pnl: float = 0.0
        self.current_floating_pnl:  float = 0.0

        # Pulse guard state (counter is kept even when suspension guard is removed,
        # for informational logging purposes)
        self.pulse_consecutive_losses: int = 0
        self.pulse_suspended_today:    bool = False
        self._true_realized_pnl:       float = 0.0

        # Hard cutloss state — reset each day in record_day_start()
        self.daily_cutloss_triggered: bool = False

    # ── Equity & drawdown ─────────────────────────────────────────────────────

    def update_equity(self, floating_pnl: float) -> None:
        """Update equity from floating PnL (used in paper trading mode)."""
        with self._lock:
            self.current_floating_pnl = floating_pnl
            self.equity               = self.balance + floating_pnl
            if self.equity > self.peak_equity:
                self.peak_equity = self.equity

    def sync_actual_account(self, real_balance: float, real_equity: float) -> None:
        """Absolute sync directly from MT5 account (handles slippage / commission true values)."""
        with self._lock:
            self.balance              = real_balance
            self.equity               = real_equity
            self.current_floating_pnl = real_equity - real_balance
            if self.equity > self.peak_equity:
                self.peak_equity = self.equity

    def sync_true_pnl(self, mt5_realized_pnl: float) -> None:
        """Store the exact sum of MT5 deal profits for today (from mt5_client)."""
        with self._lock:
            self._true_realized_pnl = mt5_realized_pnl

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return (self.peak_equity - self.equity) / self.peak_equity * 100

    @property
    def realized_daily_loss_pct(self) -> float:
        """Realized (closed) daily loss as % of day-start balance. Always >= 0."""
        if self._day_start_balance_val <= 0:
            return 0.0
        if self._true_realized_pnl >= 0:
            return 0.0
        return abs(self._true_realized_pnl) / self._day_start_balance_val * 100

    @property
    def realized_pnl(self) -> float:
        return self._true_realized_pnl

    @property
    def daily_pnl(self) -> float:
        """Sum of realized PnL today plus current floating PnL."""
        return self.realized_pnl + self.current_floating_pnl

    @property
    def daily_pnl_pct(self) -> float:
        start = self._day_start_balance_val
        return (self.daily_pnl / start * 100) if start > 0 else 0.0

    @property
    def total_open_risk_usd(self) -> float:
        """
        Sum of worst-case loss for all open positions.
        Calculated as: lot * |entry - stop_loss| * pip_value_per_lot
        For XAUUSD: pip_value ≈ $10 per 1.0 lot.

        This value is always computed even for positions with small floating losses
        so that total exposure is visible at all times.
        """
        total = 0.0
        for t in self.open_trades.values():
            lot = t.get("lot_size", 0.0)
            sl  = t.get("stop_loss", 0.0)
            ep  = t.get("entry_price", 0.0)
            if lot > 0 and sl > 0 and ep > 0:
                pips  = abs(ep - sl) / 0.1
                total += pips * lot * 10.0
        return round(total, 2)

    def record_day_start(self, custom_balance: float | None = None) -> None:
        """Set the reference balance for daily PnL and drawdown high-water mark."""
        today = date.today()
        with self._lock:
            self._day_start_date = today
            if custom_balance is not None:
                self._day_start_balance_val = custom_balance
                if custom_balance > self.peak_equity:
                    self.peak_equity = custom_balance
            else:
                self._day_start_balance_val = self.balance
                if self.balance > self.peak_equity:
                    self.peak_equity = self.balance

            self.pulse_suspended_today    = False
            self.pulse_consecutive_losses = 0
            self.daily_cutloss_triggered  = False
            logger.info(
                f"Day start balance set to ${self._day_start_balance_val:.2f} "
                f"(peak: ${self.peak_equity:.2f})"
            )

    # ── Trade management ──────────────────────────────────────────────────────

    def open_trade(self, trade_id: str, trade_info: dict) -> None:
        with self._lock:
            self.open_trades[trade_id]  = trade_info
            self.last_trade_opened_at   = trade_info.get("opened_at") or datetime.now(timezone.utc)
            logger.info(f"Portfolio: opened {trade_id}")

    def debit(self, amount: float, note: str = "") -> None:
        """Subtract fees (commission, swap) from cash balance — not trade PnL."""
        if amount <= 0:
            return
        with self._lock:
            self.balance -= amount
            logger.info(f"Portfolio: debit ${amount:.2f} {note} balance=${self.balance:.2f}")

    def close_trade(self, ct: ClosedTrade) -> None:
        with self._lock:
            self.balance += ct.pnl
            self.open_trades.pop(ct.trade_id, None)
            self.closed_trades.append(ct)

            # Keep only last 500 trades in RAM; DB stores full history
            if len(self.closed_trades) > 500:
                self.closed_trades.pop(0)

            self.last_trade_closed_at  = ct.closed_at
            self.last_trade_result_pnl = ct.pnl

            # Track Pulse consecutive losses for informational purposes
            if "PULSE" in ct.trade_id:
                if ct.pnl < 0:
                    self.pulse_consecutive_losses += 1
                else:
                    self.pulse_consecutive_losses = 0

            logger.info(
                f"Portfolio: closed {ct.trade_id} "
                f"PnL=${ct.pnl:.2f} reason={ct.reason} "
                f"balance=${self.balance:.2f}"
            )

    def partial_close_trade(self, ct: ClosedTrade, remaining_lot: float) -> None:
        with self._lock:
            self.balance += ct.pnl
            self.closed_trades.append(ct)

            # Update remaining lot on the parent trade
            original_id = ct.trade_id.replace("_P", "")
            if original_id in self.open_trades:
                self.open_trades[original_id]["lot_size"] = remaining_lot

            self.last_trade_closed_at  = ct.closed_at
            self.last_trade_result_pnl = ct.pnl

            logger.info(
                f"Portfolio: partial close {original_id} "
                f"PnL=${ct.pnl:.2f} remaining_lot={remaining_lot} "
                f"reason={ct.reason} balance=${self.balance:.2f}"
            )

    def update_trade_sl_tp(self, trade_id: str, sl: float = None, tp: float = None) -> None:
        """Update SL/TP on a tracked open trade (e.g. when manually adjusted on MT5)."""
        with self._lock:
            trade = self.open_trades.get(trade_id)
            if not trade:
                return
            if sl is not None:
                trade["stop_loss"]   = sl
            if tp is not None:
                trade["take_profit"] = tp

    def get_anchor_tp(self, direction: str) -> Optional[float]:
        """Return the furthest take-profit level among open trades in the given direction."""
        with self._lock:
            tps = [
                t["take_profit"]
                for t in self.open_trades.values()
                if t["direction"] == direction and t.get("take_profit")
            ]
            if not tps:
                return None
            return max(tps) if direction == "buy" else min(tps)

    # ── Statistics ────────────────────────────────────────────────────────────

    @property
    def total_trades(self) -> int:
        return len(self.closed_trades)

    @property
    def win_rate(self) -> float:
        if not self.closed_trades:
            return 0.0
        wins = sum(1 for t in self.closed_trades if t.pnl > 0)
        return wins / len(self.closed_trades) * 100

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.closed_trades)

    @property
    def open_trade_count(self) -> int:
        return len(self.open_trades)

    def summary(self) -> dict:
        return {
            "balance":          round(self.balance, 2),
            "equity":           round(self.equity, 2),
            "peak_equity":      round(self.peak_equity, 2),
            "drawdown_pct":     round(self.drawdown_pct, 2),
            "daily_pnl":        round(self.daily_pnl, 2),
            "daily_pnl_pct":    round(self.daily_pnl_pct, 2),
            "total_open_risk":  self.total_open_risk_usd,
            "total_trades":     self.total_trades,
            "win_rate":         round(self.win_rate, 2),
            "total_pnl":        round(self.total_pnl, 2),
            "open_trades":      self.open_trade_count,
        }
