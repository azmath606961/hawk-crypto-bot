"""
HAWK Comprehensive Backtester
==============================
Exhaustive grid search across every meaningful combination:

  Assets     : ETH, BTC, SOL, XRP, BNB, ADA
  Timeframes : 1h, 4h  (15m/30m excluded — proven negative EV)
  Leverages  : 3x, 5x, 10x, 15x, 20x
  Channel    : 8, 12, 16 bars
  SL ATR     : 1.0×, 1.5×, 2.0×
  RR         : 2.0, 2.5, 3.0
  ADX gate   : off, 15, 20, 25  (trend-strength filter)
  RSI filter : off, on  (RSI>50 longs / RSI<50 shorts)
  MACD filter: off, on  (MACD>Signal longs / MACD<Signal shorts)

Total combinations: ~15,000+
Output: data/backtest_results.csv  (all rows)
        stdout: top strategies + best per asset/TF/leverage

Position sizing (R1 — never changes):
  risk_usdt = equity × 1.5%
  qty       = risk_usdt / sl_dist      # leverage-independent
  margin    = qty × price / leverage

Runtime: ~5-15 min depending on CPU count (uses multiprocessing).
"""
from __future__ import annotations

import itertools
import math
import multiprocessing as mp
import os
import sys
import time
import warnings

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import requests

# ── Constants ────────────────────────────────────────────────────────────────
GBP_TO_USDT     = 1.27
TAKER_FEE       = 0.0004
FUNDING_RATE_8H = 0.0001
RISK_PCT        = 1.5
MAX_MARGIN_PCT  = 0.60
MAX_DD_PCT      = 30.0
INITIAL_GBP     = 500.0
INITIAL_USDT    = INITIAL_GBP * GBP_TO_USDT

DATA_DIR   = os.path.join(os.path.dirname(__file__), "..", "data")
RESULT_CSV = os.path.join(os.path.dirname(__file__), "..", "data", "backtest_results.csv")

BINANCE_KLINE = "https://api.binance.com/api/v3/klines"

# ── Grid parameters ───────────────────────────────────────────────────────────
LEVERAGES   = [3, 5, 10, 15, 20]
CHANNELS    = [8, 12, 16]
SL_ATRS     = [1.0, 1.5, 2.0]
RRS         = [2.0, 2.5, 3.0]
ADX_MINS    = [0.0, 15.0, 20.0, 25.0]   # 0.0 = disabled
RSI_FLAGS   = [False, True]
MACD_FLAGS  = [False, True]

ASSETS_1H = ["ETHUSDT", "BTCUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT"]
ASSETS_4H = ["ETHUSDT", "BTCUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT"]

MAX_HOLD = {"1h": 30, "4h": 12}      # bars
FUND_BARS = {"1h": 8, "4h": 2}       # every N bars = 8h of funding


# ─────────────────────────────────────────────────────────────────────────── #
#  Data download helpers                                                        #
# ─────────────────────────────────────────────────────────────────────────── #

def _download(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    bar_ms = {"1h": 3_600_000, "4h": 14_400_000}[interval]
    rows: list = []
    cursor = start_ms
    while cursor < end_ms:
        resp = requests.get(
            BINANCE_KLINE,
            params={"symbol": symbol, "interval": interval,
                    "startTime": cursor, "endTime": end_ms, "limit": 1000},
            timeout=20,
        )
        resp.raise_for_status()
        chunk = resp.json()
        if not chunk:
            break
        rows.extend(chunk)
        cursor = int(chunk[-1][0]) + bar_ms
        time.sleep(0.12)
        if len(chunk) < 1000:
            break
    if not rows:
        raise RuntimeError(f"No data: {symbol} {interval}")
    df = pd.DataFrame(rows, columns=[
        "ts", "open", "high", "low", "close", "volume",
        "cts", "qv", "n", "tb", "tq", "_"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts")[["open", "high", "low", "close", "volume"]].astype(float)
    # Drop current (possibly incomplete) bar
    now_ms = int(time.time() * 1000)
    if now_ms - int(df.index[-1].timestamp() * 1000) < bar_ms:
        df = df.iloc[:-1]
    return df


def ensure_data(symbol: str, interval: str) -> str | None:
    fname = f"{symbol}_{interval}.csv"
    path  = os.path.join(DATA_DIR, fname)
    if os.path.exists(path):
        return path
    # Derive date range from ETH 1h (our reference)
    ref = pd.read_csv(os.path.join(DATA_DIR, "ETHUSDT_1h.csv"),
                      parse_dates=["timestamp"], index_col="timestamp")
    start_ms = int(ref.index[0].timestamp() * 1000)
    end_ms   = int(ref.index[-1].timestamp() * 1000)
    print(f"  Downloading {fname} ...", end=" ", flush=True)
    try:
        df = _download(symbol, interval, start_ms, end_ms)
        df.index.name = "timestamp"
        df.to_csv(path)
        print(f"OK ({len(df)} bars)")
        return path
    except Exception as e:
        print(f"FAILED: {e}")
        return None


def load_df(symbol: str, interval: str) -> pd.DataFrame | None:
    path = ensure_data(symbol, interval)
    if path is None:
        return None
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    df.columns = [c.lower() for c in df.columns]
    return df


# ─────────────────────────────────────────────────────────────────────────── #
#  Indicators (vectorised — computed once per dataset)                         #
# ─────────────────────────────────────────────────────────────────────────── #

def _ema_np(arr: np.ndarray, p: int) -> np.ndarray:
    alpha = 1.0 / p
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def compute_dataset_indicators(df: pd.DataFrame) -> dict:
    """
    Pre-compute ALL indicators once.  The grid loop just references these arrays.
    """
    h = df["high"].values.astype(np.float64)
    l = df["low"].values.astype(np.float64)
    c = df["close"].values.astype(np.float64)
    n = len(c)

    # ATR(14) — Wilder
    tr = np.maximum(h[1:] - l[1:],
         np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    tr_full = np.empty(n); tr_full[0] = h[0] - l[0]
    tr_full[1:] = tr
    atr14 = _ema_np(tr_full, 14)

    # EMA 20 / 50
    ema20 = _ema_np(c, 20)
    ema50 = _ema_np(c, 50)

    # ADX(14) — Wilder
    up = np.diff(h, prepend=h[0])
    dn = -np.diff(l, prepend=l[0])
    dm_pos = np.where((up > dn) & (up > 0), up, 0.0)
    dm_neg = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr14s = np.where(atr14 == 0, 1e-10, atr14)
    di_pos = 100 * _ema_np(dm_pos, 14) / atr14s
    di_neg = 100 * _ema_np(dm_neg, 14) / atr14s
    di_sum = di_pos + di_neg
    di_sum = np.where(di_sum == 0, 1e-10, di_sum)
    dx  = 100 * np.abs(di_pos - di_neg) / di_sum
    adx = _ema_np(dx, 14)

    # RSI(14) — Wilder
    delta = np.diff(c, prepend=c[0])
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    avg_g = _ema_np(gain, 14)
    avg_l = _ema_np(loss, 14)
    rs    = avg_g / np.where(avg_l == 0, 1e-10, avg_l)
    rsi   = 100 - (100 / (1 + rs))

    # MACD (12, 26, 9)
    ema12 = _ema_np(c, 12)
    ema26 = _ema_np(c, 26)
    macd  = ema12 - ema26
    sig   = _ema_np(macd, 9)
    macd_above = (macd > sig).astype(np.int8)

    # Channel variants: shift(1) = prev bar's extreme (no look-ahead)
    chan_hi: dict[int, np.ndarray] = {}
    chan_lo: dict[int, np.ndarray] = {}
    for n_bars in CHANNELS:
        hi = np.full(n, np.nan)
        lo = np.full(n, np.nan)
        for i in range(n_bars, n):
            hi[i] = h[i - n_bars: i].max()   # max of prev n_bars (shifted by 1)
            lo[i] = l[i - n_bars: i].min()
        chan_hi[n_bars] = hi
        chan_lo[n_bars] = lo

    return {
        "c": c, "h": h, "l": l,
        "ema20": ema20, "ema50": ema50,
        "atr14": atr14, "adx": adx, "rsi": rsi,
        "macd_above": macd_above,
        "chan_hi": chan_hi, "chan_lo": chan_lo,
        "n": n,
    }


# ─────────────────────────────────────────────────────────────────────────── #
#  Fast numpy bar-loop engine                                                   #
# ─────────────────────────────────────────────────────────────────────────── #

def run_combo(
    c, h, l, ema20, ema50, atr14, adx, rsi, macd_above,
    chan_hi, chan_lo,
    leverage: int,
    sl_m: float,
    rr: float,
    adx_min: float,
    rsi_on: bool,
    macd_on: bool,
    max_hold: int,
    fund_bars: int,
) -> tuple:
    """
    Returns: (return_pct, wr_pct, actual_rr, n_trades, n_liqs, monthly_pct)
    Position sizing: qty = equity*risk_pct / sl_dist  (leverage-independent)
    """
    equity   = float(INITIAL_USDT)
    peak     = equity
    liq_dist = 1.0 / leverage - 0.005
    n        = len(c)

    # Estimate max_pos from margin
    sl_est  = sl_m * 0.0066          # approx SL as pct of price
    mpt     = RISK_PCT / 100 / (sl_est * leverage)
    max_pos = min(3, max(1, int(MAX_MARGIN_PCT / mpt))) if mpt < MAX_MARGIN_PCT else 1

    WARMUP = 75  # ema50(50) + max_channel(16) + ADX(14) warmup

    # Position storage: list of [side, entry, qty, notional, margin, sl, tp, bar_in]
    positions: list = []
    last_close = -999
    wins = 0; losses = 0; liqs = 0
    win_pnl = 0.0; loss_pnl_abs = 0.0

    for i in range(WARMUP, n):
        bc = c[i]; bh = h[i]; bl = l[i]
        batr = atr14[i]; bch = chan_hi[i]; bcl = chan_lo[i]

        if math.isnan(batr) or math.isnan(bch):
            continue

        # DD halt
        if (1.0 - equity / peak) * 100.0 >= MAX_DD_PCT:
            break

        # Funding every fund_bars bars
        if i % fund_bars == 0:
            for pos in positions:
                fund = pos[3] * FUNDING_RATE_8H
                if pos[0] == 1:
                    equity -= fund
                else:
                    equity += fund

        # Manage open positions
        closed_idx: list = []
        for j in range(len(positions)):
            pos = positions[j]
            side, entry, qty, notional, margin, sl, tp, bar_in = pos
            ep = 0.0; reason = 0

            liq_p = entry * (1.0 - liq_dist) if side == 1 else entry * (1.0 + liq_dist)

            if side == 1:
                if bl <= liq_p:
                    ep = liq_p;            reason = -2
                elif bl <= sl:
                    ep = sl * 0.9995;      reason = -1
                elif bh >= tp:
                    ep = tp * 0.9995;      reason = 1
                elif i - bar_in >= max_hold:
                    ep = bc;               reason = 0
            else:
                if bh >= liq_p:
                    ep = liq_p;            reason = -2
                elif bh >= sl:
                    ep = sl * 1.0005;      reason = -1
                elif bl <= tp:
                    ep = tp * 1.0005;      reason = 1
                elif i - bar_in >= max_hold:
                    ep = bc;               reason = 0

            if ep > 0.0:
                fee = qty * ep * TAKER_FEE
                if reason == -2:
                    pnl = -margin; liqs += 1
                else:
                    raw = (ep - entry) if side == 1 else (entry - ep)
                    pnl = raw * qty - fee
                equity += pnl
                if pnl > 0:
                    wins += 1; win_pnl += pnl
                else:
                    losses += 1; loss_pnl_abs += abs(pnl)
                if equity > peak:
                    peak = equity
                closed_idx.append(j)
                last_close = i

        for j in reversed(closed_idx):
            positions.pop(j)

        # Entry
        if i - last_close < 1:
            continue
        if len(positions) >= max_pos:
            continue

        total_margin = sum(p[4] for p in positions)
        avail = equity * MAX_MARGIN_PCT - total_margin
        if avail <= 0.0:
            continue

        bull = ema20[i] > ema50[i]
        bear = ema20[i] < ema50[i]

        # ADX filter
        adx_ok = True
        if adx_min > 0.0:
            adx_ok = adx[i] >= adx_min

        # RSI filter
        rsi_long_ok = rsi_short_ok = True
        if rsi_on:
            rsi_long_ok  = rsi[i] >= 50.0
            rsi_short_ok = rsi[i] <= 50.0

        # MACD filter
        macd_long_ok = macd_short_ok = True
        if macd_on:
            mv = int(macd_above[i])
            macd_long_ok  = mv == 1
            macd_short_ok = mv == 0

        sd = sl_m * batr
        if sd <= 0:
            continue

        if bull and adx_ok and rsi_long_ok and macd_long_ok and bc > bch:
            ep   = bc * 1.0005
            qty  = (equity * RISK_PCT / 100.0) / sd
            not_ = qty * ep
            mrg  = not_ / leverage
            fee  = not_ * TAKER_FEE
            if mrg + fee > avail:
                scale = avail / (mrg + fee)
                qty  *= scale; not_ = qty * ep
                mrg   = not_ / leverage; fee = not_ * TAKER_FEE
            if not_ >= 2.0:
                equity -= fee
                positions.append([1, ep, qty, not_, mrg,
                                   ep - sd, ep + rr * sd, i])

        elif bear and adx_ok and rsi_short_ok and macd_short_ok and bc < bcl:
            ep   = bc * 0.9995
            qty  = (equity * RISK_PCT / 100.0) / sd
            not_ = qty * ep
            mrg  = not_ / leverage
            fee  = not_ * TAKER_FEE
            if mrg + fee > avail:
                scale = avail / (mrg + fee)
                qty  *= scale; not_ = qty * ep
                mrg   = not_ / leverage; fee = not_ * TAKER_FEE
            if not_ >= 2.0:
                equity -= fee
                positions.append([-1, ep, qty, not_, mrg,
                                   ep + sd, ep - rr * sd, i])

    # Close remaining at EOD
    for pos in positions:
        lp  = c[-1]
        raw = (lp - pos[1]) if pos[0] == 1 else (pos[1] - lp)
        pnl = raw * pos[2] - pos[2] * lp * TAKER_FEE
        equity += pnl
        if pnl > 0: wins += 1; win_pnl += pnl
        else:       losses += 1; loss_pnl_abs += abs(pnl)

    n_trades  = wins + losses
    wr        = wins / n_trades * 100.0 if n_trades else 0.0
    actual_rr = win_pnl / loss_pnl_abs if loss_pnl_abs > 0 else 0.0
    ret_pct   = (equity / INITIAL_USDT - 1.0) * 100.0

    return ret_pct, wr, actual_rr, n_trades, liqs


# ─────────────────────────────────────────────────────────────────────────── #
#  Worker function (top-level for multiprocessing pickling)                    #
# ─────────────────────────────────────────────────────────────────────────── #

def _worker(args):
    """Unpack args and run one combo. Returns dict row for CSV."""
    (asset, tf, days,
     c, h, l, ema20, ema50, atr14, adx_arr, rsi_arr, macd_arr,
     chan_hi_dict, chan_lo_dict,
     lev, chan, sl_m, rr, adx_min, rsi_on, macd_on) = args

    mh = MAX_HOLD[tf]
    fb = FUND_BARS[tf]

    ret, wr, actual_rr, n_trades, n_liqs = run_combo(
        c, h, l, ema20, ema50, atr14,
        adx_arr, rsi_arr, macd_arr,
        chan_hi_dict[chan], chan_lo_dict[chan],
        lev, sl_m, rr, adx_min, rsi_on, macd_on, mh, fb,
    )

    # Monthly% from actual equity curve
    months   = days / 30.44
    final_eq = INITIAL_USDT * (1 + ret / 100)
    mo_pct   = ((final_eq / INITIAL_USDT) ** (1.0 / months) - 1) * 100 if months > 0 else 0.0

    tpd = n_trades / days if days > 0 else 0.0
    ev  = (wr / 100) * actual_rr - (1 - wr / 100) if actual_rr else 0.0

    return {
        "asset":    asset,
        "tf":       tf,
        "leverage": lev,
        "channel":  chan,
        "sl_atr":   sl_m,
        "rr":       rr,
        "adx_min":  int(adx_min) if adx_min > 0 else "off",
        "rsi":      "on" if rsi_on  else "off",
        "macd":     "on" if macd_on else "off",
        "return_pct": round(ret, 2),
        "monthly_pct": round(mo_pct, 3),
        "wr":        round(wr, 2),
        "actual_rr": round(actual_rr, 3),
        "trades":    n_trades,
        "tpd":       round(tpd, 3),
        "liqs":      n_liqs,
        "ev":        round(ev, 4),
    }


# ─────────────────────────────────────────────────────────────────────────── #
#  Print helpers                                                                #
# ─────────────────────────────────────────────────────────────────────────── #

HDR = (f"  {'Asset':<10} {'TF':<3} {'Lev':>4} {'Ch':>3} {'SL':>4} {'RR':>4} "
       f"{'ADX':>4} {'RSI':>4} {'MACD':>5} "
       f"{'Return':>8} {'Mo%':>7} {'WR%':>6} {'RR_act':>7} "
       f"{'T':>5} {'Liqs':>5} {'EV':>7}")
SEP = "  " + "-" * 110


def _row(r: dict) -> str:
    return (f"  {r['asset']:<10} {r['tf']:<3} {r['leverage']:>4}x {r['channel']:>3} "
            f"{r['sl_atr']:>4.1f} {r['rr']:>4.1f} "
            f"{str(r['adx_min']):>4} {r['rsi']:>4} {r['macd']:>5} "
            f"{r['return_pct']:>+7.1f}% {r['monthly_pct']:>+6.2f}% "
            f"{r['wr']:>5.1f}% {r['actual_rr']:>7.3f} "
            f"{r['trades']:>5} {r['liqs']:>5} {r['ev']:>+7.4f}")


# ─────────────────────────────────────────────────────────────────────────── #
#  Main                                                                         #
# ─────────────────────────────────────────────────────────────────────────── #

def main() -> None:
    t0 = time.time()

    print("\n" + "=" * 80)
    print("  HAWK COMPREHENSIVE BACKTESTER")
    print("  Assets: ETH BTC SOL XRP BNB ADA | TF: 1h 4h | Lev: 3-20x")
    print("  Indicators: ADX / RSI / MACD | All channel/SL/RR combos")
    print("=" * 80 + "\n")

    # ── 1. Download missing data ────────────────────────────────────────── #
    print("Checking / downloading required data files ...\n")
    missing_4h = [s for s in ASSETS_4H if not os.path.exists(
        os.path.join(DATA_DIR, f"{s}_4h.csv"))]
    if missing_4h:
        for sym in missing_4h:
            ensure_data(sym, "4h")
    print()

    # ── 2. Load datasets + precompute indicators ────────────────────────── #
    print("Loading datasets and precomputing indicators ...")
    datasets: list[dict] = []

    for asset in ASSETS_1H:
        for tf in ["1h", "4h"]:
            df = load_df(asset, tf)
            if df is None:
                print(f"  SKIP {asset} {tf} (no data)")
                continue
            days = (df.index[-1] - df.index[0]).total_seconds() / 86_400
            if days < 60:
                print(f"  SKIP {asset} {tf} (only {days:.0f} days)")
                continue
            ind = compute_dataset_indicators(df)
            datasets.append({"asset": asset, "tf": tf, "days": days, "ind": ind})
            print(f"  {asset} {tf:3s}  {len(df):>6} bars  {days:.0f} days  "
                  f"[EMA/ATR/ADX/RSI/MACD/Chan done]")

    n_datasets  = len(datasets)
    n_per_ds    = (len(LEVERAGES) * len(CHANNELS) * len(SL_ATRS) *
                   len(RRS) * len(ADX_MINS) * len(RSI_FLAGS) * len(MACD_FLAGS))
    total_combos = n_datasets * n_per_ds
    print(f"\n  Datasets: {n_datasets}  |  Combos/dataset: {n_per_ds}  "
          f"|  Total: {total_combos:,}\n")

    # ── 3. Build task list ──────────────────────────────────────────────── #
    tasks = []
    for ds in datasets:
        ind  = ds["ind"]
        base = (ds["asset"], ds["tf"], ds["days"],
                ind["c"], ind["h"], ind["l"],
                ind["ema20"], ind["ema50"], ind["atr14"],
                ind["adx"], ind["rsi"], ind["macd_above"],
                ind["chan_hi"], ind["chan_lo"])

        for combo in itertools.product(
            LEVERAGES, CHANNELS, SL_ATRS, RRS, ADX_MINS, RSI_FLAGS, MACD_FLAGS
        ):
            tasks.append(base + combo)

    # ── 4. Run grid (multiprocessing) ───────────────────────────────────── #
    cpu_count = max(1, mp.cpu_count() - 1)
    print(f"Running grid search on {cpu_count} CPU cores ...")
    print(f"Est. time: {total_combos // cpu_count * 0.25 / 60:.1f}-"
          f"{total_combos // cpu_count * 0.5 / 60:.1f} min\n")

    results: list[dict] = []
    done = 0

    with mp.Pool(processes=cpu_count) as pool:
        for res in pool.imap_unordered(_worker, tasks, chunksize=50):
            results.append(res)
            done += 1
            if done % 500 == 0:
                elapsed = time.time() - t0
                rate    = done / elapsed
                remain  = (total_combos - done) / rate if rate > 0 else 0
                print(f"  [{done:>6}/{total_combos}]  "
                      f"{elapsed/60:.1f}m elapsed  ~{remain/60:.1f}m remaining",
                      flush=True)

    elapsed = time.time() - t0
    print(f"\n  Done in {elapsed/60:.1f} min  ({len(results):,} results)\n")

    # ── 5. Save all results to CSV ──────────────────────────────────────── #
    df_out = pd.DataFrame(results).sort_values("monthly_pct", ascending=False)
    df_out.to_csv(RESULT_CSV, index=False)
    print(f"  Full results saved to: data/backtest_results.csv\n")

    # ── 6. Print summary ────────────────────────────────────────────────── #
    _print_summary(df_out)


def _print_summary(df: pd.DataFrame) -> None:
    # Filter positive EV, min 20 trades
    valid = df[(df["ev"] > 0) & (df["trades"] >= 20) & (df["liqs"] <= 10)].copy()

    print("\n" + "=" * 115)
    print("  TOP 30 STRATEGIES  (sorted by monthly%, min 20 trades, EV > 0, liqs <= 10)")
    print("=" * 115)
    print(HDR)
    print(SEP)
    for _, r in df[(df["trades"] >= 20) & (df["liqs"] <= 10)].head(30).iterrows():
        marker = " ***" if r["monthly_pct"] >= 10 else (
                 " **"  if r["monthly_pct"] >=  5 else "")
        print(_row(r.to_dict()) + marker)

    print("\n  *** = 10%+/month   ** = 5%+/month\n")

    # ── Best per asset ─────────────────────────────────────────────────── #
    print("=" * 115)
    print("  BEST STRATEGY PER ASSET  (highest monthly%, min 20 trades, EV > 0)")
    print("=" * 115)
    print(HDR)
    print(SEP)
    for asset in ["ETHUSDT", "BTCUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT"]:
        sub = valid[valid["asset"] == asset]
        if sub.empty:
            print(f"  {asset:<10}  No positive-EV result")
            continue
        best = sub.iloc[0].to_dict()
        print(_row(best))

    # ── Best per TF ────────────────────────────────────────────────────── #
    print("\n" + "=" * 115)
    print("  BEST STRATEGY PER TIMEFRAME")
    print("=" * 115)
    print(HDR)
    print(SEP)
    for tf in ["1h", "4h"]:
        sub = valid[valid["tf"] == tf]
        if sub.empty:
            print(f"  {tf:5s}  No positive-EV result")
            continue
        best = sub.iloc[0].to_dict()
        print(_row(best))

    # ── Best per leverage ──────────────────────────────────────────────── #
    print("\n" + "=" * 115)
    print("  BEST STRATEGY PER LEVERAGE")
    print("=" * 115)
    print(HDR)
    print(SEP)
    for lev in LEVERAGES:
        sub = valid[valid["leverage"] == lev]
        if sub.empty:
            print(f"  Lev={lev:2d}x  No positive-EV result")
            continue
        best = sub.iloc[0].to_dict()
        print(_row(best))

    # ── 10%+/month strategies ──────────────────────────────────────────── #
    ten_pct = valid[valid["monthly_pct"] >= 10.0]
    print(f"\n{'=' * 115}")
    print(f"  STRATEGIES ACHIEVING 10%+/MONTH  ({len(ten_pct)} found)")
    print("=" * 115)
    if len(ten_pct) > 0:
        print(HDR)
        print(SEP)
        for _, r in ten_pct.head(50).iterrows():
            print(_row(r.to_dict()))
    else:
        print("  None found with current grid. Best result:")
        best = valid.iloc[0].to_dict() if not valid.empty else df.iloc[0].to_dict()
        print(HDR)
        print(SEP)
        print(_row(best))

    # ── Portfolio combos ───────────────────────────────────────────────── #
    print(f"\n{'=' * 115}")
    print("  TOP MULTI-ASSET PORTFOLIO COMBINATIONS")
    print("  (Best strategy per asset, combined monthly% if run in parallel)")
    print("=" * 115)

    import math

    def roadmap(total_mo: float) -> str:
        def to_m(start, end, r):
            if r <= 0: return float("inf")
            return math.log(end / start) / math.log(1 + r / 100)
        m1   = to_m(500, 1_000, total_mo)
        m10  = to_m(1_000, 10_000, total_mo)
        m100 = to_m(10_000, 100_000, total_mo)
        tot  = to_m(500, 100_000, total_mo)
        def f(m):
            if not math.isfinite(m): return "  never"
            y, mo = divmod(int(round(m)), 12)
            return f"{y}y{mo:02d}m" if y else f"   {mo:02d}m"
        return f"500->1k:{f(m1)}  1k->10k:{f(m10)}  10k->100k:{f(m100)}  TOTAL:{f(tot)}"

    # Best-per-asset portfolio with matching leverage
    print(f"\n  Strategy: pick best per asset (any leverage), run in parallel\n")
    portfolio_total = 0.0
    portfolio_lines = []
    for asset in ["ETHUSDT", "BTCUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT"]:
        sub = valid[valid["asset"] == asset]
        if sub.empty:
            continue
        r = sub.iloc[0].to_dict()
        portfolio_total += r["monthly_pct"]
        portfolio_lines.append(f"    {r['asset']:<10} {r['tf']:<3} {r['leverage']:>2}x  "
                               f"{r['monthly_pct']:>+6.2f}%/mo  "
                               f"(ch={r['channel']} sl={r['sl_atr']} rr={r['rr']} "
                               f"adx={r['adx_min']} rsi={r['rsi']} macd={r['macd']})")
    for line in portfolio_lines:
        print(line)
    print(f"\n  Combined monthly%: {portfolio_total:+.2f}%")
    print(f"  Roadmap: {roadmap(portfolio_total)}")

    # Conservative (10x only) portfolio
    valid_10x = valid[valid["leverage"] == 10]
    print(f"\n  Conservative portfolio: 10x leverage only\n")
    portfolio_10x = 0.0
    for asset in ["ETHUSDT", "BTCUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT"]:
        sub = valid_10x[valid_10x["asset"] == asset]
        if sub.empty:
            continue
        r = sub.iloc[0].to_dict()
        portfolio_10x += r["monthly_pct"]
        print(f"    {r['asset']:<10} {r['tf']:<3} 10x  {r['monthly_pct']:>+6.2f}%/mo  "
              f"(ch={r['channel']} sl={r['sl_atr']} rr={r['rr']} "
              f"adx={r['adx_min']} rsi={r['rsi']} macd={r['macd']})")
    print(f"\n  Combined monthly%: {portfolio_10x:+.2f}%")
    print(f"  Roadmap: {roadmap(portfolio_10x)}")

    print("\n" + "=" * 115 + "\n")


if __name__ == "__main__":
    main()
