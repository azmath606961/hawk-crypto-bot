"""
HAWK Trading Dashboard
======================
Real-time web dashboard for HAWK paper/live trading.
Reads logs/hawk_state.json and logs/hawk_trades.csv.

Usage:
    python scripts/hawk_dashboard.py
    python scripts/hawk_dashboard.py --state logs/hawk_state.json --port 5000

Open browser at: http://localhost:5000
Auto-refreshes every 60 seconds.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

# ── Config (set by CLI args) ─────────────────────────────────────────────────
STATE_FILE  = "logs/hawk_state.json"
TRADE_LOG   = "logs/hawk_trades.csv"
GBP_TO_USDT = 1.27

# Backtest reference numbers (from 25,920-combo grid search, corrected Wilder EMA)
BACKTEST = {
    "win_rate":    43.0,   # % (10x portfolio average)
    "avg_win":     45.0,   # USDT
    "avg_loss":    -22.0,  # USDT
    "monthly_pct": 14.54,  # % combined portfolio
    "max_consec_losses": 4,
}

# ─────────────────────────────────────────────────────────────────────────── #
#  Data helpers                                                                 #
# ─────────────────────────────────────────────────────────────────────────── #

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE) as f:
        return json.load(f)


def load_trades() -> list[dict]:
    if not os.path.exists(TRADE_LOG):
        return []
    trades = []
    with open(TRADE_LOG, newline="") as f:
        for row in csv.DictReader(f):
            trades.append(row)
    return trades


def compute_stats(state: dict, trades: list[dict]) -> dict:
    equity       = state.get("equity", 0)
    peak         = state.get("peak_equity", equity or 1)
    initial_usdt = state.get("equity", 635.0)   # fallback
    # Try to derive initial from started equity (not stored — use first trade or GBP500)
    total_pnl    = state.get("total_pnl", 0.0)
    initial_usdt = equity - total_pnl if equity else 635.0

    closed       = state.get("closed_trades", 0)
    wins         = state.get("wins", 0)
    losses       = closed - wins
    win_rate     = (wins / closed * 100) if closed else 0
    dd_pct       = (1 - equity / peak) * 100 if peak else 0
    return_pct   = ((equity - initial_usdt) / initial_usdt * 100) if initial_usdt else 0

    pnls = [float(t["pnl_usdt"]) for t in trades if t.get("pnl_usdt")]
    wins_usdt  = [p for p in pnls if p > 0]
    loss_usdt  = [p for p in pnls if p < 0]
    avg_win    = sum(wins_usdt) / len(wins_usdt) if wins_usdt else 0
    avg_loss   = sum(loss_usdt) / len(loss_usdt) if loss_usdt else 0
    gross_p    = sum(wins_usdt)
    gross_l    = abs(sum(loss_usdt))
    pf         = (gross_p / gross_l) if gross_l else float("inf")

    # Max consecutive losses
    max_consec = consec = 0
    for p in pnls:
        if p < 0:
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0

    # Daily P&L
    daily: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.get("ts_close") and t.get("pnl_usdt"):
            day = t["ts_close"][:10]
            daily[day] += float(t["pnl_usdt"])

    # Equity curve from trades
    eq_curve = []
    running = initial_usdt
    for t in trades:
        if t.get("pnl_usdt") and t.get("ts_close"):
            running += float(t["pnl_usdt"])
            eq_curve.append({"ts": t["ts_close"][:16], "equity": round(running, 2)})

    # Started
    started = state.get("started", "")[:10] if state.get("started") else "—"

    # Days running
    days = 0
    if state.get("started"):
        try:
            start_dt = datetime.fromisoformat(state["started"].replace("Z", "+00:00"))
            days = (datetime.now(timezone.utc) - start_dt).days
        except Exception:
            pass

    monthly_pct = 0.0
    if days > 0:
        monthly_pct = (((equity / initial_usdt) ** (30 / days)) - 1) * 100 if equity > 0 else 0

    return {
        "equity":       round(equity, 2),
        "equity_gbp":   round(equity / GBP_TO_USDT, 2),
        "initial_usdt": round(initial_usdt, 2),
        "peak":         round(peak, 2),
        "total_pnl":    round(total_pnl, 2),
        "return_pct":   round(return_pct, 2),
        "dd_pct":       round(dd_pct, 2),
        "closed":       closed,
        "wins":         wins,
        "losses":       losses,
        "win_rate":     round(win_rate, 1),
        "avg_win":      round(avg_win, 2),
        "avg_loss":     round(avg_loss, 2),
        "profit_factor": round(pf, 3) if pf != float("inf") else "∞",
        "max_consec_losses": max_consec,
        "funding_paid": round(state.get("funding_paid", 0), 2),
        "liqs":         state.get("liqs", 0),
        "bar_count":    state.get("bar_count", 0),
        "bar_count_4h": state.get("bar_count_4h", 0),
        "positions":    state.get("positions", []),
        "daily_pnl":    dict(sorted(daily.items())),
        "eq_curve":     eq_curve,
        "started":      started,
        "days":         days,
        "monthly_pct":  round(monthly_pct, 2),
        "now":          datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ─────────────────────────────────────────────────────────────────────────── #
#  API                                                                          #
# ─────────────────────────────────────────────────────────────────────────── #

@app.route("/api/data")
def api_data():
    state  = load_state()
    trades = load_trades()
    stats  = compute_stats(state, trades)
    return jsonify({
        "stats":     stats,
        "trades":    trades[-50:][::-1],   # last 50, newest first
        "backtest":  BACKTEST,
        "positions": state.get("positions", []),
    })


# ─────────────────────────────────────────────────────────────────────────── #
#  HTML Dashboard                                                               #
# ─────────────────────────────────────────────────────────────────────────── #

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HAWK Trading Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }
  header { padding: 18px 28px; border-bottom: 1px solid #21262d; display:flex; justify-content:space-between; align-items:center; }
  header h1 { font-size: 18px; font-weight: 600; color: #58a6ff; }
  header .sub { font-size: 12px; color: #8b949e; }
  .container { padding: 20px 28px; max-width: 1400px; margin: 0 auto; }

  /* Guards row */
  .guards { display:flex; gap:12px; margin-bottom:20px; flex-wrap:wrap; }
  .guard { padding: 6px 14px; border-radius: 20px; font-size: 12px; font-weight: 600; display:flex; align-items:center; gap:6px; }
  .guard .dot { width:8px; height:8px; border-radius:50%; }
  .guard.ok  { background:#0d2b1a; color:#3fb950; border:1px solid #238636; }
  .guard.ok .dot { background:#3fb950; }
  .guard.warn { background:#2b1a0d; color:#e3b341; border:1px solid #9e6a03; }
  .guard.warn .dot { background:#e3b341; }
  .guard.bad  { background:#2b0d0d; color:#f85149; border:1px solid #da3633; }
  .guard.bad .dot { background:#f85149; }

  /* Stats cards */
  .cards { display:grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap:12px; margin-bottom:20px; }
  .card { background:#161b22; border:1px solid #21262d; border-radius:8px; padding:14px 16px; }
  .card .label { font-size:11px; color:#8b949e; text-transform:uppercase; letter-spacing:.5px; margin-bottom:6px; }
  .card .value { font-size:22px; font-weight:700; }
  .card .bt    { font-size:11px; color:#8b949e; margin-top:4px; }
  .green { color:#3fb950; }
  .red   { color:#f85149; }
  .blue  { color:#58a6ff; }
  .yellow{ color:#e3b341; }
  .white { color:#e6edf3; }

  /* Reality check */
  .reality { background:#161b22; border:1px solid #21262d; border-radius:8px; padding:16px 20px; margin-bottom:20px; }
  .reality h2 { font-size:12px; color:#8b949e; text-transform:uppercase; letter-spacing:.5px; margin-bottom:12px; }
  .reality-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap:12px; }
  .rc-item { }
  .rc-item .rc-label { font-size:11px; color:#8b949e; margin-bottom:4px; }
  .rc-item .rc-bt    { font-size:11px; color:#8b949e; }
  .rc-item .rc-live  { font-size:20px; font-weight:700; }

  /* Charts row */
  .charts { display:grid; grid-template-columns: 2fr 1fr; gap:16px; margin-bottom:20px; }
  .chart-box { background:#161b22; border:1px solid #21262d; border-radius:8px; padding:16px; }
  .chart-box h2 { font-size:12px; color:#8b949e; text-transform:uppercase; letter-spacing:.5px; margin-bottom:12px; }
  .chart-box canvas { width:100% !important; }

  /* Tables */
  .table-box { background:#161b22; border:1px solid #21262d; border-radius:8px; padding:16px; margin-bottom:20px; }
  .table-box h2 { font-size:12px; color:#8b949e; text-transform:uppercase; letter-spacing:.5px; margin-bottom:12px; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th { color:#8b949e; text-align:left; padding:6px 10px; border-bottom:1px solid #21262d; font-weight:500; }
  td { padding:7px 10px; border-bottom:1px solid #161b22; }
  tr:hover td { background:#1c2128; }
  .badge { padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; }
  .badge.long  { background:#0d2b1a; color:#3fb950; }
  .badge.short { background:#2b0d0d; color:#f85149; }
  .badge.tp    { background:#0d1f3a; color:#58a6ff; }
  .badge.sl    { background:#2b1a0d; color:#e3b341; }
  .badge.timeout { background:#1c2128; color:#8b949e; }
  .badge.liq   { background:#3b0d0d; color:#f85149; }
  .empty { text-align:center; color:#8b949e; padding:20px; }
  .refresh-note { font-size:11px; color:#8b949e; text-align:right; margin-bottom:10px; }
</style>
</head>
<body>

<header>
  <div>
    <h1>HAWK Trading Dashboard</h1>
    <div class="sub" id="mode-label">HAWK v6 — Channel Breakout + EMA + ADX</div>
  </div>
  <div class="sub" id="last-update">Loading...</div>
</header>

<div class="container">
  <div class="refresh-note">Auto-refreshes every 60s</div>

  <!-- Guards -->
  <div class="guards" id="guards"></div>

  <!-- Stats cards -->
  <div class="cards" id="cards"></div>

  <!-- Reality check -->
  <div class="reality">
    <h2>Execution Reality Check — Live vs Backtest</h2>
    <div class="reality-grid" id="reality"></div>
  </div>

  <!-- Charts -->
  <div class="charts">
    <div class="chart-box">
      <h2>Equity Curve (USDT)</h2>
      <canvas id="eqChart" height="160"></canvas>
    </div>
    <div class="chart-box">
      <h2>Daily P&L (USDT)</h2>
      <canvas id="dailyChart" height="160"></canvas>
    </div>
  </div>

  <!-- Open positions -->
  <div class="table-box">
    <h2>Open Positions</h2>
    <div id="open-pos"></div>
  </div>

  <!-- Trade log -->
  <div class="table-box">
    <h2>Trade Log (last 50)</h2>
    <div id="trade-log"></div>
  </div>
</div>

<script>
let eqChart, dailyChart;

function fmt(v, decimals=2) {
  if (v === null || v === undefined) return '—';
  return Number(v).toFixed(decimals);
}
function sign(v) { return v >= 0 ? '+' : ''; }
function cls(v)  { return v >= 0 ? 'green' : 'red'; }

function render(data) {
  const s  = data.stats;
  const bt = data.backtest;

  document.getElementById('last-update').textContent = 'Generated: ' + s.now;

  // Guards
  const ddClass   = s.dd_pct < 15 ? 'ok' : s.dd_pct < 25 ? 'warn' : 'bad';
  const liqClass  = s.liqs === 0 ? 'ok' : s.liqs < 3 ? 'warn' : 'bad';
  const guards = [
    { label: `Drawdown: ${fmt(s.dd_pct,1)}% (limit 30%)`, cls: ddClass },
    { label: `Liquidations: ${s.liqs}`, cls: liqClass },
    { label: `1h Ticks: ${s.bar_count} | 4h Ticks: ${s.bar_count_4h}`, cls: 'ok' },
    { label: `Running: ${s.days} days since ${s.started}`, cls: 'ok' },
  ];
  document.getElementById('guards').innerHTML = guards.map(g =>
    `<div class="guard ${g.cls}"><span class="dot"></span>${g.label}</div>`
  ).join('');

  // Cards
  const pfDisp = typeof s.profit_factor === 'number' ? fmt(s.profit_factor, 3) : s.profit_factor;
  const cards = [
    { label:'Total P&L',     value:`$${sign(s.total_pnl)}${fmt(s.total_pnl)}`, cls: cls(s.total_pnl), bt: `GBP ${fmt(s.equity_gbp,0)}` },
    { label:'Return',        value:`${sign(s.return_pct)}${fmt(s.return_pct,1)}%`, cls: cls(s.return_pct), bt: `~${fmt(s.monthly_pct,1)}%/mo` },
    { label:'Win Rate',      value:`${fmt(s.win_rate,1)}%`, cls: 'blue', bt: `BT: ${bt.win_rate}%` },
    { label:'Trades',        value:`${s.closed}`, cls: 'white', bt: `${s.wins}W · ${s.losses}L` },
    { label:'Profit Factor', value: pfDisp, cls: s.profit_factor >= 1 ? 'green' : 'red', bt: 'Gross P / Gross L' },
    { label:'Avg Win',       value:`$${sign(s.avg_win)}${fmt(s.avg_win)}`, cls: 'green', bt: `BT: $${fmt(bt.avg_win)}` },
    { label:'Avg Loss',      value:`$${fmt(s.avg_loss)}`, cls: s.avg_loss < 0 ? 'red' : 'white', bt: `BT: $${bt.avg_loss}` },
    { label:'Max Drawdown',  value:`${fmt(s.dd_pct,1)}%`, cls: s.dd_pct > 20 ? 'red' : 'white', bt: `Consec. losses: ${s.max_consec_losses}` },
    { label:'Equity (USDT)', value:`$${fmt(s.equity)}`, cls: 'white', bt: `Peak: $${fmt(s.peak)}` },
    { label:'Funding Paid',  value:`$${fmt(s.funding_paid)}`, cls: 'yellow', bt: 'Total' },
  ];
  document.getElementById('cards').innerHTML = cards.map(c =>
    `<div class="card">
       <div class="label">${c.label}</div>
       <div class="value ${c.cls}">${c.value}</div>
       <div class="bt">${c.bt}</div>
     </div>`
  ).join('');

  // Reality check
  const reality = [
    { label:'WIN RATE',         bt:`BT: ${bt.win_rate}%`,         live: `${fmt(s.win_rate,1)}%`,           cls: cls(s.win_rate - bt.win_rate) },
    { label:'AVG WIN',          bt:`BT: $${fmt(bt.avg_win)}`,     live: `$${sign(s.avg_win)}${fmt(s.avg_win)}`, cls: cls(s.avg_win - bt.avg_win) },
    { label:'AVG LOSS',         bt:`BT: $${bt.avg_loss}`,         live: `$${fmt(s.avg_loss)}`,              cls: cls(-(s.avg_loss - bt.avg_loss)) },
    { label:'CONSEC. LOSSES',   bt:`BT max: ${bt.max_consec_losses}`, live: `${s.max_consec_losses}`,      cls: s.max_consec_losses <= bt.max_consec_losses ? 'green' : 'red' },
    { label:'MONTHLY RETURN',   bt:`BT: ${bt.monthly_pct}%/mo`,   live: `${sign(s.monthly_pct)}${fmt(s.monthly_pct,2)}%`, cls: cls(s.monthly_pct) },
    { label:'LIQUIDATIONS',     bt:`BT: 0`,                        live: `${s.liqs}`,                       cls: s.liqs === 0 ? 'green' : 'red' },
  ];
  document.getElementById('reality').innerHTML = reality.map(r =>
    `<div class="rc-item">
       <div class="rc-label">${r.label}</div>
       <div class="rc-bt">${r.bt}</div>
       <div class="rc-live ${r.cls}">${r.live}</div>
     </div>`
  ).join('');

  // Equity curve
  const eqLabels = s.eq_curve.map(p => p.ts);
  const eqValues = s.eq_curve.map(p => p.equity);
  if (eqLabels.length === 0) { eqLabels.push('Now'); eqValues.push(s.equity); }
  if (eqChart) eqChart.destroy();
  eqChart = new Chart(document.getElementById('eqChart'), {
    type: 'line',
    data: {
      labels: eqLabels,
      datasets: [{
        data: eqValues,
        borderColor: '#58a6ff',
        backgroundColor: 'rgba(88,166,255,0.08)',
        fill: true,
        tension: 0.3,
        pointRadius: eqLabels.length > 50 ? 0 : 3,
        borderWidth: 2,
      }]
    },
    options: {
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color:'#8b949e', maxTicksLimit:6, maxRotation:0 }, grid: { color:'#21262d' } },
        y: { ticks: { color:'#8b949e', callback: v => '$'+v.toFixed(0) }, grid: { color:'#21262d' } },
      }
    }
  });

  // Daily P&L
  const days    = Object.keys(s.daily_pnl);
  const dayVals = Object.values(s.daily_pnl);
  if (dailyChart) dailyChart.destroy();
  dailyChart = new Chart(document.getElementById('dailyChart'), {
    type: 'bar',
    data: {
      labels: days,
      datasets: [{
        data: dayVals,
        backgroundColor: dayVals.map(v => v >= 0 ? 'rgba(63,185,80,0.7)' : 'rgba(248,81,73,0.7)'),
        borderRadius: 4,
      }]
    },
    options: {
      plugins: { legend: { display:false } },
      scales: {
        x: { ticks: { color:'#8b949e' }, grid: { color:'#21262d' } },
        y: { ticks: { color:'#8b949e', callback: v => '$'+v.toFixed(0) }, grid: { color:'#21262d' } },
      }
    }
  });

  // Open positions
  const pos = data.positions;
  if (!pos || pos.length === 0) {
    document.getElementById('open-pos').innerHTML = '<div class="empty">No open positions</div>';
  } else {
    document.getElementById('open-pos').innerHTML = `
      <table>
        <thead><tr><th>Symbol</th><th>TF</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th><th>Qty</th><th>Notional</th><th>Opened</th></tr></thead>
        <tbody>${pos.map(p => `
          <tr>
            <td>${p.symbol}</td>
            <td>${p.tf || '1h'}</td>
            <td><span class="badge ${p.side}">${p.side.toUpperCase()}</span></td>
            <td>$${fmt(p.entry, 4)}</td>
            <td class="red">$${fmt(p.sl, 4)}</td>
            <td class="green">$${fmt(p.tp, 4)}</td>
            <td>${fmt(p.qty, 4)}</td>
            <td>$${fmt(p.notional, 0)}</td>
            <td>${(p.ts_open || '').slice(0,16)}</td>
          </tr>`).join('')}
        </tbody>
      </table>`;
  }

  // Trade log
  const trades = data.trades;
  if (!trades || trades.length === 0) {
    document.getElementById('trade-log').innerHTML = '<div class="empty">No trades yet</div>';
  } else {
    document.getElementById('trade-log').innerHTML = `
      <table>
        <thead><tr><th>Entry</th><th>Exit</th><th>Symbol</th><th>TF</th><th>Side</th><th>Entry $</th><th>Exit $</th><th>P&L</th><th>Reason</th></tr></thead>
        <tbody>${trades.map(t => {
          const pnl = parseFloat(t.pnl_usdt || 0);
          return `<tr>
            <td>${(t.ts_open || '').slice(0,16)}</td>
            <td>${(t.ts_close || '').slice(0,16)}</td>
            <td>${t.symbol || ''}</td>
            <td>${t.tf || '1h'}</td>
            <td><span class="badge ${t.side}">${(t.side||'').toUpperCase()}</span></td>
            <td>$${fmt(t.entry, 4)}</td>
            <td>$${fmt(t.exit, 4)}</td>
            <td class="${cls(pnl)}">${sign(pnl)}$${fmt(pnl)}</td>
            <td><span class="badge ${t.reason}">${(t.reason||'').toUpperCase()}</span></td>
          </tr>`;
        }).join('')}
        </tbody>
      </table>`;
  }
}

async function refresh() {
  try {
    const res  = await fetch('/api/data');
    const data = await res.json();
    render(data);
  } catch(e) {
    console.error('Refresh failed:', e);
  }
}

refresh();
setInterval(refresh, 60000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML)


# ─────────────────────────────────────────────────────────────────────────── #
#  Main                                                                         #
# ─────────────────────────────────────────────────────────────────────────── #

_PORTFOLIO_DEFAULTS = {
    "conservative": ("logs/hawk_state_conservative.json", "logs/hawk_trades_conservative.csv", 5000),
    "optimal":      ("logs/hawk_state_optimal.json",      "logs/hawk_trades_optimal.csv",      5002),
}


def main():
    global STATE_FILE, TRADE_LOG

    parser = argparse.ArgumentParser(
        description="HAWK Trading Dashboard",
        epilog=(
            "Portfolio shortcuts:\n"
            "  --portfolio conservative   # port 5000\n"
            "  --portfolio optimal        # port 5002\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--portfolio", choices=list(_PORTFOLIO_DEFAULTS.keys()),
                        help="Shortcut: auto-fills --state, --trades, --port")
    parser.add_argument("--state",  default=None)
    parser.add_argument("--trades", default=None)
    parser.add_argument("--port",   type=int, default=None)
    parser.add_argument("--host",   default="127.0.0.1")
    args = parser.parse_args()

    if args.portfolio:
        def_state, def_trades, def_port = _PORTFOLIO_DEFAULTS[args.portfolio]
        STATE_FILE = args.state  or def_state
        TRADE_LOG  = args.trades or def_trades
        port       = args.port   or def_port
    else:
        STATE_FILE = args.state  or "logs/hawk_state.json"
        TRADE_LOG  = args.trades or "logs/hawk_trades.csv"
        port       = args.port   or 5000

    print(f"\n  HAWK Dashboard running at http://{args.host}:{port}")
    print(f"  State file : {STATE_FILE}")
    print(f"  Trade log  : {TRADE_LOG}\n")

    app.run(host=args.host, port=port, debug=False)


if __name__ == "__main__":
    main()
