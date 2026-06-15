"""Guards added after the 2026-06-12 independent review: secret redaction,
quote plausibility, live-start refusals (gas / holdings / host), quote-gap
persistence, and the journal tail reader."""

import json
import subprocess

import pytest

from agent import config, runner
from agent.data.cmc import CMCClient
from agent.execution.portfolio import Portfolio, Position
from agent.execution.twak import TwakClient, TwakError, _run, find_balance
from agent.record.journal import Journal, read_jsonl_tail

PASSWORD = "hunter2-super-secret"


# --- B1: the wallet password must never reach an exception/journal ------------

def _fail_run(monkeypatch, stderr: str):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr=stderr)
    monkeypatch.setattr(subprocess, "run", fake_run)


def test_password_redacted_from_failed_swap(monkeypatch):
    monkeypatch.setenv("TWAK_WALLET_PASSWORD", PASSWORD)
    # CLI echoing its argv back is the worst case — both argv and stderr leak
    _fail_run(monkeypatch, f"bad args: swap --password {PASSWORD}")
    with pytest.raises(TwakError) as err:
        TwakClient().swap(30.0, "USDT", "BNB")
    assert PASSWORD not in str(err.value)
    assert "***" in str(err.value)


def test_timeout_is_sanitized_twak_error(monkeypatch):
    monkeypatch.setenv("TWAK_WALLET_PASSWORD", PASSWORD)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TwakError) as err:
        TwakClient().swap(30.0, "USDT", "BNB")
    assert PASSWORD not in str(err.value)
    # the chain is cut: TimeoutExpired.cmd holds the raw argv
    assert err.value.__cause__ is None
    assert err.value.__context__ is None or PASSWORD not in str(err.value.__context__)


def test_non_json_output_sanitized(monkeypatch):
    monkeypatch.setenv("TWAK_WALLET_PASSWORD", PASSWORD)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0,
                                           stdout=f"echo {PASSWORD}", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(TwakError) as err:
        _run(["swap", "30", "USDT", "BNB", "--password", PASSWORD])
    assert PASSWORD not in str(err.value)


# --- B2: quote plausibility ----------------------------------------------------

def test_first_sample_trusted_then_jump_quarantined():
    c = CMCClient("test-key")
    assert c._implausible("BNB", 600.0) is None  # no history: baseline
    c._last_accepted["BNB"] = 600.0
    assert c._implausible("BNB", 615.0) is None          # 2.5% move: fine
    assert c._implausible("BNB", 900.0) is not None      # 50% spike: quarantined
    # glitch resolved: next poll back near the accepted price
    assert c._implausible("BNB", 603.0) is None


def test_real_move_confirmed_by_second_poll():
    c = CMCClient("test-key")
    c._last_accepted["BNB"] = 600.0
    assert c._implausible("BNB", 900.0) is not None      # first sighting held back
    assert c._implausible("BNB", 905.0) is None          # second poll agrees: real


def test_nonpositive_prices_always_dropped():
    c = CMCClient("test-key")
    assert c._implausible("BNB", 0.0) is not None
    assert c._implausible("BNB", -3.0) is not None
    assert c._implausible("BNB", float("nan")) is not None


def test_quotes_wiring_drops_quarantined_symbol(monkeypatch):
    """End-to-end: the quotes() loop must actually drop a quarantined quote
    and leave the accepted baseline unpoisoned — deleting the `continue` in
    quotes() would pass the _implausible unit tests but reopen B2."""
    c = CMCClient("test-key")
    c._last_accepted["BNB"] = 600.0
    body = {"status": {"error_code": 0},
            "data": {"BNB": [{"quote": {"USDT": {
                "price": 900.0, "volume_24h": 1.0, "percent_change_24h": 1.0,
                "last_updated": None}}}]}}

    class Resp:
        status_code = 200

        def json(self):
            return body

    monkeypatch.setattr(c._session, "get", lambda *a, **k: Resp())
    assert "BNB" not in c.quotes(["BNB"])      # 50% spike: dropped, not stored
    assert c._last_accepted["BNB"] == 600.0    # baseline not poisoned

    out = c.quotes(["BNB"])                    # second consecutive poll agrees
    assert out["BNB"].price == 900.0           # -> real move accepted
    assert c._last_accepted["BNB"] == 900.0


# --- H2/H4: live-start refusals -------------------------------------------------

class FakeClient:
    def __init__(self, data):
        self._data = data

    def portfolio(self):
        return self._data


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "PORTFOLIO_PATH", tmp_path / "portfolio.json")
    return tmp_path


def _journal(tmp_path):
    return Journal(tmp_path / "journal.jsonl", tmp_path / "ledger.jsonl")


def test_live_start_refused_without_gas(isolated_paths):
    client = FakeClient({"tokens": [{"symbol": "USDT", "balance": "20"},
                                    {"symbol": "BNB", "balance": "0.001"}]})
    with pytest.raises(SystemExit, match="gas check"):
        runner._reconcile_live(client, Portfolio(cash=150.0), _journal(isolated_paths))


def test_live_start_refused_when_wallet_missing_held_token(isolated_paths):
    client = FakeClient({"tokens": [{"symbol": "USDT", "balance": "5"},
                                    {"symbol": "BNB", "balance": "0.02"}]})
    p = Portfolio(cash=5.0, mode="live")
    p.positions["BTC"] = Position("BTC", 0.0002, 100000.0, 1.0)
    with pytest.raises(SystemExit, match="no BTC balance"):
        runner._reconcile_live(client, p, _journal(isolated_paths))


def test_live_restart_accepts_matching_balance(isolated_paths):
    client = FakeClient({"tokens": [{"symbol": "USDT", "balance": "5"},
                                    {"symbol": "BNB", "balance": "0.02"},
                                    {"symbol": "ETH", "balance": "0.01"}]})
    p = Portfolio(cash=5.0, mode="live")
    p.positions["ETH"] = Position("ETH", 0.01, 3000.0, 1.0)
    runner._reconcile_live(client, p, _journal(isolated_paths))  # no refusal


def test_live_start_refused_on_other_host(isolated_paths):
    client = FakeClient({})
    p = Portfolio(cash=20.0, mode="live", live_host="some-other-box")
    with pytest.raises(SystemExit, match="double-trade"):
        runner._reconcile_live(client, p, _journal(isolated_paths))


def test_live_rebase_records_baseline_and_host(isolated_paths):
    client = FakeClient({"tokens": [{"symbol": "USDT", "balance": "20"},
                                    {"symbol": "BNB", "balance": "0.02"}]})
    p = Portfolio(cash=150.0, mode="paper")
    runner._reconcile_live(client, p, _journal(isolated_paths))
    assert p.mode == "live"
    assert p.baseline_equity == 20.0
    assert p.live_host != ""


def test_find_balance_bnb_and_pegged_symbols():
    data = {"tokens": [{"symbol": "BTCB", "balance": "0.001"},
                       {"symbol": "BNB", "balance": "0.4"}]}
    assert find_balance(data, ("BNB",)) == 0.4
    assert find_balance(data, ("BTC", "BTCB")) == 0.001
    assert find_balance(data, ("SOL",)) is None


# --- LOW: quote-gap timer survives restarts -------------------------------------

def test_quote_gap_persistence_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "QUOTE_GAPS_PATH", tmp_path / "quote_gaps.json")
    monkeypatch.setitem(runner._quote_gap_since, "BNB", 1750000000.0)
    runner._save_quote_gaps()
    assert runner._load_quote_gaps() == {"BNB": 1750000000.0}
    runner._quote_gap_since.pop("BNB", None)


def test_quote_gap_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "QUOTE_GAPS_PATH", tmp_path / "absent.json")
    assert runner._load_quote_gaps() == {}


@pytest.mark.parametrize("content", [
    "null", "[1, 2]", '{"BNB": null}', '{"BNB": [1]}', '{"BNB": "junk"}', "{torn",
])
def test_quote_gap_malformed_content_fails_open(tmp_path, monkeypatch, content):
    """A weird-but-parseable file must not crash main() pre-loop — systemd
    would turn that into a tickless 10s restart loop with no alert."""
    path = tmp_path / "quote_gaps.json"
    path.write_text(content)
    monkeypatch.setattr(runner, "QUOTE_GAPS_PATH", path)
    assert runner._load_quote_gaps() == {}


# --- M3: tail reader -------------------------------------------------------------

def test_read_jsonl_tail_reads_last_records_and_skips_torn(tmp_path):
    path = tmp_path / "journal.jsonl"
    with path.open("w") as f:
        for i in range(500):
            f.write(json.dumps({"i": i}) + "\n")
        f.write('{"torn": tru')  # crash mid-write
    tail = read_jsonl_tail(path, 10)
    assert [r["i"] for r in tail] == list(range(490, 500))
    assert read_jsonl_tail(tmp_path / "absent.jsonl", 10) == []


def test_read_jsonl_tail_multi_chunk(tmp_path):
    """Exercises the seek-backwards accumulation across several 64KB chunks —
    the only hard part of the reader, invisible to small-file tests."""
    path = tmp_path / "big.jsonl"
    pad = "x" * 120  # ~140 bytes/line -> 3000 lines ≈ 420KB ≈ 7 chunks
    with path.open("w") as f:
        for i in range(3000):
            f.write(json.dumps({"i": i, "pad": pad}) + "\n")
    tail = read_jsonl_tail(path, 2000)
    assert [r["i"] for r in tail] == list(range(1000, 3000))


# --- config sanity ----------------------------------------------------------------

def test_gas_and_quote_guard_config_sane():
    # warn must fire ABOVE the start-refusal level — otherwise there is a
    # silent dead zone where the agent runs but a restart would be refused
    assert config.LOW_GAS_BNB_WARN > config.MIN_GAS_BNB_START
    assert 0 < config.QUOTE_CONFIRM_TOLERANCE_PCT < config.QUOTE_MAX_JUMP_PCT
