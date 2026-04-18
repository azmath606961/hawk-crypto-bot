# HAWK Crypto Bot
### Channel Breakout + EMA + ADX · Binance Futures · Python · GBP 500 → GBP 100k

---

## Current Version: v6 (HAWK-ACTIVE)

**Strategy:** Channel breakout + EMA20/50 trend filter + per-symbol RSI/MACD/ADX gates.

**Comprehensive backtest — 25,920 combos, Apr 2024–Apr 2026, Wilder EMA (confirmed reproducible):**

### Best per asset (optimal leverage)

| Asset | TF | Lev | ch | SL | RR | Filters | Return | Monthly% | WR% | Liqs |
|-------|----|----|----|----|----|---------|----|----------|-----|------|
| **XRP** | 1h | 20x | 12 | 1.5× | 2.5 | none | +650% | **+8.78%** | 29.1% | 0 |
| **ETH** | 1h | 20x | 12 | 1.0× | 2.5 | RSI+MACD | +240% | **+5.25%** | 24.0% | 0 |
| **BTC** | 4h | 10x | 8 | 1.5× | 2.0 | RSI+MACD | +87% | **+2.64%** | 47.1% | 0 |
| **BNB** | 4h | 10x | 16 | 1.5× | 3.0 | ADX≥25+RSI | +61% | **+2.00%** | 43.9% | 0 |
| **ADA** | 4h | 5x | 8 | 2.0× | 2.5 | MACD | +53% | **+1.80%** | 46.5% | 0 |
| SOL | — | — | — | — | — | — | **REJECTED** — no positive EV in any of 2,160 combos | — |

### Best per asset (10x cap)

| Asset | TF | ch | SL | RR | Filters | Monthly% |
|-------|----|----|----|----|---------|----------|
| XRP | 1h | 16 | 1.0× | 3.0 | none | +5.84% |
| ETH | 1h | 8 | 2.0× | 2.0 | RSI | +2.56% |
| BTC | 4h | 8 | 1.5× | 2.0 | RSI+MACD | +2.64% |
| BNB | 4h | 16 | 1.5× | 3.0 | ADX≥25+RSI | +2.00% |
| ADA | 4h | 16 | 2.0× | 2.5 | RSI+MACD | +1.51% |

> All scripts use Wilder EMA (alpha=1/p). RSI/MACD/ADX filters are now wired into `hawk_trader.py` — paper mode applies the exact same signal logic as the backtest.

**Combined portfolios:**

| Portfolio | Monthly% | GBP 500→100k |
|-----------|----------|--------------|
| Conservative (all 10x) | **+14.56%** | ~3y 3m |
| Optimal (mixed leverage) | **+20.47%** | ~2y 4m |

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
# ── Paper trading — baseline portfolios ───────────────────────────────────────
python scripts/hawk_trader.py --paper --portfolio conservative      # 10x all, +14.56%/mo
python scripts/hawk_trader.py --paper --portfolio optimal           # mixed leverage, +20.47%/mo

# ── Paper trading — A/B test portfolios (vol filter on ETH/XRP 1h) ────────────
python scripts/hawk_trader.py --paper --portfolio conservative_vol  # baseline + vol filter
python scripts/hawk_trader.py --paper --portfolio optimal_vol       # baseline + vol filter

# Single test tick — verify it runs before leaving it overnight
python scripts/hawk_trader.py --paper --portfolio conservative --run-once

# ── Dashboards (one per terminal, --portfolio auto-fills state/port) ───────────
python scripts/hawk_dashboard.py --portfolio conservative        # http://localhost:5000
python scripts/hawk_dashboard.py --portfolio conservative_vol    # http://localhost:5001
python scripts/hawk_dashboard.py --portfolio optimal             # http://localhost:5002
python scripts/hawk_dashboard.py --portfolio optimal_vol         # http://localhost:5003

# ── 4-way comparator ──────────────────────────────────────────────────────────
python scripts/hawk_comparator.py            # one-shot snapshot
python scripts/hawk_comparator.py --watch    # refresh every 60s

# ── Live trading ──────────────────────────────────────────────────────────────
python scripts/hawk_trader.py --testnet --portfolio conservative   # testnet first
python scripts/hawk_trader.py --portfolio conservative             # real money

# ── Backtests ─────────────────────────────────────────────────────────────────
python scripts/hawk_comprehensive_backtest.py   # 25,920-combo grid search
python scripts/hawk_volume_study.py             # focused vol/body filter study
```

## Portfolio Presets

Four presets — two baselines + two A/B test variants with volume Z-score filter. Each writes to its own state file and trade log.

| Portfolio | Leverage | Monthly% | GBP 500→100k | Notes |
|-----------|----------|----------|--------------|-------|
| **conservative** | all 10x | **+14.56%** | ~3y 3m | Baseline |
| **conservative_vol** | all 10x | TBD (A/B) | TBD | +Vol filter on ETH/XRP 1h |
| **optimal** | mixed (20x/20x/10x/10x/5x) | **+20.47%** | ~2y 4m | Baseline |
| **optimal_vol** | mixed (20x/20x/10x/10x/5x) | TBD (A/B) | TBD | +Vol filter on ETH/XRP 1h |

**Conservative / Conservative+Vol params (all 10x):**

| Symbol | TF | ch | SL | RR | Filters (baseline) | Filters (+vol) |
|--------|----|----|----|----|-------------------|----------------|
| ETH/USDT | 1h | 8 | 2.0× | 2.0 | RSI | RSI+VOL |
| XRP/USDT | 1h | 16 | 1.0× | 3.0 | none | VOL |
| BTC/USDT | 4h | 8 | 1.5× | 2.0 | RSI+MACD | RSI+MACD (unchanged) |
| BNB/USDT | 4h | 16 | 1.5× | 3.0 | ADX≥25+RSI | ADX≥25+RSI (unchanged) |
| ADA/USDT | 4h | 16 | 2.0× | 2.5 | RSI+MACD | RSI+MACD (unchanged) |

**Optimal / Optimal+Vol params (mixed leverage):**

| Symbol | TF | Lev | ch | SL | RR | Filters (baseline) | Filters (+vol) |
|--------|----|----|----|----|----|--------------------|----------------|
| ETH/USDT | 1h | 20x | 12 | 1.0× | 2.5 | RSI+MACD | RSI+MACD+VOL |
| XRP/USDT | 1h | 20x | 12 | 1.5× | 2.5 | none | VOL |
| BTC/USDT | 4h | 10x | 8 | 1.5× | 2.0 | RSI+MACD | RSI+MACD (unchanged) |
| BNB/USDT | 4h | 10x | 16 | 1.5× | 3.0 | ADX≥25+RSI | ADX≥25+RSI (unchanged) |
| ADA/USDT | 4h | 5x | 8 | 2.0× | 2.5 | MACD | MACD (unchanged) |

**Volume filter logic:** Signal candle volume must be ≥ 20-bar rolling mean + 0.5σ. Applied to 1h strategies only (4h backtest showed negative delta). Study results: ETH 1h +1.96%/mo delta, XRP 1h +2.74%/mo delta.

> **Before going live:** Accumulate 30+ paper trades per portfolio with positive EV. Check `logs/hawk_trades_<portfolio>.csv`. Vol-filter variants require PR approval before merging to master.

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
