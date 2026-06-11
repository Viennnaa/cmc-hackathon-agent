"""DECIDE layer, variant B: mean reversion.

Entry: RSI oversold (< 30) with the 24h drop not in free-fall (knife
filter) — buy the dip, not the crash. Exit: RSI recovers above 55 (bounce
captured). The -3% stop-loss in the risk engine bounds the downside when
the dip keeps dipping.

Same Signal interface as momentum so the runner/backtest can switch
strategies by name.
"""

from agent import config
from agent.signals.indicators import rsi
from agent.strategy.momentum import Signal

RSI_OVERSOLD = 30.0
RSI_RECOVERED = 55.0
KNIFE_24H_DROP = -8.0  # skip entries when 24h change is worse than this


def evaluate(
    symbol: str,
    prices: list[float],
    holding: bool,
    fear_greed: int | None,
    change_24h: float | None = None,
) -> Signal:
    r = rsi(prices, config.RSI_PERIOD)
    if r is None:
        return Signal(symbol, "hold", f"warming up ({len(prices)}/{config.RSI_PERIOD + 1} bars)", r)

    if holding:
        if r > RSI_RECOVERED:
            return Signal(symbol, "exit", f"RSI recovered ({r:.1f} > {RSI_RECOVERED})", r)
        return Signal(symbol, "hold", f"awaiting recovery (RSI {r:.1f})", r)

    if change_24h is not None and change_24h < KNIFE_24H_DROP:
        return Signal(symbol, "hold", f"knife filter: 24h change {change_24h:.2f}% < {KNIFE_24H_DROP}%", r)
    if r < RSI_OVERSOLD:
        return Signal(symbol, "enter", f"RSI oversold ({r:.1f} < {RSI_OVERSOLD})", r)
    return Signal(symbol, "hold", f"no setup (RSI {r:.1f})", r)
