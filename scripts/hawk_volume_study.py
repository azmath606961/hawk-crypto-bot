"""
HAWK Volume Filter Study
========================
Tests two new filters derived from TradingView's Institutional Volume Flow
and Enhanced Buy/Sell Profile indicators:

  1. Volume Z-score filter  — enter only when candle volume > rolling mean + k*std
     (confirms breakout on real institutional volume, not thin-air fake-out)

  2. Body ratio filter      — (close-low)/(high-low) >= 0.5 for longs, <= 0.5 for shorts
     (proxy for buy/sell pressure: candle must close in the dominant half)

Tests are run on top of the already-optimal params from the 25,920-combo backtest.
Grid: 5 assets × best params × {vol_filter on/off} × {body_filter on/off} = ~20 combos

Output: data/volume_study_results.csv + stdout summary
"""
from __future__ import annotations

import os
import sys
import time
import warnings

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

# ── Constants (must match leveraged_engine.py) ────────────────────────────────
GBP_TO_USDT     = 1.27
TAKER_FEE       = 0.0004
FUNDING_RATE_8H = 0.0001
RISK_PCT        = 1.5
MAX_MARGIN_PCT  = 0.60
INITIAL_USDT    = 500.0 * GBP_TO_USDT   # ~$635

DATA_DIR   = os.path.join(os.path.dirname(__file__), "..", "data")
RESULT_CSV = os.path.join(DATA_DIR, "volume_study_results.csv")

# ── Best params from 25,920-combo backtest ────────────────────────────────────
# Each tuple: (asset, tf, leverage, channel, sl_atr, rr, adx_min, rsi_on, macd_on, max_hold_bars)
BEST_CONFIGS = [
    # Conservative (all 10x)
    dict(label="ETH 1h  (consv)",  asset="ETHUSDT", tf="1h",  lev=10, ch=8,  sl=2.0, rr=2.0, adx=0.0,  rsi=True,  macd=False, hold=30),
    dict(label="XRP 1h  (consv)",  asset="XRPUSDT", tf="1h",  lev=10, ch=16, sl=1.0, rr=3.0, adx=0.0,  rsi=False, macd=False, hold=30),
    dict(label="BTC 4h  (consv)",  asset="BTCUSDT", tf="4h",  lev=10, ch=8,  sl=1.5, rr=2.0, adx=0.0,  rsi=True,  macd=True,  hold=12),
    dict(label="BNB 4h  (consv)",  asset="BNBUSDT", tf="4h",  lev=10, ch=16, sl=1.5, rr=3.0, adx=25.0, rsi=True,  macd=False, hold=12),
    dict(label="ADA 4h  (consv)",  asset="ADAUSDT", tf="4h",  lev=10, ch=16, sl=2.0, rr=2.5, adx=0.0,  rsi=True,  macd=True,  hold=12),
    # Optimal (mixed leverage)
    dict(label="ETH 1h  (optim)",  asset="ETHUSDT", tf="1h",  lev=20, ch=12, sl=1.0, rr=2.5, adx=0.0,  rsi=True,  macd=True,  hold=30),
    dict(label="XRP 1h  (optim)",  asset="XRPUSDT", tf="1h",  lev=20, ch=12, sl=1.5, rr=2.5, adx=0.0,  rsi=False, macd=False, hold=30),
    dict(label="ADA 4h  (optim)",  asset="ADAUSDT", tf="4h",  lev=5,  ch=8,  sl=2.0, rr=2.5, adx=0.0,  rsi=False, macd=True,  hold=12),
]

VOL_ZSCORE_K = 0.5   # volume must exceed mean + k*std; 0.0 = above-average only
VOL_LOOKBACK = 20    # rolling window for mean/std


# ─────────────────────────────────────────────────────────────────────────── #
#  Indicators (numpy, Wilder EMA — R18)                                        #
# ─────────────────────────────────────────────────────────────────────────── #

def _ema(arr: np.ndarray, p: int) -> np.ndarray:
    alpha = 1.0 / p
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def prep_arrays(df: pd.DataFrame, ch: int) -> dict:
    h = df["high"].values.astype(np.float64)
    l = df["low"].values.astype(np.float64)
    c = df["close"].values.astype(np.float64)
    o = df["open"].values.astype(np.float64)
    v = df["volume"].values.astype(np.float64)
    n = len(c)

    # ATR(14) Wilder
    tr = np.maximum(h[1:]-l[1:], np.maximum(np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])))
    tr_f = np.empty(n); tr_f[0] = h[0]-l[0]; tr_f[1:] = tr
    atr = _ema(tr_f, 14)

    ema20 = _ema(c, 20)
    ema50 = _ema(c, 50)

    # ADX(14) Wilder
    up = np.diff(h, prepend=h[0])
    dn = -np.diff(l, prepend=l[0])
    dm_pos = np.where((up > dn) & (up > 0), up, 0.0)
    dm_neg = np.where((dn > up) & (dn > 0), dn, 0.0)
    atrs = np.where(atr == 0, 1e-10, atr)
    di_pos = 100 * _ema(dm_pos, 14) / atrs
    di_neg = 100 * _ema(dm_neg, 14) / atrs
    di_sum = np.where((di_pos+di_neg)==0, 1e-10, di_pos+di_neg)
    dx = 100 * np.abs(di_pos-di_neg) / di_sum
    adx = _ema(dx, 14)

    # RSI(14) Wilder
    delta = np.diff(c, prepend=c[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_g = _ema(gain, 14)
    avg_l = _ema(loss, 14)
    rs = avg_g / np.where(avg_l==0, 1e-10, avg_l)
    rsi = 100 - (100/(1+rs))

    # MACD (12,26,9)
    ema12 = _ema(c, 12)
    ema26 = _ema(c, 26)
    macd = ema12 - ema26
    sig = _ema(macd, 9)
    macd_above = (macd > sig).astype(np.int8)

    # Channel (shift-1, no look-ahead)
    chan_hi = np.full(n, np.nan)
    chan_lo = np.full(n, np.nan)
    for i in range(ch, n):
        chan_hi[i] = h[i-ch:i].max()
        chan_lo[i] = l[i-ch:i].min()

    # Volume Z-score (rolling)
    vol_mean = np.full(n, np.nan)
    vol_std  = np.full(n, np.nan)
    for i in range(VOL_LOOKBACK, n):
        window = v[i-VOL_LOOKBACK:i]
        vol_mean[i] = window.mean()
        vol_std[i]  = window.std()

    # Candle body ratio: (close-low)/(high-low)
    hl = h - l
    body_ratio = np.where(hl > 0, (c - l) / hl, 0.5)

    return dict(h=h, l=l, c=c, o=o, v=v, atr=atr,
                ema20=ema20, ema50=ema50, adx=adx, rsi=rsi,
                macd_above=macd_above, chan_hi=chan_hi, chan_lo=chan_lo,
                vol_mean=vol_mean, vol_std=vol_std, body_ratio=body_ratio)


# ─────────────────────────────────────────────────────────────────────────── #
#  Backtest engine                                                              #
# ─────────────────────────────────────────────────────────────────────────── #

def run_backtest(arr: dict, cfg: dict,
                 vol_filter: bool, body_filter: bool) -> dict:
    h = arr["h"]; l = arr["l"]; c = arr["c"]
    atr = arr["atr"]
    ema20 = arr["ema20"]; ema50 = arr["ema50"]
    adx   = arr["adx"];   rsi   = arr["rsi"]
    macd_above = arr["macd_above"]
    chan_hi = arr["chan_hi"]; chan_lo = arr["chan_lo"]
    vol_mean = arr["vol_mean"]; vol_std = arr["vol_std"]
    body_ratio = arr["body_ratio"]

    lev = cfg["lev"]; sl_m = cfg["sl"]; rr = cfg["rr"]
    adx_min = cfg["adx"]; rsi_on = cfg["rsi"]; macd_on = cfg["macd"]
    max_hold = cfg["hold"]

    fund_bars = 8 if cfg["tf"] == "1h" else 2
    n = len(c)
    equity = INITIAL_USDT
    peak   = equity
    positions: list = []
    wins = losses = 0
    win_pnl = loss_pnl_abs = 0.0
    liqs = 0
    last_close = -999

    for i in range(1, n):
        bc = c[i]; bh = h[i]; bl = l[i]; batr = atr[i]

        # Funding on open positions
        for pos in positions:
            if (i - pos[7]) % fund_bars == 0:
                funding = pos[3] * FUNDING_RATE_8H
                equity -= funding

        # Check exits
        closed_idx = []
        for j, pos in enumerate(positions):
            side = pos[0]; entry = pos[1]; qty = pos[2]
            mrg = pos[4]; sl_p = pos[5]; tp_p = pos[6]
            liq_p = (entry * (1 - 1/lev * 0.9)) if side == 1 else (entry * (1 + 1/lev * 0.9))
            ep = 0.0; reason = 99
            if side == 1:
                if bl <= liq_p:   ep = liq_p;            reason = -2
                elif bl <= sl_p:  ep = sl_p * 0.9995;    reason = -1
                elif bh >= tp_p:  ep = tp_p * 0.9995;    reason = 1
                elif i - pos[7] >= max_hold: ep = bc;    reason = 0
            else:
                if bh >= liq_p:   ep = liq_p;            reason = -2
                elif bh >= sl_p:  ep = sl_p * 1.0005;    reason = -1
                elif bl <= tp_p:  ep = tp_p * 1.0005;    reason = 1
                elif i - pos[7] >= max_hold: ep = bc;    reason = 0
            if ep > 0.0:
                fee = qty * ep * TAKER_FEE
                if reason == -2:
                    pnl = -mrg; liqs += 1
                else:
                    raw = (ep - entry) if side == 1 else (entry - ep)
                    pnl = raw * qty - fee
                equity += pnl
                if pnl > 0: wins += 1; win_pnl += pnl
                else:       losses += 1; loss_pnl_abs += abs(pnl)
                if equity > peak: peak = equity
                closed_idx.append(j)
                last_close = i
        for j in reversed(closed_idx): positions.pop(j)

        if i - last_close < 1: continue
        if len(positions) >= 3: continue
        total_margin = sum(p[4] for p in positions)
        avail = equity * MAX_MARGIN_PCT - total_margin
        if avail <= 0.0: continue

        bull = ema20[i] > ema50[i]
        bear = ema20[i] < ema50[i]

        adx_ok = (adx_min == 0.0) or (adx[i] >= adx_min)
        rsi_long_ok  = (not rsi_on) or (rsi[i] >= 50.0)
        rsi_short_ok = (not rsi_on) or (rsi[i] <= 50.0)
        macd_long_ok  = (not macd_on) or (int(macd_above[i]) == 1)
        macd_short_ok = (not macd_on) or (int(macd_above[i]) == 0)

        # Volume Z-score filter (new from Institutional Volume Flow)
        vol_ok = True
        if vol_filter and not np.isnan(vol_mean[i]) and vol_std[i] > 0:
            threshold = vol_mean[i] + VOL_ZSCORE_K * vol_std[i]
            vol_ok = arr["v"][i] >= threshold

        # Body ratio filter (new from Enhanced Buy/Sell Profile)
        body_long_ok  = (not body_filter) or (body_ratio[i] >= 0.5)
        body_short_ok = (not body_filter) or (body_ratio[i] <= 0.5)

        sd = sl_m * batr
        if sd <= 0 or np.isnan(chan_hi[i]): continue

        for side, ok_set, ep_mult, sl_dir in [
            (1,  bull and adx_ok and rsi_long_ok  and macd_long_ok  and vol_ok and body_long_ok  and bc > chan_hi[i],  1.0005, -1),
            (-1, bear and adx_ok and rsi_short_ok and macd_short_ok and vol_ok and body_short_ok and bc < chan_lo[i], 0.9995,  1),
        ]:
            if not ok_set: continue
            ep  = bc * ep_mult
            qty = (equity * RISK_PCT / 100.0) / sd
            not_ = qty * ep
            mrg  = not_ / lev
            fee  = not_ * TAKER_FEE
            if mrg + fee > avail:
                scale = avail / (mrg + fee)
                qty *= scale; not_ = qty * ep
                mrg = not_ / lev; fee = not_ * TAKER_FEE
            if not_ >= 2.0:
                equity -= fee
                sl_p = ep - sd if side == 1 else ep + sd
                tp_p = ep + rr * sd if side == 1 else ep - rr * sd
                positions.append([side, ep, qty, not_, mrg, sl_p, tp_p, i])
            break  # one trade per bar

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
    months    = 24.0
    monthly   = ((1 + ret_pct/100) ** (1/months) - 1) * 100 if ret_pct > -100 else -99.0

    return dict(ret=ret_pct, monthly=monthly, wr=wr, rr=actual_rr,
                trades=n_trades, liqs=liqs)


# ─────────────────────────────────────────────────────────────────────────── #
#  Main                                                                         #
# ─────────────────────────────────────────────────────────────────────────── #

def main():
    print("\nHAWK Volume Filter Study")
    print("=" * 70)
    print(f"Vol Z-score threshold: mean + {VOL_ZSCORE_K}×std  (lookback={VOL_LOOKBACK} bars)")
    print(f"Body ratio threshold : 0.50 (close in upper/lower half)")
    print()

    rows = []

    for cfg in BEST_CONFIGS:
        asset = cfg["asset"]
        tf    = cfg["tf"]
        fname = f"{asset}_{tf}.csv"
        path  = os.path.join(DATA_DIR, fname)
        if not os.path.exists(path):
            print(f"  SKIP {fname} — not found")
            continue

        df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
        df.columns = [c.lower() for c in df.columns]
        arr = prep_arrays(df, cfg["ch"])

        print(f"\n{'─'*70}")
        print(f"  {cfg['label']}  |  {asset} {tf}  lev={cfg['lev']}x  ch={cfg['ch']}  "
              f"SL={cfg['sl']}x  RR={cfg['rr']}  ADX={'%.0f'%cfg['adx'] if cfg['adx']>0 else 'off'}  "
              f"RSI={'on' if cfg['rsi'] else 'off'}  MACD={'on' if cfg['macd'] else 'off'}")
        print(f"  {'Vol':>5} {'Body':>5} | {'Return%':>8} {'Mo%':>7} {'WR%':>6} {'RR':>5} {'T':>4} {'Liqs':>5}  {'Delta Mo%':>10}")

        baseline = None
        for vol_on in [False, True]:
            for body_on in [False, True]:
                r = run_backtest(arr, cfg, vol_filter=vol_on, body_filter=body_on)
                tag = "BASE" if (not vol_on and not body_on) else ""
                delta = ""
                if baseline is None:
                    baseline = r["monthly"]
                else:
                    d = r["monthly"] - baseline
                    delta = f"{d:+.2f}%"

                print(f"  {'on' if vol_on else 'off':>5} {'on' if body_on else 'off':>5} | "
                      f"{r['ret']:>8.1f}% {r['monthly']:>7.2f}% {r['wr']:>6.1f}% "
                      f"{r['rr']:>5.2f} {r['trades']:>4d} {r['liqs']:>5d}  "
                      f"{delta:>10}  {tag}")

                rows.append(dict(
                    label=cfg["label"], asset=asset, tf=tf,
                    lev=cfg["lev"], ch=cfg["ch"], sl=cfg["sl"], rr=cfg["rr"],
                    adx=cfg["adx"], rsi=cfg["rsi"], macd=cfg["macd"],
                    vol_filter=vol_on, body_filter=body_on,
                    ret_pct=round(r["ret"],2), monthly_pct=round(r["monthly"],2),
                    wr=round(r["wr"],1), actual_rr=round(r["rr"],2),
                    trades=r["trades"], liqs=r["liqs"],
                    delta_monthly=round(r["monthly"]-baseline, 2),
                ))

    # Save results
    out = pd.DataFrame(rows)
    out.to_csv(RESULT_CSV, index=False)

    # Summary: where do the new filters help?
    print(f"\n{'='*70}")
    print("SUMMARY — Filters that IMPROVE monthly% vs baseline:")
    print(f"  {'Label':<22} {'Filters':<14} {'Delta Mo%':>10} {'WR%':>6} {'RR':>5} {'T':>4}")
    for _, row in out.iterrows():
        if row["vol_filter"] or row["body_filter"]:
            d = row["delta_monthly"]
            if d > 0:
                filt = ("vol+" if row["vol_filter"] else "") + ("body" if row["body_filter"] else "")
                print(f"  {row['label']:<22} {filt:<14} {d:>+10.2f}% {row['wr']:>6.1f}% "
                      f"{row['actual_rr']:>5.2f} {row['trades']:>4d}")

    print(f"\nResults saved → {RESULT_CSV}")
    print()


if __name__ == "__main__":
    main()
