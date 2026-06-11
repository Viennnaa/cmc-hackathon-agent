"""DECIDE layer, variant C: regime router — the adaptation layer.

Classifies each symbol's regime from its own price history (5-day SMA,
no external data) and routes to the strategy that fits:

  uptrend   -> momentum (ride fresh trend turns)
  chop      -> mean reversion (buy dips, sell recoveries)
  downtrend -> cash preservation (no entries; exits and stops still run)

Long-only on spot means the bearish position IS cash — in a falling
market the router's edge is refusing dead-cat-bounce entries the 24h
filter can't see (30d@1h validation: -4.05%/DD 3.85% vs momentum's
-7.12%/DD 6.99% vs buy&hold -20.13%).

Every reason is tagged [uptrend]/[chop]/[downtrend] so the journal shows
the regime behind each decision for the judged replay.
"""

from agent import config
from agent.strategy import mean_revert, momentum
from agent.strategy.momentum import Signal


def classify(prices: list[float]) -> str | None:
    """"up" | "down" | "chop", or None while the SMA window fills."""
    if len(prices) < config.REGIME_SMA_BARS:
        return None
    sma = sum(prices[-config.REGIME_SMA_BARS:]) / config.REGIME_SMA_BARS
    px = prices[-1]
    if px > sma * (1 + config.REGIME_BAND_PCT):
        return "up"
    if px < sma * (1 - config.REGIME_BAND_PCT):
        return "down"
    return "chop"


def evaluate(
    symbol: str,
    prices: list[float],
    holding: bool,
    fear_greed: int | None,
    change_24h: float | None = None,
) -> Signal:
    regime = classify(prices)
    if regime is None:
        return Signal(symbol, "hold",
                      f"warming up (regime needs {config.REGIME_SMA_BARS} bars, "
                      f"have {len(prices)})")

    if regime == "up":
        sig = momentum.evaluate(symbol, prices, holding, fear_greed, change_24h)
        sig.reason = f"[uptrend] {sig.reason}"
        return sig

    if regime == "down":
        if holding:
            # momentum's exit logic (MACD negative 2 bars / RSI overbought)
            # unwinds promptly in a downtrend; the -3% stop bounds the rest
            sig = momentum.evaluate(symbol, prices, holding, fear_greed, change_24h)
            sig.reason = f"[downtrend] {sig.reason}"
            return sig
        return Signal(symbol, "hold", "[downtrend] cash preservation: no entries")

    sig = mean_revert.evaluate(symbol, prices, holding, fear_greed, change_24h)
    sig.reason = f"[chop] {sig.reason}"
    return sig
