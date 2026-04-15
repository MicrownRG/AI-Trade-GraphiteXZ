"""Tests for signal engine and AI scorer."""
import pytest
import sys, os
import numpy as np
import pandas as pd
from types import SimpleNamespace
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.signal.signal_engine import SignalEngine, TradeSignal
from core.signal.advanced_signal_engine import AdvancedSignalEngine
from ai.scorer import AIScorer, AIEvaluation
from datetime import datetime
from config.trading_config import TRADING_CONFIG, TradeMode


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
            session="LONDON",
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
        signal.session = "TRANSITION"
        eval_   = scorer._rule_based(signal)
        assert eval_.decision == "SKIP"


class TestAdvancedSignalEngine:
    @staticmethod
    def _make_df(rows: int = 240, base: float = 4850.0) -> pd.DataFrame:
        idx = pd.date_range("2024-01-02 00:00:00", periods=rows, freq="min", tz="UTC")
        close = np.full(rows, base, dtype=float)
        open_ = close.copy()
        high = close + 0.4
        low = close - 0.4
        tick_volume = np.full(rows, 100.0, dtype=float)
        return pd.DataFrame(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "tick_volume": tick_volume,
            },
            index=idx,
        )

    def test_transitional_guard_blocks_premature_buy_when_ltf_opposes_htf(self, monkeypatch):
        engine = AdvancedSignalEngine()
        df_h4 = self._make_df(rows=300, base=4855.0)
        df_h1 = self._make_df(rows=300, base=4852.0)
        df_m30 = self._make_df(rows=300, base=4851.0)
        df_m15 = self._make_df(rows=300, base=4850.0)
        df_m5 = self._make_df(rows=300, base=4849.8)
        df_m1 = self._make_df(rows=300, base=4849.7)

        tf_h4 = SimpleNamespace(tf="h4", bias="BULLISH", adx=28.0, ema_cross=True, at_zone="NONE", rejection="NONE")
        tf_h1 = SimpleNamespace(tf="h1", bias="BULLISH", adx=29.0, ema_cross=True, at_zone="NONE", rejection="NONE")
        tf_m30 = SimpleNamespace(tf="m30", bias="BULLISH", adx=22.0, ema_cross=True, at_zone="NONE", rejection="NONE")
        tf_m15 = SimpleNamespace(tf="m15", bias="NEUTRAL", adx=18.0, ema_cross=False, at_zone="NONE", rejection="NONE")
        tf_m5 = SimpleNamespace(tf="m5", bias="BEARISH", adx=24.0, ema_cross=False, at_zone="NONE", rejection="NONE")
        tf_m1 = SimpleNamespace(tf="m1", bias="BEARISH", adx=25.0, ema_cross=False, at_zone="NONE", rejection="NONE")

        mtf = SimpleNamespace(
            direction="buy",
            bull_score=15.0,
            bear_score=8.0,
            is_strong=True,
            htf_aligned=False,
            reversal_detected=True,
            master_bias="BULLISH",
            tfs={"h4": tf_h4, "h1": tf_h1, "m30": tf_m30, "m15": tf_m15, "m5": tf_m5, "m1": tf_m1},
        )

        monkeypatch.setattr(engine.multi_tf, "analyze", lambda **kwargs: mtf)
        monkeypatch.setattr(engine.smc, "detect_structure", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(engine.smc, "get_order_blocks", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(engine.smc, "get_fvgs", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(engine.smc, "has_liquidity_sweep", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(engine.scalp, "get_scalp_signals", lambda *_args, **_kwargs: {"signal": "NEUTRAL", "divergence": "NONE", "rsi": 50})

        prev_mode = TRADING_CONFIG.current_mode
        try:
            TRADING_CONFIG.current_mode = TradeMode.VERY_AGGRESSIVE
            signal = engine.generate(
                df_h4=df_h4,
                df_h1=df_h1,
                df_m30=df_m30,
                df_m15=df_m15,
                df_m5=df_m5,
                df_m1=df_m1,
                symbol="XAUUSD",
            )
        finally:
            TRADING_CONFIG.current_mode = prev_mode

        assert signal is None
