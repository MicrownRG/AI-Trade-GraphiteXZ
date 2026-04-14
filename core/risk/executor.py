"""
Risk Executor.

Orchestrates the full risk pipeline for a given signal:
  1. Run all pre-trade filters
  2. Calculate lot size (with volatile session cap)
  3. Confirm stop loss / take profit levels
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
        pf           = self.portfolio
        session_name = get_session_name(current_time)

        # ── Margin protection (session-aware) ─────────────────────────────────
        # Stricter margin requirement during volatile sessions (NY / London)
        # to avoid forced liquidation during spike moves.
        min_margin = 1000.0 if session_name in ("NY", "LONDON") else 500.0
        if margin_level < min_margin:
            return self._reject(
                signal,
                f"Low margin for {session_name} session: {margin_level:.1f}% (need > {min_margin:.0f}%)"
            )

        is_pulse = "PULSE" in signal.signal_id

        if is_pulse:
            # Pulse trades supply their own tight SL/TP
            sl      = signal.stop_loss
            sl_pips = price_to_pips(abs(signal.entry_price - sl))

            settings     = TRADING_CONFIG.mode_settings.get(TRADING_CONFIG.current_mode, {})
            use_compound = settings.get("pulse_compound", False)

            if use_compound:
                _, max_lot = get_equity_lot_caps(pf.balance, session_name=session_name)
                lot_size   = max(0.01, min(max_lot, round((pf.balance // 500) * 0.01, 2) or 0.01))
            else:
                lot_size = 0.01

        else:
            if getattr(signal, "is_extreme", False):
                sl = signal.stop_loss
            else:
                # Calculate SL from market structure using M15 for reliability
                sl = calculate_stop_loss(
                    direction   = signal.direction,
                    entry_price = signal.entry_price,
                    swings      = swings,
                    df_ltf      = df_m15,
                )

            # ── Session-aware SL floor ────────────────────────────────────────
            # Prevents SL from being so tight it is immediately swept during
            # normal session volatility, especially during US / Overlap hours.
            #
            #   Asia         :  1.5 pip  — calm, tight spreads, thin SL OK
            #   Pre-London   :  2.5 pip  — warming up, slightly wider
            #   London       :  4.0 pip  — active but structured
            #   NY           : 10.0 pip  — very volatile, spike protection
            #   Overlap 12-16: 15.0 pip  — most brutal window, maximum buffer
            #   Late US/other:  3.0 pip  — transitioning, medium protection
            hour       = current_time.hour
            is_overlap = (12 <= hour < 16)

            if is_overlap:
                min_sl_floor = 15.0
            elif session_name == "NY":
                min_sl_floor = 10.0
            elif session_name == "LONDON":
                min_sl_floor = 4.0
            elif session_name == "ASIA":
                min_sl_floor = 1.5
            else:
                min_sl_floor = 3.0

            raw_sl_pips = price_to_pips(abs(signal.entry_price - sl))
            sl_pips     = max(min_sl_floor, raw_sl_pips)

            # Reposition SL price to match the floored pip distance
            # (ensures the SL price and sl_pips are consistent for lot sizing)
            if sl_pips > raw_sl_pips and raw_sl_pips > 0:
                sl_price_dist = sl_pips * 0.1
                if signal.direction == "buy":
                    sl = signal.entry_price - sl_price_dist
                else:
                    sl = signal.entry_price + sl_price_dist

            # Session and VIX multiplier for lot scaling
            multiplier = self._get_session_multiplier(current_time)

            atr_m1  = (df_m1["high"] - df_m1["low"]).tail(5).mean()  if df_m1 is not None and len(df_m1) >= 5  else 0.0
            atr_avg = (df_m1["high"] - df_m1["low"]).tail(20).mean() if df_m1 is not None and len(df_m1) >= 20 else 0.0

            # Use min(balance, equity) to prevent over-sizing when floating losses exist
            safe_balance = min(pf.balance, pf.equity) if pf.equity > 0 else pf.balance

            lot_size = calculate_lot_size(
                account_balance    = safe_balance,
                stop_loss_pips     = sl_pips,
                risk_pct           = risk_pct_override or RISK_CONFIG.risk_per_trade_pct,
                news_active        = news_active,
                signal_score       = getattr(signal, "score", 8.0),
                recovery_multiplier= recovery_multiplier * multiplier,
                current_atr        = atr_m1,
                avg_atr            = atr_avg,
                strategy           = signal.signal_id,
                session_name       = session_name,   # No.31 volatile session reduction
            )

        # ── Cumulative lot cap check (equity-aware) ───────────────────────────
        safe_balance = min(pf.balance, pf.equity) if pf.equity > 0 else pf.balance
        total_cap, _ = get_equity_lot_caps(
            safe_balance, strategy=signal.signal_id, session_name=session_name
        )
        current_open_lots = sum(t["lot_size"] for t in pf.open_trades.values())
        remaining_cap     = max(0.0, total_cap - current_open_lots)

        if lot_size > remaining_cap:
            logger.info(
                f"Lot {lot_size} capped to {remaining_cap:.2f} "
                f"(cumulative portfolio cap, session={session_name})"
            )
            lot_size = remaining_cap

        risk_amount = lot_size * sl_pips * 10.0   # approximate USD
        risk_pct    = risk_amount / safe_balance * 100 if safe_balance > 0 else 0

        # ── Take profit ───────────────────────────────────────────────────────
        if is_pulse:
            tp_levels = None  # Pulse uses its own TP from the signal
        else:
            tp_levels = calculate_take_profit(
                direction   = signal.direction,
                entry_price = signal.entry_price,
                stop_loss   = sl,
                swings      = swings,
                min_rr      = getattr(signal, "rr_ratio", None),
            )

        # ── Position count and pyramiding logic ───────────────────────────────
        open_positions        = list(pf.open_trades.values())
        same_direction_count  = len([
            t for t in open_positions
            if t.get("symbol", signal.symbol) == signal.symbol and t["direction"] == signal.direction
        ])

        mode = TRADING_CONFIG.current_mode
        if mode in (TradeMode.VERY_AGGRESSIVE, TradeMode.ULTRA_SCALPER):
            max_pos = 5
        elif mode == TradeMode.AGGRESSIVE:
            max_pos = 2
        else:  # CONSERVATIVE / MODERATE
            max_pos = 1

        if is_pulse:
            pulse_count = len([t for t in pf.open_trades.keys() if "PULSE" in t])
            if pulse_count >= 2:
                return self._reject(
                    signal,
                    f"Max Pulse positions (2) reached ({pulse_count} active)"
                )
            if margin_level < 300.0:
                return self._reject(
                    signal,
                    f"Pulse rejected: margin {margin_level:.1f}% < 300% required"
                )
        else:
            if same_direction_count >= max_pos:
                return self._reject(
                    signal,
                    f"Max {mode.value} positions ({max_pos}) reached for "
                    f"{signal.symbol} {signal.direction.upper()}"
                )

        # ── Pre-trade filters ─────────────────────────────────────────────────
        # Extreme re-entries bypass the global concurrent limit
        is_extreme           = getattr(signal, "is_extreme", False)
        effective_open_count = (
            0 if (is_extreme and same_direction_count > 0)
            else pf.open_trade_count
        )

        passed, failures = run_all_filters(
            spread_pips            = spread_pips,
            dt                     = current_time,
            daily_pnl              = pf.realized_pnl,   # realized-only, not floating
            balance                = pf.balance,
            trades_today           = trades_today,
            open_positions         = effective_open_count,
            atr_pips               = signal.atr_pips if hasattr(signal, "atr_pips") else 10.0,
            rr                     = tp_levels.rr_at_tp2 if tp_levels else signal.rr_ratio,
            margin_level           = margin_level,
            news_active            = news_active,
            session                = signal.session,
            calculated_risk_pct    = risk_pct,
            last_trade_closed_at   = pf.last_trade_closed_at,
            last_trade_result_pnl  = pf.last_trade_result_pnl,
            last_trade_opened_at   = pf.last_trade_opened_at,
            current_floating_pnl   = pf.current_floating_pnl,
            signal_type            = "PULSE" if is_pulse else "REGULAR",
            cutloss_triggered      = pf.daily_cutloss_triggered,
        )

        if not passed:
            return self._reject(signal, "; ".join(failures))

        if is_pulse:
            logger.info(
                f"PULSE approved: {signal.direction.upper()} {signal.symbol} "
                f"lot={lot_size} SL={sl:.2f} TP={signal.take_profit:.2f} "
                f"risk=${risk_amount:.2f}"
            )
        else:
            logger.info(
                f"Trade approved: {signal.direction.upper()} {signal.symbol} "
                f"lot={lot_size} SL={sl:.2f} TP2={tp_levels.tp2:.2f} "
                f"RR={tp_levels.rr_at_tp2:.2f} risk={risk_pct:.2f}% session={session_name}"
            )

        # ── No.18 Round number offset ─────────────────────────────────────────
        # Shift SL and TP slightly away from .00 round numbers to avoid
        # market-maker stop-hunts at clean psychological levels.
        sl_adjusted = round_number_offset(sl, signal.direction, is_tp=False)
        if sl_adjusted != sl:
            logger.debug(f"Round# offset (No.18): SL {sl:.2f} -> {sl_adjusted:.2f}")
            sl = sl_adjusted

        if is_pulse:
            tp_adj = round_number_offset(signal.take_profit, signal.direction, is_tp=True)
            if tp_adj != signal.take_profit:
                logger.debug(f"Round# offset (No.18): Pulse TP {signal.take_profit:.2f} -> {tp_adj:.2f}")
                signal.take_profit = tp_adj
        elif tp_levels:
            tp2_adj = round_number_offset(tp_levels.tp2, signal.direction, is_tp=True)
            if tp2_adj != tp_levels.tp2:
                logger.debug(f"Round# offset (No.18): TP2 {tp_levels.tp2:.2f} -> {tp2_adj:.2f}")
                tp_levels.tp2 = tp2_adj
            if hasattr(tp_levels, "tp1") and tp_levels.tp1:
                tp1_adj = round_number_offset(tp_levels.tp1, signal.direction, is_tp=True)
                if tp1_adj != tp_levels.tp1:
                    logger.debug(f"Round# offset (No.18): TP1 {tp_levels.tp1:.2f} -> {tp1_adj:.2f}")
                    tp_levels.tp1 = tp1_adj

        return TradeOrder(
            signal_id        = signal.signal_id,
            symbol           = signal.symbol,
            direction        = signal.direction,
            entry_price      = signal.entry_price,
            stop_loss        = sl,
            take_profit_levels = tp_levels,
            lot_size         = lot_size,
            risk_amount      = round(risk_amount, 2),
            risk_pct         = round(risk_pct, 2),
            approved         = True,
            take_profit      = signal.take_profit,
            timestamp        = current_time,
        )

    @staticmethod
    def _reject(signal: TradeSignal, reason: str) -> TradeOrder:
        logger.warning(f"Trade rejected [{signal.signal_id}]: {reason}")
        return TradeOrder(
            signal_id          = signal.signal_id,
            symbol             = signal.symbol,
            direction          = signal.direction,
            entry_price        = signal.entry_price,
            stop_loss          = 0.0,
            take_profit_levels = None,  # type: ignore
            lot_size           = 0.0,
            risk_amount        = 0.0,
            risk_pct           = 0.0,
            approved           = False,
            rejection_reason   = reason,
        )

    def _get_session_multiplier(self, dt: datetime) -> float:
        """
        Session-aware risk multiplier applied to lot sizing.

        Reduces position size during volatile sessions to protect against spikes
        and limit drawdown.  Combined with No.31 equity cap reduction, this gives
        a two-layer protection during US / Overlap hours.

        Multipliers:
          Asia / Late US  -> 1.00x  (calm, tight SL, full lot OK)
          Pre-London      -> 0.90x  (warming up)
          London          -> 0.85x  (active but structured)
          NY              -> 0.60x  (very volatile, reduce lot)
          Overlap 12-16   -> 0.50x  (most brutal session, minimum lot)
        """
        hour       = dt.hour
        cfg        = TRADING_CONFIG
        is_overlap = (12 <= hour < 16)

        if is_overlap:
            logger.debug(f"Session multiplier: OVERLAP (12-16 UTC) -> x0.50")
            return 0.50

        is_ny     = cfg.ny_session[0]     <= hour < cfg.ny_session[1]
        is_london = cfg.london_session[0] <= hour < cfg.london_session[1]
        is_asia   = cfg.asian_session[0]  <= hour < cfg.asian_session[1]

        if is_ny:
            logger.debug(f"Session multiplier: NY (hour={hour}) -> x0.60")
            return 0.60
        if is_london:
            logger.debug(f"Session multiplier: LONDON (hour={hour}) -> x0.85")
            return 0.85
        if is_asia:
            return 1.00

        return 0.90  # Pre-London / Late US transition
