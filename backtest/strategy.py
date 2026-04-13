"""
Backtest Strategy interface.

Allows plugging different strategy variants into the backtest engine
for comparison without touching engine internals.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional
import pandas as pd
from datetime import datetime

from core.signal.signal_engine import SignalEngine, TradeSignal
from core.signal.advanced_signal_engine import AdvancedSignalEngine


class BaseStrategy(ABC):
    name: str = "BaseStrategy"

    @abstractmethod
    def on_bar(
        self,
        df_h4: pd.DataFrame,
        df_h1: pd.DataFrame,
        df_ltf: pd.DataFrame,
        df_m5: Optional[pd.DataFrame],
        current_time: datetime,
    ) -> Optional[TradeSignal]:
        """Called on every new bar. Returns a signal or None."""


class ICTStrategy(BaseStrategy):
    """
    Standard ICT-based strategy using BOS / CHOCH / Liquidity / Displacement.
    """
    name = "ICT_Standard"

    def __init__(self):
        self.engine = SignalEngine()

    def on_bar(self, df_h4, df_h1, df_ltf, df_m5, current_time):
        return self.engine.generate(
            df_h4=df_h4,
            df_h1=df_h1,
            df_ltf=df_ltf,
            current_time=current_time,
        )


class AdvancedICTStrategy(BaseStrategy):
    """
    ICT strategy with FVG / Order Block refinement on M5.
    """
    name = "ICT_Advanced_FVG"

    def __init__(self):
        self.engine = AdvancedSignalEngine()

    def on_bar(self, df_h4, df_h1, df_ltf, df_m5, current_time):
        signal = self.engine.generate(
            df_h4=df_h4,
            df_h1=df_h1,
            df_m15=df_ltf,
            df_m5=df_m5 if df_m5 is not None else df_ltf,
            df_m1=df_ltf,
            current_time=current_time,
        )
        if signal and df_m5 is not None and not df_m5.empty:
            signal = self.engine.refine_entry(signal, df_m5)
        return signal
