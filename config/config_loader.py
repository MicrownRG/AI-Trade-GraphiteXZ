"""
Config Loader — layered configuration per MT5 account.

Load priority (highest → lowest):
    1. Database  (account_config table, keyed by MT5 login)
    2. .env      (BOT_<FIELD> prefix, e.g. BOT_TRADE_MODE)
    3. Default   (hardcoded in RiskConfig / TradingConfig)

Bootstrap rule:
    If a DB field is NULL *and* a value is found in .env or defaults,
    the DB field is written once and never overwritten again.
    This means the first startup populates the DB from .env/defaults,
    and all subsequent changes go through DB (or Telegram commands).

Usage (in main.py, after MT5 connects and init_db() runs):
    from config.config_loader import ConfigLoader
    ConfigLoader.load(account_id=str(mt5_login))
"""
from __future__ import annotations

import os
from typing import Any, Optional

from database.connection import get_session
from database.models import AccountConfigModel
from config.risk_config import RISK_CONFIG
from config.trading_config import TRADING_CONFIG, TradeMode
from utils.logger import get_logger

logger = get_logger(__name__)

# ── .env key → (db_field, type, default_attr_path) ────────────────────────────
# default_attr_path: "risk.field" reads RISK_CONFIG.field
#                    "trade.field" reads TRADING_CONFIG.field
#                    "trade.mode_settings.MODE.field" not supported — use trade.current_mode

_FIELD_MAP: list[tuple[str, str, type, str]] = [
    # (env_key, db_column, python_type, default_source)
    ("BOT_RISK_PER_TRADE_PCT",      "risk_per_trade_pct",       float, "risk.risk_per_trade_pct"),
    ("BOT_MIN_RR_RATIO",            "min_rr_ratio",             float, "risk.min_rr_ratio"),
    ("BOT_MAX_LOT_SIZE",            "max_lot_size",             float, "risk.max_lot_size"),
    ("BOT_HARD_CUTLOSS_PCT",        "hard_cutloss_daily_pct",   float, "risk.hard_cutloss_daily_pct"),
    ("BOT_MAX_CONCURRENT_TRADES",   "max_concurrent_trades",    int,   "risk.max_concurrent_trades"),
    ("BOT_MULTI_ENTRY_DELAY_SEC",   "multi_entry_delay_sec",    int,   "risk.multi_entry_delay_sec"),
    ("BOT_AUTO_CLOSE_STAGNANT_H",   "auto_close_stagnant_hours",int,   "risk.auto_close_stagnant_hours"),
    ("BOT_AUTO_CLOSE_EOD",          "auto_close_eod",           bool,  "risk.auto_close_eod"),
    ("BOT_MIN_MARGIN_LEVEL_PCT",    "min_margin_level_pct",     float, "risk.min_margin_level_pct"),
    ("BOT_NEWS_RISK_MULTIPLIER",    "news_risk_multiplier",     float, "risk.news_risk_multiplier"),
    ("BOT_REVENGE_COOLDOWN_MIN",    "revenge_cooldown_min",     float, "risk.revenge_cooldown_min"),
    ("BOT_CONSECUTIVE_LOSS_LIMIT",  "consecutive_loss_limit",   int,   "risk.consecutive_loss_limit"),
    ("BOT_MAX_SPREAD_PIPS",         "max_allowed_spread_pips",  float, "risk.max_allowed_spread_pips"),
    ("BOT_MAX_SLIPPAGE_PIPS",       "max_slippage_pips",        float, "risk.max_slippage_pips"),
    ("BOT_MIN_SIGNAL_SCORE",        "min_signal_score",         int,   "risk.min_signal_score"),
    ("BOT_MIN_AI_CONFIDENCE",       "min_ai_confidence",        float, "risk.min_ai_confidence"),
    ("BOT_TRADE_MODE",              "trade_mode",               str,   "trade.current_mode"),
    ("BOT_ENABLE_ASIAN_SESSION",    "enable_asian_session",     bool,  "trade.enable_asian_session"),
    ("BOT_ENABLE_PARTIAL_CLOSE",    "enable_partial_close",     bool,  "trade.enable_partial_close"),
    ("BOT_AI_ADAPTER_ENABLED",      "ai_adapter_enabled",       bool,  "trade.ai_adapter_enabled"),
    ("BOT_NEWS_BLACKOUT_MINUTES",   "news_blackout_minutes",    int,   "trade.news_blackout_minutes"),
]


def _get_default(source: str) -> Any:
    """Read a default value from the live singleton config objects."""
    prefix, field = source.split(".", 1)
    if prefix == "risk":
        val = getattr(RISK_CONFIG, field, None)
        # TradeMode enum → string
        return val.value if isinstance(val, TradeMode) else val
    if prefix == "trade":
        val = getattr(TRADING_CONFIG, field, None)
        return val.value if isinstance(val, TradeMode) else val
    return None


def _parse_env(raw: str, typ: type) -> Any:
    """Cast a raw .env string to the correct Python type."""
    raw = raw.strip()
    if typ is bool:
        return raw.lower() in ("1", "true", "yes", "on")
    if typ is int:
        return int(raw)
    if typ is float:
        return float(raw)
    return raw  # str


def _apply_to_config(db_field: str, value: Any) -> None:
    """Write a resolved value back into the in-memory singleton configs."""
    # Risk config fields (frozen dataclass — use object.__setattr__)
    risk_fields = {
        "risk_per_trade_pct", "min_rr_ratio", "max_lot_size",
        "hard_cutloss_daily_pct", "max_concurrent_trades", "multi_entry_delay_sec",
        "auto_close_stagnant_hours", "auto_close_eod", "min_margin_level_pct",
        "news_risk_multiplier", "revenge_cooldown_min", "consecutive_loss_limit",
        "max_allowed_spread_pips", "max_slippage_pips", "min_signal_score",
        "min_ai_confidence",
    }

    if db_field in risk_fields:
        try:
            # Enforce the hard ceiling for cutloss
            if db_field == "hard_cutloss_daily_pct":
                value = min(float(value), 5.0)
            object.__setattr__(RISK_CONFIG, db_field, value)
        except Exception as e:
            logger.error(f"ConfigLoader: failed to apply {db_field}={value} to RISK_CONFIG: {e}")
        return

    # TradingConfig fields (mutable dataclass)
    if db_field == "trade_mode":
        try:
            TRADING_CONFIG.current_mode = TradeMode[str(value).upper()]
        except KeyError:
            logger.warning(f"ConfigLoader: unknown trade_mode '{value}' — keeping default")
        return

    trade_fields = {
        "enable_asian_session", "enable_partial_close",
        "ai_adapter_enabled", "news_blackout_minutes",
    }
    if db_field in trade_fields:
        try:
            setattr(TRADING_CONFIG, db_field, value)
        except Exception as e:
            logger.error(f"ConfigLoader: failed to apply {db_field}={value} to TRADING_CONFIG: {e}")


class ConfigLoader:
    @staticmethod
    def load(account_id: str) -> None:
        """
        Load config for the given MT5 account_id.
        Populates DB from .env/defaults if any field is NULL.
        Applies all resolved values to RISK_CONFIG and TRADING_CONFIG singletons.
        """
        try:
            with get_session() as session:
                row = session.query(AccountConfigModel).filter_by(account_id=account_id).first()

                if row is None:
                    row = AccountConfigModel(account_id=account_id)
                    session.add(row)
                    logger.info(f"ConfigLoader: new account config row created for {account_id}")

                needs_flush = False

                for env_key, db_field, typ, default_source in _FIELD_MAP:
                    db_val = getattr(row, db_field, None)

                    if db_val is not None:
                        # DB has a value → use it directly
                        resolved = db_val
                        source   = "db"
                    else:
                        # DB empty → try .env
                        env_raw = os.getenv(env_key)
                        if env_raw is not None:
                            try:
                                resolved = _parse_env(env_raw, typ)
                                source   = "env"
                            except (ValueError, TypeError):
                                logger.warning(
                                    f"ConfigLoader: cannot parse {env_key}='{env_raw}' as {typ.__name__} — using default"
                                )
                                resolved = _get_default(default_source)
                                source   = "default"
                        else:
                            # .env also empty → use default
                            resolved = _get_default(default_source)
                            source   = "default"

                        # Write back to DB (first-time bootstrap)
                        if resolved is not None:
                            db_write = resolved.value if isinstance(resolved, TradeMode) else resolved
                            setattr(row, db_field, db_write)
                            needs_flush = True

                    if resolved is not None:
                        _apply_to_config(db_field, resolved)

                    logger.debug(
                        f"ConfigLoader [{account_id}] {db_field}={resolved!r} (source={source})"
                    )

                if needs_flush:
                    session.flush()

            logger.info(f"ConfigLoader: config loaded for account {account_id}")

        except Exception as e:
            logger.error(f"ConfigLoader: load failed for {account_id}: {e} — using defaults")
