import pandas as pd
import numpy as np
from typing import Dict

class ScalpLogic:
    def __init__(self, rsi_period: int = 14, bb_period: int = 20, bb_std: float = 2.0):
        self.rsi_period = rsi_period
        self.bb_period = bb_period
        self.bb_std = bb_std

    def calculate_rsi(self, df: pd.DataFrame) -> pd.Series:
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=self.rsi_period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def calculate_bollinger_bands(self, df: pd.DataFrame) -> Dict[str, pd.Series]:
        mid = df['close'].rolling(window=self.bb_period).mean()
        std = df['close'].rolling(window=self.bb_period).std()
        upper = mid + (self.bb_std * std)
        lower = mid - (self.bb_std * std)
        return {"upper": upper, "mid": mid, "lower": lower}

    def get_scalp_signals(self, df: pd.DataFrame) -> Dict:
        """Weighted scoring for scalping: RSI + BB + Momentum."""
        rsi = self.calculate_rsi(df)
        bb = self.calculate_bollinger_bands(df)
        
        curr_price = df['close'].iloc[-1]
        curr_rsi = rsi.iloc[-1]
        bb_upper = bb['upper'].iloc[-1]
        bb_lower = bb['lower'].iloc[-1]
        
        score = 0
        signal = "NEUTRAL"
        
        # Pullback Logic
        if curr_price <= bb_lower and curr_rsi < 30:
            score += 3
            signal = "BUY"
        elif curr_price >= bb_upper and curr_rsi > 70:
            score += 3
            signal = "SELL"
            
        # Momentum & Divergence Check (Sniper Detection)
        momentum = df['close'].diff(3).iloc[-1]
        divergence = self.detect_divergence(df, rsi)
        
        if signal == "BUY" and momentum > 0:
            score += 2
        elif signal == "SELL" and momentum < 0:
            score += 2
            
        # Massive bonus for Sniper Divergence
        if divergence == "BULLISH" and signal == "BUY":
            score += 5
        elif divergence == "BEARISH" and signal == "SELL":
            score += 5
            
        return {
            "signal": signal,
            "score": score,
            "rsi": curr_rsi,
            "bb_touch": curr_price <= bb_lower or curr_price >= bb_upper,
            "divergence": divergence
        }

    def detect_divergence(self, df: pd.DataFrame, rsi: pd.Series, lookback: int = 20) -> str:
        """
        Detects RSI Divergence (Hidden institutional momentum).
        Bullish: Price makes Lower Low, but RSI makes Higher Low.
        Bearish: Price makes Higher High, but RSI makes Lower High.
        """
        if len(df) < lookback + 5:
            return "NONE"
            
        recent_df = df.tail(lookback)
        recent_rsi = rsi.tail(lookback)
        
        # Find local minimums in price
        lows = recent_df['low']
        min_idx_1 = lows.idxmin()
        # Find a previous minimum before the absolute minimum (to compare swings)
        prev_df = recent_df.loc[:min_idx_1].iloc[:-3] if not recent_df.loc[:min_idx_1].empty else pd.DataFrame()
        
        if not prev_df.empty:
            min_idx_2 = prev_df['low'].idxmin()
            
            p1, p2 = lows[min_idx_1], lows[min_idx_2]
            r1, r2 = rsi[min_idx_1], rsi[min_idx_2]
            
            # Lower low in price, but higher low in RSI
            if p1 < p2 and r1 > r2 and r1 < 40:
                return "BULLISH"

        # Find local maximums in price
        highs = recent_df['high']
        max_idx_1 = highs.idxmax()
        prev_df_high = recent_df.loc[:max_idx_1].iloc[:-3] if not recent_df.loc[:max_idx_1].empty else pd.DataFrame()
        
        if not prev_df_high.empty:
            max_idx_2 = prev_df_high['high'].idxmax()
            
            p1, p2 = highs[max_idx_1], highs[max_idx_2]
            r1, r2 = rsi[max_idx_1], rsi[max_idx_2]
            
            # Higher high in price, but lower high in RSI
            if p1 > p2 and r1 < r2 and r1 > 60:
                return "BEARISH"
                
        return "NONE"
