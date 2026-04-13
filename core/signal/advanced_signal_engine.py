"""
Advanced Signal Engine.

Extends the base SignalEngine with:
- Fair Value Gap (FVG) detection
- Order Block identification
- Premium / Discount zone filtering
- Multi-LTF confluence (M15 + M5)
"""
from typing import Optional
import pandas as pd
import time
from core.signal.signal_engine import SignalEngine, TradeSignal
from core.structure.smc_logic import SMCLogic
from core.structure.trend_logic import TrendLogic
from core.structure.multi_tf_analyzer import MultiTFAnalyzer
from core.structure.liquidity import LiquiditySweep
from core.structure.scalp_logic import ScalpLogic
from core.structure.displacement import detect_displacement
from ai.learning_engine import LearningEngine, LEARNINGS_FILE
from database.repository import TradeRepository
from config.settings import SYMBOL
from config.trading_config import TRADING_CONFIG, TradeMode
from utils.logger import get_logger
from utils.time_utils import utc_now, get_session_name

logger = get_logger(__name__)


# All structural detection (FVG, OB, Structure) has been moved to smc_logic.py, 
# trend_logic.py, and scalp_logic.py for better modularity.


class AdvancedSignalEngine(SignalEngine):
    """
    Hybrid Signal Engine that orchestrates SMC, Trend, and Scalp logic.
    Supports 4 modes: Conservative, Moderate, Aggressive, Very Aggressive.
    """
    def __init__(self):
        self.smc       = SMCLogic()
        self.learning_engine = LearningEngine(TradeRepository())
        self.learnings = self.learning_engine.get_learnings()
        self.trend     = TrendLogic()
        self.scalp     = ScalpLogic()
        self.multi_tf  = MultiTFAnalyzer()
        self._logged_bypasses = set()

    def generate(
        self, 
        df_h4: pd.DataFrame, 
        df_h1: pd.DataFrame, 
        df_m15: pd.DataFrame,
        df_m5: pd.DataFrame,
        df_m1: pd.DataFrame,
        df_m30: pd.DataFrame = None,
        symbol: str = "XAUUSD",
        **kwargs
    ) -> Optional[TradeSignal]:
        """Entry point for the unified scoring logic using full Multi-TF confluence."""
        mode = TRADING_CONFIG.current_mode
        settings = TRADING_CONFIG.mode_settings.get(mode, {})
        threshold = settings.get("min_score_threshold", 5)
        
        score = 0
        
        # ── 1. Direction & Confluence Analysis ───────────────────────────────
        if TRADING_CONFIG.enable_multi_tf:
            mtf = self.multi_tf.analyze(
                df_h4=df_h4, df_h1=df_h1, df_m30=df_m30,
                df_m15=df_m15, df_m5=df_m5, df_m1=df_m1
            )
            direction = mtf.direction
            
            # Confidence bonus from confluence score
            score += int(max(mtf.bull_score, mtf.bear_score) / 3)  # up to ~7 pts
            
            # --- AGGRESSIVE ZONE HIT BONUS ---
            # If we are hitting an HTF Support/Resistance, give a massive boost
            h4 = mtf.tfs.get("h4")
            h1 = mtf.tfs.get("h1")
            
            is_at_support = (h4 and h4.at_zone == "BULLISH_OB") or (h1 and h1.at_zone == "BULLISH_OB")
            is_at_resis   = (h4 and h4.at_zone == "BEARISH_OB") or (h1 and h1.at_zone == "BEARISH_OB")
            
            if (direction == "buy" and is_at_support) or (direction == "sell" and is_at_resis):
                score += 5
                logger.info(f"🛡️ STRONG ZONE HIT: Price is at HTF Support/Resistance zone. Approving Aggressive Entry.")

            if mtf.is_strong:
                score += 2
            if mtf.htf_aligned:
                score += 2
            
            # Rejection Wick Bonus (Informational Logging)
            for tf_name, trend_obj in mtf.tfs.items():
                if trend_obj.rejection != "NONE":
                    logger.info(f"🕯\ufe0f REJECTION WICK DETECTED: {tf_name.upper()} shows strong {trend_obj.rejection} rejection.")
            
            # Reversal detection bonus
            if mtf.reversal_detected and direction:
                logger.info(f"🔄 REVERSAL DETECTED: LTF opposing HTF — potential apex flip ({direction.upper()})")
                score += 3
            
            best_tf_adx = (mtf.tfs.get("h4") or mtf.tfs.get("h1")).adx if (mtf.tfs.get("h4") or mtf.tfs.get("h1")) else 0
            master_bias = mtf.master_bias
        else:
            # Fallback for Disabled Multi-TF: Use simple H1/H4 primary trend
            trend_h1 = self.trend.analyze_trend(df_h1)
            trend_h4 = self.trend.analyze_trend(df_h4)
            
            # Simple bias: H1 is king
            if trend_h1["bias"] != "NEUTRAL":
                direction = "buy" if trend_h1["bias"] == "BULLISH" else "sell"
                score = 5 # Base score for being aligned with H1
                if trend_h1["bias"] == trend_h4["bias"]:
                    score += 3 # Convergence bonus
            else:
                direction = None
                score = 0
                
            best_tf_adx = trend_h1["adx"]
            master_bias = trend_h1["bias"]

        if not direction:
            logger.info(f"Signal Multi-Logic: No bias detected (Multi-TF: {TRADING_CONFIG.enable_multi_tf})")
            return None

        # Create a stable key for this session's setup to prevent log spamming
        signal_key = f"{symbol}_{direction}"
        
        # ADX confirmation bonus
        if best_tf_adx > 25:
            score += 1

        # ── 2. SMC Structure (M15) ────────────────────────────────────────────
        # Master bias is our "patokan besar"
        # master_bias is already set in the block above
        
        structures = self.smc.detect_structure(df_m15)
        obs = self.smc.get_order_blocks(df_m15)
        fvgs_m15 = self.smc.get_fvgs(df_m15)
        fvgs_m5  = self.smc.get_fvgs(df_m5)

        if structures:
            last_structure = structures[-1]
            struct_bias = last_structure.direction  # "BULLISH" or "BEARISH"

            if struct_bias == master_bias:
                # Structure confirms HTF trend (master)
                score += 3 if last_structure.type == "BOS" else 2
            elif last_structure.type == "CHOCH" and struct_bias == ( "BULLISH" if direction == "buy" else "BEARISH" ):
                # CHoCH confirms our intended direction (even if against master trend)
                score += 3
                logger.info(f"🔄 CHoCH CONFIRMED for {direction.upper()} reversal direction.")

        # Order Block alignment
        price_now = df_m1["close"].iloc[-1]
        if obs:
            last_ob = obs[-1]
            in_bullish_ob = last_ob["type"] == "BULLISH" and last_ob["low"] <= price_now <= last_ob["high"]
            in_bearish_ob = last_ob["type"] == "BEARISH" and last_ob["low"] <= price_now <= last_ob["high"]

            if in_bullish_ob and direction == "buy":
                score += 3
            elif in_bearish_ob and direction == "sell":
                score += 3

        # FVG alignment bonus
        aligned_fvgs = [f for f in (fvgs_m15 + fvgs_m5)[-10:]
                        if (f["type"] == "BULLISH" and direction == "buy") or
                           (f["type"] == "BEARISH" and direction == "sell")]
        if aligned_fvgs:
            score += 2

        # ── 3. Scalp Indicators (Confluence confirmation) ─────────────────────
        scalp_m15 = self.scalp.get_scalp_signals(df_m15)
        scalp_m5  = self.scalp.get_scalp_signals(df_m5)
        scalp_m1  = self.scalp.get_scalp_signals(df_m1)

        # Aggregate Sniper Scalp Signals (incl. RSI Divergence)
        for scalp in [scalp_m1, scalp_m5, scalp_m15]:
            s_dir = scalp.get("signal", "NEUTRAL").upper()
            div   = scalp.get("divergence", "NONE")
            
            if s_dir != "NEUTRAL":
                s_mapped = "buy" if s_dir == "BUY" else "sell"
                if s_mapped == direction:
                    # Give baseline confirmation point
                    score += 1
                    
            if (direction == "buy" and div == "BULLISH") or (direction == "sell" and div == "BEARISH"):
                logger.info(f"🎯 INSTITUTIONAL MOMENTUM DIVERGENCE DETECTED on {direction.upper()}!")
                score += 5  # Massive bonus for Sniper entry

        logger.info(f"Signal Multi-Logic Score: {score}/{threshold} (Mode: {mode.value}, Bias: {master_bias}, Dir: {direction})")

        # ── 4. Hidden RSI Divergence Bonus (No.24) ────────────────────────
        # Hidden Divergence = tren lanjut: price makes higher low (bull) but RSI lower low
        # Atau: price makes lower high (bear) but RSI higher high
        try:
            rsi_m15 = scalp_m15.get("rsi", 50)
            rsi_m5  = scalp_m5.get("rsi", 50)
            if len(df_m15) >= 10 and len(df_m5) >= 10:
                # Hidden Bull: price higher low (more recent low > prev low) + RSI lower low
                price_low_prev = df_m15["low"].iloc[-5:-2].min()
                price_low_curr = df_m15["low"].iloc[-2:].min()
                rsi_m15_series = self.scalp._calculate_rsi(df_m15["close"]) if hasattr(self.scalp, '_calculate_rsi') else None

                if rsi_m15_series is not None and len(rsi_m15_series) >= 6:
                    rsi_prev = rsi_m15_series.iloc[-5]
                    rsi_curr = rsi_m15_series.iloc[-1]

                    hidden_bull = (price_low_curr > price_low_prev) and (rsi_curr < rsi_prev) and direction == "buy"
                    hidden_bear = (price_low_curr < price_low_prev) and (rsi_curr > rsi_prev) and direction == "sell"

                    if hidden_bull:
                        logger.info(f"📊 HIDDEN DIVERGENCE (No.24): Bullish continuation confirmed (Higher Low + Lower RSI)")
                        score += 2
                    elif hidden_bear:
                        logger.info(f"📊 HIDDEN DIVERGENCE (No.24): Bearish continuation confirmed (Lower High + Higher RSI)")
                        score += 2
        except Exception:
            pass

        # ── 5. Heikin-Ashi Momentum Filter (No.12) ───────────────────────
        # M1 HA candle direction must agree with trade direction
        try:
            if len(df_m1) >= 5:
                ha_close = (df_m1["open"] + df_m1["high"] + df_m1["low"] + df_m1["close"]) / 4
                ha_open  = (df_m1["open"].shift(1) + df_m1["close"].shift(1)) / 2
                ha_dir   = "buy" if ha_close.iloc[-1] > ha_open.iloc[-1] else "sell"

                if ha_dir == direction:
                    score += 1
                    logger.debug(f"HA Momentum (No.12): M1 Heikin-Ashi agrees with {direction.upper()}")
                else:
                    # Penalize slightly if HA disagrees (opposite momentum)
                    score -= 1
                    logger.debug(f"HA Momentum (No.12): M1 HA DISAGREES with {direction.upper()} (HA={ha_dir.upper()}) -1 score")
        except Exception:
            pass

        # ── 6. Tick Volume Burst Bonus (No.5) ─────────────────────────────
        # If tick volume on the last M1 candle is >300% of the avg, bonus +1
        try:
            if "tick_volume" in df_m1.columns and len(df_m1) >= 10:
                vol_avg = df_m1["tick_volume"].tail(10).mean()
                vol_cur = df_m1["tick_volume"].iloc[-1]
                if vol_avg > 0 and vol_cur > vol_avg * 3.0:
                    logger.info(f"📈 VOLUME BURST (No.5): Tick vol {vol_cur:.0f} = {vol_cur/vol_avg:.1f}x avg → +1 score")
                    score += 1
        except Exception:
            pass

        logger.info(f"Signal Multi-Logic FINAL Score: {score}/{threshold} (after all bonuses)")

        if score >= threshold and direction:
            current_price = df_m1["close"].iloc[-1]
            atr_val = (df_m15["high"] - df_m15["low"]).tail(20).mean()
            atr_m1  = (df_m1["high"] - df_m1["low"]).tail(20).mean()

            # ── "Absolute Extreme" Entry & Tight SL Manager ──────────────────────
            # We scan M1 and M5 specifically for the absolute extreme to hide our SL.
            obs_m1  = self.smc.get_order_blocks(df_m1)
            obs_m5  = self.smc.get_order_blocks(df_m5)
            fvgs_m1 = self.smc.get_fvgs(df_m1)
            
            # --- Detect Deep Extreme for Reentry overrides ---
            m15_rsi = scalp_m15.get("rsi", 50)
            m5_rsi  = scalp_m5.get("rsi", 50)
            is_extreme = False
            if direction == "buy" and (m15_rsi < 35 or m5_rsi < 30):
                is_extreme = True
            elif direction == "sell" and (m15_rsi > 65 or m5_rsi > 70):
                is_extreme = True
            
            # --- SESSION ADAPTIVE PARAMETERS ---
            session_name = get_session_name(utc_now())
            
            # buffer_pips: extra safety distance for the SL
            # Asian = super tight, London = cautious, NY = protective
            if session_name == "ASIA":
                session_buffer = 0.5
                session_rr_mult = 10.0
            elif session_name == "LONDON":
                session_buffer = 1.5
                session_rr_mult = 4.0
            elif session_name == "NY":
                session_buffer = 3.0
                session_rr_mult = 2.5
            else: # Transition
                session_buffer = 2.0
                session_rr_mult = 3.0

            extreme_sl = None
            target_zone_low = None
            target_zone_high = None
            
            # If extreme, use razor-thin SL. Otherwise pad it using session buffer and M1 ATR
            buffer_pips = 0.3 if is_extreme else max(session_buffer, atr_m1 * 0.5) 
            
            # 1. Prioritize M1 Order Block for tightest possible SL
            if obs_m1:
                last_ob = obs_m1[-1]
                if not self.smc.has_liquidity_sweep(df_m1, last_ob):
                    logger.info("[M1] OB reached (Informational Sweep Check — Entry active for all modes)")
                if direction == "buy" and last_ob["type"] == "BULLISH" and current_price >= last_ob["low"]:
                    extreme_sl = last_ob["low"] - buffer_pips
                    target_zone_low, target_zone_high = last_ob["low"], last_ob["high"]
                elif direction == "sell" and last_ob["type"] == "BEARISH" and current_price <= last_ob["high"]:
                    extreme_sl = last_ob["high"] + buffer_pips
                    target_zone_low, target_zone_high = last_ob["low"], last_ob["high"]
                    
            # 2. If no M1 OB, try M5 Order Block
            if not extreme_sl and obs_m5:
                last_ob = obs_m5[-1]
                if not self.smc.has_liquidity_sweep(df_m5, last_ob):
                    logger.info("[M5] OB reached (Informational Sweep Check — Entry active for all modes)")
                if direction == "buy" and last_ob["type"] == "BULLISH" and current_price >= last_ob["low"]:
                    extreme_sl = last_ob["low"] - buffer_pips
                    target_zone_low, target_zone_high = last_ob["low"], last_ob["high"]
                elif direction == "sell" and last_ob["type"] == "BEARISH" and current_price <= last_ob["high"]:
                    extreme_sl = last_ob["high"] + buffer_pips
                    target_zone_low, target_zone_high = last_ob["low"], last_ob["high"]

            # 3. If no M5 OB, try M15 Order Block
            if not extreme_sl and obs:
                last_ob = obs[-1]
                if not self.smc.has_liquidity_sweep(df_m15, last_ob, lookback=10):
                    logger.info("[M15] OB reached (Informational Sweep Check — Entry active for all modes)")
                if direction == "buy" and last_ob["type"] == "BULLISH" and current_price >= last_ob["low"]:
                    extreme_sl = last_ob["low"] - buffer_pips
                    target_zone_low, target_zone_high = last_ob["low"], last_ob["high"]
                elif direction == "sell" and last_ob["type"] == "BEARISH" and current_price <= last_ob["high"]:
                    extreme_sl = last_ob["high"] + buffer_pips
                    target_zone_low, target_zone_high = last_ob["low"], last_ob["high"]

            # 4. If M1/M5/M15 has no OB at all, fallback to M1 FVG
            if not extreme_sl and fvgs_m1:
                last_fvg = fvgs_m1[-1]
                if direction == "buy" and last_fvg["type"] == "BULLISH" and current_price >= last_fvg["bottom"]:
                    extreme_sl = last_fvg["bottom"] - buffer_pips
                    target_zone_low, target_zone_high = last_fvg["bottom"], last_fvg["top"]
                elif direction == "sell" and last_fvg["type"] == "BEARISH" and current_price <= last_fvg["top"]:
                    extreme_sl = last_fvg["top"] + buffer_pips
                    target_zone_low, target_zone_high = last_fvg["bottom"], last_fvg["top"]

            # ── Entry Execution Filter: "Extreme" targeting ────────────────────
            # To ensure minimal floating, we strictly require the price to be 
            # within or extremely close to the M1/M5 structural zone (OB or FVG).
            in_zone = False
            
            # Check interaction with the targeted structure (the one that gave us extreme_sl)
            if target_zone_low is not None and target_zone_high is not None:
                # If direction is buy, price should be near the SL (the bottom).
                # To minimize floating, we only enter if price is between the structure's high and low.
                if target_zone_low - buffer_pips <= current_price <= target_zone_high + buffer_pips:
                    in_zone = True

            # --- MOMENTUM BREAKOUT BYPASS RL ---
            # If the momentum is exceptionally strong (score >= 12 AND ADX > 30), 
            # waiting for a pullback might cause us to miss the move entirely.
            momentum_bypass = False
            if not in_zone and score >= 12 and best_tf_adx > 30 and mode in (TradeMode.ULTRA_SCALPER, TradeMode.VERY_AGGRESSIVE, TradeMode.AGGRESSIVE):
                if signal_key not in self._logged_bypasses:
                    logger.info(f"🚀 MOMENTUM BYPASS: Score is exceptionally high ({score}/15) and ADX is strong ({best_tf_adx:.1f}). Bypassing Extreme zone requirement.")
                    if signal_key:
                        self._logged_bypasses.add(signal_key)
                
                in_zone = True
                momentum_bypass = True
            
            # Clean up old bypass logs to prevent memory creep
            if len(self._logged_bypasses) > 100:
                self._logged_bypasses.clear()
            
            is_stalking = False
            stalking_reason = ""
            
            # --- Pantau (Stalking) Logic for Moderate & Conservative ---
            if not in_zone and mode in (TradeMode.MODERATE, TradeMode.CONSERVATIVE):
                if score >= threshold - 1: # High maturity, just waiting for price
                    is_stalking = True
                    stalking_reason = "Waiting for price to enter 'Extreme' zone (M1/M5 OB/FVG)"
            
            if not in_zone and not is_stalking:
                logger.info(f"Signal Multi-Logic Score {score} OK, but price is not in M1 'Extreme' zone yet. Waiting for retracement.")
                return None
            
            # If we are stalking, we still calculate hypothetical SL/TP for the radar report
            if is_stalking:
                logger.info(f"📡 [PANTAU] Setup matang (Score {score}). {stalking_reason}")

            # 4. Calculate safe distance
            min_risk = (df_m1["high"] - df_m1["low"]).tail(20).mean() * 1.5
            
            if extreme_sl and not momentum_bypass:
                proposed_sl = extreme_sl
                # Ensure it's not so tight it gets wiped out instantly by spread
                if not is_extreme:
                    if direction == "buy":
                        proposed_sl = min(proposed_sl, current_price - min_risk)
                    else:
                        proposed_sl = max(proposed_sl, current_price + min_risk)
                logger.info(f"🎯 1M HIGH PRECISION ENTRY: SL set to {proposed_sl:.2f} (Extreme: {is_extreme})")
            else:
                # Fallback to ATR or Recent Swing if no structure found, or if we used Momentum Bypass (to avoid massive SL)
                swing_risk = min_risk * 1.2
                if direction == "buy":
                    proposed_sl = current_price - max(atr_val * 1.5, swing_risk)
                else:
                    proposed_sl = current_price + max(atr_val * 1.5, swing_risk)
                    
                if momentum_bypass:
                    logger.info(f"🚀 MOMENTUM SL SET: Using ATR/Local Swing fallback SL {proposed_sl:.2f} to prevent huge risk on aggressive breakout.")
                else:
                    logger.info(f"🔌 FALLBACK SL SET: No structure found, using ATR/Swing fallback SL {proposed_sl:.2f}")

            # 5. Take Profit Calculation (Dynamic based on Session & Extreme)
            risk_dist = abs(current_price - proposed_sl)
            # Use session-aware multiplier
            risk_reward_multiplier = 10.0 if is_extreme else session_rr_mult
            
            # --- BASE TARGET CALCULATION ---
            if direction == "buy":
                raw_target = current_price + (risk_dist * risk_reward_multiplier)
            else:
                raw_target = current_price - (risk_dist * risk_reward_multiplier)
            
            # TP Synchronization: Align with existing trades if available
            anchor_tp = kwargs.get("anchor_tp")
            if is_extreme and anchor_tp:
                # If anchor_tp is further than our raw_target, use it
                is_further = (direction == "buy" and anchor_tp > raw_target) or (direction == "sell" and anchor_tp < raw_target)
                if is_further:
                    logger.info(f"🔄 TP SYNC: Aligning extreme signal target with anchor trade @ {anchor_tp:.2f}")
                    raw_target = anchor_tp

            # OPTIMALKAN HARGA PASTI (Inward Buffer)
            # Tarik mundur TP 1.5 - 2.0 pips ke arah dalam untuk memastikan harga kesentuh 
            # dan tidak meleset/front-run oleh spread atau ekor lilin ekstrem.
            tp_buffer_pips = max(1.5, atr_m1 * 0.4)
            if direction == "buy":
                take_profit = raw_target - tp_buffer_pips
            else:
                take_profit = raw_target + tp_buffer_pips
                
            # If after buffer the RR becomes too small, ensure at least RR 1.5
            final_rr = abs(current_price - take_profit) / risk_dist if risk_dist > 0 else 2.0
            if final_rr < 1.5:
                take_profit = current_price + (risk_dist * 1.5) if direction == "buy" else current_price - (risk_dist * 1.5)
                final_rr = 1.5

            return TradeSignal(
                signal_id      = f"SMC_{int(time.time())}",
                symbol         = kwargs.get("symbol", SYMBOL), # Use global SYMBOL if not provided
                timestamp      = utc_now(),
                direction      = direction,
                entry_price    = current_price,
                stop_loss      = proposed_sl,
                take_profit    = take_profit,
                rr_ratio       = risk_reward_multiplier,
                score          = score,
                max_score      = 15,
                atr_pips       = atr_val,
                session        = session_name,
                is_extreme     = is_extreme,
                is_stalking    = is_stalking,
                stalking_reason = stalking_reason,
            )
            
        return None

    def refine_entry(
        self,
        signal: TradeSignal,
        df_m5: pd.DataFrame,
    ) -> TradeSignal:
        """
        Attempt to refine entry using M5 FVG or Order Block as a tighter entry.
        Improves RR while keeping the structural stop-loss.
        """
        fvgs = self.smc.get_fvgs(df_m5)
        obs  = self.smc.get_order_blocks(df_m5)

        # Try to find aligned FVG for tighter entry
        aligned_fvgs = [
            f for f in fvgs[-5:]
            if (f["type"] == "BULLISH" and signal.direction == "buy") or
               (f["type"] == "BEARISH" and signal.direction == "sell")
        ]
        if aligned_fvgs:
            fvg = aligned_fvgs[-1]
            if signal.direction == "buy":
                # Enter at FVG bottom (better price)
                refined_entry = fvg["bottom"]
            else:
                refined_entry = fvg["top"]

            risk = abs(signal.stop_loss - refined_entry)
            if risk > 0:
                new_rr = abs(signal.take_profit - refined_entry) / risk
                if new_rr > signal.rr_ratio:
                    signal.entry_price = refined_entry
                    signal.rr_ratio    = new_rr
                    logger.info(f"Entry refined via FVG: new RR={new_rr:.2f}")

        return signal
