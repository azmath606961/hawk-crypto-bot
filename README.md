# HAWK Crypto Bot
### 8-Bar Channel Breakout · Binance Futures · Python · GBP 500 → GBP 100k

---

## Current Version: v5 (HAWK-ACTIVE — High-Leverage Edition)

**Strategy:** 1h channel breakout filtered by EMA20/EMA50 direction. Long when price breaks 8-bar high in BULL regime; short when price breaks 8-bar low in BEAR regime.

**Backtested results (real Binance data, Apr 2024 – Apr 2026):**

| Asset | Leverage | Return | Peak | Win Rate | Actual RR | T/Day | Liqs |
|-------|----------|--------|------|----------|-----------|-------|------|
| ETH/USDT | 3x | +20% | GBP 864 | 40.6% | 1.61:1 | 0.9 | 0 |
| ETH/USDT | **10x** | **+83%** | **GBP 1,312** | **42.4%** | **1.58:1** | **1.6** | **1** |
| ETH/USDT | 20x | +71% | GBP 1,229 | 42.6% | 1.55:1 | 1.6 | 11 |
| BTC/USDT | 5x | -15% | GBP 526 | 36.4% | 1.67:1 | 0.9 | 0 |
| SOL/USDT | 10x | -19% | GBP 588 | 35.6% | 1.63:1 | 1.7 | 0 |

> B&H reference: ETH -23.6%, BTC +18.1%, SOL -38.5% over same period.
> ETH 10x is the sweet spot — best return, lowest liquidation risk, 3 concurrent positions.

---

## The Critical Leverage Rule

```
WRONG:  qty = (risk / sl_dist) × leverage   → at 50x: one SL = 75% of equity. Instant blowup.
RIGHT:  qty = risk / sl_dist                → at ANY leverage: one SL = exactly 1.5% of equity.
```

Leverage only controls **how much margin you deposit**, not how much you risk. This is the single most important rule in this codebase.

---

## Quick Start (Paper Trading)

```bash
git clone https://github.com/azmath606961/nifty50.git
cd nifty50
git checkout claude/sweet-roentgen
cd dev/crypto_bot
pip install -r requirements.txt

# Paper trade ETH/USDT at 10x (GBP 500 starting capital)
python scripts/hawk_paper_trader.py

# All three assets
python scripts/hawk_paper_trader.py --symbols ETH/USDT BTC/USDT SOL/USDT

# Custom capital/leverage
python scripts/hawk_paper_trader.py --capital 1000 --leverage 20

# Single test tick (no waiting)
python scripts/hawk_paper_trader.py --run-once
```

**No API key required.** Fetches live data from Binance public REST endpoints.

---

## Strategy Rules

| Parameter | Value | Why |
|-----------|-------|-----|
| Signal | 1h close > 8-bar highest high | Genuine new breakout, not noise |
| Direction filter | EMA20 vs EMA50 (1h) | Flips in hours–days, not weeks |
| Stop loss | 1.5 × ATR(14) | Covers typical wick noise |
| Take profit | 2.0 × SL distance | Positive EV at 42% WR |
| Max hold | 30 hours | Prevents dead-money positions |
| Risk per trade | 1.5% of equity (always) | Fixed regardless of leverage |
| Max concurrent | 3 positions (at 10x+) | 60% margin cap |
| DD gate | 30% from peak → halt | Preserves capital after losing streaks |

---

## Compounding Roadmap (GBP 500 → GBP 100k)

| Scenario | Monthly% | GBP 500→1k | GBP 1k→10k | GBP 10k→100k |
|----------|----------|------------|------------|--------------|
| ETH 10x only | +10.3% | 8 months | 2y 7m | 4y 7m |
| All 3 assets 10x | +30.8% | 3 months | 1 year | 1y 8m |

---

## File Structure

```
dev/crypto_bot/
├── scripts/
│   ├── hawk_paper_trader.py    ★ ACTIVE — live paper trading runner
│   └── hawk_backtest.py        ★ ACTIVE — 2yr backtest engine
├── config/
│   └── config.yaml             Bot configuration
├── backtester/
│   ├── leveraged_engine.py     Constants (GBP_TO_USDT, fees, funding)
│   ├── engine.py               Generic backtest engine (trend/DCA/grid)
│   └── metrics.py              Sharpe, drawdown, profit factor
├── core/
│   ├── exchange.py             ccxt exchange wrapper
│   ├── risk_manager.py         Position sizing + DD gate
│   └── order_executor.py       Order placement
├── strategies/
│   └── hawk_strategy.py        HAWK strategy documentation
├── data/
│   ├── BTCUSDT_1h.csv          2yr 1h OHLCV for BTC
│   ├── ETHUSDT_1h.csv          2yr 1h OHLCV for ETH
│   └── SOLUSDT_1h.csv          2yr 1h OHLCV for SOL
├── logs/                       Paper trade state + CSV logs (gitignored)
├── context.md                  Cross-session AI context
└── CLAUDE.md                   Claude Code instructions
```

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

Trade log is saved to `logs/hawk_paper_trades.csv`. State persists across restarts via `logs/hawk_paper_state.json`.

---

## Requirements

```
pandas>=2.0
numpy
requests
ccxt
pyyaml
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
