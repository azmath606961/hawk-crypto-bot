# HAWK Crypto Bot
### Channel Breakout + EMA + ADX · Binance Futures · Python · GBP 500 → GBP 100k

---

## Current Version: v6 (HAWK-ACTIVE)

**Strategy:** Channel breakout + EMA20/50 trend filter + ADX(14)≥20 trend-strength gate.

**Comprehensive backtest results (25,920 combos, Apr 2024–Apr 2026, Wilder EMA):**

| Asset | TF | Lev | Channel | SL | RR | Filters | Return | Monthly% | WR% | Liqs |
|-------|----|----|---------|----|----|---------|--------|----------|-----|------|
| **XRP** | 1h | 10x | 16 | 1.0× | 3.0 | none | +289% | **+5.83%** | 20.0% | 0 |
| **ETH** | 1h | 10x | 8 | 2.0× | 2.0 | RSI | +134% | **+2.56%** | — | 0 |
| **BTC** | 4h | 10x | 8 | 1.5× | 2.0 | RSI+MACD | +87% | **+2.64%** | 47.1% | 0 |
| **BNB** | 4h | 10x | 16 | 1.5× | 3.0 | ADX≥25+RSI | +61% | **+2.00%** | 43.9% | 0 |
| **ADA** | 4h | 10x | 16 | 2.0× | 2.5 | RSI+MACD | +47% | **+1.51%** | — | 0 |
| SOL | — | — | — | — | — | — | **REJECTED** — no positive EV in any combo | — |

> Previous ETH figure of +8.80%/mo was an EMA implementation bug (standard vs Wilder). All scripts now use Wilder EMA (alpha=1/p) matching the backtest exactly.

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

## Quick Start

`hawk_trader.py` is the single unified runner — **identical HAWK v6 strategy** in paper and live mode.

```bash
# ── Paper trading (no API key needed) ────────────────────────────────────────

# Conservative portfolio — all 10x, +14.54%/mo (Terminal 1)
python scripts/hawk_trader.py --paper --portfolio conservative

# Optimal portfolio — mixed leverage, +20.44%/mo (Terminal 2)
python scripts/hawk_trader.py --paper --portfolio optimal

# Single test tick — verify it runs before leaving it overnight
python scripts/hawk_trader.py --paper --portfolio conservative --run-once

# ── Dashboard ─────────────────────────────────────────────────────────────────
python scripts/hawk_dashboard.py --state logs/hawk_state_conservative.json --port 5000
python scripts/hawk_dashboard.py --state logs/hawk_state_optimal.json --port 5001

# ── Live trading ──────────────────────────────────────────────────────────────
python scripts/hawk_trader.py --testnet --portfolio conservative   # testnet first
python scripts/hawk_trader.py --portfolio conservative             # real money

# ── Backtests ─────────────────────────────────────────────────────────────────
python scripts/hawk_comprehensive_backtest.py   # 25,920-combo grid search
python scripts/hawk_backtest_multi.py           # multi-TF backtest
```

## Portfolio Presets

Two presets run all 5 assets with per-symbol params from the 25,920-combo backtest. Each writes to its own state file — run both in parallel to compare performance.

| Portfolio | Assets | Leverage | Monthly% | GBP 500→100k |
|-----------|--------|----------|----------|--------------|
| **conservative** | ETH/XRP 1h · BTC/BNB/ADA 4h | all 10x | +14.54% | ~3y 3m |
| **optimal** | ETH/XRP 1h · BTC/BNB 4h · ADA 4h | 20x/20x/10x/10x/5x | +20.44% | ~2y 4m |

> **Before going live:** Accumulate 30+ paper trades per portfolio with positive EV. Check `logs/hawk_trades_<portfolio>.csv`.

---

## Going Live

The same `hawk_trader.py` script handles live trading — no config changes, no separate codebase. The strategy logic is byte-for-byte identical to paper mode; only the execution layer changes.

### 1. Binance Account Setup

1. Create account at [binance.com](https://binance.com)
2. Complete KYC verification
3. Enable **USD-M Futures** trading
4. Go to **API Management** → Create API key
5. Enable: **Read** + **Futures trading** (do NOT enable withdrawals)
6. Save your API Key and Secret

### 2. Set Environment Variables

**Windows (PowerShell):**
```powershell
$env:BINANCE_API_KEY    = "your_api_key_here"
$env:BINANCE_API_SECRET = "your_api_secret_here"
```

**Linux / macOS:**
```bash
export BINANCE_API_KEY="your_api_key_here"
export BINANCE_API_SECRET="your_api_secret_here"
```

To persist, add to `~/.bashrc` / `~/.zshrc` (Linux/macOS) or Windows user environment variables.

### 3. Test on Binance Futures Testnet First

Get free testnet API keys at [testnet.binancefuture.com](https://testnet.binancefuture.com) (separate from live keys).

```bash
# Set your TESTNET keys in env vars, then:
python scripts/hawk_trader.py --testnet
```

Confirm orders appear on the testnet dashboard before going live.

### 4. Go Live

```bash
# Set your LIVE keys in env vars, then:
python scripts/hawk_trader.py

# Full portfolio:
python scripts/hawk_trader.py --4h-symbols BTC/USDT BNB/USDT ADA/USDT

# Custom capital / leverage:
python scripts/hawk_trader.py --capital 500 --leverage 10 --risk-pct 1.5
```

The bot sets leverage and margin mode automatically on first trade per symbol.

> **Prerequisite:** Run at least 30 paper trades (`--paper`) with positive EV before going live. Check results in `logs/hawk_trades.csv`.

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
│   ├── hawk_trader.py          ★ ACTIVE — unified paper + live runner (--paper flag)
│   ├── hawk_comprehensive_backtest.py  ★ ACTIVE — 25,920-combo grid search
│   ├── hawk_backtest_multi.py  ★ ACTIVE — multi-TF backtest
│   └── hawk_paper_trader.py    legacy paper-only runner (superseded by hawk_trader.py)
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
