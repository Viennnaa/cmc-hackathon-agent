"""Crash-safety invariants: the judged rules must survive process restarts."""

import json

from agent import config
from agent.execution import pending
from agent.execution.portfolio import Portfolio, Position
from agent.execution.twak import find_usdt_balance
from agent.risk.engine import RiskEngine


def test_risk_state_roundtrip(tmp_path):
    path = tmp_path / "risk_state.json"
    eng = RiskEngine()
    eng.killed = True
    eng.halted_until = 1750000000.0
    eng.note_exit("BNB", now=1749000000.0)
    eng.save(path)

    loaded = RiskEngine.load(path)
    assert loaded.killed is True
    assert loaded.halted_until == 1750000000.0
    assert loaded.last_exit == {"BNB": 1749000000.0}


def test_risk_state_missing_file_is_fresh(tmp_path):
    eng = RiskEngine.load(tmp_path / "absent.json")
    assert eng.killed is False
    assert eng.halted_until == 0.0


def test_kill_switch_survives_restart(tmp_path):
    """The exact codex finding: restart must not re-arm a killed agent."""
    path = tmp_path / "risk_state.json"
    eng = RiskEngine()
    p = Portfolio(cash=100.0)
    p.peak_equity = 150.0  # 33% drawdown from peak -> kill
    verdict = eng.portfolio_gates(p)
    assert verdict is not None and verdict.rule == "kill_switch"
    eng.save(path)

    restarted = RiskEngine.load(path)
    assert restarted.killed is True
    assert restarted.review("enter", "BNB", p).approved is False


def test_pending_order_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(pending, "PENDING_PATH", tmp_path / "pending_order.json")
    assert pending.read() is None
    pending.write("buy", "BNB", 30.0)
    rec = pending.read()
    assert rec["side"] == "buy" and rec["symbol"] == "BNB" and rec["amount"] == 30.0
    pending.clear()
    assert pending.read() is None
    pending.clear()  # idempotent


def test_pending_corrupt_file_still_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(pending, "PENDING_PATH", tmp_path / "pending_order.json")
    (tmp_path / "pending_order.json").write_text("{torn write")
    assert pending.read() == {"corrupt": True}  # not-None -> runner refuses to trade


def test_portfolio_save_is_atomic_and_roundtrips(tmp_path):
    path = tmp_path / "portfolio.json"
    p = Portfolio(cash=120.0, mode="live")
    p.positions["BNB"] = Position("BNB", 0.05, 600.0, 1749000000.0)
    p.save(path)
    assert not path.with_suffix(".tmp").exists()

    loaded = Portfolio.load(path, starting_capital=150.0)
    assert loaded.cash == 120.0
    assert loaded.mode == "live"
    assert loaded.positions["BNB"].qty == 0.05


def test_portfolio_load_legacy_file_without_mode(tmp_path):
    path = tmp_path / "portfolio.json"
    data = {"cash": 150.0, "positions": {}, "peak_equity": 150.0,
            "day_start_equity": 150.0, "day_start_ts": 1.0, "last_prices": {}}
    path.write_text(json.dumps(data))
    loaded = Portfolio.load(path, starting_capital=150.0)
    assert loaded.mode == ""


def test_find_usdt_balance_shapes():
    # flat list of token dicts
    assert find_usdt_balance({"tokens": [
        {"symbol": "BNB", "balance": "0.4"},
        {"symbol": "USDT", "balance": "101.5"},
    ]}) == 101.5
    # amount-with-unit string, nested deeper
    assert find_usdt_balance({"chains": {"bsc": [
        {"asset": "usdt", "amount": "99.2 USDT"},
    ]}}) == 99.2
    # no USDT anywhere -> None (caller fails closed)
    assert find_usdt_balance({"tokens": [{"symbol": "BNB", "balance": 1}]}) is None


def test_stale_quote_config_sane():
    # protective-exit budget must exceed quote staleness so a single stale
    # poll cannot instantly flatten
    assert config.STALE_QUOTE_FLATTEN_SECONDS >= config.STALE_QUOTE_MAX_AGE_SECONDS
