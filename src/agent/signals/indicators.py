"""Pure-Python technical indicators (no numpy needed at this scale)."""


def ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    out = [sum(values[:period]) / period]  # seed with SMA
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values: list[float], period: int = 14) -> float | None:
    """Wilder-smoothed RSI of the latest bar; None if not enough history."""
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd_histogram_series(
    values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> list[float]:
    """MACD histogram (MACD line - signal line) per bar; [] if not enough data."""
    if len(values) < slow + signal:
        return []
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    # align: ema_slow starts (slow - fast) bars later than ema_fast
    macd_line = [f - s for f, s in zip(ema_fast[slow - fast:], ema_slow)]
    signal_line = ema(macd_line, signal)
    if not signal_line:
        return []
    return [m - s for m, s in zip(macd_line[signal - 1:], signal_line)]


def macd_histogram(
    values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> float | None:
    """Latest MACD histogram value; None if not enough history."""
    series = macd_histogram_series(values, fast, slow, signal)
    return series[-1] if series else None
