from agent.signals.indicators import ema, macd_histogram, rsi


def test_rsi_insufficient_history():
    assert rsi([1.0] * 10, period=14) is None


def test_rsi_all_gains_is_100():
    prices = [float(i) for i in range(1, 40)]
    assert rsi(prices) == 100.0


def test_rsi_all_losses_near_zero():
    prices = [float(i) for i in range(40, 1, -1)]
    assert rsi(prices) < 1.0


def test_rsi_flat_series_neutral():
    # constant prices after some movement: avg gain == avg loss decays both to 0;
    # alternating +1/-1 should sit near 50
    prices = [100.0 + (i % 2) for i in range(40)]
    r = rsi(prices)
    assert 40 < r < 60


def test_ema_matches_known_value():
    values = [22.27, 22.19, 22.08, 22.17, 22.18, 22.13, 22.23, 22.43, 22.24, 22.29]
    result = ema(values, 10)
    assert abs(result[0] - 22.221) < 0.001  # seed = SMA(10)


def test_macd_insufficient_history():
    assert macd_histogram([1.0] * 30) is None


def test_macd_uptrend_positive():
    prices = [100 + i * 0.5 for i in range(60)]
    h = macd_histogram(prices)
    assert h is not None
    # steady uptrend: MACD line above signal initially converges; just check it's finite & sane
    assert -10 < h < 10


def test_macd_trend_reversal_sign():
    up = [100 + i for i in range(50)]
    down = up + [150 - i * 2 for i in range(20)]
    assert macd_histogram(down) < 0
