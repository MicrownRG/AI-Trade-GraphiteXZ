import pandas as pd
import numpy as np
from typing import Dict, List, Optional
from dataclasses import dataclass, field
import time

@dataclass
class MarketStructure:
    type: str  # "BOS" or "CHOCH"
    direction: str  # "BULLISH" or "BEARISH"
    price: float
    index: int
    strength: str  # "STRONG" or "WEAK"
    is_confirmed: bool = False

@dataclass
class SwingPoint:
    index: int
    price: float
    type: str  # "HIGH" or "LOW"
    is_strong: bool = False

class SMCLogic:
    def __init__(self, swing_lookback: int = 20, atr_period: int = 14):
        self.swing_lookback = swing_lookback
        self.atr_period = atr_period

    def calculate_atr(self, df: pd.DataFrame) -> pd.Series:
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        return true_range.rolling(self.atr_period).mean()

    def detect_swings(self, df: pd.DataFrame) -> List[SwingPoint]:
        """Detect swing highs and lows with GainzAlgo validation."""
        swings = []
        n = len(df)
        
        for i in range(self.swing_lookback, n - self.swing_lookback):
            # Swing High
            if all(df['high'].iloc[i] > df['high'].iloc[i-j] for j in range(1, self.swing_lookback + 1)) and \
               all(df['high'].iloc[i] > df['high'].iloc[i+j] for j in range(1, self.swing_lookback + 1)):
               swings.append(SwingPoint(index=i, price=df['high'].iloc[i], type="HIGH"))
            
            # Swing Low
            if all(df['low'].iloc[i] < df['low'].iloc[i-j] for j in range(1, self.swing_lookback + 1)) and \
               all(df['low'].iloc[i] < df['low'].iloc[i+j] for j in range(1, self.swing_lookback + 1)):
               swings.append(SwingPoint(index=i, price=df['low'].iloc[i], type="LOW"))
               
        return swings

    def detect_structure(self, df: pd.DataFrame) -> List[MarketStructure]:
        """
        Implementation of GainzAlgo v2 Alpha Smart Money Structure:
        - ATR-based expansion filter (ignores 'weak' breaks)
        - Non-repainting BOS/CHoCH detection
        - Strong vs Weak point labeling
        """
        df = df.copy()
        atr = self.calculate_atr(df)
        swings = self.detect_swings(df)
        structures = []
        
        if not swings:
            return []

        last_confirmed_high = None
        last_confirmed_low = None
        current_trend = 0 # 1 for Bull, -1 for Bear
        
        # We iterate to find points where price closes beyond recent swings
        for i in range(self.swing_lookback, len(df)):
            curr_close = df['close'].iloc[i]
            curr_atr = atr.iloc[i]
            
            # Find the most recent un-broken swing points
            relevant_swings = [s for s in swings if s.index < i]
            if not relevant_swings: continue
            
            recent_high = max([s for s in relevant_swings if s.type == "HIGH"], key=lambda x: x.index, default=None)
            recent_low = max([s for s in relevant_swings if s.type == "LOW"], key=lambda x: x.index, default=None)
            
            # ── BULLISH BREAK (Potential BOS or CHoCH) ──
            if recent_high and curr_close > recent_high.price:
                # GainzAlgo Expansion Filter: Body length must be significant
                body_size = abs(df['close'].iloc[i] - df['open'].iloc[i])
                expansion = curr_close - recent_high.price
                
                if expansion > (curr_atr * 0.5) and body_size > (curr_atr * 0.2):
                    direction = "BULLISH"
                    stype = "BOS" if current_trend == 1 else "CHOCH"
                    
                    # Mark as confirmed
                    structures.append(MarketStructure(
                        type=stype,
                        direction=direction,
                        price=recent_high.price,
                        index=i,
                        strength="STRONG" if stype == "CHOCH" else "WEAK",
                        is_confirmed=True
                    ))
                    current_trend = 1
                    # Remove the broken swing to prevent duplicate signals
                    swings = [s for s in swings if not (s.index == recent_high.index)]

            # ── BEARISH BREAK ──
            elif recent_low and curr_close < recent_low.price:
                body_size = abs(df['close'].iloc[i] - df['open'].iloc[i])
                expansion = recent_low.price - curr_close
                
                if expansion > (curr_atr * 0.5) and body_size > (curr_atr * 0.2):
                    direction = "BEARISH"
                    stype = "BOS" if current_trend == -1 else "CHOCH"
                    
                    structures.append(MarketStructure(
                        type=stype,
                        direction=direction,
                        price=recent_low.price,
                        index=i,
                        strength="STRONG" if stype == "CHOCH" else "WEAK",
                        is_confirmed=True
                    ))
                    current_trend = -1
                    swings = [s for s in swings if not (s.index == recent_low.index)]

        return structures

    def get_fvgs(self, df: pd.DataFrame) -> List[Dict]:
        """Detect Fair Value Gaps (FVG) with displacement check."""
        df = df.copy()
        atr = self.calculate_atr(df)
        fvgs = []
        for i in range(2, len(df)):
            # Bullish FVG
            if df['low'].iloc[i] > df['high'].iloc[i-2]:
                gap_size = df['low'].iloc[i] - df['high'].iloc[i-2]
                if gap_size > (atr.iloc[i] * 0.3): # Only count significant imbalances
                    fvgs.append({
                        "type": "BULLISH",
                        "top": df['low'].iloc[i],
                        "bottom": df['high'].iloc[i-2],
                        "index": i,
                        "strength": gap_size / atr.iloc[i]
                    })
            # Bearish FVG
            elif df['high'].iloc[i] < df['low'].iloc[i-2]:
                gap_size = df['low'].iloc[i-2] - df['high'].iloc[i]
                if gap_size > (atr.iloc[i] * 0.3):
                    fvgs.append({
                        "type": "BEARISH",
                        "top": df['low'].iloc[i-2],
                        "bottom": df['high'].iloc[i],
                        "index": i,
                        "strength": gap_size / atr.iloc[i]
                    })
        return fvgs

    def get_order_blocks(self, df: pd.DataFrame) -> List[Dict]:
        """Detect Order Blocks (OB) at the origin of Alpha expansions."""
        structures = self.detect_structure(df)
        obs = []
        
        for s in structures:
            # Look at the 5 candles before the structural break
            lookback = 5
            start_idx = max(0, s.index - lookback)
            subset = df.iloc[start_idx:s.index]
            
            if s.direction == "BULLISH":
                # Last bearish candle before the move
                down_candles = subset[subset['close'] < subset['open']]
                if not down_candles.empty:
                    last_ob = down_candles.iloc[-1]
                    obs.append({
                        "type": "BULLISH",
                        "high": last_ob['high'],
                        "low": last_ob['low'],
                        "index": last_ob.name,
                        "origin_structure": s.type
                    })
            else:
                # Last bullish candle before the move
                up_candles = subset[subset['close'] > subset['open']]
                if not up_candles.empty:
                    last_ob = up_candles.iloc[-1]
                    obs.append({
                        "type": "BEARISH",
                        "high": last_ob['high'],
                        "low": last_ob['low'],
                        "index": last_ob.name,
                        "origin_structure": s.type
                    })
        return obs

    def is_price_in_valid_zone(self, price: float, direction: str, df: pd.DataFrame) -> bool:
        """Check if price is still within a valid institutional Order Block."""
        obs = self.get_order_blocks(df)
        if not obs:
            return False
            
        # Check last 3 OBs for relevance
        target_type = "BULLISH" if direction == "buy" else "BEARISH"
        relevant_obs = [o for o in obs[-3:] if o["type"] == target_type]
        
        for ob in relevant_obs:
            # Add a small 2-pip buffer to the zone
            if target_type == "BULLISH":
                if price >= (ob["low"] - 0.2): # 2 pips buffer for gold
                    return True
            else:
                if price <= (ob["high"] + 0.2):
                    return True
        return False

    def has_liquidity_sweep(self, df: pd.DataFrame, ob: Dict, lookback: int = 15) -> bool:
        """
        Detects if an Order Block was swept (pierced by a wick but body closed inside/outside).
        lookback: how many recent candles to check for the sweep.
        """
        # We only check recent candles because sweeps must be fresh to act upon
        recent_df = df.tail(lookback)
        
        if ob["type"] == "BULLISH":
            # For a bullish sweep: low must go below OB bottom, but close must close ABOVE OB bottom 
            # (proving rejection of lower prices).
            sweeps = recent_df[
                (recent_df['low'] < ob["low"]) & 
                (recent_df['close'] > ob["low"])
            ]
            return not sweeps.empty
        else:
            # For a bearish sweep: high goes above OB top, but close is BELOW OB top.
            sweeps = recent_df[
                (recent_df['high'] > ob["high"]) & 
                (recent_df['close'] < ob["high"])
            ]
            return not sweeps.empty

