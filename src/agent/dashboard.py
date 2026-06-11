"""Observability dashboard: local single-page UI over the agent's artifacts.

Reads the same files judges replay (journal.jsonl, ledger.jsonl,
portfolio.json) — the dashboard is a view, never a second source of truth.
Stdlib http.server only; no new dependencies.

Run:  python -m agent.dashboard            (http://localhost:8765)
"""

import json
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from agent import config
from agent.runner import JOURNAL_PATH, LEDGER_PATH, PORTFOLIO_PATH

PORT = 8765


def _read_jsonl(path: Path, limit: int = 200) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().strip().splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn tail write must not take down /api/state
    return out


def state() -> dict:
    portfolio = json.loads(PORTFOLIO_PATH.read_text()) if PORTFOLIO_PATH.exists() else {}
    journal = _read_jsonl(JOURNAL_PATH)
    ledger = _read_jsonl(LEDGER_PATH, limit=100)

    rule_counts: dict[str, int] = {}
    equity_series = []
    fear_greed = None
    for rec in journal:
        if "risk_verdict" in rec:
            rule = rec["risk_verdict"].get("rule")
            if rule and rule not in ("no_action", "position_sizing", "strategy_exit"):
                rule_counts[rule] = rule_counts.get(rule, 0) + 1
            if rec.get("equity") is not None:
                equity_series.append({"ts": rec["ts"], "equity": rec["equity"]})
            fg = (rec.get("inputs") or {}).get("fear_greed")
            if fg is not None:
                fear_greed = fg
        elif "event" in rec:
            rule_counts[rec["event"]] = rule_counts.get(rec["event"], 0) + 1

    return {
        "now": time.time(),
        "mode": config.get_settings().mode,
        "universe": config.UNIVERSE,
        "portfolio": portfolio,
        "fear_greed": fear_greed,
        "rule_counts": rule_counts,
        "equity_series": equity_series[-500:],
        "decisions": [r for r in journal if "signal" in r][-40:][::-1],
        "fills": ledger[::-1],
    }


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CMC Disciplined Trader</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0F172A; --surface:#182338; --muted:#272F42; --border:#334155;
    --fg:#F8FAFC; --fg2:#94A3B8; --fg3:#7C8CA5;
    --gold:#F59E0B; --amber:#FBBF24; --green:#2DD4BF; --red:#EF5350;
  }
  *{box-sizing:border-box}
  body{background:var(--bg);color:var(--fg);font:14px/1.5 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;padding:24px 20px 40px}
  .wrap{max-width:1280px;margin:0 auto;display:flex;flex-direction:column;gap:16px}
  .num{font-variant-numeric:tabular-nums}
  :focus-visible{outline:2px solid var(--gold);outline-offset:2px}
  header{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  .logo{width:36px;height:36px;border-radius:9px;background:linear-gradient(135deg,#B45309,var(--gold));display:flex;align-items:center;justify-content:center;flex:none}
  .logo svg{width:19px;height:19px;stroke:#0F172A;fill:none;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}
  h1{font-size:17px;font-weight:600;margin:0;letter-spacing:-.01em;line-height:1.25}
  h1 small{display:block;font-size:11px;font-weight:500;color:var(--fg2);letter-spacing:.02em}
  .chip{font-size:10.5px;font-weight:700;letter-spacing:.08em;padding:3px 11px;border-radius:999px;border:1px solid var(--border);color:var(--fg2)}
  .chip.paper{color:var(--amber);border-color:rgba(251,191,36,.4);background:rgba(251,191,36,.08)}
  .chip.live{color:var(--green);border-color:rgba(45,212,191,.4);background:rgba(45,212,191,.08)}
  .spacer{flex:1}
  .status{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--fg2)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse 2s ease-in-out infinite}
  .status.stale .dot{background:var(--amber);animation:none}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:12px}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 18px}
  .kpi .l{font-size:11px;font-weight:600;color:var(--fg2);text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px}
  .kpi .v{font-size:24px;font-weight:600;line-height:1.2}
  .kpi .s{font-size:11.5px;color:var(--fg2);margin-top:5px}
  .kpi.pos .v{color:var(--green)} .kpi.neg .v{color:var(--red)}
  .unit{font-size:12px;color:var(--fg2);font-weight:500;margin-left:4px}
  .gauge{position:relative;height:6px;border-radius:3px;margin-top:10px;background:linear-gradient(90deg,#EF5350 0 20%,#FBBF24 20% 40%,#64748B 40% 60%,#34D399 60% 80%,#26A69A 80% 100%)}
  .gauge i{position:absolute;top:-3px;width:2px;height:12px;background:var(--fg);border-radius:1px}
  .card-h{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:14px 18px;border-bottom:1px solid var(--border)}
  .card-h h2{font-size:13px;font-weight:600;margin:0}
  .card-h .meta{font-size:11.5px;color:var(--fg2)}
  .table-card{padding:0}
  .pad{padding:16px 18px}
  #chartwrap{position:relative}
  #tip{position:absolute;display:none;background:#0B1222;border:1px solid var(--border);border-radius:8px;padding:6px 10px;font-size:11.5px;pointer-events:none;z-index:10;white-space:nowrap;box-shadow:0 4px 16px rgba(0,0,0,.45)}
  #xline{position:absolute;width:1px;background:var(--border);display:none;pointer-events:none}
  .empty{color:var(--fg2);font-size:12.5px;padding:26px 12px;text-align:center}
  .gates{display:flex;flex-wrap:wrap;gap:8px}
  .gate{display:inline-flex;align-items:center;gap:7px;background:var(--muted);border:1px solid var(--border);border-radius:8px;padding:6px 11px;font-size:12px;color:var(--fg2);transition:border-color .15s ease}
  .gate:hover{border-color:var(--fg3)}
  .gate svg{width:13px;height:13px;flex:none;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
  .gate b{color:var(--fg);font-weight:600}
  .gate.veto{color:var(--red);border-color:rgba(239,83,80,.35)}
  .gate.veto b{color:var(--red)}
  .table-wrap{overflow-x:auto}
  table{border-collapse:collapse;width:100%;font-size:12.5px}
  th{text-align:left;color:var(--fg2);font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;padding:9px 14px;border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:8px 14px;border-bottom:1px solid var(--muted);white-space:nowrap;font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-size:12px;font-variant-numeric:tabular-nums}
  tr:last-child td{border-bottom:none}
  tbody tr{transition:background .15s ease}
  tbody tr:hover{background:var(--muted)}
  td.reason{white-space:normal;color:var(--fg2);font-family:Inter,system-ui,sans-serif;min-width:260px}
  th.r,td.r{text-align:right}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:10.5px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;font-family:Inter,system-ui,sans-serif}
  .pill.enter,.pill.buy{background:rgba(45,212,191,.13);color:var(--green)}
  .pill.exit,.pill.sell{background:rgba(251,191,36,.13);color:var(--amber)}
  .pill.hold{background:rgba(148,163,184,.12);color:var(--fg2)}
  .pill.veto{background:rgba(239,83,80,.13);color:var(--red)}
  .pill.info{background:rgba(148,163,184,.12);color:var(--fg2)}
  .gain{color:var(--green)} .loss{color:var(--red)} .dim{color:var(--fg3)}
  footer{font-size:11.5px;color:var(--fg3);text-align:center}
  @media (prefers-reduced-motion: reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<div class="wrap">
<header>
  <div class="logo" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg></div>
  <h1>CMC Disciplined Trader<small>autonomous BSC trading agent</small></h1>
  <span class="chip" id="mode">&mdash;</span>
  <div class="spacer"></div>
  <div class="status" id="status"><span class="dot" aria-hidden="true"></span><span id="updated">connecting&hellip;</span></div>
</header>

<div class="kpis" id="cards" aria-live="polite"></div>

<div class="card table-card">
  <div class="card-h"><h2>Equity curve <span class="unit">USDT</span></h2><span class="meta num" id="chartmeta"></span></div>
  <div class="pad" id="chartwrap">
    <canvas id="chart" role="img" aria-label="Equity over time"></canvas>
    <div id="xline" aria-hidden="true"></div><div id="tip" aria-hidden="true"></div>
    <div class="empty" id="chartempty" style="display:none">Collecting equity data &mdash; first points arrive within a couple of polls.</div>
  </div>
</div>

<div class="card table-card">
  <div class="card-h"><h2>Risk-gate activity</h2><span class="meta">counts since journal start</span></div>
  <div class="pad gates" id="rules"></div>
</div>

<div class="card table-card">
  <div class="card-h"><h2>Latest decisions</h2><span class="meta">journal replay &middot; newest first &middot; times UTC</span></div>
  <div class="table-wrap"><table id="decisions">
    <thead><tr><th>time</th><th>asset</th><th class="r">price</th><th class="r">rsi</th><th>action</th><th>risk rule</th><th>reason</th></tr></thead>
    <tbody></tbody></table></div>
</div>

<div class="card table-card">
  <div class="card-h"><h2>Fills</h2><span class="meta">ledger &middot; newest first &middot; times UTC</span></div>
  <div class="table-wrap"><table id="fills">
    <thead><tr><th>time</th><th>side</th><th>asset</th><th class="r">qty</th><th class="r">price</th><th class="r">pnl (usdt)</th></tr></thead>
    <tbody></tbody></table></div>
</div>

<footer>Deterministic strategy core &middot; hard risk gates &middot; full decision log &mdash; this dashboard reads journal.jsonl, ledger.jsonl and portfolio.json directly; it is a view, never a second source of truth.</footer>
</div>
<script>
const START = 150;
const VETO_RULES = ['token_risk_veto','stop_loss','kill_switch','daily_loss_cap','reentry_cooldown'];
const SHIELD = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>';
let lastFetch = 0, lastState = null, pts = [];

const esc = s => String(s).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = ts => new Date(ts * 1000).toISOString().slice(5, 16).replace('T', ' ');
const fmtS = ts => new Date(ts * 1000).toISOString().slice(5, 19).replace('T', ' ');
const fmtFull = ts => new Date(ts * 1000).toISOString().slice(0, 19).replace('T', ' ');

function fgZone(v) {
  return v < 20 ? ['Extreme Fear', 'var(--red)'] : v < 40 ? ['Fear', 'var(--amber)']
    : v < 60 ? ['Neutral', 'var(--fg2)'] : v < 80 ? ['Greed', 'var(--green)'] : ['Extreme Greed', 'var(--green)'];
}

function maxDrawdown(vals) {
  let peak = -Infinity, dd = 0;
  for (const v of vals) { peak = Math.max(peak, v); if (peak > 0) dd = Math.max(dd, (peak - v) / peak); }
  return dd * 100;
}

function render(s) {
  const p = s.portfolio || {};
  const positions = Object.keys(p.positions || {});
  const es = s.equity_series;
  const vals = es.map(e => e.equity);
  const eq = vals.length ? vals[vals.length - 1] : (p.cash || 0);
  const ret = (eq - START) / START * 100;
  const dd = vals.length ? maxDrawdown(vals) : 0;
  const fg = s.fear_greed;
  const [zone, zcol] = fg != null ? fgZone(fg) : ['no reading yet', 'var(--fg2)'];

  const m = document.getElementById('mode');
  m.textContent = s.mode.toUpperCase();
  m.className = 'chip ' + (s.mode === 'live' ? 'live' : 'paper');

  document.getElementById('cards').innerHTML = [
    {l:'Equity', v:`${eq.toFixed(2)}<span class="unit">USDT</span>`, s:`peak ${(p.peak_equity ?? eq).toFixed(2)}`},
    {l:'Return', v:`${ret >= 0 ? '+' : ''}${ret.toFixed(2)}<span class="unit">%</span>`, c:ret >= 0 ? 'pos' : 'neg', s:`since start &middot; ${START} USDT`},
    {l:'Cash', v:`${(p.cash ?? 0).toFixed(2)}<span class="unit">USDT</span>`, s:positions.length ? `${(eq - (p.cash ?? 0)).toFixed(2)} deployed` : 'fully in cash'},
    {l:'Open positions', v:String(positions.length), s:positions.length ? esc(positions.join(' · ')) : 'flat &mdash; waiting for signal'},
    {l:'Max drawdown', v:`${dd.toFixed(2)}<span class="unit">%</span>`, c:dd > 5 ? 'neg' : '', s:`across ${vals.length} samples`},
    {l:'Fear &amp; Greed', v:fg ?? '&mdash;', s:`<span style="color:${zcol}">${zone}</span>`,
     extra:fg != null ? `<div class="gauge" aria-hidden="true"><i style="left:${Math.min(Math.max(fg, 0), 100)}%"></i></div>` : ''},
  ].map(k => `<div class="card kpi ${k.c || ''}"><div class="l">${k.l}</div><div class="v num">${k.v}</div><div class="s">${k.s}</div>${k.extra || ''}</div>`).join('');

  document.getElementById('rules').innerHTML = Object.entries(s.rule_counts)
    .sort((a, b) => b[1] - a[1])
    .map(([r, n]) => `<span class="gate ${VETO_RULES.includes(r) ? 'veto' : ''}">${SHIELD}${esc(r).replace(/_/g, ' ')} <b class="num">&times;${n}</b></span>`)
    .join('') || `<span class="gate">${SHIELD}no gates fired yet &mdash; entries passing clean</span>`;

  document.getElementById('chartmeta').textContent = es.length > 1
    ? `${es.length} samples · ${fmt(es[0].ts)} → ${fmt(es[es.length - 1].ts)} UTC` : '';
  document.getElementById('chart').setAttribute('aria-label',
    `Equity line chart, ${es.length} samples, current ${eq.toFixed(2)} USDT, return ${ret.toFixed(2)} percent`);
  drawChart(es);

  document.querySelector('#decisions tbody').innerHTML = s.decisions.map(d => {
    const sig = d.signal || {}, rv = d.risk_verdict || {}, q = (d.inputs || {}).quote || {};
    const act = sig.action === 'enter' ? 'enter' : sig.action === 'exit' ? 'exit' : 'hold';
    const rule = rv.rule
      ? `<span class="pill ${VETO_RULES.includes(rv.rule) ? 'veto' : 'info'}">${esc(rv.rule).replace(/_/g, ' ')}</span>`
      : '<span class="dim">&mdash;</span>';
    return `<tr><td>${fmtS(d.ts)}</td><td>${esc(d.symbol)}</td><td class="r">${(q.price ?? 0).toFixed(2)}</td>
      <td class="r">${sig.rsi ? sig.rsi.toFixed(1) : '<span class="dim">&mdash;</span>'}</td>
      <td><span class="pill ${act}">${esc(sig.action)}</span></td><td>${rule}</td>
      <td class="reason">${esc(sig.reason || '')}</td></tr>`;
  }).join('') || '<tr><td colspan="7"><div class="empty">No decisions recorded yet.</div></td></tr>';

  document.querySelector('#fills tbody').innerHTML = s.fills.map(f =>
    `<tr><td>${fmtS(f.ts)}</td><td><span class="pill ${f.side === 'buy' ? 'buy' : 'sell'}">${esc(f.side)}</span></td>
     <td>${esc(f.symbol)}</td><td class="r">${f.qty.toFixed(6)}</td><td class="r">${f.price.toFixed(2)}</td>
     <td class="r ${f.pnl_usdt == null ? 'dim' : f.pnl_usdt >= 0 ? 'gain' : 'loss'}">${f.pnl_usdt == null ? '&mdash;' : (f.pnl_usdt >= 0 ? '+' : '') + f.pnl_usdt.toFixed(3)}</td></tr>`
  ).join('') || '<tr><td colspan="6"><div class="empty">No trades yet &mdash; risk gates holding.</div></td></tr>';
}

function drawChart(es) {
  const wrap = document.getElementById('chartwrap');
  const c = document.getElementById('chart');
  const emptyEl = document.getElementById('chartempty');
  if (es.length < 2) { c.style.display = 'none'; emptyEl.style.display = 'block'; pts = []; return; }
  c.style.display = 'block'; emptyEl.style.display = 'none';

  const dpr = window.devicePixelRatio || 1;
  const w = wrap.clientWidth - 36, H = 230;
  c.width = w * dpr; c.height = H * dpr;
  c.style.width = w + 'px'; c.style.height = H + 'px';
  const x = c.getContext('2d');
  x.setTransform(dpr, 0, 0, dpr, 0, 0);
  x.clearRect(0, 0, w, H);

  const PL = 56, PR = 12, PT = 12, PB = 24;
  const vals = es.map(e => e.equity);
  let lo = Math.min(...vals, START), hi = Math.max(...vals, START);
  const margin = Math.max((hi - lo) * 0.12, 0.4);
  lo -= margin; hi += margin;
  const X = i => PL + i / (es.length - 1) * (w - PL - PR);
  const Y = v => H - PB - (v - lo) / (hi - lo) * (H - PT - PB);

  x.font = '11px Inter, system-ui, sans-serif';
  x.textAlign = 'right'; x.textBaseline = 'middle';
  for (let g = 0; g <= 3; g++) {
    const v = lo + (hi - lo) * g / 3, y = Y(v);
    x.strokeStyle = 'rgba(51,65,85,.45)'; x.lineWidth = 1;
    x.beginPath(); x.moveTo(PL, y); x.lineTo(w - PR, y); x.stroke();
    x.fillStyle = '#7C8CA5'; x.fillText(v.toFixed(1), PL - 8, y);
  }

  const by = Y(START);
  x.setLineDash([4, 4]); x.strokeStyle = 'rgba(148,163,184,.55)';
  x.beginPath(); x.moveTo(PL, by); x.lineTo(w - PR, by); x.stroke();
  x.setLineDash([]);
  x.textAlign = 'left'; x.fillStyle = '#94A3B8'; x.fillText('start ' + START, PL + 4, by - 9);

  const grad = x.createLinearGradient(0, PT, 0, H - PB);
  grad.addColorStop(0, 'rgba(245,158,11,.25)'); grad.addColorStop(1, 'rgba(245,158,11,0)');
  x.beginPath();
  es.forEach((e, i) => { i ? x.lineTo(X(i), Y(e.equity)) : x.moveTo(X(i), Y(e.equity)); });
  x.lineTo(X(es.length - 1), H - PB); x.lineTo(X(0), H - PB); x.closePath();
  x.fillStyle = grad; x.fill();

  x.beginPath();
  es.forEach((e, i) => { i ? x.lineTo(X(i), Y(e.equity)) : x.moveTo(X(i), Y(e.equity)); });
  x.strokeStyle = '#F59E0B'; x.lineWidth = 2; x.lineJoin = 'round'; x.lineCap = 'round'; x.stroke();

  const lx = X(es.length - 1), ly = Y(vals[vals.length - 1]);
  x.fillStyle = 'rgba(245,158,11,.25)'; x.beginPath(); x.arc(lx, ly, 7, 0, 7); x.fill();
  x.fillStyle = '#F59E0B'; x.beginPath(); x.arc(lx, ly, 3, 0, 7); x.fill();

  x.fillStyle = '#7C8CA5';
  x.textAlign = 'left'; x.fillText(fmt(es[0].ts), PL, H - 8);
  x.textAlign = 'right'; x.fillText(fmt(es[es.length - 1].ts) + ' UTC', w - PR, H - 8);

  pts = es.map((e, i) => ({ x: X(i), y: Y(e.equity), e }));
}

const chartEl = document.getElementById('chart');
const tip = document.getElementById('tip');
const xline = document.getElementById('xline');
chartEl.addEventListener('mousemove', ev => {
  if (!pts.length) return;
  const r = chartEl.getBoundingClientRect();
  const wrapR = document.getElementById('chartwrap').getBoundingClientRect();
  const mx = ev.clientX - r.left;
  let best = pts[0];
  for (const p of pts) if (Math.abs(p.x - mx) < Math.abs(best.x - mx)) best = p;
  tip.innerHTML = `<b class="num">${best.e.equity.toFixed(2)} USDT</b><br><span style="color:var(--fg2)">${fmtFull(best.e.ts)} UTC</span>`;
  const baseX = r.left - wrapR.left, baseY = r.top - wrapR.top;
  tip.style.display = 'block';
  tip.style.left = (best.x + 160 > r.width ? baseX + best.x - 158 : baseX + best.x + 12) + 'px';
  tip.style.top = (baseY + Math.max(best.y - 44, 0)) + 'px';
  xline.style.display = 'block';
  xline.style.left = (baseX + best.x) + 'px';
  xline.style.top = baseY + 'px';
  xline.style.height = r.height + 'px';
});
chartEl.addEventListener('mouseleave', () => { tip.style.display = 'none'; xline.style.display = 'none'; });

setInterval(() => {
  if (!lastFetch) return;
  const s = Math.round((Date.now() - lastFetch) / 1000);
  const status = document.getElementById('status');
  const stale = s > 15;
  status.classList.toggle('stale', stale);
  document.getElementById('updated').textContent =
    stale ? `stale · last update ${s}s ago` : s <= 1 ? 'live · updated just now' : `live · updated ${s}s ago`;
}, 1000);

window.addEventListener('resize', () => { if (lastState) drawChart(lastState.equity_series); });

async function tick() {
  try {
    const s = await (await fetch('/api/state')).json();
    lastFetch = Date.now(); lastState = s;
    render(s);
  } catch (e) { /* keep last view; staleness indicator takes over */ }
}
tick(); setInterval(tick, 5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/state":
            body = json.dumps(state()).encode()
            ctype = "application/json"
        elif self.path == "/":
            body = PAGE.encode()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep stdout clean


def main() -> None:
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"dashboard: http://localhost:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
