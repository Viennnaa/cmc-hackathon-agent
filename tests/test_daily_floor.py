"""Daily-trade floor: the BNB Hack competition disqualifies any UTC day with
zero trades, so the runner forces one minimal compliant swap when a day would
otherwise stay flat. These cover the decision helpers and trade-day tracking."""

from agent import config, runner
from agent.execution.portfolio import Portfolio, Position
from agent.risk.engine import RiskEngine


def test_floor_needed_when_day_flat_past_cutoff():
    risk = RiskEngine()
    risk.last_trade_day = -1  # never traded
    ts = config.DAILY_TRADE_FLOOR_HOUR_UTC * 3600 + 1800  # day 0, just past cutoff
    assert runner._floor_needed(risk, ts)


def test_floor_not_needed_if_already_traded_today():
    risk = RiskEngine()
    risk.last_trade_day = 0  # already traded on day 0
    assert not runner._floor_needed(risk, 23 * 3600)  # day 0, 23:00 UTC


def test_floor_not_needed_before_cutoff():
    risk = RiskEngine()
    risk.last_trade_day = -1
    ts = (config.DAILY_TRADE_FLOOR_HOUR_UTC - 2) * 3600  # before the cutoff hour
    assert not runner._floor_needed(risk, ts)


def test_floor_not_needed_when_killed():
    risk = RiskEngine()
    risk.killed = True
    risk.last_trade_day = -1
    assert not runner._floor_needed(risk, 23 * 3600)


def test_floor_candidate_prefers_floor_symbol():
    p = Portfolio(cash=150.0)
    quotes = {s: None for s in config.UNIVERSE}
    assert runner._floor_candidate(p, quotes) == config.DAILY_FLOOR_SYMBOL


def test_floor_candidate_skips_held_symbol():
    p = Portfolio(cash=150.0)
    p.positions[config.DAILY_FLOOR_SYMBOL] = Position(config.DAILY_FLOOR_SYMBOL, 1.0, 1.0, 1.0)
    cand = runner._floor_candidate(p, {s: None for s in config.UNIVERSE})
    assert cand is not None and cand != config.DAILY_FLOOR_SYMBOL


def test_floor_candidate_none_without_cash():
    p = Portfolio(cash=config.DAILY_FLOOR_TRADE_USDT - 0.1)
    assert runner._floor_candidate(p, {s: None for s in config.UNIVERSE}) is None


def test_note_trade_persists_last_trade_day(tmp_path):
    risk = RiskEngine()
    risk.note_trade(now=5 * 86_400 + 100)  # UTC day 5
    assert risk.last_trade_day == 5
    path = tmp_path / "risk.json"
    risk.save(path)
    assert RiskEngine.load(path).last_trade_day == 5
