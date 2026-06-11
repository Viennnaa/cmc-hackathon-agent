"""Narrator must observe only: correct digests, and total silence when disabled."""

import json

from agent import narrator
from agent.execution.portfolio import Portfolio, Position
from agent.risk.engine import RiskEngine


def _setup_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(narrator, "JOURNAL_PATH", tmp_path / "journal.jsonl")
    monkeypatch.setattr(narrator, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(narrator, "NARRATION_PATH", tmp_path / "narration.jsonl")


def test_disabled_without_api_key(tmp_path, monkeypatch):
    _setup_paths(tmp_path, monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(narrator, "_narrate", lambda d: called.append(d) or "text")

    narrator.maybe_narrate(Portfolio(cash=150.0), RiskEngine())
    assert called == []
    assert not (tmp_path / "narration.jsonl").exists()


def test_digest_summarizes_artifacts(tmp_path, monkeypatch):
    _setup_paths(tmp_path, monkeypatch)
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        json.dumps({"ts": 1.0, "symbol": "BNB", "signal": {"action": "hold"},
                    "risk_verdict": {"rule": "no_action"},
                    "inputs": {"fear_greed": 16}}) + "\n" +
        json.dumps({"ts": 2.0, "event": "token_risk_veto", "detail": "x"}) + "\n" +
        "{torn line\n"
    )
    (tmp_path / "ledger.jsonl").write_text(
        json.dumps({"ts": 3.0, "symbol": "BNB", "side": "buy", "qty": 0.05,
                    "price": 600.0, "pnl_usdt": None}) + "\n")

    p = Portfolio(cash=120.0, mode="paper")
    p.positions["BNB"] = Position("BNB", 0.05, 600.0, 3.0)
    d = narrator.build_digest(p, RiskEngine())

    assert d["mode"] == "paper"
    assert d["fear_greed_index"] == 16
    assert d["recent_decision_actions"] == {"hold": 1}
    assert d["recent_rule_firings"] == {"no_action": 1, "token_risk_veto": 1}
    assert d["open_positions"] == {"BNB": 0.05}
    assert len(d["recent_fills"]) == 1
    assert d["kill_switch_engaged"] is False


def test_narrates_on_new_fill_and_appends(tmp_path, monkeypatch):
    _setup_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(narrator, "_narrate", lambda d: "Agent is holding in extreme fear.")
    monkeypatch.setattr(narrator, "_state", {"last_ts": 0.0, "fills_seen": -1, "events_seen": -1})

    narrator.maybe_narrate(Portfolio(cash=150.0), RiskEngine())  # baseline call narrates
    lines = (tmp_path / "narration.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == "Agent is holding in extreme fear."

    # no new fills/events and interval not elapsed -> silent
    narrator.maybe_narrate(Portfolio(cash=150.0), RiskEngine())
    assert len((tmp_path / "narration.jsonl").read_text().strip().splitlines()) == 1

    # a new fill triggers immediate narration
    (tmp_path / "ledger.jsonl").write_text(json.dumps({"ts": 4.0, "side": "buy"}) + "\n")
    narrator.maybe_narrate(Portfolio(cash=150.0), RiskEngine())
    assert len((tmp_path / "narration.jsonl").read_text().strip().splitlines()) == 2
