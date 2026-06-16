"""
Flask web dashboard — localhost:7433.
Four views: ratio trend, value distribution, waste×value scatter, survival stat.
Single embedded template, no external files needed.
"""
from __future__ import annotations

import threading
import webbrowser
from typing import Any

from flask import Flask, jsonify

from . import db as _db

PORT = 7433

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>promptiq dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0c0c0c;
    color: #d4d4d4;
    font-family: 'SF Mono', 'Menlo', 'Consolas', monospace;
    font-size: 13px;
    padding: 28px 32px;
    min-height: 100vh;
  }
  header {
    display: flex;
    align-items: baseline;
    gap: 20px;
    margin-bottom: 28px;
    border-bottom: 1px solid #1e1e1e;
    padding-bottom: 16px;
    flex-wrap: wrap;
  }
  header h1 { font-size: 13px; letter-spacing: 3px; color: #555; text-transform: uppercase; font-weight: 400; }
  .stat { color: #666; }
  .stat span { color: #e0e0e0; }
  .stat .hi { color: #22c55e; }
  .stat .warn { color: #f59e0b; }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
  }
  .card {
    background: #111;
    border: 1px solid #1e1e1e;
    border-radius: 6px;
    padding: 20px 22px;
  }
  .card.wide { grid-column: 1 / -1; }
  .card h2 { font-size: 10px; letter-spacing: 2px; text-transform: uppercase; color: #444; margin-bottom: 4px; font-weight: 400; }
  .card .sub { font-size: 11px; color: #333; margin-bottom: 16px; }
  .survival-stat {
    display: flex;
    align-items: baseline;
    gap: 12px;
    padding: 24px 0 8px;
  }
  .survival-num { font-size: 48px; font-weight: 300; color: #22c55e; letter-spacing: -2px; }
  .survival-label { font-size: 12px; color: #555; line-height: 1.6; }
  .survival-na { font-size: 14px; color: #333; padding: 24px 0; }
  .empty { color: #333; padding: 32px 0; text-align: center; font-size: 12px; }
  canvas { display: block; }
</style>
</head>
<body>
<header>
  <h1>promptiq</h1>
  <span id="hSessions" class="stat"></span>
  <span id="hCost" class="stat"></span>
  <span id="hWaste" class="stat"></span>
  <span id="hRatio" class="stat"></span>
</header>

<div class="grid">
  <!-- Headline: ratio over time -->
  <div class="card wide">
    <h2>Impact / Cost Ratio</h2>
    <div class="sub">value produced per dollar spent — are you getting better?</div>
    <canvas id="ratioChart" height="75"></canvas>
    <div id="ratioEmpty" class="empty" style="display:none">no git data yet — run promptiq inside a git repo</div>
  </div>

  <!-- Value score distribution -->
  <div class="card">
    <h2>Value Score Distribution</h2>
    <div class="sub">where do your sessions cluster?</div>
    <canvas id="distChart" height="180"></canvas>
    <div id="distEmpty" class="empty" style="display:none">no value data yet</div>
  </div>

  <!-- Waste × value scatter -->
  <div class="card">
    <h2>Waste vs Value</h2>
    <div class="sub">top-left = efficient · top-right = expensive but productive</div>
    <canvas id="scatterChart" height="180"></canvas>
    <div id="scatterEmpty" class="empty" style="display:none">no data yet</div>
  </div>

  <!-- Commit survival stat -->
  <div class="card wide">
    <h2>Commit Survival Rate</h2>
    <div class="sub">the most honest measure of output quality</div>
    <div id="survivalBlock"></div>
  </div>
</div>

<script>
const AMBER = '#f59e0b';
const GREEN = '#22c55e';
const BLUE  = '#3b82f6';
const RED   = '#ef4444';
const GRID  = '#1a1a1a';
const TICK  = '#444';

function baseOpts(extras = {}) {
  return {
    plugins: { legend: { display: false }, ...extras.plugins },
    scales: {
      x: { grid: { color: GRID }, ticks: { color: TICK }, ...((extras.scales||{}).x||{}) },
      y: { grid: { color: GRID }, ticks: { color: TICK }, ...((extras.scales||{}).y||{}) },
    },
    ...extras,
  };
}

async function load() {
  const [stats, ratio, dist, scatter, survival] = await Promise.all([
    fetch('/api/stats').then(r => r.json()),
    fetch('/api/ratio_trend').then(r => r.json()),
    fetch('/api/value_dist').then(r => r.json()),
    fetch('/api/waste_value').then(r => r.json()),
    fetch('/api/survival').then(r => r.json()),
  ]);

  // Header
  document.getElementById('hSessions').innerHTML =
    `<span>${stats.total_sessions}</span> sessions`;
  document.getElementById('hCost').innerHTML =
    `total <span>$${(stats.total_cost||0).toFixed(2)}</span>`;

  const wp = stats.total_cost > 0
    ? ((stats.wasted_cost||0) / stats.total_cost * 100).toFixed(1) : '0.0';
  document.getElementById('hWaste').innerHTML =
    `wasted <span class="warn">$${(stats.wasted_cost||0).toFixed(2)} (${wp}%)</span>`;

  if (stats.avg_ratio != null) {
    document.getElementById('hRatio').innerHTML =
      `avg ratio <span class="hi">${stats.avg_ratio}x</span>`;
  }

  // --- Ratio trend ---
  if (!ratio.length) {
    document.getElementById('ratioEmpty').style.display = 'block';
  } else {
    new Chart(document.getElementById('ratioChart'), {
      type: 'line',
      data: {
        labels: ratio.map(d => d.day),
        datasets: [{
          data: ratio.map(d => d.ratio),
          borderColor: GREEN,
          backgroundColor: 'rgba(34,197,94,0.07)',
          borderWidth: 2,
          pointRadius: 4,
          pointBackgroundColor: GREEN,
          tension: 0.3,
          fill: true,
        }],
      },
      options: baseOpts({
        scales: {
          x: { grid: { color: GRID }, ticks: { color: TICK, maxTicksLimit: 8 } },
          y: {
            grid: { color: GRID },
            ticks: { color: TICK, callback: v => v + 'x' },
            min: 0,
          },
        },
      }),
    });
  }

  // --- Value score distribution ---
  if (!dist.length) {
    document.getElementById('distEmpty').style.display = 'block';
  } else {
    new Chart(document.getElementById('distChart'), {
      type: 'bar',
      data: {
        labels: dist.map(d => d.bucket),
        datasets: [{
          data: dist.map(d => d.count),
          backgroundColor: BLUE + '99',
          borderColor: BLUE,
          borderWidth: 1,
          borderRadius: 3,
        }],
      },
      options: baseOpts({
        scales: {
          x: { grid: { color: GRID }, ticks: { color: TICK, maxRotation: 45 } },
          y: { grid: { color: GRID }, ticks: { color: TICK, stepSize: 1 } },
        },
      }),
    });
  }

  // --- Waste × value scatter ---
  if (!scatter.length) {
    document.getElementById('scatterEmpty').style.display = 'block';
  } else {
    new Chart(document.getElementById('scatterChart'), {
      type: 'scatter',
      data: {
        datasets: [{
          data: scatter.map(d => ({ x: d.waste_pct, y: d.value, label: d.project })),
          backgroundColor: AMBER + 'cc',
          pointRadius: 5,
          pointHoverRadius: 7,
        }],
      },
      options: baseOpts({
        plugins: {
          tooltip: {
            callbacks: {
              label: ctx => `${ctx.raw.label || 'session'}: waste ${ctx.parsed.x}% · value ${ctx.parsed.y}`,
            },
          },
        },
        scales: {
          x: {
            grid: { color: GRID },
            ticks: { color: TICK, callback: v => v + '%' },
            title: { display: true, text: 'wasted %', color: TICK, font: { size: 11 } },
            min: 0,
          },
          y: {
            grid: { color: GRID },
            ticks: { color: TICK },
            title: { display: true, text: 'value score', color: TICK, font: { size: 11 } },
            min: 0,
          },
        },
      }),
    });
  }

  // --- Survival stat ---
  const sb = document.getElementById('survivalBlock');
  if (survival.pct == null) {
    sb.innerHTML = '<div class="survival-na">not enough data — needs sessions older than 24h with git commits</div>';
  } else {
    const color = survival.pct >= 80 ? GREEN : survival.pct >= 50 ? AMBER : RED;
    sb.innerHTML = `
      <div class="survival-stat">
        <div class="survival-num" style="color:${color}">${survival.pct}%</div>
        <div class="survival-label">
          of your Claude Code commits are still in the codebase after 24h<br>
          <span style="color:#444">${survival.total} committed session${survival.total !== 1 ? 's' : ''} measured</span>
        </div>
      </div>`;
  }
}

load().catch(console.error);
</script>
</body>
</html>
"""


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["JSONIFY_SORT_KEYS"] = False

    @app.route("/")
    def index():
        return DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

    @app.route("/api/stats")
    def stats() -> Any:
        return jsonify(_db.get_summary_stats())

    @app.route("/api/ratio_trend")
    def ratio_trend() -> Any:
        return jsonify(_db.get_ratio_trend())

    @app.route("/api/value_dist")
    def value_dist() -> Any:
        return jsonify(_db.get_value_distribution())

    @app.route("/api/waste_value")
    def waste_value() -> Any:
        return jsonify(_db.get_waste_value_scatter())

    @app.route("/api/survival")
    def survival() -> Any:
        return jsonify(_db.get_survival_stat())

    return app


def run_dashboard() -> None:
    app = create_app()

    def _open():
        import time
        time.sleep(0.6)
        webbrowser.open(f"http://localhost:{PORT}")

    threading.Thread(target=_open, daemon=True).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
