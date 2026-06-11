import math

from agent import config
from agent.strategy import adaptive


def test_warmup_below_sma_window():
    sig = adaptive.evaluate("BNB", [100.0] * (config.REGIME_SMA_BARS - 1),
                            holding=False, fear_greed=None)
    assert sig.action == "hold"
    assert "warming up" in sig.reason


def test_uptrend_routes_to_momentum():
    prices = [100 + i * 0.5 for i in range(150)]
    assert adaptive.classify(prices) == "up"
    sig = adaptive.evaluate("BNB", prices, holding=False, fear_greed=None)
    assert sig.reason.startswith("[uptrend]")


def test_downtrend_blocks_entries():
    prices = [200 - i * 0.5 for i in range(150)]
    assert adaptive.classify(prices) == "down"
    sig = adaptive.evaluate("BNB", prices, holding=False, fear_greed=None,
                            change_24h=3.0)  # a bounce the 24h filter would pass
    assert sig.action == "hold"
    assert "cash preservation" in sig.reason


def test_downtrend_still_exits_holdings():
    # rally that rolls over hard: regime flips down while MACD histogram is
    # freshly negative (a pure linear decline converges the histogram to the
    # zero-clamp, which is "trend intact" — not what we want to test)
    prices = [100 + i * 0.5 for i in range(100)] + [150 - i * 1.2 for i in range(50)]
    assert adaptive.classify(prices) == "down"
    sig = adaptive.evaluate("BNB", prices, holding=True, fear_greed=None)
    assert sig.reason.startswith("[downtrend]")
    assert sig.action == "exit"


def test_chop_routes_to_mean_revert():
    # oscillation around a flat mean stays inside the +/-1% band at the trough
    prices = [100 + 0.8 * math.sin(i / 5) for i in range(150)]
    assert adaptive.classify(prices) == "chop"
    sig = adaptive.evaluate("BNB", prices, holding=False, fear_greed=None)
    assert sig.reason.startswith("[chop]")
