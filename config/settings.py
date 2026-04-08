"""
Global settings for the AI Trading System.
All environment-sensitive values should be overridden via .env
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# ─── MT5 ──────────────────────────────────────────────────────────────────────
MT5_LOGIN    = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER   = os.getenv("MT5_SERVER", "ICMarketsSC-Demo")
MT5_PATH     = os.getenv("MT5_PATH", "C:/Program Files/MetaTrader 5/terminal64.exe")

# ─── Symbol ───────────────────────────────────────────────────────────────────
SYMBOL       = "XAUUSD"
SYMBOL_POINT = 0.01       # 1 pip = 0.01 for gold
PIP_VALUE    = 1.0        # USD per pip per 0.01 lot (approximate)

# ─── Database ─────────────────────────────────────────────────────────────────
DB_URL = os.getenv(
    "DB_URL",
    "postgresql+asyncpg://trader:trader@localhost:5432/trading_db"
)
DB_ECHO = os.getenv("DB_ECHO", "false").lower() == "true"

# ─── Anthropic AI ─────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AI_MODEL          = "claude-opus-4-5"
AI_MAX_TOKENS     = 512

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR   = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ─── Mode ─────────────────────────────────────────────────────────────────────
RUN_MODE = os.getenv("RUN_MODE", "backtest")   # "backtest" | "live" | "paper"
