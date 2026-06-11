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
    lines = path.read_text().strip().splitlines()[-limit:]
    return [json.loads(l) for l in lines]


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
<html><head><meta charset="utf-8"><title>cmc-disciplined-trader</title>
<style>
  body { background:#0d1117; color:#c9d1d9; font:14px/1.5 ui-monospace,monospace; margin:0; padding:24px; }
  h1 { font-size:18px; color:#e6edf3; } h1 span { color:#7ee787; }
  h2 { font-size:13px; color:#8b949e; text-transform:uppercase; letter-spacing:.08em; margin:28px 0 8px; }
  .cards { display:flex; gap:16px; flex-wrap:wrap; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px 18px; min-width:130px; }
  .card .v { font-size:22px; color:#e6edf3; } .card .l { font-size:11px; color:#8b949e; }
  .pos .v { color:#7ee787; } .neg .v { color:#ff7b72; }
  table { border-collapse:collapse; width:100%; font-size:12.5px; }
  th { text-align:left; color:#8b949e; font-weight:normal; padding:4px 10px; border-bottom:1px solid #30363d; }
  td { padding:4px 10px; border-bottom:1px solid #21262d; white-space:nowrap; }
  td.reason { white-space:normal; color:#8b949e; }
  .enter { color:#7ee787; } .exit { color:#ffa657; } .hold { color:#8b949e; }
  .veto { color:#ff7b72; }
  canvas { background:#161b22; border:1px solid #30363d; border-radius:8px; width:100%; height:120px; }
  .badge { display:inline-block; background:#21262d; border-radius:10px; padding:1px 10px; margin:2px; font-size:12px; }
</style></head><body>
<h1>cmc-disciplined-trader <span id="mode"></span></h1>
<div class="cards" id="cards"></div>
<h2>Equity (USDT)</h2><canvas id="chart" width="1200" height="120"></canvas>
<h2>Risk-rule activity</h2><div id="rules"></div>
<h2>Latest decisions (journal replay)</h2>
<table id="decisions"><thead><tr><th>time</th><th>sym</th><th>price</th><th>RSI</th><th>action</th><th>risk rule</th><th>reason</th></tr></thead><tbody></tbody></table>
<h2>Fills (ledger)</h2>
<table id="fills"><thead><tr><th>time</th><th>side</th><th>sym</th><th>qty</th><th>price</th><th>pnl</th></tr></thead><tbody></tbody></table>
<script>
async function tick() {
  const s = await (await fetch('/api/state')).json();
  const p = s.portfolio || {};
  const positions = Object.keys(p.positions || {});
  const eq = s.equity_series.length ? s.equity_series[s.equity_series.length-1].equity : (p.cash || 0);
  const start = 150;
  const ret = ((eq - start) / start * 100);
  document.getElementById('mode').textContent = '· ' + s.mode + ' mode';
  document.getElementById('cards').innerHTML = [
    ['equity', eq.toFixed(2) + ' USDT', ret >= 0 ? 'pos' : 'neg'],
    ['return', ret.toFixed(2) + ' %', ret >= 0 ? 'pos' : 'neg'],
    ['cash', (p.cash ?? 0).toFixed(2), ''],
    ['positions', positions.length ? positions.join(' ') : 'flat', ''],
    ['fear & greed', s.fear_greed ?? '—', s.fear_greed < 20 ? 'neg' : ''],
    ['peak equity', (p.peak_equity ?? 0).toFixed(2), ''],
  ].map(([l, v, c]) => `<div class="card ${c}"><div class="v">${v}</div><div class="l">${l}</div></div>`).join('');

  document.getElementById('rules').innerHTML = Object.entries(s.rule_counts)
    .map(([r, n]) => `<span class="badge">${r} × ${n}</span>`).join('') || '<span class="badge">no gates fired</span>';

  const c = document.getElementById('chart'), ctx = c.getContext('2d');
  ctx.clearRect(0, 0, c.width, c.height);
  const es = s.equity_series;
  if (es.length > 1) {
    const vals = es.map(e => e.equity);
    const min = Math.min(...vals) - 0.1, max = Math.max(...vals) + 0.1;
    ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 1.5; ctx.beginPath();
    es.forEach((e, i) => {
      const x = i / (es.length - 1) * (c.width - 20) + 10;
      const y = c.height - 12 - (e.equity - min) / (max - min) * (c.height - 24);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
  }

  const fmt = ts => new Date(ts * 1000).toISOString().slice(5, 19).replace('T', ' ');
  document.querySelector('#decisions tbody').innerHTML = s.decisions.map(d => {
    const sig = d.signal || {}, rv = d.risk_verdict || {}, q = (d.inputs || {}).quote || {};
    const cls = sig.action === 'enter' ? 'enter' : sig.action === 'exit' ? 'exit' : 'hold';
    const ruleCls = ['token_risk_veto','stop_loss','kill_switch','daily_loss_cap','reentry_cooldown'].includes(rv.rule) ? 'veto' : '';
    return `<tr><td>${fmt(d.ts)}</td><td>${d.symbol}</td><td>${(q.price ?? 0).toFixed(2)}</td>
      <td>${sig.rsi ? sig.rsi.toFixed(1) : '—'}</td><td class="${cls}">${sig.action}</td>
      <td class="${ruleCls}">${rv.rule || ''}</td><td class="reason">${sig.reason || ''}</td></tr>`;
  }).join('');

  document.querySelector('#fills tbody').innerHTML = s.fills.map(f =>
    `<tr><td>${fmt(f.ts)}</td><td class="${f.side === 'buy' ? 'enter' : 'exit'}">${f.side}</td>
     <td>${f.symbol}</td><td>${f.qty.toFixed(6)}</td><td>${f.price.toFixed(2)}</td>
     <td class="${(f.pnl_usdt ?? 0) >= 0 ? 'enter' : 'veto'}">${f.pnl_usdt == null ? '—' : f.pnl_usdt.toFixed(3)}</td></tr>`
  ).join('') || '<tr><td colspan="6">no trades yet — gates holding</td></tr>';
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
