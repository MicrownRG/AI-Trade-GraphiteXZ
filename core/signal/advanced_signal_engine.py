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
        self._logged_thin_retrace = set()

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
        mtf = None
        reversal_detected = False
        master_dir = None
        
        # ── 1. Direction & Confluence Analysis ───────────────────────────────
        if TRADING_CONFIG.enable_multi_tf:
            mtf = self.multi_tf.analyze(
                df_h4=df_h4, df_h1=df_h1, df_m30=df_m30,
                df_m15=df_m15, df_m5=df_m5, df_m1=df_m1
            )
            direction = mtf.direction
            
            # Fibo Multi-TF Analysis bonus
            if TRADING_CONFIG.current_mode != TradeMode.CONSERVATIVE:
                try:
                    from core.structure.swing import detect_swings
                    h1_swings = detect_swings(df_h1)
                    if len(h1_swings) >= 2:
                        last_h = [s.price for s in h1_swings if s.kind == 'high']
                        last_l = [s.price for s in h1_swings if s.kind == 'low']
                        if last_h and last_l:
                            rng = last_h[-1] - last_l[-1]
                            price_now = df_h1['close'].iloc[-1]
                            if direction == "buy" and rng > 0:
                                pullback = (last_h[-1] - price_now) / rng
                                if 0.5 <= pullback <= 0.886:
                                    score += 2
                                    logger.info(f"📐 FIBO MULTI-TF: H1 Golden Pocket / Deep Retracement Hit ({pullback:.1%} pullback)")
                            elif direction == "sell" and rng > 0:
                                pullback = (price_now - last_l[-1]) / rng
                                if 0.5 <= pullback <= 0.886:
                                    score += 2
                                    logger.info(f"📐 FIBO MULTI-TF: H1 Premium Golden Pocket Hit ({pullback:.1%} pullback)")
                except Exception as e:
                    pass
                    
            # Confidence bonus from confluence score (cap at 3 pts maximum to prevent score inflation)
            confluence_pts = int(max(mtf.bull_score, mtf.bear_score) / 10)
            score += min(confluence_pts, 3)
            
            # --- AGGRESSIVE ZONE HIT BONUS ---
            # If we are hitting an HTF Support/Resistance, give a massive boost
            h4 = mtf.tfs.get("h4")
            h1 = mtf.tfs.get("h1")
            
            is_at_support = (h4 and h4.at_zone == "BULLISH_OB") or (h1 and h1.at_zone == "BULLISH_OB")
            is_at_resis   = (h4 and h4.at_zone == "BEARISH_OB") or (h1 and h1.at_zone == "BEARISH_OB")
            
            if (direction == "buy" and is_at_support) or (direction == "sell" and is_at_resis):
                score += 3
                logger.info(f"🛡️ STRONG ZONE HIT: Price is at HTF Support/Resistance zone. Approving Aggressive Entry.")

            if mtf.is_strong:
                score += 2
            if mtf.htf_aligned:
                score += 2
            
            # Rejection Wick Bonus (Informational Logging)
            for tf_name, trend_obj in mtf.tfs.items():
                if trend_obj.rejection != "NONE":
                    logger.info(f"🕯\ufe0f REJECTION WICK DETECTED: {tf_name.upper()} shows strong {trend_obj.rejection} rejection.")
            
            # Reversal alignment handling:
            # when LTF momentum opposes HTF, do NOT blindly boost current direction.
            reversal_detected = mtf.reversal_detected
            if reversal_detected and direction:
                m1_bias = (mtf.tfs.get("m1").bias if mtf.tfs.get("m1") else "NEUTRAL")
                m5_bias = (mtf.tfs.get("m5").bias if mtf.tfs.get("m5") else "NEUTRAL")
                _master_bias_now = mtf.master_bias
                master_dir = "buy" if _master_bias_now == "BULLISH" else ("sell" if _master_bias_now == "BEARISH" else None)

                if master_dir and direction == master_dir:
                    # Existing direction is still following HTF while LTF is already opposing:
                    # treat as warning (likely pullback/flip), reduce confidence.
                    score -= 3
                    logger.info(
                        f"🔄 REVERSAL WARNING: LTF ({m5_bias}/{m1_bias}) opposes HTF {_master_bias_now}. "
                        f"Reducing score for {direction.upper()} setup."
                    )
                else:
                    # Direction already follows reversal context (or HTF is neutral).
                    logger.info(
                        f"🔄 REVERSAL ALIGNED: LTF ({m5_bias}/{m1_bias}) supports {direction.upper()} "
                        f"against/without HTF anchor."
                    )
                    score += 3
            
            best_tf_adx = (mtf.tfs.get("h4") or mtf.tfs.get("h1")).adx if (mtf.tfs.get("h4") or mtf.tfs.get("h1")) else 0
            master_bias = mtf.master_bias
            master_dir = "buy" if master_bias == "BULLISH" else ("sell" if master_bias == "BEARISH" else None)
        else:
            # Fallback for Disabled Multi-TF: Use simple H1/H4 primary trend
            trend_h1 = self.trend.analyze_trend(df_h1)
            trend_h4 = self.trend.analyze_trend(df_h4)
            
            # Simple bias: H1 is king
            if trend_h1["bias"] != "NEUTRAL":
                direction = "buy" if trend_h1["bias"] == "BULLISH" else "sell"
                score = 3 # Base score for being aligned with H1
                if trend_h1["bias"] == trend_h4["bias"]:
                    score += 2 # Convergence bonus
            else:
                direction = None
                score = 0
                
            best_tf_adx = trend_h1["adx"]
            master_bias = trend_h1["bias"]
            master_dir = "buy" if master_bias == "BULLISH" else ("sell" if master_bias == "BEARISH" else None)

        if not direction:
            logger.info(f"Signal: no bias detected (multi_tf={TRADING_CONFIG.enable_multi_tf})")
            return None

        # ── HTF TREND-LOCK GUARD ─────────────────────────────────────────────
        # Block counter-trend entries when H4 AND H1 are both strongly aligned
        # AND ADX shows a real trend (not chop). Prevents "selling into a bull
        # impulse" → cutloss. Counter-trend only allowed via reversal_engine.
        try:
            if TRADING_CONFIG.enable_multi_tf and mtf is not None:
                _h4 = mtf.tfs.get("h4")
                _h1 = mtf.tfs.get("h1")
                if _h4 and _h1 and _h4.bias == _h1.bias and _h4.bias != "NEUTRAL":
                    htf_dir = "buy" if _h4.bias == "BULLISH" else "sell"
                    htf_adx = max(getattr(_h4, "adx", 0) or 0, getattr(_h1, "adx", 0) or 0)
                    if direction != htf_dir and htf_adx >= 22:
                        logger.info(
                            f"🚧 HTF TREND-LOCK: blocked {direction.upper()} — "
                            f"H4+H1 both {_h4.bias} (ADX {htf_adx:.1f}). "
                            f"Counter-trend only via reversal_engine."
                        )
                        return None
        except Exception:
            pass

        # Stable key to deduplicate repeated log messages for the same setup
        signal_key = f"{symbol}_{direction}"
        
        # ADX confirmation bonus
        if best_tf_adx > 25:
            score += 1

        # ── 2. SMC Structure (M15) ────────────────────────────────────────────
        # master_bias is already set in the block above — used as the primary filter
        
        structures = self.smc.detect_structure(df_m15)
        obs = self.smc.get_order_blocks(df_m15)
        fvgs_m15 = self.smc.get_fvgs(df_m15)
        fvgs_m5  = self.smc.get_fvgs(df_m5)

        if structures:
            last_structure = structures[-1]
            struct_bias = last_structure.direction  # "BULLISH" or "BEARISH"

            if struct_bias == master_bias:
                # Structure confirms HTF trend (master)
                score += 2 if last_structure.type == "BOS" else 1
            elif last_structure.type == "CHOCH" and struct_bias == ( "BULLISH" if direction == "buy" else "BEARISH" ):
                # CHoCH confirms our intended direction (even if against master trend)
                score += 2
                logger.info(f"🔄 CHoCH CONFIRMED for {direction.upper()} reversal direction.")

        # Order Block alignment
        price_now = df_m1["close"].iloc[-1]
        if obs:
            last_ob = obs[-1]
            in_bullish_ob = last_ob["type"] == "BULLISH" and last_ob["low"] <= price_now <= last_ob["high"]
            in_bearish_ob = last_ob["type"] == "BEARISH" and last_ob["low"] <= price_now <= last_ob["high"]

            if in_bullish_ob and direction == "buy":
                score += 2
            elif in_bearish_ob and direction == "sell":
                score += 2

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
                score += 3  # Bonus for Sniper entry

        logger.info(f"Signal score: {score}/{threshold} (mode={mode.value} bias={master_bias} dir={direction})")

        # ── 4. Hidden RSI Divergence Bonus (No.24) ────────────────────────
        # Hidden divergence = trend continuation:
        #   Bull: price makes higher low + RSI lower low (momentum diverges, trend continues)
        #   Bear: price makes lower high + RSI higher high
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
                    price_high_prev = df_m15["high"].iloc[-5:-2].max()
                    price_high_curr = df_m15["high"].iloc[-2:].max()
                    hidden_bear = (price_high_curr < price_high_prev) and (rsi_curr > rsi_prev) and direction == "sell"

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

        # ── 6. Volume Confirmation ────────────────────────────────────────
        # M1 volume burst: strong momentum confirmation
        # M5 rising volume: institutional participation
        try:
            if "tick_volume" in df_m1.columns and len(df_m1) >= 20:
                vol_avg = df_m1["tick_volume"].tail(20).mean()
                vol_cur = df_m1["tick_volume"].iloc[-1]
                if vol_avg > 0:
                    vol_ratio = vol_cur / vol_avg
                    if vol_ratio >= 3.0:
                        score += 2
                        logger.info(f"📈 VOLUME BURST: M1 tick vol {vol_cur:.0f} = {vol_ratio:.1f}x avg → +2")
                    elif vol_ratio >= 1.5:
                        score += 1
                        logger.info(f"📈 VOLUME CONFIRM: M1 tick vol {vol_cur:.0f} = {vol_ratio:.1f}x avg → +1")
            if "tick_volume" in df_m5.columns and len(df_m5) >= 20:
                _v5 = df_m5["tick_volume"]
                _v5_avg = _v5.tail(20).mean()
                _v5_last3 = _v5.tail(3).mean()
                if _v5_avg > 0 and _v5_last3 >= _v5_avg * 1.3:
                    score += 1
                    logger.info(f"📈 M5 VOL RISING: last 3 bars avg {_v5_last3:.0f} vs 20-bar {_v5_avg:.0f} → +1")
        except Exception:
            pass

        # ── 7. Bollinger Band Confluence ─────────────────────────────────
        # Price near/outside BB band in trade direction = mean-reversion or
        # breakout confirmation. M5 BB(20,2) used for entry-level precision.
        try:
            if len(df_m5) >= 20:
                _bb_close = df_m5["close"]
                _bb_sma = _bb_close.rolling(20).mean()
                _bb_std = _bb_close.rolling(20).std()
                bb_upper = (_bb_sma + 2 * _bb_std).iloc[-1]
                bb_lower = (_bb_sma - 2 * _bb_std).iloc[-1]
                bb_mid   = _bb_sma.iloc[-1]
                _bb_price = _bb_close.iloc[-1]

                if direction == "buy" and _bb_price <= bb_lower:
                    score += 2
                    logger.info(f"📊 BB: price {_bb_price:.2f} at/below lower band {bb_lower:.2f} → +2")
                elif direction == "sell" and _bb_price >= bb_upper:
                    score += 2
                    logger.info(f"📊 BB: price {_bb_price:.2f} at/above upper band {bb_upper:.2f} → +2")
                elif direction == "buy" and _bb_price < bb_mid:
                    score += 1
                    logger.debug(f"BB: price below mid-band → +1")
                elif direction == "sell" and _bb_price > bb_mid:
                    score += 1
                    logger.debug(f"BB: price above mid-band → +1")
        except Exception:
            pass

        # ── 8. Multi-TF Candlestick Pattern Recognition ────────────────
        # Scan M1, M5, M15, H1 for reversal/continuation patterns.
        # Higher TF patterns carry more weight (H1 engulfing > M1 engulfing).
        try:
            from core.signal.candle_patterns import detect_patterns, get_pattern_score, get_pattern_names
            _cp_all = []
            _cp_bonus = 0
            _cp_labels = []
            for _tf_name, _tf_df, _tf_cap in [
                ("M1",  df_m1,  2),
                ("M5",  df_m5,  2),
                ("M15", df_m15, 3),
                ("H1",  df_h1,  3),
            ]:
                if _tf_df is None or len(_tf_df) < 5:
                    continue
                _pats = detect_patterns(_tf_df)
                _sc = get_pattern_score(_pats, direction)
                _capped = min(max(_sc, -1), _tf_cap)
                _cp_bonus += _capped
                if _sc > 0:
                    _names = get_pattern_names(_pats, direction)
                    _cp_labels.append(f"{_tf_name}:{_names}")
                _cp_all.extend(_pats)

            _cp_final = min(_cp_bonus, 5)
            if _cp_final > 0:
                score += _cp_final
                logger.info(f"🕯 Candle patterns: {'; '.join(_cp_labels)} → +{_cp_final} score")
            elif _cp_final <= -2:
                score -= 1
                logger.info(f"🕯 Opposing candle patterns across TFs → -1 score")
        except Exception:
            pass

        logger.info(f"Signal FINAL score: {score}/{threshold} (after all bonuses)")

        # ── 6b. Transitional guard: avoid premature trend-following entries ──
        # If HTF is not yet aligned and LTF is actively opposing HTF, require
        # extra conviction before allowing trend-following entries.
        if (
            TRADING_CONFIG.enable_multi_tf
            and mtf is not None
            and reversal_detected
            and not mtf.htf_aligned
            and master_dir is not None
            and direction == master_dir
            and score < (threshold + 2)
        ):
            logger.info(
                f"⏸️ TRANSITION GUARD: HTF not aligned + LTF opposing {master_bias}. "
                f"Score {score} below required {threshold + 2} for early {direction.upper()} entry."
            )
            return None

        if score >= threshold and direction:
            current_price = df_m1["close"].iloc[-1]
            atr_val = (df_m15["high"] - df_m15["low"]).tail(20).mean()
            atr_m1  = (df_m1["high"] - df_m1["low"]).tail(20).mean()

            # ── Precision M1 entry and tight SL using structural zones ──────────────
            # Scan M1 and M5 for Order Blocks and FVGs to place SL tightly.
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
            event_time = kwargs.get("current_time") or utc_now()
            session_name = get_session_name(event_time)
            
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
            
            # Fibonacci 12.7% tight SL optimisation:
            # Replace oversized ATR buffer with 12.7% of M5 swing range for tighter risk.
            try:
                from core.structure.swing import detect_swings
                swings_m5 = detect_swings(df_m5.tail(100))
                if len(swings_m5) >= 2:
                    sh = [s.price for s in swings_m5 if s.kind == 'high']
                    sl = [s.price for s in swings_m5 if s.kind == 'low']
                    if sh and sl:
                        fibo_range = sh[-1] - sl[-1]
                        if fibo_range > 0:
                            fibo_buffer = fibo_range * 0.127  # 12.7% extension limit
                            buffer_pips = min(buffer_pips, max(0.5, fibo_buffer))
                            logger.info(f"📐 Fibo SL Optimization: Buffer tightened to {buffer_pips:.1f} using 12.7% Extension")
            except Exception:
                pass
            
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

            # --- THIN RETRACE ENTRY (valid-but-small) ---
            # When price overshot the zone but is now retracing BACK toward it,
            # we accept a half-size entry instead of waiting for a full tag.
            # Validity requires:
            #   1. Score >= threshold (same bar as a normal entry)
            #   2. Price within ATR_M1 * 1.5 of the zone boundary (close, not miles away)
            #   3. Last 2 M1 candles moving TOWARD the zone (real retrace, not still running away)
            #   4. Structural SL still anchored to the OB/FVG boundary
            thin_retrace  = False
            thin_mult     = 0.5  # half-size — valid but tipis
            if not in_zone and score >= threshold and len(df_m1) >= 3:
                _atr_prox = max(atr_m1, 0.5)

                # Fallback zone: if no OB/FVG matched, synthesize one from the
                # nearest M1 swing extreme so thin-retrace still applies.
                z_low, z_high = target_zone_low, target_zone_high
                zone_source = "OB/FVG"
                if z_low is None or z_high is None:
                    try:
                        win = df_m1.tail(30)
                        if direction == "buy":
                            z_ref = win["low"].min()
                            z_low, z_high = z_ref - _atr_prox * 0.3, z_ref + _atr_prox * 0.3
                        else:
                            z_ref = win["high"].max()
                            z_low, z_high = z_ref - _atr_prox * 0.3, z_ref + _atr_prox * 0.3
                        zone_source = "M1_SWING"
                    except Exception:
                        z_low = z_high = None

                if z_low is None or z_high is None:
                    logger.info("📉 thin-retrace skip: no zone derivable")
                else:
                    if direction == "buy":
                        dist_to_zone = current_price - z_high
                    else:
                        dist_to_zone = z_low - current_price

                    # Retrace: last 1 bar closing back toward zone (relaxed from 2).
                    c1 = df_m1["close"].iloc[-1]
                    c2 = df_m1["close"].iloc[-2]
                    retracing = (c1 < c2) if direction == "buy" else (c1 > c2)

                    # Volume floor (relaxed 0.6× avg) — block only truly dead bars.
                    vol_ok = True
                    try:
                        if "tick_volume" in df_m1.columns and len(df_m1) >= 20:
                            _vavg = df_m1["tick_volume"].tail(20).mean()
                            _vcur = df_m1["tick_volume"].iloc[-1]
                            if _vavg > 0 and _vcur < _vavg * 0.6:
                                vol_ok = False
                    except Exception:
                        pass

                    # Widen proximity to ATR × 2.0 (was 1.5).
                    in_range = 0 < dist_to_zone <= _atr_prox * 2.0
                    if in_range and retracing and vol_ok:
                        thin_retrace = True
                        in_zone = True
                        # Anchor SL to synthesized zone if we had none before
                        if extreme_sl is None:
                            extreme_sl = (z_low - buffer_pips) if direction == "buy" else (z_high + buffer_pips)
                            target_zone_low, target_zone_high = z_low, z_high
                        if signal_key not in self._logged_thin_retrace:
                            logger.info(
                                f"📉 THIN RETRACE ENTRY [{zone_source}]: dist {dist_to_zone:.2f} "
                                f"(max {_atr_prox*2.0:.2f}), retrace={retracing}, vol_ok={vol_ok} "
                                f"— half-size (x{thin_mult}). score={score}/{threshold}"
                            )
                            self._logged_thin_retrace.add(signal_key)
                    else:
                        # One-shot diagnostic per setup — explain why skipped
                        if signal_key not in self._logged_thin_retrace:
                            logger.info(
                                f"📉 thin-retrace skip [{zone_source}]: dist={dist_to_zone:.2f} "
                                f"(max {_atr_prox*2.0:.2f}) in_range={in_range} "
                                f"retrace={retracing} vol_ok={vol_ok}"
                            )
                            self._logged_thin_retrace.add(signal_key)

            if len(self._logged_thin_retrace) > 100:
                self._logged_thin_retrace.clear()

            # --- MOMENTUM BREAKOUT BYPASS ---
            # When fast momentum carries price beyond the structural zone before
            # we can enter, waiting for a retracement that never comes means a
            # missed trade.  Fire immediately if score and ADX are both strong.
            #
            # Threshold: score >= mode_threshold + 2 (at least 7), ADX > 25.
            # Applies to MODERATE and above so all non-conservative modes benefit.
            momentum_bypass = False
            _bypass_score_min = max(threshold + 2, 7)
            if not in_zone and score >= _bypass_score_min and best_tf_adx > 25 and mode in (
                TradeMode.ULTRA_SCALPER, TradeMode.VERY_AGGRESSIVE,
                TradeMode.AGGRESSIVE, TradeMode.MODERATE,
            ):
                if signal_key not in self._logged_bypasses:
                    logger.info(
                        f"🚀 MOMENTUM BYPASS: score={score} (min={_bypass_score_min}) "
                        f"ADX={best_tf_adx:.1f} — entering without zone retracement."
                    )
                    self._logged_bypasses.add(signal_key)

                in_zone = True
                momentum_bypass = True
            
            # Clean up old bypass logs to prevent memory creep
            if len(self._logged_bypasses) > 100:
                self._logged_bypasses.clear()
            
            is_stalking = False
            stalking_reason = ""
            
            # Stalking mode for Conservative & Moderate:
            # Setup is mature but price has not yet entered the structural zone.
            # Notify via Telegram and wait — do NOT enter early.
            if not in_zone and mode in (TradeMode.MODERATE, TradeMode.CONSERVATIVE):
                if score >= threshold - 1:
                    is_stalking     = True
                    stalking_reason = "Waiting for price to enter the M1/M5 OB/FVG zone"
            
            if not in_zone and not is_stalking:
                logger.info(f"Signal Multi-Logic Score {score} OK, but price is not in M1 'Extreme' zone yet. Waiting for retracement.")
                return None
            
            # If we are stalking, we still calculate hypothetical SL/TP for the radar report
            if is_stalking:
                logger.info(f"[STALKING] Mature setup (score {score}). {stalking_reason}")

            # 4. Calculate safe distance
            min_risk = (df_m1["high"] - df_m1["low"]).tail(20).mean() * 1.5
            
            if extreme_sl:
                proposed_sl = extreme_sl
                # For normal zone entries: ensure SL is not trivially tight vs spread
                if not is_extreme and not momentum_bypass:
                    if direction == "buy":
                        proposed_sl = min(proposed_sl, current_price - min_risk)
                    else:
                        proposed_sl = max(proposed_sl, current_price + min_risk)
                if momentum_bypass:
                    logger.info(f"🚀 MOMENTUM ZONE SL: Breakout entry, SL at zone boundary {proposed_sl:.2f}")
                else:
                    logger.info(f"🎯 1M HIGH PRECISION ENTRY: SL set to {proposed_sl:.2f} (Extreme: {is_extreme})")
            else:
                # No structural zone found — fallback to ATR/swing
                swing_risk = min_risk * 1.2
                if direction == "buy":
                    proposed_sl = current_price - max(atr_val * 1.5, swing_risk)
                else:
                    proposed_sl = current_price + max(atr_val * 1.5, swing_risk)
                if momentum_bypass:
                    logger.info(f"🚀 MOMENTUM ATR SL: No zone found, ATR/Swing SL {proposed_sl:.2f}")
                else:
                    logger.info(f"🔌 FALLBACK SL SET: No structure, ATR/Swing SL {proposed_sl:.2f}")

            # 5. Take Profit Calculation — Fibo extension first, session-RR fallback
            risk_dist = abs(current_price - proposed_sl)
            risk_reward_multiplier = 10.0 if is_extreme else session_rr_mult

            # Session-RR baseline (fallback if no clean Fibo swing found)
            if direction == "buy":
                rr_target = current_price + (risk_dist * risk_reward_multiplier)
            else:
                rr_target = current_price - (risk_dist * risk_reward_multiplier)

            # Fibo extension TP: derive from H1 (preferred) or M15 swing
            fibo_tp = None
            fibo_label = ""
            try:
                from core.structure.swing import detect_swings
                for tf_name, df_tf in (("H1", df_h1), ("M15", df_m15)):
                    if df_tf is None or len(df_tf) < 50:
                        continue
                    sw = detect_swings(df_tf.tail(120))
                    highs = [s.price for s in sw if s.kind == 'high']
                    lows  = [s.price for s in sw if s.kind == 'low']
                    if not (highs and lows):
                        continue
                    sh, sl_pt = highs[-1], lows[-1]
                    if sh <= sl_pt:
                        continue
                    rng = sh - sl_pt
                    if rng < 1.0:
                        continue
                    if direction == "buy":
                        cand_127 = sh + rng * 0.272
                        cand_161 = sh + rng * 0.618
                        # If price already past 127.2%, jump to 161.8%
                        fibo_tp = cand_127 if cand_127 > current_price else cand_161
                        fibo_label = f"{tf_name} 127.2%" if cand_127 > current_price else f"{tf_name} 161.8%"
                    else:
                        cand_127 = sl_pt - rng * 0.272
                        cand_161 = sl_pt - rng * 0.618
                        fibo_tp = cand_127 if cand_127 < current_price else cand_161
                        fibo_label = f"{tf_name} 127.2%" if cand_127 < current_price else f"{tf_name} 161.8%"
                    break
            except Exception:
                pass

            # Use Fibo TP only if it yields structurally-valid RR (≥1.5)
            if fibo_tp is not None and risk_dist > 0:
                fibo_rr = abs(fibo_tp - current_price) / risk_dist
                if fibo_rr >= 1.5:
                    raw_target = fibo_tp
                    logger.info(
                        f"📐 FIBO TP [{fibo_label}] @ {fibo_tp:.2f} (RR {fibo_rr:.2f}) — "
                        f"overriding session {risk_reward_multiplier}x RR"
                    )
                else:
                    raw_target = rr_target
                    logger.info(
                        f"📐 Fibo TP too close (RR {fibo_rr:.2f} < 1.5) — "
                        f"fallback to session RR {risk_reward_multiplier}x"
                    )
            else:
                raw_target = rr_target
            
            # TP Synchronization: Align with existing trades if available
            anchor_tp = kwargs.get("anchor_tp")
            if is_extreme and anchor_tp:
                # If anchor_tp is further than our raw_target, use it
                is_further = (direction == "buy" and anchor_tp > raw_target) or (direction == "sell" and anchor_tp < raw_target)
                if is_further:
                    logger.info(f"🔄 TP SYNC: Aligning extreme signal target with anchor trade @ {anchor_tp:.2f}")
                    raw_target = anchor_tp

            # Inward TP buffer (1.5–2.0 pips):
            # Pull TP slightly inside the raw target to ensure it is reached
            # before spread or wick overshoot can front-run the exit.
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

            # ── THIN RETRACE TP OVERRIDE ─────────────────────────────────────
            # When we entered "thin" (price not yet at zone, only retracing toward
            # it), TP must be CLOSER not farther. Otherwise SL hits before the
            # far Fibo extension. Use the nearest Fibo level (50%-100% of the
            # impulse, capped at RR 1.5) so the trade has high hit probability.
            entry_tier_label = "ZONE"
            lot_mult = 1.0
            if thin_retrace:
                entry_tier_label = "THIN_RETRACE"
                lot_mult = thin_mult  # 0.5 by default
                # Tight TP: cap at RR 1.5 (closer than session/Fibo extension)
                tight_rr = 1.5
                if direction == "buy":
                    tight_tp = current_price + (risk_dist * tight_rr)
                    if tight_tp < take_profit:
                        take_profit = tight_tp
                        final_rr = tight_rr
                        logger.info(
                            f"📉 THIN RETRACE TP CAP: TP pulled in to {tight_tp:.2f} "
                            f"(RR {tight_rr}) — high hit probability for half-size entry"
                        )
                else:
                    tight_tp = current_price - (risk_dist * tight_rr)
                    if tight_tp > take_profit:
                        take_profit = tight_tp
                        final_rr = tight_rr
                        logger.info(
                            f"📉 THIN RETRACE TP CAP: TP pulled in to {tight_tp:.2f} "
                            f"(RR {tight_rr}) — high hit probability for half-size entry"
                        )
            elif momentum_bypass:
                entry_tier_label = "MOMENTUM"

            return TradeSignal(
                signal_id      = f"SMC_{int(time.time())}",
                symbol         = kwargs.get("symbol", SYMBOL), # Use global SYMBOL if not provided
                timestamp      = utc_now(),
                direction      = direction,
                entry_price    = current_price,
                stop_loss      = proposed_sl,
                take_profit    = take_profit,
                rr_ratio       = final_rr,
                score          = score,
                max_score      = 15,
                atr_pips       = atr_val,
                session        = session_name,
                is_extreme     = is_extreme,
                is_stalking    = is_stalking,
                stalking_reason = stalking_reason,
                lot_multiplier = lot_mult,
                entry_tier     = entry_tier_label,
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
