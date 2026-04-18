"""
HAWK Comparator Dashboard
==========================
Single Flask page showing all 4 portfolios side-by-side.
Combines the terminal comparator with the single-portfolio dashboard UI.

  conservative       — baseline 10x
  optimal            — baseline mixed leverage
  conservative_vol   — 10x + vol filter on 1h (A/B)
  optimal_vol        — mixed leverage + vol filter on 1h (A/B)

Usage:
  python scripts/hawk_comparator_dashboard.py          # port 5010
  python scripts/hawk_comparator_dashboard.py --port 8080

Open browser at: http://localhost:5010
Auto-refreshes every 30s.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

LOGS_DIR    = os.path.join(os.path.dirname(__file__), "..", "logs")
GBP_TO_USDT = 1.27

PORTFOLIOS = [
    dict(key="conservative",     label="Conservative",     short="Consv",
         state="hawk_state_conservative.json",     trades="hawk_trades_conservative.csv",
         color="#58a6ff"),
    dict(key="optimal",          label="Optimal",          short="Optim",
         state="hawk_state_optimal.json",          trades="hawk_trades_optimal.csv",
         color="#3fb950"),
    dict(key="conservative_vol", label="Conservative+Vol", short="Consv+V",
         state="hawk_state_conservative_vol.json", trades="hawk_trades_conservative_vol.csv",
         color="#e3b341"),
    dict(key="optimal_vol",      label="Optimal+Vol",      short="Optim+V",
         state="hawk_state_optimal_vol.json",      trades="hawk_trades_optimal_vol.csv",
         color="#f78166"),
]

# Backtest reference numbers per portfolio
BACKTEST = {
    "conservative":     {"monthly_pct": 14.54, "win_rate": 36.6, "rr": 2.54},
    "optimal":          {"monthly_pct": 20.44, "win_rate": 31.1, "rr": 5.26},
    "conservative_vol": {"monthly_pct":  4.17, "win_rate": 35.4, "rr": 1.08},
    "optimal_vol":      {"monthly_pct":  7.94, "win_rate": 31.4, "rr": 1.22},
}


# ─────────────────────────────────────────────────────────────────────────── #
#  Data helpers                                                                 #
# ─────────────────────────────────────────────────────────────────────────── #

def _load_json(fname: str) -> dict:
    path = os.path.join(LOGS_DIR, fname)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _load_trades(fname: str) -> list[dict]:
    path = os.path.join(LOGS_DIR, fname)
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _stats(state: dict, trades: list[dict]) -> dict:
    equity       = state.get("equity", 0.0)
    peak         = state.get("peak_equity", equity) or equity or 1
    total_pnl    = state.get("total_pnl", 0.0)
    initial_usdt = equity - total_pnl if equity else 635.0
    closed       = state.get("closed_trades", 0)
    wins         = state.get("wins", 0)
    losses       = closed - wins
    win_rate     = wins / closed * 100 if closed else 0.0
    dd_pct       = (1 - equity / peak) * 100 if peak else 0.0
    return_pct   = (equity - initial_usdt) / initial_usdt * 100 if initial_usdt else 0.0

    pnls      = [float(t["pnl_usdt"]) for t in trades if t.get("pnl_usdt")]
    wins_usdt = [p for p in pnls if p > 0]
    loss_usdt = [p for p in pnls if p < 0]
    avg_win   = sum(wins_usdt) / len(wins_usdt) if wins_usdt else 0.0
    avg_loss  = sum(loss_usdt) / len(loss_usdt) if loss_usdt else 0.0
    gross_p   = sum(wins_usdt)
    gross_l   = abs(sum(loss_usdt))
    pf        = gross_p / gross_l if gross_l else 0.0
    rr        = gross_p / gross_l if gross_l else 0.0

    # Equity curve
    eq_curve: list[dict] = []
    running = initial_usdt
    for t in trades:
        if t.get("pnl_usdt") and t.get("ts_close"):
            running += float(t["pnl_usdt"])
            eq_curve.append({"ts": t["ts_close"][:16], "eq": round(running, 2)})

    # Daily P&L
    daily: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.get("ts_close") and t.get("pnl_usdt"):
            daily[t["ts_close"][:10]] += float(t["pnl_usdt"])

    # Monthly return estimate
    days = 0
    monthly_pct = 0.0
    started = state.get("started", "")
    if started:
        try:
            start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            days = max(1, (datetime.now(timezone.utc) - start_dt).days)
            if equity > 0 and initial_usdt > 0:
                monthly_pct = (((equity / initial_usdt) ** (30 / days)) - 1) * 100
        except Exception:
            pass

    # Max consec losses
    max_consec = consec = 0
    for p in pnls:
        if p < 0:
            consec += 1; max_consec = max(max_consec, consec)
        else:
            consec = 0

    return dict(
        equity=round(equity, 2),
        equity_gbp=round(equity / GBP_TO_USDT, 2),
        initial_usdt=round(initial_usdt, 2),
        peak=round(peak, 2),
        total_pnl=round(total_pnl, 2),
        return_pct=round(return_pct, 2),
        dd_pct=round(dd_pct, 2),
        closed=closed, wins=wins, losses=losses,
        win_rate=round(win_rate, 1),
        avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2),
        profit_factor=round(pf, 3),
        rr=round(rr, 3),
        max_consec=max_consec,
        funding_paid=round(state.get("funding_paid", 0.0), 2),
        liqs=state.get("liqs", 0),
        open_pos=len(state.get("positions", [])),
        positions=state.get("positions", []),
        bar_count=state.get("bar_count", 0),
        bar_count_4h=state.get("bar_count_4h", 0),
        monthly_pct=round(monthly_pct, 2),
        days=days,
        started=started[:10] if started else "—",
        eq_curve=eq_curve,
        daily_pnl=dict(sorted(daily.items())),
        running=equity > 0,
    )


# ─────────────────────────────────────────────────────────────────────────── #
#  API                                                                          #
# ─────────────────────────────────────────────────────────────────────────── #

@app.route("/api/data")
def api_data():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    result = {"now": now, "portfolios": {}}
    for pf in PORTFOLIOS:
        state  = _load_json(pf["state"])
        trades = _load_trades(pf["trades"])
        result["portfolios"][pf["key"]] = {
            "label":    pf["label"],
            "short":    pf["short"],
            "color":    pf["color"],
            "backtest": BACKTEST[pf["key"]],
            "stats":    _stats(state, trades),
            "trades":   trades[-30:][::-1],
        }
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────── #
#  HTML                                                                         #
# ─────────────────────────────────────────────────────────────────────────── #

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HAWK Comparator Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
* { box-sizing:border-box; margin:0; padding:0; }
body { background:#0d1117; color:#e6edf3; font-family:'Segoe UI',system-ui,sans-serif; font-size:14px; }
header { padding:14px 24px; border-bottom:1px solid #21262d; display:flex; justify-content:space-between; align-items:center; }
header h1 { font-size:17px; font-weight:600; color:#58a6ff; }
.sub { font-size:12px; color:#8b949e; }
.container { padding:18px 24px; max-width:1600px; margin:0 auto; }
.refresh-note { font-size:11px; color:#8b949e; text-align:right; margin-bottom:12px; }

/* Portfolio cards grid */
.pf-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:18px; }
.pf-card { background:#161b22; border:1px solid #21262d; border-radius:8px; padding:14px 16px; border-top:3px solid var(--accent); }
.pf-card h2 { font-size:13px; font-weight:600; color:var(--accent); margin-bottom:10px; }
.pf-card .winner-badge { display:inline-block; background:#0d2b1a; color:#3fb950; border:1px solid #238636; border-radius:10px; font-size:10px; font-weight:700; padding:1px 8px; margin-left:6px; vertical-align:middle; }
.metric-row { display:flex; justify-content:space-between; align-items:baseline; padding:3px 0; border-bottom:1px solid #21262d11; }
.metric-row:last-child { border-bottom:none; }
.metric-label { font-size:11px; color:#8b949e; }
.metric-val   { font-size:13px; font-weight:600; }
.metric-bt    { font-size:10px; color:#8b949e; margin-top:1px; }
.not-running  { color:#8b949e; font-style:italic; font-size:12px; margin-top:8px; }

/* Colors */
.green { color:#3fb950; } .red { color:#f85149; } .blue { color:#58a6ff; }
.yellow{ color:#e3b341; } .white{ color:#e6edf3; } .grey { color:#8b949e; }

/* Charts row */
.charts-row { display:grid; grid-template-columns:2fr 1fr; gap:14px; margin-bottom:18px; }
.chart-box { background:#161b22; border:1px solid #21262d; border-radius:8px; padding:16px; }
.chart-box h2 { font-size:11px; color:#8b949e; text-transform:uppercase; letter-spacing:.5px; margin-bottom:10px; }

/* Insights */
.insights { background:#161b22; border:1px solid #21262d; border-radius:8px; padding:14px 18px; margin-bottom:18px; }
.insights h2 { font-size:11px; color:#8b949e; text-transform:uppercase; letter-spacing:.5px; margin-bottom:10px; }
.insight-pair { display:flex; gap:24px; flex-wrap:wrap; margin-bottom:6px; }
.insight-item { font-size:13px; }

/* Tabs */
.tabs { display:flex; gap:6px; margin-bottom:12px; flex-wrap:wrap; }
.tab  { padding:5px 14px; border-radius:4px; font-size:12px; cursor:pointer; border:1px solid #21262d; background:#161b22; color:#8b949e; }
.tab.active { background:#21262d; color:#e6edf3; border-color:#58a6ff44; }

/* Table */
.table-box { background:#161b22; border:1px solid #21262d; border-radius:8px; padding:14px 16px; margin-bottom:18px; }
.table-box h2 { font-size:11px; color:#8b949e; text-transform:uppercase; letter-spacing:.5px; margin-bottom:10px; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th { color:#8b949e; text-align:left; padding:5px 8px; border-bottom:1px solid #21262d; font-weight:500; }
td { padding:6px 8px; border-bottom:1px solid #21262d22; }
tr:hover td { background:#1c2128; }
.badge { padding:2px 7px; border-radius:10px; font-size:10px; font-weight:600; }
.badge.long    { background:#0d2b1a; color:#3fb950; }
.badge.short   { background:#2b0d0d; color:#f85149; }
.badge.tp      { background:#0d1f3a; color:#58a6ff; }
.badge.sl      { background:#2b1a0d; color:#e3b341; }
.badge.timeout { background:#1c2128; color:#8b949e; }
.badge.liq     { background:#3b0d0d; color:#f85149; }
.empty { text-align:center; color:#8b949e; padding:16px; }

@media(max-width:1100px) { .pf-grid { grid-template-columns:repeat(2,1fr); } }
@media(max-width:700px)  { .pf-grid { grid-template-columns:1fr; } .charts-row { grid-template-columns:1fr; } }
</style>
</head>
<body>

<header>
  <div>
    <h1>HAWK Comparator Dashboard</h1>
    <div class="sub">Conservative · Optimal · +Vol variants — side-by-side</div>
  </div>
  <div class="sub" id="last-update">Loading...</div>
</header>

<div class="container">
  <div class="refresh-note">Auto-refreshes every 30s</div>

  <!-- 4 portfolio cards -->
  <div class="pf-grid" id="pf-grid"></div>

  <!-- Charts -->
  <div class="charts-row">
    <div class="chart-box">
      <h2>Equity Curves (USDT) — all portfolios</h2>
      <canvas id="eqChart" height="140"></canvas>
    </div>
    <div class="chart-box">
      <h2>Daily P&amp;L — combined</h2>
      <canvas id="dailyChart" height="140"></canvas>
    </div>
  </div>

  <!-- Insights -->
  <div class="insights" id="insights"></div>

  <!-- Open positions -->
  <div class="table-box">
    <h2>Open Positions</h2>
    <div class="tabs" id="pos-tabs"></div>
    <div id="pos-body"></div>
  </div>

  <!-- Trade log -->
  <div class="table-box">
    <h2>Trade Log (last 30 per portfolio)</h2>
    <div class="tabs" id="trade-tabs"></div>
    <div id="trade-body"></div>
  </div>
</div>

<script>
let eqChart, dailyChart;
let activePosTab   = null;
let activeTradeTab = null;
let lastData       = null;

const PF_KEYS = ['conservative','optimal','conservative_vol','optimal_vol'];

function fmt(v, d=2) { return v == null ? '—' : Number(v).toFixed(d); }
function sign(v)     { return v >= 0 ? '+' : ''; }
function cls(v)      { return v >= 0 ? 'green' : 'red'; }
function pct(v,d=1)  { return `${sign(v)}${fmt(v,d)}%`; }

function renderCards(data) {
  const pfdata = data.portfolios;
  // Find best equity
  const equities = PF_KEYS.map(k => pfdata[k]?.stats?.equity || 0);
  const maxEq    = Math.max(...equities);

  const html = PF_KEYS.map(key => {
    const pf = pfdata[key];
    if (!pf) return '';
    const s  = pf.stats;
    const bt = pf.backtest;
    const isWinner = s.equity > 0 && s.equity === maxEq && equities.filter(e=>e>0).length > 1;
    const running  = s.running;

    if (!running) {
      return `<div class="pf-card" style="--accent:${pf.color}">
        <h2>${pf.label}</h2>
        <div class="not-running">Not started — run the bot first</div>
        <div class="sub" style="margin-top:8px">BT: ${sign(bt.monthly_pct)}${fmt(bt.monthly_pct)}%/mo</div>
      </div>`;
    }

    return `<div class="pf-card" style="--accent:${pf.color}">
      <h2>${pf.label}${isWinner ? '<span class="winner-badge">★ LEADING</span>' : ''}</h2>
      <div class="metric-row"><span class="metric-label">Equity</span>
        <div style="text-align:right">
          <div class="metric-val ${cls(s.total_pnl)}">$${fmt(s.equity)}</div>
          <div class="metric-bt">£${fmt(s.equity_gbp)}</div>
        </div></div>
      <div class="metric-row"><span class="metric-label">Total P&L</span>
        <div style="text-align:right">
          <div class="metric-val ${cls(s.total_pnl)}">${sign(s.total_pnl)}$${fmt(s.total_pnl)}</div>
          <div class="metric-bt">${pct(s.return_pct)} return</div>
        </div></div>
      <div class="metric-row"><span class="metric-label">Monthly est.</span>
        <div style="text-align:right">
          <div class="metric-val ${cls(s.monthly_pct)}">${pct(s.monthly_pct)}</div>
          <div class="metric-bt">BT: ${sign(bt.monthly_pct)}${fmt(bt.monthly_pct)}%</div>
        </div></div>
      <div class="metric-row"><span class="metric-label">Win rate</span>
        <div style="text-align:right">
          <div class="metric-val blue">${fmt(s.win_rate,1)}%</div>
          <div class="metric-bt">BT: ${fmt(bt.win_rate,1)}%</div>
        </div></div>
      <div class="metric-row"><span class="metric-label">Actual RR</span>
        <div style="text-align:right">
          <div class="metric-val white">${fmt(s.rr,2)}</div>
          <div class="metric-bt">BT: ${fmt(bt.rr,2)}</div>
        </div></div>
      <div class="metric-row"><span class="metric-label">Trades</span>
        <span class="metric-val white">${s.closed} <span class="sub">(${s.wins}W·${s.losses}L)</span></span></div>
      <div class="metric-row"><span class="metric-label">Drawdown</span>
        <span class="metric-val ${s.dd_pct > 20 ? 'red' : s.dd_pct > 12 ? 'yellow' : 'white'}">${fmt(s.dd_pct,1)}%</span></div>
      <div class="metric-row"><span class="metric-label">Open positions</span>
        <span class="metric-val white">${s.open_pos}</span></div>
      <div class="metric-row"><span class="metric-label">Liqs / Funding</span>
        <span class="metric-val ${s.liqs > 0 ? 'red' : 'white'}">${s.liqs} / $${fmt(s.funding_paid)}</span></div>
      <div class="metric-row"><span class="metric-label">Running</span>
        <span class="metric-val white">${s.days}d since ${s.started}</span></div>
    </div>`;
  }).join('');
  document.getElementById('pf-grid').innerHTML = html;
}

function renderCharts(data) {
  const pfdata = data.portfolios;

  // Equity curves — one dataset per portfolio
  const datasets = PF_KEYS.map(key => {
    const pf = pfdata[key];
    if (!pf || !pf.stats.running) return null;
    return {
      label: pf.short,
      data: pf.stats.eq_curve.map(p => ({ x: p.ts, y: p.eq })),
      borderColor: pf.color,
      backgroundColor: 'transparent',
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.3,
    };
  }).filter(Boolean);

  if (eqChart) eqChart.destroy();
  eqChart = new Chart(document.getElementById('eqChart'), {
    type: 'line',
    data: { datasets },
    options: {
      parsing: false,
      plugins: { legend: { labels: { color:'#8b949e', boxWidth:12, font:{size:11} } } },
      scales: {
        x: { type:'category', ticks:{ color:'#8b949e', maxTicksLimit:6, maxRotation:0 }, grid:{ color:'#21262d' } },
        y: { ticks:{ color:'#8b949e', callback: v => '$'+v.toFixed(0) }, grid:{ color:'#21262d' } },
      }
    }
  });

  // Combined daily P&L (all portfolios summed)
  const allDays: { [k:string]: number } = {};
  PF_KEYS.forEach(key => {
    const pf = pfdata[key];
    if (!pf || !pf.stats.running) return;
    Object.entries(pf.stats.daily_pnl).forEach(([day, v]) => {
      allDays[day] = (allDays[day] || 0) + (v as number);
    });
  });
  const sortedDays = Object.keys(allDays).sort();
  const dayVals    = sortedDays.map(d => allDays[d]);

  if (dailyChart) dailyChart.destroy();
  dailyChart = new Chart(document.getElementById('dailyChart'), {
    type: 'bar',
    data: {
      labels: sortedDays,
      datasets: [{ data: dayVals,
        backgroundColor: dayVals.map(v => v >= 0 ? 'rgba(63,185,80,0.7)' : 'rgba(248,81,73,0.7)'),
        borderRadius: 3 }]
    },
    options: {
      plugins: { legend:{ display:false } },
      scales: {
        x: { ticks:{ color:'#8b949e', maxTicksLimit:8 }, grid:{ color:'#21262d' } },
        y: { ticks:{ color:'#8b949e', callback: v => '$'+v.toFixed(0) }, grid:{ color:'#21262d' } },
      }
    }
  });
}

function renderInsights(data) {
  const pfdata = data.portfolios;
  let html = '<h2>Insights — vol filter vs baseline</h2><div class="insight-pair">';

  const pairs = [
    ['conservative', 'conservative_vol', 'Conservative'],
    ['optimal',      'optimal_vol',      'Optimal'],
  ];

  pairs.forEach(([baseKey, volKey, name]) => {
    const base = pfdata[baseKey];
    const vol  = pfdata[volKey];
    if (!base?.stats?.running || !vol?.stats?.running) {
      html += `<div class="insight-item grey">${name}: start both bots to compare</div>`;
      return;
    }
    const bs = base.stats, vs = vol.stats;
    const eqDelta  = vs.equity - bs.equity;
    const pnlDelta = vs.total_pnl - bs.total_pnl;
    const ahead    = eqDelta >= 0;
    const winner   = ahead ? volKey.replace('_',' ') : baseKey;
    html += `<div class="insight-item">
      <strong style="color:${ahead ? '#3fb950' : '#f85149'}">${name} vol filter is ${ahead ? '▲ AHEAD' : '▼ BEHIND'}</strong>
      by <strong>$${Math.abs(eqDelta).toFixed(2)}</strong> equity
      (PnL delta: <span class="${cls(pnlDelta)}">${sign(pnlDelta)}$${Math.abs(pnlDelta).toFixed(2)}</span>)
      &nbsp;·&nbsp; WR: ${fmt(bs.win_rate,1)}% → ${fmt(vs.win_rate,1)}%
      &nbsp;·&nbsp; RR: ${fmt(bs.rr,2)} → ${fmt(vs.rr,2)}
    </div>`;
  });

  html += '</div>';
  document.getElementById('insights').innerHTML = html;
}

function renderTabs(containerId, bodyId, tabKey, data, renderFn) {
  const pfdata = data.portfolios;
  const running = PF_KEYS.filter(k => pfdata[k]?.stats?.running);
  if (running.length === 0) {
    document.getElementById(containerId).innerHTML = '';
    document.getElementById(bodyId).innerHTML = '<div class="empty">No portfolios running yet</div>';
    return;
  }
  if (!window[tabKey] || !running.includes(window[tabKey])) {
    window[tabKey] = running[0];
  }
  document.getElementById(containerId).innerHTML = running.map(k =>
    `<div class="tab ${k === window[tabKey] ? 'active' : ''}"
          style="${k === window[tabKey] ? 'border-color:' + pfdata[k].color + '88' : ''}"
          onclick="window['${tabKey}']='${k}';renderBody()">
      ${pfdata[k].short}
    </div>`
  ).join('');
  renderFn(document.getElementById(bodyId), pfdata[window[tabKey]]);
}

function renderPosBody(el, pf) {
  const pos = pf?.stats?.positions || [];
  if (pos.length === 0) { el.innerHTML = '<div class="empty">No open positions</div>'; return; }
  el.innerHTML = `<table>
    <thead><tr><th>Symbol</th><th>TF</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th><th>Qty</th><th>Notional</th><th>Opened</th></tr></thead>
    <tbody>${pos.map(p => `<tr>
      <td>${p.symbol||''}</td><td>${p.tf||'1h'}</td>
      <td><span class="badge ${p.side}">${(p.side||'').toUpperCase()}</span></td>
      <td>$${fmt(p.entry,4)}</td><td class="red">$${fmt(p.sl,4)}</td><td class="green">$${fmt(p.tp,4)}</td>
      <td>${fmt(p.qty,4)}</td><td>$${fmt(p.notional,0)}</td>
      <td>${(p.ts_open||'').slice(0,16)}</td>
    </tr>`).join('')}</tbody></table>`;
}

function renderTradeBody(el, pf) {
  const trades = pf?.trades || [];
  if (trades.length === 0) { el.innerHTML = '<div class="empty">No trades yet</div>'; return; }
  el.innerHTML = `<table>
    <thead><tr><th>Entry</th><th>Exit</th><th>Symbol</th><th>TF</th><th>Side</th><th>Entry $</th><th>Exit $</th><th>P&L</th><th>Reason</th></tr></thead>
    <tbody>${trades.map(t => {
      const pnl = parseFloat(t.pnl_usdt||0);
      return `<tr>
        <td>${(t.ts_open||'').slice(0,16)}</td><td>${(t.ts_close||'').slice(0,16)}</td>
        <td>${t.symbol||''}</td><td>${t.tf||'1h'}</td>
        <td><span class="badge ${t.side}">${(t.side||'').toUpperCase()}</span></td>
        <td>$${fmt(t.entry,4)}</td><td>$${fmt(t.exit,4)}</td>
        <td class="${cls(pnl)}">${sign(pnl)}$${fmt(Math.abs(pnl))}</td>
        <td><span class="badge ${t.reason}">${(t.reason||'').toUpperCase()}</span></td>
      </tr>`;
    }).join('')}</tbody></table>`;
}

function renderBody() {
  if (!lastData) return;
  renderTabs('pos-tabs',   'pos-body',   'activePosTab',   lastData, renderPosBody);
  renderTabs('trade-tabs', 'trade-body', 'activeTradeTab', lastData, renderTradeBody);
}

async function refresh() {
  try {
    const res  = await fetch('/api/data');
    lastData   = await res.json();
    document.getElementById('last-update').textContent = 'Updated: ' + lastData.now;
    renderCards(lastData);
    renderCharts(lastData);
    renderInsights(lastData);
    renderBody();
  } catch(e) {
    console.error('Refresh failed:', e);
  }
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML)


def main() -> None:
    parser = argparse.ArgumentParser(description="HAWK Comparator Dashboard")
    parser.add_argument("--port", type=int, default=5010)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    print(f"\n  HAWK Comparator Dashboard → http://{args.host}:{args.port}")
    print("  Shows: conservative · optimal · conservative_vol · optimal_vol")
    print("  Auto-refreshes every 30s. Ctrl+C to stop.\n")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
