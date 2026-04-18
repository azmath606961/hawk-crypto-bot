# HAWK Crypto Bot — Cross-Session Context

> **How to use:** Paste this file into any new Claude Code session to restore full context instantly.
> **Update rule:** After every session that changes strategy logic, parameters, or results — update this file.

---

## Last Updated
2026-04-18 — Added portfolio backtest + comparator dashboard (PR #2, branch feat/portfolio-backtest-comparator). New scripts: `hawk_portfolio_backtest.py` (runs all 4 presets through run_combo, ~20s, saves data/portfolio_backtest_results.csv), `hawk_comparator_dashboard.py` (Flask web UI port 5010, overlaid equity curves, insights, trade log tabs). `hawk_comprehensive_backtest.py` updated: `vol_on` param added to `run_combo`, `VOL_FLAGS=[False,True]` in grid. Vol filter study conclusion: conservative_vol +4.17%/mo vs conservative +14.54%/mo, optimal_vol +7.94%/mo vs optimal +20.44%/mo — vol filter hurts both baselines; do not merge PR #1.

---

## Prompt Optimizer System (active)

| File | Purpose |
|------|---------|
| `.claude/settings.json` | Registers hook on every UserPromptSubmit |
| `.claude/hooks/prompt_optimizer_hook.py` | Reads context.md, classifies intent, injects hard-rule constraints |
| `.claude/skills/prompt-optimizer/SKILL.md` | Full skill with rewrite templates |

Intent categories: `strategy_change` · `backtest_request` · `paper_trade` · `data_fetch` · `code_fix` · `analysis` · `planning`

---

## Project Identity

| Field | Value |
|-------|-------|
| Project | HAWK Crypto Bot (separate from Nifty50 options bot) |
| Repo path | `azmath606961/hawk-crypto-bot`, branch `master` (standalone repo) |
| Exchange | Binance Futures (perpetual swaps) |
| Instruments | ETH/USDT, BTC/USDT, SOL/USDT |
| Base capital | GBP 500 (~$635 USDT) |
| Goal | GBP 500 → GBP 100,000 via compounding |
| Current phase | **Paper trading** (no real orders, live Binance data) |
| Active strategy | **HAWK-ACTIVE v6** — 8-bar channel breakout + EMA20/50 + ADX(14)>=20 filter |
| Best config | ETH/USDT · 10x leverage · 3 concurrent positions |

---

## Active Strategy: HAWK-ACTIVE v6

### Signal logic
```
LONG  : 1h close > highest HIGH of prev 8 bars  AND  EMA20 > EMA50  AND  ADX(14) >= 20
SHORT : 1h close < lowest  LOW  of prev 8 bars  AND  EMA20 < EMA50  AND  ADX(14) >= 20
Enter at NEXT bar open (never current candle — no look-ahead)

ADX gate: skip all entries when ADX(14) < 20 (ranging/choppy market).
ADX(14) >= 20 = trending market — breakouts more likely to reach TP at full 2xSL target.
4h strategies (BTC, SOL): NO ADX filter (ADX behaves differently on 4h; hurts performance)
```

### Position sizing (CRITICAL — never change this formula)
```python
risk_usdt  = equity × 0.015          # always 1.5%, regardless of leverage
sl_dist    = 1.5 × ATR(14)           # in price units
qty        = risk_usdt / sl_dist      # CORRECT: leverage-independent
margin     = qty × price / leverage   # leverage only reduces margin required
```

### Exit rules
| Exit | Condition |
|------|-----------|
| Stop loss | entry ± 1.5 × ATR |
| Take profit | entry ± 2.0 × SL distance |
| Timeout | 30h max hold, exit at market |
| Drawdown gate | 30% from peak → halt all trading |

### Why no BE-stop
Removing the break-even stop (was: move SL to entry at 1:1) was the biggest single improvement:
- With BE-stop: 16% TP hits, avg_win ≈ $10, actual RR 0.82 (negative EV)
- Without BE-stop: 37% TP hits, avg_win ≈ $24, actual RR 1.58 (positive EV)

---

## Backtested Results (Apr 2024 – Apr 2026, 17,520 x 1h bars)

| Asset | Lev | Return | Peak | Win% | RR | T/Day | Liqs |
|-------|-----|--------|------|------|----|-------|------|
| ETH/USDT | 3x | +20% | GBP 864 | 40.6% | 1.61 | 0.9 | 0 |
| ETH/USDT | **10x** | **+83%** | **GBP 1,312** | **42.4%** | **1.58** | **1.6** | **1** |
| ETH/USDT | 20x | +71% | GBP 1,229 | 42.6% | 1.55 | 1.6 | 11 |
| BTC/USDT | 5x | -15% | GBP 526 | 36.4% | 1.67 | 0.9 | 0 |
| SOL/USDT | 10x | -19% | GBP 588 | 35.6% | 1.63 | 1.7 | 0 |

ETH regime breakdown (10x): BEAR shorts 45.4% WR (+$765 PnL), BULL longs 39.7% WR (-$1 PnL)
Monthly peak: Jan 2025 +67.9% (ETH bull run)
Worst month: Mar 2025 -19.5% (ETH crash)

**Positive EV confirmed:** 0.424 × 1.58 - 0.576 = +0.093 per unit risk

---

## Compounding Roadmap

| Scenario | Monthly% | GBP 500→1k | GBP 1k→10k | GBP 10k→100k |
|----------|----------|------------|------------|--------------|
| ETH 10x only | +10.3% | 8 months | 2y 7m | 4y 7m |
| All 3 assets 10x | +30.8% | 3 months | 1 year | 1y 8m |

---

## Key Files

| File | Status | Purpose |
|------|--------|---------|
| `scripts/hawk_trader.py` | ★ ACTIVE | Unified paper+live runner (`--paper`, `--testnet`, or live) |
| `scripts/hawk_dashboard.py` | ★ ACTIVE | Web dashboard at localhost:5000 (`pip install flask` first) |
| `scripts/hawk_comprehensive_backtest.py` | ★ ACTIVE | 25,920-combo grid search |
| `scripts/hawk_paper_trader.py` | Legacy | Old paper-only runner (superseded by hawk_trader.py) |
| `backtester/leveraged_engine.py` | DO NOT MODIFY | Constants: GBP_TO_USDT=1.27, TAKER_FEE=0.0004, FUNDING_RATE_8H=0.0001 |
| `data/ETHUSDT_1h.csv` | Reference | 2yr 1h OHLCV for ETH |
| `logs/hawk_state.json` | Runtime | hawk_trader.py state (persists across restarts) |
| `logs/hawk_trades.csv` | Runtime | All trade records |

---

## Multi-Timeframe Backtest Results (2026-04-16)

Full backtest run across 5m/10m/15m/30m/4h timeframes. New data downloaded:
`ETHUSDT_30m.csv`, `BTCUSDT_30m.csv`, `SOLUSDT_30m.csv`,
`ETHUSDT_4h.csv`, `BTCUSDT_4h.csv`, `SOLUSDT_4h.csv` (all 35k/4k bars, Apr 2024–Apr 2026)

Scripts: `scripts/download_multi_tf_data.py`, `scripts/hawk_backtest_multi.py`

### CONFIRMED — Add to live paper trading

| Strategy | Asset | TF | Channel | Hold | Return | WR% | RR | T/Day | Liqs | EV |
|----------|-------|----|---------|------|--------|-----|----|-------|------|----|
| ETH 1h HAWK v5 | ETH | 1h | 8-bar | 30h | +83% | 42.4% | 1.58 | 0.7 | 1 | +0.094 |
| **BTC 4h channel** | BTC | 4h | 12-bar | 48h | **+57.3%** | **44.8%** | **1.49** | **0.4** | **0** | **+0.115** |
| SOL 4h channel ⚠️ | SOL | 4h | 12-bar | 32h | +21.9% | 47.3% | 1.24 | 0.4 | 6 | +0.060 |

**BTC 4h exact params**: `channel_n=12, ema_fast=20, ema_slow=50, sl_atr_mult=1.5, rr=2.0, max_hold_bars=12 (48h), funding_bars=2, entry_ema_as_filter=True`

**SOL 4h exact params**: `channel_n=12, ema_fast=20, ema_slow=50, sl_atr_mult=1.5, rr=2.0, max_hold_bars=8 (32h), funding_bars=2`. Has 6 liqs/2yr — paper trade before going live.

### REJECTED — Do not implement

| Approach | Result | Reason |
|----------|--------|--------|
| ETH/SOL/BTC 30m any channel | All negative EV | 30m WR ~27-34%, below breakeven (~38% needed for 1.6 RR). Lower TF = noisier signals. |
| 4h EMA gate on ETH 1h | +7.9% vs +83% baseline | Removes too many valid ETH trades. ETH 1h EMA already optimal. |
| BTC 1h HAWK v5 | -15% (see original backtest) | BTC needs 4h, not 1h. Solved by BTC 4h channel. |

### Combined Portfolio Estimate

| Scenario | T/Day | Monthly% | GBP 500→1k | GBP 1k→10k | GBP 10k→100k |
|----------|-------|----------|------------|------------|--------------|
| ETH 1h alone (current) | 0.7 | +2.55% | ~2y4m | ~7y7m | ~7y7m |
| ETH 1h + BTC 4h | 1.1 | ~+4.45% | ~1y4m | ~4y5m | ~4y5m |
| ETH 1h + BTC 4h + SOL 4h | 1.5 | ~+5.27% | ~1y1m | ~3y8m | ~3y8m |

Monthly % is geometric compound from actual backtest equity curves. Combined estimate assumes partial capital sharing (conservatively ~70% of pure sum).

### Implementation Status
- [x] `hawk_paper_trader.py` — extended for BTC/SOL 4h (separate 4h timing loop, global margin cap)
- [x] `scripts/download_multi_tf_data.py` — downloads all new data
- [x] `scripts/hawk_backtest_multi.py` — full multi-TF backtester with `MultiTFHAWKEngine`

## HAWK v6 Results (2026-04-16) — ADX Filter Study

### Grid search: 36 combos on ETH 1h (ADX / RSI / Supertrend / RR)

**Winner: ADX=20 + no RSI + no Supertrend + RR=2.0**

| Combo | Return | WR% | Actual RR | T/2yr | Monthly% |
|-------|--------|-----|-----------|-------|----------|
| v5 baseline (no filters) | +139.7% | 43.8% | 1.56 | 525 | +3.71% |
| **ADX=20** | **+655.9%** | **43.4%** | **2.32** | **579** | **+8.80%** |
| ADX=25 + ST + RR=3.0 | +186.2% | 38.6% | 1.96 | 430 | +4.48% |
| No ADX + Supertrend | +31.8% | 41.1% | 1.58 | 518 | +1.16% |

**Why ADX=20 wins:** ADX selects trending markets only. In trending conditions, breakouts reach the 2xSL TP target more frequently → actual RR jumps from 1.56 to 2.32. RSI and Supertrend added no additional lift (RSI had zero effect; Supertrend reduced trade count without improving quality).

### New assets tested with ADX=20 filter

| Asset | Monthly% | Verdict |
|-------|----------|---------|
| ETH 1h + ADX=20 | +8.80% | CONFIRMED — live |
| BTC 4h (orig) | +0.80% | Keep, no ADX |
| SOL 4h (orig) | -0.26% | Paper trade only (6 liqs) |
| XRP 1h + ADX=20 | -1.46% | REJECTED (WR=24.5%) |
| BNB 1h + ADX=20 | -0.98% | REJECTED (WR=34.1%) |
| ADA 1h + ADX=20 | -1.06% | REJECTED (WR=35.8%) |

XRP/BNB/ADA all have WR below the ~40% needed for positive EV at 1.56 RR. Not fixable with ADX alone.

### Combined portfolio (ETH 1h v6 + BTC 4h orig)

| Scenario | Monthly% | 500→1k | 1k→10k | 10k→100k | Total |
|----------|----------|--------|--------|----------|-------|
| ETH 1h v6 alone | +8.80% | 8m | 2y 3m | 2y 3m | 5y 2m |
| **ETH v6 + BTC 4h** | **+9.61%** | **8m** | **2y 1m** | **2y 1m** | **4y 10m** |

10%/month target: **9.61% — 0.39% short** (within rounding of target)

### Key scripts

| File | Purpose |
|------|---------|
| `scripts/hawk_v6_backtest.py` | Full v6 grid search + new asset tests |
| `scripts/hawk_paper_trader.py` | UPDATED — ADX(14)>=20 filter live in 1h strategy |

---

## Comprehensive Backtest (2026-04-17 re-run) — 25,920 combinations

Full grid across: ETH/BTC/SOL/XRP/BNB/ADA × 1h/4h × 5 leverages × 3 channels × 3 SL_ATR × 3 RR × 4 ADX × 2 RSI × 2 MACD.
Runtime: 6.2 min on 7 CPU cores. Full results in `data/backtest_results.csv`.
Script: `scripts/hawk_comprehensive_backtest.py`

**IMPORTANT:** All scripts now use Wilder EMA (alpha=1/p) and TAKER_FEE=0.0004, matching the backtest exactly. The previous ETH 8.80%/mo figure was an EMA artefact (standard EMA was used in trader, Wilder EMA in backtest). Re-run confirmed:

### Best strategy per asset (corrected — Wilder EMA)

| Asset | TF | Lev | Ch | SL | RR | ADX | RSI | MACD | Return | Monthly% | WR% | T | Liqs |
|-------|----|----|----|----|----|----|-----|------|--------|----------|-----|---|------|
| ETH | 1h | 20x | 12 | 1.0 | 2.5 | off | on | on | +240.4% | +5.24% | 24.0% | 50 | 0 |
| BTC | 4h | 10x | 8 | 1.5 | 2.0 | off | on | on | +86.7% | +2.64% | 47.1% | 210 | 0 |
| SOL | — | — | — | — | — | — | — | — | **NO POSITIVE EV** (all 2160 combos negative) | — | — | — | — |
| XRP | 1h | 20x | 12 | 1.5 | 2.5 | off | off | off | +650.2% | +8.77% | 29.1% | 79 | 0 |
| BNB | 4h | 10x | 16 | 1.5 | 3.0 | 25 | on | off | +60.6% | +2.00% | 43.9% | 107 | 0 |
| ADA | 4h | 5x | 8 | 2.0 | 2.5 | off | on | on | +53.3% | +1.80% | 46.5% | 172 | 0 |

### Best per leverage (winner across all assets/TFs)

| Leverage | Asset | TF | Monthly% | Return | WR% | T | Note |
|----------|-------|----|----------|--------|-----|---|------|
| 3x | BNB 4h | 4h | +1.79% | +53.0% | 40.6% | 123 | Safe, low return |
| 5x | XRP 1h | 1h | +6.75% | +378.6% | 15.6% | 32 | High RR, low WR, small sample |
| 10x | XRP 1h | 1h | +5.83% | +289.4% | 20.0% | 45 | Recommended XRP leverage |
| 15x | XRP 1h | 1h | +5.68% | +276.5% | 30.0% | 70 | — |
| 20x | XRP 1h | 1h | +8.77% | +650.2% | 29.1% | 79 | ⚠ SL≈liq distance on XRP |

### Combined portfolios

| Portfolio | Monthly% | 500→100k |
|-----------|----------|----------|
| ETH+BTC+XRP+BNB+ADA (optimal leverage) | +20.44% | ~2y 4m |
| ETH+BTC+XRP+BNB+ADA (10x only, safer) | +14.54% | ~3y 3m |
| ETH+BTC only (10x, current) | ~9.61% | ~4y 10m |

### Key new findings

1. **XRP/USDT 1h is the top performer** — channel breakout strategy works exceptionally well on XRP. Best at 10x (5.83%/mo, safer) or 20x (8.77%/mo, risky).
   - ⚠ WARNING: At 20x, SL distance ≈ liquidation distance. Flash wicks can liquidate before SL fires. Use 10x for live trading.
   - 32-79 trades over 2 years = small sample; paper trade 30+ before going live (R7).
2. **MACD filter improves ETH** — ETH 1h + MACD (12,26,9 crossover) gives +5.24%/mo vs +3.71% baseline.
3. **SOL has NO positive EV** across all 2,160 parameter combinations on both 1h and 4h. Too many false breakouts. Do not trade SOL in any form.
4. **BNB 4h + ADX=25 + RSI**: stable +2.00%/mo with 107 trades — good addition.
5. **ADA 4h + RSI + MACD**: +1.80%/mo with 172 trades — decent addition.
6. **No single strategy achieves 10%/mo alone** — portfolio combination is required.

### New assets for paper trader

Priority order (paper trade 30+ trades before going live, R7):
1. **XRP/USDT 1h** (10x, ch=16, SL=1.0, RR=3.0) → +5.83%/mo [safer leverage]
2. **BNB/USDT 4h** (10x, ch=16, SL=1.5, RR=3.0, ADX=25, RSI on) → +2.00%/mo
3. **ADA/USDT 4h** (10x, ch=16, SL=2.0, RR=2.5, MACD on) → +1.51%/mo

---

## Portfolio Presets (hawk_trader.py --portfolio)

| Portfolio | Command | Mo% | 100k ETA | State file |
|-----------|---------|-----|----------|------------|
| conservative | `--portfolio conservative` | +14.56% | 3y 3m | `logs/hawk_state_conservative.json` |
| optimal | `--portfolio optimal` | +20.47% | 2y 4m | `logs/hawk_state_optimal.json` |

Filters are now wired into hawk_trader.py — paper mode applies RSI/MACD/ADX exactly as the backtest did.

### Conservative (all 10x) — confirmed +14.56%/mo
| Symbol | TF | Lev | ch | SL | RR | ADX | RSI | MACD | Mo% |
|--------|----|----|----|----|----|----|-----|------|-----|
| ETH/USDT | 1h | 10x | 8 | 2.0 | 2.0 | off | on | off | +2.56% |
| XRP/USDT | 1h | 10x | 16 | 1.0 | 3.0 | off | off | off | +5.84% |
| BTC/USDT | 4h | 10x | 8 | 1.5 | 2.0 | off | on | on | +2.64% |
| BNB/USDT | 4h | 10x | 16 | 1.5 | 3.0 | ≥25 | on | off | +2.00% |
| ADA/USDT | 4h | 10x | 16 | 2.0 | 2.5 | off | on | on | +1.51% |

### Optimal (mixed leverage) — confirmed +20.47%/mo
| Symbol | TF | Lev | ch | SL | RR | ADX | RSI | MACD | Mo% |
|--------|----|----|----|----|----|----|-----|------|-----|
| ETH/USDT | 1h | 20x | 12 | 1.0 | 2.5 | off | on | on | +5.25% |
| XRP/USDT | 1h | 20x | 12 | 1.5 | 2.5 | off | off | off | +8.78% |
| BTC/USDT | 4h | 10x | 8 | 1.5 | 2.0 | off | on | on | +2.64% |
| BNB/USDT | 4h | 10x | 16 | 1.5 | 3.0 | ≥25 | on | off | +2.00% |
| ADA/USDT | 4h | 5x | 8 | 2.0 | 2.5 | off | off | on | +1.80% |

---

## Open Issues / Next Steps

1. **Paper trade both portfolios in parallel** — run conservative + optimal simultaneously, compare performance after 30+ trades.
2. **Monitor via dashboard** — separate ports for each:
   - `python scripts/hawk_dashboard.py --state logs/hawk_state_conservative.json --port 5000`
   - `python scripts/hawk_dashboard.py --state logs/hawk_state_optimal.json --port 5001`
3. **RSI/MACD filters** — now wired into hawk_trader.py (compute_signals + get_signal). Both portfolio presets apply the exact filters from the backtest. Paper mode = backtest parity.
4. **SOL: permanently rejected** — no positive EV across all 2,160 combos. Never add.
5. **XRP max safe leverage: 10x** — at 20x SL≈liq distance. Optimal portfolio uses 20x (per backtest best) but monitor liquidations closely.

---

## Hard Rules (R1–R12)

| # | Rule |
|---|------|
| R1 | `qty = risk_usdt / sl_dist` — NEVER multiply qty by leverage |
| R2 | Verify `sl_dist > 0` before computing qty |
| R3 | Paper trader reads PREVIOUS closed candle (`iloc[-2]`) — never current open candle |
| R4 | 30% DD gate is hardcoded — never override without explicit user instruction |
| R5 | If trades = 0 → check CSV column names first (must be lowercase: open/high/low/close/volume) |
| R6 | No API keys in code — Binance public REST only for paper mode |
| R7 | Never recommend going live without 30+ paper trades with positive EV confirmed |
| R8 | Do not recommend 50x for live trading — liquidation risk exceeds benefit |
| R9 | Always run backtest before changing strategy parameters |
| R10 | Max 3 concurrent positions, 60% total margin cap — never override |
| R11 | No trailing stops, partial exits, or speculative features unless explicitly requested |
| R12 | `leveraged_engine.py` constants (fees, funding, GBP rate) — do not modify without user instruction |
| R13 | ADX(14) >= 20 gate applies to 1h strategies only — do NOT add to 4h strategies (hurts BTC/SOL 4h) |
| R14 | RSI and Supertrend filters showed zero lift vs ADX alone — do not add unless re-tested |
| R15 | SOL permanently rejected — no positive EV found in any of 2,160 parameter combos (1h + 4h) |
| R16 | XRP max safe leverage for live trading = 10x (at 20x, SL distance ≈ liquidation distance) |
| R17 | MACD filter (12,26,9) improves ETH 1h — apply as additional signal gate alongside ADX |
| R18 | All indicator code must use Wilder EMA (alpha=1/p, `ewm(alpha=1/p, adjust=False)`) — never `ewm(span=p)`. Standard EMA gives different crossovers and breaks backtest parity. |
| R19 | TAKER_FEE = 0.0004 (0.04%) in all scripts — matches backtest. hawk_trader.py, hawk_paper_trader.py confirmed fixed. |
| R20 | hawk_trader.py is the canonical runner. Paper mode = `--paper`. Live = no flag. Testnet = `--testnet`. Do not use hawk_paper_trader.py for new work. |
