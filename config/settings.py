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

# ─── AI Settings ─────────────────────────────────────────────────────────────
USE_AI            = os.getenv("USE_AI", "true").lower() == "true"
AI_PROVIDER       = os.getenv("AI_PROVIDER", "deepseek").lower()
DEEPSEEK_API_KEY  = os.getenv("DEEPSEEK_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
AI_MODEL          = os.getenv("AI_MODEL", "deepseek-chat")
AI_MAX_TOKENS     = int(os.getenv("AI_MAX_TOKENS", "512"))

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR   = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ─── Mode ─────────────────────────────────────────────────────────────────────
RUN_MODE           = os.getenv("RUN_MODE", "backtest")   # "backtest" | "live" | "paper"
DEFAULT_TRADE_MODE = os.getenv("DEFAULT_TRADE_MODE", "VERY_AGGRESSIVE")
