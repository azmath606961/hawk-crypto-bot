"""
HAWK v6 Backtester — Best Indicators Study
==========================================
Extends HAWK v5 with three new filters to find strategies targeting 10%+/month:

  ADX(14)       — Trend strength gate. Only trade when ADX > threshold.
                  Eliminates channel-breakout false signals in choppy/ranging markets.

  RSI(14)       — Momentum confirmation. Longs need RSI > rsi_long_min (default 50),
                  shorts need RSI < rsi_short_max (default 50). Avoids entering
                  when momentum is exhausted or reversing.

  Supertrend    — ATR-based trend direction. Faster than EMA crossover;
                  confirms the breakout direction with a dynamic trailing band.

New assets tested (all at 1h, same HAWK v5 signal logic):
  XRP/USDT, BNB/USDT, ADA/USDT — adds signal frequency to fill the 60% margin cap.

Grid search on ETH 1h (the proven base) across:
  ADX min      : None, 20, 25
  RSI filter   : off, RSI>50 for longs / <50 for shorts
  Supertrend   : off, on (period=10, mult=3)
  RR           : 2.0, 2.5, 3.0

Then the best filter combo is applied to all assets.

Position sizing — unchanged (R1):
  risk_usdt = equity * 1.5%
  qty       = risk_usdt / sl_dist      # NEVER * leverage
  margin    = qty * price / leverage
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
import requests
from backtester.leveraged_engine import GBP_TO_USDT, TAKER_FEE, FUNDING_RATE_8H

DATA_DIR  = os.path.join(os.path.dirname(__file__), "..", "data")
START_GBP = 500.0
LEVERAGE  = 10
RISK_PCT  = 1.5

BINANCE_KLINE = "https://api.binance.com/api/v3/klines"

# ─────────────────────────────────────────────────────────────────────────── #
#  Indicator library                                                            #
# ─────────────────────────────────────────────────────────────────────────── #

def _ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()


def _atr(h: pd.Series, l: pd.Series, c: pd.Series, p: int = 14) -> pd.Series:
    tr = pd.concat([h - l,
                    (h - c.shift(1)).abs(),
                    (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / p, adjust=False).mean()


def _rsi(c: pd.Series, p: int = 14) -> pd.Series:
    """Wilder RSI via EMA of gains/losses."""
    delta    = c.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / p, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / p, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def _adx(h: pd.Series, l: pd.Series, c: pd.Series, p: int = 14) -> pd.Series:
    """ADX via Wilder smoothing.  Returns the ADX series (0-100)."""
    atr14  = _atr(h, l, c, p)
    up     = h.diff()
    dn     = -l.diff()
    dm_pos = up.where((up > dn) & (up > 0), 0.0)
    dm_neg = dn.where((dn > up) & (dn > 0), 0.0)
    di_pos = 100 * dm_pos.ewm(alpha=1 / p, adjust=False).mean() / atr14.replace(0, 1e-10)
    di_neg = 100 * dm_neg.ewm(alpha=1 / p, adjust=False).mean() / atr14.replace(0, 1e-10)
    dx     = 100 * (di_pos - di_neg).abs() / (di_pos + di_neg).replace(0, 1e-10)
    return dx.ewm(alpha=1 / p, adjust=False).mean()


def _supertrend(h: pd.Series, l: pd.Series, c: pd.Series,
                period: int = 10, mult: float = 3.0) -> pd.Series:
    """
    Returns a boolean Series: True = bullish (price above Supertrend line).
    Computed via a fast vectorised approximation with a single forward pass.
    """
    atr      = _atr(h, l, c, period)
    hl2      = (h + l) / 2
    upper_b  = (hl2 + mult * atr).values.copy()
    lower_b  = (hl2 - mult * atr).values.copy()
    close    = c.values.copy()
    n        = len(c)

    direction = np.ones(n, dtype=np.int8)   # 1 = bull, -1 = bear
    st        = np.zeros(n, dtype=np.float64)
    st[0]     = lower_b[0]

    for i in range(1, n):
        # Adjust bands — only tighten, never widen
        if close[i - 1] <= upper_b[i - 1]:
            upper_b[i] = min(upper_b[i], upper_b[i - 1])
        if close[i - 1] >= lower_b[i - 1]:
            lower_b[i] = max(lower_b[i], lower_b[i - 1])

        # Flip / keep direction
        if direction[i - 1] == -1 and close[i] > st[i - 1]:
            direction[i] = 1
            st[i]        = lower_b[i]
        elif direction[i - 1] == 1 and close[i] < st[i - 1]:
            direction[i] = -1
            st[i]        = upper_b[i]
        elif direction[i - 1] == 1:
            direction[i] = 1
            st[i]        = lower_b[i]
        else:
            direction[i] = -1
            st[i]        = upper_b[i]

    return pd.Series(direction == 1, index=c.index)


# ─────────────────────────────────────────────────────────────────────────── #
#  HAWKv6Engine                                                                #
# ─────────────────────────────────────────────────────────────────────────── #

class HAWKv6Engine:
    """
    HAWK v5 channel-breakout engine extended with ADX / RSI / Supertrend filters.

    All new filter params default to off (None / False) so the baseline result
    matches hawk_backtest_multi.py exactly when all filters are disabled.
    """

    def __init__(
        self,
        df:              pd.DataFrame,
        initial_gbp:     float = 500.0,
        leverage:        int   = 10,
        risk_pct:        float = 1.5,
        rr:              float = 2.0,
        channel_n:       int   = 8,
        ema_fast:        int   = 20,
        ema_slow:        int   = 50,
        sl_atr_mult:     float = 1.5,
        max_hold_bars:   int   = 30,
        funding_bars:    int   = 8,
        max_margin_pct:  float = 0.60,
        max_dd_pct:      float = 30.0,
        # --- new v6 filters ---
        adx_min:         float | None = None,   # e.g. 20.0; None = disabled
        rsi_filter:      bool         = False,   # True -> RSI>50 long, RSI<50 short
        rsi_long_min:    float        = 50.0,
        rsi_short_max:   float        = 50.0,
        supertrend:      bool         = False,   # True -> Supertrend must agree
        st_period:       int          = 10,
        st_mult:         float        = 3.0,
    ) -> None:
        self._df      = df
        self._equity  = initial_gbp * GBP_TO_USDT
        self._init    = self._equity
        self._lev     = leverage
        self._risk    = risk_pct
        self._rr      = rr
        self._chan_n  = channel_n
        self._etf     = ema_fast
        self._ets     = ema_slow
        self._sl_m    = sl_atr_mult
        self._mhold   = max_hold_bars
        self._fund    = funding_bars
        self._maxmar  = max_margin_pct
        self._max_dd  = max_dd_pct
        self._adx_min = adx_min
        self._rsi_f   = rsi_filter
        self._rsi_lo  = rsi_long_min
        self._rsi_sh  = rsi_short_max
        self._st      = supertrend
        self._stp     = st_period
        self._stm     = st_mult

        sl_pct_est       = sl_atr_mult * 0.0066
        margin_per_trade = risk_pct / 100 / (sl_pct_est * leverage)
        self._max_pos    = min(3, max(1, int(max_margin_pct / margin_per_trade))) \
                           if margin_per_trade < max_margin_pct else 1

        self._peak   = self._equity
        self._trades: list[dict] = []
        self._eq_log: list[float] = []
        self._liqs   = 0
        self._fund_drag = 0.0

    # ── Run ──────────────────────────────────────────────────────────────── #

    def run(self) -> dict:
        df = self._df
        c  = df["close"]; h = df["high"]; l = df["low"]

        # Core indicators (always computed)
        ema_f    = _ema(c, self._etf)
        ema_s    = _ema(c, self._ets)
        atr14    = _atr(h, l, c, 14)
        chan_hi  = h.rolling(self._chan_n).max().shift(1)
        chan_lo  = l.rolling(self._chan_n).min().shift(1)

        # Optional indicators
        adx_ser  = _adx(h, l, c, 14)             if self._adx_min else None
        rsi_ser  = _rsi(c, 14)                   if self._rsi_f   else None
        st_bull  = _supertrend(h, l, c,
                               self._stp,
                               self._stm)        if self._st      else None

        WARMUP = self._ets + self._chan_n + 20
        positions: list[dict] = []
        last_close_bar = -999
        liq_dist = 1.0 / self._lev - 0.005

        for i in range(WARMUP, len(df)):
            bc   = float(c.iloc[i])
            bh   = float(h.iloc[i])
            bl   = float(l.iloc[i])
            batr = float(atr14.iloc[i])
            betf = float(ema_f.iloc[i])
            bets = float(ema_s.iloc[i])
            bch  = float(chan_hi.iloc[i])
            bcl  = float(chan_lo.iloc[i])

            if any(pd.isna(x) for x in [batr, betf, bets, bch, bcl]):
                self._eq_log.append(self._equity)
                continue

            # Drawdown halt
            if (1 - self._equity / self._peak) * 100 >= self._max_dd:
                break

            # Funding
            if i % self._fund == 0:
                for pos in positions:
                    fee = pos["notional"] * FUNDING_RATE_8H
                    if pos["side"] == "long":
                        self._equity    -= fee
                        self._fund_drag += fee
                    else:
                        self._equity += fee

            # Manage open positions
            closed: list[dict] = []
            for pos in positions:
                ep = None; reason = ""
                if pos["side"] == "long":
                    liq_p = pos["entry"] * (1 - liq_dist)
                    if bl <= liq_p:
                        ep = liq_p; reason = "liq"
                    elif bl <= pos["sl"]:
                        ep = pos["sl"] * 0.9995; reason = "sl"
                    elif bh >= pos["tp"]:
                        ep = pos["tp"] * 0.9995; reason = "tp"
                    elif i - pos["bar_in"] >= self._mhold:
                        ep = bc; reason = "timeout"
                else:
                    liq_p = pos["entry"] * (1 + liq_dist)
                    if bh >= liq_p:
                        ep = liq_p; reason = "liq"
                    elif bh >= pos["sl"]:
                        ep = pos["sl"] * 1.0005; reason = "sl"
                    elif bl <= pos["tp"]:
                        ep = pos["tp"] * 1.0005; reason = "tp"
                    elif i - pos["bar_in"] >= self._mhold:
                        ep = bc; reason = "timeout"

                if ep is not None:
                    fee = pos["qty"] * ep * TAKER_FEE
                    if reason == "liq":
                        pnl = -pos["margin"]; self._liqs += 1
                    else:
                        raw = (ep - pos["entry"]) if pos["side"] == "long" \
                              else (pos["entry"] - ep)
                        pnl = raw * pos["qty"] - fee
                    self._equity += pnl
                    if self._equity > self._peak:
                        self._peak = self._equity
                    self._trades.append({
                        "side": pos["side"], "entry": pos["entry"],
                        "exit": ep, "pnl_usdt": pnl, "reason": reason,
                        "eq_after": self._equity, "hold_bars": i - pos["bar_in"],
                    })
                    closed.append(pos)

            for p in closed:
                positions.remove(p)
            if closed:
                last_close_bar = i

            self._eq_log.append(self._equity)

            # ── Entry gate ────────────────────────────────────────────────
            if i - last_close_bar < 1:
                continue
            if len(positions) >= self._max_pos:
                continue

            total_margin = sum(p["margin"] for p in positions)
            avail_margin = self._equity * self._maxmar - total_margin

            ema_bull = betf > bets
            ema_bear = betf < bets

            # --- v6 filters ---
            adx_ok = True
            if self._adx_min and adx_ser is not None:
                val = float(adx_ser.iloc[i])
                adx_ok = (not pd.isna(val)) and (val >= self._adx_min)

            rsi_long_ok = rsi_short_ok = True
            if self._rsi_f and rsi_ser is not None:
                rv = float(rsi_ser.iloc[i])
                if not pd.isna(rv):
                    rsi_long_ok  = rv >= self._rsi_lo
                    rsi_short_ok = rv <= self._rsi_sh

            st_long_ok = st_short_ok = True
            if self._st and st_bull is not None:
                st_v = bool(st_bull.iloc[i])
                st_long_ok  = st_v
                st_short_ok = not st_v

            # --- Entry ---
            if ema_bull and adx_ok and rsi_long_ok and st_long_ok and bc > bch:
                ep   = bc * 1.0005
                sd   = self._sl_m * batr
                pos  = self._open(
                    "long", ep, ep - sd, ep + self._rr * sd, sd, i, avail_margin)
                if pos:
                    positions.append(pos)

            elif ema_bear and adx_ok and rsi_short_ok and st_short_ok and bc < bcl:
                ep   = bc * 0.9995
                sd   = self._sl_m * batr
                pos  = self._open(
                    "short", ep, ep + sd, ep - self._rr * sd, sd, i, avail_margin)
                if pos:
                    positions.append(pos)

        # Close remaining at end
        for pos in positions:
            lp  = float(df["close"].iloc[-1])
            fee = pos["qty"] * lp * TAKER_FEE
            raw = (lp - pos["entry"]) if pos["side"] == "long" else (pos["entry"] - lp)
            self._equity += raw * pos["qty"] - fee
            self._trades.append({
                "side": pos["side"], "entry": pos["entry"], "exit": lp,
                "pnl_usdt": raw * pos["qty"] - fee, "reason": "eod",
                "eq_after": self._equity, "hold_bars": len(df) - 1 - pos["bar_in"],
            })

        return self._build()

    def _open(self, side, entry, sl, tp, sl_dist, bar_idx, avail):
        risk_usdt = self._equity * self._risk / 100
        qty       = risk_usdt / sl_dist          # leverage-independent (R1)
        notional  = qty * entry
        margin    = notional / self._lev
        fee       = notional * TAKER_FEE

        if margin + fee > avail:
            scale    = avail / (margin + fee)
            qty     *= scale
            notional = qty * entry
            margin   = notional / self._lev
            fee      = notional * TAKER_FEE

        if notional < 2.0:
            return None

        self._equity -= fee
        return {"side": side, "entry": entry, "qty": qty,
                "sl": sl, "tp": tp, "notional": notional,
                "margin": margin, "bar_in": bar_idx}

    def _build(self) -> dict:
        tdf  = pd.DataFrame(self._trades)
        eqs  = pd.Series(self._eq_log if self._eq_log else [self._init])
        n    = len(tdf)
        final_gbp = self._equity / GBP_TO_USDT
        ret_pct   = (self._equity / self._init - 1) * 100
        peak_gbp  = eqs.max() / GBP_TO_USDT

        if n == 0:
            return {"trades": 0, "wr": 0, "rr": 0, "return": ret_pct,
                    "final_gbp": final_gbp, "peak_gbp": peak_gbp,
                    "max_dd": 0, "liqs": self._liqs, "avg_hold": 0,
                    "eq_log": eqs, "tdf": tdf}

        wins = tdf[tdf.pnl_usdt > 0]; loss = tdf[tdf.pnl_usdt <= 0]
        wr   = len(wins) / n * 100
        rr_a = (wins.pnl_usdt.mean() / abs(loss.pnl_usdt.mean())
                if len(wins) and len(loss) else 0.0)
        dd   = (eqs / eqs.cummax() - 1) * 100

        return {"trades": n, "wr": wr, "rr": rr_a, "return": ret_pct,
                "final_gbp": final_gbp, "peak_gbp": peak_gbp,
                "max_dd": float(dd.min()), "liqs": self._liqs,
                "avg_hold": float(tdf.hold_bars.dropna().mean()),
                "eq_log": eqs, "tdf": tdf}


# ─────────────────────────────────────────────────────────────────────────── #
#  Data helpers                                                                 #
# ─────────────────────────────────────────────────────────────────────────── #

def _load(fname: str) -> pd.DataFrame:
    path = os.path.join(DATA_DIR, fname)
    df   = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    df.columns = [c.lower() for c in df.columns]
    return df


def _days(df: pd.DataFrame) -> float:
    return (df.index[-1] - df.index[0]).total_seconds() / 86_400


def _download_1h(symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Download 1h OHLCV from Binance public REST, paginate automatically."""
    bar_ms  = 3_600_000
    all_bars: list = []
    cursor  = start_ms
    while cursor < end_ms:
        resp = requests.get(
            BINANCE_KLINE,
            params={"symbol": symbol, "interval": "1h",
                    "startTime": cursor, "endTime": end_ms, "limit": 1000},
            timeout=20,
        )
        resp.raise_for_status()
        chunk = resp.json()
        if not chunk:
            break
        all_bars.extend(chunk)
        cursor = int(chunk[-1][0]) + bar_ms
        time.sleep(0.15)
        if len(chunk) < 1000:
            break

    df = pd.DataFrame(all_bars, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "tb", "tq", "_"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]].astype(float)
    now_ms = int(time.time() * 1000)
    last_ms = int(pd.Timestamp(df.index[-1]).timestamp() * 1000)
    if now_ms - last_ms < bar_ms:
        df = df.iloc[:-1]
    return df


def _ensure_asset(symbol: str, fname: str, ref_df: pd.DataFrame) -> pd.DataFrame | None:
    """Load from CSV or download from Binance if not present."""
    path = os.path.join(DATA_DIR, fname)
    if os.path.exists(path):
        df = _load(fname)
        print(f"  LOAD  {fname} ({len(df)} bars)")
        return df

    print(f"  DOWNLOADING {fname} from Binance ...", end=" ", flush=True)
    try:
        start_ms = int(ref_df.index[0].timestamp() * 1000)
        end_ms   = int(ref_df.index[-1].timestamp() * 1000)
        df = _download_1h(symbol, start_ms, end_ms)
        df.index.name = "timestamp"
        df.to_csv(path)
        print(f"OK ({len(df)} bars)")
        return df
    except Exception as e:
        print(f"FAILED: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────── #
#  EV / monthly return helper                                                  #
# ─────────────────────────────────────────────────────────────────────────── #

def _monthly_pct(r: dict, days: float, risk_pct: float = 1.5) -> float:
    """Estimate monthly compound return from actual equity curve."""
    if r["trades"] < 5 or days < 10:
        return 0.0
    # Derive from actual final equity (most accurate)
    months = days / 30.44
    return ((r["final_gbp"] / START_GBP) ** (1 / months) - 1) * 100


def _ev_label(r: dict) -> str:
    wr = r["wr"] / 100
    rr = r["rr"]
    ev = wr * rr - (1 - wr)
    return f"EV={ev:+.3f}"


# ─────────────────────────────────────────────────────────────────────────── #
#  Grid search (ETH 1h — finds best filter combo)                              #
# ─────────────────────────────────────────────────────────────────────────── #

def grid_search_eth(df_eth: pd.DataFrame) -> dict:
    days  = _days(df_eth)
    grid  = []

    # ADX thresholds: None = disabled, 20 = standard, 25 = strong trend only
    adx_opts = [None, 20.0, 25.0]
    rsi_opts  = [False, True]
    st_opts   = [False, True]
    rr_opts   = [2.0, 2.5, 3.0]

    total = len(adx_opts) * len(rsi_opts) * len(st_opts) * len(rr_opts)
    done  = 0

    print(f"\n  Grid search: {total} combos on ETH 1h ...")
    for adx in adx_opts:
        for rsi in rsi_opts:
            for st in st_opts:
                for rr in rr_opts:
                    done += 1
                    label = (f"ADX={adx or 'off':>4}  RSI={'on' if rsi else 'off'}  "
                             f"ST={'on' if st else 'off'}  RR={rr:.1f}")
                    print(f"  [{done:02d}/{total}] {label}", end=" ... ", flush=True)

                    eng = HAWKv6Engine(
                        df=df_eth, initial_gbp=START_GBP, leverage=LEVERAGE,
                        risk_pct=RISK_PCT, rr=rr,
                        channel_n=8, ema_fast=20, ema_slow=50,
                        sl_atr_mult=1.5, max_hold_bars=30, funding_bars=8,
                        adx_min=adx, rsi_filter=rsi, supertrend=st,
                    )
                    r   = eng.run()
                    mo  = _monthly_pct(r, days)
                    tpd = r["trades"] / max(days, 1)
                    ev  = _ev_label(r)
                    print(f"ret={r['return']:+6.1f}%  WR={r['wr']:.1f}%  RR={r['rr']:.2f}  "
                          f"T={r['trades']}  {ev}  mo%={mo:+.2f}%")

                    grid.append({
                        "label": label, "r": r, "days": days,
                        "adx": adx, "rsi": rsi, "st": st, "rr": rr,
                        "monthly_pct": mo, "tpd": tpd,
                    })

    # Best by monthly%
    valid = [g for g in grid if g["r"]["trades"] >= 20]
    best  = max(valid, key=lambda g: g["monthly_pct"]) if valid else grid[0]
    return best


# ─────────────────────────────────────────────────────────────────────────── #
#  Full asset backtest using best filter params                                 #
# ─────────────────────────────────────────────────────────────────────────── #

ASSET_CONFIGS = {
    # name      : (csv_fname,           binance_sym,  channel_n, max_hold, funding_bars, use_best_filter)
    "ETH 1h"    : ("ETHUSDT_1h.csv",   "ETHUSDT",     8,  30,  8,  True),
    "BTC 4h"    : ("BTCUSDT_4h.csv",   "BTCUSDT",    12,  12,  2,  False),  # orig params; ADX hurts 4h
    "SOL 4h"    : ("SOLUSDT_4h.csv",   "SOLUSDT",    12,   8,  2,  False),
    "XRP 1h"    : ("XRPUSDT_1h.csv",   "XRPUSDT",     8,  30,  8,  True),
    "BNB 1h"    : ("BNBUSDT_1h.csv",   "BNBUSDT",     8,  30,  8,  True),
    "ADA 1h"    : ("ADAUSDT_1h.csv",   "ADAUSDT",     8,  30,  8,  True),
}


def run_all_assets(best: dict, ref_df: pd.DataFrame) -> list[dict]:
    results = []
    for name, (fname, binance_sym, chan_n, hold, fund_bars, use_filter) in ASSET_CONFIGS.items():
        df = _ensure_asset(binance_sym, fname, ref_df)
        if df is None:
            print(f"  SKIP {name} (no data)")
            continue

        days = _days(df)
        print(f"\n  Running {name} ...", end=" ", flush=True)

        # Apply best filter to 1h assets; keep original params for 4h assets
        if use_filter:
            adx = best["adx"]
            rsi = best["rsi"]
            st  = best["st"]
            rr  = best["rr"]
        else:
            # 4h strategies: original proven params (no ADX — hurts 4h)
            adx = None
            rsi = False
            st  = False
            rr  = 2.0

        eng = HAWKv6Engine(
            df=df, initial_gbp=START_GBP, leverage=LEVERAGE,
            risk_pct=RISK_PCT, rr=rr,
            channel_n=chan_n, ema_fast=20, ema_slow=50,
            sl_atr_mult=1.5, max_hold_bars=hold, funding_bars=fund_bars,
            adx_min=adx, rsi_filter=rsi, supertrend=st,
        )
        r  = eng.run()
        mo = _monthly_pct(r, days)
        filters = f"ADX={adx or 'off'} RR={rr}" if use_filter else "orig params"
        print(f"[{filters}]  ret={r['return']:+6.1f}%  WR={r['wr']:.1f}%  "
              f"RR={r['rr']:.2f}  T={r['trades']}  mo%={mo:+.2f}%  liqs={r['liqs']}")

        results.append({
            "name": name, "r": r, "days": days,
            "monthly_pct": mo, "tpd": r["trades"] / max(days, 1),
        })

    return results


# ─────────────────────────────────────────────────────────────────────────── #
#  Main                                                                         #
# ─────────────────────────────────────────────────────────────────────────── #

def main() -> None:
    print("\n" + "=" * 72)
    print("  HAWK v6 — Best Indicators Study")
    print("  Indicators: ADX(14) + RSI(14) + Supertrend(10,3)")
    print("  New assets: XRP, BNB, ADA (auto-download if missing)")
    print(f"  Capital: GBP {START_GBP:.0f}  |  Leverage: {LEVERAGE}x  "
          f"|  Risk: {RISK_PCT}%/trade")
    print("=" * 72)

    # Reference data
    print("\nLoading reference data ...")
    df_eth_1h = _load("ETHUSDT_1h.csv")

    # ── Step 1: Grid search on ETH 1h ──────────────────────────────────── #
    print("\n" + "#" * 72)
    print("  STEP 1: Grid search on ETH 1h (proven base)")
    print("#" * 72)
    best = grid_search_eth(df_eth_1h)

    print(f"\n  BEST COMBO: {best['label']}")
    print(f"  Return={best['r']['return']:+.1f}%  WR={best['r']['wr']:.1f}%  "
          f"RR={best['r']['rr']:.2f}  Trades={best['r']['trades']}  "
          f"Monthly={best['monthly_pct']:+.2f}%  Liqs={best['r']['liqs']}")

    # ── Step 2: Apply best filters to all assets ────────────────────────── #
    print("\n" + "#" * 72)
    print(f"  STEP 2: All assets with best filters  [{best['label']}]")
    print("#" * 72)
    results = run_all_assets(best, df_eth_1h)

    # ── Step 3: Summary table ────────────────────────────────────────────── #
    print("\n" + "=" * 90)
    print("  HAWK v6 RESULTS — All Assets with Best Filter Combo")
    print("=" * 90)
    print(f"  Filters applied: {best['label']}")
    print(f"  {'Asset':<10}  {'Return':>8}  {'WR%':>5}  {'RR':>5}  "
          f"{'Trades':>7}  {'T/Day':>6}  {'MaxDD':>7}  {'Liqs':>5}  "
          f"{'Monthly%':>9}  {'EV':>9}")
    print("  " + "-" * 80)

    positive_results = []
    for item in results:
        r   = item["r"]
        tpd = item["tpd"]
        mo  = item["monthly_pct"]
        ev  = _ev_label(r)
        marker = " <--LIVE" if mo > 2.0 and r["liqs"] <= 6 and r["trades"] >= 20 else ""
        print(f"  {item['name']:<10}  {r['return']:>+7.1f}%  {r['wr']:>5.1f}%  "
              f"{r['rr']:>5.2f}  {r['trades']:>7}  {tpd:>6.2f}  "
              f"{r['max_dd']:>+6.1f}%  {r['liqs']:>5}  {mo:>+8.2f}%  {ev:>9}{marker}")
        if mo > 0 and r["trades"] >= 20:
            positive_results.append(item)

    # ── Step 4: Combined portfolio math ──────────────────────────────────── #
    print("\n" + "=" * 72)
    print("  COMBINED PORTFOLIO ESTIMATE")
    print("=" * 72)

    import math

    def fmt_m(m):
        if m is None or not math.isfinite(m):
            return "   never"
        y, mo = divmod(int(round(m)), 12)
        return f"{y}y {mo:02d}m" if y else f"  {mo:02d}m"

    def months_to(start, end, r_mo):
        if r_mo <= 0:
            return float("inf")
        return math.log(end / start) / math.log(1 + r_mo / 100)

    if positive_results:
        # Show individual contributions
        print(f"\n  Positive strategies contributing to portfolio:")
        total_mo = 0.0
        total_tpd = 0.0
        for item in positive_results:
            print(f"    {item['name']:<10}  {item['monthly_pct']:+.2f}%/mo  "
                  f"{item['tpd']:.2f} T/day")
            total_mo  += item["monthly_pct"]
            total_tpd += item["tpd"]

        print(f"\n  Combined (all run in parallel, shared equity pool):")
        print(f"    Monthly rate  : {total_mo:+.2f}%/month")
        print(f"    Trades/day    : {total_tpd:.1f}")

        m_1k    = months_to(500,    1_000,   total_mo)
        m_10k   = months_to(1_000,  10_000,  total_mo)
        m_100k  = months_to(10_000, 100_000, total_mo)
        m_total = months_to(500,    100_000, total_mo)

        print(f"\n  Roadmap to GBP 100,000:")
        print(f"    GBP 500  ->  1k  : {fmt_m(m_1k)}")
        print(f"    GBP 1k   -> 10k  : {fmt_m(m_10k)}")
        print(f"    GBP 10k  -> 100k : {fmt_m(m_100k)}")
        print(f"    GBP 500  -> 100k : {fmt_m(m_total)}  (TOTAL)")

        target_10pct = total_mo >= 10.0
        print(f"\n  10%/month target: {'ACHIEVED' if target_10pct else 'NOT YET'} "
              f"({total_mo:.2f}% vs 10.00% target)")

        if not target_10pct:
            shortfall = 10.0 - total_mo
            print(f"  Shortfall: {shortfall:.2f}%/mo")
            print(f"  To close the gap: add {math.ceil(shortfall / (total_mo / max(len(positive_results),1)))}"
                  f" more similar-performing assets OR improve WR by "
                  f"{shortfall / (total_tpd * 30 * RISK_PCT / 100) * 100:.1f}pp")

    # ── Step 5: Baseline comparison ──────────────────────────────────────── #
    print("\n" + "=" * 72)
    print("  BASELINE vs v6 COMPARISON (ETH 1h)")
    print("=" * 72)

    print("  Running BASELINE (HAWK v5, no new filters) ...")
    eng_base = HAWKv6Engine(
        df=df_eth_1h, initial_gbp=START_GBP, leverage=LEVERAGE,
        risk_pct=RISK_PCT, rr=2.0,
        channel_n=8, ema_fast=20, ema_slow=50,
        sl_atr_mult=1.5, max_hold_bars=30, funding_bars=8,
        adx_min=None, rsi_filter=False, supertrend=False,
    )
    r_base = eng_base.run()
    days_base = _days(df_eth_1h)
    mo_base = _monthly_pct(r_base, days_base)

    eth_v6 = next((i for i in results if i["name"] == "ETH 1h"), None)

    print(f"\n  {'Metric':<20}  {'v5 Baseline':>12}  {'v6 Best':>12}  {'Delta':>10}")
    print("  " + "-" * 58)
    if eth_v6:
        rv6 = eth_v6["r"]
        mo6 = eth_v6["monthly_pct"]
        rows = [
            ("Return (2yr)",   f"{r_base['return']:+.1f}%",  f"{rv6['return']:+.1f}%",
             f"{rv6['return']-r_base['return']:+.1f}pp"),
            ("Win rate",       f"{r_base['wr']:.1f}%",       f"{rv6['wr']:.1f}%",
             f"{rv6['wr']-r_base['wr']:+.1f}pp"),
            ("Actual RR",      f"{r_base['rr']:.2f}",        f"{rv6['rr']:.2f}",
             f"{rv6['rr']-r_base['rr']:+.2f}"),
            ("Trades",         str(r_base["trades"]),         str(rv6["trades"]),
             str(rv6["trades"] - r_base["trades"])),
            ("Liquidations",   str(r_base["liqs"]),           str(rv6["liqs"]),
             str(rv6["liqs"] - r_base["liqs"])),
            ("Monthly%",       f"{mo_base:+.2f}%",           f"{mo6:+.2f}%",
             f"{mo6-mo_base:+.2f}pp"),
        ]
        for label, base_v, v6_v, delta in rows:
            print(f"  {label:<20}  {base_v:>12}  {v6_v:>12}  {delta:>10}")

    print("\n" + "=" * 72)
    print("  NEXT STEPS")
    print("=" * 72)
    print(f"  Best filter combo for paper trader:  {best['label']}")
    print(f"  Add to hawk_paper_trader.py:         All assets with monthly% > 2%")
    print(f"  Live rule (R7):                      30+ paper trades, positive EV first")
    print("=" * 72 + "\n")


if __name__ == "__main__":
    main()
