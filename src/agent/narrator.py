"""Observe-only LLM narrator: plain-English commentary for the dashboard.

Reads the same artifacts judges replay and appends narration.jsonl. The LLM
observes and explains; it NEVER influences trading — the deterministic core
and risk gates are upstream and unaware of it, and any failure here is
swallowed by the runner. Disabled entirely unless ANTHROPIC_API_KEY is set.

Narrates on a slow clock (hourly by default) plus immediately after notable
events (fills, risk-gate firings), so the dashboard always explains itself.
"""

import json
import logging
import os
import time

from agent import config
from agent.execution.portfolio import Portfolio
from agent.record.journal import read_jsonl_tail
from agent.risk.engine import RiskEngine

log = logging.getLogger("agent.narrator")

NARRATION_PATH = config.DATA_DIR / "narration.jsonl"
JOURNAL_PATH = config.DATA_DIR / "journal.jsonl"
LEDGER_PATH = config.DATA_DIR / "ledger.jsonl"

SYSTEM = (
    "You are the observability narrator for 'CMC Disciplined Trader', an autonomous "
    "crypto trading agent on BNB Smart Chain competing in a judged hackathon. From the "
    "telemetry digest provided, explain in plain English what the agent has been doing "
    "and why: which risk gates fired, why it is or isn't trading, and how the portfolio "
    "looks. You only observe — you never advise, predict markets, or decide trades. "
    "Write 2-4 sentences for judges glancing at a dashboard. Be concrete with the "
    "numbers in the digest. No preamble, no headers, no bullet points."
)

# narration cadence state (in-memory; a restart just narrates once more)
_state = {"last_ts": 0.0, "fills_seen": -1, "events_seen": -1}


# the journal grows unbounded over the window — tail from EOF, never slurp
_read_jsonl_tail = read_jsonl_tail


def _count_lines(path) -> int:
    """Ledger line count (fills only — stays small enough to read whole)."""
    if not path.exists():
        return 0
    return len(path.read_text().strip().splitlines())


def build_digest(portfolio: Portfolio, risk: RiskEngine, tail: int = 120) -> dict:
    """Compact, numbers-first telemetry summary — the LLM's only input."""
    journal = _read_jsonl_tail(JOURNAL_PATH, tail)
    fills = _read_jsonl_tail(LEDGER_PATH, 5)

    actions: dict[str, int] = {}
    rules: dict[str, int] = {}
    fear_greed = None
    for rec in journal:
        if "risk_verdict" in rec:
            sig = rec.get("signal") or {}
            actions[sig.get("action", "?")] = actions.get(sig.get("action", "?"), 0) + 1
            rule = (rec.get("risk_verdict") or {}).get("rule")
            if rule:
                rules[rule] = rules.get(rule, 0) + 1
            fg = (rec.get("inputs") or {}).get("fear_greed")
            if fg is not None:
                fear_greed = fg
        elif "event" in rec:
            rules[rec["event"]] = rules.get(rec["event"], 0) + 1

    return {
        "utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
        "mode": portfolio.mode or "paper",
        "equity_usdt": round(portfolio.equity(), 2),
        "cash_usdt": round(portfolio.cash, 2),
        "peak_equity": round(portfolio.peak_equity, 2),
        "open_positions": {s: round(p.qty, 6) for s, p in portfolio.positions.items()},
        "universe": config.UNIVERSE,
        "fear_greed_index": fear_greed,
        "fear_greed_entry_veto_below": config.FEAR_GREED_VETO_BELOW,
        "kill_switch_engaged": risk.killed,
        "daily_halt_active": time.time() < risk.halted_until,
        "recent_decision_actions": actions,
        "recent_rule_firings": rules,
        "recent_fills": fills,
        "declared_rules": {
            "max_position_pct": config.MAX_POSITION_PCT,
            "max_concurrent_positions": config.MAX_CONCURRENT_POSITIONS,
            "stop_loss_pct": config.STOP_LOSS_PCT,
            "daily_loss_cap_pct": config.DAILY_LOSS_CAP_PCT,
            "kill_switch_drawdown_pct": config.KILL_SWITCH_DRAWDOWN_PCT,
        },
    }


def _narrate(digest: dict) -> str:
    import anthropic  # lazy: trading must work without the narrator dep configured

    client = anthropic.Anthropic(timeout=30.0, max_retries=1)
    msg = client.messages.create(
        model=os.getenv("NARRATOR_MODEL", "claude-opus-4-8"),
        max_tokens=300,
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(digest, sort_keys=True)}],
    )
    if msg.stop_reason == "refusal":
        return ""
    return next((b.text for b in msg.content if b.type == "text"), "").strip()


def maybe_narrate(portfolio: Portfolio, risk: RiskEngine) -> None:
    """Called once per tick by the runner; cheap no-op almost always.

    Narrates when (a) a fill or risk-gate event happened since the last
    narration, or (b) the interval elapsed. Raises nothing fatal — the
    runner wraps this in a broad except as well.
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        return

    fills_now = _count_lines(LEDGER_PATH)
    events_now = sum(1 for r in _read_jsonl_tail(JOURNAL_PATH, 200) if "event" in r)
    if _state["fills_seen"] < 0:  # first call after start: baseline, narrate once
        eventful = True
    else:
        eventful = fills_now > _state["fills_seen"] or events_now > _state["events_seen"]

    interval = int(os.getenv("NARRATOR_INTERVAL_SECONDS", "3600"))
    now = time.time()
    if not eventful and now - _state["last_ts"] < interval:
        return

    text = _narrate(build_digest(portfolio, risk))
    _state.update(last_ts=now, fills_seen=fills_now, events_seen=events_now)
    if not text:
        return
    NARRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with NARRATION_PATH.open("a") as f:
        f.write(json.dumps({"ts": now, "text": text}) + "\n")
    log.info("narration: %s", text[:120])
