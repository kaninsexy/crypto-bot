"""
dashboard/server.py — Auto-refresh web dashboard for the Crypto Bot.

Starts a local Flask server at http://localhost:5000
The page polls /api/status every 5 seconds and updates live.

Usage:
    # From crypto_bot/ with venv active:
    python -m dashboard.server

    # Or via run.sh:
    ./run.sh dashboard
"""

import os
import sys
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

# ── Allow running as  python -m dashboard.server  from crypto_bot/ ───────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, jsonify, Response
from dashboard.state import (
    read_paper_state,
    read_backtest_results,
    read_bot_status,
    is_bot_running,
)

# ── Live BTC price cache ───────────────────────────────────────────────────────
_price_cache: dict = {}
_price_lock = threading.Lock()


def _fetch_live_price() -> None:
    """Background thread: fetches BTC/USDT price every 30s and caches it."""
    while True:
        try:
            import ccxt
            exchange = ccxt.binance({"enableRateLimit": True})
            ticker = exchange.fetch_ticker("BTC/USDT")
            with _price_lock:
                _price_cache["BTC/USDT"] = {
                    "price": ticker["last"],
                    "change_pct": ticker.get("percentage", 0),
                    "high_24h":   ticker.get("high", 0),
                    "low_24h":    ticker.get("low", 0),
                    "volume_24h": ticker.get("quoteVolume", 0),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
        except Exception:
            pass
        import time
        time.sleep(30)


# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/api/status")
def api_status():
    paper    = read_paper_state()
    backtest = read_backtest_results()
    bot      = read_bot_status()
    running  = is_bot_running()

    # ── Profit reserve defaults ───────────────────────────────────────────
    # PortfolioManager.export_state() now writes reserve_balance,
    # earned_profit, and safe_withdrawal into paper_state.json.  Fall back
    # to 0.0 for stale / pre-reserve-system checkpoints so the dashboard
    # never chokes on missing keys.
    paper.setdefault("reserve_balance", 0.0)
    paper.setdefault("earned_profit",   0.0)
    paper.setdefault("safe_withdrawal", paper.get("reserve_balance", 0.0))

    with _price_lock:
        live_price = dict(_price_cache)

    return jsonify({
        "paper":      paper,
        "backtest":   backtest,
        "bot":        {**bot, "is_running": running},
        "live_price": live_price,
        "server_time": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/trades")
def api_trades():
    """Return persistent trade history from SQLite."""
    try:
        from dashboard.trade_logger import get_all_trades, get_strategy_stats, get_equity_curve
        strategy = __import__("flask").request.args.get("strategy")
        trades   = get_all_trades(limit=1000, strategy=strategy or None)
        stats    = get_strategy_stats()
        curve    = get_equity_curve(strategy=strategy or None)
        return jsonify({"trades": trades, "stats": stats, "curve": curve, "ok": True})
    except Exception as e:
        return jsonify({"trades": [], "stats": [], "curve": [], "ok": False, "error": str(e)})


@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")


# ── Dashboard HTML ────────────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Crypto Bot Dashboard</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  :root{
    --bg:#0d1117;--surface:#161b22;--surface2:#21262d;
    --border:#30363d;--text:#e6edf3;--muted:#8b949e;
    --green:#3fb950;--red:#f85149;--yellow:#d29922;--blue:#58a6ff;--purple:#bc8cff;
    --bull:#3fb950;--range:#58a6ff;--bear:#f85149;--crash:#f85149;
  }
  body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;min-height:100vh}
  a{color:var(--blue);text-decoration:none}

  /* ── Layout ── */
  .container{max-width:1400px;margin:0 auto;padding:20px}
  .grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
  .grid-4{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}
  .grid-6{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
  @media(max-width:900px){.grid-2,.grid-3,.grid-4,.grid-6{grid-template-columns:1fr 1fr}}
  @media(max-width:600px){.grid-2,.grid-3,.grid-4,.grid-6{grid-template-columns:1fr}}

  /* ── Header ── */
  .header{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;
    background:var(--surface);border-bottom:1px solid var(--border);margin-bottom:20px;
    border-radius:8px;flex-wrap:wrap;gap:12px}
  .header-left{display:flex;align-items:center;gap:16px}
  .header-title{font-size:20px;font-weight:700;color:var(--text)}
  .header-right{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  .btc-price{font-size:22px;font-weight:700;color:var(--text)}
  .btc-change{font-size:13px;font-weight:600}
  .last-updated{font-size:12px;color:var(--muted)}
  .pulse{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;animation:pulse 2s infinite}
  .pulse.green{background:var(--green)}
  .pulse.red{background:var(--red)}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}

  /* ── Cards ── */
  .card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px}
  .card-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:8px}
  .card-value{font-size:24px;font-weight:700}
  .card-sub{font-size:12px;color:var(--muted);margin-top:4px}
  .section-label{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;
    color:var(--muted);margin:24px 0 12px}

  /* ── Badges ── */
  .badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
  .badge.green{background:rgba(63,185,80,.15);color:var(--green);border:1px solid rgba(63,185,80,.3)}
  .badge.red{background:rgba(248,81,73,.15);color:var(--red);border:1px solid rgba(248,81,73,.3)}
  .badge.yellow{background:rgba(210,153,34,.15);color:var(--yellow);border:1px solid rgba(210,153,34,.3)}
  .badge.blue{background:rgba(88,166,255,.15);color:var(--blue);border:1px solid rgba(88,166,255,.3)}
  .badge.purple{background:rgba(188,140,255,.15);color:var(--purple);border:1px solid rgba(188,140,255,.3)}
  .badge.gray{background:rgba(139,148,158,.15);color:var(--muted);border:1px solid rgba(139,148,158,.3)}

  /* ── Strategy cards ── */
  .strat-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px;position:relative}
  .strat-card.has-position{border-color:rgba(88,166,255,.4)}
  .strat-name{font-weight:700;font-size:15px;margin-bottom:2px}
  .strat-meta{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
  .strat-row{display:flex;justify-content:space-between;margin-bottom:4px;font-size:13px}
  .strat-label{color:var(--muted)}
  .position-box{margin-top:10px;padding:8px;background:var(--surface2);border-radius:6px;
    border-left:3px solid var(--blue);font-size:12px}
  .position-title{font-weight:600;font-size:11px;text-transform:uppercase;color:var(--blue);margin-bottom:4px}
  .position-row{display:flex;justify-content:space-between;margin-bottom:2px}

  /* ── Tables ── */
  .table-wrap{overflow-x:auto}
  table{width:100%;border-collapse:collapse}
  th{text-align:left;padding:8px 10px;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
    color:var(--muted);border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:7px 10px;border-bottom:1px solid var(--border);font-size:13px}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:var(--surface2)}

  /* ── Regime box ── */
  .regime-box{padding:12px;background:var(--surface2);border-radius:6px;margin-top:8px}
  .regime-indicators{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px;font-size:12px}
  .ind-item{display:flex;justify-content:space-between}
  .ind-label{color:var(--muted)}

  /* ── Positive / Negative colours ── */
  .pos{color:var(--green)}
  .neg{color:var(--red)}
  .neu{color:var(--muted)}

  /* ── No-data state ── */
  .no-data{text-align:center;padding:40px;color:var(--muted)}
  .no-data-icon{font-size:36px;margin-bottom:8px}

  /* ── Tab buttons ── */
  .tab-btn{background:var(--surface2);color:var(--muted);border:1px solid var(--border);
    border-radius:6px;padding:5px 12px;font-size:12px;cursor:pointer;transition:all .15s}
  .tab-btn:hover{border-color:var(--blue);color:var(--text)}
  .tab-btn.active{background:rgba(88,166,255,.15);color:var(--blue);border-color:var(--blue);font-weight:600}

  /* ── Stat mini-cards ── */
  .stat-mini{background:var(--surface2);border-radius:6px;padding:10px 12px}
  .stat-mini-label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:3px}
  .stat-mini-value{font-size:17px;font-weight:700}
</style>
</head>
<body>
<div class="container">

  <!-- Header -->
  <div class="header">
    <div class="header-left">
      <span class="header-title">🤖 Crypto Bot Dashboard</span>
      <span id="mode-badge" class="badge gray">—</span>
    </div>
    <div class="header-right">
      <div>
        <span class="btc-price" id="btc-price">—</span>
        <span class="btc-change" id="btc-change"></span>
      </div>
      <div style="text-align:right">
        <div><span class="pulse" id="bot-pulse"></span><span id="bot-status-text" style="font-size:12px"></span></div>
        <div class="last-updated" id="last-updated">Loading…</div>
      </div>
    </div>
  </div>

  <!-- ── Portfolio Summary ── -->
  <div class="section-label">Portfolio Summary</div>
  <div class="grid-4">
    <div class="card">
      <div class="card-title">Total Capital</div>
      <div class="card-value" id="total-capital">—</div>
      <div class="card-sub">Starting allocation</div>
    </div>
    <div class="card">
      <div class="card-title">Total Equity</div>
      <div class="card-value" id="total-equity">—</div>
      <div class="card-sub" id="equity-sub">Cash + open positions</div>
    </div>
    <div class="card">
      <div class="card-title">Portfolio Return</div>
      <div class="card-value" id="total-return">—</div>
      <div class="card-sub" id="candle-count">—</div>
    </div>
    <div class="card">
      <div class="card-title">Circuit Breaker</div>
      <div class="card-value" id="cb-state">—</div>
      <div class="card-sub" id="cb-sub">—</div>
    </div>
  </div>

  <!-- ── Market Info ── -->
  <div class="section-label">Market & Bot Status</div>
  <div class="grid-3">
    <div class="card">
      <div class="card-title">Market Regime</div>
      <div id="regime-badge" style="margin-bottom:8px"></div>
      <div class="regime-box" id="regime-box">
        <div class="regime-indicators" id="regime-indicators"></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Strategy Allocations</div>
      <div id="allocations-list" style="margin-top:4px"></div>
    </div>
    <div class="card">
      <div class="card-title">Bot Status</div>
      <div id="bot-info"></div>
    </div>
  </div>

  <!-- ── Strategies ── -->
  <div class="section-label">Strategy Performance</div>
  <div class="grid-6" id="strategy-grid">
    <div class="no-data"><div class="no-data-icon">⏳</div><div>Waiting for bot data…</div></div>
  </div>

  <!-- ── Backtest + Live Recent Trades ── -->
  <div class="grid-2" style="margin-top:24px">
    <div>
      <div class="section-label" style="margin-top:0">Backtest Results (OOS)</div>
      <div class="card">
        <div id="backtest-updated" style="font-size:11px;color:var(--muted);margin-bottom:10px"></div>
        <div class="table-wrap">
          <table id="backtest-table">
            <thead><tr>
              <th>Strategy</th><th>OOS Return</th><th>Sharpe</th>
              <th>Max DD</th><th>Win Rate</th><th>Trades</th>
            </tr></thead>
            <tbody id="backtest-body">
              <tr><td colspan="6" class="no-data">Run ./run.sh backtest-full to populate</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
    <div>
      <div class="section-label" style="margin-top:0">Recent Live Trades</div>
      <div class="card">
        <div class="table-wrap">
          <table id="trades-table">
            <thead><tr>
              <th>Strategy</th><th>Time</th><th>Side</th>
              <th>Entry</th><th>Exit</th><th>P&L</th><th>Reason</th>
            </tr></thead>
            <tbody id="trades-body">
              <tr><td colspan="7" class="no-data">No trades yet</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- ── Full Persistent Trade History ── -->
  <div class="section-label" style="margin-top:28px">📋 Full Trade History</div>
  <div class="card">

    <!-- Strategy tab buttons -->
    <div id="hist-tabs" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px">
      <button class="tab-btn active" data-strat="">All Strategies</button>
      <button class="tab-btn" data-strat="DCA">DCA</button>
      <button class="tab-btn" data-strat="Supertrend">Supertrend</button>
      <button class="tab-btn" data-strat="Breakout">Breakout</button>
      <button class="tab-btn" data-strat="GridTrading">Grid</button>
      <button class="tab-btn" data-strat="TrendFollowing">Trend</button>
      <button class="tab-btn" data-strat="MeanReversion">MeanRev</button>
    </div>

    <!-- Per-strategy stats bar -->
    <div id="hist-stats" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:16px"></div>

    <!-- Equity curve -->
    <div style="margin-bottom:16px">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:6px">Cumulative P&L</div>
      <div style="position:relative;height:120px;background:var(--surface2);border-radius:6px;overflow:hidden">
        <svg id="equity-svg" width="100%" height="100%" preserveAspectRatio="none" style="display:block"></svg>
        <div id="equity-empty" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:13px">No trades recorded yet</div>
      </div>
    </div>

    <!-- Full trade table -->
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>#</th><th>Strategy</th><th>Date</th><th>Side</th>
          <th>Entry $</th><th>Exit $</th><th>P&L $</th><th>P&L %</th>
          <th>Reason</th>
        </tr></thead>
        <tbody id="hist-body">
          <tr><td colspan="9" class="no-data">No trades logged yet — start the bot to begin recording</td></tr>
        </tbody>
      </table>
    </div>

    <div id="hist-footer" style="font-size:11px;color:var(--muted);margin-top:10px;text-align:right"></div>
  </div>

</div><!-- /container -->

<script>
// ── Helpers ──────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const fmt = (n, d=2) => n == null ? '—' : n.toLocaleString('en-US', {minimumFractionDigits:d, maximumFractionDigits:d});
const fmtUSD = n => n == null ? '—' : '$' + fmt(n, 2);
const pnlClass = v => v > 0 ? 'pos' : v < 0 ? 'neg' : 'neu';
const pnlSign  = v => v > 0 ? '+' : '';

function regimeBadgeClass(regime) {
  if (!regime) return 'gray';
  const r = regime.toUpperCase();
  if (r.includes('BULL'))  return 'green';
  if (r.includes('RANGE')) return 'blue';
  if (r.includes('BEAR') || r.includes('CRASH') || r.includes('VOLATILE')) return 'red';
  return 'gray';
}

function timeAgo(iso) {
  if (!iso) return '—';
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60)   return Math.round(diff) + 's ago';
  if (diff < 3600) return Math.round(diff/60) + 'm ago';
  if (diff < 86400) return Math.round(diff/3600) + 'h ago';
  return Math.round(diff/86400) + 'd ago';
}

function shortTime(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('en-GB', {
    month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'
  });
}

// ── Strategy colour map ───────────────────────────────────────────────────────
const STRAT_COLOURS = {
  DCA:           '#58a6ff',
  Supertrend:    '#3fb950',
  Breakout:      '#bc8cff',
  GridTrading:   '#d29922',
  TrendFollowing:'#79c0ff',
  MeanReversion: '#f85149',
};
const stratColour = name => STRAT_COLOURS[name] || '#8b949e';

// ── Equity curve SVG renderer ─────────────────────────────────────────────────
function drawEquityCurve(curvePoints) {
  const svg = $('equity-svg');
  const empty = $('equity-empty');
  svg.innerHTML = '';

  if (!curvePoints || curvePoints.length < 2) {
    empty.style.display = 'flex';
    return;
  }
  empty.style.display = 'none';

  const W = svg.clientWidth  || 800;
  const H = svg.clientHeight || 120;
  const PAD = 4;

  const vals = curvePoints.map(p => p.cumulative_pnl);
  const minV = Math.min(0, ...vals);
  const maxV = Math.max(0, ...vals);
  const range = maxV - minV || 1;

  // Zero line
  const zeroY = H - PAD - ((0 - minV) / range) * (H - PAD * 2);
  const zeroLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  zeroLine.setAttribute('x1', 0); zeroLine.setAttribute('x2', W);
  zeroLine.setAttribute('y1', zeroY); zeroLine.setAttribute('y2', zeroY);
  zeroLine.setAttribute('stroke', '#30363d'); zeroLine.setAttribute('stroke-width', '1');
  svg.appendChild(zeroLine);

  // Build polyline points
  const pts = curvePoints.map((p, i) => {
    const x = PAD + (i / (curvePoints.length - 1)) * (W - PAD * 2);
    const y = H - PAD - ((p.cumulative_pnl - minV) / range) * (H - PAD * 2);
    return `${x},${y}`;
  });

  // Fill area
  const lastPt = pts[pts.length - 1].split(',');
  const firstX = PAD;
  const fillPts = `${firstX},${zeroY} ${pts.join(' ')} ${lastPt[0]},${zeroY}`;
  const fill = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
  fill.setAttribute('points', fillPts);
  const lastVal = vals[vals.length - 1];
  fill.setAttribute('fill', lastVal >= 0 ? 'rgba(63,185,80,.12)' : 'rgba(248,81,73,.12)');
  svg.appendChild(fill);

  // Line
  const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
  line.setAttribute('points', pts.join(' '));
  line.setAttribute('fill', 'none');
  line.setAttribute('stroke', lastVal >= 0 ? '#3fb950' : '#f85149');
  line.setAttribute('stroke-width', '2');
  line.setAttribute('stroke-linejoin', 'round');
  svg.appendChild(line);

  // Final value label
  const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  const lx = parseFloat(pts[pts.length-1].split(',')[0]);
  const ly = parseFloat(pts[pts.length-1].split(',')[1]);
  label.setAttribute('x', Math.min(lx, W - 60));
  label.setAttribute('y', Math.max(ly - 6, 14));
  label.setAttribute('fill', lastVal >= 0 ? '#3fb950' : '#f85149');
  label.setAttribute('font-size', '11');
  label.setAttribute('font-family', 'monospace');
  label.textContent = (lastVal >= 0 ? '+$' : '-$') + Math.abs(lastVal).toFixed(2);
  svg.appendChild(label);
}

// ── Trade history section ─────────────────────────────────────────────────────
let _activeStrat = '';

async function loadTradeHistory(strategy) {
  _activeStrat = strategy || '';
  // Update tab buttons
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.strat === _activeStrat);
  });

  let data;
  try {
    const url = '/api/trades' + (_activeStrat ? `?strategy=${encodeURIComponent(_activeStrat)}` : '');
    data = await fetch(url).then(r => r.json());
  } catch(e) {
    return;
  }

  const { trades, stats, curve } = data;

  // ── Stats bar ────────────────────────────────────────────────────────────
  const statsEl = $('hist-stats');
  if (!stats || stats.length === 0) {
    statsEl.innerHTML = '';
  } else {
    // Show stats for active strategy or aggregate
    let display;
    if (_activeStrat) {
      display = stats.filter(s => s.strategy === _activeStrat);
    } else {
      // Aggregate
      const agg = stats.reduce((a, s) => ({
        total_trades: (a.total_trades||0) + (s.total_trades||0),
        wins:         (a.wins||0)         + (s.wins||0),
        total_pnl:    (a.total_pnl||0)    + (s.total_pnl||0),
        best_trade:   Math.max(a.best_trade||0, s.best_trade||0),
        worst_trade:  Math.min(a.worst_trade??Infinity, s.worst_trade??Infinity),
      }), {});
      agg.win_rate = agg.total_trades ? Math.round(100*agg.wins/agg.total_trades*10)/10 : 0;
      agg.strategy = 'All';
      display = [agg];
    }
    const s = display[0] || {};
    const winR = s.win_rate || 0;
    statsEl.innerHTML = `
      <div class="stat-mini">
        <div class="stat-mini-label">Total Trades</div>
        <div class="stat-mini-value">${s.total_trades || 0}</div>
      </div>
      <div class="stat-mini">
        <div class="stat-mini-label">Win Rate</div>
        <div class="stat-mini-value ${winR >= 50 ? 'pos' : 'neg'}">${fmt(winR, 1)}%</div>
      </div>
      <div class="stat-mini">
        <div class="stat-mini-label">Total P&L</div>
        <div class="stat-mini-value ${pnlClass(s.total_pnl)}">${pnlSign(s.total_pnl)}$${fmt(Math.abs(s.total_pnl||0))}</div>
      </div>
      <div class="stat-mini">
        <div class="stat-mini-label">Best Trade</div>
        <div class="stat-mini-value pos">+$${fmt(s.best_trade||0)}</div>
      </div>
      <div class="stat-mini">
        <div class="stat-mini-label">Worst Trade</div>
        <div class="stat-mini-value neg">${s.worst_trade != null ? '-$'+fmt(Math.abs(s.worst_trade)) : '—'}</div>
      </div>
    `;
  }

  // ── Equity curve ──────────────────────────────────────────────────────────
  drawEquityCurve(curve);

  // ── Trade table ───────────────────────────────────────────────────────────
  const body = $('hist-body');
  if (!trades || trades.length === 0) {
    body.innerHTML = '<tr><td colspan="9" class="no-data">No trades logged yet — start the bot to begin recording</td></tr>';
    $('hist-footer').textContent = '';
    return;
  }

  body.innerHTML = trades.map((t, i) => {
    const pnl = t.pnl;
    const colour = stratColour(t.strategy);
    return `<tr>
      <td style="color:var(--muted);font-size:11px">${t.id || (trades.length - i)}</td>
      <td><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${colour};margin-right:5px"></span>${t.strategy}</td>
      <td style="color:var(--muted);font-size:12px;white-space:nowrap">${shortTime(t.exit_time)}</td>
      <td><span class="badge ${t.side==='long'?'green':'red'}">${t.side||'—'}</span>${t.is_partial ? ' <span class="badge gray" style="font-size:10px">pt</span>' : ''}</td>
      <td>$${fmt(t.entry_price, 2)}</td>
      <td>$${fmt(t.exit_price,  2)}</td>
      <td class="${pnlClass(pnl)}">${pnlSign(pnl)}$${fmt(Math.abs(pnl))}</td>
      <td class="${pnlClass(t.pnl_pct)}" style="font-size:12px">${pnlSign(t.pnl_pct)}${fmt(t.pnl_pct||0, 2)}%</td>
      <td style="color:var(--muted);font-size:12px">${t.exit_reason||'—'}</td>
    </tr>`;
  }).join('');

  $('hist-footer').textContent = `Showing ${trades.length} trade${trades.length!==1?'s':''} · Persisted to SQLite · Survives bot restarts`;
}

// Set up tab click handlers
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => loadTradeHistory(btn.dataset.strat));
});

// ── Main render ───────────────────────────────────────────────────────────────
async function refresh() {
  let data;
  try {
    const res = await fetch('/api/status');
    data = await res.json();
  } catch(e) {
    $('last-updated').textContent = 'Connection error — retrying…';
    return;
  }

  const { paper, backtest, bot, live_price, server_time } = data;
  $('last-updated').textContent = 'Updated ' + timeAgo(server_time);

  // ── BTC live price ────────────────────────────────────────────────────────
  const btc = live_price['BTC/USDT'];
  if (btc) {
    $('btc-price').textContent = '$' + fmt(btc.price, 0);
    const chg = btc.change_pct || 0;
    $('btc-change').textContent = (chg >= 0 ? '+' : '') + fmt(chg, 2) + '%';
    $('btc-change').className = 'btc-change ' + pnlClass(chg);
  }

  // ── Bot status ────────────────────────────────────────────────────────────
  const running = bot.is_running;
  const pulse   = $('bot-pulse');
  pulse.className = 'pulse ' + (running ? 'green' : 'red');
  $('bot-status-text').textContent = running ? 'Bot running' : 'Bot stopped';

  // ── Mode badge ────────────────────────────────────────────────────────────
  const mode = paper.mode || bot.mode || 'paper';
  const modeBadge = $('mode-badge');
  modeBadge.textContent = mode.toUpperCase();
  modeBadge.className = 'badge ' + (mode === 'live' ? 'red' : 'blue');

  // ── Portfolio summary cards ────────────────────────────────────────────────
  if (paper.total_capital != null) {
    $('total-capital').textContent = fmtUSD(paper.total_capital);
    $('total-equity').textContent  = fmtUSD(paper.total_equity);

    const ret = paper.total_return_pct;
    $('total-return').textContent = (ret >= 0 ? '+' : '') + fmt(ret) + '%';
    $('total-return').className = 'card-value ' + pnlClass(ret);
    $('candle-count').textContent = (paper.candle_count || 0) + ' candles processed';

    const cbState = paper.circuit_breaker || '—';
    $('cb-state').textContent = cbState;
    const cbEl = $('cb-state');
    cbEl.style.color = cbState === 'NORMAL'    ? 'var(--green)'
                     : cbState === 'WARNING'   ? 'var(--yellow)'
                     : cbState === 'TRIPPED'   ? 'var(--red)'
                     : 'var(--muted)';
    $('cb-sub').textContent = 'DD from peak: -' + fmt(paper.circuit_breaker_drawdown_pct || 0) + '%';
  }

  // ── Regime ────────────────────────────────────────────────────────────────
  if (paper.regime) {
    $('regime-badge').innerHTML =
      `<span class="badge ${regimeBadgeClass(paper.regime)}">${paper.regime}</span>
       <span style="font-size:12px;color:var(--muted);margin-left:8px">${fmt(paper.regime_confidence || 0, 0)}% confidence</span>`;

    const inds = [
      ['EMA50',  paper.regime_ema50  ? '$'+fmt(paper.regime_ema50,  0) : '—'],
      ['EMA200', paper.regime_ema200 ? '$'+fmt(paper.regime_ema200, 0) : '—'],
      ['RSI',    paper.regime_rsi    ? fmt(paper.regime_rsi, 1) : '—'],
      ['ATR%',   paper.regime_atr_pct ? fmt(paper.regime_atr_pct, 2)+'%' : '—'],
    ];
    $('regime-indicators').innerHTML = inds.map(([l,v]) =>
      `<div class="ind-item"><span class="ind-label">${l}</span><span>${v}</span></div>`
    ).join('');
  }

  // ── Allocations ───────────────────────────────────────────────────────────
  if (paper.strategies) {
    const allRows = Object.entries(paper.strategies)
      .sort((a,b) => b[1].weight_pct - a[1].weight_pct)
      .map(([name, s]) => {
        const bar = Math.round((s.weight_pct || 0) / 100 * 80);
        const colour = stratColour(name);
        return `<div style="margin-bottom:8px">
          <div style="display:flex;justify-content:space-between;margin-bottom:3px;font-size:12px">
            <span style="display:flex;align-items:center;gap:5px">
              <span style="width:8px;height:8px;border-radius:50%;background:${colour};display:inline-block"></span>
              ${name}
            </span>
            <span style="color:var(--muted)">${fmt(s.weight_pct, 0)}%</span>
          </div>
          <div style="height:4px;background:var(--surface2);border-radius:2px">
            <div style="height:4px;width:${bar}%;background:${colour};border-radius:2px"></div>
          </div>
        </div>`;
      }).join('');
    $('allocations-list').innerHTML = allRows;
  }

  // ── Bot info ──────────────────────────────────────────────────────────────
  $('bot-info').innerHTML = `
    <div style="font-size:13px">
      <div style="margin-bottom:6px">
        <span class="badge ${running ? 'green' : 'red'}">${running ? 'RUNNING' : 'OFFLINE'}</span>
      </div>
      <div style="color:var(--muted);margin-bottom:4px;font-size:12px">Last candle</div>
      <div style="margin-bottom:10px">${shortTime(bot.last_candle_ts || paper.updated_at)}</div>
      <div style="color:var(--muted);margin-bottom:4px;font-size:12px">Symbol / Timeframe</div>
      <div style="margin-bottom:10px">${bot.symbol || paper.symbol || '—'} · ${bot.timeframe || paper.timeframe || '—'}</div>
      <div style="color:var(--muted);margin-bottom:4px;font-size:12px">Candles processed</div>
      <div>${bot.candle_count || paper.candle_count || 0}</div>
    </div>`;

  // ── Strategy cards ────────────────────────────────────────────────────────
  if (paper.strategies && Object.keys(paper.strategies).length) {
    const cards = Object.entries(paper.strategies).map(([name, s]) => {
      const ret = s.return_pct;
      const pos = s.position;
      const colour = stratColour(name);

      let posBlock = '';
      if (pos) {
        const upnlClass = pnlClass(pos.unrealized_pnl);
        posBlock = `<div class="position-box">
          <div class="position-title">📌 Open ${pos.side.toUpperCase()}</div>
          <div class="position-row">
            <span style="color:var(--muted)">Entry</span>
            <span>$${fmt(pos.avg_entry_price, 2)}</span>
          </div>
          <div class="position-row">
            <span style="color:var(--muted)">Current</span>
            <span>$${pos.current_price ? fmt(pos.current_price, 2) : '—'}</span>
          </div>
          <div class="position-row">
            <span style="color:var(--muted)">Unrealised P&L</span>
            <span class="${upnlClass}">${pnlSign(pos.unrealized_pnl)}$${fmt(Math.abs(pos.unrealized_pnl))} (${pnlSign(pos.unrealized_pnl_pct)}${fmt(pos.unrealized_pnl_pct)}%)</span>
          </div>
          ${pos.stop_loss   ? `<div class="position-row"><span style="color:var(--muted)">SL</span><span style="color:var(--red)">$${fmt(pos.stop_loss,2)}</span></div>` : ''}
          ${pos.take_profit ? `<div class="position-row"><span style="color:var(--muted)">TP</span><span style="color:var(--green)">$${fmt(pos.take_profit,2)}</span></div>` : ''}
        </div>`;
      }

      return `<div class="strat-card ${pos ? 'has-position' : ''}" style="border-top:3px solid ${colour}">
        <div class="strat-name">${name}</div>
        <div class="strat-meta">
          <span class="badge ${s.active && s.weight_pct > 0 ? 'green' : 'gray'}">${s.weight_pct > 0 ? 'Active' : 'Suspended'}</span>
          <span class="badge blue">${fmt(s.weight_pct,0)}%</span>
        </div>
        <div class="strat-row">
          <span class="strat-label">Capital</span>
          <span>${fmtUSD(s.capital)}</span>
        </div>
        <div class="strat-row">
          <span class="strat-label">Equity</span>
          <span>${fmtUSD(s.equity)}</span>
        </div>
        <div class="strat-row">
          <span class="strat-label">Return</span>
          <span class="${pnlClass(ret)}">${pnlSign(ret)}${fmt(ret)}%</span>
        </div>
        <div class="strat-row">
          <span class="strat-label">P&L</span>
          <span class="${pnlClass(s.total_pnl)}">${pnlSign(s.total_pnl)}${fmtUSD(Math.abs(s.total_pnl))}</span>
        </div>
        <div class="strat-row">
          <span class="strat-label">Trades / Win%</span>
          <span>${s.trades} · ${fmt(s.win_rate,0)}%</span>
        </div>
        ${posBlock}
      </div>`;
    }).join('');
    $('strategy-grid').innerHTML = cards;
  }

  // ── Backtest table ────────────────────────────────────────────────────────
  if (backtest.strategies && Object.keys(backtest.strategies).length) {
    $('backtest-updated').textContent = 'Last run: ' + timeAgo(backtest.updated_at) + ' · ' + (backtest.symbol || '') + ' ' + (backtest.timeframe || '');

    const sorted = Object.entries(backtest.strategies)
      .filter(([,r]) => r.oos)
      .sort((a,b) => (b[1].oos.sharpe_ratio || 0) - (a[1].oos.sharpe_ratio || 0));

    $('backtest-body').innerHTML = sorted.map(([name, r]) => {
      const oos = r.oos;
      const ret = oos.total_return_pct;
      const sharpe = oos.sharpe_ratio;
      return `<tr>
        <td><strong>${name}</strong></td>
        <td class="${pnlClass(ret)}">${pnlSign(ret)}${fmt(ret)}%</td>
        <td class="${pnlClass(sharpe)}">${fmt(sharpe, 3)}</td>
        <td style="color:var(--red)">-${fmt(oos.max_drawdown_pct)}%</td>
        <td>${fmt(oos.win_rate_pct, 0)}%</td>
        <td>${oos.total_trades}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="6" class="no-data">No OOS results</td></tr>';
  }

  // ── Recent live trades (in-memory, last 15) ────────────────────────────────
  const trades = paper.recent_trades || [];
  if (trades.length) {
    $('trades-body').innerHTML = trades.slice(0, 15).map(t => {
      const pnl = t.pnl;
      return `<tr>
        <td>${t.strategy}</td>
        <td style="color:var(--muted);font-size:12px">${shortTime(t.exit_time)}</td>
        <td><span class="badge ${t.side==='long'?'green':'red'}">${t.side}</span>${t.is_partial ? ' <span class="badge gray" style="font-size:10px">pt</span>' : ''}</td>
        <td>$${fmt(t.entry_price,2)}</td>
        <td>$${fmt(t.exit_price,2)}</td>
        <td class="${pnlClass(pnl)}">${pnlSign(pnl)}$${fmt(Math.abs(pnl))}</td>
        <td style="color:var(--muted);font-size:12px">${t.exit_reason}</td>
      </tr>`;
    }).join('');
  }
}

// ── Auto-refresh ──────────────────────────────────────────────────────────────
// Live status: every 5 seconds
setInterval(refresh, 5000);
refresh();

// Trade history: every 30 seconds (SQLite reads are heavier)
setInterval(() => loadTradeHistory(_activeStrat), 30000);
loadTradeHistory('');
</script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Start background price fetcher
    t = threading.Thread(target=_fetch_live_price, daemon=True)
    t.start()

    # Port 5000 is taken by macOS AirPlay Receiver — use 8080 by default.
    # Override with env var: DASHBOARD_PORT=9000 ./run.sh dashboard
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    print(f"\n  🚀 Dashboard running at http://localhost:{port}")
    print(f"  Auto-refreshes every 5 seconds.")
    print(f"  Press Ctrl+C to stop.\n")

    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
