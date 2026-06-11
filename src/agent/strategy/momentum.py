"""DECIDE layer: deterministic momentum strategy.

Entry: MACD histogram positive (trend up) AND RSI in [50, 70] (momentum
confirmed, not overbought), with sentiment veto on extreme fear.
Exit: MACD histogram flips negative OR RSI > 75 (overbought).

Returns intents only — the risk engine has final say on everything.
"""

from dataclasses import dataclass

from agent import config
from agent.signals.indicators import macd_histogram, rsi


@dataclass
class Signal:
    symbol: str
    action: str  # "enter" | "exit" | "hold"
    reason: str
    rsi: float | None = None
    macd_hist: float | None = None


def evaluate(
    symbol: str,
    prices: list[float],
    holding: bool,
    fear_greed: int | None,
) -> Signal:
    r = rsi(prices, config.RSI_PERIOD)
    h = macd_histogram(prices, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL)

    if r is None or h is None:
        return Signal(symbol, "hold", f"warming up ({len(prices)}/{config.MIN_HISTORY} bars)", r, h)

    if holding:
        if h < 0:
            return Signal(symbol, "exit", f"MACD histogram flipped negative ({h:.6f})", r, h)
        if r > config.RSI_EXIT:
            return Signal(symbol, "exit", f"RSI overbought ({r:.1f} > {config.RSI_EXIT})", r, h)
        return Signal(symbol, "hold", "in position, trend intact", r, h)

    # flat: consider entry
    if fear_greed is not None and fear_greed < config.FEAR_GREED_VETO_BELOW:
        return Signal(symbol, "hold", f"sentiment veto: fear&greed {fear_greed} < {config.FEAR_GREED_VETO_BELOW}", r, h)
    if h > 0 and config.RSI_ENTRY_MIN <= r <= config.RSI_ENTRY_MAX:
        return Signal(symbol, "enter", f"MACD hist {h:.6f} > 0, RSI {r:.1f} in entry band", r, h)
    return Signal(symbol, "hold", f"no setup (RSI {r:.1f}, MACD hist {h:.6f})", r, h)
