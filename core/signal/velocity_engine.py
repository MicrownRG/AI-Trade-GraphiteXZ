"""
Velocity Engine - Opening Range Momentum Burst (VELO_)

Designed to snipe extreme volatility injections specifically during the opening
phase of London and New York sessions. Very tight SL and fast TP.
"""
import uuid
import pandas as pd
from typing import Optional

from core.signal.signal_engine import TradeSignal
from utils.logger import get_logger
from utils.time_utils import utc_now

logger = get_logger(__name__)

class VelocityEngine:
    def __init__(self):
        # Default UTC opening hours
        self.london_open = 7
        self.ny_open_1 = 12  # US Pre-market / overlap
        self.ny_open_2 = 13  # US Core open

    def _is_opening_session(self, utc_hour: int) -> bool:
        """Returns True if we are inside the magic window (first 60 mins of open)."""
        if utc_hour == self.london_open:
            return True
        if utc_hour in (self.ny_open_1, self.ny_open_2):
            return True
        return False

    def _calc_adx(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        hi = df['high']
        lo = df['low']
        cl = df['close']
        
        tr1 = hi - lo
        tr2 = (hi - cl.shift(1)).abs()
        tr3 = (lo - cl.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        up = hi.diff()
        down = -lo.diff()
        
        pdm = pd.Series(0.0, index=df.index)
        ndm = pd.Series(0.0, index=df.index)
        
        pdm[(up > down) & (up > 0)] = up[(up > down) & (up > 0)]
        ndm[(down > up) & (down > 0)] = down[(down > up) & (down > 0)]
        
        atr = tr.rolling(period).mean()
        pdi = 100 * (pdm.rolling(period).mean() / atr)
        ndi = 100 * (ndm.rolling(period).mean() / atr)
        
        dx = 100 * (pdi - ndi).abs() / (pdi + ndi)
        adx = dx.rolling(period).mean()
        return adx.fillna(0)

    def generate(
        self,
        df_m1: pd.DataFrame,
        current_spread_pips: float,
        balance: float,
        **kwargs
    ) -> Optional[TradeSignal]:
        # 1. basic gates
        if balance < 50:
            return None
            
        if current_spread_pips > 3.0:
            return None

        if len(df_m1) < 150:
            return None

        now = utc_now()
        utc_hour = now.hour
        
        # 2. Strict Session Gate
        if not self._is_opening_session(utc_hour):
            return None

        # 3. Determine pre-session window dynamically
        try:
            if utc_hour == self.london_open:
                # e.g., if London open is 07:00, pre-session is 05:00-06:59
                pre_session_df = df_m1[(df_m1.index.hour == 5) | (df_m1.index.hour == 6)]
            elif utc_hour == self.ny_open_1:
                # Pre NY (12:00 UTC), calc from 10:00 to 11:59
                pre_session_df = df_m1[(df_m1.index.hour == 10) | (df_m1.index.hour == 11)]
            elif utc_hour == self.ny_open_2:
                # NY Core (13:00 UTC), calc from 11:00 to 12:59
                pre_session_df = df_m1[(df_m1.index.hour == 11) | (df_m1.index.hour == 12)]
            else:
                return None
        except Exception as e:
            logger.error(f"VELOCITY ENGINE index parsing err: {e}")
            return None
            
        if pre_session_df.empty or len(pre_session_df) < 30:
            # Fallback if no exact timezone timestamps exists (MT5 localized vs UTC offset issues)
            pre_session_df = df_m1.iloc[-150:-30]

        box_high = pre_session_df['high'].max()
        box_low  = pre_session_df['low'].min()
        box_size_pips = (box_high - box_low) * 10
        
        # 4. Box Size Filter: ORB requires tight pre-session compression
        if box_size_pips > 120:
            # If the past 2 hours already moved more than 120 pips, momentum has leaked. Extinguished.
            return None

        curr_close = df_m1['close'].iloc[-1]
        curr_candle = df_m1.iloc[-1]
        prev_candle = df_m1.iloc[-2]
        
        # 5. Momentum Burst Detection
        adx_series = self._calc_adx(df_m1, 14)
        curr_adx = adx_series.iloc[-1]
        
        if 'tick_volume' in df_m1.columns:
            vol_ma = df_m1['tick_volume'].rolling(20).mean()
            curr_vol = df_m1['tick_volume'].iloc[-1]
            is_volume_burst = curr_vol > (vol_ma.iloc[-2] * 1.5)
        else:
            is_volume_burst = True # Fallback if provider has no volume API

        has_momentum = curr_adx >= 25 and is_volume_burst
        
        if not has_momentum:
            return None

        # 6. Breakout Detection
        signal_dir = None
        sl = None
        
        # Breakout UP (Clean pierce of the Box High)
        if prev_candle['close'] <= box_high and curr_close > box_high:
            signal_dir = "buy"
            # SL just below the breakout engulfing candle low, VERY TIGHT
            sl = curr_candle['low'] - 0.5 
            
        # Breakout DOWN
        elif prev_candle['close'] >= box_low and curr_close < box_low:
            signal_dir = "sell"
            # SL just above the breakout engulfing candle high
            sl = curr_candle['high'] + 0.5
            
        if not signal_dir:
            return None
            
        # 7. Risk Validation (Tight Control)
        risk_dist = abs(curr_close - sl)
        if risk_dist < 0.8: # Cap minimum risk at 8 pips to survive spread shock
            if signal_dir == "buy": sl = curr_close - 0.8
            else: sl = curr_close + 0.8
            risk_dist = abs(curr_close - sl)
            
        if risk_dist > 4.5: # Cap max risk at 45 pips. We don't want to buy the top of a 50 pip massive nuke
            logger.debug(f"Velocity block: Breakout too massive, SL too wide ({risk_dist*10:.0f} pips).")
            return None

        # 8. Set Target (Fixed RR 1:2) 
        if signal_dir == "buy":
            tp = curr_close + (risk_dist * 2.0)
        else:
            tp = curr_close - (risk_dist * 2.0)

        sig_id = f"VELO_{str(uuid.uuid4())[:6]}"
        
        signal = TradeSignal(
            signal_id=sig_id,
            symbol="XAUUSD",
            timestamp=utc_now(),
            direction=signal_dir,
            entry_price=curr_close,
            stop_loss=sl,
            take_profit=tp,
            rr_ratio=2.0,
            score=15, # Maximum priority
            max_score=15,
            session="VELOCITY",
            ai_decision="TAKE",
            ai_reason=f"ORB Momentum Burst | ADX: {curr_adx:.1f} | Box: {box_size_pips:.1f} pips",
            ai_confidence=0.95,
            is_extreme=True  # Enables trailing features
        )
        
        logger.info(
            f"💥 VELOCITY BURST DETECTED! {signal_dir.upper()} @ {curr_close:.2f} | "
            f"SL={sl:.2f} (dist={risk_dist:.2f}pt) | TP={tp:.2f} | ADX={curr_adx:.1f}"
        )
        return signal
