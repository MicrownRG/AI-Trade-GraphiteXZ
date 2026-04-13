import pandas as pd
import numpy as np
from typing import Dict, Optional

class TrendLogic:
    def __init__(self, ema_periods=[9, 21, 50, 200], adx_period: int = 14):
        self.ema_periods = ema_periods
        self.adx_period = adx_period

    def calculate_emas(self, df: pd.DataFrame) -> pd.DataFrame:
        emas = pd.DataFrame(index=df.index)
        for p in self.ema_periods:
            emas[f'ema_{p}'] = df['close'].ewm(span=p, adjust=False).mean()
        return emas

    def calculate_adx(self, df: pd.DataFrame) -> pd.Series:
        """Standard ADX calculation."""
        df = df.copy()
        df['tr'] = np.maximum(df['high'] - df['low'], 
                             np.maximum(abs(df['high'] - df['close'].shift()), 
                                      abs(df['low'] - df['close'].shift())))
        
        df['plus_dm'] = np.where((df['high'] - df['high'].shift() > df['low'].shift() - df['low']) & 
                                (df['high'] - df['high'].shift() > 0), 
                                df['high'] - df['high'].shift(), 0)
        df['minus_dm'] = np.where((df['low'].shift() - df['low'] > df['high'] - df['high'].shift()) & 
                                 (df['low'].shift() - df['low'] > 0), 
                                 df['low'].shift() - df['low'], 0)
        
        tr_smooth = df['tr'].rolling(self.adx_period).mean()
        plus_di = 100 * (df['plus_dm'].rolling(self.adx_period).mean() / tr_smooth)
        minus_di = 100 * (df['minus_dm'].rolling(self.adx_period).mean() / tr_smooth)
        
        dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di))
        adx = dx.rolling(self.adx_period).mean()
        return adx

    def analyze_trend(self, df: pd.DataFrame) -> Dict:
        """Determine trend bias based on EMAs and ADX."""
        emas = self.calculate_emas(df)
        adx = self.calculate_adx(df)
        
        curr_price = df['close'].iloc[-1]
        ema_50 = emas['ema_50'].iloc[-1]
        ema_200 = emas['ema_200'].iloc[-1]
        curr_adx = adx.iloc[-1]
        
        bias = "NEUTRAL"
        if curr_price > ema_200 and curr_price > ema_50:
            bias = "BULLISH"
        elif curr_price < ema_200 and curr_price < ema_50:
            bias = "BEARISH"
            
        # --- CANDLESTICK REJECTION LOGIC (Ekor Panjang) ---
        rejection = "NONE"
        recent_candles = df.tail(2)
        for _, candle in recent_candles.iterrows():
            body = max(0.0001, abs(candle['close'] - candle['open'])) # avoid div zero
            upper_wick = candle['high'] - max(candle['close'], candle['open'])
            lower_wick = min(candle['close'], candle['open']) - candle['low']
            rng = max(0.0001, candle['high'] - candle['low'])
            
            # Strong Bearish Rejection (Shooting Star / Ekor Atas Panjang)
            if upper_wick >= body * 2.0 and upper_wick > rng * 0.40:
                bias = "BEARISH"
                rejection = "BEARISH"
            # Strong Bullish Rejection (Hammer / Ekor Bawah Panjang)
            elif lower_wick >= body * 2.0 and lower_wick > rng * 0.40:
                bias = "BULLISH"
                rejection = "BULLISH"
            
        return {
            "bias": bias,
            "rejection": rejection,
            "adx": curr_adx,
            "trend_strength": "STRONG" if curr_adx > 25 else "WEAK",
            "ema_cross": emas['ema_9'].iloc[-1] > emas['ema_21'].iloc[-1]
        }
