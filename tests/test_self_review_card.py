"""Nightly self-review scorecard: snapshot file + structured journal event +
dashboard surfacing.

The dashboard reads a once-a-day event that can't fit in any bounded journal
tail, so review.py persists the latest scorecard to self_review.json (the same
view-only pattern as strategy_state.json) and the dashboard renders it.
"""
import json
import time

from agent import config, dashboard, review
from agent.data.store import PriceStore
from agent.record.journal import Journal, read_jsonl_tail
from agent.strategy import STRATEGIES


def _seed_bars(store: PriceStore, n: int) -> None:
    """n completed hourly bars per universe symbol (all on shared buckets so
    the backtest's timestamp-intersection alignment keeps every bar)."""
    cur = int(time.time() // 3600)
    for sym in config.UNIVERSE:
        for i in range(n):
            ts = (cur - 1 - i) * 3600 + 1800       # mid-hour, strictly completed
            store.append(sym, ts, 100.0 + (i % 10))  # gentle sawtooth = some movement


def test_self_review_writes_snapshot_and_structured_event(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "SELF_REVIEW_PATH", tmp_path / "self_review.json")
    monkeypatch.setattr(review, "STRATEGY_STATE_PATH", tmp_path / "strategy_state.json")
    store = PriceStore(tmp_path / "prices.sqlite")
    _seed_bars(store, config.SELF_REVIEW_MIN_BARS + 32)  # clear the min-bars gate
    journal = Journal(tmp_path / "journal.jsonl", tmp_path / "ledger.jsonl")

    ran = review.maybe_review(review.StrategyState(), store, journal, equity=150.0)
    assert ran

    snap = json.loads((tmp_path / "self_review.json").read_text())
    assert set(snap) >= {"scorecard", "adopted", "size_factor", "switched",
                         "trailing_bars", "reviewed_day", "ts"}
    assert snap["adopted"] in STRATEGIES
    assert set(snap["scorecard"]) == set(STRATEGIES)          # every menu strategy scored
    for v in snap["scorecard"].values():
        assert set(v) == {"return", "max_drawdown", "trades", "score"}

    # the journal event carries the same structured scorecard, not just a string
    events = read_jsonl_tail(tmp_path / "journal.jsonl", 50)
    sr = next(e for e in events if e.get("event") == "self_review")
    assert sr["adopted"] == snap["adopted"]
    assert sr["scorecard"] == snap["scorecard"]
    assert sr["detail"]                                       # human-readable detail preserved


def test_dashboard_state_surfaces_self_review(tmp_path, monkeypatch):
    snap = {"reviewed_day": 100, "ts": 1.0, "iso": "2026-06-16T00:00:00Z",
            "trailing_bars": 60, "adopted": "adaptive", "size_factor": 1.0,
            "switched": False,
            "scorecard": {"adaptive": {"return": 0.01, "max_drawdown": 0.02,
                                       "trades": 3, "score": 0.01}}}
    sr_path = tmp_path / "self_review.json"
    sr_path.write_text(json.dumps(snap))
    monkeypatch.setattr(review, "SELF_REVIEW_PATH", sr_path)
    # point the dashboard's other reads at empty tmp paths so state() never
    # touches real agent data
    for name in ("PORTFOLIO_PATH", "JOURNAL_PATH", "LEDGER_PATH",
                 "RISK_STATE_PATH", "NARRATION_PATH"):
        monkeypatch.setattr(dashboard, name, tmp_path / f"{name.lower()}")

    st = dashboard.state()
    assert st["self_review"]["adopted"] == "adaptive"
    assert st["self_review"]["scorecard"]["adaptive"]["trades"] == 3


def test_dashboard_state_self_review_absent_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "SELF_REVIEW_PATH", tmp_path / "absent.json")
    for name in ("PORTFOLIO_PATH", "JOURNAL_PATH", "LEDGER_PATH",
                 "RISK_STATE_PATH", "NARRATION_PATH"):
        monkeypatch.setattr(dashboard, name, tmp_path / f"{name.lower()}")
    assert dashboard.state()["self_review"] is None   # no card until the first review
