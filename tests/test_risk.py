"""Tests for risk management modules."""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.risk.lot_sizing import calculate_lot_size, price_to_pips, pips_to_price
from core.risk.stop_loss import calculate_stop_loss, adjust_sl_to_breakeven
from core.risk.take_profit import calculate_take_profit, trailing_stop
from core.risk.portfolio import Portfolio, ClosedTrade
from core.risk.kill_switch import KillSwitch
from core.risk.filters import (
    filter_spread, filter_session, filter_daily_loss,
    filter_daily_trade_count, filter_rr_ratio, run_all_filters,
)
from datetime import datetime


# ── Lot sizing ────────────────────────────────────────────────────────────────

class TestLotSizing:
    def test_basic_calculation(self):
        # $10,000 balance, 1% risk, 20 pip SL → ~$100 risk
        # $100 / (20 pips × $10/pip) = 0.50 lots
        lot = calculate_lot_size(account_balance=10_000, stop_loss_pips=20, risk_pct=1.0)
        assert abs(lot - 0.50) < 0.02

    def test_lot_respects_min(self):
        lot = calculate_lot_size(account_balance=100, stop_loss_pips=200, risk_pct=0.1)
        assert lot >= 0.01

    def test_lot_respects_max(self):
        lot = calculate_lot_size(account_balance=1_000_000, stop_loss_pips=1, risk_pct=5.0)
        assert lot <= 5.0

    def test_lot_is_rounded_to_step(self):
        lot = calculate_lot_size(account_balance=10_000, stop_loss_pips=15)
        # Should be a multiple of 0.01
        assert abs(lot * 100 - round(lot * 100)) < 1e-9

    def test_raises_on_zero_sl(self):
        with pytest.raises(ValueError):
            calculate_lot_size(account_balance=10_000, stop_loss_pips=0)

    def test_price_pip_conversion(self):
        assert abs(price_to_pips(1.0) - 10.0) < 1e-9   # $1.0 = 10 pips for gold
        assert abs(pips_to_price(10.0) - 1.0) < 1e-9


# ── Stop Loss ─────────────────────────────────────────────────────────────────

class TestStopLoss:
    def test_buy_sl_below_entry(self, bullish_df):
        from core.structure.swing import detect_swings
        swings = detect_swings(bullish_df)
        entry  = bullish_df["close"].iloc[-1]
        sl     = calculate_stop_loss("buy", entry, swings, bullish_df)
        assert sl < entry

    def test_sell_sl_above_entry(self, bearish_df):
        from core.structure.swing import detect_swings
        swings = detect_swings(bearish_df)
        entry  = bearish_df["close"].iloc[-1]
        sl     = calculate_stop_loss("sell", entry, swings, bearish_df)
        assert sl > entry

    def test_breakeven_moves_sl(self):
        entry = 1950.0
        sl_   = adjust_sl_to_breakeven(entry, 1960.0, "buy", activation_rr=1.0, original_sl=1940.0)
        assert sl_ > 1940.0   # moved to breakeven

    def test_breakeven_not_triggered_early(self):
        entry = 1950.0
        sl_   = adjust_sl_to_breakeven(entry, 1953.0, "buy", activation_rr=1.0, original_sl=1940.0)
        assert sl_ == 1940.0  # not triggered yet


# ── Take Profit ───────────────────────────────────────────────────────────────

class TestTakeProfit:
    def test_buy_tp_above_entry(self, bullish_df):
        from core.structure.swing import detect_swings
        swings = detect_swings(bullish_df)
        entry  = 1950.0
        sl_    = 1940.0
        levels = calculate_take_profit("buy", entry, sl_, swings)
        assert levels.tp2 > entry

    def test_sell_tp_below_entry(self, bearish_df):
        from core.structure.swing import detect_swings
        swings = detect_swings(bearish_df)
        entry  = 1950.0
        sl_    = 1960.0
        levels = calculate_take_profit("sell", entry, sl_, swings)
        assert levels.tp2 < entry

    def test_rr_meets_minimum(self, bullish_df):
        from core.structure.swing import detect_swings
        swings = detect_swings(bullish_df)
        entry  = 1950.0
        sl_    = 1940.0
        levels = calculate_take_profit("buy", entry, sl_, swings, min_rr=2.0)
        assert levels.rr_at_tp2 >= 1.9   # slight float tolerance

    def test_trailing_stop_buy_moves_up(self):
        new_sl = trailing_stop(1950.0, 1980.0, 1940.0, "buy", trail_pips=20.0)
        assert new_sl > 1940.0

    def test_trailing_stop_never_moves_backward(self):
        new_sl = trailing_stop(1950.0, 1952.0, 1945.0, "buy", trail_pips=20.0)
        assert new_sl >= 1945.0  # should not regress


# ── Portfolio ─────────────────────────────────────────────────────────────────

class TestPortfolio:
    def test_initial_state(self):
        pf = Portfolio(10_000.0)
        assert pf.balance == 10_000.0
        assert pf.total_trades == 0
        assert pf.win_rate == 0.0

    def test_close_winning_trade_updates_balance(self):
        pf = Portfolio(10_000.0)
        pf.open_trade("t1", {"symbol": "XAUUSD"})
        ct = ClosedTrade(
            trade_id="t1", symbol="XAUUSD", direction="buy",
            entry_price=1950.0, exit_price=1970.0, lot_size=0.1,
            pnl=200.0, pnl_pips=200.0,
            opened_at=datetime.utcnow(), closed_at=datetime.utcnow(),
            reason="tp2",
        )
        pf.close_trade(ct)
        assert pf.balance == 10_200.0
        assert pf.total_trades == 1
        assert pf.win_rate == 100.0

    def test_drawdown_calculation(self):
        pf = Portfolio(10_000.0)
        pf.update_equity(1_000.0)   # equity = 11000 (new peak)
        pf.update_equity(-2_000.0)  # equity = 9000
        assert pf.drawdown_pct > 0


# ── Kill Switch ───────────────────────────────────────────────────────────────

class TestKillSwitch:
    def test_not_active_by_default(self):
        ks = KillSwitch()
        assert not ks.is_active

    def test_trigger_activates(self):
        ks = KillSwitch()
        ks.trigger("test")
        assert ks.is_active

    def test_reset_deactivates(self):
        ks = KillSwitch()
        ks.trigger("test")
        ks.reset()
        assert not ks.is_active

    def test_drawdown_trigger(self):
        ks = KillSwitch()
        ks.check_drawdown(7.0)  # above 6% kill threshold
        assert ks.is_active

    def test_consecutive_loss_trigger(self):
        ks = KillSwitch()
        for _ in range(5):
            ks.record_trade_result(-100.0)
        assert ks.is_active


# ── Filters ───────────────────────────────────────────────────────────────────

class TestFilters:
    def test_spread_filter_pass(self):
        passed, _ = filter_spread(5.0)
        assert passed

    def test_spread_filter_fail(self):
        passed, msg = filter_spread(999.0)
        assert not passed
        assert "Spread" in msg

    def test_session_filter_london(self):
        dt = datetime(2024, 1, 15, 9, 0)  # 09:00 UTC = London
        passed, _ = filter_session(dt)
        assert passed

    def test_session_filter_asian_rejected(self):
        dt = datetime(2024, 1, 15, 3, 0)  # 03:00 UTC = Asian
        passed, _ = filter_session(dt)
        assert not passed

    def test_daily_loss_filter(self):
        passed, msg = filter_daily_loss(-350.0, 10_000.0)
        assert not passed   # 3.5% > 3% limit

    def test_rr_filter_pass(self):
        passed, _ = filter_rr_ratio(2.5)
        assert passed

    def test_rr_filter_fail(self):
        passed, msg = filter_rr_ratio(1.0)
        assert not passed

    def test_run_all_filters_all_pass(self):
        passed, failures = run_all_filters(
            spread_pips=5.0,
            dt=datetime(2024, 1, 15, 10, 0),
            daily_pnl=100.0,
            balance=10_000.0,
            trades_today=2,
            open_positions=1,
            atr_pips=30.0,
            rr=2.0,
        )
        assert passed
        assert len(failures) == 0

    def test_run_all_filters_multiple_fail(self):
        passed, failures = run_all_filters(
            spread_pips=999.0,              # fail
            dt=datetime(2024, 1, 15, 3, 0), # fail (Asian)
            daily_pnl=-500.0,               # fail (5% loss)
            balance=10_000.0,
            trades_today=10,                # fail (over limit)
            open_positions=1,
            atr_pips=30.0,
            rr=2.0,
        )
        assert not passed
        assert len(failures) >= 3
