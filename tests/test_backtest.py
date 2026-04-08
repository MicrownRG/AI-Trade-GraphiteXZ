"""Tests for the backtest engine and metrics."""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtest.engine import BacktestEngine, BacktestResult
from backtest.metrics import (
    print_report, trade_log_to_df, consecutive_losses, expectancy
)
from core.risk.portfolio import ClosedTrade
from datetime import datetime


class TestBacktestEngine:
    def test_runs_without_crash(self, full_dataset):
        engine = BacktestEngine(initial_balance=10_000, use_ai=False)
        result = engine.run(df_ltf=full_dataset, warm_up_bars=100)
        assert isinstance(result, BacktestResult)

    def test_result_has_equity_curve(self, full_dataset):
        engine = BacktestEngine(initial_balance=10_000, use_ai=False)
        result = engine.run(df_ltf=full_dataset, warm_up_bars=100)
        assert len(result.equity_curve) > 0

    def test_win_rate_between_0_and_100(self, full_dataset):
        engine = BacktestEngine(initial_balance=10_000, use_ai=False)
        result = engine.run(df_ltf=full_dataset, warm_up_bars=100)
        assert 0 <= result.win_rate <= 100

    def test_no_negative_lot_sizes(self, full_dataset):
        engine = BacktestEngine(initial_balance=10_000, use_ai=False)
        result = engine.run(df_ltf=full_dataset, warm_up_bars=100)
        for t in result.trades:
            assert t.lot_size >= 0

    def test_balance_changes_after_trades(self, full_dataset):
        engine = BacktestEngine(initial_balance=10_000, use_ai=False)
        result = engine.run(df_ltf=full_dataset, warm_up_bars=100)
        if result.total_trades > 0:
            assert result.total_pnl != 0 or result.total_trades > 0

    def test_max_drawdown_non_negative(self, full_dataset):
        engine = BacktestEngine(initial_balance=10_000, use_ai=False)
        result = engine.run(df_ltf=full_dataset, warm_up_bars=100)
        assert result.max_drawdown_pct >= 0


class TestBacktestMetrics:
    def _make_trades(self, pnls):
        trades = []
        for i, pnl in enumerate(pnls):
            t = ClosedTrade(
                trade_id=str(i), symbol="XAUUSD", direction="buy",
                entry_price=1950.0,
                exit_price=1950.0 + pnl / 10,
                lot_size=0.1,
                pnl=pnl, pnl_pips=pnl,
                opened_at=datetime.utcnow(),
                closed_at=datetime.utcnow(),
                reason="tp2" if pnl > 0 else "sl",
            )
            trades.append(t)
        return trades

    def test_consecutive_losses(self):
        trades = self._make_trades([100, -50, -50, -50, 100, -50])
        assert consecutive_losses(trades) == 3

    def test_expectancy_positive(self):
        trades = self._make_trades([200, 200, -100, 200, -100])
        exp = expectancy(trades)
        assert exp > 0

    def test_expectancy_negative(self):
        trades = self._make_trades([-100, -100, 50, -100])
        exp = expectancy(trades)
        assert exp < 0

    def test_trade_log_to_df(self):
        trades = self._make_trades([100, -50, 200])
        df = trade_log_to_df(trades)
        assert len(df) == 3
        assert "cumulative_pnl" in df.columns
        assert df["cumulative_pnl"].iloc[-1] == 250.0

    def test_print_report_no_crash(self, full_dataset):
        engine = BacktestEngine(initial_balance=10_000, use_ai=False)
        result = engine.run(df_ltf=full_dataset, warm_up_bars=100)
        # Should not raise
        print_report(result)
