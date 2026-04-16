# HAWK Crypto Bot — Claude Instructions

> The prompt optimizer hook fires on every prompt — it reads `context.md` and injects a phase summary + relevant hard-rule constraints automatically. **Only read `context.md` in full when you need detail the hook summary doesn't cover.**
> Always update `context.md` after every meaningful change.

---

## Prompt Optimizer System

Every prompt is intercepted by `.claude/hooks/prompt_optimizer_hook.py` (configured in `.claude/settings.json`).

The hook: reads `context.md` → classifies intent → injects relevant hard-rule constraints → outputs a structured header prepended to model context.

Intent categories: `strategy_change` · `backtest_request` · `paper_trade` · `data_fetch` · `code_fix` · `analysis` · `planning`

Hard rules R1–R12 are encoded in the hook and injected per-intent. Full rule text lives in `.claude/skills/prompt-optimizer/SKILL.md`.

On-demand: `/prompt-optimize <raw prompt>` previews the optimized version before executing.

---

## Project in One Line

Python paper trading bot for crypto futures (ETH/BTC/SOL) via Binance public API — HAWK-ACTIVE v5 strategy (8-bar channel breakout + EMA20/50 filter), correct leverage sizing, GBP 500 → GBP 100k compounding goal.

## Stack

- **Language**: Python 3.x
- **Data**: Binance public REST API (`api.binance.com/api/v3/klines`) — no API key needed for paper mode
- **Key libs**: pandas, numpy, requests
- **No web framework, no DB** — JSON state file, CSV trade log

## Repo Structure (guarded zones ★)

```
scripts/
  hawk_paper_trader.py      — Live paper trader (ACTIVE — run this)
  hawk_backtest.py          — 2yr backtest engine (ACTIVE)
backtester/
  leveraged_engine.py ★     — Constants: GBP_TO_USDT, fees, funding (DO NOT MODIFY)
  engine.py                  — Generic backtest (trend/DCA/grid)
  metrics.py                 — Sharpe, drawdown, profit factor
config/
  config.yaml                — Bot configuration
data/
  BTCUSDT_1h.csv             — 2yr 1h OHLCV for BTC
  ETHUSDT_1h.csv             — 2yr 1h OHLCV for ETH
  SOLUSDT_1h.csv             — 2yr 1h OHLCV for SOL
logs/                        — Runtime state (gitignored)
  hawk_paper_state.json      — Paper trader state (persists across restarts)
  hawk_paper_trades.csv      — Trade log
context.md                   — Cross-session AI context (always update)
CLAUDE.md                    — This file
```

---

## THE Critical Rule (say this before every backtest or code change)

```python
# CORRECT position sizing — leverage-independent
risk_usdt  = equity * risk_pct / 100      # always 1.5% regardless of leverage
sl_dist    = sl_atr_mult * atr            # in price units
qty        = risk_usdt / sl_dist          # qty is NOT multiplied by leverage
margin     = qty * price / leverage       # leverage only reduces margin deposit
```

Violating this causes instant account blowup at 10x+ leverage.

---

## Debug Playbook (run before touching strategy code)

```python
# If trades = 0 → 90% chance it's data, not strategy
print(df.columns)           # must be: open high low close volume (lowercase)
print(df.index[:5])         # must be DatetimeIndex
print(df.dtypes)            # all float64
print(len(df))              # should be ~17,500 for 2yr 1h data

# Sanity check signals
df["chan_high"] = df["high"].rolling(8).max().shift(1)
df["ema20"] = df["close"].ewm(span=20).mean()
df["ema50"] = df["close"].ewm(span=50).mean()
signals = df[(df["close"] > df["chan_high"]) & (df["ema20"] > df["ema50"])]
print(f"Long signals: {len(signals)}")   # should be ~700 over 2yr
```

---

## Behaviour Rules

1. **Hook provides context summary** — read `context.md` in full only when you need detail beyond the hook's one-line summary.

2. **Update `context.md` after every meaningful change** — not optional. Future sessions depend on it.

3. **Guarded zone** (`leveraged_engine.py`) — do not modify constants without explicit user instruction.

4. **Data correctness > strategy correctness** — if trades = 0, check CSV column names and index type first.

5. **No API credentials** — paper mode uses Binance public REST only. Never suggest adding an API key for paper trading.

6. **Phase awareness** — paper mode = no real orders. Never suggest going live without 30+ paper trades confirming positive EV.

7. **No speculative features** — no trailing stops, partial exits, Telegram alerts, or web UI unless explicitly asked.

8. **Verify before committing** — strategy change: run backtest first. Paper trader change: test with `--run-once`. Bug fix: reproduce then verify. Uncertain root cause: use `git checkout -b fix/...`.

9. **Leverage ceiling** — 20x is the practical maximum for live trading. 30x+ produces excess liquidations (verified in backtest). Never recommend 50x for live use.

10. **BE-stop is removed** — do not re-add a break-even stop. It was the single biggest performance killer (actual RR dropped from 1.58 to 0.82). Document if the user asks why.

---

## Common Commands

```bash
# Paper trade (ETH/USDT, 10x, GBP 500 — recommended)
python scripts/hawk_paper_trader.py

# Paper trade all 3 assets
python scripts/hawk_paper_trader.py --symbols ETH/USDT BTC/USDT SOL/USDT

# Single test tick (no waiting)
python scripts/hawk_paper_trader.py --run-once

# Run full backtest (BTC/ETH/SOL across all leverages)
python scripts/hawk_backtest.py

# Backtest with custom parameters
python scripts/hawk_backtest.py   # edit LEVERAGES and DATASETS at top of file
```
