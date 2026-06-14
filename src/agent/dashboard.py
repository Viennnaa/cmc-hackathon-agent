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
from agent.narrator import NARRATION_PATH
from agent.record.journal import read_jsonl_tail
from agent.runner import JOURNAL_PATH, LEDGER_PATH, PORTFOLIO_PATH

PORT = 8765

# enough journal tail for a multi-day equity curve (6 symbols x 60s polls
# ≈ 8.6k lines/day) without slurping the whole unbounded file every 5s poll
JOURNAL_TAIL_LINES = 20_000
EQUITY_MAX_POINTS = 600


def _read_jsonl(path: Path, limit: int = 200) -> list[dict]:
    return read_jsonl_tail(path, limit)  # torn tail writes are skipped inside


def state() -> dict:
    portfolio = json.loads(PORTFOLIO_PATH.read_text()) if PORTFOLIO_PATH.exists() else {}
    journal = _read_jsonl(JOURNAL_PATH, limit=JOURNAL_TAIL_LINES)
    ledger = _read_jsonl(LEDGER_PATH, limit=100)

    rule_counts: dict[str, int] = {}
    rule_details: dict[str, str] = {}  # latest detail string per gate (for hover info)
    equity_series = []
    fear_greed = None
    for rec in journal:
        if "risk_verdict" in rec:
            rule = rec["risk_verdict"].get("rule")
            if rule and rule not in ("no_action", "position_sizing", "strategy_exit"):
                rule_counts[rule] = rule_counts.get(rule, 0) + 1
                detail = rec["risk_verdict"].get("detail")
                if detail:
                    rule_details[rule] = detail
            if rec.get("equity") is not None:
                equity_series.append({"ts": rec["ts"], "equity": rec["equity"]})
            fg = (rec.get("inputs") or {}).get("fear_greed")
            if fg is not None:
                fear_greed = fg
        elif "event" in rec:
            rule_counts[rec["event"]] = rule_counts.get(rec["event"], 0) + 1
            if rec.get("detail"):
                rule_details[rec["event"]] = rec["detail"]

    # full-resolution drawdown BEFORE downsampling: the client only sees a
    # downsampled curve and would miss intra-gap troughs on the headline KPI
    peak, dd = float("-inf"), 0.0
    for pt in equity_series:
        peak = max(peak, pt["equity"])
        if peak > 0:
            dd = max(dd, (peak - pt["equity"]) / peak)
    drawdown_samples = len(equity_series)

    # downsample for the chart: judges care about the whole window's shape
    # (return + max drawdown), not per-tick noise
    if len(equity_series) > EQUITY_MAX_POINTS:
        step = -(-len(equity_series) // EQUITY_MAX_POINTS)
        equity_series = equity_series[::step] + [equity_series[-1]]

    # judged return baseline: live_rebase wallet amount once live, else the
    # configured starting capital (pre-upgrade state files lack the field)
    baseline = portfolio.get("baseline_equity") or config.get_settings().starting_capital

    return {
        "now": time.time(),
        "mode": config.get_settings().mode,
        "universe": config.UNIVERSE,
        "portfolio": portfolio,
        "baseline": round(baseline, 2),
        "max_drawdown_pct": round(dd * 100, 2),
        "drawdown_samples": drawdown_samples,
        "fear_greed": fear_greed,
        "rule_counts": rule_counts,
        "rule_details": rule_details,
        "equity_series": equity_series,
        "decisions": [r for r in journal if "signal" in r][-40:][::-1],
        "fills": ledger[::-1],
        "narration": _read_jsonl(NARRATION_PATH, limit=40)[::-1],
    }


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CMC Disciplined Trader</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23F59E0B'/%3E%3Cpath d='M12 42 28 26 36 34 52 18' stroke='%230F172A' stroke-width='7' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3Cpath d='M40 18h12v12' stroke='%230F172A' stroke-width='7' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
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
  .note{display:flex;gap:14px;padding:9px 0;border-bottom:1px solid var(--muted)}
  .note:last-child{border-bottom:none}
  .note .ts{color:var(--fg3);font-size:11px;white-space:nowrap;padding-top:3px}
  .note p{margin:0;font-size:13px;line-height:1.55;color:var(--fg)}
  footer{font-size:11.5px;color:var(--fg3);text-align:center}
  .deck{display:grid;grid-template-columns:minmax(0,2fr) minmax(280px,1fr);gap:16px;align-items:start}
  .rail{display:flex;flex-direction:column;gap:16px;min-width:0}
  .duo{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}
  .deck>*,.duo>*{min-width:0}
  .gate[data-gate]{cursor:pointer}
  .gate.active{border-color:var(--gold);color:var(--fg)}
  .gate.active b{color:var(--gold)}
  .gate-detail{padding:2px 18px 16px;font-size:12.5px;line-height:1.55;color:var(--fg2)}
  .gate-detail[hidden]{display:none}
  .gate-detail b{color:var(--fg);font-weight:600}
  .gate-detail .gd-latest{display:block;margin-top:6px;font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-size:11px;color:var(--fg3);word-break:break-word}
  @media (max-width:980px){.deck,.duo{grid-template-columns:1fr}}
  .scroll-cap{max-height:360px;overflow:auto}
  .scroll-cap thead th{position:sticky;top:0;background:var(--surface);z-index:2}
  .narration-scroll{max-height:300px;overflow-y:auto}
  .pos-list{display:flex;flex-direction:column;gap:9px}
  .pos-item{background:var(--muted);border:1px solid var(--border);border-radius:9px;padding:9px 11px}
  .pos-top{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
  .pos-sym{font-weight:600;font-size:13px;letter-spacing:.01em}
  .pos-pnl{font-size:12px;font-weight:600;font-variant-numeric:tabular-nums}
  .pos-sub{display:flex;justify-content:space-between;gap:8px;margin-top:4px;font-size:11px;color:var(--fg2);font-variant-numeric:tabular-nums}
  .pos-empty{color:var(--fg2);font-size:12.5px;padding:6px 2px}
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

<div class="deck">
  <div class="card table-card">
    <div class="card-h"><h2>Equity curve <span class="unit">USDT</span></h2><span class="meta num" id="chartmeta"></span></div>
    <div class="pad" id="chartwrap">
      <canvas id="chart" role="img" aria-label="Equity over time"></canvas>
      <div id="xline" aria-hidden="true"></div><div id="tip" aria-hidden="true"></div>
      <div class="empty" id="chartempty" style="display:none">Collecting equity data &mdash; first points arrive within a couple of polls.</div>
    </div>
  </div>
  <div class="rail">
    <div class="card table-card">
      <div class="card-h"><h2>Open positions</h2><span class="meta num" id="posmeta"></span></div>
      <div class="pad"><div class="pos-list" id="positions"></div></div>
    </div>
    <div class="card table-card">
      <div class="card-h"><h2>Risk-gate activity</h2><span class="meta">risk gates &amp; nightly reviews &middot; click to expand</span></div>
      <div class="pad gates" id="rules"></div>
      <div class="gate-detail" id="gate-detail" hidden></div>
    </div>
  </div>
</div>

<div class="card table-card" id="narration-card" style="display:none">
  <div class="card-h"><h2>Agent commentary</h2><span class="meta">observe-only &middot; never trades</span></div>
  <div class="pad narration-scroll" id="narration"></div>
</div>

<div class="duo">
  <div class="card table-card">
    <div class="card-h"><h2>Latest decisions</h2><span class="meta">journal &middot; newest first &middot; UTC</span></div>
    <div class="table-wrap scroll-cap"><table id="decisions">
      <thead><tr><th>time</th><th>asset</th><th class="r">price</th><th class="r">rsi</th><th>action</th><th>risk rule</th><th>reason</th></tr></thead>
      <tbody></tbody></table></div>
  </div>
  <div class="card table-card">
    <div class="card-h"><h2>Fills</h2><span class="meta">ledger &middot; newest first &middot; UTC</span></div>
    <div class="table-wrap scroll-cap"><table id="fills">
      <thead><tr><th>time</th><th>side</th><th>asset</th><th class="r">qty</th><th class="r">price</th><th class="r">pnl (usdt)</th></tr></thead>
      <tbody></tbody></table></div>
  </div>
</div>

<footer>Deterministic strategy core &middot; hard risk gates &middot; full decision log &mdash; this dashboard reads journal.jsonl, ledger.jsonl and portfolio.json directly; it is a view, never a second source of truth.</footer>
</div>
<script>
let BASELINE = 150;  // judged baseline; replaced by /api/state's value
const VETO_RULES = ['token_risk_veto','stop_loss','kill_switch','daily_loss_cap','reentry_cooldown'];
// plain-English gloss per gate/event — surfaced as a hover tooltip so the chips
// explain themselves (they are counters, not buttons; clicking does nothing)
const GATE_HELP = {
  self_review: 'Nightly strategy self-review: replays every strategy over the trailing window and adopts the best performer. Narrow-only — it can shrink entry size but never loosen a risk limit.',
  stop_loss: 'Per-position stop: exits when price falls 3% below entry.',
  daily_loss_cap: 'Flatten everything and halt for 24h after a 5% daily loss.',
  kill_switch: 'Flatten and permanently stop at 10% drawdown from peak equity.',
  reentry_cooldown: 'Blocks re-entering a symbol for 8 bars after an exit (anti-churn).',
  sentiment_veto: 'No new entries while Fear & Greed is in extreme fear (below 20).',
  single_position: 'Already holding this symbol — no duplicate entry.',
  max_concurrent: 'Position-count cap reached (max 3 concurrent).',
  insufficient_cash: 'Not enough cash to fund the sized entry.',
  daily_halt: 'Entries paused during the post-loss 24h halt window.',
  token_risk_veto: 'On-chain token-risk check blocked the entry.',
};
const SHIELD = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>';
let lastFetch = 0, lastState = null, pts = [], selectedGate = null;

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
  if (s.baseline) BASELINE = s.baseline;
  const es = s.equity_series;
  const vals = es.map(e => e.equity);
  const eq = vals.length ? vals[vals.length - 1] : (p.cash || 0);
  const ret = (eq - BASELINE) / BASELINE * 100;
  const dd = s.max_drawdown_pct ?? (vals.length ? maxDrawdown(vals) : 0);
  const fg = s.fear_greed;
  const [zone, zcol] = fg != null ? fgZone(fg) : ['no reading yet', 'var(--fg2)'];

  const m = document.getElementById('mode');
  m.textContent = s.mode.toUpperCase();
  m.className = 'chip ' + (s.mode === 'live' ? 'live' : 'paper');

  // Open positions detail for the sidebar panel. Everything here is already in
  // portfolio.json (qty, entry_price, last_prices) — a pure view, no new source
  // of truth. The KPI card stays a bare count; the breakdown lives in the panel.
  const marks = p.last_prices || {};
  let totalUpl = 0;
  const posList = positions.map(sym => {
    const ps = (p.positions || {})[sym] || {};
    const mark = marks[sym] ?? ps.entry_price;
    const cost = (ps.qty || 0) * (ps.entry_price || 0);
    const cur = (ps.qty || 0) * mark;
    const upl = cur - cost; totalUpl += upl;
    const pct = cost ? upl / cost * 100 : 0;
    const cls = upl >= 0 ? 'gain' : 'loss', sg = upl >= 0 ? '+' : '';
    return `<div class="pos-item">
      <div class="pos-top"><span class="pos-sym">${esc(sym)}</span>
        <span class="pos-pnl ${cls}">${sg}${upl.toFixed(2)} (${sg}${pct.toFixed(2)}%)</span></div>
      <div class="pos-sub"><span>${(ps.qty || 0).toFixed(6)} @ ${(ps.entry_price || 0).toFixed(2)}</span>
        <span>${cost.toFixed(2)} &rarr; ${cur.toFixed(2)}</span></div></div>`;
  }).join('');

  document.getElementById('cards').innerHTML = [
    {l:'Equity', v:`${eq.toFixed(2)}<span class="unit">USDT</span>`, s:`peak ${(p.peak_equity ?? eq).toFixed(2)}`},
    {l:'Return', v:`${ret >= 0 ? '+' : ''}${ret.toFixed(2)}<span class="unit">%</span>`, c:ret >= 0 ? 'pos' : 'neg', s:`since baseline &middot; ${BASELINE} USDT`},
    {l:'Cash', v:`${(p.cash ?? 0).toFixed(2)}<span class="unit">USDT</span>`, s:positions.length ? `${(eq - (p.cash ?? 0)).toFixed(2)} deployed` : 'fully in cash'},
    {l:'Open positions', v:String(positions.length), s:positions.length ? esc(positions.join(' · ')) : 'flat &mdash; waiting for signal'},
    {l:'Max drawdown', v:`${dd.toFixed(2)}<span class="unit">%</span>`, c:dd > 5 ? 'neg' : '', s:`full-res over ${s.drawdown_samples ?? vals.length} samples`},
    {l:'Fear &amp; Greed', v:fg ?? '&mdash;', s:`<span style="color:${zcol}">${zone}</span>`,
     extra:fg != null ? `<div class="gauge" aria-hidden="true"><i style="left:${Math.min(Math.max(fg, 0), 100)}%"></i></div>` : ''},
  ].map(k => `<div class="card kpi ${k.c || ''}"><div class="l">${k.l}</div><div class="v num">${k.v}</div><div class="s">${k.s}</div>${k.extra || ''}</div>`).join('');

  document.getElementById('positions').innerHTML = positions.length ? posList
    : '<div class="pos-empty">Flat &mdash; waiting for a signal.</div>';
  const tcls = totalUpl >= 0 ? 'gain' : 'loss', tsg = totalUpl >= 0 ? '+' : '';
  document.getElementById('posmeta').innerHTML = positions.length
    ? `<span class="${tcls}">${tsg}${totalUpl.toFixed(2)} unrealized</span>` : '';

  document.getElementById('rules').innerHTML = Object.entries(s.rule_counts)
    .sort((a, b) => b[1] - a[1])
    .map(([r, n]) => {
      const detail = (s.rule_details || {})[r] || '';
      let tip = GATE_HELP[r] || '';
      if (detail) tip += (tip ? '\\n\\nLatest: ' : '') + detail;
      const active = r === selectedGate ? ' active' : '';
      return `<span class="gate ${VETO_RULES.includes(r) ? 'veto' : ''}${active}" data-gate="${esc(r)}" role="button" tabindex="0"${tip ? ` title="${esc(tip)}"` : ''}>${SHIELD}${esc(r).replace(/_/g, ' ')} <b class="num">&times;${n}</b></span>`;
    })
    .join('') || `<span class="gate">${SHIELD}no gates fired yet &mdash; entries passing clean</span>`;
  // keep the expanded detail in sync across the 5s re-render (or drop it if the gate aged out)
  if (selectedGate && s.rule_counts[selectedGate]) renderGateDetail(selectedGate);
  else { selectedGate = null; const gd = document.getElementById('gate-detail'); gd.hidden = true; gd.innerHTML = ''; }

  const notes = s.narration || [];
  document.getElementById('narration-card').style.display = notes.length ? '' : 'none';
  document.getElementById('narration').innerHTML = notes.map(n =>
    `<div class="note"><span class="ts num">${fmtS(n.ts)}</span><p>${esc(n.text)}</p></div>`).join('');

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
  let lo = Math.min(...vals, BASELINE), hi = Math.max(...vals, BASELINE);
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

  const by = Y(BASELINE);
  x.setLineDash([4, 4]); x.strokeStyle = 'rgba(148,163,184,.55)';
  x.beginPath(); x.moveTo(PL, by); x.lineTo(w - PR, by); x.stroke();
  x.setLineDash([]);
  x.textAlign = 'left'; x.fillStyle = '#94A3B8'; x.fillText('baseline ' + BASELINE, PL + 4, by - 9);

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
// risk-gate chips are clickable: expand a detail line (gloss + latest journal
// detail). #rules re-renders every 5s, so the listener lives on the stable
// parent and selectedGate persists the open chip across refreshes.
function renderGateDetail(gate) {
  const gd = document.getElementById('gate-detail');
  const help = GATE_HELP[gate] || 'no description available';
  const detail = ((lastState && lastState.rule_details) || {})[gate] || '';
  gd.innerHTML = `<b>${esc(gate.replace(/_/g, ' '))}</b> &mdash; ${esc(help)}`
    + (detail ? `<span class="gd-latest">Latest: ${esc(detail)}</span>` : '');
  gd.hidden = false;
}
const rulesEl = document.getElementById('rules');
rulesEl.addEventListener('click', ev => {
  const chip = ev.target.closest('[data-gate]');
  if (!chip) return;
  const g = chip.getAttribute('data-gate');
  selectedGate = selectedGate === g ? null : g;
  const gd = document.getElementById('gate-detail');
  if (selectedGate) renderGateDetail(selectedGate);
  else { gd.hidden = true; gd.innerHTML = ''; }
  document.querySelectorAll('#rules .gate').forEach(el =>
    el.classList.toggle('active', el.getAttribute('data-gate') === selectedGate));
});
rulesEl.addEventListener('keydown', ev => {
  if (ev.key === 'Enter' || ev.key === ' ') {
    const chip = ev.target.closest('[data-gate]');
    if (chip) { ev.preventDefault(); chip.click(); }
  }
});

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
