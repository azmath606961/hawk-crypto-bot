"""
HAWK Trader — Unified Paper & Live Trading (HAWK v6)
=====================================================
Identical HAWK v6 strategy in both modes. Only order execution differs.

  --paper    : Simulate all fills locally. No API key required.
               Uses Binance public REST for price data.
  (no flag)  : Live mode. Places real Binance USD-M Futures orders.
               Requires env vars: BINANCE_API_KEY, BINANCE_API_SECRET
  --testnet  : Live on Binance Futures testnet (safe test before going live).
               Requires testnet API keys from testnet.binancefuture.com

Usage:
    python scripts/hawk_trader.py --paper                            # paper, ETH 1h
    python scripts/hawk_trader.py --paper --run-once                 # single test tick
    python scripts/hawk_trader.py --testnet                          # live on testnet
    python scripts/hawk_trader.py                                    # live (real money)
    python scripts/hawk_trader.py --paper --symbols ETH/USDT XRP/USDT
    python scripts/hawk_trader.py --paper --4h-symbols BTC/USDT BNB/USDT ADA/USDT

Strategy parity guarantee:
    Signal logic, position sizing, SL/TP calculation, and risk rules are
    implemented once and shared across both modes. Only order execution
    (simulate vs place real order) branches at the executor layer.
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
#  Constants — identical to backtest (do not modify)                           #
# ─────────────────────────────────────────────────────────────────────────── #

GBP_TO_USDT  = 1.27
TAKER_FEE    = 0.0004   # 0.04% Binance Futures taker fee (matches backtest)
FUNDING_8H   = 0.0001   # 0.01% per 8h funding rate

# Confirmed optimal params from 25,920-combo backtest (Apr 2024 – Apr 2026)
STRATEGY_1H = dict(
    channel_n     = 8,
    ema_fast      = 20,
    ema_slow      = 50,
    sl_atr_mult   = 1.5,
    rr            = 2.0,
    max_hold_bars = 30,     # 30h max hold
    bar_secs      = 3600,
    funding_bars  = 8,
    interval      = "1h",
    adx_min       = 20.0,   # v6: only trade trending markets (ADX≥20)
)
STRATEGY_4H = dict(
    channel_n     = 12,
    ema_fast      = 20,
    ema_slow      = 50,
    sl_atr_mult   = 1.5,
    rr            = 2.0,
    max_hold_bars = 12,     # 48h max hold
    bar_secs      = 14400,
    funding_bars  = 2,
    interval      = "4h",
)

# ─────────────────────────────────────────────────────────────────────────── #
#  Logging                                                                      #
# ─────────────────────────────────────────────────────────────────────────── #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hawk")

# ─────────────────────────────────────────────────────────────────────────── #
#  Live Executor — Binance USD-M Futures via ccxt                              #
#  Paper mode passes executor=None everywhere; live passes a LiveExecutor.     #
# ─────────────────────────────────────────────────────────────────────────── #

class LiveExecutor:
    """
    Thin ccxt wrapper for Binance USD-M Futures.
    Handles leverage setup, market entry, SL/TP stop orders, and position sync.
    All strategy logic lives outside this class — it is pure execution only.
    """

    def __init__(self, testnet: bool = False) -> None:
        import ccxt  # imported here so paper mode has no ccxt dep at runtime

        api_key    = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
        if not api_key or not api_secret:
            raise RuntimeError(
                "\nBINANCE_API_KEY and BINANCE_API_SECRET env vars are required for live mode.\n"
                "Set them in your shell:\n"
                "  export BINANCE_API_KEY=your_key\n"
                "  export BINANCE_API_SECRET=your_secret\n"
                "Or use --paper for paper trading (no API key required)."
            )

        self._ex = ccxt.binanceusdm({
            "apiKey":          api_key,
            "secret":          api_secret,
            "enableRateLimit": True,
        })
        if testnet:
            self._ex.set_sandbox_mode(True)
            log.info("LiveExecutor: Binance Futures TESTNET")
        else:
            log.warning("LiveExecutor: Binance Futures LIVE — real orders will be placed")

        self._leverage_set: set[str] = set()   # symbols already configured

    # ── Leverage ──────────────────────────────────────────────────────────

    def ensure_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage once per symbol per session."""
        if symbol in self._leverage_set:
            return
        try:
            self._ex.set_leverage(leverage, symbol)
            self._ex.set_margin_mode("isolated", symbol)
            log.info("Leverage %dx (isolated) set for %s", leverage, symbol)
        except Exception as exc:
            log.warning("Could not set leverage/margin for %s: %s", symbol, exc)
        self._leverage_set.add(symbol)

    # ── Order placement ───────────────────────────────────────────────────

    def open_trade(
        self, symbol: str, side: str, qty: float,
        sl_price: float, tp_price: float,
    ) -> float:
        """
        Place market entry + SL/TP stop orders.
        Returns actual average fill price (used to recalculate SL/TP offsets).
        """
        entry_side = "buy"  if side == "long" else "sell"
        close_side = "sell" if side == "long" else "buy"
        qty = float(f"{qty:.4f}")   # round to 4dp to avoid precision errors

        # Market entry
        entry_order = self._ex.create_order(symbol, "MARKET", entry_side, qty)
        fill = float(entry_order.get("average") or entry_order.get("price") or 0)
        log.info("[LIVE OPEN ] %s %s  qty=%.4f  fill=%.4f", symbol, side.upper(), qty, fill)

        # SL — stop market, reduce-only
        self._ex.create_order(symbol, "STOP_MARKET", close_side, qty, None, {
            "stopPrice":  sl_price,
            "reduceOnly": True,
        })
        # TP — take profit market, reduce-only
        self._ex.create_order(symbol, "TAKE_PROFIT_MARKET", close_side, qty, None, {
            "stopPrice":  tp_price,
            "reduceOnly": True,
        })
        log.info("[LIVE SL/TP] %s  SL=%.4f  TP=%.4f", symbol, sl_price, tp_price)
        return fill

    def close_trade(self, symbol: str, side: str, qty: float) -> float:
        """
        Market close + cancel remaining SL/TP orders (timeout / manual close).
        Returns fill price.
        """
        close_side = "sell" if side == "long" else "buy"
        qty = float(f"{qty:.4f}")
        try:
            self._ex.cancel_all_orders(symbol)
        except Exception as exc:
            log.warning("cancel_all_orders failed for %s: %s", symbol, exc)
        order = self._ex.create_order(symbol, "MARKET", close_side, qty, None, {
            "reduceOnly": True,
        })
        fill = float(order.get("average") or order.get("price") or 0)
        log.info("[LIVE CLOSE] %s %s  qty=%.4f  fill=%.4f", symbol, side.upper(), qty, fill)
        return fill

    # ── Position state ────────────────────────────────────────────────────

    def fetch_position(self, symbol: str) -> dict | None:
        """Current open position for symbol from exchange, or None."""
        try:
            positions = self._ex.fetch_positions([symbol])
            for p in positions:
                contracts = float(p.get("contracts") or p.get("info", {}).get("positionAmt", 0) or 0)
                if abs(contracts) > 0:
                    return p
        except Exception as exc:
            log.warning("fetch_position failed for %s: %s", symbol, exc)
        return None

    def fetch_last_fill_price(self, symbol: str) -> float | None:
        """Price of the most recent fill for symbol (used after SL/TP fires)."""
        try:
            trades = self._ex.fetch_my_trades(symbol, limit=5)
            if trades:
                return float(trades[-1]["price"])
        except Exception as exc:
            log.warning("fetch_last_fill_price failed for %s: %s", symbol, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────── #
#  Binance public data — no API key required                                   #
# ─────────────────────────────────────────────────────────────────────────── #

BINANCE_KLINE = "https://api.binance.com/api/v3/klines"

def fetch_ohlcv(symbol: str, interval: str = "1h", limit: int = 200) -> pd.DataFrame:
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
#  Indicators — identical to backtest                                           #
# ─────────────────────────────────────────────────────────────────────────── #

def _ema(s: pd.Series, p: int) -> pd.Series:
    # Wilder EMA (alpha=1/p) — matches hawk_comprehensive_backtest.py exactly.
    # Standard EMA uses alpha=2/(p+1) which gives different crossovers.
    return s.ewm(alpha=1 / p, adjust=False).mean()

def _atr(h: pd.Series, l: pd.Series, c: pd.Series, p: int = 14) -> pd.Series:
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / p, adjust=False).mean()  # Wilder smoothing

def _adx(h: pd.Series, l: pd.Series, c: pd.Series, p: int = 14) -> pd.Series:
    """ADX(14) via Wilder smoothing. ADX>=20 = trending, <20 = choppy."""
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
#  Signal logic — identical to backtest                                         #
# ─────────────────────────────────────────────────────────────────────────── #

def compute_signals(df: pd.DataFrame, channel_n: int = 8,
                    ema_fast: int = 20, ema_slow: int = 50,
                    compute_adx: bool = False) -> pd.DataFrame:
    df = df.copy()
    df["ema_f"]     = _ema(df["close"], ema_fast)
    df["ema_s"]     = _ema(df["close"], ema_slow)
    df["atr"]       = _atr(df["high"], df["low"], df["close"], 14)
    df["chan_high"]  = df["high"].rolling(channel_n).max().shift(1)
    df["chan_low"]   = df["low"].rolling(channel_n).min().shift(1)
    if compute_adx:
        df["adx"] = _adx(df["high"], df["low"], df["close"], 14)
    return df


def get_signal(df: pd.DataFrame, adx_min: float | None = None) -> dict:
    """
    Signal from last confirmed candle (iloc[-2]).
    adx_min: if set, skip entries when ADX < adx_min (ranging market gate).
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
        "signal":   signal,
        "price":    c,
        "ema_f":    etf,
        "ema_s":    ets,
        "atr":      atr,
        "chan_high": ch,
        "chan_low":  cl,
        "adx":      adx_val,
        "adx_ok":   adx_ok,
        "bull":     bull,
        "regime":   "BULL" if bull else ("BEAR" if bear else "FLAT"),
        "ts":       df.index[-2].isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────── #
#  State persistence                                                            #
# ─────────────────────────────────────────────────────────────────────────── #

def load_state(path: str, initial_usdt: float) -> dict:
    if os.path.exists(path):
        s = json.load(open(path))
        s.setdefault("bar_count_4h",     0)
        s.setdefault("last_close_bar_4h", -999)
        log.info("Loaded state — equity $%.2f, %d open positions",
                 s["equity"], len(s["positions"]))
        return s
    return {
        "equity":             initial_usdt,
        "peak_equity":        initial_usdt,
        "positions":          [],
        "closed_trades":      0,
        "wins":               0,
        "total_pnl":          0.0,
        "funding_paid":       0.0,
        "liqs":               0,
        "bar_count":          0,
        "bar_count_4h":       0,
        "last_close_bar":     -999,
        "last_close_bar_4h":  -999,
        "started":            datetime.now(timezone.utc).isoformat(),
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
        f.write(",".join([str(trade.get(c, "")) for c in cols]) + "\n")


# ─────────────────────────────────────────────────────────────────────────── #
#  Position sizing — identical formula to backtest (leverage-independent)      #
# ─────────────────────────────────────────────────────────────────────────── #

def open_position(
    state: dict, sig: dict, symbol: str, leverage: int,
    risk_pct: float, rr: float, sl_atr_mult: float,
    max_margin_pct: float, max_pos: int,
    max_hold_bars: int, tf: str = "1h",
    executor: LiveExecutor | None = None,
) -> dict | None:
    """
    Compute position size and open a position.
    Paper mode (executor=None): updates state dict only.
    Live mode (executor set): calls executor.open_trade() to place real orders.
    """
    equity = state["equity"]
    side   = sig["signal"]
    price  = sig["price"]
    atr    = sig["atr"]

    sl_dist   = sl_atr_mult * atr
    if sl_dist <= 0:
        return None

    risk_usdt = equity * risk_pct / 100
    qty       = risk_usdt / sl_dist     # CORRECT: leverage-independent sizing
    notional  = qty * price
    margin    = notional / leverage
    fee       = notional * TAKER_FEE

    # Global 60% margin cap across all open positions
    used_margin = sum(p["margin"] for p in state["positions"])
    avail = equity * max_margin_pct - used_margin
    if margin + fee > avail:
        if avail <= 0:
            log.info("Global margin cap reached — skipping %s [%s]", symbol, tf)
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

    # ── Execute order ─────────────────────────────────────────────────────
    if executor is not None:
        executor.ensure_leverage(symbol, leverage)
        try:
            fill = executor.open_trade(symbol, side, qty, sl, tp)
            if fill > 0:
                # Recalculate SL/TP from actual fill price (slippage adjustment)
                price = fill
                sl = (price - sl_dist) if side == "long" else (price + sl_dist)
                tp = (price + rr * sl_dist) if side == "long" else (price - rr * sl_dist)
        except Exception as exc:
            log.error("open_trade failed for %s: %s — skipping", symbol, exc)
            return None
        mode = "LIVE"
    else:
        mode = "PAPER"

    state["equity"] -= fee

    bar_secs = STRATEGY_4H["bar_secs"] if tf == "4h" else STRATEGY_1H["bar_secs"]
    pos = {
        "symbol":        symbol,
        "tf":            tf,
        "side":          side,
        "entry":         price,
        "qty":           qty,
        "notional":      notional,
        "margin":        margin,
        "sl":            sl,
        "tp":            tp,
        "sl_dist":       sl_dist,
        "ts_open":       sig["ts"],
        "ts_open_epoch": time.time(),
        "max_hold_secs": max_hold_bars * bar_secs,
        "bar_in":        state["bar_count"] if tf == "1h" else state["bar_count_4h"],
        "fee_open":      fee,
        "live":          executor is not None,   # tag for state file clarity
    }
    log.info("[%s OPEN ] %-10s [%s] %5s  entry=%.4f  SL=%.4f  TP=%.4f  qty=%.4f  notional=$%.0f",
             mode, symbol, tf, side.upper(), price, sl, tp, qty, notional)
    return pos


def close_position(
    state: dict, pos: dict, exit_price: float,
    reason: str, csv_path: str,
    executor: LiveExecutor | None = None,
) -> None:
    """
    Close a position and update equity.
    Live mode + timeout: executor.close_trade() places a market close order.
    Live mode + SL/TP:   exchange already closed it — we just record the result.
    """
    if executor is not None and reason == "timeout":
        # Manually close position that exchange SL/TP didn't hit in time
        try:
            fill = executor.close_trade(pos["symbol"], pos["side"], pos["qty"])
            if fill > 0:
                exit_price = fill
        except Exception as exc:
            log.error("close_trade (timeout) failed for %s: %s", pos["symbol"], exc)

    qty = pos["qty"]
    raw_pnl = ((exit_price - pos["entry"]) if pos["side"] == "long"
               else (pos["entry"] - exit_price)) * qty
    fee  = qty * exit_price * TAKER_FEE
    pnl  = raw_pnl - fee

    state["equity"]        += pnl
    state["total_pnl"]     += pnl
    state["closed_trades"] += 1
    if pnl > 0:
        state["wins"] += 1
    if reason == "liq":
        state["liqs"] += 1
    if state["equity"] > state["peak_equity"]:
        state["peak_equity"] = state["equity"]

    tf = pos.get("tf", "1h")
    if tf == "4h":
        state["last_close_bar_4h"] = state["bar_count_4h"]
    else:
        state["last_close_bar"] = state["bar_count"]

    log_trade(csv_path, {
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
    })
    mode = "LIVE" if pos.get("live") else "PAPER"
    win  = "WIN " if pnl > 0 else "LOSS"
    log.info("[%s CLOSE] %-10s [%s] %5s  exit=%.4f  PnL=%+.2f USDT  (%s)  [%s]",
             mode, pos["symbol"], tf, pos["side"].upper(), exit_price, pnl, win, reason.upper())


# ─────────────────────────────────────────────────────────────────────────── #
#  Live mode: sync local state with exchange positions                          #
# ─────────────────────────────────────────────────────────────────────────── #

def sync_live_positions(
    state: dict, symbol: str, tf: str,
    executor: LiveExecutor, csv_path: str,
) -> None:
    """
    Detect positions closed by the exchange (SL/TP fired).
    Compares local tracked positions against exchange state.
    Called once per tick, before signal evaluation.
    """
    ex_pos   = executor.fetch_position(symbol)
    ex_qty   = 0.0
    ex_side  = None
    if ex_pos is not None:
        contracts = float(ex_pos.get("contracts") or
                          ex_pos.get("info", {}).get("positionAmt", 0) or 0)
        ex_qty  = abs(contracts)
        ex_side = "long" if contracts > 0 else "short"

    to_close = []
    for pos in state["positions"]:
        if pos["symbol"] != symbol or pos.get("tf", "1h") != tf:
            continue

        # Position still live on exchange?
        still_open = (
            ex_pos is not None
            and ex_side == pos["side"]
            and abs(ex_qty - pos["qty"]) / max(pos["qty"], 1e-9) < 0.10
        )
        if not still_open:
            # Exchange closed this position (SL or TP triggered)
            exit_price = executor.fetch_last_fill_price(symbol)
            if exit_price is None:
                exit_price = pos["sl"]
                reason = "sl"
            else:
                # Infer reason from where price closed relative to SL/TP
                if pos["side"] == "long":
                    reason = "tp" if exit_price >= pos["tp"] * 0.995 else "sl"
                else:
                    reason = "tp" if exit_price <= pos["tp"] * 1.005 else "sl"
            close_position(state, pos, exit_price, reason, csv_path, executor=executor)
            to_close.append(pos)

    for p in to_close:
        state["positions"].remove(p)


# ─────────────────────────────────────────────────────────────────────────── #
#  Per-tick logic — strategy is identical for paper and live                   #
# ─────────────────────────────────────────────────────────────────────────── #

def _process_tick(
    state:          dict,
    symbol:         str,
    tf:             str,
    leverage:       int,
    risk_pct:       float,
    rr:             float,
    sl_atr_mult:    float,
    max_hold_bars:  int,
    cooldown_bars:  int,
    max_margin_pct: float,
    max_pos:        int,
    csv_path:       str,
    cfg:            dict,
    executor:       LiveExecutor | None = None,
) -> None:
    # ── Bar counter ───────────────────────────────────────────────────────
    if tf == "4h":
        state["bar_count_4h"] += 1
        bar     = state["bar_count_4h"]
        lcb_key = "last_close_bar_4h"
    else:
        state["bar_count"] += 1
        bar     = state["bar_count"]
        lcb_key = "last_close_bar"

    # ── Live: sync with exchange before doing anything else ───────────────
    if executor is not None:
        sync_live_positions(state, symbol, tf, executor, csv_path)

    # ── Fetch market data ─────────────────────────────────────────────────
    adx_min = cfg.get("adx_min")
    try:
        df_raw = fetch_ohlcv(symbol, cfg["interval"], limit=200)
        df     = compute_signals(df_raw, cfg["channel_n"], cfg["ema_fast"],
                                 cfg["ema_slow"], compute_adx=(adx_min is not None))
        sig    = get_signal(df, adx_min=adx_min)
        price  = float(df_raw["close"].iloc[-1])
    except Exception as exc:
        log.error("Data fetch failed for %s [%s]: %s", symbol, tf, exc)
        return

    adx_str = (f"  ADX={sig['adx']:.1f}{'(ok)' if sig['adx_ok'] else '(low)'}"
               if sig["adx"] is not None else "")
    mode_tag = "LIVE" if executor else "PAPER"
    log.info("[%s] Tick #%d [%s] %s | $%.4f | %s | Signal: %s%s",
             mode_tag, bar, tf, symbol, price, sig["regime"], sig["signal"] or "none", adx_str)

    # ── Funding every N bars ──────────────────────────────────────────────
    if bar % cfg["funding_bars"] == 0:
        for pos in state["positions"]:
            if pos["symbol"] != symbol or pos.get("tf", "1h") != tf:
                continue
            fund = pos["notional"] * FUNDING_8H
            if pos["side"] == "long":
                state["equity"]       -= fund
                state["funding_paid"] += fund
            else:
                state["equity"] += fund

    # ── Check / close open positions ──────────────────────────────────────
    liq_dist = 1 / leverage - 0.005
    to_close = []

    for pos in state["positions"]:
        if pos["symbol"] != symbol or pos.get("tf", "1h") != tf:
            continue
        ep = None; reason = ""

        timed_out = False
        if "ts_open_epoch" in pos:
            timed_out = (time.time() - pos["ts_open_epoch"]) >= pos.get(
                "max_hold_secs", max_hold_bars * cfg["bar_secs"])
        else:
            timed_out = (bar - pos["bar_in"]) >= max_hold_bars

        if executor is None:
            # Paper mode: evaluate SL/TP/liq locally against current price
            if pos["side"] == "long":
                liq_p = pos["entry"] * (1 - liq_dist)
                if price <= liq_p:         ep = liq_p;              reason = "liq"
                elif price <= pos["sl"]:   ep = pos["sl"] * 0.9995; reason = "sl"
                elif price >= pos["tp"]:   ep = pos["tp"] * 0.9995; reason = "tp"
                elif timed_out:            ep = price;              reason = "timeout"
            else:
                liq_p = pos["entry"] * (1 + liq_dist)
                if price >= liq_p:         ep = liq_p;              reason = "liq"
                elif price >= pos["sl"]:   ep = pos["sl"] * 1.0005; reason = "sl"
                elif price <= pos["tp"]:   ep = pos["tp"] * 1.0005; reason = "tp"
                elif timed_out:            ep = price;              reason = "timeout"
        else:
            # Live mode: SL/TP handled by exchange (sync_live_positions above).
            # Only handle timeout here — manually close via market order.
            if timed_out:
                ep = price; reason = "timeout"

        if ep is not None:
            close_position(state, pos, ep, reason, csv_path, executor=executor)
            to_close.append(pos)

    for p in to_close:
        state["positions"].remove(p)

    # ── Drawdown halt ─────────────────────────────────────────────────────
    dd_pct = (1 - state["equity"] / state["peak_equity"]) * 100
    if dd_pct >= 30:
        log.warning("DRAWDOWN GATE: %.1f%% from peak — no new entries", dd_pct)
        return

    # ── Cooldown after any close on this TF ──────────────────────────────
    if bar - state[lcb_key] < cooldown_bars:
        return

    # ── Max concurrent check ──────────────────────────────────────────────
    sym_tf_pos = [p for p in state["positions"]
                  if p["symbol"] == symbol and p.get("tf", "1h") == tf]
    if len(sym_tf_pos) >= max_pos:
        return

    # ── Entry ─────────────────────────────────────────────────────────────
    if sig["signal"] is None:
        return
    if len([p for p in sym_tf_pos if p["side"] == sig["signal"]]) >= max_pos:
        return

    new_pos = open_position(
        state, sig, symbol, leverage, risk_pct, rr,
        sl_atr_mult, max_margin_pct, max_pos, max_hold_bars,
        tf=tf, executor=executor,
    )
    if new_pos:
        state["positions"].append(new_pos)


def process_tick(state, symbol, leverage, risk_pct, rr, sl_atr_mult,
                 max_hold_bars, cooldown_bars, max_margin_pct, max_pos,
                 csv_path, executor=None):
    _process_tick(state=state, symbol=symbol, tf="1h", leverage=leverage,
                  risk_pct=risk_pct, rr=rr, sl_atr_mult=sl_atr_mult,
                  max_hold_bars=max_hold_bars, cooldown_bars=cooldown_bars,
                  max_margin_pct=max_margin_pct, max_pos=max_pos,
                  csv_path=csv_path, cfg=STRATEGY_1H, executor=executor)


def process_tick_4h(state, symbol, leverage, risk_pct, rr, sl_atr_mult,
                    max_hold_bars, cooldown_bars, max_margin_pct, max_pos,
                    csv_path, executor=None):
    _process_tick(state=state, symbol=symbol, tf="4h", leverage=leverage,
                  risk_pct=risk_pct, rr=rr, sl_atr_mult=sl_atr_mult,
                  max_hold_bars=max_hold_bars, cooldown_bars=cooldown_bars,
                  max_margin_pct=max_margin_pct, max_pos=max_pos,
                  csv_path=csv_path, cfg=STRATEGY_4H, executor=executor)


# ─────────────────────────────────────────────────────────────────────────── #
#  Dashboard                                                                    #
# ─────────────────────────────────────────────────────────────────────────── #

def print_dashboard(state: dict, symbols_1h: list[str],
                    symbols_4h: list[str], leverage: int,
                    mode: str = "PAPER") -> None:
    equity = state["equity"]
    peak   = state["peak_equity"]
    dd_pct = (1 - equity / peak) * 100
    trades = state["closed_trades"]
    wins   = state["wins"]
    wr     = (wins / trades * 100) if trades else 0
    gbp    = equity / GBP_TO_USDT
    all_syms = list(symbols_1h) + [f"{s} [4h]" for s in symbols_4h]

    print("\n" + "=" * 65)
    print(f"  HAWK {mode} TRADER  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Leverage: {leverage}x  |  Symbols: {', '.join(all_syms)}")
    print("=" * 65)
    print(f"  Equity        : ${equity:>10.2f}  (GBP {gbp:.2f})")
    print(f"  Peak equity   : ${peak:>10.2f}")
    print(f"  Drawdown      : {dd_pct:>9.1f}%")
    print(f"  Total PnL     : ${state['total_pnl']:>+10.2f}")
    print(f"  Trades        : {trades}  |  Wins: {wins}  |  WR: {wr:.1f}%")
    print(f"  Funding paid  : ${state['funding_paid']:>10.2f}")
    print(f"  Liquidations  : {state['liqs']}")
    print(f"  1h ticks      : {state['bar_count']}  |  4h ticks: {state['bar_count_4h']}")

    if state["positions"]:
        print(f"\n  Open positions ({len(state['positions'])}):")
        for pos in state["positions"]:
            try:
                cur    = fetch_current_price(pos["symbol"])
                unreal = ((cur - pos["entry"]) if pos["side"] == "long"
                          else (pos["entry"] - cur)) * pos["qty"]
            except Exception:
                cur = pos["entry"]; unreal = 0.0
            print(f"    {pos['symbol']} [{pos.get('tf','1h')}] {pos['side'].upper():5}  "
                  f"entry={pos['entry']:.4f}  cur={cur:.4f}  "
                  f"SL={pos['sl']:.4f}  TP={pos['tp']:.4f}  unreal={unreal:+.2f}")
    else:
        print("\n  No open positions.")
    print("=" * 65)


# ─────────────────────────────────────────────────────────────────────────── #
#  Timing helpers                                                               #
# ─────────────────────────────────────────────────────────────────────────── #

def seconds_until_next_hour_candle() -> float:
    now = datetime.now(timezone.utc)
    wait = (60 - now.minute) * 60 - now.second + 60
    return max(wait, 10)


def is_4h_boundary() -> bool:
    return datetime.now(timezone.utc).hour % 4 == 0


def calc_max_pos(leverage: int, risk_pct: float,
                 sl_atr_mult: float, max_margin_pct: float) -> int:
    sl_pct_est       = sl_atr_mult * 0.0066
    margin_per_trade = risk_pct / 100 / (sl_pct_est * leverage)
    if margin_per_trade < max_margin_pct:
        return min(3, max(1, int(max_margin_pct / margin_per_trade)))
    return 1


# ─────────────────────────────────────────────────────────────────────────── #
#  Main                                                                         #
# ─────────────────────────────────────────────────────────────────────────── #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="HAWK Trader — Unified Paper & Live (HAWK v6)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/hawk_trader.py --paper                  # paper, ETH/USDT 1h
  python scripts/hawk_trader.py --paper --run-once       # single test tick
  python scripts/hawk_trader.py --testnet                # live on Binance testnet
  python scripts/hawk_trader.py                          # live (real money)
  python scripts/hawk_trader.py --paper --symbols ETH/USDT XRP/USDT
  python scripts/hawk_trader.py --paper --4h-symbols BTC/USDT BNB/USDT ADA/USDT

Live mode env vars (not needed for --paper):
  BINANCE_API_KEY     your Binance API key
  BINANCE_API_SECRET  your Binance API secret
        """,
    )
    parser.add_argument("--paper",      action="store_true",
                        help="Paper trading mode — no API key required")
    parser.add_argument("--testnet",    action="store_true",
                        help="Live mode on Binance Futures testnet")
    parser.add_argument("--symbols",    nargs="+", default=["ETH/USDT"],
                        help="1h strategy symbols (default: ETH/USDT)")
    parser.add_argument("--4h-symbols", nargs="+", default=[], dest="symbols_4h",
                        help="4h strategy symbols (e.g. BTC/USDT BNB/USDT ADA/USDT)")
    parser.add_argument("--leverage",   type=int,   default=10)
    parser.add_argument("--capital",    type=float, default=500.0,
                        help="Starting capital in GBP (default: 500, paper mode only)")
    parser.add_argument("--risk-pct",   type=float, default=1.5,
                        help="Risk per trade as %% of equity (default: 1.5)")
    parser.add_argument("--state-file", default="logs/hawk_state.json")
    parser.add_argument("--trade-log",  default="logs/hawk_trades.csv")
    parser.add_argument("--run-once",   action="store_true",
                        help="Run one tick for all symbols then exit (for testing)")
    args = parser.parse_args()

    os.makedirs("logs", exist_ok=True)

    # ── Build executor ────────────────────────────────────────────────────
    executor: LiveExecutor | None = None
    if not args.paper:
        try:
            executor = LiveExecutor(testnet=args.testnet)
        except RuntimeError as exc:
            print(exc)
            sys.exit(1)

    mode       = "PAPER" if executor is None else ("TESTNET" if args.testnet else "LIVE")
    symbols_1h = args.symbols
    symbols_4h = args.symbols_4h
    leverage   = args.leverage
    risk_pct   = args.risk_pct
    max_margin = 0.60
    max_pos_1h = calc_max_pos(leverage, risk_pct, STRATEGY_1H["sl_atr_mult"], max_margin)
    max_pos_4h = calc_max_pos(leverage, risk_pct, STRATEGY_4H["sl_atr_mult"], max_margin)

    initial_usdt = args.capital * GBP_TO_USDT
    state = load_state(args.state_file, initial_usdt)

    log.info("=" * 65)
    log.info("  HAWK TRADER  [%s]  (HAWK v6 — channel breakout + EMA + ADX)", mode)
    log.info("  1h symbols : %s  (max %d concurrent each)", symbols_1h, max_pos_1h)
    log.info("  4h symbols : %s  (max %d concurrent each)",
             symbols_4h or "(none)", max_pos_4h)
    log.info("  Leverage   : %dx  |  Risk/trade: %.1f%%", leverage, risk_pct)
    log.info("  Global margin cap: %.0f%%  |  DD halt: 30%%", max_margin * 100)
    log.info("=" * 65)

    def run_1h():
        for sym in symbols_1h:
            process_tick(state, sym, leverage, risk_pct,
                         STRATEGY_1H["rr"], STRATEGY_1H["sl_atr_mult"],
                         STRATEGY_1H["max_hold_bars"], 1, max_margin, max_pos_1h,
                         args.trade_log, executor=executor)

    def run_4h():
        for sym in symbols_4h:
            process_tick_4h(state, sym, leverage, risk_pct,
                            STRATEGY_4H["rr"], STRATEGY_4H["sl_atr_mult"],
                            STRATEGY_4H["max_hold_bars"], 1, max_margin, max_pos_4h,
                            args.trade_log, executor=executor)

    def run_and_save(force_4h: bool = False):
        run_1h()
        if symbols_4h and (force_4h or is_4h_boundary()):
            run_4h()
        print_dashboard(state, symbols_1h, symbols_4h, leverage, mode=mode)
        save_state(args.state_file, state)

    if args.run_once:
        run_and_save(force_4h=True)
        return

    log.info("Running first tick immediately ...")
    run_and_save(force_4h=True)

    while True:
        wait = seconds_until_next_hour_candle()
        next_ts = datetime.now(timezone.utc).timestamp() + wait
        next_dt = datetime.fromtimestamp(next_ts, tz=timezone.utc)
        log.info("Next tick in %.0f min  (at %s UTC)%s",
                 wait / 60,
                 next_dt.strftime("%H:%M"),
                 "  [4h tick also due]" if symbols_4h and next_dt.hour % 4 == 0 else "")
        time.sleep(wait)
        run_and_save()


if __name__ == "__main__":
    main()
