import json
import math
import time

from agent import config, review
from agent.data.store import PriceStore
from agent.record.journal import Journal
from agent.strategy import STRATEGIES


def make_store(tmp_path, hours=24 * 7):
    """A week of synthetic hourly closes for the whole universe."""
    store = PriceStore(tmp_path / "prices.sqlite")
    now = time.time()
    start = now - hours * 3600
    for sym in config.UNIVERSE:
        for i in range(hours):
            # gentle oscillating drift, ends before the current (open) bucket
            price = 100 + 5 * math.sin(i / 9) + i * 0.01
            store.append(sym, start + i * 3600, price)
    return store


def test_state_narrow_only_clamp(tmp_path):
    path = tmp_path / "strategy_state.json"
    path.write_text(json.dumps({"strategy": "momentum", "size_factor": 2.5,
                                "reviewed_day": 1}))
    state = review.StrategyState.load(path)
    assert state.size_factor == 1.0  # widened factor clamped on load


def test_state_unknown_strategy_falls_back(tmp_path):
    path = tmp_path / "strategy_state.json"
    path.write_text(json.dumps({"strategy": "yolo_leverage", "size_factor": 1.0}))
    state = review.StrategyState.load(path)
    assert state.strategy in STRATEGIES


def test_review_runs_once_per_day(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "STRATEGY_STATE_PATH", tmp_path / "s.json")
    store = make_store(tmp_path)
    journal = Journal(tmp_path / "j.jsonl", tmp_path / "l.jsonl")
    state = review.StrategyState()

    assert review.maybe_review(state, store, journal) is True
    assert state.strategy in STRATEGIES
    assert state.size_factor <= 1.0
    assert state.reviewed_day == int(time.time() // 86_400)
    # journaled scorecard
    lines = [json.loads(l) for l in (tmp_path / "j.jsonl").read_text().splitlines()]
    assert any(r.get("event") == "self_review" for r in lines)
    # same day: no second run
    assert review.maybe_review(state, store, journal) is False


def test_review_never_switches_on_a_tie(tmp_path, monkeypatch):
    # flat prices: no strategy trades, all scores identical -> keep incumbent
    monkeypatch.setattr(review, "STRATEGY_STATE_PATH", tmp_path / "s.json")
    store = PriceStore(tmp_path / "prices.sqlite")
    now = time.time()
    for sym in config.UNIVERSE:
        for i in range(24 * 7):
            store.append(sym, now - (24 * 7 - i) * 3600, 100.0)
    journal = Journal(tmp_path / "j.jsonl", tmp_path / "l.jsonl")
    state = review.StrategyState()  # incumbent = DEFAULT_STRATEGY (adaptive)

    assert review.maybe_review(state, store, journal) is True
    assert state.strategy == review.DEFAULT_STRATEGY


def test_review_skips_thin_history_but_burns_the_day(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "STRATEGY_STATE_PATH", tmp_path / "s.json")
    store = make_store(tmp_path, hours=10)  # below SELF_REVIEW_MIN_BARS
    journal = Journal(tmp_path / "j.jsonl", tmp_path / "l.jsonl")
    state = review.StrategyState()
    before = state.strategy

    assert review.maybe_review(state, store, journal) is False
    assert state.strategy == before  # unchanged
    assert state.reviewed_day == int(time.time() // 86_400)  # one attempt per day
