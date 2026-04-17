# Changelog

All notable logic & behavior changes to the trade-bot are documented here.
Format: date, files touched, reason, observable impact.

---

## 2026-04-16 — Timezone Fix + SL+ Delay + BB/Volume + Retrace Entry

### 29. Critical: UTC timezone fix for daily PnL and state persistence
- **Files:** `execution/mt5_client.py`, `database/state_repo.py`, `core/risk/portfolio.py`, `telegram/bot.py`
- **Why:** `get_daily_realized_pnl()` and `get_daily_deals_today()` used `datetime.now()` (local WIB = UTC+7) but MT5 API interprets naive datetimes as UTC. State repo used `date.today()` (local) wrapped as UTC. Result: yesterday's PnL leaked into today, cutloss state loaded from wrong day.
- **Fix:** All MT5 deal queries now use `datetime.now(timezone.utc)`. State repo date key uses `datetime.now(timezone.utc).date()`. Portfolio `_day_start_date` uses UTC.
- **Impact:** Daily PnL is now accurate. Hard cutloss state correctly loads/resets per UTC day.

### 30. SL+ delay — prevent premature breakeven
- **Files:** `execution/order_manager.py`, `config/trading_config.py`
- **Why:** REV signals had SL+ (micro-lock / BE) activate within seconds of entry. Price briefly spiked in favor, SL moved to breakeven, then normal retrace hit the tight SL+ and closed the trade — right before price moved to TP.
- **Fix:** New `sl_plus_delay_sec` config per mode (AGG=90s, V_AGG=60s, ULTRA=45s, CONS/MOD=0). Both micro-lock and BE blocks check `trade age >= sl_plus_delay` before moving SL.
- **Impact:** Trades have breathing room to establish direction before SL+ kicks in.

### 31. Bollinger Band confluence scoring
- **Files:** `core/signal/advanced_signal_engine.py`, `core/signal/reversal_engine.py`
- **Why:** No BB confirmation existed. Entries without BB confluence had lower probability.
- **Fix:** M5 BB(20,2) in SMC engine: price at/outside band +2 score, below/above mid +1. Reversal engine: BB bonus +2 when price outside band in reversal direction.
- **Impact:** Signals with BB confluence get higher scores, improving entry quality.

### 32. Enhanced volume confirmation for entry timing
- **Files:** `core/signal/advanced_signal_engine.py`, `core/signal/reversal_engine.py`
- **Why:** Only a 3x volume burst gave +1. No M5 rising volume check. Reversal engine had no volume confirmation.
- **Fix:** SMC: M1 vol 3x→+2 (was +1), 1.5x→+1 (new). M5 rising volume (3-bar avg > 1.3x 20-bar avg)→+1. Reversal: M5 vol spike 2x→+2, 1.3x→+1.
- **Impact:** Better entry timing — volume confirms institutional participation.

### 33. Reversal retrace-entry filter
- **File:** `core/signal/reversal_engine.py`
- **Why:** REV signals entered immediately on signal generation. For SELL, price hadn't retraced up yet → entered at a worse price. Same for BUY without a dip.
- **Fix:** Before returning a REV signal, check that the last M1 candle shows a counter-move (SELL needs bullish candle, BUY needs bearish candle). Also reject if retrace volume > 1.5x avg (could be new impulse, not a pullback).
- **Impact:** REV entries happen at better prices after a small counter-retrace. Avoids chasing.

### 34. MarkdownV2 fix in entry notification
- **File:** `telegram/formatter.py`
- **Why:** `{risk_pct:.2f}%` outside backticks had unescaped `.` → Telegram 400 on every entry.
- **Fix:** Moved risk_pct inside backtick code span. Also fixed daily summary PnL line.
- **Impact:** Entry and daily summary notifications render first try.

### 36. Multi-TF Candlestick Pattern Recognition
- **Files:** `core/signal/candle_patterns.py` (NEW), `core/signal/advanced_signal_engine.py`, `core/signal/reversal_engine.py`
- **Why:** No candlestick pattern analysis existed. Bot relied purely on structural zones and indicators without reading candle formations.
- **Fix:** New `candle_patterns.py` module detects 12 patterns: Engulfing (bull/bear), Hammer, Shooting Star, Morning/Evening Star, Harami (bull/bear), Tweezer Top/Bottom, Three White Soldiers/Black Crows, Marubozu. SMC engine scans M1/M5/M15/H1 (capped per TF, max +5 score). Reversal engine scans M1/M5/H1 (max +3). Opposing patterns penalize or reject signals.
- **Impact:** Higher-TF engulfing/star patterns (H1, M15) provide strong confirmation; M1 patterns refine timing.

### 35. Post-TP re-entry (continuation or flip)
- **Files:** `execution/order_manager.py`, `main.py`
- **Why:** After a trade hits TP, price often continues or reverses immediately. Previously the bot sat idle waiting for the next signal cycle (~3s scan). User requested instant re-entry capability.
- **Fix:** When TP2 hits, `_pending_tp_reentry` is queued with the closed trade context. Main loop calls `consume_tp_reentry()` each cycle: if price moved >0.3R past TP in either direction within 120s, generates a re-entry signal (continuation = same dir, flip = opposite). HTF bias veto prevents counter-trend entries. Signal goes through normal risk evaluation before execution.
- **Impact:** After TP hit, bot can immediately re-enter if price shows clear continuation or reversal. No idle gap.

---

## 2026-04-16 — Micro-Account Unblock + Compound + 100% WR Profit-Lock

### 24. Micro-account ($<500) margin requirements relaxed
- **File:** `core/risk/executor.py`
- **Why:** At $280, margin level is ~150-300%. Session checks required 1000% (NY/LONDON) and 500% (others) → all entries rejected. Pulse required 300%.
- **Fix:** When `balance < 500` (micro-account): session margin → 150%, Pulse margin → 150%. Normal accounts unchanged.
- **Impact:** $280 account can now execute 0.01 lots during any session.

### 25. Equity-aware cutloss threshold
- **File:** `execution/order_manager.py`
- **Why:** At $280, a single $14 loss = 5% → triggers hard cutloss halt for entire day. Too sensitive for micro-accounts.
- **Fix:** `limit = max(5%, min(10%, 3000/equity))`. At $280 → 10% ($28 drawdown before halt); at $600 → 5%; at $1000+ → 5%.
- **Impact:** Micro-accounts get more breathing room before hard halt.

### 28. Critical: Hard cutloss re-trigger survives bot restart after /reset
- **File:** `main.py`
- **Why:** Even after `/reset` saved `daily_cutloss_triggered=False` to DB, restarting the bot created a fresh Portfolio with `_cutloss_pnl_offset=0`. MT5 still reported the old daily loss → immediately re-triggered cutloss.
- **Fix:** On startup, when DB state shows `cutloss=False` (was reset by user), automatically set `_cutloss_pnl_offset = daily_realized` so pre-reset losses are excluded from the threshold calculation.
- **Impact:** After `/reset` + restart, bot truly resumes. Old losses don't count toward new cutloss threshold.

### 27. Critical: Hard cutloss re-trigger after /reset (bug fix)
- **Files:** `core/risk/portfolio.py`, `telegram/commands.py`
- **Why:** After `/reset` cleared `daily_cutloss_triggered`, the next cycle (~3s) re-calculated `realized_daily_loss_pct` from the same MT5 history → immediately re-triggered cutloss. Reset was completely useless.
- **Fix:** Added `_cutloss_pnl_offset` field to Portfolio. On `/reset`, `reset_cutloss()` now: (a) offsets PnL so pre-reset losses excluded from calculation, (b) resets `peak_equity` to current equity so `drawdown_pct` starts fresh, (c) resets `_day_start_balance_val` to current balance. `realized_daily_loss_pct` now subtracts offset. Reset via "Reset All" button also calls `reset_cutloss()`.
- **Impact:** After `/reset`, bot genuinely resumes trading. Only NEW losses post-reset can re-trigger cutloss.

### 26. SMC rejection logging
- **File:** `main.py`
- **Why:** When entries are blocked, no log showed WHY. User had no visibility.
- **Fix:** Log `🚫 SMC REJECTED: {reason}` with signal ID and score when risk_executor rejects.
- **Impact:** Visible in logs exactly why each signal was rejected (margin, cap, cutloss, etc.).

---

## 2026-04-16 — Compound System + 100% WR Profit-Lock + Thin-Retrace + /planning

### 20. Critical: BE buffer too small vs XAU spread
- **Files:** `execution/order_manager.py`
- **Why:** BE buffer was `0.08` points (~0.8 pips) but XAU spread = 2-5 pips. Trades hitting "breakeven" actually closed at a loss because the buffer didn't cover spread. Same issue for micro-lock buffer (0.5 pips → 0.05 pts) and partial-close BE (0.01 pts). This was the #1 cause of "losses despite correct direction".
- **Fix:** All three buffers now use `max(0.40-0.50 pts, micro_lock_buffer × 0.1)` — always covers spread. BE buffer also scales with mode's micro_lock_buffer_pips.
- **Impact:** "Breakeven" trades now actually close at breakeven+profit. Critical for 100% WR.

### 21. Mode-specific compound effect tuning (AGG+ modes)
- **Files:** `config/trading_config.py`, `core/risk/executor.py`
- **Why:** Compound lot scaling only existed for ULTRA pulse trades. No equity growth scaling for standard/reversal/flip signals.
- **Fix:**
  - `pulse_compound: True` enabled for AGG, VERY_AGG, ULTRA
  - Compound scaling for ALL signals in AGG+ modes: `factor = min(2.0, 1 + (equity - 300) / 1000)`. At $800 equity → 1.5× lots; at $1300 → 2× lots.
  - Still capped by total portfolio cap (staircase) so risk is bounded.
- **Impact:** Equity growth accelerates as balance compounds. Natural reinvestment.

### 22. 100% WR profit-lock tuning (AGG+ modes)
- **File:** `config/trading_config.py`
- **Changes per mode:**
  | Mode | be_threshold_r | micro_lock_r | trailing_pips | micro_buf (pips) |
  |------|---------------|-------------|--------------|-----------------|
  | AGG | 0.8→0.5 | 0.20→0.12 | 25→18 | 1.0→5.0 |
  | V_AGG | 0.7→0.4 | 0.15→0.08 | 20→12 | 0.8→5.0 |
  | ULTRA | 0.5→0.3 | 0.10→0.05 | 15→8 | 0.5→5.0 |
- ULTRA now has `partial_close: True` + `auto_be: True` (were False).
- **Impact:** Micro-lock fires in 1-3 pips of profit; BE fires at 0.3-0.5R; trailing is very tight. Almost impossible for a profitable trade to reverse to loss.

### 23. Momentum-fade quick exit (100% WR guard)
- **File:** `execution/order_manager.py`
- **Why:** Trades reaching +0.5R profit but fading back to +0.15R would eventually hit SL at loss. Winning trades became losers.
- **Fix:** Track peak profit R per trade. When `peak_r >= 0.5R` AND current profit fades to `<= 0.15R` (but still positive), close immediately. Enabled via `momentum_fade_exit: True` config (AGG/V_AGG/ULTRA).
- **Impact:** Guarantees that any trade that ever saw significant profit closes positive. Profit may be small, but loss is prevented.

---

## 2026-04-16 — Thin-Retrace Sizing + HTF Trend-Lock + /planning + Volume Filter

### 14. Thin-retrace tier (valid-but-small entry)
- **Files:** `core/signal/signal_engine.py`, `core/signal/advanced_signal_engine.py`, `main.py`
- **Why:** Late-entry problem — price overshoots OB then retraces, but engine waits for full zone tag that often never comes.
- **Fix:** New `THIN_RETRACE` tier fires when price is within `ATR_M1 × 1.5` of zone boundary AND last 2 M1 closes are moving toward the zone. Adds `lot_multiplier` + `entry_tier` fields to `TradeSignal`. Half-size (×0.5) entry. **TP capped at RR 1.5** (close target — tight-profit so SL doesn't catch first). Volume floor: rejects if last M1 tick_volume < 0.8× 20-bar avg.
- **Impact:** Catches setups previously missed; small-but-frequent profit instead of "waiting forever or eating SL on far TP".

### 15. HTF Trend-lock guard
- **File:** `core/signal/advanced_signal_engine.py`
- **Why:** User reported counter-trend SMC signals (e.g. SELL inside strong bull impulse) hitting SL despite "correct direction" elsewhere.
- **Fix:** When H4 AND H1 share the same bias AND `max(H4.adx, H1.adx) ≥ 22`, block opposite-direction signals from the SMC engine. Counter-trend trades must come through `reversal_engine` (which has structural confirmation).
- **Impact:** Eliminates "selling into the bull" / "buying into bear" cutloss bleed.

### 16. Telegram chart caption MarkdownV2 fix
- **File:** `execution/order_manager.py`
- **Why:** Repeated `Bad Request: can't parse entities: Character '.' is reserved` warnings on every chart send.
- **Fix:** Switched chart caption to `parse_mode=""` (plain text). No more 400 fallbacks.
- **Impact:** Cleaner logs; chart captions render reliably.

### 18. Preemptive floating equity cutloss
- **Files:** `execution/order_manager.py`, `main.py`
- **Why:** Bot observed realized daily loss of **13.69%** before the 5% hard cutloss fired — because check ran only on realized P&L. Floating losses compounded until all trades closed at once.
- **Fix:** `check_hard_cutloss` now triggers on EITHER `realized_daily_loss_pct >= limit` OR `drawdown_pct >= limit` (equity from peak). Floating bleed is caught before full blow-up.
- **Impact:** Stops loss near the 5% mark regardless of whether trades have closed yet.

### 19. Cutloss notification Telegram fix
- **File:** `main.py`
- **Why:** `{realized_daily_loss_pct:.1f}%` produces `13.7%` — the `.` triggered MarkdownV2 400 every cutloss event.
- **Fix:** Send cutloss notification as plain-text (`parse_mode=""`). Message also reports equity drawdown alongside realized loss.
- **Impact:** Critical alert renders first try.

### 17. `/planning` Telegram command
- **File:** `telegram/commands.py`
- **Why:** User wanted to preview the planning area (entry/SL/TP/Fibo extensions) and an estimated win-rate before/while a setup is live.
- **Fix:** Loads the most-recent `SignalModel` from DB, computes Fibo 127.2 / 161.8 / 200% extensions, and estimates win-rate with a heuristic: `base_WR(RR) + score_bonus + HTF_bonus`, banded LOW (<60%) / OK (60-80%) / HIGH (≥80%). Registered in setMyCommands autocomplete.
- **Impact:** One command (`/planning`) shows full entry plan + accuracy tier without needing log access.

---

## 2026-04-15 — Fibo RR Integration + Flip/Reversal Re-optimization

### 1. ICT → SMC naming consistency
- **Files:** `backtest/strategy.py`, `telegram/commands.py`, `telegram/formatter.py`, `main.py`
- **Why:** User requested consistent terminology — drop "ICT" label in favor of "SMC" since the two overlap.
- **Impact:** Class `ICTStrategy` → `SMCStrategy`, `AdvancedICTStrategy` → `AdvancedSMCStrategy`. Telegram labels now show "🛡️ SMC Standard" and "SMC + Alpha Filter".

### 2. FiboEngine TP ladder bug fix
- **File:** `core/signal/fibo_engine.py`
- **Why:** TP1 at Fibo 38.2% gave RR **0.61** — partial close locked LESS than risk.
- **Fix:** TP1 = swing_high (100%) → RR ≈ 1.60; TP2 = 127.2% ext → RR ≈ 2.31; TP3 = 161.8% ext → RR ≈ 3.21.
- **Impact:** Fibo setups now have meaningful partial-close outcomes. Average RR on completed Fibo trades should rise ~40-60%.

### 3. Fibo TP in AdvancedSignalEngine (main SMC engine)
- **File:** `core/signal/advanced_signal_engine.py`
- **Why:** Previously used arbitrary session-RR multiplier (ASIA 10×, LONDON 4×, NY 2.5×) — not structurally anchored.
- **Fix:** Derive TP from H1 (preferred) or M15 swing → 127.2% extension, auto-promote to 161.8% if price already past 127.2%. Fallback to session-RR only if Fibo RR < 1.5.
- **Impact:** TPs land on natural resistance/support levels. Fewer "TP overshoot" cases in LONDON/NY.

### 4. Fibo TP in PulseEngine
- **File:** `core/signal/pulse_engine.py`
- **Why:** Pulse used fixed 1.5×/2.5× session multiplier.
- **Fix:** Derive TP from M5 swing 127.2%/161.8% ext, guard RR ∈ [1.2, 3.5] (Pulse must stay conservative).
- **Impact:** Pulse scalp targets align with recent M5 structure.

### 5. MultiTF Golden Pocket detection
- **File:** `core/structure/multi_tf_analyzer.py`
- **Why:** Multi-TF scoring missed a key SMC confluence — price in 61.8-78.6% retracement zone.
- **Fix:** Added `fibo_golden`/`fibo_retrace_pct` fields to `TFTrend`, bonus = weight × 0.6 when aligned with bias. Summary shows `φ` indicator per TF.
- **Impact:** Setups in golden pocket on multiple TFs get higher confluence score.

### 6. Telegram entry message detail
- **Files:** `telegram/formatter.py`, `telegram/bot.py`
- **Why:** User needed to trace WHY each trade fired.
- **Fix:** `fmt_trade_entry` now shows: signal type (FIBO/PULSE/REV/SMC), TP source (e.g. "H1 127.2%"), SL source, multi-TF summary string, HTF bias, score breakdown.
- **Impact:** Every entry notification is self-documenting.

### 7. `/config` shows live config
- **File:** `telegram/commands.py`
- **Why:** Previous `/config` only showed DB-overridable fields, not the full live state.
- **Fix:** Now dumps mode settings (risk%, min_score, partial_close, be_threshold_r, trailing_pips, pulse_scalping), profit-lock config, global risk limits, feature flags, score weights, AND DB overrides. Auto-splits if > 3900 chars.
- **Impact:** `/config` is now the single source of truth for "what is the bot running with right now?"

### 8. HTF-flip signal: Fibo TP + structural SL
- **File:** `main.py`
- **Why:** Flip signal used ATR×2.0 SL + fixed RR 2.0 — arbitrary and often mis-aligned.
- **Fix:** SL from M5 nearest swing extreme (fallback ATR×2); TP from H1/M15 127.2%/161.8% ext with RR guard [1.5, 5.0].
- **Impact:** Flip entries now have structural anchoring. Better odds when HTF bias genuinely flipped.

### 9. Flip skip diagnostic logging
- **File:** `execution/order_manager.py`
- **Why:** User reported "flip signal not firing" — no visibility into why.
- **Fix:** When `check_structural_contra_exit` skips because `htf_bias` is NEUTRAL/None, log once per trade at DEBUG level.
- **Impact:** Grep logs for "Contra-flip skip" to diagnose when flip doesn't fire. Common cause: HTF bias still NEUTRAL.

### 10-13. Reversal engine re-optimization (4 strategies)
- **File:** `core/signal/reversal_engine.py`
- **Why:** User requested "reversal signals need re-optimization". Session confirm counts were over-filtering; TPs were pure RR multiplier.
- **Fix:**
  - **Session params re-tuned:** OVERLAP 4→3 confirm, NY 3→2, ASIA 2→1; fade_thresh lowered 1-2 per session.
  - **Impulse-Retest (Strategy 1):** TP = impulse_high/low + 127.2% ext (fallback RR).
  - **Exhaustion Recovery (Strategy 2):** TP = M5 swing 127.2%/161.8% ext, guard RR ∈ [1.5, 4.0].
  - **Consecutive Fade (Strategy 3):** TP = streak range 127.2% ext, guard RR ∈ [1.2, 3.5].
  - **MACD Hist Accel (Strategy 4):** TP = M5 swing 127.2%/161.8% ext, guard RR ∈ [1.3, 3.5].
- **Impact:** Reversal fires more often (looser confirms) with Fibo-anchored targets.

---

## Earlier Changes (pre-2026-04-15)

See git log for history prior to this changelog being established.
Key inflection points:

- **Portfolio cap staircase:** Executor excludes SL+/micro_locked trades from `at_risk_lots`, freeing cap for next batch once prior batch is safe. (`core/risk/executor.py`)
- **MT5 broker volume normalization:** Volume normalized to `volume_min/max/step`, rejects lot_size ≤ 0 to prevent portfolio-cap bypass. (`execution/mt5_client.py`)
- **Micro-profit-lock ratchet:** When profit ≥ `micro_profit_lock_r`, SL jumps to `entry + micro_lock_buffer_pips`. Staircase equity curve enabled. (`execution/order_manager.py`)
- **Mode-scaled anti-revenge cooldown:** ULTRA/VERY_AGG 3 min, AGG 10 min, MOD 20 min, CONS 30 min. Telegram `/reset` clears state AND entry-delay. (`core/risk/filters.py`)
- **News sentiment integration:** Go scraper → Redis → `get_dominant_sentiment()` → signal scoring bonus/penalty + adverse-news quick-harvest exit.
