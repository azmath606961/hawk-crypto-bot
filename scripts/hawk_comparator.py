"""
HAWK Portfolio Comparator
==========================
Reads all four portfolio state files + trade logs and renders a side-by-side
comparison dashboard in the terminal, plus a summary of which setup is winning.

Portfolios compared:
  conservative       — baseline 10x
  conservative_vol   — 10x + volume Z-score filter on 1h symbols (A/B test)
  optimal            — baseline mixed leverage
  optimal_vol        — mixed leverage + volume Z-score filter on 1h symbols

Usage:
  python scripts/hawk_comparator.py           # one-shot snapshot
  python scripts/hawk_comparator.py --watch   # refresh every 60s
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd

LOGS_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")
GBP_RATE = 1.27

PORTFOLIOS = [
    dict(key="conservative",     label="Conservative    (baseline)",     state="hawk_state_conservative.json",     trades="hawk_trades_conservative.csv"),
    dict(key="conservative_vol", label="Conservative+Vol (vol filter)",  state="hawk_state_conservative_vol.json", trades="hawk_trades_conservative_vol.csv"),
    dict(key="optimal",          label="Optimal         (baseline)",     state="hawk_state_optimal.json",          trades="hawk_trades_optimal.csv"),
    dict(key="optimal_vol",      label="Optimal+Vol     (vol filter)",   state="hawk_state_optimal_vol.json",      trades="hawk_trades_optimal_vol.csv"),
]


def load_state(fname: str) -> dict | None:
    path = os.path.join(LOGS_DIR, fname)
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path))
    except Exception:
        return None


def load_trades(fname: str) -> pd.DataFrame | None:
    path = os.path.join(LOGS_DIR, fname)
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
        if df.empty:
            return None
        return df
    except Exception:
        return None


def portfolio_stats(state: dict, trades_df: pd.DataFrame | None) -> dict:
    equity   = state.get("equity", 0.0)
    peak     = state.get("peak_equity", equity)
    dd       = (1 - equity / peak) * 100 if peak > 0 else 0.0
    init     = state.get("initial_usdt", 635.0)
    total_pnl = equity - init

    ticks_1h = state.get("bar_count",    0)
    ticks_4h = state.get("bar_count_4h", 0)
    funding  = sum(p.get("funding_paid", 0) for p in state.get("positions", []))

    wins = losses = win_pnl = loss_pnl = 0
    if trades_df is not None and not trades_df.empty:
        closed = trades_df[trades_df.get("reason", pd.Series()).notna()] if "reason" in trades_df.columns else trades_df
        for _, row in closed.iterrows():
            pnl = row.get("pnl", 0.0)
            if pnl > 0:
                wins += 1; win_pnl += pnl
            elif pnl < 0:
                losses += 1; loss_pnl += abs(pnl)
    n_trades = wins + losses
    wr       = wins / n_trades * 100 if n_trades else 0.0
    rr       = win_pnl / loss_pnl if loss_pnl > 0 else 0.0
    open_pos = len(state.get("positions", []))
    unreal   = sum(0.0 for _ in state.get("positions", []))  # no cur_price in state

    return dict(
        equity=equity, gbp=equity/GBP_RATE, peak=peak, dd=dd,
        total_pnl=total_pnl, trades=n_trades, wins=wins,
        wr=wr, rr=rr, open_pos=open_pos,
        ticks_1h=ticks_1h, ticks_4h=ticks_4h,
        funding=funding,
    )


def render(portfolios: list[dict]) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*90}")
    print(f"  HAWK PORTFOLIO COMPARATOR  |  {now}")
    print(f"{'='*90}")

    loaded = []
    for pf in portfolios:
        state = load_state(pf["state"])
        if state is None:
            print(f"  {pf['label']:<36}  — no state file yet (bot not started)")
            loaded.append(None)
            continue
        trades = load_trades(pf["trades"])
        s = portfolio_stats(state, trades)
        loaded.append((pf, s))

    available = [(pf, s) for item in loaded if item is not None for pf, s in [item]]
    if not available:
        print("  No portfolios running yet. Start bots first.")
        print(f"{'='*90}\n")
        return

    # Header row
    col = 20
    header = f"  {'Metric':<24}"
    for pf, _ in available:
        header += f"  {pf['label'][:col]:<{col}}"
    print(header)
    print(f"  {'-'*24}" + (f"  {'-'*col}" * len(available)))

    def row(label, fmt, *vals):
        line = f"  {label:<24}"
        for v in vals:
            line += f"  {fmt.format(v):<{col}}"
        print(line)

    equities = [s["equity"] for _, s in available]
    max_eq   = max(equities) if equities else 0

    for label, key, fmt in [
        ("Equity ($)",       "equity",    "${:.2f}"),
        ("Equity (GBP)",     "gbp",       "£{:.2f}"),
        ("Peak equity ($)",  "peak",      "${:.2f}"),
        ("Drawdown",         "dd",        "{:.1f}%"),
        ("Total PnL ($)",    "total_pnl", "${:+.2f}"),
        ("Open positions",   "open_pos",  "{}"),
        ("Closed trades",    "trades",    "{}"),
        ("Win rate",         "wr",        "{:.1f}%"),
        ("Actual RR",        "rr",        "{:.2f}"),
        ("1h ticks",         "ticks_1h",  "{}"),
        ("4h ticks",         "ticks_4h",  "{}"),
    ]:
        vals = [s[key] for _, s in available]
        formatted = []
        for i, v in enumerate(vals):
            cell = fmt.format(v)
            # Highlight best equity
            if key == "equity" and v == max_eq and len(available) > 1:
                cell = f"★ {cell}"
            formatted.append(cell)
        line = f"  {label:<24}"
        for cell in formatted:
            line += f"  {cell:<{col}}"
        print(line)

    # ── Insight section ──────────────────────────────────────────────────────
    print(f"\n  {'─'*86}")
    print("  INSIGHTS")

    # Compare vol variants vs their baselines
    pf_keys = {pf["key"]: s for pf, s in available}
    for base, enhanced in [("conservative", "conservative_vol"), ("optimal", "optimal_vol")]:
        if base in pf_keys and enhanced in pf_keys:
            bs = pf_keys[base];  es = pf_keys[enhanced]
            delta = es["equity"] - bs["equity"]
            delta_pnl = es["total_pnl"] - bs["total_pnl"]
            winner = enhanced if delta > 0 else base
            sign = "+" if delta >= 0 else ""
            print(f"  {base} vs {enhanced}: vol-filter is "
                  f"{'AHEAD' if delta > 0 else 'BEHIND'} by ${abs(delta):.2f}  "
                  f"(PnL delta: {sign}${delta_pnl:.2f})")
            if bs["trades"] > 5 and es["trades"] > 5:
                wr_delta = es["wr"] - bs["wr"]
                rr_delta = es["rr"] - bs["rr"]
                print(f"    WR: {bs['wr']:.1f}% → {es['wr']:.1f}% ({'+' if wr_delta>=0 else ''}{wr_delta:.1f}pp)  "
                      f"RR: {bs['rr']:.2f} → {es['rr']:.2f} ({'+' if rr_delta>=0 else ''}{rr_delta:.2f})")
        else:
            missing = [k for k in [base, enhanced] if k not in pf_keys]
            print(f"  [{', '.join(missing)}] not running — start those bots to compare")

    print(f"{'='*90}\n")


def main():
    parser = argparse.ArgumentParser(description="HAWK 4-portfolio comparator")
    parser.add_argument("--watch", action="store_true", help="Refresh every 60s (Ctrl+C to stop)")
    args = parser.parse_args()

    if args.watch:
        print("Watching — refreshing every 60s. Ctrl+C to stop.")
        while True:
            os.system("cls" if os.name == "nt" else "clear")
            render(PORTFOLIOS)
            time.sleep(60)
    else:
        render(PORTFOLIOS)


if __name__ == "__main__":
    main()
