"""
Event-Driven Backtest Engine.

Simulates the full trading pipeline bar-by-bar:
  data → structure → signal → risk → execution → portfolio → metrics

Features:
- Spread simulation (dynamic)
- Slippage simulation
- Session filtering
- Equity curve tracking
- Realistic fill logic (market orders on next bar open)
"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
import pandas as pd
import numpy as np

from core.structure.swing import detect_swings
from core.signal.signal_engine import SignalEngine
from core.risk.executor import RiskExecutor, TradeOrder
from core.risk.portfolio import Portfolio, ClosedTrade

from core.risk.take_profit import trailing_stop
from ai.scorer import AIScorer
from data.feature_engineering import prepare_backtest_data
from data.fetcher import slice_window, resample_to_htf
from config.risk_config import RISK_CONFIG
from config.trading_config import TRADING_CONFIG
from config.ai_config import AI_CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)

SLIPPAGE_PIPS = 2.0
SLIPPAGE_PRICE = SLIPPAGE_PIPS * 0.1


@dataclass
class BacktestPosition:
    trade_id: str
    signal_id: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    lot_size: float
    opened_at: datetime
    tp1_hit: bool = False
    half_closed: bool = False


@dataclass
class BacktestResult:
    total_trades: int
    win_rate: float
    profit_factor: float
    total_pnl: float
    max_drawdown_pct: float
    sharpe_ratio: float
    avg_rr: float
    equity_curve: List[float] = field(default_factory=list)
    trades: List[ClosedTrade] = field(default_factory=list)
    monthly_breakdown: Dict[str, float] = field(default_factory=dict)


class BacktestEngine:
    def __init__(
        self,
        initial_balance: float = 10_000.0,
        use_ai: bool = False,
        allowed_strategies: list[str] = None,
    ):
        self.initial_balance = initial_balance
        self.allowed_strategies = allowed_strategies or ["all"]
        self.portfolio       = Portfolio(initial_balance)
        self.signal_engine   = SignalEngine()
        self.risk_executor   = RiskExecutor(self.portfolio)
        self.ai_scorer       = AIScorer(enabled=use_ai and AI_CONFIG.enabled_in_backtest)
        self.open_positions: Dict[str, BacktestPosition] = {}
        self.equity_curve: List[float] = [initial_balance]
        self.trades_today     = 0
        self._last_date: Optional[datetime] = None

    def run(
        self,
        df_ltf: pd.DataFrame,         # M15 — primary entry TF
        df_h1: Optional[pd.DataFrame] = None,
        df_h4: Optional[pd.DataFrame] = None,
        warm_up_bars: int = 100,
    ) -> BacktestResult:
        """
        Main backtest loop — iterates bar by bar over df_ltf.
        HTF frames are resampled from ltf if not provided.
        """
        logger.info(f"Backtest start: {len(df_ltf)} bars | balance={self.initial_balance:.2f}")

        # Enrich data
        df_ltf = prepare_backtest_data(df_ltf)

        # Derive HTF from LTF if not provided
        if df_h1 is None:
            df_h1 = resample_to_htf(df_ltf, "1h")
            df_h1 = prepare_backtest_data(df_h1)
        if df_h4 is None:
            df_h4 = resample_to_htf(df_ltf, "4h")
            df_h4 = prepare_backtest_data(df_h4)

        n = len(df_ltf)

        for i in range(warm_up_bars, n - 1):
            bar      = df_ltf.iloc[i]
            bar_time = df_ltf.index[i]

            # ── Day reset ─────────────────────────────────────────────────────
            if self._last_date is None or bar_time.date() != self._last_date:
                self.trades_today = 0
                self.portfolio.record_day_start()
                self._last_date = bar_time.date()

            # ── Manage open positions ─────────────────────────────────────────
            self._manage_positions(bar, bar_time)

            # ── Update equity curve ───────────────────────────────────────────
            floating = self._calc_floating_pnl(bar["close"])
            self.portfolio.update_equity(floating)
            self.equity_curve.append(self.portfolio.equity)

            # ── Signal generation ─────────────────────────────────────────────
            window_ltf = slice_window(df_ltf, i, window=200)
            window_h1  = self._align_htf(df_h1, bar_time, "1h")
            window_h4  = self._align_htf(df_h4, bar_time, "4h")

            if len(window_h4) < 20 or len(window_h1) < 20:
                continue

            signal = self.signal_engine.generate(
                df_h4=window_h4,
                df_h1=window_h1,
                df_ltf=window_ltf,
                current_time=bar_time.to_pydatetime(),
                symbol="XAUUSD",
            )

            if signal is None:
                continue

            # ── AI evaluation ─────────────────────────────────────────────────
            ai_eval = self.ai_scorer.evaluate(signal)
            signal.ai_confidence = ai_eval.confidence
            signal.ai_decision   = ai_eval.decision
            signal.ai_reason     = ai_eval.reason

            if ai_eval.decision == "SKIP":
                logger.debug(f"AI skip [{signal.signal_id}]: {ai_eval.reason[:60]}")
                continue

            # ── Risk evaluation ───────────────────────────────────────────────
            swings = detect_swings(window_ltf)
            spread = bar.get("spread_pips", 3.0)

            order = self.risk_executor.evaluate(
                signal=signal,
                df_m15=window_ltf,
                df_m5=window_ltf,
                df_m1=window_ltf,
                swings=swings,
                spread_pips=spread,
                current_time=bar_time.to_pydatetime(),
                trades_today=self.trades_today,
            )

            if not order.approved:
                continue

            # ── Simulate fill on next bar open ────────────────────────────────
            next_bar = df_ltf.iloc[i + 1]
            fill_price = self._simulate_fill(
                direction=order.direction,
                intended_price=order.entry_price,
                next_open=next_bar["open"],
            )

            pos = BacktestPosition(
                trade_id=str(uuid.uuid4())[:10],
                signal_id=signal.signal_id,
                direction=order.direction,
                entry_price=fill_price,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit_levels.tp2,
                lot_size=order.lot_size,
                opened_at=bar_time.to_pydatetime(),
            )
            self.open_positions[pos.trade_id] = pos
            self.portfolio.open_trade(pos.trade_id, vars(pos))
            self.trades_today += 1

            logger.debug(
                f"BT open: {pos.direction.upper()} @ {fill_price:.2f} "
                f"SL={pos.stop_loss:.2f} TP={pos.take_profit:.2f}"
            )

        # ── Force-close remaining positions ───────────────────────────────────
        final_price = df_ltf["close"].iloc[-1]
        for tid, pos in list(self.open_positions.items()):
            pnl = self._calc_pnl(pos.direction, pos.entry_price, final_price, pos.lot_size)
            self._close_position(tid, pos, final_price, pnl, "end_of_backtest")

        logger.info(
            f"Backtest complete: trades={self.portfolio.total_trades} "
            f"WR={self.portfolio.win_rate:.1f}% PnL={self.portfolio.total_pnl:.2f}"
        )
        return self._compile_results()

    # ── Position management ───────────────────────────────────────────────────

    def _manage_positions(self, bar: pd.Series, bar_time) -> None:
        for tid, pos in list(self.open_positions.items()):
            h, l = bar["high"], bar["low"]

            # SL hit
            sl_hit = (pos.direction == "buy" and l <= pos.stop_loss) or \
                     (pos.direction == "sell" and h >= pos.stop_loss)
            if sl_hit:
                exit_p = pos.stop_loss
                pnl    = self._calc_pnl(pos.direction, pos.entry_price, exit_p, pos.lot_size)
                self._close_position(tid, pos, exit_p, pnl, "sl")
                continue

            # TP hit
            tp_hit = (pos.direction == "buy" and h >= pos.take_profit) or \
                     (pos.direction == "sell" and l <= pos.take_profit)
            if tp_hit:
                exit_p = pos.take_profit
                pnl    = self._calc_pnl(pos.direction, pos.entry_price, exit_p, pos.lot_size)
                self._close_position(tid, pos, exit_p, pnl, "tp2")
                continue

            # Trailing stop update
            current = bar["close"]
            new_sl  = trailing_stop(pos.entry_price, current, pos.stop_loss,
                                     pos.direction, trail_pips=20.0)
            pos.stop_loss = new_sl

    def _close_position(
        self, tid: str, pos: BacktestPosition,
        exit_price: float, pnl: float, reason: str
    ) -> None:
        ct = ClosedTrade(
            trade_id=tid,
            symbol="XAUUSD",
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            lot_size=pos.lot_size,
            pnl=pnl,
            pnl_pips=pnl / (pos.lot_size * 10.0) if pos.lot_size > 0 else 0,
            opened_at=pos.opened_at,
            closed_at=datetime.now(timezone.utc),
            reason=reason,
        )
        self.portfolio.close_trade(ct)
        self.open_positions.pop(tid, None)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _calc_floating_pnl(self, price: float) -> float:
        total = 0.0
        for pos in self.open_positions.values():
            total += self._calc_pnl(pos.direction, pos.entry_price, price, pos.lot_size)
        return total

    @staticmethod
    def _calc_pnl(direction: str, entry: float, exit_: float, lot: float) -> float:
        pips = (exit_ - entry) / 0.1 if direction == "buy" else (entry - exit_) / 0.1
        return round(pips * lot * 10.0, 2)

    @staticmethod
    def _simulate_fill(direction: str, intended_price: float, next_open: float) -> float:
        """Fill at next bar open + slippage."""
        slippage = SLIPPAGE_PRICE
        if direction == "buy":
            return round(max(intended_price, next_open) + slippage, 2)
        else:
            return round(min(intended_price, next_open) - slippage, 2)

    @staticmethod
    def _align_htf(df_htf: pd.DataFrame, bar_time, freq: str) -> pd.DataFrame:
        """Return HTF bars up to and including the current LTF bar time."""
        return df_htf[df_htf.index <= bar_time].tail(100)

    def _compile_results(self) -> BacktestResult:
        trades    = self.portfolio.closed_trades
        n         = len(trades)
        wins      = [t for t in trades if t.pnl > 0]
        losses    = [t for t in trades if t.pnl <= 0]

        win_rate  = len(wins) / n * 100 if n > 0 else 0
        gross_p   = sum(t.pnl for t in wins)
        gross_l   = abs(sum(t.pnl for t in losses))
        pf        = gross_p / gross_l if gross_l > 0 else float("inf")
        avg_rr    = (
            sum(abs(t.exit_price - t.entry_price) / abs(t.entry_price - t.exit_price)
                for t in wins) / len(wins)
            if wins else 0
        )

        eq = np.array(self.equity_curve)
        peak       = np.maximum.accumulate(eq)
        dd         = (peak - eq) / peak * 100
        max_dd     = float(dd.max()) if len(dd) > 0 else 0

        # Sharpe (annualised, assuming M15 data)
        pnl_series  = np.array([t.pnl for t in trades])
        bars_per_yr = 252 * 24 * 4   # M15 bars per year
        if len(pnl_series) > 1 and pnl_series.std() > 0:
            sharpe = float(pnl_series.mean() / pnl_series.std() * np.sqrt(bars_per_yr / n))
        else:
            sharpe = 0.0

        # Monthly breakdown
        monthly: Dict[str, float] = {}
        for t in trades:
            key = t.closed_at.strftime("%Y-%m")
            monthly[key] = monthly.get(key, 0.0) + t.pnl

        return BacktestResult(
            total_trades=n,
            win_rate=round(win_rate, 2),
            profit_factor=round(pf, 3),
            total_pnl=round(self.portfolio.total_pnl, 2),
            max_drawdown_pct=round(max_dd, 2),
            sharpe_ratio=round(sharpe, 3),
            avg_rr=round(avg_rr, 3),
            equity_curve=self.equity_curve,
            trades=trades,
            monthly_breakdown=monthly,
        )
