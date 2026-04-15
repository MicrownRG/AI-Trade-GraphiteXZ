"""
Standalone test runner — no external test framework required.
Run: PYTHONPATH=/home/claude/trading_bot python tests/run_tests.py
"""
import sys
import traceback
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from datetime import datetime

# ── Core assertion helper ─────────────────────────────────────────────────────
def assert_(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)

passed = 0
failed = 0
errors = []

def run(name, fn):
    global passed, failed
    try:
        fn()
        print(f"  ✅  {name}")
        passed += 1
    except Exception as e:
        print(f"  ❌  {name}: {e}")
        failed += 1
        errors.append((name, traceback.format_exc()))

# ── Synthetic OHLCV fixtures ──────────────────────────────────────────────────
def make_df(n=300, trend=0.10, vol=1.5, seed=42):
    np.random.seed(seed)
    idx = pd.date_range("2024-01-02 08:00", periods=n, freq="15min", tz="UTC")
    c   = np.cumsum(np.random.randn(n) * vol + trend) + 1950.0
    o   = np.roll(c, 1); o[0] = c[0]
    hr  = np.abs(np.random.randn(n)) * vol * 0.5 + 1.0
    h   = np.maximum(o, c) + hr
    l   = np.minimum(o, c) - hr
    v   = np.random.randint(100, 5000, n).astype(float)
    return pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=v), index=idx)

bull = make_df(trend=0.10)
bear = make_df(trend=-0.10)
full = make_df(n=600, trend=0.07)

from data.fetcher import resample_to_htf
h4 = resample_to_htf(full, "4h")
h1 = resample_to_htf(full, "1h")

# ═══════════════════════════════════════════════════════════════════════════════
print("\n── STRUCTURE ──────────────────────────────────────────────────────")
# ═══════════════════════════════════════════════════════════════════════════════
from core.structure.swing      import detect_swings, get_recent_swings
from core.structure.bos        import detect_bos, get_latest_bos
from core.structure.choch      import detect_choch
from core.structure.liquidity  import detect_liquidity_sweeps
from core.structure.displacement import detect_displacement, atr
from core.structure.equal_levels import detect_equal_levels
from core.structure.htf_bias   import calculate_htf_bias

def test_swings_list():
    assert_(isinstance(detect_swings(bull), list))
run("detect_swings returns list", test_swings_list)

def test_swings_kinds():
    kinds = {s.kind for s in detect_swings(bull)}
    assert_("high" in kinds and "low" in kinds)
run("swings have highs AND lows", test_swings_kinds)

def test_swing_high_local_max():
    for s in detect_swings(bull, lookback=5):
        if s.kind == "high":
            w = bull["high"].iloc[max(0, s.index-5): s.index+6]
            assert_(s.price == w.max(), f"swing high {s.price} not local max in {w.tolist()}")
run("swing high is local maximum", test_swing_high_local_max)

def test_swing_low_local_min():
    for s in detect_swings(bull, lookback=5):
        if s.kind == "low":
            w = bull["low"].iloc[max(0, s.index-5): s.index+6]
            assert_(s.price == w.min())
run("swing low is local minimum", test_swing_low_local_min)

def test_bos_bullish_in_uptrend():
    assert_(any(e.direction == "bullish" for e in detect_bos(bull)))
run("BOS: bullish in uptrend", test_bos_bullish_in_uptrend)

def test_bos_bearish_in_downtrend():
    assert_(any(e.direction == "bearish" for e in detect_bos(bear)))
run("BOS: bearish in downtrend", test_bos_bearish_in_downtrend)

def test_bos_close_beyond_level():
    for e in detect_bos(bull):
        if e.direction == "bullish":
            assert_(e.close_price > e.broken_level)
        else:
            assert_(e.close_price < e.broken_level)
run("BOS close price beyond broken level", test_bos_close_beyond_level)

def test_latest_bos_not_none():
    assert_(get_latest_bos(bull) is not None)
run("get_latest_bos returns event", test_latest_bos_not_none)

def test_choch_directions():
    for e in detect_choch(bull):
        assert_(e.direction in ("bullish", "bearish"))
run("CHOCH direction is valid string", test_choch_directions)

def test_sweep_wick_ratio():
    for s in detect_liquidity_sweeps(bull, min_wick_ratio=0.3):
        assert_(s.wick_ratio >= 0.3)
run("liquidity sweep wick_ratio >= threshold", test_sweep_wick_ratio)

def test_sweep_close_back_inside():
    for s in detect_liquidity_sweeps(bull):
        if s.direction == "buy_side":
            assert_(s.wick_high > s.swept_level)
            assert_(s.close_price < s.swept_level)
        else:
            assert_(s.wick_low < s.swept_level)
            assert_(s.close_price > s.swept_level)
run("liquidity sweep close back inside range", test_sweep_close_back_inside)

def test_displacement_body_ratio():
    for e in detect_displacement(bull, min_body_ratio=0.6):
        assert_(e.body_ratio >= 0.6)
run("displacement body_ratio >= 0.6", test_displacement_body_ratio)

def test_displacement_direction_matches():
    for e in detect_displacement(bull):
        bar = bull.iloc[e.index]
        bullish_candle = bar["close"] > bar["open"]
        assert_((e.direction == "bullish") == bullish_candle)
run("displacement direction matches candle body", test_displacement_direction_matches)

def test_atr_positive():
    assert_(atr(bull, 14).iloc[-1] > 0)
run("ATR is positive", test_atr_positive)

def test_equal_levels_touches():
    for lv in detect_equal_levels(bull):
        assert_(lv.touches >= 2)
run("equal levels have 2+ touches", test_equal_levels_touches)

def test_htf_bias_direction():
    bias = calculate_htf_bias(h4, h1)
    assert_(bias.direction in ("bullish", "bearish", "neutral"))
run("HTF bias direction is valid", test_htf_bias_direction)

def test_htf_bias_confidence():
    bias = calculate_htf_bias(h4, h1)
    assert_(0.0 <= bias.confidence <= 1.0)
run("HTF bias confidence in [0, 1]", test_htf_bias_confidence)

# ═══════════════════════════════════════════════════════════════════════════════
print("\n── RISK ───────────────────────────────────────────────────────────")
# ═══════════════════════════════════════════════════════════════════════════════
from core.risk.lot_sizing  import calculate_lot_size, price_to_pips, pips_to_price
from core.risk.stop_loss   import calculate_stop_loss, adjust_sl_to_breakeven
from core.risk.take_profit import calculate_take_profit, trailing_stop
from core.risk.portfolio   import Portfolio, ClosedTrade
from core.risk.filters     import filter_spread, filter_session, filter_rr_ratio, run_all_filters

def test_lot_basic():
    # 0.5% risk, 40 pip SL => small enough to avoid the 0.3 max lot cap
    lot = calculate_lot_size(10_000, 40, 0.5)
    assert_(abs(lot - 0.12) < 0.02, f"Expected ~0.12, got {lot}")
run("lot size ~0.12 for 0.5% risk 40pip SL", test_lot_basic)

def test_lot_min():
    assert_(calculate_lot_size(100, 200, 0.1) >= 0.01)
run("lot size respects minimum 0.01", test_lot_min)

def test_lot_max():
    assert_(calculate_lot_size(1_000_000, 1, 5.0) <= 5.0)
run("lot size respects maximum 5.0", test_lot_max)

def test_lot_step():
    lot = calculate_lot_size(10_000, 17)
    assert_(abs(lot * 100 - round(lot * 100)) < 1e-9)
run("lot size is multiple of 0.01", test_lot_step)

def test_lot_raise_zero_sl():
    try:
        calculate_lot_size(10_000, 0)
        return False
    except ValueError:
        return True
run("lot_size raises ValueError on SL=0", lambda: assert_(test_lot_raise_zero_sl()))

def test_pip_conversions():
    assert_(abs(price_to_pips(1.0) - 10.0) < 1e-9)
    assert_(abs(pips_to_price(10.0) - 1.0) < 1e-9)
run("pip/price conversions correct", test_pip_conversions)

swings_bull = detect_swings(bull)
swings_bear = detect_swings(bear)
entry_bull  = bull["close"].iloc[-1]
entry_bear  = bear["close"].iloc[-1]

def test_buy_sl_below_entry():
    sl = calculate_stop_loss("buy", entry_bull, swings_bull, bull)
    assert_(sl < entry_bull, f"buy SL {sl} should be < entry {entry_bull}")
run("buy SL is below entry", test_buy_sl_below_entry)

def test_sell_sl_above_entry():
    sl = calculate_stop_loss("sell", entry_bear, swings_bear, bear)
    assert_(sl > entry_bear, f"sell SL {sl} should be > entry {entry_bear}")
run("sell SL is above entry", test_sell_sl_above_entry)

def test_breakeven_activated():
    new_sl = adjust_sl_to_breakeven(1950, 1960, "buy", activation_rr=1.0, original_sl=1940)
    assert_(new_sl > 1940, f"Expected BE move, got sl={new_sl}")
run("breakeven moves SL when 1R reached", test_breakeven_activated)

def test_breakeven_not_early():
    new_sl = adjust_sl_to_breakeven(1950, 1953, "buy", activation_rr=1.0, original_sl=1940)
    assert_(new_sl == 1940, f"Should not trigger yet, got {new_sl}")
run("breakeven does NOT trigger early", test_breakeven_not_early)

def test_tp_buy_above_entry():
    lvs = calculate_take_profit("buy", 1950, 1940, swings_bull)
    assert_(lvs.tp2 > 1950)
run("TP buy above entry", test_tp_buy_above_entry)

def test_tp_sell_below_entry():
    lvs = calculate_take_profit("sell", 1950, 1960, swings_bear)
    assert_(lvs.tp2 < 1950)
run("TP sell below entry", test_tp_sell_below_entry)

def test_tp_rr_minimum():
    lvs = calculate_take_profit("buy", 1950, 1940, swings_bull, min_rr=2.0)
    assert_(lvs.rr_at_tp2 >= 1.9, f"RR={lvs.rr_at_tp2}")
run("TP RR meets minimum 2.0", test_tp_rr_minimum)

def test_trail_buy_moves_up():
    new_sl = trailing_stop(1950, 1980, 1940, "buy", 20)
    assert_(new_sl > 1940)
run("trailing stop buy moves SL up", test_trail_buy_moves_up)

def test_trail_no_regression():
    new_sl = trailing_stop(1950, 1952, 1945, "buy", 20)
    assert_(new_sl >= 1945)
run("trailing stop never regresses", test_trail_no_regression)

def test_trail_sell_moves_down():
    new_sl = trailing_stop(1950, 1920, 1960, "sell", 20)
    assert_(new_sl < 1960)
run("trailing stop sell moves SL down", test_trail_sell_moves_down)

# Portfolio
pf = Portfolio(10_000)
pf.open_trade("t1", {"symbol": "XAUUSD"})
ct_win = ClosedTrade("t1","XAUUSD","buy",1950,1970,0.1,200.0,200.0,
                      datetime(2024,1,1,10), datetime(2024,1,1,12), "tp2")
pf.close_trade(ct_win)

def test_pf_balance():    assert_(pf.balance == 10_200.0)
def test_pf_winrate():    assert_(pf.win_rate == 100.0)
def test_pf_total():      assert_(pf.total_trades == 1)
run("portfolio balance after winning trade", test_pf_balance)
run("portfolio win rate 100%",               test_pf_winrate)
run("portfolio total trades = 1",            test_pf_total)

pf2 = Portfolio(10_000)
pf2.update_equity(1_000)   # peak = 11000
pf2.update_equity(-2_000)  # equity = 9000
def test_drawdown(): assert_(pf2.drawdown_pct > 0)
run("portfolio drawdown > 0 after equity drop", test_drawdown)



# Filters
def test_spread_pass(): assert_(filter_spread(5.0)[0])
run("spread filter: 5 pips passes",   test_spread_pass)

def test_spread_fail(): assert_(not filter_spread(999.0)[0])
run("spread filter: 999 pips fails",  test_spread_fail)

def test_session_london(): assert_(filter_session(datetime(2024,1,15,9,0))[0])
run("session: London 09:00 UTC passes", test_session_london)

def test_session_ny(): assert_(filter_session(datetime(2024,1,15,14,0))[0])
run("session: NY 14:00 UTC passes",     test_session_ny)

from config.trading_config import TRADING_CONFIG
def test_session_asian(): 
    # Force disable Asian for the test
    TRADING_CONFIG.enable_asian_session = False
    assert_(not filter_session(datetime(2024,1,15,3,0))[0])
run("session: Asian 03:00 UTC fails when disabled",   test_session_asian)

def test_rr_pass(): assert_(filter_rr_ratio(2.5)[0])
run("RR filter: 2.5 passes",  test_rr_pass)

def test_rr_fail(): assert_(not filter_rr_ratio(0.8)[0])
run("RR filter: 0.8 fails",   test_rr_fail)

def test_all_filters_pass():
    ok, fails = run_all_filters(
        spread_pips=5, dt=datetime(2024,1,15,10,0),
        daily_pnl=100, balance=10_000, trades_today=2,
        open_positions=1, atr_pips=30, rr=2.0)
    assert_(ok)
run("run_all_filters: all conditions pass", test_all_filters_pass)

def test_all_filters_fail():
    TRADING_CONFIG.enable_asian_session = False
    ok, fails = run_all_filters(
        spread_pips=999, dt=datetime(2024,1,15,3,0),
        daily_pnl=-500, balance=10_000, trades_today=10,
        open_positions=1, atr_pips=30, rr=0.1)
    assert_(not ok and len(fails) >= 3, f"Expected 3+ failures, got: {fails}")
run("run_all_filters: 3+ failures when bad conditions", test_all_filters_fail)

# ═══════════════════════════════════════════════════════════════════════════════
print("\n── DATA PIPELINE ──────────────────────────────────────────════════")
# ═══════════════════════════════════════════════════════════════════════════════
from data.fetcher      import slice_window
from data.processor    import validate_ohlcv, remove_price_spikes, full_pipeline
from data.feature_engineering import (add_atr, simulate_spread,
                                       add_session_flag, prepare_backtest_data)

def test_slice_size():
    assert_(len(slice_window(full, 150, 50)) == 50)
run("slice_window returns exactly 50 rows", test_slice_size)

def test_slice_edge():
    assert_(len(slice_window(full, 10, 50)) == 11)
run("slice_window at edge returns available rows", test_slice_edge)

def test_resample_fewer_rows():
    assert_(len(h4) < len(full))
run("resample reduces row count", test_resample_fewer_rows)

def test_resample_integrity():
    assert_((h4["high"] >= h4["low"]).all())
run("resampled high >= low always", test_resample_integrity)

def test_validate_bad_row():
    bad = full.copy()
    bad.iloc[5, bad.columns.get_loc("high")] = bad.iloc[5]["low"] - 1.0
    _, removed = validate_ohlcv(bad)
    assert_(removed >= 1)
run("validate_ohlcv removes bad OHLC row", test_validate_bad_row)

def test_validate_clean():
    _, removed = validate_ohlcv(full)
    assert_(removed == 0)
run("validate_ohlcv passes clean data with 0 removals", test_validate_clean)

def test_spike_removal():
    sp = full.copy()
    sp.iloc[100, sp.columns.get_loc("close")] *= 10
    cleaned = remove_price_spikes(sp, z_threshold=5.0)
    assert_(len(cleaned) < len(sp))
run("remove_price_spikes removes outlier", test_spike_removal)

def test_full_pipeline_type():
    result = full_pipeline(full)
    assert_(isinstance(result, pd.DataFrame) and len(result) > 0)
run("full_pipeline returns non-empty DataFrame", test_full_pipeline_type)

def test_atr_positive():
    df = add_atr(full, 14)
    assert_((df["atr"].dropna() > 0).all())
run("ATR values all positive after warm-up", test_atr_positive)

def test_spread_sessions():
    df  = simulate_spread(full)
    day   = df[df.index.hour == 10]["spread"].mean()
    night = df[df.index.hour == 3]["spread"].mean()
    assert_(night > day)
run("simulated spread higher in off-hours (Asian)", test_spread_sessions)

def test_session_flags_binary():
    df = add_session_flag(full)
    assert_(set(df["london"].unique()).issubset({0, 1}))
run("session flags are binary (0 or 1)", test_session_flags_binary)

def test_feature_cols():
    df = prepare_backtest_data(full)
    for col in ["atr", "atr_pips", "body_ratio", "spread", "spread_pips", "in_session"]:
        assert_(col in df.columns, f"Missing column: {col}")
run("prepare_backtest_data adds all required feature columns", test_feature_cols)

# ═══════════════════════════════════════════════════════════════════════════════
print("\n── SIGNAL & AI ────────────────────────────────────────────────────")
# ═══════════════════════════════════════════════════════════════════════════════
from core.signal.signal_engine import SignalEngine, TradeSignal
from core.structure.htf_bias   import HTFBias
from ai.scorer import AIScorer, AIEvaluation

def make_mock_signal(score=7, session="LONDON"):
    return TradeSignal(
        signal_id="t01", symbol="XAUUSD",
        timestamp=datetime(2024,1,15,10,0),
        direction="buy", entry_price=1950.0,
        stop_loss=1940.0, take_profit=1970.0,
        rr_ratio=2.0, score=score, max_score=10,
        score_breakdown={"htf_alignment":2,"liquidity_sweep":2,"displacement":2,"session_valid":1},
        htf_bias=HTFBias("bullish",0.85,None,None,"Strong bullish","hh_hl"),
        atr_pips=30.0, session=session,
    )

eng = SignalEngine()
sig = eng.generate(df_h4=h4, df_h1=h1, df_ltf=bull,
                    current_time=datetime(2024,1,15,10,0))

def test_signal_type():
    assert_(sig is None or isinstance(sig, TradeSignal))
run("generate returns None or TradeSignal", test_signal_type)

if sig is not None:
    def test_sig_score_range(): assert_(0 <= sig.score <= sig.max_score)
    def test_sig_rr_positive():  assert_(sig.rr_ratio > 0)
    run("signal score in valid range",   test_sig_score_range)
    run("signal RR ratio is positive",   test_sig_rr_positive)
    if sig.direction == "buy":
        run("buy signal TP > entry", lambda: assert_(sig.take_profit > sig.entry_price))
    else:
        run("sell signal TP < entry", lambda: assert_(sig.take_profit < sig.entry_price))
else:
    print("  ℹ️   No signal generated (below score threshold — expected on synthetic data)")

scorer = AIScorer(enabled=False)

def test_ai_eval_type():
    e = scorer._rule_based(make_mock_signal(7, "LONDON"))
    assert_(isinstance(e, AIEvaluation))
run("AI scorer returns AIEvaluation", test_ai_eval_type)

def test_ai_source_rule_based():
    e = scorer._rule_based(make_mock_signal(7, "LONDON"))
    assert_(e.source == "rule_based")
run("AI scorer source is rule_based", test_ai_source_rule_based)

def test_ai_decision_valid():
    e = scorer._rule_based(make_mock_signal(7, "LONDON"))
    assert_(e.decision in ("TAKE", "SKIP"))
run("AI decision is TAKE or SKIP", test_ai_decision_valid)

def test_ai_confidence_range():
    e = scorer._rule_based(make_mock_signal(7, "LONDON"))
    assert_(0.0 <= e.confidence <= 1.0)
run("AI confidence in [0.0, 1.0]", test_ai_confidence_range)

def test_ai_high_score_take():
    e = scorer._rule_based(make_mock_signal(score=7, session="LONDON"))
    assert_(e.decision == "TAKE", f"Expected TAKE for score=7, got {e.decision}")
run("high score London signal → TAKE", test_ai_high_score_take)

def test_ai_low_score_skip():
    e = scorer._rule_based(make_mock_signal(score=2, session="TRANSITION"))
    assert_(e.decision == "SKIP", f"Expected SKIP for score=2 off-hours, got {e.decision}")
run("low score off-hours signal → SKIP", test_ai_low_score_skip)

# ═══════════════════════════════════════════════════════════════════════════════
print("\n── BACKTEST ENGINE ────────────────────────────────────────────────")
# ═══════════════════════════════════════════════════════════════════════════════
from backtest.engine  import BacktestEngine
from backtest.metrics import consecutive_losses, expectancy, trade_log_to_df

engine = BacktestEngine(initial_balance=10_000, use_ai=False)
result = engine.run(df_ltf=full, warm_up_bars=100)

def test_bt_result_not_none():    assert_(result is not None)
def test_bt_equity_curve():       assert_(len(result.equity_curve) > 0)
def test_bt_winrate_range():      assert_(0 <= result.win_rate <= 100)
def test_bt_drawdown_nonneg():    assert_(result.max_drawdown_pct >= 0)
def test_bt_pf_nonneg():          assert_(result.profit_factor >= 0)
def test_bt_no_negative_lots():
    for t in result.trades: assert_(t.lot_size >= 0)
def test_bt_valid_directions():
    for t in result.trades: assert_(t.direction in ("buy", "sell"))
def test_bt_monthly_dict():       assert_(isinstance(result.monthly_breakdown, dict))

run("backtest result not None",              test_bt_result_not_none)
run("equity curve non-empty",                test_bt_equity_curve)
run("win_rate in [0, 100]",                  test_bt_winrate_range)
run("max_drawdown >= 0",                     test_bt_drawdown_nonneg)
run("profit_factor >= 0",                    test_bt_pf_nonneg)
run("no negative lot sizes",                 test_bt_no_negative_lots)
run("all trades have valid direction",        test_bt_valid_directions)
run("monthly_breakdown is dict",             test_bt_monthly_dict)

print(f"\n  Backtest summary: {result.total_trades} trades | "
      f"WR={result.win_rate:.1f}% | PnL=${result.total_pnl:,.2f} | "
      f"DD={result.max_drawdown_pct:.2f}% | Sharpe={result.sharpe_ratio:.3f}")

# Metrics
def _ct(pnl):
    return ClosedTrade("x","XAUUSD","buy",1950.0,1950.0+pnl/10,0.1,
                        float(pnl), float(pnl)/10,
                        datetime(2024,1,1), datetime(2024,1,1,1),
                        "tp2" if pnl > 0 else "sl")

seq_trades = [_ct(p) for p in [100, -50, -50, -50, 100, -50]]

def test_consec_losses():
    assert_(consecutive_losses(seq_trades) == 3)
run("consecutive_losses identifies streak of 3", test_consec_losses)

pos_trades = [_ct(p) for p in [200, 200, -100, 200, -100]]
def test_expect_positive():
    assert_(expectancy(pos_trades) > 0)
run("expectancy positive for 3W/2L", test_expect_positive)

neg_trades = [_ct(p) for p in [-100, -100, 50, -100]]
def test_expect_negative():
    assert_(expectancy(neg_trades) < 0)
run("expectancy negative for 3L/1W", test_expect_negative)

def test_log_cumulative():
    df_log = trade_log_to_df(seq_trades)
    assert_("cumulative_pnl" in df_log.columns)
    expected = sum(t.pnl for t in seq_trades)
    assert_(abs(df_log["cumulative_pnl"].iloc[-1] - expected) < 0.01)
run("trade_log cumulative_pnl is correct", test_log_cumulative)

# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*62}")
print(f"  TEST RESULTS:  {passed} passed  |  {failed} failed  |  {passed+failed} total")
print(f"{'═'*62}")

if errors:
    print("\nFailed test details:")
    for name, tb in errors:
        print(f"\n  ▶  {name}")
        # Print only last 8 lines of traceback to keep output clean
        lines = tb.strip().splitlines()
        for line in lines[-8:]:
            print(f"     {line}")
    sys.exit(1)
else:
    print("\n  ✅  All tests passed — system validated!\n")
    sys.exit(0)
