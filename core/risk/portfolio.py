"""
Portfolio state tracker.
Tracks equity, daily P&L, drawdown, and trade history in memory.
Persisted to DB separately via repository.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime, date
import threading

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ClosedTrade:
    trade_id: str
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    lot_size: float
    pnl: float
    pnl_pips: float
    opened_at: datetime
    closed_at: datetime
    reason: str   # "tp1" | "tp2" | "sl" | "manual" | "kill_switch"


class Portfolio:
    def __init__(self, initial_balance: float):
        self._lock = threading.Lock()
        self.initial_balance = initial_balance
        self.balance         = initial_balance
        self.equity          = initial_balance
        self.peak_equity     = initial_balance
        self.open_trades: dict[str, dict] = {}
        self.closed_trades: List[ClosedTrade] = []
        self._day_start_balance: dict[date, float] = {}

    # ── Equity & Drawdown ─────────────────────────────────────────────────────

    def update_equity(self, floating_pnl: float) -> None:
        with self._lock:
            self.equity = self.balance + floating_pnl
            if self.equity > self.peak_equity:
                self.peak_equity = self.equity

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return (self.peak_equity - self.equity) / self.peak_equity * 100

    @property
    def daily_pnl(self) -> float:
        today = date.today()
        start = self._day_start_balance.get(today, self.initial_balance)
        return self.equity - start

    @property
    def daily_pnl_pct(self) -> float:
        today = date.today()
        start = self._day_start_balance.get(today, self.initial_balance)
        return (self.daily_pnl / start * 100) if start > 0 else 0.0

    def record_day_start(self) -> None:
        today = date.today()
        with self._lock:
            if today not in self._day_start_balance:
                self._day_start_balance[today] = self.balance

    # ── Trade management ──────────────────────────────────────────────────────

    def open_trade(self, trade_id: str, trade_info: dict) -> None:
        with self._lock:
            self.open_trades[trade_id] = trade_info
            logger.info(f"Portfolio: opened {trade_id}")

    def close_trade(self, ct: ClosedTrade) -> None:
        with self._lock:
            self.balance += ct.pnl
            self.open_trades.pop(ct.trade_id, None)
            self.closed_trades.append(ct)
            logger.info(
                f"Portfolio: closed {ct.trade_id} PnL={ct.pnl:.2f} "
                f"reason={ct.reason} balance={self.balance:.2f}"
            )

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
            "balance":       round(self.balance, 2),
            "equity":        round(self.equity, 2),
            "peak_equity":   round(self.peak_equity, 2),
            "drawdown_pct":  round(self.drawdown_pct, 2),
            "daily_pnl":     round(self.daily_pnl, 2),
            "daily_pnl_pct": round(self.daily_pnl_pct, 2),
            "total_trades":  self.total_trades,
            "win_rate":      round(self.win_rate, 2),
            "total_pnl":     round(self.total_pnl, 2),
            "open_trades":   self.open_trade_count,
        }
