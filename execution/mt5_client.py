"""
MT5 Client — wraps the MetaTrader5 Python API.

All MT5 operations are isolated here.  The rest of the system
never imports MetaTrader5 directly.
"""
from __future__ import annotations
import time
from typing import Optional, List, Dict, Any
from datetime import datetime

from config.settings import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_PATH, SYMBOL
from config.risk_config import RISK_CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None          # type: ignore
    MT5_AVAILABLE = False
    logger.warning("MetaTrader5 package not installed — MT5 operations will be simulated")


class MT5Client:
    def __init__(self):
        self._connected = False

    # ── Connection ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        if not MT5_AVAILABLE:
            logger.warning("MT5 not available — running in simulation mode")
            self._connected = True
            return True

        if not mt5.initialize(path=MT5_PATH):
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
            return {"balance": 10000.0, "equity": 10000.0, "margin_free": 9000.0}
        info = mt5.account_info()
        if info is None:
            return None
        return {
            "balance":     info.balance,
            "equity":      info.equity,
            "margin_free": info.margin_free,
            "profit":      info.profit,
            "leverage":    info.leverage,
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
        """Fetch OHLCV bars from MT5."""
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

        for attempt in range(max_retries):
            result = mt5.order_send(request)
            if result is None:
                logger.warning(f"order_send returned None (attempt {attempt+1})")
                time.sleep(0.5)
                continue

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(
                    f"Order placed: #{result.order} {direction} {lot_size}L "
                    f"@ {result.price} SL={stop_loss} TP={take_profit}"
                )
                return {"order": result.order, "price": result.price, "retcode": result.retcode}

            logger.warning(
                f"Order failed (attempt {attempt+1}): retcode={result.retcode} "
                f"comment={result.comment}"
            )
            time.sleep(0.5)

        logger.error(f"Order placement failed after {max_retries} attempts")
        return None

    def close_position(self, position_ticket: int, symbol: str, volume: float) -> bool:
        if not MT5_AVAILABLE:
            logger.info(f"[SIM] Close position #{position_ticket}")
            return True

        pos = mt5.positions_get(ticket=position_ticket)
        if not pos:
            logger.warning(f"Position #{position_ticket} not found")
            return False

        p = pos[0]
        close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(symbol)
        close_price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       symbol,
            "volume":       volume,
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
                "comment":    p.comment,
            }
            for p in positions
        ]
