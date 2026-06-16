"""x402 autonomous micropayment loop: pinned-route wrapper, response parsing,
and the throttle / budget / failure-isolation guards on maybe_pay.

No real payment is made (the funded wallet lives only on the VPS); subprocess
and client are faked. These pin the safety contract: off-by-default is enforced
at the runner, here we prove the money guards once enabled.
"""
import json

from agent import config, x402
from agent.execution import twak
from agent.execution.twak import TwakError


# --- response parsing ---------------------------------------------------------

def test_extract_parses_real_v3_list_shape():
    # the shape confirmed against a real x402 payment: data is a LIST of coins
    # sharing the symbol, each coin's quote is a LIST of convert objects, no tx
    price, tx = x402._extract(_ok_resp())
    assert price == 1800.0          # canonical ETH (rank 2), not the rank-None dupe
    assert tx is None               # eip3009 settles server-side: no tx in the body


def test_extract_tolerates_v2_dict_and_wrapped_tx():
    # legacy v2 dict shapes + a wrapped envelope carrying a tx are still parsed
    resp = {"payment": {"transaction": "0xabc123"},
            "data": {"data": {"ETH": [{"quote": {"USDT": {"price": 1700.0}}}]},
                     "status": {"error_code": 0}}}
    price, tx = x402._extract(resp)
    assert price == 1700.0
    assert tx == "0xabc123"


def test_extract_tolerates_unknown_shape():
    price, tx = x402._extract({"weird": "shape"})
    assert price is None and tx is None
    # a tx ref is still salvaged even when the data payload is missing
    _, tx2 = x402._extract({"transactionHash": "0xdead"})
    assert tx2 == "0xdead"


# --- twak wrapper pins the documented route -----------------------------------

def test_x402_request_builds_pinned_args(monkeypatch):
    captured = {}

    class Proc:
        returncode = 0
        stdout = '{"success": true}'
        stderr = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return Proc()

    monkeypatch.setattr(twak.subprocess, "run", fake_run)
    monkeypatch.setenv("TWAK_WALLET_PASSWORD", "secret")

    out = twak.TwakClient().x402_request("https://x/y", "base", "0xUSDC", "eip3009", "10000")
    assert out == {"success": True}
    cmd = captured["cmd"]
    assert cmd[:3] == ["twak", "x402", "request"] and cmd[3] == "https://x/y"
    for flag, val in (("--prefer-network", "base"), ("--prefer-asset", "0xUSDC"),
                      ("--prefer-method", "eip3009"), ("--max-payment", "10000")):
        assert cmd[cmd.index(flag) + 1] == val
    assert "--yes" in cmd
    assert cmd[cmd.index("--password") + 1] == "secret"
    assert cmd[-1] == "--json"        # _run always requests JSON


# --- maybe_pay guards ---------------------------------------------------------

class _Journal:
    def __init__(self):
        self.events = []

    def event(self, kind, detail, equity=None, extra=None):
        self.events.append({"event": kind, "detail": detail, "extra": extra or {}})


class _Store:
    def __init__(self, price):
        self._price = price

    def series(self, symbol, limit):
        return [self._price] if self._price is not None else []


class _Client:
    def __init__(self, resp=None, raise_exc=None):
        self.resp, self.raise_exc, self.calls = resp, raise_exc, []

    def x402_request(self, url, network, asset, method, max_payment_atomic):
        self.calls.append((url, network, asset, method, max_payment_atomic))
        if self.raise_exc:
            raise self.raise_exc
        return self.resp


class _Settings:
    def __init__(self, enabled=True, cap=2.50):
        self.x402_enabled, self.x402_max_spend_usd = enabled, cap


def _ok_resp():
    # real x402 v3 shape: data is a LIST (canonical ETH + a rank-None meme dupe),
    # each coin's quote is a LIST of convert objects, and there is no tx hash
    return {"status": {"error_code": 0},
            "data": [
                {"id": 1027, "symbol": "ETH", "name": "Ethereum", "cmc_rank": 2,
                 "quote": [{"id": 825, "symbol": "USDT", "price": 1800.0}]},
                {"id": 99999, "symbol": "ETH", "name": "scam dupe", "cmc_rank": None,
                 "quote": [{"symbol": "USDT", "price": 0.0001}]},
            ]}


def test_maybe_pay_success_accounts_and_journals(tmp_path, monkeypatch):
    monkeypatch.setattr(x402, "X402_STATE_PATH", tmp_path / "x402_state.json")
    client = _Client(resp=_ok_resp())
    journal = _Journal()
    state = x402.X402State()
    ok = x402.maybe_pay(client, _Store(1818.0), journal, _Settings(), state, now=10_000.0)

    assert ok is True
    # exactly the CMC-documented route was pinned
    assert client.calls[0] == (config.X402_QUOTE_URL, config.X402_NETWORK,
                               config.X402_ASSET, config.X402_METHOD,
                               config.X402_MAX_PAYMENT_ATOMIC)
    assert state.calls == 1 and state.spent_usd == config.X402_COST_USD
    assert state.last_tx is None                              # eip3009: no tx in body
    assert state.last_price == 1800.0 and state.last_primary_price == 1818.0
    assert abs(state.last_delta_pct - (-0.99)) < 0.2          # cross-check delta
    pay = [e for e in journal.events if e["event"] == "x402_payment"]
    assert len(pay) == 1 and pay[0]["extra"]["paid_price"] == 1800.0
    # persisted for the dashboard to read O(1)
    assert json.loads((tmp_path / "x402_state.json").read_text())["calls"] == 1


def test_maybe_pay_throttled_when_not_due(tmp_path, monkeypatch):
    monkeypatch.setattr(x402, "X402_STATE_PATH", tmp_path / "s.json")
    client = _Client(resp=_ok_resp())
    state = x402.X402State(last_ts=10_000.0)
    ok = x402.maybe_pay(client, _Store(1.0), _Journal(), _Settings(), state, now=10_060.0)  # +60s
    assert ok is False and client.calls == []                # no payment attempted


def test_maybe_pay_stops_at_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(x402, "X402_STATE_PATH", tmp_path / "s.json")
    client = _Client(resp=_ok_resp())
    state = x402.X402State(spent_usd=2.50, last_ts=0.0)       # already at the $2.50 cap
    ok = x402.maybe_pay(client, _Store(1.0), _Journal(), _Settings(cap=2.50), state, now=10_000.0)
    assert ok is False and client.calls == []
    assert state.budget_logged is True


def test_maybe_pay_failure_is_contained(tmp_path, monkeypatch):
    monkeypatch.setattr(x402, "X402_STATE_PATH", tmp_path / "s.json")
    client = _Client(raise_exc=TwakError("boom"))
    journal = _Journal()
    state = x402.X402State(last_ts=0.0)
    ok = x402.maybe_pay(client, _Store(1.0), journal, _Settings(), state, now=10_000.0)
    assert ok is False
    assert state.spent_usd == 0.0 and state.calls == 0       # no spend on failure
    assert state.last_ts == 10_000.0                         # throttle advanced (no retry storm)
    assert any(e["event"] == "x402_error" for e in journal.events)
