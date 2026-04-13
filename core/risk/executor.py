"""
Risk Executor.

Orchestrates the full risk pipeline for a given signal:
  1. Run all pre-trade filters
  2. Calculate lot size
  3. Confirm stop loss / take profit
  4. Return an approved (or rejected) TradeOrder
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List
from datetime import datetime
import pandas as pd

from core.signal.signal_engine import TradeSignal
from core.structure.swing import SwingPoint
from core.risk.lot_sizing import calculate_lot_size, round_number_offset, price_to_pips, get_equity_lot_caps
from core.risk.stop_loss import calculate_stop_loss
from core.risk.take_profit import calculate_take_profit, TakeProfitLevels
from core.risk.filters import run_all_filters

from core.risk.portfolio import Portfolio
from config.risk_config import RISK_CONFIG
from config.trading_config import TRADING_CONFIG, TradeMode
from utils.logger import get_logger
from utils.time_utils import get_session_name

logger = get_logger(__name__)


@dataclass
class TradeOrder:
    signal_id: str
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit_levels: TakeProfitLevels
    lot_size: float
    risk_amount: float
    risk_pct: float
    approved: bool
    rejection_reason: str = ""
    take_profit: float = 0.0  # Direct TP for PULSE (bypasses tp_levels)
    timestamp: Optional[datetime] = None


class RiskExecutor:
    def __init__(self, portfolio: Portfolio):
        self.portfolio = portfolio

    def evaluate(
        self,
        signal: TradeSignal,
        df_m15: pd.DataFrame,
        df_m5: pd.DataFrame,
        df_m1: pd.DataFrame,
        swings: List[SwingPoint],
        spread_pips: float,
        current_time: datetime,
        trades_today: int,
        margin_level: float = 1000.0,
        news_active: bool = False,
        recovery_multiplier: float = 1.0,
        risk_pct_override: float | None = None,
    ) -> TradeOrder:
        """
        Full risk evaluation pipeline for a signal.
        Returns an approved or rejected TradeOrder.
        """
        pf = self.portfolio

        # ── Margin Protection (Session-Aware) ────────────────────────────────
        # Stricter margin requirement for volatile sessions (NY/London)
        session_name = get_session_name(current_time)
        min_margin = 1000.0 if session_name in ("NY", "LONDON") else 500.0
        if margin_level < min_margin:
            return self._reject(signal, f"Low margin level for {session_name} session: {margin_level:.1f}% (Required > {min_margin}%)")

        is_pulse = "PULSE" in signal.signal_id
        if is_pulse:
            # Pulse trades bring their own tight SL/TP and enforce extreme safety
            sl = signal.stop_loss
            sl_pips = price_to_pips(abs(signal.entry_price - sl))
            
            # Compounding Check
            settings = TRADING_CONFIG.mode_settings.get(TRADING_CONFIG.current_mode, {})
            use_compound = settings.get("pulse_compound", False)
            
            if use_compound:
                _, max_lot = get_equity_lot_caps(pf.balance)
                lot_size = max(0.01, min(max_lot, round((pf.balance // 500) * 0.01, 2) or 0.01))
            else:
                lot_size = 0.01
        else:
            if getattr(signal, "is_extreme", False):
                sl = signal.stop_loss
            else:
                # ── Calculate SL from structure (Using M15 for reliability) ───────────
                sl = calculate_stop_loss(
                    direction=signal.direction,
                    entry_price=signal.entry_price,
                    swings=swings,
                    df_ltf=df_m15,
                )
            
            # floor SL pips for sizing to prevent astronomical lots on micro-pips (e.g. 0.1 pip)
            # SESSION-AWARE SL FLOOR:
            #   Asia         : 1.5 pip  — tenang, spread kecil, SL tipis aman
            #   Pre-London   : 2.5 pip  — sedikit lebih lebar antisipasi London open
            #   London       : 4.0 pip  — aktif tapi masih terstruktur
            #   NY / Overlap : 10.0 pip — SANGAT LIAR, spike besar, SL harus jauh
            #   Late US      : 5.0 pip  — mulai mereda tapi masih bisa volatile
            session_name = get_session_name(current_time)
            hour = current_time.hour
            is_overlap = (12 <= hour < 16)  # London-NY overlap: paling volatile

            if is_overlap:
                min_sl_floor = 15.0   # Overlap = paling brutal, butuh buffer besar
            elif session_name == "NY":
                min_sl_floor = 10.0   # NY open/close masih sangat volatile
            elif session_name == "LONDON":
                min_sl_floor = 4.0    # London: terstruktur tapi aktif
            elif session_name == "ASIA":
                min_sl_floor = 1.5    # Asia: tenang, spread kecil, SL tipis OK
            else:
                min_sl_floor = 3.0    # Transisi / pre-session
                
            raw_sl_pips = price_to_pips(abs(signal.entry_price - sl))
            sl_pips = max(min_sl_floor, raw_sl_pips)

            # ── Lot size & Score-based Scaling ────────────────────────────────────
            multiplier = self._get_session_multiplier(current_time)

            # ATR for Synthetic VIX (already computed above)
            atr_m1 = (df_m1["high"] - df_m1["low"]).tail(5).mean()  if df_m1 is not None and len(df_m1) >= 5 else 0.0
            atr_avg = (df_m1["high"] - df_m1["low"]).tail(20).mean() if df_m1 is not None and len(df_m1) >= 20 else 0.0

            # Use min(balance, equity) to prevent over-sizing when floating losses are present
            safe_balance = min(pf.balance, pf.equity) if pf.equity > 0 else pf.balance
            # Lot size calculation now handles quality, news, and Synthetic VIX internally
            lot_size = calculate_lot_size(
                account_balance=safe_balance,
                stop_loss_pips=sl_pips,
                risk_pct=risk_pct_override or RISK_CONFIG.risk_per_trade_pct,
                news_active=news_active,
                signal_score=getattr(signal, "score", 8.0),
                recovery_multiplier=recovery_multiplier * multiplier,
                current_atr=atr_m1,
                avg_atr=atr_avg,
                strategy=signal.signal_id,
            )
        
        # ── Cumulative Lot Cap Check (equity-aware) ────────────────────────────
        safe_balance = min(pf.balance, pf.equity) if pf.equity > 0 else pf.balance
        total_pnl_cap, _ = get_equity_lot_caps(safe_balance, strategy=signal.signal_id)
        current_open_lots = sum(t["lot_size"] for t in pf.open_trades.values())
        remaining_cap = max(0.0, total_pnl_cap - current_open_lots)
        
        if lot_size > remaining_cap:
            logger.info(f"Lot size {lot_size} capped to {remaining_cap:.2f} (Cumulative Portfolio Cap)")
            lot_size = remaining_cap

        risk_amount = lot_size * sl_pips * 10.0   # approx USD
        risk_pct    = risk_amount / safe_balance * 100 if safe_balance > 0 else 0

        # ── Take profit ───────────────────────────────────────────────────────
        if is_pulse:
            # Pulse uses its own TP from the signal, no structural swing needed
            tp_levels = None
        else:
            tp_levels = calculate_take_profit(
                direction=signal.direction,
                entry_price=signal.entry_price,
                stop_loss=sl,
                swings=swings,
                min_rr=getattr(signal, "rr_ratio", None),
            )

        # ── Position Count & Pyramiding Logic ─────────────────────────────────
        open_positions = list(pf.open_trades.values())
        same_direction_count = len([t for t in open_positions if t["symbol"] == signal.symbol and t["direction"] == signal.direction])
        
        # Determine Max Positions based on Trade Mode
        mode = TRADING_CONFIG.current_mode
        if mode in (TradeMode.VERY_AGGRESSIVE, TradeMode.ULTRA_SCALPER):
            max_pos = 5
        elif mode == TradeMode.AGGRESSIVE:
            max_pos = 2
        else: # CONSERVATIVE / MODERATE
            max_pos = 1
            
        is_extreme = getattr(signal, "is_extreme", False)
        
        if is_pulse:
            # Count specifically PULSE trades for the limit check
            pulse_count = len([t for t in pf.open_trades.keys() if "PULSE" in t])
            if pulse_count >= 2:
                 return self._reject(signal, f"Max Pulse Positions (2) reached ({pulse_count} active Pulse trades)")
            
            # Special Margin Check for Pulse as requested
            if margin_level < 300.0:
                return self._reject(signal, f"Pulse rejected: Margin Level {margin_level:.1f}% < 300% required.")
        else:
            # Handle standard/extreme positions based on mode-calculated max_pos
            if same_direction_count >= max_pos:
                 return self._reject(signal, f"Max {mode.value} Positions ({max_pos}) reached for {signal.symbol} {signal.direction.upper()}")

        # ── Filters ───────────────────────────────────────────────────────────
        # Allow extreme re-entries to slightly bypass global concurrent limit, otherwise use true count
        effective_open_count = 0 if (is_extreme and same_direction_count > 0) else pf.open_trade_count
        
        passed, failures = run_all_filters(
            spread_pips=spread_pips,
            dt=current_time,
            daily_pnl=pf.daily_pnl,
            balance=pf.balance,
            trades_today=trades_today,
            open_positions=effective_open_count,
            atr_pips=signal.atr_pips if hasattr(signal, "atr_pips") else 10.0,
            rr=tp_levels.rr_at_tp2 if tp_levels else signal.rr_ratio,
            margin_level=margin_level,
            news_active=news_active,
            session=signal.session,
            calculated_risk_pct=risk_pct,
            last_trade_closed_at=pf.last_trade_closed_at,
            last_trade_result_pnl=pf.last_trade_result_pnl,
            last_trade_opened_at=pf.last_trade_opened_at,
            current_floating_pnl=pf.current_floating_pnl,
            signal_type="PULSE" if is_pulse else "REGULAR",
        )

        if not passed:
            return self._reject(signal, "; ".join(failures))

        if is_pulse:
            logger.info(
                f"⚡ PULSE approved: {signal.direction.upper()} {signal.symbol} "
                f"lot={lot_size} SL={sl:.2f} TP={signal.take_profit:.2f} risk=${risk_amount:.2f}"
            )
        else:
            logger.info(
                f"✅ Trade approved: {signal.direction.upper()} {signal.symbol} "
                f"lot={lot_size} SL={sl:.2f} TP2={tp_levels.tp2:.2f} "
                f"RR={tp_levels.rr_at_tp2:.2f} risk={risk_pct:.2f}%"
            )

        # ── No.18 Round Number Offset ────────────────────────────────────────
        # Shift SL and TP slightly away from round numbers (.00) to avoid
        # market-maker stop hunts at clean psychological levels.
        sl_adjusted = round_number_offset(sl, signal.direction, is_tp=False)
        if sl_adjusted != sl:
            logger.debug(f"Round# Offset (No.18): SL {sl:.2f} → {sl_adjusted:.2f}")
            sl = sl_adjusted

        if is_pulse:
            tp_adjusted = round_number_offset(signal.take_profit, signal.direction, is_tp=True)
            if tp_adjusted != signal.take_profit:
                logger.debug(f"Round# Offset (No.18): Pulse TP {signal.take_profit:.2f} → {tp_adjusted:.2f}")
                signal.take_profit = tp_adjusted
        elif tp_levels:
            tp2_adjusted = round_number_offset(tp_levels.tp2, signal.direction, is_tp=True)
            if tp2_adjusted != tp_levels.tp2:
                logger.debug(f"Round# Offset (No.18): TP2 {tp_levels.tp2:.2f} → {tp2_adjusted:.2f}")
                tp_levels.tp2 = tp2_adjusted
            if hasattr(tp_levels, 'tp1') and tp_levels.tp1:
                tp1_adjusted = round_number_offset(tp_levels.tp1, signal.direction, is_tp=True)
                if tp1_adjusted != tp_levels.tp1:
                    logger.debug(f"Round# Offset (No.18): TP1 {tp_levels.tp1:.2f} → {tp1_adjusted:.2f}")
                    tp_levels.tp1 = tp1_adjusted

        return TradeOrder(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_loss=sl,
            take_profit_levels=tp_levels,
            lot_size=lot_size,
            risk_amount=round(risk_amount, 2),
            risk_pct=round(risk_pct, 2),
            approved=True,
            take_profit=signal.take_profit,
            timestamp=current_time,
        )

    @staticmethod
    def _reject(signal: TradeSignal, reason: str) -> TradeOrder:
        logger.warning(f"❌ Trade rejected [{signal.signal_id}]: {reason}")
        return TradeOrder(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_loss=0.0,
            take_profit_levels=None,  # type: ignore
            lot_size=0.0,
            risk_amount=0.0,
            risk_pct=0.0,
            approved=False,
            rejection_reason=reason,
        )

    def _get_session_multiplier(self, dt: datetime) -> float:
        """
        Session-aware risk multiplier.
        NY/Overlap is the most volatile: reduce lot to protect against spikes.
        Asia is calm: full lot allowed.
        London: slightly cautious.

        Multipliers:
          Asia / Late US  → 1.00x (calm, tight SL, full sizing OK)
          Pre-London      → 0.90x (warming up)
          London          → 0.85x (active but structured)
          NY              → 0.60x (very volatile, reduce lot)
          Overlap (12-16) → 0.50x (most brutal session, minimum lot)
        """
        hour = dt.hour
        cfg  = TRADING_CONFIG

        # London-NY Overlap 12:00–16:00 UTC: most volatile window
        is_overlap = (12 <= hour < 16)
        if is_overlap:
            logger.debug(f"Session multiplier: OVERLAP (12-16 UTC) → x0.50")
            return 0.50

        is_ny     = cfg.ny_session[0] <= hour < cfg.ny_session[1]    # 12-21
        is_london = cfg.london_session[0] <= hour < cfg.london_session[1]  # 7-16
        is_asia   = cfg.asian_session[0] <= hour < cfg.asian_session[1]    # 0-9

        if is_ny:
            logger.debug(f"Session multiplier: NY (hour={hour}) → x0.60")
            return 0.60
        if is_london:
            logger.debug(f"Session multiplier: LONDON (hour={hour}) → x0.85")
            return 0.85
        if is_asia:
            return 1.00  # Asia: calm, SL tipis, full lot OK

        # Pre-London / Late US transition
        return 0.90
