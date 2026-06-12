import math
import time

from agent import config
from agent.execution.portfolio import Portfolio
from agent.risk.engine import RiskEngine
from agent.strategy import mean_revert, momentum


def rising_then_cross():
    """Sine-wave base: MACD hist crosses positive at bar 51 with RSI ~55."""
    return [100 + 3 * math.sin(i / 4) for i in range(60)]


def test_momentum_entry_requires_fresh_cross():
    prices = rising_then_cross()
    # find the bar where the signal first fires
    fired = [
        momentum.evaluate("BNB", prices[:i], holding=False, fear_greed=None).action
        for i in range(config.MIN_HISTORY + 2, len(prices) + 1)
    ]
    assert "enter" in fired
    # a long-established uptrend (no fresh cross) must NOT fire:
    # MACD hist stays positive (or zero-noise, clamped) the whole way up
    steady = [100 + i * 0.5 for i in range(120)]
    sig = momentum.evaluate("BNB", steady, holding=False, fear_greed=None)
    assert sig.action == "hold"


def test_momentum_sentiment_veto():
    prices = rising_then_cross()
    sig = momentum.evaluate("BNB", prices, holding=False, fear_greed=10)
    assert sig.action == "hold"
    assert "sentiment veto" in sig.reason


def test_momentum_regime_veto():
    prices = rising_then_cross()
    sig = momentum.evaluate("BNB", prices, holding=False, fear_greed=None, change_24h=-2.0)
    assert sig.action == "hold"
    assert "regime veto" in sig.reason


def test_momentum_exit_needs_two_negative_bars():
    # on the sine wave the histogram first turns negative at bar 40 (hold)
    # and is negative a second consecutive bar at 41 (exit)
    prices = rising_then_cross()
    one_neg = momentum.evaluate("BNB", prices[:40], holding=True, fear_greed=None)
    assert one_neg.action == "hold"
    two_neg = momentum.evaluate("BNB", prices[:41], holding=True, fear_greed=None)
    assert two_neg.action == "exit"


def test_mean_revert_enters_oversold_exits_recovered():
    falling = [100 - i * 0.8 for i in range(30)]
    sig = mean_revert.evaluate("BNB", falling, holding=False, fear_greed=None, change_24h=-3.0)
    assert sig.action == "enter"
    recovering = falling + [falling[-1] + i * 1.2 for i in range(15)]
    sig = mean_revert.evaluate("BNB", recovering, holding=True, fear_greed=None)
    assert sig.action == "exit"


def test_mean_revert_knife_filter():
    falling = [100 - i * 0.8 for i in range(30)]
    sig = mean_revert.evaluate("BNB", falling, holding=False, fear_greed=None, change_24h=-12.0)
    assert sig.action == "hold"
    assert "knife filter" in sig.reason


def test_reentry_cooldown_blocks_then_expires():
    p = Portfolio(cash=150.0)
    engine = RiskEngine()
    now = time.time()
    engine.note_exit("BNB", now=now)
    blocked = engine.review("enter", "BNB", p, now=now + 60, fear_greed=50)
    assert not blocked.approved and blocked.rule == "reentry_cooldown"
    allowed = engine.review("enter", "BNB", p, now=now + config.REENTRY_COOLDOWN_SECONDS + 1,
                            fear_greed=50)
    assert allowed.approved
