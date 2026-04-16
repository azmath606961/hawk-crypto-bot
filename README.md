# HAWK Crypto Bot
### Channel Breakout + EMA + ADX · Binance Futures · Python · GBP 500 → GBP 100k

---

## Current Version: v6 (HAWK-ACTIVE)

**Strategy:** Channel breakout + EMA20/50 trend filter + ADX(14)≥20 trend-strength gate.

**Comprehensive backtest results (25,920 combos, Apr 2024–Apr 2026):**

| Asset | TF | Lev | Channel | SL | RR | Filters | Return | Monthly% | WR% | Liqs |
|-------|----|----|---------|----|----|---------|--------|----------|-----|------|
| **ETH** | 1h | 10x | 8 | 1.5× | 2.0 | ADX≥20 | +656% | **+8.80%** | 43.4% | 0 |
| **XRP** | 1h | 10x | 16 | 1.0× | 3.0 | none | +289% | **+5.83%** | 20.0% | 0 |
| **BTC** | 4h | 10x | 8 | 1.5× | 2.0 | MACD | +87% | **+2.64%** | 47.1% | 0 |
| **BNB** | 4h | 10x | 16 | 1.5× | 3.0 | ADX≥25+RSI | +61% | **+2.00%** | 43.9% | 0 |
| **ADA** | 4h | 10x | 16 | 2.0× | 2.5 | MACD | +47% | **+1.51%** | — | 0 |
| SOL | — | — | — | — | — | — | **REJECTED** — no positive EV in any combo | — |

**Combined portfolio (all 5 assets, 10x):** ~14.54%/mo → GBP 100k in ~3 years 3 months

---

## No Exchange Account Required

The paper trader and backtester **do not need a Binance account or API key**. They use:

- **Paper trader** — fetches live 1h candles from `api.binance.com/api/v3/klines` (public endpoint). All positions are simulated and persisted in `logs/hawk_paper_state.json`. No real orders are ever placed.
- **Backtester** — reads local CSV files in `data/`. Fully offline.

---

## Setup

### Prerequisites

- Python 3.10+
- Git

### Windows (PowerShell)

```powershell
git clone https://github.com/azmath606961/hawk-crypto-bot.git
cd hawk-crypto-bot

python -m venv .venv
.\.venv\Scripts\Activate

pip install -r requirements.txt
```

> If activation is blocked by execution policy, run once:
> `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

### Ubuntu / macOS (bash)

```bash
git clone https://github.com/azmath606961/hawk-crypto-bot.git
cd hawk-crypto-bot

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

---

## Quick Start (Paper Trading)

```bash
# ETH 1h only (default)
python scripts/hawk_paper_trader.py

# Full portfolio: ETH 1h + BTC/BNB/ADA 4h
python scripts/hawk_paper_trader.py --4h-symbols BTC/USDT BNB/USDT ADA/USDT

# Single test tick
python scripts/hawk_paper_trader.py --run-once

# Comprehensive backtest (all assets/TFs/leverages/indicators — 25,920 combos)
python scripts/hawk_comprehensive_backtest.py

# Multi-TF backtest
python scripts/hawk_backtest_multi.py
```

---

## The Critical Leverage Rule

```
WRONG:  qty = (risk / sl_dist) × leverage   → at 50x: one SL = 75% of equity. Instant blowup.
RIGHT:  qty = risk / sl_dist                → at ANY leverage: one SL = exactly 1.5% of equity.
```

Leverage only controls **how much margin you deposit**, not how much you risk. This is the single most important rule in this codebase.

---

## Strategy Rules (v6)

| Parameter | Value |
|-----------|-------|
| Signal | Close > 8-bar high (long) / < 8-bar low (short) |
| Trend filter | EMA20 > EMA50 (long) / EMA20 < EMA50 (short) |
| ADX gate | ADX(14) ≥ 20 — skip entries in ranging markets (1h only) |
| Stop loss | 1.5 × ATR(14) |
| Take profit | 2.0–3.0 × SL distance (asset-dependent) |
| Max hold | 30h (1h) / 48h (4h) |
| Risk/trade | 1.5% of equity — leverage-independent |
| Max concurrent | 3 positions per 60% margin cap |
| DD gate | 30% drawdown → halt all trading |

---

## Paper Trading Output

Every tick prints a live dashboard:
```
============================================================
  HAWK PAPER TRADER  |  2026-04-16 09:01
  Leverage: 10x  |  Symbols: ETH/USDT
============================================================
  Equity        : $    892.47  (GBP 702.73)
  Peak equity   : $    947.21
  Drawdown      :       5.8%
  Total PnL     : $    +257.47
  Trades        : 43  |  Wins: 18  |  WR: 41.9%
  Funding paid  : $      4.21
  Liquidations  : 0
  Ticks run     : 312

  Open positions (2):
    ETH/USDT LONG   entry=1823.45  cur=1871.20  SL=1791.20  TP=1887.95  unreal=+24.83
    ETH/USDT SHORT  entry=1901.00  cur=1871.20  SL=1931.20  TP=1841.20  unreal=+15.53
============================================================
```

State persists across restarts via `logs/hawk_paper_state.json`. Trade log saved to `logs/hawk_paper_trades.csv`.

---

## Compounding Roadmap (GBP 500 → GBP 100k)

| Scenario | Monthly% | GBP 500→1k | GBP 1k→10k | GBP 10k→100k |
|----------|----------|------------|------------|--------------|
| ETH 10x only | +10.3% | 8 months | 2y 7m | 4y 7m |
| All 3 assets 10x | +30.8% | 3 months | 1 year | 1y 8m |

---

## File Structure

```
hawk-crypto-bot/
├── scripts/
│   ├── hawk_paper_trader.py    ★ ACTIVE — live paper trading runner
│   └── hawk_backtest.py        ★ ACTIVE — 2yr backtest engine
├── config/
│   └── config.yaml             Bot configuration
├── backtester/
│   ├── leveraged_engine.py     Constants (GBP_TO_USDT, fees, funding) — DO NOT MODIFY
│   ├── engine.py               Generic backtest engine (trend/DCA/grid)
│   └── metrics.py              Sharpe, drawdown, profit factor
├── data/
│   ├── BTCUSDT_1h.csv          2yr 1h OHLCV for BTC
│   ├── ETHUSDT_1h.csv          2yr 1h OHLCV for ETH
│   └── SOLUSDT_1h.csv          2yr 1h OHLCV for SOL
├── logs/                       Paper trade state + CSV logs (gitignored)
├── requirements.txt
├── context.md                  Cross-session AI context
└── CLAUDE.md                   Claude Code instructions
```

---

## Leverage Guide

| Leverage | Margin/trade | Max concurrent | SL cost | Recommended? |
|----------|-------------|----------------|---------|--------------|
| 3x | 50.5% | 1 | 1.5% | Yes (safe, slow) |
| 5x | 30.3% | 1 | 1.5% | Yes |
| **10x** | **15.2%** | **3** | **1.5%** | **Yes (optimal)** |
| 20x | 7.6% | 3 | 1.5% | Caution (11 liqs/2yr) |
| 30x | 5.1% | 3 | 1.5% | High risk (52 liqs/2yr) |
| 50x | 3.0% | 3 | 1.5% | Not recommended |

At 50x the liquidation distance is only ~1.5% — flash wicks can bypass your SL and liquidate the full position even though your risk formula is correct.
