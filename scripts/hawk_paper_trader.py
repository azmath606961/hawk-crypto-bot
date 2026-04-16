"""
HAWK Paper Trader — Live Paper Trading
=======================================
Runs the HAWK-ACTIVE strategy (8-bar channel breakout + EMA20/50 filter)
against live Binance 1h candles. No real orders are placed.

Usage:
    python scripts/hawk_paper_trader.py                     # ETH/USDT, 10x, GBP 500
    python scripts/hawk_paper_trader.py --symbol BTC/USDT
    python scripts/hawk_paper_trader.py --leverage 20 --capital 1000
    python scripts/hawk_paper_trader.py --symbols ETH/USDT BTC/USDT SOL/USDT

State is saved to logs/hawk_paper_state.json after every tick.
Trades are logged to logs/hawk_paper_trades.csv.

Tick: runs once at the start, then every hour after the candle closes.
      (waits until :01 past each hour so the 1h candle is confirmed)
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
TAKER_FEE      = 0.0005     # 0.05% futures taker fee
FUNDING_8H     = 0.0001     # 0.01% per 8h


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
    return s.ewm(span=p, adjust=False).mean()

def _atr(h: pd.Series, l: pd.Series, c: pd.Series, p: int = 14) -> pd.Series:
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()


# ─────────────────────────────────────────────────────────────────────────── #
#  Signal logic — mirrors backtest exactly                                      #
# ─────────────────────────────────────────────────────────────────────────── #

def compute_signals(df: pd.DataFrame, channel_n: int = 8,
                    ema_fast: int = 20, ema_slow: int = 50) -> pd.DataFrame:
    """Attach indicator columns. Returns the last row as a dict."""
    df = df.copy()
    df["ema_f"]    = _ema(df["close"], ema_fast)
    df["ema_s"]    = _ema(df["close"], ema_slow)
    df["atr"]      = _atr(df["high"], df["low"], df["close"], 14)
    df["chan_high"] = df["high"].rolling(channel_n).max().shift(1)
    df["chan_low"]  = df["low"].rolling(channel_n).min().shift(1)
    return df


def get_signal(df: pd.DataFrame) -> dict:
    """
    Return signal dict from the LAST CONFIRMED candle (iloc[-2]).
    We always use the previous closed candle so there's no look-ahead.
    """
    row = df.iloc[-2]   # last CLOSED 1h candle
    c   = float(row["close"])
    etf = float(row["ema_f"])
    ets = float(row["ema_s"])
    atr = float(row["atr"])
    ch  = float(row["chan_high"])
    cl  = float(row["chan_low"])

    bull = etf > ets
    bear = etf < ets

    signal = None
    if bull and c > ch:
        signal = "long"
    elif bear and c < cl:
        signal = "short"

    return {
        "signal":     signal,
        "price":      c,
        "ema_f":      etf,
        "ema_s":      ets,
        "atr":        atr,
        "chan_high":  ch,
        "chan_low":   cl,
        "bull":       bull,
        "regime":     "BULL" if bull else ("BEAR" if bear else "FLAT"),
        "ts":         df.index[-2].isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────── #
#  State persistence                                                            #
# ─────────────────────────────────────────────────────────────────────────── #

def load_state(path: str, initial_usdt: float) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            s = json.load(f)
        log.info("Loaded existing state — equity $%.2f, %d open positions",
                 s["equity"], len(s["positions"]))
        return s
    return {
        "equity":       initial_usdt,
        "peak_equity":  initial_usdt,
        "positions":    [],          # list of position dicts
        "closed_trades": 0,
        "wins":         0,
        "total_pnl":    0.0,
        "funding_paid": 0.0,
        "liqs":         0,
        "bar_count":    0,           # total hourly ticks processed
        "last_close_bar": -999,
        "started":      datetime.now(timezone.utc).isoformat(),
    }


def save_state(path: str, state: dict) -> None:
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def log_trade(csv_path: str, trade: dict) -> None:
    new_file = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="") as f:
        cols = ["ts_open", "ts_close", "symbol", "side", "entry", "exit",
                "qty", "notional", "pnl_usdt", "reason", "equity_after"]
        if new_file:
            f.write(",".join(cols) + "\n")
        row = [str(trade.get(c, "")) for c in cols]
        f.write(",".join(row) + "\n")


# ─────────────────────────────────────────────────────────────────────────── #
#  Position sizing — identical formula to backtest                              #
# ─────────────────────────────────────────────────────────────────────────── #

def open_position(state: dict, sig: dict, symbol: str, leverage: int,
                  risk_pct: float, rr: float, sl_atr_mult: float,
                  max_margin_pct: float, max_pos: int) -> dict | None:
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

    # Check margin budget
    used_margin = sum(p["margin"] for p in state["positions"] if p["symbol"] == symbol)
    avail = equity * max_margin_pct - used_margin
    if margin + fee > avail:
        scale    = avail / (margin + fee)
        qty     *= scale
        notional = qty * price
        margin   = notional / leverage
        fee      = notional * TAKER_FEE

    if notional < 5.0:   # minimum position size
        return None

    if side == "long":
        sl = price - sl_dist
        tp = price + rr * sl_dist
    else:
        sl = price + sl_dist
        tp = price - rr * sl_dist

    state["equity"] -= fee

    pos = {
        "symbol":   symbol,
        "side":     side,
        "entry":    price,
        "qty":      qty,
        "notional": notional,
        "margin":   margin,
        "sl":       sl,
        "tp":       tp,
        "sl_dist":  sl_dist,
        "ts_open":  sig["ts"],
        "bar_in":   state["bar_count"],
        "fee_open": fee,
    }
    log.info("[PAPER OPEN ] %-10s %5s  entry=%.4f  SL=%.4f  TP=%.4f  qty=%.4f  notional=$%.0f",
             symbol, side.upper(), price, sl, tp, qty, notional)
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

    state["equity"]     += pnl
    state["total_pnl"]  += pnl
    state["closed_trades"] += 1
    if pnl > 0:
        state["wins"] += 1
    if reason == "liq":
        state["liqs"] += 1
    if state["equity"] > state["peak_equity"]:
        state["peak_equity"] = state["equity"]

    state["last_close_bar"] = state["bar_count"]

    trade = {
        "ts_open":     pos["ts_open"],
        "ts_close":    datetime.now(timezone.utc).isoformat(),
        "symbol":      pos["symbol"],
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
    log.info("[PAPER CLOSE] %-10s %5s  exit=%.4f  PnL=%+.2f USDT  (%s)  [%s]",
             pos["symbol"], pos["side"].upper(), exit_price, pnl, win_str, reason.upper())


# ─────────────────────────────────────────────────────────────────────────── #
#  Per-tick logic                                                               #
# ─────────────────────────────────────────────────────────────────────────── #

def process_tick(
    state:          dict,
    symbol:         str,
    leverage:       int,
    risk_pct:       float,
    rr:             float,
    sl_atr_mult:    float,
    max_hold_bars:  int,
    cooldown_bars:  int,
    max_margin_pct: float,
    max_pos:        int,
    csv_path:       str,
) -> None:
    state["bar_count"] += 1
    bar = state["bar_count"]

    # ── Fetch live candles ────────────────────────────────────────────────
    try:
        df_raw  = fetch_ohlcv(symbol, "1h", limit=200)
        df      = compute_signals(df_raw)
        sig     = get_signal(df)
        price   = float(df_raw["close"].iloc[-1])   # current (possibly open) bar
    except Exception as exc:
        log.error("Data fetch failed for %s: %s", symbol, exc)
        return

    log.info("Tick #%d | %s | $%.2f | Regime: %s | Signal: %s",
             bar, symbol, price, sig["regime"], sig["signal"] or "none")

    # ── Funding every 8 ticks ─────────────────────────────────────────────
    if bar % 8 == 0:
        for pos in state["positions"]:
            if pos["symbol"] != symbol:
                continue
            fund = pos["notional"] * FUNDING_8H
            if pos["side"] == "long":
                state["equity"] -= fund
                state["funding_paid"] += fund
            else:
                state["equity"] += fund

    # ── Check / close open positions ──────────────────────────────────────
    liq_dist  = 1 / leverage - 0.005
    to_close  = []

    for pos in state["positions"]:
        if pos["symbol"] != symbol:
            continue
        ep = None; reason = ""

        if pos["side"] == "long":
            liq_p = pos["entry"] * (1 - liq_dist)
            if price <= liq_p:
                ep = liq_p; reason = "liq"
            elif price <= pos["sl"]:
                ep = pos["sl"] * 0.9995; reason = "sl"
            elif price >= pos["tp"]:
                ep = pos["tp"] * 0.9995; reason = "tp"
            elif bar - pos["bar_in"] >= max_hold_bars:
                ep = price; reason = "timeout"
        else:
            liq_p = pos["entry"] * (1 + liq_dist)
            if price >= liq_p:
                ep = liq_p; reason = "liq"
            elif price >= pos["sl"]:
                ep = pos["sl"] * 1.0005; reason = "sl"
            elif price <= pos["tp"]:
                ep = pos["tp"] * 1.0005; reason = "tp"
            elif bar - pos["bar_in"] >= max_hold_bars:
                ep = price; reason = "timeout"

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

    # ── Cooldown after close ──────────────────────────────────────────────
    if bar - state["last_close_bar"] < cooldown_bars:
        return

    # ── Check concurrent position count for this symbol ──────────────────
    sym_positions = [p for p in state["positions"] if p["symbol"] == symbol]
    if len(sym_positions) >= max_pos:
        return

    # ── Entry ─────────────────────────────────────────────────────────────
    if sig["signal"] is None:
        return

    # Don't add another position in the same direction if already in it
    same_dir = [p for p in sym_positions if p["side"] == sig["signal"]]
    if len(same_dir) >= max_pos:
        return

    new_pos = open_position(
        state, sig, symbol, leverage, risk_pct, rr,
        sl_atr_mult, max_margin_pct, max_pos,
    )
    if new_pos:
        state["positions"].append(new_pos)


# ─────────────────────────────────────────────────────────────────────────── #
#  Dashboard                                                                    #
# ─────────────────────────────────────────────────────────────────────────── #

def print_dashboard(state: dict, symbols: list[str], leverage: int) -> None:
    equity = state["equity"]
    peak   = state["peak_equity"]
    init   = equity  # we don't store initial, use first peak as proxy
    dd_pct = (1 - equity / peak) * 100
    trades = state["closed_trades"]
    wins   = state["wins"]
    wr     = (wins / trades * 100) if trades else 0
    pnl    = state["total_pnl"]
    gbp    = equity / GBP_TO_USDT

    print("\n" + "=" * 60)
    print(f"  HAWK PAPER TRADER  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Leverage: {leverage}x  |  Symbols: {', '.join(symbols)}")
    print("=" * 60)
    print(f"  Equity        : ${equity:>10.2f}  (GBP {gbp:.2f})")
    print(f"  Peak equity   : ${peak:>10.2f}")
    print(f"  Drawdown      : {dd_pct:>9.1f}%")
    print(f"  Total PnL     : ${pnl:>+10.2f}")
    print(f"  Trades        : {trades}  |  Wins: {wins}  |  WR: {wr:.1f}%")
    print(f"  Funding paid  : ${state['funding_paid']:>10.2f}")
    print(f"  Liquidations  : {state['liqs']}")
    print(f"  Ticks run     : {state['bar_count']}")

    open_pos = state["positions"]
    if open_pos:
        print(f"\n  Open positions ({len(open_pos)}):")
        for pos in open_pos:
            # Fetch current price to show unrealized PnL
            try:
                cur = fetch_current_price(pos["symbol"])
                if pos["side"] == "long":
                    unreal = (cur - pos["entry"]) * pos["qty"]
                else:
                    unreal = (pos["entry"] - cur) * pos["qty"]
            except Exception:
                cur = pos["entry"]; unreal = 0.0
            print(f"    {pos['symbol']} {pos['side'].upper():5}  "
                  f"entry={pos['entry']:.4f}  cur={cur:.4f}  "
                  f"SL={pos['sl']:.4f}  TP={pos['tp']:.4f}  "
                  f"unreal={unreal:+.2f}")
    else:
        print("\n  No open positions.")
    print("=" * 60)


# ─────────────────────────────────────────────────────────────────────────── #
#  Sleep until next candle close  (+1 min buffer)                              #
# ─────────────────────────────────────────────────────────────────────────── #

def seconds_until_next_hour_candle() -> float:
    now = datetime.now(timezone.utc)
    # Next candle closes at the next :00 — wait until :01 to be safe
    minutes_past = now.minute
    seconds_past = now.second
    wait = (60 - minutes_past) * 60 - seconds_past + 60  # next hour + 1 min
    return max(wait, 10)


# ─────────────────────────────────────────────────────────────────────────── #
#  Max concurrent positions — same derivation as backtest                      #
# ─────────────────────────────────────────────────────────────────────────── #

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
    parser = argparse.ArgumentParser(description="HAWK Paper Trader")
    parser.add_argument("--symbols", nargs="+", default=["ETH/USDT"],
                        help="Symbols to trade (default: ETH/USDT)")
    parser.add_argument("--leverage",   type=int,   default=10,
                        help="Leverage (default: 10)")
    parser.add_argument("--capital",    type=float, default=500.0,
                        help="Starting capital in GBP (default: 500)")
    parser.add_argument("--risk-pct",   type=float, default=1.5,
                        help="% of equity to risk per trade (default: 1.5)")
    parser.add_argument("--rr",         type=float, default=2.0,
                        help="Risk:reward ratio (default: 2.0)")
    parser.add_argument("--channel-n",  type=int,   default=8,
                        help="Channel breakout lookback bars (default: 8)")
    parser.add_argument("--ema-fast",   type=int,   default=20)
    parser.add_argument("--ema-slow",   type=int,   default=50)
    parser.add_argument("--sl-mult",    type=float, default=1.5,
                        help="SL = sl_mult × ATR (default: 1.5)")
    parser.add_argument("--max-hold",   type=int,   default=30,
                        help="Max hold bars before timeout exit (default: 30)")
    parser.add_argument("--state-file", default="logs/hawk_paper_state.json")
    parser.add_argument("--trade-log",  default="logs/hawk_paper_trades.csv")
    parser.add_argument("--run-once",   action="store_true",
                        help="Run one tick immediately then exit (for testing)")
    args = parser.parse_args()

    # Ensure logs directory exists
    os.makedirs("logs", exist_ok=True)

    symbols     = args.symbols
    leverage    = args.leverage
    risk_pct    = args.risk_pct
    rr          = args.rr
    sl_mult     = args.sl_mult
    max_hold    = args.max_hold
    max_margin  = 0.60
    max_pos     = calc_max_pos(leverage, risk_pct, sl_mult, max_margin)

    initial_usdt = args.capital * GBP_TO_USDT
    state = load_state(args.state_file, initial_usdt)

    log.info("=" * 60)
    log.info("  HAWK PAPER TRADER starting")
    log.info("  Symbols   : %s", symbols)
    log.info("  Leverage  : %dx  (max %d concurrent positions)", leverage, max_pos)
    log.info("  Capital   : GBP %.0f  ($%.0f USDT)", args.capital, initial_usdt)
    log.info("  Risk/trade: %.1f%%  |  RR: %.1f  |  SL: %.1fx ATR", risk_pct, rr, sl_mult)
    log.info("  State file: %s", args.state_file)
    log.info("  Trade log : %s", args.trade_log)
    log.info("=" * 60)

    def run_tick():
        for sym in symbols:
            process_tick(
                state       = state,
                symbol      = sym,
                leverage    = leverage,
                risk_pct    = risk_pct,
                rr          = rr,
                sl_atr_mult = sl_mult,
                max_hold_bars  = max_hold,
                cooldown_bars  = 1,
                max_margin_pct = max_margin,
                max_pos     = max_pos,
                csv_path    = args.trade_log,
            )
        print_dashboard(state, symbols, leverage)
        save_state(args.state_file, state)

    if args.run_once:
        run_tick()
        return

    # Continuous loop — run at start, then every hour
    log.info("Running first tick immediately...")
    run_tick()

    while True:
        wait = seconds_until_next_hour_candle()
        next_run = datetime.now(timezone.utc)
        log.info("Next tick in %.0f minutes (at %s UTC)",
                 wait / 60,
                 datetime.fromtimestamp(
                     next_run.timestamp() + wait, tz=timezone.utc
                 ).strftime("%H:%M"))
        time.sleep(wait)
        run_tick()


if __name__ == "__main__":
    main()
