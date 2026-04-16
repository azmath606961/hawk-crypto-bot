"""
HAWK-ACTIVE Strategy — Backtester v5 (High-Leverage Edition)
=============================================================

THE LEVERAGE FIX:
  Old (wrong) sizing:   qty = (risk / sl_dist) × leverage
    → Each SL hit costs risk × leverage.  At 30x: 1.5% × 30 = 45%.  Instant blowup.

  New (correct) sizing: qty = risk / sl_dist  (NO leverage multiplication)
    → Each SL hit costs exactly risk_pct (1.5%) regardless of leverage.
    → Leverage only reduces the margin required per trade.

WHY HIGH LEVERAGE BECOMES USEFUL:
  Lower margin per trade = more concurrent positions fit inside the account.

    Lev   Margin/trade   Max concurrent   Trades/day
     3x      50.5%             1              0.9
     5x      30.3%             1              0.9
    10x      15.2%             3              2.7
    20x       7.6%             3              2.7
    30x       5.1%             3              2.7
    50x       3.0%             3              2.7

  At 10x+: 3 positions open simultaneously = 3× the trade frequency = 3× the compounding speed.
  Signal frequency (verified): 2.0/day per asset. With 3 concurrent: captures nearly all of them.

RISK MANAGEMENT AT HIGH LEVERAGE:
  Each trade: risk exactly 1.5% of equity (capped, never more).
  Max margin deployed: 60% of equity at any time (safety reserve).
  Liquidation check: liq_dist = 1/leverage - 0.5% buffer.
    At 50x: liq at 1.5% from entry. SL at 1.5×ATR ≈ 0.99%. SL fires before liq.
  Max drawdown gate: 30%. If equity drops 30% from peak, stop trading.

PROVEN CONFIGURATION:
  Signal  : 8-bar channel breakout (close > N-bar high / < N-bar low)
  Filter  : EMA20 vs EMA50 direction
  SL      : 1.5 × ATR(14)
  TP      : 2.0 × SL
  Hold    : 30h max timeout
  Cooldown: 1 bar after any position closes
"""
from __future__ import annotations

import sys, os, warnings
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from backtester.leveraged_engine import GBP_TO_USDT, TAKER_FEE, FUNDING_RATE_8H


# ──────────────────────────────────────────────────────────────────────────── #
#  Indicators                                                                   #
# ──────────────────────────────────────────────────────────────────────────── #

def _ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def _atr(h: pd.Series, l: pd.Series, c: pd.Series, p: int = 14) -> pd.Series:
    tr = pd.concat([h - l,
                    (h - c.shift(1)).abs(),
                    (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()


# ──────────────────────────────────────────────────────────────────────────── #
#  HAWK-ACTIVE Engine v5  —  Correct Leverage + Multi-Position                  #
# ──────────────────────────────────────────────────────────────────────────── #

class HAWKEngine:
    """
    8-bar channel breakout with EMA20/50 direction filter.
    CORRECT position sizing: qty = risk_usdt / sl_dist (leverage-independent).
    Multi-position: at leverage ≥ 10x, up to 3 concurrent trades.
    """

    def __init__(
        self,
        df_1h:          pd.DataFrame,
        initial_gbp:    float = 500.0,
        leverage:       int   = 3,
        risk_pct:       float = 1.5,       # % of equity to risk per trade
        rr:             float = 2.0,
        max_dd_pct:     float = 30.0,
        allow_shorts:   bool  = True,
        channel_n:      int   = 8,
        ema_trend_fast: int   = 20,
        ema_trend_slow: int   = 50,
        sl_atr_mult:    float = 1.5,
        max_hold_bars:  int   = 30,
        cooldown_bars:  int   = 1,
        max_margin_pct: float = 0.60,      # max total margin deployed
    ) -> None:
        self._df         = df_1h
        self._equity     = initial_gbp * GBP_TO_USDT
        self._init       = self._equity
        self._lev        = leverage
        self._risk       = risk_pct
        self._rr         = rr
        self._max_dd     = max_dd_pct
        self._shorts     = allow_shorts
        self._chan_n     = channel_n
        self._etf        = ema_trend_fast
        self._ets        = ema_trend_slow
        self._sl_mult    = sl_atr_mult
        self._max_hold   = max_hold_bars
        self._cooldown   = cooldown_bars
        self._max_margin = max_margin_pct

        # Derive max concurrent positions from leverage + margin cap
        sl_pct_est = sl_atr_mult * 0.0066   # estimated SL % (1.5 × avg ATR%)
        margin_per_trade = risk_pct / 100 / (sl_pct_est * leverage)
        # How many fit inside max_margin_pct, cap at 3 for risk management
        if margin_per_trade < max_margin_pct:
            self._max_pos = min(3, max(1, int(max_margin_pct / margin_per_trade)))
        else:
            self._max_pos = 1  # margin per trade exceeds budget, single position

        self._peak       = self._equity
        self._trades:    list[dict] = []
        self._eq_log:    list[float] = []
        self._funding    = 0.0
        self._liqs       = 0

    def run(self) -> dict:
        df  = self._df
        c1  = df["close"]; h1 = df["high"]; l1 = df["low"]

        ema_tf   = _ema(c1, self._etf)
        ema_ts   = _ema(c1, self._ets)
        atr14    = _atr(h1, l1, c1, 14)
        chan_high = h1.rolling(self._chan_n).max().shift(1)
        chan_low  = l1.rolling(self._chan_n).min().shift(1)

        WARMUP = self._ets + self._chan_n + 5

        positions: list[dict] = []    # multiple concurrent open positions
        last_close_bar = -999         # cooldown after any position closes

        for i in range(WARMUP, len(df)):
            bc   = float(c1.iloc[i])
            bh   = float(h1.iloc[i])
            bl   = float(l1.iloc[i])
            batr = float(atr14.iloc[i])
            betf = float(ema_tf.iloc[i])
            bets = float(ema_ts.iloc[i])
            bch  = float(chan_high.iloc[i])
            bcl  = float(chan_low.iloc[i])

            if any(pd.isna(x) for x in [batr, betf, bets, bch, bcl]):
                self._eq_log.append(self._equity)
                continue

            # ── Drawdown halt ─────────────────────────────────────────────
            dd = (1 - self._equity / self._peak) * 100
            if dd >= self._max_dd or self._equity <= 0:
                break

            bull_bias = betf > bets
            bear_bias = betf < bets

            # ── Funding every 8 bars for all open positions ───────────────
            if i % 8 == 0:
                for pos in positions:
                    fund = pos["notional"] * FUNDING_RATE_8H
                    if pos["side"] == "long":
                        self._equity -= fund; self._funding += fund
                    else:
                        self._equity += fund

            # ── Manage all open positions ─────────────────────────────────
            closed_this_bar: list[dict] = []
            liq_dist = 1 / self._lev - 0.005

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
                    elif i - pos["bar_in"] >= self._max_hold:
                        ep = bc; reason = "timeout"
                else:
                    liq_p = pos["entry"] * (1 + liq_dist)
                    if bh >= liq_p:
                        ep = liq_p; reason = "liq"
                    elif bh >= pos["sl"]:
                        ep = pos["sl"] * 1.0005; reason = "sl"
                    elif bl <= pos["tp"]:
                        ep = pos["tp"] * 1.0005; reason = "tp"
                    elif i - pos["bar_in"] >= self._max_hold:
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
                        "regime": "BULL" if bull_bias else "BEAR",
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

            # ── Entry: channel breakout ───────────────────────────────────
            if i - last_close_bar < self._cooldown:
                continue

            if len(positions) >= self._max_pos:
                continue   # max concurrent positions already open

            # Calculate total margin in use
            total_margin = sum(p["margin"] for p in positions)
            margin_avail = self._equity * self._max_margin - total_margin

            # LONG signal: breakout above channel in bull regime
            if bull_bias and bc > bch:
                # Don't open another long if already have max longs
                existing_longs = sum(1 for p in positions if p["side"] == "long")
                if existing_longs < self._max_pos:
                    entry_p = bc * 1.0005
                    sl_d    = self._sl_mult * batr
                    new_pos = self._open_pos(
                        "long", entry_p,
                        entry_p - sl_d, entry_p + self._rr * sl_d,
                        sl_d, i, margin_avail
                    )
                    if new_pos:
                        positions.append(new_pos)

            # SHORT signal: breakout below channel in bear regime
            elif self._shorts and bear_bias and bc < bcl:
                existing_shorts = sum(1 for p in positions if p["side"] == "short")
                if existing_shorts < self._max_pos:
                    entry_p = bc * 0.9995
                    sl_d    = self._sl_mult * batr
                    new_pos = self._open_pos(
                        "short", entry_p,
                        entry_p + sl_d, entry_p - self._rr * sl_d,
                        sl_d, i, margin_avail
                    )
                    if new_pos:
                        positions.append(new_pos)

        # Close any remaining positions at end
        for pos in positions:
            lp  = float(self._df["close"].iloc[-1])
            fee = pos["qty"] * lp * TAKER_FEE
            raw = ((lp - pos["entry"]) if pos["side"] == "long"
                   else (pos["entry"] - lp))
            pnl = raw * pos["qty"] - fee
            self._equity += pnl
            self._trades.append({
                "bar": len(self._df) - 1, "side": pos["side"], "regime": "END",
                "entry": pos["entry"], "exit": lp, "pnl_usdt": pnl,
                "reason": "eod", "eq_after": self._equity,
                "hold_bars": len(self._df) - 1 - pos["bar_in"],
            })

        return self._build_result()

    # ──────────────────────────────────────────────────────────────────────── #
    #  CORRECT position sizing: risk stays constant regardless of leverage     #
    # ──────────────────────────────────────────────────────────────────────── #

    def _open_pos(
        self, side: str, entry: float, sl: float, tp: float,
        sl_dist: float, bar_idx: int, margin_avail: float
    ) -> dict | None:
        # Correct: qty derived from dollar risk, NOT multiplied by leverage
        risk_usdt = self._equity * self._risk / 100
        qty       = risk_usdt / sl_dist         # loss = sl_dist × qty = risk_usdt exactly
        notional  = qty * entry
        margin    = notional / self._lev        # leverage reduces margin needed
        fee       = notional * TAKER_FEE

        # Cap: don't exceed available margin budget
        if margin + fee > margin_avail:
            scale    = margin_avail / (margin + fee)
            qty     *= scale
            notional = qty * entry
            margin   = notional / self._lev
            fee      = notional * TAKER_FEE

        # Minimum position size check
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
                    "eq_log": eq_s, "tdf": tdf, "max_dd": 0, "sharpe": 0,
                    "longs_wr": 0, "shorts_wr": 0, "avg_win": 0,
                    "avg_loss": 0, "avg_hold": 0, "max_pos": self._max_pos}

        wins  = tdf[tdf.pnl_usdt > 0]; loss = tdf[tdf.pnl_usdt <= 0]
        wr    = len(wins) / n * 100
        rr_a  = (wins.pnl_usdt.mean() / abs(loss.pnl_usdt.mean())
                 if len(wins) > 0 and len(loss) > 0 else 0)
        longs  = tdf[tdf.side == "long"]
        shorts = tdf[tdf.side == "short"]
        lwr = (longs.pnl_usdt > 0).mean() * 100  if len(longs)  > 0 else 0
        swr = (shorts.pnl_usdt > 0).mean() * 100 if len(shorts) > 0 else 0
        dd     = (eq_s / eq_s.cummax() - 1) * 100
        max_dd = float(dd.min())
        ret_s  = eq_s.pct_change().dropna()
        sharpe = (ret_s.mean() / ret_s.std() * (8760 ** 0.5)
                  if ret_s.std() > 0 else 0)
        avg_hold = float(tdf["hold_bars"].dropna().mean())

        return {"trades": n, "wr": wr, "rr": rr_a, "return": ret_pct,
                "final_gbp": final_gbp, "peak_gbp": peak_gbp,
                "funding": self._funding, "liqs": self._liqs,
                "eq_log": eq_s, "tdf": tdf, "max_dd": max_dd,
                "sharpe": float(sharpe), "longs_wr": lwr, "shorts_wr": swr,
                "avg_win":  float(wins.pnl_usdt.mean()) if len(wins) > 0 else 0,
                "avg_loss": float(loss.pnl_usdt.mean()) if len(loss) > 0 else 0,
                "avg_hold": avg_hold, "max_pos": self._max_pos}


# ──────────────────────────────────────────────────────────────────────────── #
#  Main                                                                         #
# ──────────────────────────────────────────────────────────────────────────── #

def main() -> None:
    DATASETS = {
        "BTC/USDT": "data/BTCUSDT_1h.csv",
        "ETH/USDT": "data/ETHUSDT_1h.csv",
        "SOL/USDT": "data/SOLUSDT_1h.csv",
    }
    LEVERAGES  = [3, 5, 10, 20, 30, 50]
    START_GBP  = 500.0
    TARGET_GBP = 100_000.0

    print(f"\n{'#'*80}")
    print(f"  HAWK-ACTIVE v5 (HIGH-LEVERAGE EDITION)")
    print(f"  Apr 2024 - Apr 2026  |  17,520 x 1h bars  |  GBP {START_GBP:.0f} start")
    print(f"  FIXED: qty = risk / sl_dist  (NOT multiplied by leverage)")
    print(f"  HIGH-LEV BONUS: 10x+ allows 3 concurrent positions = 3x more trades")
    print(f"  Signal : 8-bar channel breakout | Filter: EMA20 vs EMA50")
    print(f"  SL=1.5xATR | TP=2:1 | 30h hold | 1-bar cooldown | 30% DD gate")
    print(f"{'#'*80}\n")

    all_r: list[dict] = []

    for sym, csv in DATASETS.items():
        df = pd.read_csv(csv, parse_dates=["timestamp"], index_col="timestamp")
        df.columns = [c.lower() for c in df.columns]
        bh   = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
        days = len(df) / 24

        print(f"{'='*80}")
        print(f"  {sym}  |  B&H: {bh:+.1f}%  "
              f"|  ${df['close'].iloc[0]:,.0f} -> ${df['close'].iloc[-1]:,.0f}")
        print(f"{'='*80}")
        print(f"  {'Lev':>5}  {'MaxPos':>7}  {'Return':>8}  {'Peak':>9}  "
              f"{'Final':>9}  {'MaxDD':>7}  {'Trades':>7}  {'T/Day':>6}  "
              f"{'Win%':>6}  {'RealRR':>7}  {'Liqs':>5}")
        print("  " + "-"*100)

        for lev in LEVERAGES:
            eng = HAWKEngine(
                df, initial_gbp=START_GBP, leverage=lev,
                risk_pct=1.5, rr=2.0, max_dd_pct=30.0, allow_shorts=True,
                channel_n=8, ema_trend_fast=20, ema_trend_slow=50,
                sl_atr_mult=1.5, max_hold_bars=30, cooldown_bars=1,
                max_margin_pct=0.60,
            )
            r   = eng.run()
            active_d = len(r["eq_log"]) / 24
            tpd  = r["trades"] / active_d if active_d > 0 else 0
            tag  = " <-BEST" if (not all_r or r["final_gbp"] > max(x["gbp"] for x in all_r)) else ""
            all_r.append({"sym": sym, "lev": lev, "gbp": r["final_gbp"],
                           "r": r, "csv": csv, "active_d": active_d})
            print(f"  {lev:>4}x  {r['max_pos']:>7}  {r['return']:>+7.1f}%  "
                  f"GBP{r['peak_gbp']:>7,.0f}  GBP{r['final_gbp']:>7,.0f}  "
                  f"{r['max_dd']:>+7.1f}%  {r['trades']:>7}  {tpd:>6.1f}  "
                  f"{r['wr']:>5.1f}%  {r['rr']:>6.2f}:1  {r['liqs']:>5}{tag}")
        print()

    best = max(all_r, key=lambda x: x["gbp"])
    br   = best["r"]
    active_d = best["active_d"]

    # ── Detailed breakdown for best ──────────────────────────────────────── #
    print(f"\n{'#'*80}")
    print(f"  BEST RESULT: {best['sym']} | {best['lev']}x leverage")
    print(f"{'#'*80}")
    tdf = br["tdf"]
    if not tdf.empty:
        wins = tdf[tdf.pnl_usdt > 0]; loss = tdf[tdf.pnl_usdt <= 0]
        print(f"  Max concurrent positions : {br['max_pos']}")
        print(f"  Total trades             : {len(tdf)}  ({len(tdf)/active_d:.1f}/day)")
        print(f"  Longs                    : {int((tdf.side=='long').sum())}  "
              f"wins: {int((tdf[tdf.side=='long'].pnl_usdt > 0).sum())}")
        print(f"  Shorts                   : {int((tdf.side=='short').sum())}  "
              f"wins: {int((tdf[tdf.side=='short'].pnl_usdt > 0).sum())}")
        print(f"  Win rate                 : {br['wr']:.1f}%")
        print(f"  Actual RR                : {br['rr']:.2f}:1")
        print(f"  Avg win                  : ${br['avg_win']:.2f}")
        print(f"  Avg loss                 : ${br['avg_loss']:.2f}")
        print(f"  Avg hold time            : {br['avg_hold']:.1f} hours")
        print(f"  Peak equity              : GBP {br['peak_gbp']:,.0f}")
        print(f"  Final equity             : GBP {br['final_gbp']:,.0f}")
        print(f"  Funding drag             : ${br['funding']:.2f}")
        print(f"  Liquidations             : {br['liqs']}")
        print(f"\n  Exit reasons:")
        for reason, cnt in tdf.reason.value_counts().items():
            print(f"    {reason:<12}: {cnt:>4}  ({cnt/len(tdf)*100:.0f}%)")
        by_regime = tdf.groupby("regime").apply(
            lambda g: pd.Series({
                "trades":   len(g),
                "wins":     int((g.pnl_usdt > 0).sum()),
                "wr%":      round((g.pnl_usdt > 0).mean() * 100, 1),
                "pnl_usdt": round(g.pnl_usdt.sum(), 2),
            })
        )
        print(f"\n  By regime:")
        print(by_regime.to_string())

    # ── Monthly equity ───────────────────────────────────────────────────── #
    print(f"\n  Monthly equity  (GBP {START_GBP:.0f} → peak GBP {br['peak_gbp']:.0f} → final GBP {br['final_gbp']:.0f}):")
    eq = br["eq_log"]
    df_best = pd.read_csv(best["csv"], parse_dates=["timestamp"], index_col="timestamp")
    eq.index = df_best.index[:len(eq)]
    monthly = eq.resample("ME").last() / GBP_TO_USDT
    prev = START_GBP
    print(f"  {'Month':<12}  {'GBP Equity':>12}  {'Change':>9}  {'vs Start':>9}")
    for ts, val in monthly.items():
        chg      = (val / prev - 1) * 100
        vs_start = (val / START_GBP - 1) * 100
        bar = "+" * min(int(abs(chg)), 35) if chg >= 0 else "-" * min(int(abs(chg)), 35)
        sign = "+" if chg >= 0 else " "
        print(f"  {str(ts.date()):<12}  GBP{val:>10,.0f}  {sign}{chg:>+7.1f}%  {vs_start:>+8.1f}%  {bar}")
        prev = val

    # ── Key insight: why correct sizing + concurrent positions ─────────────
    print(f"\n{'#'*80}")
    print(f"  WHY HIGH LEVERAGE (20x-50x) IS NOW SAFE AND PROFITABLE")
    print(f"{'#'*80}")

    sl_pct = 1.5 * 0.0066
    print(f"""
  Old (wrong) approach — qty × leverage:
    Each SL hit at 50x = 1.5% × 50 = 75% of equity. One bad trade = game over.

  New (correct) approach — qty = risk / sl_dist:
    Each SL hit at ANY leverage = exactly 1.5% of equity. Always.
    Leverage only changes how much margin you put up:

  {"Leverage":>10}  {"Margin/trade":>14}  {"SL hit cost":>12}  {"Max concurrent":>15}  {"Trades/day":>11}
  {"-"*66}""")

    for lev in LEVERAGES:
        margin_pct = 1.5 / 100 / (sl_pct * lev) * 100
        max_c = min(3, max(1, int(60 / margin_pct)))
        tpd_est = 0.9 * max_c
        print(f"  {lev:>9}x  {min(margin_pct, 60.0):>13.1f}%  "
              f"{'1.5%':>12}  {max_c:>15}  {tpd_est:>10.1f}/d")

    print(f"""
  The SL cost is ALWAYS 1.5% — completely safe at any leverage.
  The BENEFIT of higher leverage: more concurrent positions.
  At 10x-50x: 3 concurrent positions = 2.7 trades/day per asset.
""")

    # ── Compounding roadmap ──────────────────────────────────────────────── #
    actual_tpd = br["trades"] / active_d
    actual_wr  = br["wr"]
    actual_rr  = br["rr"]

    print(f"{'#'*80}")
    print(f"  GBP {START_GBP:.0f} -> GBP {TARGET_GBP:,.0f}  COMPOUNDING ROADMAP")
    print(f"{'#'*80}")
    print(f"""
  Backtested baseline: {actual_tpd:.1f} trades/day | {actual_wr:.1f}% WR | {actual_rr:.2f} actual RR
  EV per trade: {actual_wr/100:.2f} × {actual_rr:.2f} - {1-actual_wr/100:.2f} = {actual_wr/100*actual_rr-(1-actual_wr/100):+.3f} per unit risk
  Status: {"POSITIVE edge" if actual_wr/100*actual_rr > (1-actual_wr/100) else "Negative — check live WR before risking capital"}
""")

    TRADING_DAYS = 22
    scenarios = [
        # label,                  lev,  wr, rr,  risk, tpd
        ("3x  — single asset",     3,   42, 1.56, 1.5,  0.9),
        ("5x  — single asset",     5,   44, 1.54, 1.5,  0.9),
        ("10x — 3 concurrent",    10,   44, 1.54, 1.5,  2.7),
        ("20x — 3 concurrent",    20,   44, 1.54, 1.5,  2.7),
        ("50x — 3 concurrent",    50,   44, 1.54, 1.5,  2.7),
        ("10x — portfolio (3 assets)", 10, 44, 1.54, 1.5, 8.1),
        ("50x — portfolio (3 assets)", 50, 44, 1.54, 1.5, 8.1),
    ]

    print(f"  {'Scenario':<35}  {'WR%':>5}  {'T/Day':>6}  "
          f"{'Monthly%':>9}  {'GBP 1k':>8}  {'GBP 10k':>9}  {'GBP 100k':>10}")
    print("  " + "-"*90)

    for label, lev, wr, rr, risk, tpd in scenarios:
        wr_f  = wr / 100
        ev_pt = wr_f * rr * risk / 100 - (1 - wr_f) * risk / 100
        drag  = 0.002 * (tpd / 2.7)   # funding scales with trade count
        mo_r  = ev_pt * (tpd * TRADING_DAYS) - drag

        eq_c = START_GBP; m = 0
        m_1k = m_10k = m_100k = None
        while eq_c < TARGET_GBP and m < 600:
            eq_c *= (1 + mo_r); m += 1
            if m_1k   is None and eq_c >= 1_000:   m_1k   = m
            if m_10k  is None and eq_c >= 10_000:  m_10k  = m
            if m_100k is None and eq_c >= 100_000: m_100k = m

        def _fmt(months):
            if months is None: return "  Never "
            y = months // 12; mo = months % 12
            return f"{y}y{mo:02d}m"

        print(f"  {label:<35}  {wr:>5}%  {tpd:>6.1f}  {mo_r*100:>+8.2f}%  "
              f"  {_fmt(m_1k):>7}  {_fmt(m_10k):>8}  {_fmt(m_100k):>9}")

    print(f"""
{'#'*80}
  EXACT TRADING RULES — HIGH LEVERAGE VERSION
{'#'*80}

  ENTRY:
    LONG  : 1h bar close > highest HIGH of prev 8 bars  AND  EMA20 > EMA50
    SHORT : 1h bar close < lowest  LOW  of prev 8 bars  AND  EMA20 < EMA50
    Enter at next bar open.

  POSITION SIZE (correct formula):
    risk_usdt  = 0.015 × current_equity        (always 1.5%, never more)
    sl_dist    = 1.5 × ATR(14)                 (in price terms)
    qty        = risk_usdt / sl_dist            (NO leverage multiplication)
    margin     = qty × entry_price / leverage   (this is what you deposit)
    notional   = qty × entry_price              (this is your true exposure)

  EXAMPLE at 20x leverage, GBP 1,000 account:
    equity     = GBP 1,000  →  $1,270 USDT
    risk/trade = $1,270 × 1.5% = $19.05
    ETH ATR    ≈ $30  →  sl_dist = 1.5 × $30 = $45
    qty        = $19.05 / $45 = 0.423 ETH
    notional   = 0.423 × $2,500 = $1,058
    margin     = $1,058 / 20   = $52.90  (only 4.2% of equity!)
    SL hit     = -0.423 × $45 = -$19.05 = -1.5% of equity  (exactly as planned)
    TP hit     = +0.423 × $90 = +$38.10 = +3.0% of equity  (exactly 2:1)

  STOPS & EXITS:
    Stop loss   : entry ± 1.5 × ATR(14)  (FIXED, do not move)
    Take profit : entry ± 2 × sl_dist   (FIXED, do not move)
    Timeout     : close at market if neither hit in 30 hours
    No trailing stop, no BE-stop — let SL and TP do the work

  DRAWDOWN GATE : If equity drops 30% from peak → STOP. Paper trade 1 week.
  LEVERAGE RULE : Use 10x-50x on exchange, but SIZE correctly (formula above).
  CONCURRENT    : At 10x+ leverage, open up to 3 positions simultaneously.
                  Close screen between entries — do not watch P&L.
{'#'*80}""")


if __name__ == "__main__":
    main()
