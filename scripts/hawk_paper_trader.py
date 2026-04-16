"""
HAWK Paper Trader — Multi-Timeframe Live Paper Trading
=======================================================
Runs two HAWK strategies simultaneously from one equity pool:

  TIER 1 — 1h strategy (ETH/USDT by default)
    Signal : 8-bar channel breakout + EMA20/50 direction filter
    Tick   : every hour at :01 UTC
    Hold   : up to 30 bars (30h)

  TIER 2 — 4h strategy (BTC/USDT, optionally SOL/USDT)
    Signal : 12-bar channel breakout + EMA20/50 direction filter
    Tick   : every 4h at :01 UTC (at 00:01, 04:01, 08:01, 12:01, 16:01, 20:01)
    Hold   : up to 12 bars (48h)

Both strategies share a single equity pool with a 60% global margin cap.
No real orders placed. State persists in logs/hawk_paper_state.json.

Usage:
    python scripts/hawk_paper_trader.py                          # ETH 1h only
    python scripts/hawk_paper_trader.py --4h-symbols BTC/USDT   # ETH 1h + BTC 4h
    python scripts/hawk_paper_trader.py --4h-symbols BTC/USDT SOL/USDT
    python scripts/hawk_paper_trader.py --run-once               # single test tick
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

# ─────────────────────────────────────────────────────────────────────────── #
#  Constants — identical to backtest                                            #
# ─────────────────────────────────────────────────────────────────────────── #

GBP_TO_USDT    = 1.27
TAKER_FEE      = 0.0004     # 0.04% futures taker fee (matches backtest)
FUNDING_8H     = 0.0001     # 0.01% per 8h

# Confirmed optimal params from backtest (Apr 2024 – Apr 2026)
# v6 update: adx_min=20 added — grid search across 36 combos showed 4.7x better
# monthly return (8.80%/mo vs 3.71%) by keeping only trending-market entries.
# Mechanism: ADX≥20 ensures breakouts happen in directional markets; in those
# conditions TP hits occur at full target more often, raising actual RR 1.56→2.32.
STRATEGY_1H = dict(
    channel_n    = 8,
    ema_fast     = 20,
    ema_slow     = 50,
    sl_atr_mult  = 1.5,
    rr           = 2.0,
    max_hold_bars= 30,       # 30 × 1h = 30h
    bar_secs     = 3600,     # seconds per 1h bar
    funding_bars = 8,        # fund every 8 × 1h = 8h
    interval     = "1h",
    adx_min      = 20.0,     # HAWK v6: only trade when ADX(14) >= 20 (trending market)
)
STRATEGY_4H = dict(
    channel_n    = 12,       # wider lookback cuts BTC/SOL 4h noise
    ema_fast     = 20,
    ema_slow     = 50,
    sl_atr_mult  = 1.5,
    rr           = 2.0,
    max_hold_bars= 12,       # 12 × 4h = 48h
    bar_secs     = 14400,    # seconds per 4h bar
    funding_bars = 2,        # fund every 2 × 4h = 8h
    interval     = "4h",
)


# ─────────────────────────────────────────────────────────────────────────── #
#  Logging                                                                      #
# ─────────────────────────────────────────────────────────────────────────── #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hawk_paper")


# ─────────────────────────────────────────────────────────────────────────── #
#  Binance public data — no API key required                                    #
# ─────────────────────────────────────────────────────────────────────────── #

BINANCE_KLINE = "https://api.binance.com/api/v3/klines"

def fetch_ohlcv(symbol: str, interval: str = "1h", limit: int = 200) -> pd.DataFrame:
    """Fetch OHLCV from Binance public REST API (no auth needed)."""
    sym = symbol.replace("/", "")
    resp = requests.get(
        BINANCE_KLINE,
        params={"symbol": sym, "interval": interval, "limit": limit},
        timeout=10,
    )
    resp.raise_for_status()
    raw = resp.json()
    df = pd.DataFrame(raw, columns=[
        "ts", "open", "high", "low", "close", "volume",
        "close_ts", "quote_vol", "trades", "taker_base", "taker_quote", "_",
    ])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts")[["open", "high", "low", "close", "volume"]].astype(float)
    return df


def fetch_current_price(symbol: str) -> float:
    sym = symbol.replace("/", "")
    resp = requests.get(
        "https://api.binance.com/api/v3/ticker/price",
        params={"symbol": sym},
        timeout=5,
    )
    resp.raise_for_status()
    return float(resp.json()["price"])


# ─────────────────────────────────────────────────────────────────────────── #
#  Indicators                                                                   #
# ─────────────────────────────────────────────────────────────────────────── #

def _ema(s: pd.Series, p: int) -> pd.Series:
    # Wilder EMA (alpha=1/p) — matches hawk_comprehensive_backtest.py exactly
    return s.ewm(alpha=1 / p, adjust=False).mean()

def _atr(h: pd.Series, l: pd.Series, c: pd.Series, p: int = 14) -> pd.Series:
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / p, adjust=False).mean()

def _adx(h: pd.Series, l: pd.Series, c: pd.Series, p: int = 14) -> pd.Series:
    """ADX(14) via Wilder smoothing. Returns 0-100 trend-strength series.
    ADX >= 20: trending market (use this to gate entries).
    ADX < 20:  ranging/choppy market (skip breakout signals).
    """
    atr14  = _atr(h, l, c, p)
    up     = h.diff()
    dn     = -l.diff()
    dm_pos = up.where((up > dn) & (up > 0), 0.0)
    dm_neg = dn.where((dn > up) & (dn > 0), 0.0)
    di_pos = 100 * dm_pos.ewm(alpha=1 / p, adjust=False).mean() / atr14.replace(0, 1e-10)
    di_neg = 100 * dm_neg.ewm(alpha=1 / p, adjust=False).mean() / atr14.replace(0, 1e-10)
    dx     = 100 * (di_pos - di_neg).abs() / (di_pos + di_neg).replace(0, 1e-10)
    return dx.ewm(alpha=1 / p, adjust=False).mean()


# ─────────────────────────────────────────────────────────────────────────── #
#  Signal logic                                                                 #
# ─────────────────────────────────────────────────────────────────────────── #

def compute_signals(df: pd.DataFrame, channel_n: int = 8,
                    ema_fast: int = 20, ema_slow: int = 50,
                    compute_adx: bool = False) -> pd.DataFrame:
    df = df.copy()
    df["ema_f"]    = _ema(df["close"], ema_fast)
    df["ema_s"]    = _ema(df["close"], ema_slow)
    df["atr"]      = _atr(df["high"], df["low"], df["close"], 14)
    df["chan_high"] = df["high"].rolling(channel_n).max().shift(1)
    df["chan_low"]  = df["low"].rolling(channel_n).min().shift(1)
    if compute_adx:
        df["adx"] = _adx(df["high"], df["low"], df["close"], 14)
    return df


def get_signal(df: pd.DataFrame, adx_min: float | None = None) -> dict:
    """
    Signal from the last CONFIRMED candle (iloc[-2]).
    Works for any timeframe: the previous closed bar is always iloc[-2].

    adx_min: if set, only generate a signal when ADX(14) >= adx_min.
             Requires compute_signals(..., compute_adx=True) was called first.
    """
    row = df.iloc[-2]
    c   = float(row["close"])
    etf = float(row["ema_f"])
    ets = float(row["ema_s"])
    atr = float(row["atr"])
    ch  = float(row["chan_high"])
    cl  = float(row["chan_low"])

    bull = etf > ets
    bear = etf < ets

    # ADX gate: skip entry in ranging/choppy markets
    adx_val = None
    adx_ok  = True
    if adx_min is not None and "adx" in df.columns:
        adx_val = float(row["adx"]) if not pd.isna(row["adx"]) else 0.0
        adx_ok  = adx_val >= adx_min

    signal = None
    if adx_ok and bull and c > ch:
        signal = "long"
    elif adx_ok and bear and c < cl:
        signal = "short"

    return {
        "signal":    signal,
        "price":     c,
        "ema_f":     etf,
        "ema_s":     ets,
        "atr":       atr,
        "chan_high":  ch,
        "chan_low":   cl,
        "adx":       adx_val,
        "adx_ok":    adx_ok,
        "bull":      bull,
        "regime":    "BULL" if bull else ("BEAR" if bear else "FLAT"),
        "ts":        df.index[-2].isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────── #
#  State persistence                                                            #
# ─────────────────────────────────────────────────────────────────────────── #

def load_state(path: str, initial_usdt: float) -> dict:
    if os.path.exists(path):
        s = json.load(open(path))
        # Migrate old state files — add 4h fields if missing
        s.setdefault("bar_count_4h",    0)
        s.setdefault("last_close_bar_4h", -999)
        log.info("Loaded existing state — equity $%.2f, %d open positions",
                 s["equity"], len(s["positions"]))
        return s
    return {
        "equity":           initial_usdt,
        "peak_equity":      initial_usdt,
        "positions":        [],
        "closed_trades":    0,
        "wins":             0,
        "total_pnl":        0.0,
        "funding_paid":     0.0,
        "liqs":             0,
        "bar_count":        0,       # 1h ticks
        "bar_count_4h":     0,       # 4h ticks
        "last_close_bar":   -999,    # last 1h bar when any position closed
        "last_close_bar_4h": -999,   # last 4h bar when any position closed
        "started":          datetime.now(timezone.utc).isoformat(),
    }


def save_state(path: str, state: dict) -> None:
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def log_trade(csv_path: str, trade: dict) -> None:
    new_file = not os.path.exists(csv_path)
    cols = ["ts_open", "ts_close", "symbol", "tf", "side", "entry", "exit",
            "qty", "notional", "pnl_usdt", "reason", "equity_after"]
    with open(csv_path, "a", newline="") as f:
        if new_file:
            f.write(",".join(cols) + "\n")
        row = [str(trade.get(c, "")) for c in cols]
        f.write(",".join(row) + "\n")


# ─────────────────────────────────────────────────────────────────────────── #
#  Position sizing — identical formula to backtest                              #
# ─────────────────────────────────────────────────────────────────────────── #

def open_position(
    state: dict, sig: dict, symbol: str, leverage: int,
    risk_pct: float, rr: float, sl_atr_mult: float,
    max_margin_pct: float, max_pos: int,
    max_hold_bars: int, tf: str = "1h",
) -> dict | None:
    equity = state["equity"]
    side   = sig["signal"]
    price  = sig["price"]
    atr    = sig["atr"]

    sl_dist = sl_atr_mult * atr
    if sl_dist <= 0:
        return None

    risk_usdt = equity * risk_pct / 100
    qty       = risk_usdt / sl_dist          # CORRECT: leverage-independent sizing
    notional  = qty * price
    margin    = notional / leverage
    fee       = notional * TAKER_FEE

    # ── Global margin cap across ALL open positions ───────────────────────
    # Prevents ETH + BTC combined from exceeding 60% margin usage
    used_margin_total = sum(p["margin"] for p in state["positions"])
    avail = equity * max_margin_pct - used_margin_total
    if margin + fee > avail:
        if avail <= 0:
            log.info("Global margin cap reached — skipping %s %s", symbol, tf)
            return None
        scale    = avail / (margin + fee)
        qty     *= scale
        notional = qty * price
        margin   = notional / leverage
        fee      = notional * TAKER_FEE

    if notional < 5.0:
        return None

    if side == "long":
        sl = price - sl_dist
        tp = price + rr * sl_dist
    else:
        sl = price + sl_dist
        tp = price - rr * sl_dist

    state["equity"] -= fee

    bar_secs = STRATEGY_4H["bar_secs"] if tf == "4h" else STRATEGY_1H["bar_secs"]
    pos = {
        "symbol":         symbol,
        "tf":             tf,
        "side":           side,
        "entry":          price,
        "qty":            qty,
        "notional":       notional,
        "margin":         margin,
        "sl":             sl,
        "tp":             tp,
        "sl_dist":        sl_dist,
        "ts_open":        sig["ts"],
        "ts_open_epoch":  time.time(),   # real clock — for timeout
        "max_hold_secs":  max_hold_bars * bar_secs,
        # Legacy bar_count fields (kept for state compatibility)
        "bar_in":         state["bar_count"] if tf == "1h" else state["bar_count_4h"],
        "fee_open":       fee,
    }
    log.info("[PAPER OPEN ] %-10s [%2s] %5s  entry=%.4f  SL=%.4f  TP=%.4f  qty=%.4f  notional=$%.0f",
             symbol, tf, side.upper(), price, sl, tp, qty, notional)
    return pos


def close_position(state: dict, pos: dict, exit_price: float,
                   reason: str, csv_path: str) -> None:
    qty = pos["qty"]
    if pos["side"] == "long":
        raw_pnl = (exit_price - pos["entry"]) * qty
    else:
        raw_pnl = (pos["entry"] - exit_price) * qty

    fee = qty * exit_price * TAKER_FEE
    pnl = raw_pnl - fee

    state["equity"]      += pnl
    state["total_pnl"]   += pnl
    state["closed_trades"] += 1
    if pnl > 0:
        state["wins"] += 1
    if reason == "liq":
        state["liqs"] += 1
    if state["equity"] > state["peak_equity"]:
        state["peak_equity"] = state["equity"]

    # Update the correct cooldown counter for this TF
    tf = pos.get("tf", "1h")
    if tf == "4h":
        state["last_close_bar_4h"] = state["bar_count_4h"]
    else:
        state["last_close_bar"] = state["bar_count"]

    trade = {
        "ts_open":     pos["ts_open"],
        "ts_close":    datetime.now(timezone.utc).isoformat(),
        "symbol":      pos["symbol"],
        "tf":          tf,
        "side":        pos["side"],
        "entry":       f"{pos['entry']:.4f}",
        "exit":        f"{exit_price:.4f}",
        "qty":         f"{qty:.6f}",
        "notional":    f"{pos['notional']:.2f}",
        "pnl_usdt":    f"{pnl:.2f}",
        "reason":      reason,
        "equity_after": f"{state['equity']:.2f}",
    }
    log_trade(csv_path, trade)

    win_str = "WIN " if pnl > 0 else "LOSS"
    log.info("[PAPER CLOSE] %-10s [%2s] %5s  exit=%.4f  PnL=%+.2f USDT  (%s)  [%s]",
             pos["symbol"], tf, pos["side"].upper(), exit_price, pnl, win_str, reason.upper())


# ─────────────────────────────────────────────────────────────────────────── #
#  Per-tick logic — shared by both 1h and 4h strategies                        #
# ─────────────────────────────────────────────────────────────────────────── #

def _process_tick(
    state:          dict,
    symbol:         str,
    tf:             str,         # "1h" or "4h"
    leverage:       int,
    risk_pct:       float,
    rr:             float,
    sl_atr_mult:    float,
    max_hold_bars:  int,
    cooldown_bars:  int,
    max_margin_pct: float,
    max_pos:        int,
    csv_path:       str,
    cfg:            dict,        # STRATEGY_1H or STRATEGY_4H
) -> None:
    # Increment the correct bar counter
    if tf == "4h":
        state["bar_count_4h"] += 1
        bar    = state["bar_count_4h"]
        lcb    = state["last_close_bar_4h"]
        lcb_key = "last_close_bar_4h"
    else:
        state["bar_count"] += 1
        bar    = state["bar_count"]
        lcb    = state["last_close_bar"]
        lcb_key = "last_close_bar"

    # ── Fetch live candles ────────────────────────────────────────────────
    adx_min = cfg.get("adx_min")   # None for 4h strategies; 20.0 for 1h
    try:
        df_raw  = fetch_ohlcv(symbol, cfg["interval"], limit=200)
        df      = compute_signals(df_raw, cfg["channel_n"], cfg["ema_fast"],
                                  cfg["ema_slow"], compute_adx=(adx_min is not None))
        sig     = get_signal(df, adx_min=adx_min)
        price   = float(df_raw["close"].iloc[-1])
    except Exception as exc:
        log.error("Data fetch failed for %s [%s]: %s", symbol, tf, exc)
        return

    adx_str = f"  ADX={sig['adx']:.1f}{'(ok)' if sig['adx_ok'] else '(low)'}" \
              if sig["adx"] is not None else ""
    log.info("Tick #%d [%s] %s | $%.2f | Regime: %s | Signal: %s%s",
             bar, tf, symbol, price, sig["regime"], sig["signal"] or "none", adx_str)

    # ── Funding every N bars ──────────────────────────────────────────────
    if bar % cfg["funding_bars"] == 0:
        for pos in state["positions"]:
            if pos["symbol"] != symbol or pos.get("tf", "1h") != tf:
                continue
            fund = pos["notional"] * FUNDING_8H
            if pos["side"] == "long":
                state["equity"] -= fund
                state["funding_paid"] += fund
            else:
                state["equity"] += fund

    # ── Check / close open positions for this symbol+tf ──────────────────
    liq_dist = 1 / leverage - 0.005
    to_close  = []

    for pos in state["positions"]:
        if pos["symbol"] != symbol or pos.get("tf", "1h") != tf:
            continue
        ep = None; reason = ""

        # Timeout: use real elapsed time (works across restarts)
        timed_out = False
        if "ts_open_epoch" in pos:
            elapsed = time.time() - pos["ts_open_epoch"]
            timed_out = elapsed >= pos.get("max_hold_secs", max_hold_bars * cfg["bar_secs"])
        else:
            # Fallback for old state files that lack ts_open_epoch
            timed_out = (bar - pos["bar_in"]) >= max_hold_bars

        if pos["side"] == "long":
            liq_p = pos["entry"] * (1 - liq_dist)
            if price <= liq_p:
                ep = liq_p;              reason = "liq"
            elif price <= pos["sl"]:
                ep = pos["sl"] * 0.9995; reason = "sl"
            elif price >= pos["tp"]:
                ep = pos["tp"] * 0.9995; reason = "tp"
            elif timed_out:
                ep = price;              reason = "timeout"
        else:
            liq_p = pos["entry"] * (1 + liq_dist)
            if price >= liq_p:
                ep = liq_p;              reason = "liq"
            elif price >= pos["sl"]:
                ep = pos["sl"] * 1.0005; reason = "sl"
            elif price <= pos["tp"]:
                ep = pos["tp"] * 1.0005; reason = "tp"
            elif timed_out:
                ep = price;              reason = "timeout"

        if ep is not None:
            close_position(state, pos, ep, reason, csv_path)
            to_close.append(pos)

    for p in to_close:
        state["positions"].remove(p)

    # ── Drawdown halt ─────────────────────────────────────────────────────
    dd_pct = (1 - state["equity"] / state["peak_equity"]) * 100
    if dd_pct >= 30:
        log.warning("DRAWDOWN GATE: %.1f%% from peak — no new entries", dd_pct)
        return

    # ── Cooldown after any close on this TF ──────────────────────────────
    lcb_current = state[lcb_key]
    if bar - lcb_current < cooldown_bars:
        return

    # ── Max concurrent positions for this symbol+tf ──────────────────────
    sym_tf_pos = [p for p in state["positions"]
                  if p["symbol"] == symbol and p.get("tf", "1h") == tf]
    if len(sym_tf_pos) >= max_pos:
        return

    # ── Entry ─────────────────────────────────────────────────────────────
    if sig["signal"] is None:
        return

    same_dir = [p for p in sym_tf_pos if p["side"] == sig["signal"]]
    if len(same_dir) >= max_pos:
        return

    new_pos = open_position(
        state, sig, symbol, leverage, risk_pct, rr,
        sl_atr_mult, max_margin_pct, max_pos, max_hold_bars, tf=tf,
    )
    if new_pos:
        state["positions"].append(new_pos)


def process_tick(state, symbol, leverage, risk_pct, rr, sl_atr_mult,
                 max_hold_bars, cooldown_bars, max_margin_pct, max_pos, csv_path):
    """Run one 1h tick for the given symbol."""
    _process_tick(
        state=state, symbol=symbol, tf="1h", leverage=leverage,
        risk_pct=risk_pct, rr=rr, sl_atr_mult=sl_atr_mult,
        max_hold_bars=max_hold_bars, cooldown_bars=cooldown_bars,
        max_margin_pct=max_margin_pct, max_pos=max_pos,
        csv_path=csv_path, cfg=STRATEGY_1H,
    )


def process_tick_4h(state, symbol, leverage, risk_pct, rr, sl_atr_mult,
                    max_hold_bars, cooldown_bars, max_margin_pct, max_pos, csv_path):
    """Run one 4h tick for the given symbol (BTC/USDT or SOL/USDT)."""
    _process_tick(
        state=state, symbol=symbol, tf="4h", leverage=leverage,
        risk_pct=risk_pct, rr=rr, sl_atr_mult=sl_atr_mult,
        max_hold_bars=max_hold_bars, cooldown_bars=cooldown_bars,
        max_margin_pct=max_margin_pct, max_pos=max_pos,
        csv_path=csv_path, cfg=STRATEGY_4H,
    )


# ─────────────────────────────────────────────────────────────────────────── #
#  Dashboard                                                                    #
# ─────────────────────────────────────────────────────────────────────────── #

def print_dashboard(state: dict, symbols_1h: list[str],
                    symbols_4h: list[str], leverage: int) -> None:
    equity = state["equity"]
    peak   = state["peak_equity"]
    dd_pct = (1 - equity / peak) * 100
    trades = state["closed_trades"]
    wins   = state["wins"]
    wr     = (wins / trades * 100) if trades else 0
    pnl    = state["total_pnl"]
    gbp    = equity / GBP_TO_USDT

    all_syms = list(symbols_1h) + [f"{s} [4h]" for s in symbols_4h]

    print("\n" + "=" * 65)
    print(f"  HAWK PAPER TRADER  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Leverage: {leverage}x  |  Symbols: {', '.join(all_syms)}")
    print("=" * 65)
    print(f"  Equity        : ${equity:>10.2f}  (GBP {gbp:.2f})")
    print(f"  Peak equity   : ${peak:>10.2f}")
    print(f"  Drawdown      : {dd_pct:>9.1f}%")
    print(f"  Total PnL     : ${pnl:>+10.2f}")
    print(f"  Trades        : {trades}  |  Wins: {wins}  |  WR: {wr:.1f}%")
    print(f"  Funding paid  : ${state['funding_paid']:>10.2f}")
    print(f"  Liquidations  : {state['liqs']}")
    print(f"  1h ticks      : {state['bar_count']}  |  4h ticks: {state['bar_count_4h']}")

    open_pos = state["positions"]
    if open_pos:
        print(f"\n  Open positions ({len(open_pos)}):")
        for pos in open_pos:
            try:
                cur = fetch_current_price(pos["symbol"])
                unreal = ((cur - pos["entry"]) if pos["side"] == "long"
                          else (pos["entry"] - cur)) * pos["qty"]
            except Exception:
                cur = pos["entry"]; unreal = 0.0
            tf_tag = pos.get("tf", "1h")
            print(f"    {pos['symbol']} [{tf_tag}] {pos['side'].upper():5}  "
                  f"entry={pos['entry']:.4f}  cur={cur:.4f}  "
                  f"SL={pos['sl']:.4f}  TP={pos['tp']:.4f}  "
                  f"unreal={unreal:+.2f}")
    else:
        print("\n  No open positions.")
    print("=" * 65)


# ─────────────────────────────────────────────────────────────────────────── #
#  Timing helpers                                                               #
# ─────────────────────────────────────────────────────────────────────────── #

def seconds_until_next_hour_candle() -> float:
    """Wait until :01 past the next hour (candle close + 1 min buffer)."""
    now = datetime.now(timezone.utc)
    mins_past  = now.minute
    secs_past  = now.second
    wait = (60 - mins_past) * 60 - secs_past + 60  # next :00 + 1 min buffer
    return max(wait, 10)


def is_4h_boundary() -> bool:
    """True at hours 0, 4, 8, 12, 16, 20 UTC (4h candle close boundaries)."""
    return datetime.now(timezone.utc).hour % 4 == 0


def calc_max_pos(leverage: int, risk_pct: float, sl_atr_mult: float,
                 max_margin_pct: float) -> int:
    sl_pct_est       = sl_atr_mult * 0.0066
    margin_per_trade = risk_pct / 100 / (sl_pct_est * leverage)
    if margin_per_trade < max_margin_pct:
        return min(3, max(1, int(max_margin_pct / margin_per_trade)))
    return 1


# ─────────────────────────────────────────────────────────────────────────── #
#  Main                                                                         #
# ─────────────────────────────────────────────────────────────────────────── #

def main() -> None:
    parser = argparse.ArgumentParser(description="HAWK Paper Trader — Multi-TF")
    parser.add_argument("--symbols",     nargs="+", default=["ETH/USDT"],
                        help="1h strategy symbols (default: ETH/USDT)")
    parser.add_argument("--4h-symbols",  nargs="+", default=[],
                        dest="symbols_4h",
                        help="4h strategy symbols (e.g. BTC/USDT SOL/USDT)")
    parser.add_argument("--leverage",    type=int,   default=10)
    parser.add_argument("--capital",     type=float, default=500.0,
                        help="Starting capital in GBP (default: 500)")
    parser.add_argument("--risk-pct",    type=float, default=1.5)
    parser.add_argument("--state-file",  default="logs/hawk_paper_state.json")
    parser.add_argument("--trade-log",   default="logs/hawk_paper_trades.csv")
    parser.add_argument("--run-once",    action="store_true",
                        help="Run one tick for all symbols then exit (for testing)")
    args = parser.parse_args()

    os.makedirs("logs", exist_ok=True)

    symbols_1h  = args.symbols
    symbols_4h  = args.symbols_4h
    leverage    = args.leverage
    risk_pct    = args.risk_pct
    max_margin  = 0.60
    max_pos_1h  = calc_max_pos(leverage, risk_pct,
                                STRATEGY_1H["sl_atr_mult"], max_margin)
    max_pos_4h  = calc_max_pos(leverage, risk_pct,
                                STRATEGY_4H["sl_atr_mult"], max_margin)

    initial_usdt = args.capital * GBP_TO_USDT
    state = load_state(args.state_file, initial_usdt)

    log.info("=" * 65)
    log.info("  HAWK PAPER TRADER  (multi-timeframe)")
    log.info("  1h symbols : %s  (max %d concurrent each)",
             symbols_1h, max_pos_1h)
    log.info("  4h symbols : %s  (max %d concurrent each)",
             symbols_4h or "(none)", max_pos_4h)
    log.info("  Leverage   : %dx  |  Risk/trade: %.1f%%  |  Capital: GBP %.0f",
             leverage, risk_pct, args.capital)
    log.info("  Global margin cap: %.0f%%  (shared across all positions)",
             max_margin * 100)
    log.info("  1h strategy: ch=%d  SL=%.1fx ATR  TP=%.1f:1  hold=%dh",
             STRATEGY_1H["channel_n"], STRATEGY_1H["sl_atr_mult"],
             STRATEGY_1H["rr"], STRATEGY_1H["max_hold_bars"])
    if symbols_4h:
        log.info("  4h strategy: ch=%d  SL=%.1fx ATR  TP=%.1f:1  hold=%dh",
                 STRATEGY_4H["channel_n"], STRATEGY_4H["sl_atr_mult"],
                 STRATEGY_4H["rr"], STRATEGY_4H["max_hold_bars"] * 4)
    log.info("=" * 65)

    def run_1h_ticks():
        for sym in symbols_1h:
            process_tick(
                state, sym, leverage, risk_pct,
                STRATEGY_1H["rr"], STRATEGY_1H["sl_atr_mult"],
                STRATEGY_1H["max_hold_bars"], 1, max_margin, max_pos_1h,
                args.trade_log,
            )

    def run_4h_ticks():
        for sym in symbols_4h:
            process_tick_4h(
                state, sym, leverage, risk_pct,
                STRATEGY_4H["rr"], STRATEGY_4H["sl_atr_mult"],
                STRATEGY_4H["max_hold_bars"], 1, max_margin, max_pos_4h,
                args.trade_log,
            )

    def run_all_and_save():
        run_1h_ticks()
        if symbols_4h and is_4h_boundary():
            run_4h_ticks()
        print_dashboard(state, symbols_1h, symbols_4h, leverage)
        save_state(args.state_file, state)

    def run_all_once():
        """For --run-once: run every strategy regardless of time."""
        run_1h_ticks()
        if symbols_4h:
            run_4h_ticks()
        print_dashboard(state, symbols_1h, symbols_4h, leverage)
        save_state(args.state_file, state)

    if args.run_once:
        run_all_once()
        return

    # ── Continuous loop ───────────────────────────────────────────────────
    # Runs every hour at :01 UTC. At 4h boundaries (00:01, 04:01, 08:01, …)
    # also fires the 4h strategy tick.
    log.info("Running first tick immediately ...")
    run_all_once()   # always run all strategies on startup

    while True:
        wait = seconds_until_next_hour_candle()
        next_run_ts = datetime.now(timezone.utc).timestamp() + wait
        log.info("Next 1h tick in %.0f min  (at %s UTC)%s",
                 wait / 60,
                 datetime.fromtimestamp(next_run_ts, tz=timezone.utc).strftime("%H:%M"),
                 "  [4h tick also due]" if (
                     symbols_4h and
                     datetime.fromtimestamp(next_run_ts, tz=timezone.utc).hour % 4 == 0
                 ) else "")
        time.sleep(wait)
        run_all_and_save()


if __name__ == "__main__":
    main()
