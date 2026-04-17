"""
Verify --optimal portfolio params against previously recorded backtest results.

Optimal preset (from PORTFOLIOS dict in hawk_trader.py):
  ETH/USDT  1h  20x  ch=12  SL=1.0  RR=2.5  ADX=off  RSI=off  MACD=off
  XRP/USDT  1h  20x  ch=12  SL=1.5  RR=2.5  ADX=off  RSI=off  MACD=off
  BTC/USDT  4h  10x  ch=8   SL=1.5  RR=2.0  ADX=off  RSI=off  MACD=off
  BNB/USDT  4h  10x  ch=16  SL=1.5  RR=3.0  ADX=25   RSI=off  MACD=off
  ADA/USDT  4h  5x   ch=8   SL=2.0  RR=2.5  ADX=off  RSI=?    MACD=?

Expected recorded results from context.md:
  ETH  +240.4%  +5.24%/mo
  XRP  +650.2%  +8.77%/mo
  BTC  +86.7%   +2.64%/mo
  BNB  +60.6%   +2.00%/mo
  ADA  +53.3%   +1.80%/mo  (backtest used RSI+MACD=on per context.md table)

Sum → +20.44%/mo
"""
from __future__ import annotations
import math, os, sys, warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Constants (must match leveraged_engine.py and comprehensive backtest) ─────
GBP_TO_USDT     = 1.27
TAKER_FEE       = 0.0004
FUNDING_RATE_8H = 0.0001
RISK_PCT        = 1.5
MAX_MARGIN_PCT  = 0.60
MAX_DD_PCT      = 30.0
INITIAL_USDT    = 500.0 * GBP_TO_USDT   # 635.0

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


# ── Wilder EMA (alpha = 1/p) ──────────────────────────────────────────────────
def _ema(arr: np.ndarray, p: int) -> np.ndarray:
    alpha = 1.0 / p
    out   = np.empty_like(arr, dtype=np.float64)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def compute_indicators(df: pd.DataFrame, channel_n: int) -> dict:
    h = df["high"].values.astype(np.float64)
    l = df["low"].values.astype(np.float64)
    c = df["close"].values.astype(np.float64)
    n = len(c)

    # ATR(14)
    tr = np.maximum(h[1:] - l[1:],
         np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    tr_full = np.empty(n); tr_full[0] = h[0] - l[0]; tr_full[1:] = tr
    atr14 = _ema(tr_full, 14)

    # EMA 20 / 50
    ema20 = _ema(c, 20)
    ema50 = _ema(c, 50)

    # ADX(14) — Wilder
    up  = np.diff(h, prepend=h[0])
    dn  = -np.diff(l, prepend=l[0])
    dm_pos = np.where((up > dn) & (up > 0), up, 0.0)
    dm_neg = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr14s = np.where(atr14 == 0, 1e-10, atr14)
    di_pos = 100 * _ema(dm_pos, 14) / atr14s
    di_neg = 100 * _ema(dm_neg, 14) / atr14s
    di_sum = np.where(di_pos + di_neg == 0, 1e-10, di_pos + di_neg)
    dx  = 100 * np.abs(di_pos - di_neg) / di_sum
    adx = _ema(dx, 14)

    # RSI(14)
    delta = np.diff(c, prepend=c[0])
    avg_g = _ema(np.where(delta > 0, delta, 0.0), 14)
    avg_l = _ema(np.where(delta < 0, -delta, 0.0), 14)
    rs    = avg_g / np.where(avg_l == 0, 1e-10, avg_l)
    rsi   = 100 - (100 / (1 + rs))

    # MACD(12,26,9)
    macd      = _ema(c, 12) - _ema(c, 26)
    macd_above = (_ema(macd, 9) < macd).astype(np.int8)  # macd > signal

    # Channel (no look-ahead: previous n bars)
    chan_hi = np.full(n, np.nan)
    chan_lo = np.full(n, np.nan)
    for i in range(channel_n, n):
        chan_hi[i] = h[i - channel_n: i].max()
        chan_lo[i] = l[i - channel_n: i].min()

    return dict(c=c, h=h, l=l, ema20=ema20, ema50=ema50, atr14=atr14,
                adx=adx, rsi=rsi, macd_above=macd_above,
                chan_hi=chan_hi, chan_lo=chan_lo, n=n)


# ── Bar-loop engine (identical to hawk_comprehensive_backtest.py) ─────────────
def run_combo(ind: dict, leverage: int, sl_m: float, rr: float,
              adx_min: float, rsi_on: bool, macd_on: bool,
              max_hold: int, fund_bars: int) -> dict:
    c, h, l = ind["c"], ind["h"], ind["l"]
    ema20, ema50 = ind["ema20"], ind["ema50"]
    atr14        = ind["atr14"]
    adx, rsi     = ind["adx"], ind["rsi"]
    macd_above   = ind["macd_above"]
    chan_hi, chan_lo = ind["chan_hi"], ind["chan_lo"]
    n = ind["n"]

    equity   = float(INITIAL_USDT)
    peak     = equity
    liq_dist = 1.0 / leverage - 0.005

    sl_est   = sl_m * 0.0066
    mpt      = RISK_PCT / 100 / (sl_est * leverage)
    max_pos  = min(3, max(1, int(MAX_MARGIN_PCT / mpt))) if mpt < MAX_MARGIN_PCT else 1

    WARMUP = 75
    positions: list = []
    last_close = -999
    wins = losses = liqs = 0
    win_pnl = loss_pnl_abs = 0.0

    for i in range(WARMUP, n):
        bc = c[i]; bh = h[i]; bl = l[i]
        batr = atr14[i]; bch = chan_hi[i]; bcl = chan_lo[i]

        if math.isnan(batr) or math.isnan(bch):
            continue
        if (1.0 - equity / peak) * 100.0 >= MAX_DD_PCT:
            break

        # Funding
        if i % fund_bars == 0:
            for pos in positions:
                fund = pos[3] * FUNDING_RATE_8H
                equity -= fund if pos[0] == 1 else -fund

        # Manage positions
        closed_idx = []
        for j, pos in enumerate(positions):
            side, entry, qty, notional, margin, sl, tp, bar_in = pos
            ep = 0.0; reason = 0
            liq_p = entry * (1.0 - liq_dist) if side == 1 else entry * (1.0 + liq_dist)
            if side == 1:
                if bl <= liq_p:   ep = liq_p;        reason = -2
                elif bl <= sl:    ep = sl * 0.9995;  reason = -1
                elif bh >= tp:    ep = tp * 0.9995;  reason =  1
                elif i - bar_in >= max_hold: ep = bc; reason = 0
            else:
                if bh >= liq_p:   ep = liq_p;        reason = -2
                elif bh >= sl:    ep = sl * 1.0005;  reason = -1
                elif bl <= tp:    ep = tp * 1.0005;  reason =  1
                elif i - bar_in >= max_hold: ep = bc; reason = 0

            if ep > 0.0:
                fee = qty * ep * TAKER_FEE
                if reason == -2:
                    pnl = -margin; liqs += 1
                else:
                    raw = (ep - entry) if side == 1 else (entry - ep)
                    pnl = raw * qty - fee
                equity += pnl
                if pnl > 0: wins   += 1; win_pnl      += pnl
                else:        losses += 1; loss_pnl_abs += abs(pnl)
                peak = max(peak, equity)
                closed_idx.append(j)
                last_close = i

        for j in reversed(closed_idx):
            positions.pop(j)

        # Entry
        if i - last_close < 1: continue
        if len(positions) >= max_pos: continue
        total_margin = sum(p[4] for p in positions)
        avail = equity * MAX_MARGIN_PCT - total_margin
        if avail <= 0.0: continue

        bull = ema20[i] > ema50[i]
        bear = ema20[i] < ema50[i]
        adx_ok         = (adx[i] >= adx_min) if adx_min > 0 else True
        rsi_long_ok    = (rsi[i] >= 50.0) if rsi_on else True
        rsi_short_ok   = (rsi[i] <= 50.0) if rsi_on else True
        macd_long_ok   = (int(macd_above[i]) == 1) if macd_on else True
        macd_short_ok  = (int(macd_above[i]) == 0) if macd_on else True

        sd = sl_m * batr
        if sd <= 0: continue

        if bull and adx_ok and rsi_long_ok and macd_long_ok and bc > bch:
            ep  = bc * 1.0005
            qty = (equity * RISK_PCT / 100.0) / sd
            not_ = qty * ep; mrg = not_ / leverage; fee = not_ * TAKER_FEE
            if mrg + fee > avail:
                scale = avail / (mrg + fee)
                qty *= scale; not_ = qty * ep; mrg = not_ / leverage; fee = not_ * TAKER_FEE
            if not_ >= 2.0:
                equity -= fee
                positions.append([1, ep, qty, not_, mrg, ep - sd, ep + rr * sd, i])

        elif bear and adx_ok and rsi_short_ok and macd_short_ok and bc < bcl:
            ep  = bc * 0.9995
            qty = (equity * RISK_PCT / 100.0) / sd
            not_ = qty * ep; mrg = not_ / leverage; fee = not_ * TAKER_FEE
            if mrg + fee > avail:
                scale = avail / (mrg + fee)
                qty *= scale; not_ = qty * ep; mrg = not_ / leverage; fee = not_ * TAKER_FEE
            if not_ >= 2.0:
                equity -= fee
                positions.append([-1, ep, qty, not_, mrg, ep + sd, ep - rr * sd, i])

    # Close remaining
    for pos in positions:
        lp  = c[-1]
        raw = (lp - pos[1]) if pos[0] == 1 else (pos[1] - lp)
        pnl = raw * pos[2] - pos[2] * lp * TAKER_FEE
        equity += pnl
        if pnl > 0: wins   += 1; win_pnl      += pnl
        else:        losses += 1; loss_pnl_abs += abs(pnl)

    n_trades = wins + losses
    wr        = wins / n_trades * 100.0 if n_trades else 0.0
    actual_rr = win_pnl / loss_pnl_abs if loss_pnl_abs > 0 else 0.0
    ret_pct   = (equity / INITIAL_USDT - 1.0) * 100.0
    return dict(ret=ret_pct, wr=wr, rr=actual_rr, trades=n_trades, liqs=liqs)


# ── Load CSV ──────────────────────────────────────────────────────────────────
def load(symbol: str, tf: str) -> pd.DataFrame:
    fname = f"{symbol.replace('/', '')}_{tf}.csv"
    path  = os.path.join(DATA_DIR, fname)
    df    = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    df.columns = [c.lower() for c in df.columns]
    return df


# ── Main ──────────────────────────────────────────────────────────────────────
OPTIMAL = [
    # Exact backtest params per context.md "Best per asset" table:
    #   ETH 1h 20x: ch=12, SL=1.0, RR=2.5, RSI+MACD on, no ADX
    #   XRP 1h 20x: ch=12, SL=1.5, RR=2.5, no filters
    #   BTC 4h 10x: ch=8,  SL=1.5, RR=2.0, RSI+MACD on, no ADX
    #   BNB 4h 10x: ch=16, SL=1.5, RR=3.0, ADX>=25, RSI on, no MACD
    #   ADA 4h 5x:  ch=8,  SL=2.0, RR=2.5, RSI+MACD on, no ADX
    #
    # label          csv_sym     tf    lev  ch   sl    rr   adx_min rsi   macd   exp_ret  exp_mo  note
    ("ETH 1h 20x",  "ETHUSDT",  "1h", 20, 12, 1.0, 2.5,    0,   True,  True,  240.4,  5.24,  "RSI+MACD"),
    ("XRP 1h 20x",  "XRPUSDT",  "1h", 20, 12, 1.5, 2.5,    0,   False, False, 650.2,  8.77,  "no filters"),
    ("BTC 4h 10x",  "BTCUSDT",  "4h", 10,  8, 1.5, 2.0,    0,   True,  True,   86.7,  2.64,  "RSI+MACD"),
    ("BNB 4h 10x",  "BNBUSDT",  "4h", 10, 16, 1.5, 3.0,   25,   True,  False,  60.6,  2.00,  "ADX=25+RSI"),
    ("ADA 4h 5x",   "ADAUSDT",  "4h",  5,  8, 2.0, 2.5,    0,   True,  True,   53.3,  1.80,  "RSI+MACD"),
]

MAX_HOLD = {"1h": 30, "4h": 12}
FUND_BARS= {"1h":  8, "4h":  2}

def mo_pct(ret_pct: float, days: int) -> float:
    months = days / 30.44
    final  = INITIAL_USDT * (1 + ret_pct / 100)
    return ((final / INITIAL_USDT) ** (1 / months) - 1) * 100

OK  = "[OK]"
BAD = "[MISMATCH]"
WARN= "[N/A]"

def check(got: float, exp: float, tol: float = 0.5) -> str:
    if exp is None: return WARN
    return OK if abs(got - exp) <= tol else BAD

print()
print("=" * 78)
print("  OPTIMAL PORTFOLIO — backtest verification")
print("=" * 78)
print(f"  Engine: same bar-loop as hawk_comprehensive_backtest.py")
print(f"  Capital: GBP 500 = ${INITIAL_USDT:.2f} USDT")
print(f"  Fee: {TAKER_FEE*100:.2f}%  Funding: {FUNDING_RATE_8H*100:.3f}%/8h")
print()

total_mo = 0.0

for (label, sym, tf, lev, ch, sl, rr, adx_min, rsi_on, macd_on,
     exp_ret, exp_mo, note) in OPTIMAL:
    df   = load(sym, tf)
    days = (df.index[-1] - df.index[0]).days
    ind  = compute_indicators(df, ch)
    res  = run_combo(ind, lev, sl, rr, adx_min, rsi_on, macd_on,
                     MAX_HOLD[tf], FUND_BARS[tf])

    mo = mo_pct(res["ret"], days)
    cr = check(res["ret"], exp_ret)
    cm = check(mo, exp_mo)

    exp_ret_s = f"{exp_ret:+.1f}%" if exp_ret is not None else "---"
    exp_mo_s  = f"{exp_mo:+.2f}%" if exp_mo  is not None else "---"

    print(f"  {label}  ({note})")
    print(f"    Return  : {res['ret']:+7.1f}%   expected {exp_ret_s}  {cr}")
    print(f"    Monthly : {mo:+7.2f}%   expected {exp_mo_s}  {cm}")
    print(f"    WR      : {res['wr']:5.1f}%   RR={res['rr']:.2f}   Trades={res['trades']}   Liqs={res['liqs']}")
    print()

    total_mo += mo

print("-" * 78)
print(f"  Combined monthly% (sum of 5 individual results):")
print(f"    Got      : {total_mo:+.2f}%")
print(f"    Expected : +20.44%   {check(total_mo, 20.44, 1.0)}")
print()
print("  NOTE: hawk_trader.py optimal preset is missing RSI/MACD wiring for ETH/BTC/ADA.")
print("  The +20.44%/mo backtest result requires those filters to be active in the trader.")
print("=" * 78)
print()
