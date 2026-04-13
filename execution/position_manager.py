"""
Position Manager.

Reconciles in-memory portfolio state with live MT5 positions.
Used on startup to re-hydrate state after a crash or restart.
"""
from __future__ import annotations
from typing import List, Dict
from datetime import datetime, timezone

from execution.mt5_client import MT5Client
from core.risk.portfolio import Portfolio
from database.repository import TradeRepository
from utils.logger import get_logger

logger = get_logger(__name__)


class PositionManager:
    def __init__(
        self,
        mt5: MT5Client,
        portfolio: Portfolio,
        repo: TradeRepository,
    ):
        self.mt5       = mt5
        self.portfolio = portfolio
        self.repo      = repo

    def sync_on_startup(self) -> None:
        """
        On live system startup, reconcile MT5 open positions
        with database records to restore portfolio state.
        """
        mt5_positions = self.mt5.get_open_positions()
        db_trades     = self.repo.get_open_trades()  # returns list of dicts

        db_tickets  = {t["mt5_ticket"] for t in db_trades if t.get("mt5_ticket")}
        mt5_tickets = {p["ticket"] for p in mt5_positions}

        # Positions in MT5 but NOT in DB → orphaned, log a warning
        orphaned = mt5_tickets - db_tickets
        if orphaned:
            logger.warning(f"Orphaned MT5 positions (not in DB): {orphaned}")

        # Positions in DB as open but NOT in MT5 → closed externally
        externally_closed = db_tickets - mt5_tickets
        for ticket in externally_closed:
            trade = next((t for t in db_trades if t.get("mt5_ticket") == ticket), None)
            if trade:
                logger.warning(
                    f"Trade {trade['trade_id']} closed externally (ticket #{ticket})"
                )
                from core.risk.portfolio import ClosedTrade
                ct = ClosedTrade(
                    trade_id   = trade["trade_id"],
                    symbol     = trade["symbol"],
                    direction  = trade["direction"],
                    entry_price= trade["entry_price"],
                    exit_price = trade["entry_price"],  # unknown
                    lot_size   = trade["lot_size"],
                    pnl        = 0.0,
                    pnl_pips   = 0.0,
                    opened_at  = trade["opened_at"],
                    closed_at  = datetime.now(timezone.utc),
                    reason     = "external_close",
                )
                self.portfolio.close_trade(ct)
                self.repo.save_trade_close(ct)

        # Re-register remaining open positions (ONLY if they belong to the bot)
        BOT_MAGIC = 20240101
        restored_count = 0
        
        for pos in mt5_positions:
            # Check if trade belongs to this bot (via magic number or specialized comment)
            is_bot_trade = (pos.get("magic") == BOT_MAGIC) or (str(pos.get("comment", "")).startswith("sig:"))
            
            if not is_bot_trade:
                logger.info(f"Ignoring manual/orphaned position #{pos['ticket']} (Symbol: {pos['symbol']})")
                continue

            ticket = pos["ticket"]
            
            # Prevent duplication if portfolio.json already loaded this ticket
            already_tracked = any(t.get("mt5_ticket") == ticket for t in self.portfolio.open_trades.values())
            if already_tracked:
                continue

            trade_id = f"restored_{ticket}"
            
            # Safe SL/TP conversion: 0.0 means 'not set' in MT5
            sl_val = pos["sl"] if pos["sl"] > 0 else None
            tp_val = pos["tp"] if pos["tp"] > 0 else None
            
            self.portfolio.open_trade(trade_id, {
                "mt5_ticket":  pos["ticket"],
                "signal_id":   "restored",
                "symbol":      pos["symbol"],
                "direction":   pos["direction"],
                "entry_price": pos["open_price"],
                "lot_size":    pos["volume"],
                "stop_loss":   sl_val,
                "take_profit": tp_val,
                "tp_levels":   None,
                "be_activated": False,
                "opened_at":   datetime.now(timezone.utc),
            })
            restored_count += 1

        logger.info(
            f"Position sync: {restored_count} MT5 positions restored "
            f"| {len(externally_closed)} externally closed reconciled"
        )

    def close_all_positions(self, reason: str = "manual_cutloss") -> int:
        """Emergency: close all open positions. Returns number closed."""
        positions = self.mt5.get_open_positions()
        closed = 0
        for p in positions:
            success = self.mt5.close_position(
                position_ticket=p["ticket"],
                symbol=p["symbol"],
                volume=p["volume"],
            )
            if success:
                closed += 1
                logger.warning(f"Force-closed position #{p['ticket']} reason={reason}")
        return closed

    def get_total_exposure_lots(self) -> float:
        """Total lots across all open positions."""
        return sum(
            t.get("lot_size", 0) for t in self.portfolio.open_trades.values()
        )

    def get_floating_pnl(self) -> float:
        """Sum of floating P&L from MT5."""
        positions = self.mt5.get_open_positions()
        return sum(p.get("profit", 0) for p in positions)
