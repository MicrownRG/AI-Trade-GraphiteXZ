# 🏆 XAUUSD AI Quantitative Trading System

<div align="center">

![Python](https://img.shields.io/badge/Python-3.11+-blue?style=flat-square&logo=python)
![MT5](https://img.shields.io/badge/MetaTrader5-ICMarkets_Raw-purple?style=flat-square)
![AI](https://img.shields.io/badge/AI-DeepSeek_%7C_Claude_%7C_Gemini-orange?style=flat-square)
![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL-336791?style=flat-square&logo=postgresql)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)
![Tests](https://img.shields.io/badge/Tests-83%20passed-brightgreen?style=flat-square)

**Production-grade institutional trading system for XAUUSD (Gold) on MetaTrader 5**

*ICT-based structure analysis · Multi-AI provider scoring · Event-driven backtesting · Full risk management*

</div>

---

## 📋 Table of Contents

- [Overview](#-overview)
- [Key Features](#-key-features)
- [System Architecture](#-system-architecture)
- [Trading Logic](#-trading-logic)
- [Signal Scoring System](#-signal-scoring-system)
- [AI Evaluation Layer](#-ai-evaluation-layer)
- [Telegram Control & Notifications](#-telegram-control--notifications)
- [Risk Management Engine](#-risk-management-engine)
- [Logic Flow](#-logic-flow)
- [Project Structure](#-project-structure)
- [Installation](#-installation)
- [Configuration](#-configuration)
- [Data Requirements](#-data-requirements)
- [Running the System](#-running-the-system)
- [Backtesting](#-backtesting)
- [Live Trading](#-live-trading)
- [Database Schema](#-database-schema)
- [Performance Metrics](#-performance-metrics)
- [Troubleshooting](#-troubleshooting)
- [Disclaimer](#-disclaimer)

---

## 🎯 Overview

This system is a **production-grade algorithmic trading bot** built on **ICT (Inner Circle Trader) institutional methodology** for trading **XAUUSD (Gold) on MetaTrader 5** via ICMarkets Raw Spread account.

Unlike indicator-only bots, this system replicates how institutional traders read the market — through **market structure shifts**, **liquidity engineering**, and **displacement analysis** across multiple timeframes. An **AI layer** (DeepSeek, Claude, or Gemini) acts as an independent second opinion to filter low-quality setups before execution.

**Core philosophy:**
- Signal generation is **rules-based** (structure + math) — not hallucinated by AI
- AI is used **only to evaluate and filter** signals, never to generate them
- Every trade must pass **structural + risk + AI gates** before execution
- The system is fully **auditable, logged, and explainable**

---

## ✨ Key Features

### 🚀 Advanced Trading Modes
- **Conservative**: 0.5% risk, high score threshold (ICT-only logic).
- **Moderate**: 1.5% risk, balanced scoring.
- **Aggressive**: 5% risk, lower threshold, recovery logic enabled.
- **Very Aggressive**: 15% risk, high-frequency focus, MARTINGALE recovery, + **Pulse Scalping Engine** enabled (rapid 0.01 lot injections).
- **Ultra Scalper**: Fast high-frequency M1 RSI/Bollinger mode. Targets small 15-pip TP ($1.50 - $5.00+), compounds safely based on equity ($500+ = +0.01 lot), and strictly throttles (Wait & Snipe) when experiencing minus daily PnL.

### 🛡️ Premium Anti-Liquidation
- **Margin Level Watch**: Automatic lot scaling if margin level < 200%.
- **News Logic**: 50% risk reduction 30m before High-Impact events.
- **Drawdown Kill-Switch**: Hard stop at 15% daily drawdown (adjustable).
- **Auto-BE & Partial Close**: Protects equity on structural targets (50% TP1).

### ⚡ Ultra-Low Latency Execution & Micro-Scalping
- **Live Tick Streaming**: Split-loop architecture. Caches HTF (H4/H1) every 60s while streaming LTF (M1/M5) every 3 seconds for zero-delay reaction.
- **Pulse Scalper Engine**: Dispatches rapid 0.01 lot injections directly off M1 momentum when aligned with H1 bias. Auto-suspends if 3 consecutive losses hit to protect margins.
- **M1 Sweep Sniper**: Filters out early entries on Order Blocks by waiting for a confirmed wick rejection (sweep) before entering.

### 🧠 Hybrid Logic Engine
- **Fibonacci MTF Strategy**: Automated Fibo retracement/extension logic across M15/M5/M1 with strict RSI bounce gates and Staged Trailing Stop-Loss (50% TP1, 25% TP2, ride TP3/Ext).
- **SMC (Smart Money Concepts)**: Ported institutional logic (FVG, OB, Liquidity).
- **Trend & Momentum**: Weighted scoring (EMA, ADX, RSI, BB).
- **GainzAlgo v2 Alpha**: ATR-based BOS/CHoCH structural filtering.

### Market Structure Analysis
- **Swing High/Low** detection using fractal-style logic
- **BOS (Break of Structure)** — trend continuation confirmation
- **CHOCH (Change of Character)** — early trend reversal detection
- **Liquidity Sweep** detection (stop-hunt / wick reversal patterns)
- **Equal Highs/Lows** — clustered stop-loss liquidity pool identification
- **Displacement** — strong impulsive candle (institutional order flow) detection
- **Fair Value Gap (FVG)** — imbalance zone for refined entries
- **Order Block** detection — last opposing candle before impulse

### Multi-Timeframe Analysis
- **HTF Bias** (H4 + H1): determines directional filter
- **LTF Entry** (M15 + M5): precise entry refinement
- Only trades in the direction of HTF structure — never counter-trend

### Multi-Provider AI Layer
- Supports **DeepSeek** (default), **Anthropic Claude**, **Google Gemini**
- Switch providers via single env variable `AI_PROVIDER=deepseek`
- Fully decoupled — new providers can be added by implementing `BaseAIProvider`
- Falls back to **rule-based scoring** in backtest mode (no API cost)

### Risk Management Engine
- Dynamic **lot sizing** based on % account risk per trade
- **Structure-based Stop Loss** — placed beyond the invalidation swing
- **Tiered Take Profit** (TP1 at 1R, TP2 at 2R, TP3 at structure target)
- **Zero-Trail Breakeven** — SL moves precisely to Entry (+0.01) after reaching target ratio, freezing there to guarantee a risk-free trade without premature trailing interruptions.
- **Kill Switch** — auto-halts trading on drawdown / loss streak breach
- **Pre-trade filters**: spread, session, daily loss, concurrent positions, ATR, RR

### Backtest Engine
- Fully **event-driven** (bar-by-bar, zero look-ahead bias)
- **Simulated spread** (dynamic, higher during Asian session)
- **Slippage simulation** (fill at next bar open + 2 pip slippage)
- **Session filtering** (London / New York only)
- Equity curve, monthly P&L breakdown, Sharpe ratio, profit factor

### Infrastructure
- **Cross-Platform (Linux / VPS)**: Full support for Linux execution via `mt5linux` bridge and official MetaQuotes Wine script without needing a Windows machine.
- **Chart Snapshots**: Auto-generates annotated candlestick charts (Fibo levels, Entry, SL, TP) sent instantly to Telegram for every trade using `mplfinance`.
- **PostgreSQL** persistence (trades, signals, performance metrics)
- **Telegram Bot** integration (real-time alerts, remote commands, loss guard notifications)
- **Multi-User support** (authorize multiple chat IDs to control the bot)
- **Docker Compose** for one-command full stack deployment
- **Rotating log files** with structured logging (Loguru + structlog)
- **83 automated tests** covering all modules

---

## 🏗 System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         TRADING SYSTEM                              │
│                                                                     │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────────────────┐  │
│  │  Data    │───▶│  Structure   │───▶│     Signal Engine        │  │
│  │ Fetcher  │    │  Analysis    │    │  (Scoring + Filtering)   │  │
│  └──────────┘    └──────────────┘    └────────────┬─────────────┘  │
│  MT5 / CSV       BOS  CHOCH  FVG                  │                │
│  H4  H1  M15     Liq  Swing  OB      ┌────────────▼─────────────┐  │
│  M5  feeds       Disp HTFBias        │       AI Scorer           │  │
│                                      │  DeepSeek / Claude /      │  │
│                                      │  Gemini / Rule-based      │  │
│                                      └────────────┬─────────────┘  │
│                                                   │                │
│                                      ┌────────────▼─────────────┐  │
│                                      │     Risk Executor         │  │
│                                      │  Lot Sizing · SL/TP       │  │
│                                      │  Filters · Kill Switch    │  │
│                                      └────────────┬─────────────┘  │
│                                                   │                │
│                   ┌────────────────────────────────▼─────────────┐ │
│                   │              Execution Layer                  │ │
│                   │    MT5Client · OrderManager · PositionMgr    │ │
│                   └────────────────────────────────┬─────────────┘ │
│                                                    │               │
│          ┌─────────────────────────────────────────▼────────────┐  │
│          │                  Persistence Layer                    │  │
│          │          PostgreSQL · Portfolio · Logger              │  │
│          └────────────────────────────────────────────────────--┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 📊 Trading Logic

### 1. HTF Bias Determination (H4 + H1)

The system first establishes **which direction to trade** before looking for entries.

| H4 Signal | H1 Confirmation | Result |
|---|---|---|
| Bullish BOS | Bullish BOS | **Strong Bullish** (confidence 0.85) |
| Bearish BOS | Bearish BOS | **Strong Bearish** (confidence 0.85) |
| Bullish BOS | Neutral | **Weak Bullish** (confidence 0.65) |
| Bullish CHOCH only | — | **Early Bullish** (confidence 0.45) |
| Conflicting | — | **Neutral — no trade** |

> If HTF is neutral, **no signals are generated** for that cycle.

### 2. LTF Entry Logic (M15 + M5)

Once HTF bias is established, the system scans M15 for:

```
Entry Checklist:
  ✅ HTF bias confidence >= 0.60
  ✅ Liquidity sweep in the OPPOSITE direction of bias
     (sell-side sweep → buy setup | buy-side sweep → sell setup)
  ✅ Displacement candle aligned with bias direction
  ✅ CHOCH on LTF confirms structure shift
  ✅ Session: London (07:00–16:00 UTC) or New York (12:00–21:00 UTC)
  ✅ ATR between 8 and 120 pips (not dead / not news)
  ✅ Signal score >= 5/10
  ✅ AI confidence >= 0.60
```

### 3. Entry Refinement (M5)

The `AdvancedSignalEngine` further refines the entry using:
- **Fair Value Gap (FVG)** — enter at gap boundary for better RR
- **Order Block** — enter at the origin of the impulse
- **Premium/Discount zone** — only buy in discount, only sell in premium

---

## 🧮 Signal Scoring System

Each trade setup receives a score out of **10 points**:

| Component | Points | Condition |
|---|---|---|
| HTF Alignment | +2 | HTF bias confidence ≥ 0.60 |
| Liquidity Sweep | +2 | Valid sweep aligned with bias |
| Displacement | +2 | Strong impulsive candle aligned with bias |
| Session Valid | +1 | London or New York session |
| Volatility OK | +1 | ATR between 8–120 pips |
| Structure Shift | +1 | CHOCH on LTF aligns with bias |
| CHOCH Confirmed | +1 | CHOCH event detected |

**Minimum threshold to proceed: 5/10**

Signals below 5 are discarded before reaching the AI or risk layer.

---

## 🤖 AI Evaluation Layer

The AI acts as a **senior analyst second opinion** — it evaluates, not generates.

### Input (sent to AI)
```json
{
  "symbol": "XAUUSD",
  "timestamp": "2024-01-15T10:00:00",
  "direction": "buy",
  "session": "London",
  "entry": 1950.00,
  "stop_loss": 1940.00,
  "take_profit": 1970.00,
  "rr_ratio": 2.0,
  "signal_score": "7/10",
  "htf_bias": "direction=bullish confidence=0.85 structure=hh_hl",
  "liquidity_sweep": "direction=sell_side swept_level=1945.50 wick_ratio=0.62",
  "displacement": "direction=bullish body_ratio=0.78 move_pips=22.5",
  "atr_pips": 30.1
}
```

### Output (from AI)
```json
{
  "confidence": 0.82,
  "decision": "TAKE",
  "reason": "Strong HTF bullish structure with confirmed sell-side sweep and displacement. London session entry at discount. RR 2.0 acceptable."
}
```

### Provider switching
```bash
AI_PROVIDER=deepseek    # DeepSeek-V3 (default — fastest, cheapest)
AI_PROVIDER=anthropic   # Claude Opus/Sonnet
AI_PROVIDER=gemini      # Gemini 2.0 Flash
```

**In backtest mode**, AI is disabled by default and replaced with a deterministic rule-based scorer to avoid API costs and ensure reproducibility.

---

## 📱 Telegram Control & Notifications

The system includes a dedicated Telegram bot for real-time monitoring and remote control.

### Core Features
- **Instant Alerts**: Get notified for trade entries, TP/SL hits, and breakeven shifts.
- **Remote Commands**: Monitor status, pause/resume trading, or close positions from your phone.
- **Adaptive Risk**: Loss Guard auto-pauses trading after consecutive losses or daily drawdown breaches.
- **Daily Summaries**: Automatic performance report at the end of every trading day.

### Bot Commands
| Command | Description |
|---|---|
| `/status` | View current balance, equity, and bot state |
| `/trades` | List all open positions with live PnL |
| `/mode` | 🛠 Switch between 5 trading modes (Con/Mod/Agr/V-Agr/Ultra) |
| `/mode_info` | 📑 Detail settings for each trading mode |
| `/history` | View today's closed trades summary |
| `/pause` | Temporarily halt new trade generation |
| `/resume` | Resume trading mode |
| `/pulse` | Manually reset and un-suspend the 3x Loss Guard for Pulse Scalper |
| `/trend` | Toggle H1 Trend Change notifications on/off (default OFF) |
| `/asian` | Toggle Asian session on/off |
| `/partial` | Toggle Partial Close feature on/off (50% TP1) |
| `/kill` | Emergency: activate kill switch |
| `/help` | List all available commands & info |

### Multi-User Security
Configure `TELEGRAM_ALLOWED_CHAT_IDS` in your `.env` to allow multiple administrators. Only authorized users can issue commands.

---

## 🛡 Risk Management Engine

### Lot Sizing Formula
```
risk_amount  = account_balance × (risk_pct / 100)
lot_size     = risk_amount / (sl_pips × pip_value_per_lot)

# XAUUSD: pip_value = $10 per pip per 1.0 lot
# Example: $10,000 balance, 1% risk, 20 pip SL
# → $100 / (20 × $10) = 0.50 lots
```

### 📈 Granular Equity Scaling (The "Measured" Path)
Batas lot akumulasi dan per posisi dirinci secara granular untuk akurasi pertumbuhan modal:

| Saldo (Equity) | Plafon Total (Kumulatif) | **Max Lot Per Entry** |
| :--- | :--- | :--- |
| **<$300** | 0.02 Lot | **0.01 Lot** |
| **$301 - $500** | 0.03 Lot | **0.01 Lot** |
| **$501 - $1000** | **0.04 Lot** | **0.01 Lot** |
| **$1001 - $1500** | **0.06 Lot** | **0.02 Lot** |
| **$1501 - $2500** | **0.10 Lot** | **0.03 Lot** |
| **$2501 - $4000** | **0.20 Lot** | **0.05 Lot** |
| **$4001 - $5000** | **0.30 Lot** | **0.06 Lot** |
| **$5001 - $6000** | **0.35 Lot** | **0.07 Lot** |
| **$6001 - $8000** | **0.50 Lot** | **0.10 Lot** |
| **$8001 - $10000** | **0.65 Lot** | **0.12 Lot** |
| **>$10000** | **1.00 Lot** | **0.20 Lot** |

### 🛡️ Anti-Revenge & Stability Logic
Bot dilengkapi dengan kontrol stabilitas untuk mencegah "revenge trading" dan *over-exposure*:
- **Cooldown Setelah Loss**: Jeda **15 menit** setelah posisi loss untuk menenangkan market.
- **Entry Delay Jeda**: Jeda **120 detik (2 menit)** antar entry untuk stabilitas sinyal.
- **Profit-Lock**: Baru menambah posisi (multi-entry) jika posisi sebelumnya sedang **PROFIT**.
- **Max Consecutive Loss**: Berhenti (Pause) 2 jam otomatis jika loss 3x berturut-turut.

### Stop Loss Placement
SL is placed **beyond the structural invalidation level**:
- **BUY**: Below the last confirmed swing low that would invalidate the setup
- **SELL**: Above the last confirmed swing high
- Buffer: 0.5 × ATR beyond the swing
- Minimum distance: 10 pips from entry

### Take Profit Levels
| Level | RR | Action |
|---|---|---|
| TP1 | 1:1 | Partial close 50% (optional) |
| TP2 | 1:2 | Main target (minimum) |
| TP3 | Structure | Next major swing level |

### Kill Switch Conditions
The kill switch permanently halts trading (requires manual reset) when:
- Drawdown exceeds **6%** of peak equity
- Daily loss exceeds **2.5%** of account
- **5 consecutive losing trades**
- Manual trigger via code

---

## 🔄 Logic Flow

```
Every M15 candle close:
│
├─ 1. Kill switch check → if ACTIVE, halt all operations
│
├─ 2. Telegram Commands → Process any incoming remote instructions
│
├─ 3. Fetch data
│      H4 (200 bars) → HTF structure
│      H1 (300 bars) → HTF confirmation
│      M15 (300 bars) → LTF entry
│
├─ 3. Manage open positions
│      • Check SL/TP hit on current bar H/L
│      • Update trailing stop
│      • Breakeven activation at 1R
│
├─ 4. Portfolio update
│      • Equity = balance + floating PnL
│      • Update drawdown
│      • Kill switch auto-check
│
├─ 5. Structure analysis (rolling 200-bar window)
│      H4: Swing → BOS → CHOCH → HTF Bias
│      M15: Swing → Liquidity → Displacement → CHOCH
│
├─ 6. Signal generation
│      • Score all components
│      • Score < 5 → DISCARD
│      • Score ≥ 5 → proceed
│
├─ 7. AI evaluation
│      • Build context prompt
│      • Call active provider (DeepSeek / Claude / Gemini)
│      • decision = SKIP → DISCARD
│      • confidence < 0.60 → DISCARD
│
├─ 8. Risk evaluation
│      • Spread check → > 30 pips → REJECT
│      • Session check → Asian session → REJECT
│      • Daily loss limit → breached → REJECT
│      • Concurrent positions → max 2 → REJECT
│      • ATR filter → out of range → REJECT
│      • RR check → < 2.0 → REJECT
│      • Calculate lot size (1% risk default)
│      • Calculate structure-based SL
│      • Calculate tiered TP levels
│
├─ 9. Execute trade
│      • Place market order on next bar open
│      • Apply 2 pip slippage simulation
│      • Log to PostgreSQL
│      • Update portfolio state
│      • Send Telegram notification (entry/AI reasoning)
│
└─10. Save performance snapshot to DB
```

---

## 📁 Project Structure

```
trading_bot/
│
├── config/                         # All configuration parameters
│   ├── settings.py                 # MT5, DB, AI credentials, run mode
│   ├── risk_config.py              # Risk thresholds, lot limits, kill switch levels
│   ├── trading_config.py           # Timeframes, sessions, scoring weights, ATR
│   └── ai_config.py                # AI model, temperature, confidence threshold
│
├── core/
│   ├── structure/                  # Market structure detection (all bar-by-bar, no look-ahead)
│   │   ├── swing.py                # Swing high/low detection (fractal method)
│   │   ├── bos.py                  # Break of Structure detection
│   │   ├── choch.py                # Change of Character detection
│   │   ├── liquidity.py            # Liquidity sweep detection (stop hunts)
│   │   ├── equal_levels.py         # Equal highs/lows (liquidity pool clusters)
│   │   ├── displacement.py         # Strong impulsive candle detection + ATR
│   │   └── htf_bias.py             # H4+H1 directional bias calculation
│   │
│   ├── signal/
│   │   ├── signal_engine.py        # Main signal generator (scoring + entry calc)
│   │   └── advanced_signal_engine.py  # FVG, Order Block, Premium/Discount zone
│   │
│   └── risk/
│       ├── lot_sizing.py           # Dynamic lot calculation (% risk method)
│       ├── stop_loss.py            # Structure-based SL + breakeven logic
│       ├── take_profit.py          # Tiered TP + trailing stop
│       ├── filters.py              # Pre-trade filter suite (7 filters)
│       ├── portfolio.py            # In-memory portfolio state tracker
│       ├── kill_switch.py          # Emergency halt with thread safety
│       └── executor.py             # Risk pipeline orchestrator
│
├── ai/
│   ├── client.py                   # Unified call_ai() / call_claude() interface
│   ├── prompt_builder.py           # Trade context → structured AI prompt
│   ├── scorer.py                   # AIScorer class + rule-based fallback
│   └── providers/
│       ├── base.py                 # BaseAIProvider abstract class + JSON helpers
│       ├── registry.py             # Factory: reads AI_PROVIDER env → returns instance
│       ├── anthropic_provider.py   # Anthropic Claude (claude-opus-4-5 etc.)
│       ├── deepseek_provider.py    # DeepSeek (deepseek-chat / deepseek-reasoner)
│       └── gemini_provider.py      # Google Gemini (gemini-2.0-flash etc.)
│
├── execution/
│   ├── mt5_client.py               # MetaTrader5 API wrapper (connect, tick, orders)
│   ├── order_manager.py            # Trade lifecycle: open → monitor → close
│   └── position_manager.py         # Startup reconciliation with MT5 live positions
│
├── data/
│   ├── fetcher.py                  # Load from MT5 or CSV/Parquet files
│   ├── processor.py                # OHLCV validation, spike removal, gap fill
│   └── feature_engineering.py      # ATR, body ratio, spread simulation, sessions
│
├── backtest/
│   ├── engine.py                   # Event-driven backtest (bar-by-bar, spread+slippage)
│   ├── strategy.py                 # Pluggable strategy interface (ICT / ICT_Advanced)
│   └── metrics.py                  # Performance report, equity curve, expectancy
│
├── database/
│   ├── models.py                   # SQLAlchemy ORM models (signals, trades, metrics)
│   ├── connection.py               # Engine + session factory
│   └── repository.py               # CRUD operations for all models
│
├── telegram/
│   ├── bot.py                      # Top-level bot orchestrator
│   ├── commands.py                 # Inbound command handlers (/status, /pause, etc.)
│   ├── notifier.py                 # Outbound message sending (Markdown-V2)
│   ├── formatter.py                # Rich message templates (Profit/Loss alerts)
│   └── pause_manager.py            # Global bot state (RUNNING, PAUSED, HALTED)
│
├── utils/
│   ├── logger.py                   # Rotating file logger with console output
│   ├── helpers.py                  # Price/pip conversion, rounding, formatting
│   └── time_utils.py               # UTC, WIB timezone, session validators
│
├── tests/
│   ├── run_tests.py                # Standalone test runner (no pytest required)
│   ├── conftest.py                 # Shared fixtures for pytest
│   ├── test_structure.py           # 17 structure module tests
│   ├── test_risk.py                # 33 risk module tests
│   ├── test_signal.py              # 8 signal + AI scorer tests
│   ├── test_data.py                # 13 data pipeline tests
│   └── test_backtest.py            # 12 backtest engine tests
│
├── logs/                           # Rotating log files (auto-created)
├── results/                        # Backtest output CSVs (auto-created)
├── data/                           # Place your OHLCV CSV/Parquet files here
│
├── main.py                         # Entry point (backtest / live / paper modes)
├── requirements.txt                # Python dependencies
├── Dockerfile                      # Multi-stage Docker build
├── docker-compose.yml              # PostgreSQL + Bot full stack
└── .env.example                    # Environment variable template
```

---

## ⚙️ Installation

### Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | 3.12 recommended |
| PostgreSQL | 14+ | Or use Docker Compose |
| MetaTrader 5 | Latest | **Windows only** for live trading |
| ICMarkets Account | Raw Spread | For live execution |

### Step 1 — Clone the repository

```bash
git clone https://github.com/yourname/trading_bot.git
cd trading_bot
```

### Step 2 — Create virtual environment

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Linux / macOS
python3 -m venv venv
source venv/bin/activate
```

### Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

> **Note for MetaTrader5**: The `MetaTrader5` package is Windows-only. On Linux/macOS it will be skipped automatically (backtesting still works fully).

### Step 4 — Set up environment variables

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# ── MT5 (required for live trading) ──
MT5_LOGIN=12345678
MT5_PASSWORD=your_password
MT5_SERVER=ICMarketsSC-Raw

# ── AI Provider (choose one) ──────────
AI_PROVIDER=deepseek          # deepseek | anthropic | gemini
DEEPSEEK_API_KEY=sk-...
# ANTHROPIC_API_KEY=sk-ant-...
# GEMINI_API_KEY=AIza...

# ── Database ──────────────────────────
DB_URL=postgresql+asyncpg://trader:trader@localhost:5432/trading_db

# ── Mode ──────────────────────────────
RUN_MODE=backtest             # backtest | live | paper

# ── Telegram ──────────────────────────
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_primary_id
TELEGRAM_ALLOWED_CHAT_IDS=id1,id2   # Optional: secondary admins
TELEGRAM_LOSS_PAUSE_MINUTES=60
TELEGRAM_CONSECUTIVE_LOSS_TRIGGER=3
TELEGRAM_DAILY_LOSS_PCT_TRIGGER=2.0
```

### Step 5 — Start PostgreSQL

**Option A — Docker (recommended):**
```bash
docker-compose up -d postgres
```

**Option B — Local PostgreSQL:**
```sql
CREATE USER trader WITH PASSWORD 'trader';
CREATE DATABASE trading_db OWNER trader;
```

### Step 6 — Verify installation

```bash
PYTHONPATH=. python tests/run_tests.py
```

Expected output:
```
── STRUCTURE ───────────────────────────────────────────
  ✅  detect_swings returns list
  ✅  BOS: bullish in uptrend
  ...

══════════════════════════════════════════════════════
  TEST RESULTS:  83 passed  |  0 failed  |  83 total
══════════════════════════════════════════════════════
  ✅  All tests passed — system validated!
```

---

## ⚙️ Configuration

### Risk Parameters (`config/risk_config.py`)

| Parameter | Default | Description |
|---|---|---|
| `risk_per_trade_pct` | `1.0` | % of balance risked per trade |
| `min_rr_ratio` | `2.0` | Minimum reward:risk ratio |
| `max_lot_size` | `5.0` | Hard cap on lot size |
| `max_daily_loss_pct` | `3.0` | Daily loss limit as % of balance |
| `max_drawdown_pct` | `8.0` | Max peak-to-trough drawdown |
| `max_daily_trades` | `5` | Max trades per day |
| `max_concurrent_trades` | `2` | Max open positions simultaneously |
| `kill_switch_drawdown_pct` | `6.0` | Auto-halt on this drawdown level |
| `kill_switch_daily_loss_pct` | `2.5` | Auto-halt on daily loss this % |
| `max_allowed_spread_pips` | `30.0` | Reject trade if spread exceeds this |
| `min_signal_score` | `5` | Minimum score out of 10 to proceed |
| `min_ai_confidence` | `0.60` | Minimum AI confidence to execute |

### Trading Parameters (`config/trading_config.py`)

| Parameter | Default | Description |
|---|---|---|
| `primary_htf` | `H4` | Primary bias timeframe |
| `primary_ltf` | `M15` | Primary entry timeframe |
| `swing_lookback` | `20` | Bars each side for swing detection |
| `displacement_min_body_ratio` | `0.6` | Body/range ratio for displacement |
| `displacement_min_pips` | `15.0` | Minimum displacement size |
| `sweep_wick_ratio` | `0.4` | Min wick/range for liquidity sweep |
| `london_session` | `(7, 16)` | UTC hours |
| `ny_session` | `(12, 21)` | UTC hours |
| `min_atr_pips` | `8.0` | Minimum volatility (dead market filter) |
| `max_atr_pips` | `120.0` | Maximum volatility (news filter) |

### AI Configuration (`config/ai_config.py`)

| Parameter | Default | Description |
|---|---|---|
| `confidence_threshold` | `0.60` | Min confidence to TAKE trade |
| `temperature` | `0.0` | Deterministic responses |
| `max_tokens` | `512` | Response length cap |
| `max_retries` | `3` | API retry attempts |
| `enabled_in_backtest` | `False` | Use rule-based fallback in backtest |

---

## 📊 Data Requirements

### Format

The system accepts **CSV** or **Parquet** files with the following structure:

| Column | Type | Description | Example |
|---|---|---|---|
| `time` / `datetime` | datetime | Bar timestamp (UTC) | `2024-01-02 08:00:00` |
| `open` | float | Open price | `2062.45` |
| `high` | float | High price | `2064.20` |
| `low` | float | Low price | `2061.10` |
| `close` | float | Close price | `2063.55` |
| `volume` | float | Tick volume | `1842` |

### Minimum data requirements

| Timeframe | Minimum bars | Recommended | Covers |
|---|---|---|---|
| M15 | 2,000 | 10,000+ | ~1 month / ~6 months |
| M5 | 5,000 | 20,000+ | ~2 weeks / ~2 months |
| H1 | 500 | 2,000 | ~3 weeks / ~3 months |
| H4 | 200 | 500 | ~5 weeks / ~12 weeks |

> For **meaningful backtest results** with 1,000+ trades: provide at least **12 months of M15 data** (~35,000 bars).

### Where to get historical data

**Option 1 — Export directly from MetaTrader 5:**
1. Open MT5 → History Center (`Tools > History Center`)
2. Select `XAUUSD` → `M15`
3. Click `Download` → Export as CSV
4. Place in `data/XAUUSD_M15.csv`

**Option 2 — Python via MT5 API:**
```python
from execution.mt5_client import MT5Client
from data.fetcher import fetch_from_mt5
import pandas as pd

mt5 = MT5Client()
mt5.connect()
df = fetch_from_mt5(mt5, "XAUUSD", "M15", count=50000)
df.to_csv("data/XAUUSD_M15.csv")
```

**Option 3 — Third-party sources:**
- [TrueFX](https://www.truefx.com/) — free tick data
- [Dukascopy](https://www.dukascopy.com/swiss/english/marketwatch/historical/) — free OHLCV
- [HistData](https://www.histdata.com/) — free Forex/Gold historical data

### CSV naming convention
```
data/
├── XAUUSD_M5.csv       # 5-minute bars
├── XAUUSD_M15.csv      # 15-minute bars (primary)
├── XAUUSD_H1.csv       # 1-hour bars
└── XAUUSD_H4.csv       # 4-hour bars
```

---

## 🚀 Running the System

### Backtest Mode

```bash
# Basic backtest with M15 data
python main.py --mode backtest --data data/XAUUSD_M15.csv

# Custom starting balance
python main.py --mode backtest --data data/XAUUSD_M15.csv --balance 50000

# Results saved to:
#   results/trades_YYYYMMDD_HHMMSS.csv
#   results/equity_YYYYMMDD_HHMMSS.csv

### Backtest with Live MT5 Data
You can also run a backtest using the latest 500 bars directly from your MT5 demo account (no CSV required):

```bash
python main.py --mode backtest --mt5
```
```

### Paper Trading Mode

Runs the full live pipeline but sends **simulated orders** (no real MT5 execution):

```bash
python main.py --mode paper
```

### Live Trading Mode

```bash
# Requires Windows + MT5 installed + credentials in .env
python main.py --mode live
```

### Docker (Full Stack)

```bash
# Start PostgreSQL + trading bot
docker-compose up -d

# View live logs
docker-compose logs -f trading_bot

# Run backtest inside Docker
docker-compose run trading_bot python main.py --mode backtest --data data/XAUUSD_M15.csv

# Stop everything
docker-compose down
```

### Switch AI Provider at Runtime

```bash
# Use DeepSeek (default)
AI_PROVIDER=deepseek python main.py --mode live

# Use Claude
AI_PROVIDER=anthropic python main.py --mode live

# Use Gemini
AI_PROVIDER=gemini python main.py --mode live
```

---

## 📈 Backtesting

### Running a backtest

```bash
python main.py --mode backtest --data data/XAUUSD_M15.csv --balance 10000
```

### Sample output

```
══════════════════════════════════════════════════════
  📊 BACKTEST PERFORMANCE REPORT
══════════════════════════════════════════════════════
  Total Trades      : 1,247
  Win Rate          : 54.37%
  Profit Factor     : 1.84
  Total PnL         : $12,450.00
  Max Drawdown      : 4.82%
  Sharpe Ratio      : 1.247
───────────────────────────────────────────────────────
  Avg Win           : $187.50
  Avg Loss          : $94.20
  Avg Hold (hrs)    : 3.2
  Wins / Losses     : 678 / 569

  📅 Monthly P&L:
    2024-01   +$1,240.00
    2024-02   +$980.00
    2024-03   -$320.00
    ...
══════════════════════════════════════════════════════
```

### Backtest output files

| File | Contents |
|---|---|
| `results/trades_*.csv` | Full trade log with entry, exit, PnL, direction, reason |
| `results/equity_*.csv` | Bar-by-bar equity curve for plotting |

### Strategy comparison

```python
from backtest.strategy import ICTStrategy, AdvancedICTStrategy

# Standard ICT
strategy_a = ICTStrategy()

# ICT + FVG/Order Block refinement
strategy_b = AdvancedICTStrategy()
```

---

## 🔴 Live Trading

### Pre-launch checklist

- [ ] Backtest completed with 1,000+ trades
- [ ] Win rate > 50%, Profit Factor > 1.5 on backtest
- [ ] Max drawdown < 6% on backtest
- [ ] `.env` configured with real MT5 credentials
- [ ] PostgreSQL running and `init_db()` called
- [ ] API key for chosen AI provider is valid
- [ ] `risk_per_trade_pct` reviewed and appropriate for account size
- [ ] Kill switch thresholds reviewed
- [ ] Tested in **paper mode** for at least 1 week

### MT5 Account Setup (ICMarkets)

1. Open account at [ICMarkets](https://www.icmarkets.com)
2. Select **Raw Spread** account type
3. Enable **Algorithmic Trading** in MT5:
   `Tools → Options → Expert Advisors → Allow Algorithmic Trading`
4. Add credentials to `.env`

### Monitoring

```bash
# Watch live logs
tail -f logs/trading_bot.log

# PostgreSQL monitoring
psql -U trader -d trading_db -c "SELECT * FROM trades ORDER BY opened_at DESC LIMIT 10;"
psql -U trader -d trading_db -c "SELECT * FROM performance_metrics ORDER BY date DESC LIMIT 5;"

# Check kill switch status (from Python)
from core.risk.kill_switch import kill_switch
print(kill_switch.status())

# Manual kill switch reset (emergency)
kill_switch.reset(authorized_by="admin")
```

---

## 🗄 Database Schema

### `signals` table
| Column | Type | Description |
|---|---|---|
| `signal_id` | varchar | Short unique ID |
| `symbol` | varchar | Trading symbol (XAUUSD) |
| `timestamp` | datetime | Signal generation time |
| `direction` | varchar | buy / sell |
| `entry_price` | float | Planned entry |
| `stop_loss` | float | Structural SL |
| `take_profit` | float | TP2 target |
| `rr_ratio` | float | Reward:risk |
| `score` | int | Signal score (0–10) |
| `score_breakdown` | JSON | Per-component scores |
| `htf_direction` | varchar | HTF bias direction |
| `htf_confidence` | float | HTF bias confidence |
| `ai_confidence` | float | AI evaluation score |
| `ai_decision` | varchar | TAKE / SKIP |
| `ai_reason` | text | AI explanation |

### `trades` table
| Column | Type | Description |
|---|---|---|
| `trade_id` | varchar | Internal trade ID |
| `mt5_ticket` | int | MT5 order ticket |
| `direction` | varchar | buy / sell |
| `lot_size` | float | Position size |
| `entry_price` | float | Actual fill price |
| `stop_loss` | float | SL price |
| `take_profit` | float | TP price |
| `exit_price` | float | Actual close price |
| `pnl` | float | Profit/loss in USD |
| `pnl_pips` | float | Profit/loss in pips |
| `risk_pct` | float | % of balance risked |
| `close_reason` | varchar | tp1/tp2/sl/manual/kill_switch |
| `opened_at` | datetime | Trade open time |
| `closed_at` | datetime | Trade close time |
| `is_open` | bool | Current status |

### `performance_metrics` table
Snapshot saved every cycle: balance, equity, drawdown, daily P&L, win rate.

---

## 📉 Performance Metrics

| Metric | Formula | Target |
|---|---|---|
| **Win Rate** | wins / total_trades × 100 | ≥ 50% |
| **Profit Factor** | gross_profit / gross_loss | ≥ 1.5 |
| **Sharpe Ratio** | mean_return / std_return × √n | ≥ 1.0 |
| **Max Drawdown** | (peak - trough) / peak × 100 | ≤ 8% |
| **Expectancy** | WR × avg_win + (1-WR) × avg_loss | > 0 |
| **Consecutive Losses** | max streak | ≤ 5 |

---

## 🔧 Troubleshooting

### `ModuleNotFoundError: No module named 'MetaTrader5'`
MT5 is Windows-only. On Linux/macOS, use backtest or paper mode:
```bash
python main.py --mode backtest --data data/XAUUSD_M15.csv
```

### `ModuleNotFoundError: No module named 'pytz'`
Already removed from codebase. If it appears in old imports, update `utils/time_utils.py` (already uses stdlib `datetime.timezone`).

### `AI provider not responding`
The system automatically falls back to rule-based scoring. Check your API key:
```bash
echo $DEEPSEEK_API_KEY   # or ANTHROPIC_API_KEY / GEMINI_API_KEY
```

### `DB connection error`
Verify PostgreSQL is running:
```bash
docker-compose ps         # if using Docker
psql -U trader -d trading_db -c "SELECT 1;"
```

### `Kill switch is active — system halted`
```python
from core.risk.kill_switch import kill_switch
print(kill_switch.status())    # see why it triggered
kill_switch.reset(authorized_by="admin")
```

### `Signal score always < 5`
Increase lookback window or lower `min_signal_score` in `risk_config.py`. On synthetic/low-volatility data, signals may be sparse — use real XAUUSD data with proper volatility.

### MT5 order rejected (`retcode != DONE`)
Common causes:
- **10004** — Requote → increase `deviation` parameter
- **10006** — Request rejected → check account permissions
- **10013** — Invalid request → verify lot size and symbol settings
- **10019** — Not enough money → check free margin

---

## 🗺 Roadmap

- [ ] Telegram / Discord alerts for signals and trade events
- [ ] Web dashboard (FastAPI + React) for live monitoring
- [ ] Walk-forward optimization for parameter validation
- [ ] News event calendar integration (auto-blackout before high-impact events)
- [ ] Multi-symbol support (EURUSD, GBPUSD)
- [ ] Portfolio-level risk management across symbols

---

## ⚠️ Disclaimer

> **This software is for educational and research purposes only.**
>
> Trading foreign exchange, CFDs, and commodities (including Gold/XAUUSD) involves substantial risk of loss and is not suitable for all investors. Past performance of any trading system — including backtesting results — is not indicative of future results.
>
> - **Never risk money you cannot afford to lose**
> - **Always test thoroughly in paper/backtest mode before going live**
> - **The authors take no responsibility for financial losses incurred using this software**
> - This system does not constitute financial advice
>
> Ensure algorithmic trading is legal in your jurisdiction before deployment.

---

## 📄 License

MIT License — see [LICENSE](LICENSE) file for details.

---

<div align="center">

Built with ❤️ for the algorithmic trading community

*If this helped you, consider giving it a ⭐*

</div>