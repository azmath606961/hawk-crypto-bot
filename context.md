# HAWK Crypto Bot — Cross-Session Context

> **How to use:** Paste this file into any new Claude Code session to restore full context instantly.
> **Update rule:** After every session that changes strategy logic, parameters, or results — update this file.

---

## Last Updated
2026-04-16 — Moved to standalone repo `azmath606961/hawk-crypto-bot` (master branch); README rewritten with .venv setup for Windows + Ubuntu, paper trading explanation, no-account-needed section

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
| Active strategy | **HAWK-ACTIVE v5** — 8-bar channel breakout + EMA20/50 filter |
| Best config | ETH/USDT · 10x leverage · 3 concurrent positions |

---

## Active Strategy: HAWK-ACTIVE v5

### Signal logic
```
LONG  : 1h close > highest HIGH of prev 8 bars  AND  EMA20 > EMA50
SHORT : 1h close < lowest  LOW  of prev 8 bars  AND  EMA20 < EMA50
Enter at NEXT bar open (never current candle — no look-ahead)
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
| `scripts/hawk_paper_trader.py` | ★ ACTIVE | Live paper trader — run this |
| `scripts/hawk_backtest.py` | ★ ACTIVE | Full 2yr backtest engine |
| `backtester/leveraged_engine.py` | DO NOT MODIFY | Constants: GBP_TO_USDT=1.27, TAKER_FEE=0.0005, FUNDING_RATE_8H=0.0001 |
| `data/ETHUSDT_1h.csv` | Reference | 2yr 1h OHLCV for ETH |
| `logs/hawk_paper_state.json` | Runtime | Paper trader state (persists across restarts) |
| `logs/hawk_paper_trades.csv` | Runtime | All paper trade records |

---

## Open Issues / Next Steps

1. **BTC and SOL still unprofitable** — strategy parameters optimised for ETH volatility profile; BTC/SOL need separate ATR mult or channel_n tuning
2. **50x liquidation problem** — at 50x, liq distance = 1.5%; flash wicks bypass SL and liquidate whole position. Max safe leverage ≈ 20x
3. **BTC/SOL still unprofitable** — strategy parameters optimised for ETH volatility profile; BTC/SOL need separate ATR mult or channel_n tuning (unchanged)

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
