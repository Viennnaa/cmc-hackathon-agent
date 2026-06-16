"""OHLCV historical parsing + runner warm-start.

The Professional tier enabled ohlcv/historical (2026-06-16); these pin the
client parser and the startup backfill that makes the regime/MACD indicators
valid from the first tick instead of after days of self-sampling.
"""
import time

from agent import config, runner
from agent.data.cmc import CMCClient, CMCError
from agent.data.store import PriceStore


class _Resp:
    def __init__(self, body):
        self.status_code = 200
        self._body = body

    def json(self):
        return self._body


def _ohlcv_body(quotes_by_symbol):
    """CMC v2 ohlcv/historical shape: data[SYM] is a list whose [0] holds quotes."""
    return {
        "status": {"error_code": 0},
        "data": {
            sym: [{"id": 1, "name": sym, "symbol": sym, "quotes": quotes}]
            for sym, quotes in quotes_by_symbol.items()
        },
    }


def _bar(iso_close, close):
    return {"time_close": iso_close, "quote": {"USDT": {"close": close}}}


def test_ohlcv_historical_parses_sorts_and_filters(monkeypatch):
    c = CMCClient("test-key")
    # ETH quotes deliberately newest-first + one glitched bar (zero close) that
    # must be dropped so it can never seed a poisoned baseline. XRP is requested
    # but absent from the response and must simply be omitted, not raise.
    body = _ohlcv_body({
        "ETH": [
            _bar("2026-06-16T05:59:59.999Z", 1800.0),   # newest
            _bar("2026-06-16T04:59:59.999Z", 0.0),      # glitch -> dropped
            _bar("2026-06-16T03:59:59.999Z", 1700.0),   # oldest
        ],
    })
    monkeypatch.setattr(c._session, "get", lambda *a, **k: _Resp(body))

    out = c.ohlcv_historical(["ETH", "XRP"], count=3)
    assert "XRP" not in out                              # no history -> omitted
    closes = [px for _, px in out["ETH"]]
    assert closes == [1700.0, 1800.0]                    # sorted oldest-first, glitch gone
    ts = [t for t, _ in out["ETH"]]
    assert ts == sorted(ts)                              # ascending by close time


def test_ohlcv_historical_plan_error_raises_cmcerror(monkeypatch):
    """A free/over-limit tier returns an error_code here; callers (warm-start)
    rely on it surfacing as CMCError so they can fall back to self-sampling."""
    c = CMCClient("test-key")
    body = {"status": {"error_code": 1006, "error_message": "plan does not support"},
            "data": {}}

    class Resp(_Resp):
        status_code = 403

    monkeypatch.setattr(c._session, "get", lambda *a, **k: Resp(body))
    try:
        c.ohlcv_historical(["ETH"], count=3)
        assert False, "expected CMCError"
    except CMCError:
        pass


class _FakeCMC:
    """Stand-in returning `count` synthetic completed hourly bars per symbol."""
    def __init__(self, raise_exc=None):
        self._raise = raise_exc

    def ohlcv_historical(self, symbols, count, convert="USDT"):
        if self._raise:
            raise self._raise
        cur = int(time.time() // 3600)
        out = {}
        for sym in symbols:
            # buckets cur-1 .. cur-count are all completed (strictly before now)
            out[sym] = [((cur - 1 - i) * 3600 + 3599, 100.0 + i) for i in range(count)]
            out[sym].sort(key=lambda b: b[0])
        return out


def test_warm_start_seeds_enough_bars_for_regime_router(tmp_path):
    store = PriceStore(tmp_path / "prices.sqlite")
    runner._warm_start(_FakeCMC(), store)
    # every traded symbol must clear the widest indicator window from tick 1
    for sym in config.UNIVERSE:
        bars = store.bars(sym, config.BAR_SECONDS)
        assert len(bars) >= config.REGIME_SMA_BARS, f"{sym}: only {len(bars)} bars"


def test_warm_start_swallows_failure(tmp_path):
    """Backfill is best-effort: a CMC failure must not block startup, leaving
    the store empty for the live poll to warm by self-sampling."""
    store = PriceStore(tmp_path / "prices.sqlite")
    runner._warm_start(_FakeCMC(raise_exc=CMCError("boom")), store)  # must not raise
    assert store.count(config.UNIVERSE[0]) == 0
