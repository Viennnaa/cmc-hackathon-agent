"""DECIDE layer: deterministic momentum strategy.

Entry: MACD histogram CROSSES positive this bar (fresh trend turn, not a
level — buying mid-trend strength backtested at -10% from whipsaw churn)
AND RSI in [50, 70], with sentiment veto on extreme fear.
Exit: MACD histogram negative two consecutive bars (one-bar dips are
noise) OR RSI > 75 (overbought).

Returns intents only — the risk engine has final say on everything.
"""

from dataclasses import dataclass

from agent import config
from agent.signals.indicators import macd_histogram_series, rsi


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
    change_24h: float | None = None,
) -> Signal:
    r = rsi(prices, config.RSI_PERIOD)
    hist = macd_histogram_series(prices, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL)

    if r is None or len(hist) < 2:
        return Signal(symbol, "hold", f"warming up ({len(prices)}/{config.MIN_HISTORY + 1} bars)", r,
                      hist[-1] if hist else None)
    # clamp float noise to zero: in flat/linear stretches the histogram
    # converges to 0 and ±1e-13 jitter would register as fake crosses
    eps = abs(prices[-1]) * 1e-9
    h, h_prev = (0.0 if abs(v) < eps else v for v in (hist[-1], hist[-2]))

    if holding:
        if h < 0 and h_prev < 0:
            return Signal(symbol, "exit", f"MACD histogram negative 2 bars ({h_prev:.6f}, {h:.6f})", r, h)
        if r > config.RSI_EXIT:
            return Signal(symbol, "exit", f"RSI overbought ({r:.1f} > {config.RSI_EXIT})", r, h)
        return Signal(symbol, "hold", "in position, trend intact", r, h)

    # flat: consider entry
    if fear_greed is not None and fear_greed < config.FEAR_GREED_VETO_BELOW:
        return Signal(symbol, "hold", f"sentiment veto: fear&greed {fear_greed} < {config.FEAR_GREED_VETO_BELOW}", r, h)
    if change_24h is not None and change_24h <= config.REGIME_MIN_24H_CHANGE:
        return Signal(symbol, "hold",
                      f"regime veto: 24h change {change_24h:.2f}% <= {config.REGIME_MIN_24H_CHANGE}%", r, h)
    if h > 0 and h_prev <= 0 and config.RSI_ENTRY_MIN <= r <= config.RSI_ENTRY_MAX:
        return Signal(symbol, "enter", f"MACD hist crossed positive ({h_prev:.6f} -> {h:.6f}), RSI {r:.1f}", r, h)
    return Signal(symbol, "hold", f"no setup (RSI {r:.1f}, MACD hist {h:.6f})", r, h)
