"""
HAWK Crypto Bot — Multi-Timeframe Backtester v1
================================================
Tests 4 strategy configurations designed to increase trade frequency
and fix BTC/SOL profitability:

  Strategy A — ETH 1hr + 4hr macro gate
    Existing HAWK v5 signal, but gated by 4hr EMA direction.
    Expected: fewer bad trades, better quality signals.

  Strategy B — ETH 30m channel + volume + 1hr + 4hr bias
    Same breakout logic on 30m bars.  Volume spike filter removes fakeouts.
    Expected: ~2-3 trades/day vs 1.6 on 1hr alone.

  Strategy C — SOL 30m channel + volume + 1hr + 4hr bias
    Wider channel (10-bar) + stricter volume filter to handle SOL's spikes.
    Expected: ~2 trades/day, fixes SOL's 1hr unprofitability.

  Strategy D — BTC 4hr channel (12-bar)
    BTC trends more cleanly at 4h than 1h.  Wider channel cuts false breaks.
    Expected: 0.5-1 trade/day, fixes BTC's 1hr unprofitability.

POSITION SIZING — unchanged from HAWK v5 (leverage-independent):
    risk_usdt  = equity × risk_pct / 100
    sl_dist    = sl_atr_mult × ATR(entry_timeframe)
    qty        = risk_usdt / sl_dist      ← NEVER multiply by leverage
    margin     = qty × price / leverage

Requirements:
    data/ETHUSDT_30m.csv  data/BTCUSDT_4h.csv  data/SOLUSDT_30m.csv
    data/ETHUSDT_4h.csv   (for macro bias on 30m strategies)
    Run: python scripts/download_multi_tf_data.py  to fetch them.
"""
from __future__ import annotations

import os
import sys
import warnings

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from backtester.leveraged_engine import GBP_TO_USDT, TAKER_FEE, FUNDING_RATE_8H

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


# ─────────────────────────────────────────────────────────────────────────── #
#  Indicators                                                                  #
# ─────────────────────────────────────────────────────────────────────────── #

def _ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()


def _atr(h: pd.Series, l: pd.Series, c: pd.Series, p: int = 14) -> pd.Series:
    tr = pd.concat([h - l,
                    (h - c.shift(1)).abs(),
                    (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample a OHLCV DataFrame to a coarser timeframe."""
    return df.resample(rule).agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna()


# ─────────────────────────────────────────────────────────────────────────── #
#  Multi-Timeframe HAWK Engine                                                 #
# ─────────────────────────────────────────────────────────────────────────── #

class MultiTFHAWKEngine:
    """
    Channel breakout engine that supports:
      - Any entry timeframe (30m, 1h, 4h, …)
      - Optional higher-TF bias gate (e.g. 1h when entry=30m)
      - Optional macro-TF bias gate (e.g. 4h for all strategies)
      - Volume spike filter (optional)
      - Correct leverage-independent position sizing (same formula as HAWK v5)

    Bias gates use the last *closed* higher-TF bar — no look-ahead.
    Forward-fill with .shift(1) before reindex ensures this.
    """

    def __init__(
        self,
        df_entry:        pd.DataFrame,              # entry TF OHLCV
        df_bias:         pd.DataFrame | None = None, # higher TF for direction gate
        df_macro:        pd.DataFrame | None = None, # macro TF for macro gate
        initial_gbp:     float = 500.0,
        leverage:        int   = 10,
        risk_pct:        float = 1.5,
        rr:              float = 2.0,
        max_dd_pct:      float = 30.0,
        allow_shorts:    bool  = True,
        channel_n:       int   = 8,
        ema_fast:        int   = 20,
        ema_slow:        int   = 50,
        sl_atr_mult:     float = 1.5,
        max_hold_bars:   int   = 60,   # bars on entry TF (60×30m = 30h, 8×4h ≈ 32h)
        cooldown_bars:   int   = 1,
        max_margin_pct:  float = 0.60,
        volume_filter:        bool  = False,
        volume_mult:          float = 1.15,
        funding_bars:         int   = 16,   # bars between funding deductions (16×30m=8h, 2×4h=8h)
        entry_ema_as_filter:  bool  = True, # False = skip entry-TF EMA check, use only bias/macro
    ) -> None:
        self._df      = df_entry
        self._bias    = df_bias
        self._macro   = df_macro
        self._equity  = initial_gbp * GBP_TO_USDT
        self._init    = self._equity
        self._lev     = leverage
        self._risk    = risk_pct
        self._rr      = rr
        self._max_dd  = max_dd_pct
        self._shorts  = allow_shorts
        self._chan_n  = channel_n
        self._etf     = ema_fast
        self._ets     = ema_slow
        self._sl_m    = sl_atr_mult
        self._mhold   = max_hold_bars
        self._cool    = cooldown_bars
        self._maxmar  = max_margin_pct
        self._volfil  = volume_filter
        self._volmult = volume_mult
        self._fundbars = funding_bars
        self._entry_ema = entry_ema_as_filter

        # Max concurrent positions (same formula as HAWKEngine)
        sl_pct_est       = sl_atr_mult * 0.0066
        margin_per_trade = risk_pct / 100 / (sl_pct_est * leverage)
        if margin_per_trade < max_margin_pct:
            self._max_pos = min(3, max(1, int(max_margin_pct / margin_per_trade)))
        else:
            self._max_pos = 1

        self._peak    = self._equity
        self._trades: list[dict] = []
        self._eq_log: list[float] = []
        self._funding = 0.0
        self._liqs    = 0

    # ──────────────────────────────────────────────────────────────────────── #
    #  Run                                                                      #
    # ──────────────────────────────────────────────────────────────────────── #

    def run(self) -> dict:
        df = self._df
        c  = df["close"]; h = df["high"]; l = df["low"]

        # Entry TF indicators
        ema_f    = _ema(c, self._etf)
        ema_s    = _ema(c, self._ets)
        atr14    = _atr(h, l, c, 14)
        chan_hi   = h.rolling(self._chan_n).max().shift(1)
        chan_lo   = l.rolling(self._chan_n).min().shift(1)
        vol_sma  = df["volume"].rolling(20).mean() if self._volfil else None

        # ── Higher-TF bias (e.g. 1h EMA when entry=30m) ──────────────────
        # .shift(1) on the higher-TF EMA prevents look-ahead:
        #   e.g. the 30m bar at 10:00 sees the 1h EMA from the bar closed at 09:00,
        #   not the EMA that incorporates the 10:00 bar (still open on 1h).
        bias_bull = bias_bear = None
        if self._bias is not None:
            bf = _ema(self._bias["close"], self._etf).shift(1).reindex(df.index, method="ffill")
            bs = _ema(self._bias["close"], self._ets).shift(1).reindex(df.index, method="ffill")
            bias_bull = bf > bs
            bias_bear = bf < bs

        # ── Macro-TF bias (4h) ────────────────────────────────────────────
        macro_bull = macro_bear = None
        if self._macro is not None:
            mf = _ema(self._macro["close"], self._etf).shift(1).reindex(df.index, method="ffill")
            ms = _ema(self._macro["close"], self._ets).shift(1).reindex(df.index, method="ffill")
            macro_bull = mf > ms
            macro_bear = mf < ms

        WARMUP = self._ets + self._chan_n + 5
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

            # ── Drawdown halt ──────────────────────────────────────────────
            dd = (1 - self._equity / self._peak) * 100
            if dd >= self._max_dd or self._equity <= 0:
                break

            # ── Combined directional bias ──────────────────────────────────
            # When entry_ema_as_filter=True:  entry TF EMA + bias/macro TFs must all agree.
            # When entry_ema_as_filter=False: direction comes only from bias/macro TFs.
            #   (useful for 30m entries where requiring BOTH 30m EMA AND 1h EMA kills signals)
            entry_bull = betf > bets
            entry_bear = betf < bets

            if self._entry_ema:
                bull = entry_bull
                bear = entry_bear
            else:
                # Start permissive; bias/macro gates narrow it below
                bull = True
                bear = True

            if bias_bull is not None:
                bull = bull and bool(bias_bull.iloc[i])
                bear = bear and bool(bias_bear.iloc[i])
            if macro_bull is not None:
                bull = bull and bool(macro_bull.iloc[i])
                bear = bear and bool(macro_bear.iloc[i])

            # When no bias/macro TFs provided, fall back to entry TF EMA
            if bias_bull is None and macro_bull is None:
                bull = entry_bull
                bear = entry_bear

            # ── Volume filter ──────────────────────────────────────────────
            vol_ok = True
            if self._volfil and vol_sma is not None:
                bvol   = float(df["volume"].iloc[i])
                bvsma  = float(vol_sma.iloc[i])
                vol_ok = (not pd.isna(bvsma)) and (bvsma > 0) and (bvol >= self._volmult * bvsma)

            # ── Funding every N bars (N = 8h in entry-TF bars) ───────────
            if i % self._fundbars == 0:
                for pos in positions:
                    fund = pos["notional"] * FUNDING_RATE_8H
                    if pos["side"] == "long":
                        self._equity -= fund; self._funding += fund
                    else:
                        self._equity += fund

            # ── Manage open positions ──────────────────────────────────────
            closed_this_bar: list[dict] = []

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
                        raw = ((ep - pos["entry"]) if pos["side"] == "long"
                               else (pos["entry"] - ep))
                        pnl = raw * pos["qty"] - fee
                    self._equity += pnl
                    if self._equity > self._peak:
                        self._peak = self._equity
                    self._trades.append({
                        "bar": i, "side": pos["side"],
                        "regime": "BULL" if entry_bull else "BEAR",
                        "entry": pos["entry"], "exit": ep,
                        "pnl_usdt": pnl, "reason": reason,
                        "eq_after": self._equity,
                        "hold_bars": i - pos["bar_in"],
                    })
                    closed_this_bar.append(pos)

            for p in closed_this_bar:
                positions.remove(p)
            if closed_this_bar:
                last_close_bar = i

            self._eq_log.append(self._equity)

            # ── Entry ──────────────────────────────────────────────────────
            if i - last_close_bar < self._cool:
                continue
            if len(positions) >= self._max_pos:
                continue

            total_margin = sum(p["margin"] for p in positions)
            margin_avail = self._equity * self._maxmar - total_margin

            if bull and vol_ok and bc > bch:
                existing_longs = sum(1 for p in positions if p["side"] == "long")
                if existing_longs < self._max_pos:
                    ep = bc * 1.0005
                    sd = self._sl_m * batr
                    pos = self._open_pos("long", ep, ep - sd, ep + self._rr * sd,
                                         sd, i, margin_avail)
                    if pos:
                        positions.append(pos)

            elif self._shorts and bear and vol_ok and bc < bcl:
                existing_shorts = sum(1 for p in positions if p["side"] == "short")
                if existing_shorts < self._max_pos:
                    ep = bc * 0.9995
                    sd = self._sl_m * batr
                    pos = self._open_pos("short", ep, ep + sd, ep - self._rr * sd,
                                         sd, i, margin_avail)
                    if pos:
                        positions.append(pos)

        # Close remaining positions at end of data
        for pos in positions:
            lp  = float(df["close"].iloc[-1])
            fee = pos["qty"] * lp * TAKER_FEE
            raw = ((lp - pos["entry"]) if pos["side"] == "long"
                   else (pos["entry"] - lp))
            pnl = raw * pos["qty"] - fee
            self._equity += pnl
            self._trades.append({
                "bar": len(df) - 1, "side": pos["side"], "regime": "END",
                "entry": pos["entry"], "exit": lp, "pnl_usdt": pnl,
                "reason": "eod", "eq_after": self._equity,
                "hold_bars": len(df) - 1 - pos["bar_in"],
            })

        return self._build_result()

    # ──────────────────────────────────────────────────────────────────────── #
    #  CORRECT position sizing: risk is constant regardless of leverage        #
    # ──────────────────────────────────────────────────────────────────────── #

    def _open_pos(
        self, side: str, entry: float, sl: float, tp: float,
        sl_dist: float, bar_idx: int, margin_avail: float
    ) -> dict | None:
        risk_usdt = self._equity * self._risk / 100
        qty       = risk_usdt / sl_dist        # leverage-independent
        notional  = qty * entry
        margin    = notional / self._lev       # leverage reduces margin needed
        fee       = notional * TAKER_FEE

        if margin + fee > margin_avail:
            scale    = margin_avail / (margin + fee)
            qty     *= scale
            notional = qty * entry
            margin   = notional / self._lev
            fee      = notional * TAKER_FEE

        if notional < 2.0:
            return None

        self._equity -= fee
        return {
            "side": side, "entry": entry, "qty": qty,
            "sl": sl, "tp": tp, "sl_dist": sl_dist,
            "notional": notional, "margin": margin,
            "bar_in": bar_idx,
        }

    def _build_result(self) -> dict:
        tdf  = pd.DataFrame(self._trades)
        eq_s = pd.Series(self._eq_log if self._eq_log else [self._init])
        n    = len(tdf)
        final_gbp = self._equity / GBP_TO_USDT
        ret_pct   = (self._equity / self._init - 1) * 100
        peak_gbp  = eq_s.max() / GBP_TO_USDT

        if n == 0:
            return {"trades": 0, "wr": 0, "rr": 0, "return": ret_pct,
                    "final_gbp": final_gbp, "peak_gbp": peak_gbp,
                    "funding": self._funding, "liqs": self._liqs,
                    "eq_log": eq_s, "tdf": tdf, "max_dd": 0,
                    "avg_hold": 0, "max_pos": self._max_pos}

        wins  = tdf[tdf.pnl_usdt > 0]; loss = tdf[tdf.pnl_usdt <= 0]
        wr    = len(wins) / n * 100
        rr_a  = (wins.pnl_usdt.mean() / abs(loss.pnl_usdt.mean())
                 if len(wins) > 0 and len(loss) > 0 else 0)
        dd    = (eq_s / eq_s.cummax() - 1) * 100
        max_dd = float(dd.min())
        avg_hold = float(tdf["hold_bars"].dropna().mean())

        return {"trades": n, "wr": wr, "rr": rr_a, "return": ret_pct,
                "final_gbp": final_gbp, "peak_gbp": peak_gbp,
                "funding": self._funding, "liqs": self._liqs,
                "eq_log": eq_s, "tdf": tdf, "max_dd": max_dd,
                "avg_hold": avg_hold, "max_pos": self._max_pos}


# ─────────────────────────────────────────────────────────────────────────── #
#  Data loading helpers                                                        #
# ─────────────────────────────────────────────────────────────────────────── #

def _load(fname: str) -> pd.DataFrame:
    path = os.path.join(DATA_DIR, fname)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Missing: {path}\n"
            f"Run:  python scripts/download_multi_tf_data.py"
        )
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    df.columns = [c.lower() for c in df.columns]
    return df


def _days(df: pd.DataFrame) -> float:
    delta = df.index[-1] - df.index[0]
    return delta.total_seconds() / 86_400


# ─────────────────────────────────────────────────────────────────────────── #
#  Print helpers                                                               #
# ─────────────────────────────────────────────────────────────────────────── #

def _print_row(label: str, r: dict, tpd: float, tag: str = "") -> None:
    print(f"  {label:<38}  {r['max_pos']:>5}  {r['return']:>+8.1f}%  "
          f"GBP{r['peak_gbp']:>7,.0f}  GBP{r['final_gbp']:>7,.0f}  "
          f"{r['max_dd']:>+7.1f}%  {r['trades']:>7}  {tpd:>6.1f}  "
          f"{r['wr']:>5.1f}%  {r['rr']:>6.2f}:1  {r['liqs']:>5}{tag}")


def _print_detail(label: str, r: dict, days: float) -> None:
    tdf = r["tdf"]
    if tdf.empty:
        print(f"  {label}: 0 trades")
        return
    wins = tdf[tdf.pnl_usdt > 0]; loss = tdf[tdf.pnl_usdt <= 0]
    longs  = tdf[tdf.side == "long"]; shorts = tdf[tdf.side == "short"]
    tpd    = r["trades"] / max(days, 1)
    avg_h  = r["avg_hold"]

    print(f"\n  ── {label} ─────────────────────────────────")
    print(f"  Trades          : {r['trades']}  ({tpd:.1f}/day)")
    print(f"  Win rate        : {r['wr']:.1f}%  "
          f"(longs {(longs.pnl_usdt>0).mean()*100:.1f}% / "
          f"shorts {(shorts.pnl_usdt>0).mean()*100:.1f}%)")
    print(f"  Actual RR       : {r['rr']:.2f}:1")
    print(f"  Avg win / loss  : ${wins.pnl_usdt.mean():.2f} / ${loss.pnl_usdt.mean():.2f}")
    print(f"  Avg hold        : {avg_h:.1f} bars")
    print(f"  Peak equity     : GBP {r['peak_gbp']:,.0f}")
    print(f"  Final equity    : GBP {r['final_gbp']:,.0f}")
    print(f"  Max drawdown    : {r['max_dd']:.1f}%")
    print(f"  Funding drag    : ${r['funding']:.2f}")
    print(f"  Liquidations    : {r['liqs']}")
    print(f"  Exit reasons:")
    for reason, cnt in tdf.reason.value_counts().items():
        print(f"    {reason:<12}: {cnt:>4}  ({cnt/r['trades']*100:.0f}%)")


# ─────────────────────────────────────────────────────────────────────────── #
#  Main                                                                        #
# ─────────────────────────────────────────────────────────────────────────── #

def main() -> None:
    START_GBP  = 500.0
    TARGET_GBP = 100_000.0
    LEV        = 10   # optimum from HAWK v5

    print(f"\n{'#'*80}")
    print("  HAWK Multi-Timeframe Backtester v1")
    print("  Strategies: ETH-1h+4hGate / ETH-30m / SOL-30m / BTC-4h")
    print(f"  Capital: GBP {START_GBP:.0f} → target GBP {TARGET_GBP:,.0f}")
    print(f"  Leverage: {LEV}x  |  Risk/trade: 1.5%  |  SL=1.5×ATR  TP=2×SL")
    print(f"{'#'*80}\n")

    results: list[dict] = []

    # ── Reference: original ETH 1h HAWK v5 ──────────────────────────── #
    print("Loading ETH 1h + 4h data ...")
    df_eth_1h = _load("ETHUSDT_1h.csv")
    df_eth_4h = _load("ETHUSDT_4h.csv")
    days_1h   = _days(df_eth_1h)

    print("Running  REF: ETH 1h HAWK v5 (baseline) ...")
    eng_ref = MultiTFHAWKEngine(
        df_entry             = df_eth_1h,
        initial_gbp          = START_GBP,
        leverage             = LEV,
        risk_pct             = 1.5,
        rr                   = 2.0,
        channel_n            = 8,
        ema_fast             = 20,
        ema_slow             = 50,
        sl_atr_mult          = 1.5,
        max_hold_bars        = 30,         # 30×1h = 30h
        cooldown_bars        = 1,
        volume_filter        = False,
        funding_bars         = 8,          # 8×1h = 8h
        entry_ema_as_filter  = True,
    )
    r_ref = eng_ref.run()
    results.append({"label": "REF: ETH 1h HAWK v5 baseline", "r": r_ref, "days": days_1h})

    # ── Strategy A: ETH 1hr with 4hr macro gate ──────────────────────── #
    # NOTE: 4h gate on ETH 1h — included to measure impact vs baseline
    print("Running  A: ETH 1h + 4h gate ...")
    eng_a = MultiTFHAWKEngine(
        df_entry             = df_eth_1h,
        df_macro             = df_eth_4h,
        initial_gbp          = START_GBP,
        leverage             = LEV,
        risk_pct             = 1.5,
        rr                   = 2.0,
        channel_n            = 8,
        ema_fast             = 20,
        ema_slow             = 50,
        sl_atr_mult          = 1.5,
        max_hold_bars        = 30,
        cooldown_bars        = 1,
        volume_filter        = False,
        funding_bars         = 8,
        entry_ema_as_filter  = True,       # keep 1h EMA filter, gate additionally by 4h
    )
    r_a = eng_a.run()
    results.append({"label": "A: ETH 1h + 4h gate", "r": r_a, "days": days_1h})

    # ── Strategy B: ETH 30m — direction from 1h EMA only ─────────────── #
    # FIX: entry_ema_as_filter=False → 30m EMA direction NOT required.
    #      Only the 1h EMA matters for trend direction.
    #      This is correct: the 30m channel breakout fires when price breaks out;
    #      the 1h EMA tells us which direction we're allowed to trade.
    print("Loading ETH 30m data ...")
    df_eth_30m = _load("ETHUSDT_30m.csv")
    df_eth_1h_r = _resample_ohlcv(df_eth_30m, "1h")   # 1h resampled from 30m
    df_eth_4h_r = _resample_ohlcv(df_eth_30m, "4h")   # 4h resampled from 30m
    days_30m_eth = _days(df_eth_30m)

    print("Running  B: ETH 30m (1h-bias, no vol) ...")
    eng_b = MultiTFHAWKEngine(
        df_entry             = df_eth_30m,
        df_bias              = df_eth_1h_r,
        initial_gbp          = START_GBP,
        leverage             = LEV,
        risk_pct             = 1.5,
        rr                   = 2.0,
        channel_n            = 8,
        ema_fast             = 20,
        ema_slow             = 50,
        sl_atr_mult          = 1.5,
        max_hold_bars        = 60,         # 60×30m = 30h
        cooldown_bars        = 1,
        volume_filter        = False,
        funding_bars         = 16,         # 16×30m = 8h
        entry_ema_as_filter  = False,      # KEY FIX: direction from 1h EMA only
    )
    r_b = eng_b.run()
    results.append({"label": "B: ETH 30m (1h-bias, no vol)", "r": r_b, "days": days_30m_eth})

    print("Running  B2: ETH 30m (1h-bias + vol 1.15x) ...")
    eng_b2 = MultiTFHAWKEngine(
        df_entry             = df_eth_30m,
        df_bias              = df_eth_1h_r,
        initial_gbp          = START_GBP,
        leverage             = LEV,
        risk_pct             = 1.5,
        rr                   = 2.0,
        channel_n            = 8,
        ema_fast             = 20,
        ema_slow             = 50,
        sl_atr_mult          = 1.5,
        max_hold_bars        = 60,
        cooldown_bars        = 1,
        volume_filter        = True,
        volume_mult          = 1.15,
        funding_bars         = 16,
        entry_ema_as_filter  = False,
    )
    r_b2 = eng_b2.run()
    results.append({"label": "B2: ETH 30m (1h + vol 1.15x)", "r": r_b2, "days": days_30m_eth})

    print("Running  B3: ETH 30m (1h+4h-bias, no vol) ...")
    eng_b3 = MultiTFHAWKEngine(
        df_entry             = df_eth_30m,
        df_bias              = df_eth_1h_r,
        df_macro             = df_eth_4h_r,
        initial_gbp          = START_GBP,
        leverage             = LEV,
        risk_pct             = 1.5,
        rr                   = 2.0,
        channel_n            = 8,
        ema_fast             = 20,
        ema_slow             = 50,
        sl_atr_mult          = 1.5,
        max_hold_bars        = 60,
        cooldown_bars        = 1,
        volume_filter        = False,
        funding_bars         = 16,
        entry_ema_as_filter  = False,
    )
    r_b3 = eng_b3.run()
    results.append({"label": "B3: ETH 30m (1h+4h-bias)", "r": r_b3, "days": days_30m_eth})

    # ── Strategy C: SOL 30m ───────────────────────────────────────────── #
    print("Loading SOL 30m data ...")
    df_sol_30m = _load("SOLUSDT_30m.csv")
    df_sol_1h  = _resample_ohlcv(df_sol_30m, "1h")
    df_sol_4h  = _resample_ohlcv(df_sol_30m, "4h")
    days_30m_sol = _days(df_sol_30m)

    print("Running  C: SOL 30m (1h-bias, no vol) ...")
    eng_c = MultiTFHAWKEngine(
        df_entry             = df_sol_30m,
        df_bias              = df_sol_1h,
        initial_gbp          = START_GBP,
        leverage             = LEV,
        risk_pct             = 1.5,
        rr                   = 2.0,
        channel_n            = 10,         # 10×30m = 5h lookback
        ema_fast             = 20,
        ema_slow             = 50,
        sl_atr_mult          = 1.2,        # SOL ATR is large; tighter SL
        max_hold_bars        = 60,
        cooldown_bars        = 1,
        volume_filter        = False,
        funding_bars         = 16,
        entry_ema_as_filter  = False,
    )
    r_c = eng_c.run()
    results.append({"label": "C: SOL 30m (1h-bias)", "r": r_c, "days": days_30m_sol})

    print("Running  C2: SOL 30m (1h-bias, vol 1.3x) ...")
    eng_c2 = MultiTFHAWKEngine(
        df_entry             = df_sol_30m,
        df_bias              = df_sol_1h,
        df_macro             = df_sol_4h,
        initial_gbp          = START_GBP,
        leverage             = LEV,
        risk_pct             = 1.5,
        rr                   = 2.0,
        channel_n            = 10,
        ema_fast             = 20,
        ema_slow             = 50,
        sl_atr_mult          = 1.2,
        max_hold_bars        = 60,
        cooldown_bars        = 1,
        volume_filter        = True,
        volume_mult          = 1.30,
        funding_bars         = 16,
        entry_ema_as_filter  = False,
    )
    r_c2 = eng_c2.run()
    results.append({"label": "C2: SOL 30m (1h+4h, vol 1.3x)", "r": r_c2, "days": days_30m_sol})

    # ── Strategy D: BTC 4h channel ────────────────────────────────────── #
    print("Loading BTC 4h data ...")
    df_btc_4h = _load("BTCUSDT_4h.csv")
    days_4h_btc = _days(df_btc_4h)

    print("Running  D: BTC 4h 12-bar (rr=2.0, hold=12) ...")
    eng_d = MultiTFHAWKEngine(
        df_entry             = df_btc_4h,
        initial_gbp          = START_GBP,
        leverage             = LEV,
        risk_pct             = 1.5,
        rr                   = 2.0,
        channel_n            = 12,         # 12×4h = 48h lookback
        ema_fast             = 20,
        ema_slow             = 50,
        sl_atr_mult          = 1.5,
        max_hold_bars        = 12,         # 12×4h = 48h (was 8→32h, too many timeouts)
        cooldown_bars        = 1,
        volume_filter        = False,
        funding_bars         = 2,          # 2×4h = 8h
        entry_ema_as_filter  = True,
    )
    r_d = eng_d.run()
    results.append({"label": "D: BTC 4h (rr=2.0, hold=12)", "r": r_d, "days": days_4h_btc})

    print("Running  D2: BTC 4h 12-bar (rr=2.0, hold=8) ...")
    eng_d2 = MultiTFHAWKEngine(
        df_entry             = df_btc_4h,
        initial_gbp          = START_GBP,
        leverage             = LEV,
        risk_pct             = 1.5,
        rr                   = 2.0,
        channel_n            = 12,
        ema_fast             = 20,
        ema_slow             = 50,
        sl_atr_mult          = 1.5,
        max_hold_bars        = 8,          # 8×4h = 32h (original)
        cooldown_bars        = 1,
        volume_filter        = False,
        funding_bars         = 2,
        entry_ema_as_filter  = True,
    )
    r_d2 = eng_d2.run()
    results.append({"label": "D2: BTC 4h (rr=2.0, hold=8)", "r": r_d2, "days": days_4h_btc})

    # BTC 30m — use 1h bias, no entry EMA (same fix as ETH 30m)
    print("Loading BTC 30m data ...")
    df_btc_30m = _load("BTCUSDT_30m.csv")
    df_btc_1h_r = _resample_ohlcv(df_btc_30m, "1h")
    days_30m_btc = _days(df_btc_30m)

    print("Running  D3: BTC 30m (1h-bias, no vol) ...")
    eng_d3 = MultiTFHAWKEngine(
        df_entry             = df_btc_30m,
        df_bias              = df_btc_1h_r,
        initial_gbp          = START_GBP,
        leverage             = LEV,
        risk_pct             = 1.5,
        rr                   = 2.0,
        channel_n            = 8,
        ema_fast             = 20,
        ema_slow             = 50,
        sl_atr_mult          = 1.5,
        max_hold_bars        = 60,         # 60×30m = 30h
        cooldown_bars        = 1,
        volume_filter        = False,
        funding_bars         = 16,
        entry_ema_as_filter  = False,
    )
    r_d3 = eng_d3.run()
    results.append({"label": "D3: BTC 30m (1h-bias)", "r": r_d3, "days": days_30m_btc})

    # ─────────────────────────────────────────────────────────────────── #
    #  Summary Table                                                       #
    # ─────────────────────────────────────────────────────────────────── #
    print(f"\n{'='*110}")
    print("  MULTI-TIMEFRAME STRATEGY COMPARISON  |  All at 10x leverage  |  GBP 500 start")
    print(f"{'='*110}")
    print(f"  {'Strategy':<38}  {'Pos':>5}  {'Return':>9}  {'Peak':>9}  {'Final':>9}  "
          f"{'MaxDD':>7}  {'Trades':>7}  {'T/Day':>6}  {'WR%':>5}  {'RR':>7}  {'Liqs':>5}")
    print("  " + "-"*107)

    best_gbp = 0
    for item in results:
        r    = item["r"]
        days = item["days"]
        tpd  = r["trades"] / max(days, 1)
        tag  = ""
        if r["final_gbp"] > best_gbp and r["trades"] > 10:
            best_gbp = r["final_gbp"]
        _print_row(item["label"], r, tpd, tag)

    # Mark the best
    print("  " + "-"*107)
    print(f"\n  * REF is the HAWK v5 baseline (ETH 1h, no 4h gate)\n")

    # ─────────────────────────────────────────────────────────────────── #
    #  Detailed breakdown for each strategy                               #
    # ─────────────────────────────────────────────────────────────────── #
    print(f"\n{'#'*80}")
    print("  DETAILED BREAKDOWN")
    print(f"{'#'*80}")
    for item in results:
        _print_detail(item["label"], item["r"], item["days"])

    # ─────────────────────────────────────────────────────────────────── #
    #  Combined portfolio estimate                                         #
    # ─────────────────────────────────────────────────────────────────── #
    print(f"\n{'#'*80}")
    print("  COMBINED PORTFOLIO — COMPOUNDING ROADMAP")
    print(f"{'#'*80}")

    TRADING_DAYS = 22  # per month

    # Monthly return estimate per strategy
    def monthly_est(r: dict, days: float) -> tuple[float, float]:
        """Return (monthly_pct, trades_per_day)."""
        if r["trades"] < 10:
            return 0.0, 0.0
        tpd    = r["trades"] / max(days, 1)
        wr_f   = r["wr"] / 100
        rr     = r["rr"]
        risk   = 1.5 / 100
        ev_pt  = wr_f * rr * risk - (1 - wr_f) * risk
        # Funding drag scales with trade count
        drag   = (r["funding"] / (days / 30)) / (START_GBP * GBP_TO_USDT) if days > 0 else 0
        mo_r   = ev_pt * (tpd * TRADING_DAYS) - drag
        return mo_r * 100, tpd

    print(f"\n  {'Strategy':<38}  {'WR%':>5}  {'RR':>5}  {'T/Day':>6}  "
          f"{'Monthly%':>9}  {'GBP→1k':>8}  {'GBP→10k':>9}  {'GBP→100k':>10}")
    print("  " + "-"*100)

    total_tpd   = 0.0
    total_mo_r  = 0.0
    active_strategies = 0

    for item in results:
        r    = item["r"]
        days = item["days"]
        if r["trades"] < 10:
            continue
        mo_pct, tpd = monthly_est(r, days)
        total_tpd  += tpd
        total_mo_r += mo_pct / 100

        def _compound(monthly_r: float) -> tuple:
            eq_c = START_GBP; m = 0
            m_1k = m_10k = m_100k = None
            while eq_c < TARGET_GBP and m < 600:
                eq_c *= (1 + monthly_r); m += 1
                if m_1k   is None and eq_c >= 1_000:   m_1k   = m
                if m_10k  is None and eq_c >= 10_000:  m_10k  = m
                if m_100k is None and eq_c >= 100_000: m_100k = m
            return m_1k, m_10k, m_100k

        def _fmt(months):
            if months is None: return "   Never"
            y = months // 12; mo = months % 12
            return f"{y}y{mo:02d}m"

        m1, m10, m100 = _compound(mo_pct / 100)
        print(f"  {item['label']:<38}  {r['wr']:>5.1f}%  {r['rr']:>5.2f}  {tpd:>6.1f}  "
              f"{mo_pct:>+8.2f}%  {_fmt(m1):>8}  {_fmt(m10):>9}  {_fmt(m100):>10}")
        active_strategies += 1

    # Combined roadmap (per-strategy returns run in parallel, equity shared equally)
    print("  " + "-"*100)

    # For combined: each strategy gets 1/N of starting equity but compounds separately
    # Simplified: add monthly returns (each on its own compounding equity pool)
    # Show combined by summing EV across independent equity pools
    # Then show "if you split GBP 500 equally" scenario

    # More practical: split capital, sum equity curves
    # Combine only the primary strategy per asset (exclude variants and REF)
    PRIMARY_LABELS = {"B", "C", "D"}
    strategies_to_combine = [
        item for item in results
        if item["r"]["trades"] >= 50
        and any(item["label"].startswith(lbl + ":") for lbl in PRIMARY_LABELS)
        and monthly_est(item["r"], item["days"])[0] > 0
    ]
    if not strategies_to_combine:
        # Fallback: any profitable non-REF strategy with enough trades
        strategies_to_combine = [
            item for item in results
            if item["r"]["trades"] >= 50
            and "REF" not in item["label"]
            and monthly_est(item["r"], item["days"])[0] > 0
        ]
    n_strats = len(strategies_to_combine)
    if n_strats > 0:
        split_gbp = START_GBP / n_strats  # equal capital split
        combined_mo_r = sum(monthly_est(s["r"], s["days"])[0] / 100
                            for s in strategies_to_combine) / n_strats

        # Actually, for compound growth with equal splits:
        # Each pool grows at its own rate. Total = sum of pools.
        # But for simplicity, use average monthly rate × n pools / total capital
        # = same average rate applied to full capital (if all pools compound independently)
        print(f"\n  Combined portfolio ({n_strats} strategies, GBP {START_GBP:.0f} split {split_gbp:.0f}/each):")
        print(f"  Average monthly return across strategies: {combined_mo_r*100:+.2f}%")

        m1, m10, m100 = _compound(combined_mo_r)
        print(f"  Combined roadmap:")
        print(f"    GBP 500  →  GBP 1k  :  {_fmt(m1)}")
        print(f"    GBP 1k   →  GBP 10k :  {_fmt(m10)}")
        print(f"    GBP 10k  →  GBP 100k:  {_fmt(m100)}")

        # Stacked portfolio: all strategies share the SAME equity (sequential)
        # At scale, use full equity across all — more realistic
        stacked_mo_r = sum(monthly_est(s["r"], s["days"])[0] / 100
                           for s in strategies_to_combine)
        stacked_tpd  = sum(monthly_est(s["r"], s["days"])[1]
                           for s in strategies_to_combine)
        print(f"\n  Full equity across ALL strategies (compounding, shared equity pool):")
        print(f"  Combined monthly return: {stacked_mo_r*100:+.2f}%  |  Total trades/day: {stacked_tpd:.1f}")

        m1s, m10s, m100s = _compound(stacked_mo_r)
        print(f"  Roadmap:")
        print(f"    GBP 500  →  GBP 1k  :  {_fmt(m1s)}")
        print(f"    GBP 1k   →  GBP 10k :  {_fmt(m10s)}")
        print(f"    GBP 10k  →  GBP 100k:  {_fmt(m100s)}")

    # Reference comparison
    r_ref_result = next(i["r"] for i in results if "REF" in i["label"])
    ref_days     = next(i["days"] for i in results if "REF" in i["label"])
    ref_mo, ref_tpd = monthly_est(r_ref_result, ref_days)
    m1r, m10r, m100r = _compound(ref_mo / 100)

    print(f"\n  ── Baseline (ETH 1h alone, no changes) ──")
    print(f"  Monthly: {ref_mo:+.2f}%  |  T/Day: {ref_tpd:.1f}")
    print(f"    GBP 500→1k: {_fmt(m1r)}  |  GBP 1k→10k: {_fmt(m10r)}  |  GBP 10k→100k: {_fmt(m100r)}")

    print(f"""
{'#'*80}
  IMPLEMENTATION NOTE
{'#'*80}
  Verified strategies are candidates for hawk_paper_trader.py extension.
  Rule: only add a strategy to live paper trading after it shows:
    - Positive EV (WR × RR > 1 - WR)
    - At least 50 backtested trades
    - Profit factor > 1.2
  Any strategy below these thresholds needs parameter tuning first.
{'#'*80}
""")


if __name__ == "__main__":
    main()
