"""
HAWK Portfolio Backtest
=======================
Runs the 4 portfolio presets through the same run_combo engine used by the
comprehensive backtester — so vol=off results are identical to the originals.

Portfolios:
  conservative     — original, 10x, no vol filter
  optimal          — original, mixed leverage, no vol filter
  conservative_vol — conservative + vol_filter on ETH/XRP 1h
  optimal_vol      — optimal + vol_filter on ETH/XRP 1h

Usage:
  python scripts/hawk_portfolio_backtest.py                   # all 4 + comparison
  python scripts/hawk_portfolio_backtest.py --portfolio conservative
  python scripts/hawk_portfolio_backtest.py --portfolio optimal_vol
  python scripts/hawk_portfolio_backtest.py --compare conservative conservative_vol

Output: stdout table + data/portfolio_backtest_results.csv
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd

# Re-use the proven engine from hawk_comprehensive_backtest
from scripts.hawk_comprehensive_backtest import (
    compute_dataset_indicators,
    run_combo,
    load_df,
    INITIAL_USDT,
)

# ── Portfolio definitions (mirror of PORTFOLIOS in hawk_trader.py) ─────────────
# Each asset entry: (symbol_base, tf, leverage, channel_n, sl_atr, rr,
#                    adx_min, rsi_on, macd_on, vol_on)

PORTFOLIOS: dict[str, dict] = {
    "conservative": {
        "label": "Conservative — 10x all — no vol filter",
        "assets": [
            ("ETHUSDT", "1h", 10,  8,  2.0, 2.0,  0.0, True,  False, False),
            ("XRPUSDT", "1h", 10, 16,  1.0, 3.0,  0.0, False, False, False),
            ("BTCUSDT", "4h", 10,  8,  1.5, 2.0,  0.0, True,  True,  False),
            ("BNBUSDT", "4h", 10, 16,  1.5, 3.0, 25.0, True,  False, False),
            ("ADAUSDT", "4h", 10, 16,  2.0, 2.5,  0.0, True,  True,  False),
        ],
    },
    "optimal": {
        "label": "Optimal — mixed leverage — no vol filter",
        "assets": [
            ("ETHUSDT", "1h", 20, 12,  1.0, 2.5,  0.0, True,  True,  False),
            ("XRPUSDT", "1h", 20, 12,  1.5, 2.5,  0.0, False, False, False),
            ("BTCUSDT", "4h", 10,  8,  1.5, 2.0,  0.0, True,  True,  False),
            ("BNBUSDT", "4h", 10, 16,  1.5, 3.0, 25.0, True,  False, False),
            ("ADAUSDT", "4h",  5,  8,  2.0, 2.5,  0.0, False, True,  False),
        ],
    },
    "conservative_vol": {
        "label": "Conservative+Vol — 10x all — vol filter on ETH/XRP 1h",
        "assets": [
            ("ETHUSDT", "1h", 10,  8,  2.0, 2.0,  0.0, True,  False, True),   # vol ON
            ("XRPUSDT", "1h", 10, 16,  1.0, 3.0,  0.0, False, False, True),   # vol ON
            ("BTCUSDT", "4h", 10,  8,  1.5, 2.0,  0.0, True,  True,  False),  # unchanged
            ("BNBUSDT", "4h", 10, 16,  1.5, 3.0, 25.0, True,  False, False),  # unchanged
            ("ADAUSDT", "4h", 10, 16,  2.0, 2.5,  0.0, True,  True,  False),  # unchanged
        ],
    },
    "optimal_vol": {
        "label": "Optimal+Vol — mixed leverage — vol filter on ETH/XRP 1h",
        "assets": [
            ("ETHUSDT", "1h", 20, 12,  1.0, 2.5,  0.0, True,  True,  True),   # vol ON
            ("XRPUSDT", "1h", 20, 12,  1.5, 2.5,  0.0, False, False, True),   # vol ON
            ("BTCUSDT", "4h", 10,  8,  1.5, 2.0,  0.0, True,  True,  False),  # unchanged
            ("BNBUSDT", "4h", 10, 16,  1.5, 3.0, 25.0, True,  False, False),  # unchanged
            ("ADAUSDT", "4h",  5,  8,  2.0, 2.5,  0.0, False, True,  False),  # unchanged
        ],
    },
}

MAX_HOLD = {"1h": 30, "4h": 12}
FUND_BARS = {"1h": 8,  "4h": 2}

DATA_DIR   = os.path.join(os.path.dirname(__file__), "..", "data")
RESULT_CSV = os.path.join(DATA_DIR, "portfolio_backtest_results.csv")


# ── Indicator cache: compute once per (asset, tf) pair ────────────────────────

_ind_cache: dict[tuple, dict] = {}

def get_indicators(asset: str, tf: str) -> dict | None:
    key = (asset, tf)
    if key not in _ind_cache:
        df = load_df(asset, tf)
        if df is None:
            return None
        _ind_cache[key] = {
            "ind":  compute_dataset_indicators(df),
            "days": (df.index[-1] - df.index[0]).total_seconds() / 86_400,
        }
    return _ind_cache[key]


# ── Single asset backtest ──────────────────────────────────────────────────────

def backtest_asset(
    asset: str, tf: str, leverage: int,
    channel_n: int, sl_atr: float, rr: float,
    adx_min: float, rsi_on: bool, macd_on: bool, vol_on: bool,
) -> dict:
    cache = get_indicators(asset, tf)
    if cache is None:
        return {"error": f"No data for {asset} {tf}"}

    ind  = cache["ind"]
    days = cache["days"]

    ret, wr, actual_rr, n_trades, n_liqs = run_combo(
        ind["c"], ind["h"], ind["l"],
        ind["ema20"], ind["ema50"], ind["atr14"],
        ind["adx"], ind["rsi"], ind["macd_above"],
        ind["chan_hi"][channel_n], ind["chan_lo"][channel_n],
        ind["v"], ind["vol_mean"], ind["vol_std"],
        leverage, sl_atr, rr, adx_min,
        rsi_on, macd_on, vol_on,
        MAX_HOLD[tf], FUND_BARS[tf],
    )

    months  = days / 30.44
    final_eq = INITIAL_USDT * (1 + ret / 100)
    mo_pct  = ((final_eq / INITIAL_USDT) ** (1.0 / months) - 1) * 100 if months > 0 else 0.0
    ev      = (wr / 100) * actual_rr - (1 - wr / 100) if actual_rr else 0.0

    return {
        "asset":      asset,
        "tf":         tf,
        "leverage":   leverage,
        "channel":    channel_n,
        "sl_atr":     sl_atr,
        "rr":         rr,
        "adx_min":    adx_min,
        "rsi":        "on" if rsi_on  else "off",
        "macd":       "on" if macd_on else "off",
        "vol":        "on" if vol_on  else "off",
        "return_pct": round(ret, 2),
        "monthly_pct": round(mo_pct, 3),
        "wr":         round(wr, 2),
        "actual_rr":  round(actual_rr, 3),
        "trades":     n_trades,
        "liqs":       n_liqs,
        "ev":         round(ev, 4),
    }


# ── Run a full portfolio ───────────────────────────────────────────────────────

def run_portfolio(name: str) -> list[dict]:
    pf    = PORTFOLIOS[name]
    rows  = []
    for entry in pf["assets"]:
        asset, tf, lev, ch, sl, rr, adx, rsi, macd, vol = entry
        row = backtest_asset(asset, tf, lev, ch, sl, rr, adx, rsi, macd, vol)
        row["portfolio"] = name
        rows.append(row)
    return rows


# ── Print helpers ──────────────────────────────────────────────────────────────

ASSET_HDR = (
    f"  {'Asset':<10} {'TF':<3} {'Lev':>4} {'Ch':>3} {'SL':>4} {'RR':>4} "
    f"{'ADX':>4} {'RSI':>4} {'MACD':>5} {'VOL':>4} "
    f"{'Mo%':>7} {'WR%':>6} {'RR_act':>7} {'T':>5} {'Liqs':>4} {'EV':>7}"
)
ASSET_SEP = "  " + "-" * 102


def _asset_row(r: dict) -> str:
    return (
        f"  {r['asset']:<10} {r['tf']:<3} {r['leverage']:>4}x {r['channel']:>3} "
        f"{r['sl_atr']:>4.1f} {r['rr']:>4.1f} "
        f"{str(int(r['adx_min'])) if r['adx_min'] else 'off':>4} "
        f"{r['rsi']:>4} {r['macd']:>5} {r['vol']:>4} "
        f"{r['monthly_pct']:>+6.2f}% "
        f"{r['wr']:>5.1f}% {r['actual_rr']:>7.3f} "
        f"{r['trades']:>5} {r['liqs']:>4} {r['ev']:>+7.4f}"
    )


def print_portfolio(name: str, rows: list[dict]) -> None:
    pf    = PORTFOLIOS[name]
    total = sum(r["monthly_pct"] for r in rows if "error" not in r)
    print(f"\n{'=' * 104}")
    print(f"  {pf['label'].upper()}")
    print(f"  Combined monthly: {total:+.2f}%/mo")
    print(f"{'=' * 104}")
    print(ASSET_HDR)
    print(ASSET_SEP)
    for r in rows:
        if "error" in r:
            print(f"  {r['asset']} {r['tf']}  ERROR: {r['error']}")
        else:
            print(_asset_row(r))
    print(f"  {'COMBINED':>79}  {total:>+6.2f}%/mo")


# ── Comparison table ───────────────────────────────────────────────────────────

def print_comparison(all_results: dict[str, list[dict]]) -> None:
    print(f"\n\n{'=' * 104}")
    print("  PORTFOLIO COMPARISON SUMMARY")
    print(f"{'=' * 104}")

    # Build asset-level comparison
    # Collect all (asset, tf) pairs across all portfolios
    assets_seen: list[tuple] = []
    for rows in all_results.values():
        for r in rows:
            key = (r["asset"], r["tf"])
            if key not in assets_seen:
                assets_seen.append(key)

    names = list(all_results.keys())
    col_w = 12

    # Header
    hdr = f"  {'Asset':<12} {'TF':<3}"
    for n in names:
        hdr += f"  {n[:col_w]:>{col_w}}"
    print(hdr)
    print("  " + "-" * (16 + len(names) * (col_w + 2)))

    # Per-asset rows
    totals: dict[str, float] = {n: 0.0 for n in names}
    for asset, tf in assets_seen:
        line = f"  {asset:<12} {tf:<3}"
        for n in names:
            match = next((r for r in all_results[n]
                          if r["asset"] == asset and r["tf"] == tf), None)
            if match and "error" not in match:
                mo = match["monthly_pct"]
                totals[n] += mo
                line += f"  {mo:>+{col_w}.2f}%"
            else:
                line += f"  {'N/A':>{col_w}}"
        print(line)

    print("  " + "-" * (16 + len(names) * (col_w + 2)))

    # Totals row
    total_line = f"  {'COMBINED':<12} {'':3}"
    for n in names:
        total_line += f"  {totals[n]:>+{col_w}.2f}%"
    print(total_line)

    # Delta rows vs baseline portfolios
    pairs = [
        ("conservative_vol", "conservative"),
        ("optimal_vol",      "optimal"),
    ]
    for vol_name, base_name in pairs:
        if vol_name in totals and base_name in totals:
            delta = totals[vol_name] - totals[base_name]
            marker = " ✓ vol helps" if delta > 0 else " ✗ vol hurts"
            delta_line = f"  {'Δ ' + vol_name + ' vs ' + base_name:<15}"
            delta_line = f"  Delta {vol_name} vs {base_name}:"
            delta_line += f"  {delta:>+.2f}%/mo{marker}"
            print(delta_line)

    # Answer the "same as original?" question
    print()
    print("  Note: conservative_vol / optimal_vol with VOL=off are byte-for-byte")
    print("  identical to conservative / optimal (same engine, same params).")
    print(f"{'=' * 104}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="HAWK portfolio backtest — compare conservative / optimal / vol variants"
    )
    parser.add_argument(
        "--portfolio", "-p",
        choices=list(PORTFOLIOS.keys()),
        help="Run a single portfolio (default: all 4)",
    )
    parser.add_argument(
        "--compare", "-c",
        nargs="+",
        choices=list(PORTFOLIOS.keys()),
        metavar="PORTFOLIO",
        help="Portfolios to include in comparison table (default: all available)",
    )
    args = parser.parse_args()

    t0 = time.time()
    print("\n" + "=" * 104)
    print("  HAWK PORTFOLIO BACKTEST")
    print("  Engine: hawk_comprehensive_backtest.run_combo  (Wilder EMA, exact portfolio params)")
    print("  Note:   vol=off results are identical to original conservative / optimal backtests")
    print("=" * 104 + "\n")

    # Which portfolios to run
    if args.portfolio:
        names_to_run = [args.portfolio]
    else:
        names_to_run = list(PORTFOLIOS.keys())

    # Run each
    all_results: dict[str, list[dict]] = {}
    for name in names_to_run:
        print(f"  Running {name} ...", flush=True)
        rows = run_portfolio(name)
        all_results[name] = rows
        print_portfolio(name, rows)

    # Comparison (only if 2+ portfolios)
    compare_names = args.compare or names_to_run
    if len(compare_names) >= 2:
        # Filter to what was actually run
        avail = {n: all_results[n] for n in compare_names if n in all_results}
        if len(avail) >= 2:
            print_comparison(avail)

    # Save CSV
    all_rows: list[dict] = []
    for name, rows in all_results.items():
        for r in rows:
            if "error" not in r:
                all_rows.append(r)
    if all_rows:
        df_out = pd.DataFrame(all_rows)
        df_out.to_csv(RESULT_CSV, index=False)
        print(f"\n  Results saved → data/portfolio_backtest_results.csv")

    print(f"  Done in {time.time() - t0:.1f}s\n")


if __name__ == "__main__":
    main()
