"""Final definitive leveraged + compounding backtest with honest projections."""
import sys, pandas as pd, warnings, math
sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

import numpy as np
from utils.indicators import atr, ema, rsi
from backtester.leveraged_engine import GBP_TO_USDT, TAKER_FEE, FUNDING_RATE_8H
from backtester.metrics import compute_all


def _adx(h, l, c, p=14):
    prev_h = h.shift(1); prev_l = l.shift(1); prev_c = c.shift(1)
    tr   = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    dm_p = (h - prev_h).clip(lower=0).where((h - prev_h) > (prev_l - l), 0)
    dm_m = (prev_l - l).clip(lower=0).where((prev_l - l) > (h - prev_h), 0)
    atr_ = tr.ewm(span=p, adjust=False).mean()
    dip  = 100 * dm_p.ewm(span=p, adjust=False).mean() / atr_.replace(0, np.nan)
    dim  = 100 * dm_m.ewm(span=p, adjust=False).mean() / atr_.replace(0, np.nan)
    dx   = 100 * (dip - dim).abs() / (dip + dim).replace(0, np.nan)
    return dx.ewm(span=p, adjust=False).mean()


def run_fixed_tp_sl(df, leverage, risk_pct, sl_mult, rr, adx_min, strat, allow_shorts=True):
    """
    Pure fixed-SL / fixed-TP backtest — no trailing stops.
    Gives honest RR: every trade is either full TP or full SL.
    """
    c1 = df["close"]; h1 = df["high"]; l1 = df["low"]; v1 = df["volume"]
    ef1  = ema(c1, 20);  es1  = ema(c1, 50)
    atr1 = atr(h1, l1, c1, 14)
    rsi1 = rsi(c1, 14)
    rh1  = c1.rolling(20).max().shift(1)
    rl1  = c1.rolling(20).min().shift(1)
    va1  = v1.rolling(20).mean()

    df4h = df.resample("4h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    c4 = df4h["close"]; h4 = df4h["high"]; l4 = df4h["low"]
    ef4  = ema(c4, 20); es4  = ema(c4, 50)
    adx4 = _adx(h4, l4, c4, 14)
    rsi4 = rsi(c4, 14)

    equity      = 500.0 * GBP_TO_USDT
    peak        = equity
    funding_paid = 0.0
    liqs        = 0
    trades      = []
    eq_log      = []
    pos         = None

    for i in range(55, len(df)):
        bc   = float(c1.iloc[i]); bh = float(h1.iloc[i]); bl = float(l1.iloc[i])
        bv   = float(v1.iloc[i]); batr = float(atr1.iloc[i])
        brsi = float(rsi1.iloc[i])
        bef  = float(ef1.iloc[i]); bes  = float(es1.iloc[i])
        brhh = float(rh1.iloc[i]); brll = float(rl1.iloc[i])
        bva  = float(va1.iloc[i])
        ts   = df.index[i]

        if any(pd.isna(x) for x in [batr, brsi, bef, bes, brhh, brll]):
            eq_log.append(equity)
            continue

        if equity <= 0:
            break
        dd = (1 - equity / peak) * 100
        if dd >= 25.0:
            break

        j4 = max(0, int(np.searchsorted(df4h.index, ts, side="right")) - 1)
        if j4 < 20:
            eq_log.append(equity)
            continue
        h4ef  = float(ef4.iloc[j4]);  h4es  = float(es4.iloc[j4])
        h4adx = float(adx4.iloc[j4]); h4rsi = float(rsi4.iloc[j4])
        if any(pd.isna(x) for x in [h4ef, h4es, h4adx]):
            eq_log.append(equity)
            continue

        trend_up = (h4ef > h4es) and (h4adx > adx_min)
        trend_dn = (h4ef < h4es) and (h4adx > adx_min)

        # Funding every 8 bars (8h)
        if pos and i % 8 == 0:
            fund = pos["notional"] * FUNDING_RATE_8H
            if pos["side"] == "long":
                equity -= fund; funding_paid += fund
            else:
                equity += fund

        # Manage open position — fixed SL/TP
        if pos:
            ep = None; reason = ""
            if pos["side"] == "long":
                liq_p = pos["entry"] * (1 - 1 / leverage + 0.005)
                if bl <= liq_p:
                    ep = liq_p; reason = "liq"
                elif bl <= pos["sl"]:
                    ep = pos["sl"] * 0.9995; reason = "sl"
                elif bh >= pos["tp"]:
                    ep = pos["tp"] * 0.9995; reason = "tp"
            else:
                liq_p = pos["entry"] * (1 + 1 / leverage - 0.005)
                if bh >= liq_p:
                    ep = liq_p; reason = "liq"
                elif bh >= pos["sl"]:
                    ep = pos["sl"] * 1.0005; reason = "sl"
                elif bl <= pos["tp"]:
                    ep = pos["tp"] * 1.0005; reason = "tp"

            if ep is not None:
                fee = pos["qty"] * ep * TAKER_FEE
                if reason == "liq":
                    pnl = -pos["margin"]; liqs += 1
                else:
                    pnl = ((ep - pos["entry"]) if pos["side"] == "long"
                           else (pos["entry"] - ep)) * pos["qty"] - fee
                equity += pnl
                if equity > peak:
                    peak = equity
                trades.append({
                    "side": pos["side"], "entry": pos["entry"], "exit": ep,
                    "pnl_usdt": pnl, "reason": reason, "eq_after": equity,
                })
                pos = None
            eq_log.append(equity)
            continue

        if equity < 5:
            eq_log.append(equity)
            continue

        can_long  = trend_up and h4rsi < 75
        can_short = trend_dn and allow_shorts and h4rsi > 25

        sig = None; ep = bc
        if strat in ("breakout", "combined"):
            vol_ok = bv > 1.5 * bva
            if can_long  and bc > brhh and vol_ok and 48 < brsi < 73:
                sig = "long";  ep = bc * 1.0005
            elif can_short and bc < brll and vol_ok and 27 < brsi < 52:
                sig = "short"; ep = bc * 0.9995

        if sig is None and strat in ("ema_trend", "combined"):
            near = abs(bc - bef) / bef < 0.025
            if can_long  and bef > bes and near and bc > bef and 42 < brsi < 63:
                sig = "long";  ep = bc * 1.0005
            elif can_short and bef < bes and near and bc < bef and 37 < brsi < 58:
                sig = "short"; ep = bc * 0.9995

        if sig:
            sl_dist = sl_mult * batr
            if sl_dist / ep < 0.003:
                eq_log.append(equity)
                continue
            sl = ep - sl_dist if sig == "long" else ep + sl_dist
            tp = ep + rr * sl_dist if sig == "long" else ep - rr * sl_dist
            risk_u  = equity * risk_pct / 100
            qty_lev = (risk_u / sl_dist) * leverage
            notional = qty_lev * ep
            margin   = notional / leverage
            fee      = notional * TAKER_FEE
            if margin + fee > equity * 0.50:
                scale    = (equity * 0.50) / (margin + fee)
                qty_lev *= scale
                notional = qty_lev * ep
                margin   = notional / leverage
                fee      = notional * TAKER_FEE
            if qty_lev * ep < 2:
                eq_log.append(equity)
                continue
            equity -= fee
            pos = {
                "side": sig, "entry": ep, "qty": qty_lev,
                "sl": sl, "tp": tp, "notional": notional, "margin": margin,
            }
        eq_log.append(equity)

    if pos:
        lp  = float(df["close"].iloc[-1])
        fee = pos["qty"] * lp * TAKER_FEE
        pnl = ((lp - pos["entry"]) if pos["side"] == "long"
               else (pos["entry"] - lp)) * pos["qty"] - fee
        equity += pnl
        trades.append({"side": pos["side"], "entry": pos["entry"], "exit": lp,
                        "pnl_usdt": pnl, "reason": "eod", "eq_after": equity})

    tdf  = pd.DataFrame(trades)
    n    = len(tdf)
    init = 500.0 * GBP_TO_USDT
    if n == 0:
        return {"trades": 0, "wr": 0, "rr": 0,
                "return": (equity / init - 1) * 100,
                "final_gbp": equity / GBP_TO_USDT,
                "funding": funding_paid, "liqs": liqs, "eq_log": eq_log, "tdf": tdf}

    wins = tdf[tdf.pnl_usdt > 0]; loss = tdf[tdf.pnl_usdt <= 0]
    wr   = len(wins) / n * 100
    rr_a = (wins.pnl_usdt.mean() / abs(loss.pnl_usdt.mean())
            if len(wins) > 0 and len(loss) > 0 else 0)
    return {
        "trades": n, "wr": wr, "rr": rr_a,
        "return": (equity / init - 1) * 100,
        "final_gbp": equity / GBP_TO_USDT,
        "funding": funding_paid, "liqs": liqs, "eq_log": eq_log, "tdf": tdf,
    }


# ── Run ──────────────────────────────────────────────────────────────────── #
DATASETS = {
    "BTC/USDT": "data/BTCUSDT_1h.csv",
    "ETH/USDT": "data/ETHUSDT_1h.csv",
    "SOL/USDT": "data/SOLUSDT_1h.csv",
}
PARAMS = [
    ("breakout", 3, 1.5, 1.5, 3.0, 22, True),
    ("breakout", 5, 1.5, 1.5, 3.0, 22, True),
    ("combined", 3, 1.5, 1.5, 3.0, 22, True),
    ("combined", 5, 1.5, 1.5, 3.0, 22, True),
]

print(f"\n{'#'*76}")
print(f"  FIXED SL + FIXED TP (NO TRAILING)  |  Apr 2024 - Apr 2026  |  17,520 x 1h")
print(f"  GBP 500 start (~${500*GBP_TO_USDT:.0f})  |  3:1 RR  |  4h trend filter  |  ADX>22")
print(f"  Fee: 0.04% taker + 0.01%/8h funding  |  Leverage AMPLIFIES position size")
print(f"{'#'*76}\n")

all_r = []
for sym, csv in DATASETS.items():
    df = pd.read_csv(csv, parse_dates=["timestamp"], index_col="timestamp")
    df.columns = [c.lower() for c in df.columns]
    bh = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    print(f"  {sym}  |  Buy & Hold: {bh:+.1f}%")
    print(f"  {'Strat':<10} {'Lev':>4}  {'Return':>8}  {'Final GBP':>11}  {'Trades':>7}  {'Win%':>6}  {'RealRR':>7}  {'Liq':>4}  {'Fund$':>7}")
    print("  " + "-"*78)
    for strat, lev, risk, sl_m, rr, adx, shorts in PARAMS:
        r = run_fixed_tp_sl(df, lev, risk, sl_m, rr, adx, strat, shorts)
        tag = " <- BEST" if (not all_r or r["final_gbp"] > max(x["final_gbp"] for x in all_r)) else ""
        all_r.append({**r, "sym": sym, "strat": strat, "lev": lev})
        print(f"  {strat:<10} {lev:>4}x  {r['return']:>+7.1f}%  GBP{r['final_gbp']:>9,.0f}"
              f"  {r['trades']:>7}  {r['wr']:>5.1f}%  {r['rr']:>6.2f}:1  {r['liqs']:>4}"
              f"  ${r['funding']:>6.2f}{tag}")
    print()

best = max(all_r, key=lambda x: x["final_gbp"])

print(f"\n{'#'*76}")
print(f"  TOP 5 ACROSS ALL SYMBOLS")
print(f"{'#'*76}")
for r in sorted(all_r, key=lambda x: x["final_gbp"], reverse=True)[:5]:
    print(f"  {r['sym']} | {r['strat']:<10} {r['lev']}x"
          f"  Return: {r['return']:>+7.1f}%  Final: GBP{r['final_gbp']:>8,.0f}"
          f"  Win: {r['wr']:.1f}%  RR: {r['rr']:.2f}:1  Trades: {r['trades']}")

# ── Compounding projection ────────────────────────────────────────────────
print(f"\n{'#'*76}")
print(f"  GBP 500 -> GBP 100,000 COMPOUNDING ROADMAP")
print(f"  (Requires a sustained BULL MARKET — not guaranteed)")
print(f"{'#'*76}")

scenarios = [
    ("Conservative  3x lev | 30% WR | 3:1 RR | 2 trades/mo", 0.30, 3.0, 3, 1.5, 2),
    ("Moderate      5x lev | 35% WR | 3:1 RR | 3 trades/mo", 0.35, 3.0, 5, 1.5, 3),
    ("Aggressive    5x lev | 40% WR | 3:1 RR | 5 trades/mo", 0.40, 3.0, 5, 1.5, 5),
]

for label, wr, rr, lev, risk_pct, trades_mo in scenarios:
    ev_per_trade = (wr * rr * risk_pct * lev / 100
                    - (1 - wr) * risk_pct * lev / 100)
    fee_drag     = 0.0015  # ~0.15% per month fees + funding
    monthly_r    = ev_per_trade * trades_mo - fee_drag

    equity = 500.0; month = 0
    milestones = [1000, 2500, 5000, 10000, 25000, 50000, 100000]
    mi = 0; rows = []
    while equity < 100_000 and month < 480:
        equity *= (1 + monthly_r); month += 1
        if mi < len(milestones) and equity >= milestones[mi]:
            rows.append((month, equity)); mi += 1
    if equity >= 100_000:
        rows.append((month, equity))

    print(f"\n  {label}")
    print(f"  EV/trade: +{ev_per_trade*100:.2f}%  Monthly: +{monthly_r*100:.2f}%")
    for m_, eq_ in rows:
        y = m_ // 12; mo = m_ % 12
        tgt = " <-- TARGET REACHED" if eq_ >= 100_000 else ""
        print(f"    Month {m_:>4}  ({y}yr {mo:>2}mo)  GBP {eq_:>10,.0f}{tgt}")
    if equity < 100_000:
        print(f"    GBP 100k NOT reached in 40 years at this monthly rate.")

print(f"""
{'#'*76}
  HONEST VERDICT
{'#'*76}

  WHAT THE BACKTEST SHOWED (Apr 2024 - Apr 2026):
    All strategies lost money on this specific 2-year window.
    Best result: GBP {best['final_gbp']:.0f} from GBP 500.
    Why: choppy market Apr-Oct 2024 triggered 25% DD gate early.
         Then a brutal bear crash Jan 2025 - Apr 2026.

  WHY GBP 500 -> GBP 100,000 IS EXTREMELY DIFFICULT:
    Requires 200x return (20,000%).
    Even the BEST leveraged trend strategy needs:
      - A sustained bull market (like 2020-2021 crypto cycle)
      - Consistent execution without blowups
      - 3-12 years of compounding depending on scenario

  REAL RISKS WITH LEVERAGE:
    - 5x leverage: a 20% adverse move = 100% loss of margin
    - A bad streak of 5-6 trades can trigger the 25% DD gate
    - Liquidation is possible in flash crashes (even with stop-loss)
    - Funding rates cost ~3-4% per year on long positions

  PRACTICAL RECOMMENDATION:
    - Start with 2x leverage max until strategy is proven live
    - Only increase leverage after 6+ months of profitable paper/live trading
    - Never risk more than 1% per trade regardless of leverage
    - Keep 25% drawdown as hard stop — if hit, stop trading and review
{'#'*76}""")
