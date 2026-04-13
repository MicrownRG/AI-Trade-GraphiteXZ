"""
MT5 Client — wraps the MetaTrader5 Python API.

All MT5 operations are isolated here.  The rest of the system
never imports MetaTrader5 directly.
"""
from __future__ import annotations
import time
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

from config.settings import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_PATH, SYMBOL
from config.risk_config import RISK_CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
    _MT5_SOURCE = "native"
except ImportError:
    # Linux / non-Windows: try mt5linux bridge (pip install mt5linux)
    # Requires Wine + MT5 terminal running as bridge server:
    #   wine python -m mt5linux &
    try:
        import mt5linux as mt5  # type: ignore
        MT5_AVAILABLE = True
        _MT5_SOURCE = "mt5linux"
        logger.info("MetaTrader5 (via mt5linux Wine bridge) loaded")
    except ImportError:
        mt5 = None          # type: ignore
        MT5_AVAILABLE = False
        _MT5_SOURCE = "none"
        logger.warning(
            "MetaTrader5 package not installed — MT5 operations will be simulated. "
            "On Linux: install mt5linux (pip install mt5linux) and run: wine python -m mt5linux"
        )



class MT5Client:
    def __init__(self):
        self._connected = False

    # ── Connection ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        if not MT5_AVAILABLE:
            logger.warning("MT5 not available — running in simulation mode")
            self._connected = True
            return True

        # Initialize MT5. If MT5_PATH is provided, use it; otherwise rely on default lookup.
        if MT5_PATH:
            init_success = mt5.initialize(path=MT5_PATH)
        else:
            init_success = mt5.initialize()
        if not init_success:
            logger.error(f"MT5 initialize failed: {mt5.last_error()}")
            return False

        authorized = mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
        if not authorized:
            logger.error(f"MT5 login failed: {mt5.last_error()}")
            mt5.shutdown()
            return False

        info = mt5.account_info()
        logger.info(
            f"MT5 connected: account={info.login} server={info.server} "
            f"balance={info.balance:.2f} leverage=1:{info.leverage}"
        )
        self._connected = True
        return True

    def disconnect(self) -> None:
        if MT5_AVAILABLE and mt5:
            mt5.shutdown()
        self._connected = False
        logger.info("MT5 disconnected")

    # ── Account ────────────────────────────────────────────────────────────────

    def get_account_info(self) -> Optional[Dict]:
        if not MT5_AVAILABLE:
            return {
                "balance":      10000.0,
                "equity":       10000.0,
                "margin_free":  9000.0,
                "margin_level": 99999.0,   # sim: no positions open
            }
        info = mt5.account_info()
        if info is None:
            return None
        # margin_level = (equity / margin) * 100 — 0 margin means no positions
        margin_level = (
            (info.equity / info.margin * 100) if info.margin > 0 else 99999.0
        )
        return {
            "login":        info.login,
            "balance":      info.balance,
            "equity":       info.equity,
            "margin_free":  info.margin_free,
            "profit":       info.profit,
            "leverage":     info.leverage,
            "margin_level": round(margin_level, 1),
        }

    # ── Market Data ────────────────────────────────────────────────────────────

    def get_symbol_tick(self, symbol: str = SYMBOL) -> Optional[Dict]:
        if not MT5_AVAILABLE:
            return {"bid": 1950.00, "ask": 1950.03, "spread": 30}
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        spread = round((tick.ask - tick.bid) / 0.01)   # spread in pips
        return {"bid": tick.bid, "ask": tick.ask, "spread": spread}

    def get_ohlcv(
        self, symbol: str, timeframe: str, count: int = 500
    ) -> Optional[Any]:
        """Fetch OHLCV bars from MT5 for a single timeframe."""
        if not MT5_AVAILABLE:
            return None
        tf_map = {
            "M1":  mt5.TIMEFRAME_M1,  "M5":  mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
            "H1":  mt5.TIMEFRAME_H1,  "H4":  mt5.TIMEFRAME_H4,
            "D1":  mt5.TIMEFRAME_D1,
        }
        tf = tf_map.get(timeframe)
        if tf is None:
            raise ValueError(f"Unknown timeframe: {timeframe}")
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        return rates

    def get_ohlcv_multi(
        self, symbol: str, specs: list[tuple[str, int]]
    ) -> dict[str, Any]:
        """Fetch OHLCV for multiple timeframes.
        `specs` is a list of (timeframe, count) tuples.
        Returns a dict mapping timeframe string to the raw rates array.
        """
        if not MT5_AVAILABLE:
            return {}
        result: dict[str, Any] = {}
        for tf, cnt in specs:
            result[tf] = self.get_ohlcv(symbol, tf, cnt)
        return result

    # ── Order Execution ────────────────────────────────────────────────────────

    def place_market_order(
        self,
        symbol: str,
        direction: str,
        lot_size: float,
        stop_loss: float,
        take_profit: float,
        comment: str = "",
        max_retries: int = 3,
    ) -> Optional[Dict]:
        """
        Place a market order with SL and TP.
        Returns order result dict or None on failure.
        """
        if not MT5_AVAILABLE:
            logger.info(f"[SIM] Market order: {direction} {lot_size}L SL={stop_loss} TP={take_profit}")
            return {"order": 999999, "price": stop_loss, "simulated": True}

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error(f"Cannot get tick for {symbol}")
            return None

        order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
        price      = tick.ask if direction == "buy" else tick.bid

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       symbol,
            "volume":       lot_size,
            "type":         order_type,
            "price":        price,
            "sl":           stop_loss,
            "tp":           take_profit,
            "deviation":    int(RISK_CONFIG.max_slippage_pips * 10),
            "magic":        20240101,
            "comment":      comment[:31],
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        last_result = None
        for attempt in range(max_retries):
            result = mt5.order_send(request)
            if result is None:
                logger.warning(f"order_send returned None (attempt {attempt+1})")
                time.sleep(0.5)
                continue

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(
                    f"Order placed: #{result.order} {direction} {lot_size}L "
                    f"@ {result.price} Requested SL={stop_loss} TP={take_profit}"
                )
                
                # ECN/Market Execution Fix: Some brokers ignore SL/TP on the initial deal.
                # If the position has SL=0 but we requested an SL, modify it immediately.
                if stop_loss > 0 or take_profit > 0:
                    pos = mt5.positions_get(ticket=result.order)
                    if pos:
                        p = pos[0]
                        if (stop_loss > 0 and p.sl == 0.0) or (take_profit > 0 and p.tp == 0.0):
                            logger.info(f"Broker stripped SL/TP on entry. Modifying #{result.order} to apply SL={stop_loss} TP={take_profit}")
                            self.modify_position(result.order, sl=stop_loss if stop_loss > 0 else None, tp=take_profit if take_profit > 0 else None)
                            
                return {"order": result.order, "price": result.price, "retcode": result.retcode}

            last_result = result
            logger.warning(
                f"Order failed (attempt {attempt+1}): retcode={result.retcode} "
                f"comment={result.comment}"
            )
            time.sleep(0.5)

        logger.error(f"Order placement failed after {max_retries} attempts")
        if last_result is not None:
            return {"error": True, "comment": last_result.comment, "retcode": last_result.retcode}
        return None

    def modify_position(
        self,
        position_ticket: int,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> bool:
        """
        Modify SL and/or TP of an existing position.
        Returns True on success, False on failure.
        """
        if not MT5_AVAILABLE:
            logger.info(f"[SIM] Modify position #{position_ticket} SL={sl} TP={tp}")
            return True

        pos = mt5.positions_get(ticket=position_ticket)
        if not pos:
            logger.warning(f"modify_position: ticket #{position_ticket} not found")
            return False

        p = pos[0]
        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": position_ticket,
            "symbol":   p.symbol,
            "sl":       sl if sl is not None else p.sl,
            "tp":       tp if tp is not None else p.tp,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result is not None else mt5.last_error()
            retcode = result.retcode if result is not None else -1
            logger.error(
                f"modify_position failed: ticket=#{position_ticket} "
                f"sl={sl} tp={tp} err={err}"
            )
            return {"error": True, "comment": err, "retcode": retcode}
        logger.debug(f"modify_position OK: #{position_ticket} SL={sl} TP={tp}")
        return {"error": False}

    def close_position(self, position_ticket: int, symbol: str = "", volume: float = 0.0) -> bool:
        if not MT5_AVAILABLE:
            logger.info(f"[SIM] Close position #{position_ticket}")
            return True

        pos = mt5.positions_get(ticket=position_ticket)
        if not pos:
            logger.warning(f"Position #{position_ticket} not found")
            return False

        p = pos[0]
        # Use actual position symbol/volume if not provided
        _symbol = symbol or p.symbol
        _volume = volume if volume > 0 else p.volume
        close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(_symbol)
        close_price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       _symbol,
            "volume":       _volume,
            "type":         close_type,
            "position":     position_ticket,
            "price":        close_price,
            "deviation":    50,
            "magic":        20240101,
            "comment":      "close",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE

    def get_open_positions(self, symbol: str | None = None) -> List[Dict]:
        if not MT5_AVAILABLE:
            return []
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if positions is None:
            return []
        return [
            {
                "ticket":     p.ticket,
                "symbol":     p.symbol,
                "direction":  "buy" if p.type == 0 else "sell",
                "volume":     p.volume,
                "open_price": p.price_open,
                "sl":         p.sl,
                "tp":         p.tp,
                "profit":     p.profit,
                "magic":      p.magic,
                "comment":    p.comment,
            }
            for p in positions
        ]

    def get_daily_realized_pnl(self) -> float:
        """Sums specialized realized profit, commission, and swap of all trades closed since the start of today."""
        if not MT5_AVAILABLE:
            return 0.0
            
        # Get start of CURRENT day in local time (WIB context if running on local windows)
        now = datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # history_deals_get uses datetime objects
        deals = mt5.history_deals_get(start, now)
        if deals is None:
            return 0.0
            
        valid = [d for d in deals if d.type in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL) and d.entry == mt5.DEAL_ENTRY_OUT]
        total_pnl = sum(d.profit + d.commission + d.swap for d in valid)
        return total_pnl

    def get_history_deals(self, start: datetime, end: datetime) -> List[Dict]:
        """Fetches raw closed deals (entry/exit history) from MT5 for the specified date range."""
        if not MT5_AVAILABLE:
            return []
            
        deals = mt5.history_deals_get(start, end)
        if deals is None:
            return []
            
        # We only care about deals that closed a position (DEAL_ENTRY_OUT)
        closed_deals = [d for d in deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY) and d.type in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL)]
        
        result = []
        for d in closed_deals:
            pos_id = d.position_id
            
            # Reconstruct the original trade direction
            trade_dir = "sell" if d.type == mt5.DEAL_TYPE_BUY else "buy"
            
            # Fetch the entire position history to find entry price
            pos_deals = mt5.history_deals_get(position=pos_id)
            entry_price = 0.0
            if pos_deals:
                for pd in pos_deals:
                    if pd.entry == mt5.DEAL_ENTRY_IN:
                        entry_price = pd.price
                        break
            
            result.append({
                "trade_id": str(d.ticket),
                "position_id": pos_id,
                "symbol": d.symbol,
                "direction": trade_dir,
                "lot_size": d.volume,
                "entry_price": entry_price,
                "exit_price": d.price,       
                "pnl": d.profit + d.commission + d.swap,
                "profit": d.profit,
                "commission": d.commission,
                "swap": d.swap,
                "magic": d.magic,
                "comment": d.comment,
                "closed_at": datetime.fromtimestamp(d.time, tz=timezone.utc), 
            })
        
        # Sort by closing time ascending
        result.sort(key=lambda x: x["closed_at"])
        return result
    def get_daily_deals_today(self) -> list:
        """
        Returns all closed deals from today (since 00:00 UTC).
        Replaces direct `import MetaTrader5 as mt5_api` usage in main.py.
        Returns list of raw MT5 deal objects, or [] on Linux/simulation.
        """
        if not MT5_AVAILABLE:
            return []
        from datetime import datetime
        now   = datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        deals = mt5.history_deals_get(start, now)
        return list(deals) if deals else []

    def get_raw_positions(self, symbol: str | None = None) -> list:
        """
        Returns raw MT5 position objects for a symbol (or all).
        Replaces direct `mt5_api.positions_get(symbol=SYMBOL)` in main.py.
        Returns [] on Linux/simulation.
        """
        if not MT5_AVAILABLE:
            return []
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        return list(positions) if positions else []

    def get_raw_position_by_ticket(self, ticket: int) -> object | None:
        """
        Returns a single raw MT5 position object by ticket.
        Replaces direct `mt5_api.positions_get(ticket=ticket)` in main.py.
        Returns None on Linux/simulation.
        """
        if not MT5_AVAILABLE:
            return None
        positions = mt5.positions_get(ticket=ticket)
        if positions:
            return positions[0]
        return None
