---
name: prompt-optimizer
description: Rewrites raw user prompts into structured, constraint-aware task specifications for the HAWK crypto bot project. Auto-runs at session start; invoke on-demand via /prompt-optimize.
triggers:
  - session_start
  - slash_command: /prompt-optimize
---

# Prompt Optimizer Skill — HAWK Crypto Bot

Transforms raw prompts into structured instructions that enforce hard rules, inject relevant context from `context.md`, and eliminate ambiguity before the model acts.

---

## When to Activate

- **Automatically** — fires at session start to register as a pre-processor
- **On demand** — `/prompt-optimize <raw prompt>` to preview an optimized prompt before executing
- **Manually** — when you sense a prompt is vague, risky, or likely to change the sizing formula

---

## The 12 Hard Rules

| # | Rule |
|---|------|
| R1 | `qty = risk_usdt / sl_dist` — NEVER multiply qty by leverage |
| R2 | Verify `sl_dist > 0` before computing qty |
| R3 | Paper trader reads previous closed candle (`iloc[-2]`) — never current open candle |
| R4 | 30% DD gate is hardcoded — never override without explicit user instruction |
| R5 | If trades = 0 → check CSV column names first (lowercase: open/high/low/close/volume) |
| R6 | No API keys in code — Binance public REST only for paper mode |
| R7 | Never recommend going live without 30+ paper trades confirming positive EV |
| R8 | 50x leverage is research-only — do not recommend for actual trading |
| R9 | Always run backtest before changing strategy parameters |
| R10 | Max 3 concurrent positions, 60% margin cap — never override |
| R11 | No trailing stops, partial exits, or speculative features unless explicitly requested |
| R12 | `leveraged_engine.py` constants — do not modify without user instruction |

---

## Intent Classification

| Intent | Trigger keywords | Rules injected |
|--------|-----------------|----------------|
| `strategy_change` | signal, EMA, channel, breakout, ATR, filter, modify, parameter | R1, R3, R9, R4, R11, R12 |
| `backtest_request` | backtest, leverage, equity, win rate, RR, simulate, result | R1, R5, R2, R8, R12 |
| `paper_trade` | paper, live, position, open, close, trader, tick, dashboard | R1, R3, R6, R7, R4, R11 |
| `data_fetch` | fetch, Binance, OHLCV, CSV, download, API, kline | R5, R6, R3 |
| `code_fix` | fix, bug, error, refactor, lint, traceback | R1, R3, R12 |
| `analysis` | analyse, review, trade log, equity curve, performance, metrics | R5, R8 |
| `planning` | plan, roadmap, GBP 100k, compound, scale, standalone, deploy | R11, R7, R8, R4 |

---

## Rewrite Templates

### strategy_change
```
TASK: [specific change requested]
FILE: scripts/hawk_backtest.py (or hawk_paper_trader.py)
CONSTRAINT: Run backtest first to verify EV is still positive after change.
CONSTRAINT: qty formula must remain: qty = risk_usdt / sl_dist (R1)
CONSTRAINT: No speculative features (trailing SL, partial exit) unless requested (R11)
VERIFY: After change, confirm WR × RR - (1-WR) > 0
```

### backtest_request
```
TASK: Run backtest with [parameters]
FILE: scripts/hawk_backtest.py
DATA: data/ETHUSDT_1h.csv (17,520 bars, Apr 2024–Apr 2026)
CONSTRAINT: Sizing formula must be qty = risk / sl_dist, NOT qty × leverage (R1)
CONSTRAINT: If trades = 0, check column names first (R5)
EXPECTED: ~540 trades/2yr at 10x, 42% WR, 1.58 RR
```

### paper_trade
```
TASK: Run paper trader for [symbol] at [leverage]x
FILE: scripts/hawk_paper_trader.py
COMMAND: python scripts/hawk_paper_trader.py --symbols [symbol] --leverage [lev]
CONSTRAINT: Uses Binance public REST — no API key needed (R6)
CONSTRAINT: State persists in logs/hawk_paper_state.json (R4)
NOTE: --run-once flag for single-tick test without sleeping
```

---

## Context Guardian

After every ~15 exchanges, check if `context.md` needs updating:
- Did strategy parameters change? → update "Active Strategy" section
- Did backtest results improve? → update "Backtested Results" table
- Did a new issue get identified? → add to "Open Issues / Next Steps"
- Did a hard rule get violated and fixed? → note it

Prompt: "Update context.md — [what changed and why]"

---

## Session Start Checklist

When opening a new session on this project:

1. Read `context.md` last-updated date
2. Check if paper trader has run (`logs/hawk_paper_state.json` exists)
3. Note current phase (paper / moving-to-live)
4. Remind user of best config: ETH/USDT · 10x · 3 concurrent positions

---

## Common Mistakes to Prevent

| Mistake | Consequence | Rule |
|---------|-------------|------|
| `qty = risk / sl_dist * leverage` | 10x+ → 15–75% equity lost per SL hit | R1 |
| Using `iloc[-1]` (current bar) for signal | Look-ahead bias — inflated WR | R3 |
| Adding BE-stop | Actual RR drops from 1.58 to 0.82 (negative EV) | R11 |
| Recommending 50x live | Liq distance = 1.5%; flash wicks liquidate entire position | R8 |
| Hardcoding fees/funding | If exchange changes rates, all backtests become invalid | R12 |
