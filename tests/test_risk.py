import time

from agent import config
from agent.execution.paper import PaperExecutor
from agent.execution.portfolio import Portfolio, Position
from agent.risk.engine import RiskEngine


def make_portfolio(cash=150.0):
    return Portfolio(cash=cash)


def test_entry_sized_at_20_pct():
    p = make_portfolio(150.0)
    v = RiskEngine().review("enter", "BNB", p, fear_greed=50)
    assert v.approved
    assert abs(v.size_usdt - 30.0) < 1e-9
    assert v.rule == "position_sizing"


def test_no_double_position():
    p = make_portfolio()
    p.positions["BNB"] = Position("BNB", 0.05, 600.0, time.time())
    v = RiskEngine().review("enter", "BNB", p, fear_greed=50)
    assert not v.approved
    assert v.rule == "single_position"


def test_sentiment_veto_engine_enforced():
    """Judged rule: F&G < 20 -> no new entries, for EVERY strategy (engine-level)."""
    p = make_portfolio(150.0)
    v = RiskEngine().review("enter", "BNB", p, fear_greed=19)
    assert not v.approved
    assert v.rule == "sentiment_veto"
    # boundary: 20 is not extreme fear
    assert RiskEngine().review("enter", "BNB", p, fear_greed=20).approved


def test_sentiment_veto_fails_closed_when_unavailable():
    p = make_portfolio(150.0)
    v = RiskEngine().review("enter", "BNB", p, fear_greed=None)
    assert not v.approved
    assert v.rule == "sentiment_veto"
    # exits stay allowed even with no reading
    p.positions["BNB"] = Position("BNB", 0.05, 600.0, time.time())
    assert RiskEngine().review("exit", "BNB", p, fear_greed=None).approved


def test_stop_loss_fires_at_3_pct():
    p = make_portfolio()
    p.positions["BNB"] = Position("BNB", 0.05, 600.0, time.time())
    engine = RiskEngine()
    assert engine.stop_loss_check(p, "BNB", 600.0 * 0.97) is not None
    assert engine.stop_loss_check(p, "BNB", 600.0 * 0.975) is None


def test_daily_loss_cap_flattens_and_halts():
    p = make_portfolio(150.0)
    engine = RiskEngine()
    p.cash = 150.0 * (1 - config.DAILY_LOSS_CAP_PCT) - 0.01  # below -5% on the day
    v = engine.portfolio_gates(p)
    assert v is not None and v.rule == "daily_loss_cap"
    # and entries are now blocked
    assert engine.review("enter", "BNB", p, fear_greed=50).rule == "daily_halt"


def test_kill_switch_at_10_pct_drawdown():
    p = make_portfolio(150.0)
    engine = RiskEngine()
    p.cash = 150.0 * (1 - config.KILL_SWITCH_DRAWDOWN_PCT)
    v = engine.portfolio_gates(p)
    assert v is not None and v.rule == "kill_switch"
    assert engine.killed
    assert not engine.review("enter", "BNB", p).approved


def test_kill_switch_outranks_daily_cap():
    p = make_portfolio(150.0)
    engine = RiskEngine()
    p.cash = 150.0 * 0.85  # -15%: beyond both thresholds
    v = engine.portfolio_gates(p)
    assert v.rule == "kill_switch"


def test_exits_always_allowed_even_when_halted():
    p = make_portfolio()
    engine = RiskEngine()
    engine.halted_until = time.time() + 3600
    assert engine.review("exit", "BNB", p).approved


def test_paper_round_trip_pnl():
    p = make_portfolio(150.0)
    ex = PaperExecutor()
    buy = ex.buy(p, "BNB", 30.0, 600.0)
    assert p.cash == 120.0
    sell = ex.sell(p, "BNB", 600.0)  # flat price: lose fees + slippage only
    assert sell.pnl_usdt < 0
    assert abs(sell.pnl_usdt) < 30.0 * 0.01  # well under 1% cost on a round trip
    assert "BNB" not in p.positions


def test_max_concurrent_positions_cap():
    from agent.execution.portfolio import Position
    eng = RiskEngine()
    p = Portfolio(cash=60.0)
    for i, sym in enumerate(("BNB", "BTC", "ETH")):
        p.positions[sym] = Position(sym, 1.0, 30.0, 1.0)
    v = eng.review("enter", "SOL", p, fear_greed=50)
    assert not v.approved
    assert v.rule == "max_concurrent"
