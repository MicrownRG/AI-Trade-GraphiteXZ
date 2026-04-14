"""Tests for signal engine and AI scorer."""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.signal.signal_engine import SignalEngine, TradeSignal
from ai.scorer import AIScorer, AIEvaluation
from datetime import datetime


class TestSignalEngine:
    def test_returns_none_or_signal(self, bullish_df, h1_df, h4_df):
        engine = SignalEngine()
        result = engine.generate(
            df_h4=h4_df,
            df_h1=h1_df,
            df_ltf=bullish_df,
            current_time=datetime(2024, 1, 15, 10, 0),  # London session
        )
        assert result is None or isinstance(result, TradeSignal)

    def test_signal_score_within_range(self, bullish_df, h1_df, h4_df):
        engine = SignalEngine()
        result = engine.generate(
            df_h4=h4_df,
            df_h1=h1_df,
            df_ltf=bullish_df,
            current_time=datetime(2024, 1, 15, 10, 0),
        )
        if result:
            assert 0 <= result.score <= result.max_score

    def test_signal_rr_positive(self, bullish_df, h1_df, h4_df):
        engine = SignalEngine()
        result = engine.generate(
            df_h4=h4_df,
            df_h1=h1_df,
            df_ltf=bullish_df,
            current_time=datetime(2024, 1, 15, 10, 0),
        )
        if result:
            assert result.rr_ratio > 0

    def test_buy_signal_tp_above_entry(self, bullish_df, h1_df, h4_df):
        engine = SignalEngine()
        result = engine.generate(
            df_h4=h4_df,
            df_h1=h1_df,
            df_ltf=bullish_df,
            current_time=datetime(2024, 1, 15, 10, 0),
        )
        if result and result.direction == "buy":
            assert result.take_profit > result.entry_price

    def test_sell_signal_tp_below_entry(self, bearish_df, h1_df, h4_df):
        from data.fetcher import resample_to_htf
        h4 = resample_to_htf(bearish_df, "4h")
        h1 = resample_to_htf(bearish_df, "1h")
        engine = SignalEngine()
        result = engine.generate(
            df_h4=h4,
            df_h1=h1,
            df_ltf=bearish_df,
            current_time=datetime(2024, 1, 15, 10, 0),
        )
        if result and result.direction == "sell":
            assert result.take_profit < result.entry_price


class TestAIScorer:
    def _make_signal(self):
        from core.structure.htf_bias import HTFBias
        return TradeSignal(
            signal_id="test01",
            symbol="XAUUSD",
            timestamp=datetime(2024, 1, 15, 10, 0),
            direction="buy",
            entry_price=1950.0,
            stop_loss=1940.0,
            take_profit=1970.0,
            rr_ratio=2.0,
            score=7,
            max_score=10,
            score_breakdown={"htf_alignment": 2, "liquidity_sweep": 2, "displacement": 2, "session_valid": 1},
            htf_bias=HTFBias(
                direction="bullish", confidence=0.85,
                last_bos=None, last_choch=None,
                trend_str="Strong bullish", swing_structure="hh_hl",
            ),
            atr_pips=30.0,
            session="London",
        )

    def test_rule_based_returns_evaluation(self):
        scorer = AIScorer(enabled=False)
        signal = self._make_signal()
        eval_  = scorer.evaluate(signal)
        assert isinstance(eval_, AIEvaluation)
        assert eval_.source == "rule_based"
        assert eval_.decision in ("TAKE", "SKIP")
        assert 0.0 <= eval_.confidence <= 1.0

    def test_high_score_signal_gets_taken(self):
        scorer = AIScorer(enabled=True)
        signal = self._make_signal()
        eval_  = scorer._rule_based(signal)
        # Score 7/10 in London session should be TAKE
        assert eval_.decision == "TAKE"

    def test_low_score_signal_skipped(self):
        scorer  = AIScorer(enabled=True)
        signal  = self._make_signal()
        signal.score = 2       # very low
        signal.session = "Off-Hours"
        eval_   = scorer._rule_based(signal)
        assert eval_.decision == "SKIP"
