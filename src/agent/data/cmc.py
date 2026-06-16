"""SENSE layer: CoinMarketCap Data API client.

Live trading reads quotes/latest each poll and extends a price series in
PriceStore. The Professional tier (hackathon grant, 2026-06-16) also enables
ohlcv/historical, so ohlcv_historical() backfills recent completed hourly
closes to warm the indicators at startup (runner) and to feed the backtest the
same data path it trades on. Fear & Greed comes from the v3 endpoint.
"""

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from agent import config

log = logging.getLogger("agent.cmc")

BASE_URL = "https://pro-api.coinmarketcap.com"


def _parse_iso(value: str | None) -> float | None:
    """CMC last_updated ('2026-06-11T14:45:03.000Z') -> epoch seconds, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class Quote:
    symbol: str
    price: float
    volume_24h: float
    percent_change_24h: float
    timestamp: float


class CMCError(RuntimeError):
    pass


class CMCClient:
    def __init__(self, api_key: str, session: requests.Session | None = None):
        if not api_key:
            raise CMCError("CMC_API_KEY is not set (see .env.example)")
        self._session = session or requests.Session()
        self._session.headers.update({
            "X-CMC_PRO_API_KEY": api_key,
            "Accept": "application/json",
        })
        # plausibility state (in-memory: after a restart the first poll
        # re-baselines, which matches today's trust-first-sample behavior)
        self._last_accepted: dict[str, float] = {}
        self._suspect: dict[str, float] = {}

    def _get(self, path: str, params: dict) -> dict:
        try:
            resp = self._session.get(f"{BASE_URL}{path}", params=params, timeout=30)
            body = resp.json()
        except (requests.RequestException, ValueError) as e:
            # transient network/parse failures must not crash the trading loop
            raise CMCError(f"CMC {path} request failed: {e}") from e
        status = body.get("status", {})
        # v2 endpoints return error_code as int 0, v3 as string "0"
        error_code = int(status.get("error_code") or 0)
        if resp.status_code != 200 or error_code:
            raise CMCError(
                f"CMC {path} failed: HTTP {resp.status_code} "
                f"code={status.get('error_code')} {status.get('error_message')}"
            )
        return body["data"]

    def _implausible(self, sym: str, price: float) -> str | None:
        """Reason to quarantine this quote, or None to accept it.

        A glitched sample (null/zero/spike) flowing into Portfolio.mark would
        permanently inflate peak_equity or fire every stop-loss at once, so a
        price jumping more than QUOTE_MAX_JUMP_PCT from the last accepted
        sample is held back until a second consecutive poll agrees.
        """
        if not math.isfinite(price) or price <= 0:
            return f"non-positive price {price!r}"
        last = self._last_accepted.get(sym)
        if last is None:
            return None
        jump = abs(price - last) / last
        if jump <= config.QUOTE_MAX_JUMP_PCT:
            self._suspect.pop(sym, None)
            return None
        suspect = self._suspect.get(sym)
        if suspect is not None and abs(price - suspect) / suspect <= config.QUOTE_CONFIRM_TOLERANCE_PCT:
            self._suspect.pop(sym, None)  # two consecutive polls agree: real move
            return None
        self._suspect[sym] = price
        return (f"price {price} jumped {jump:.1%} from last accepted {last} "
                "(quarantined until a second poll confirms)")

    def quotes(self, symbols: list[str], convert: str = "USDT") -> dict[str, Quote]:
        data = self._get(
            "/v2/cryptocurrency/quotes/latest",
            {"symbol": ",".join(symbols), "convert": convert},
        )
        now = time.time()
        out: dict[str, Quote] = {}
        for sym in symbols:
            entries = data.get(sym) or []
            if not entries:
                continue
            q = entries[0]["quote"][convert]
            # Stale upstream data must not masquerade as a fresh price: a
            # dropped symbol routes into the runner's quote-gap protection.
            source_ts = _parse_iso(q.get("last_updated"))
            if source_ts is not None and now - source_ts > config.STALE_QUOTE_MAX_AGE_SECONDS:
                continue
            price = float(q["price"]) if q.get("price") is not None else float("nan")
            quarantine = self._implausible(sym, price)
            if quarantine:
                log.warning("%s quote dropped: %s", sym, quarantine)
                continue
            self._last_accepted[sym] = price
            out[sym] = Quote(
                symbol=sym,
                price=price,
                volume_24h=q.get("volume_24h") or 0.0,
                percent_change_24h=q.get("percent_change_24h") or 0.0,
                timestamp=now,
            )
        return out

    def ohlcv_historical(self, symbols: list[str], count: int,
                         convert: str = "USDT") -> dict[str, list[tuple[float, float]]]:
        """Recent completed 1h OHLCV closes per symbol, oldest-first:
        {sym: [(close_ts, close_price), ...]}.

        One batched call for the whole list. Each bar is a completed hourly
        candle's close, keyed and ordered so the points drop straight into the
        same PriceStore hour-buckets the live poll fills — this is how a cold or
        restarted agent warms its indicators, and how the backtest replays the
        exact data path it trades on. Symbols CMC has no history for are simply
        absent from the result.

        Pro tier only: the free tier returns a plan error here, which surfaces
        as CMCError so callers can fall back to self-sampling.
        """
        now = time.time()
        # Range mode (time_start/time_end) returns every completed hourly bar in
        # the window; a bare `count` param was observed to return empty/oddly
        # anchored results, so the window is sized to cover `count` bars + slack.
        data = self._get(
            "/v2/cryptocurrency/ohlcv/historical",
            {
                "symbol": ",".join(symbols),
                "convert": convert,
                "time_period": "hourly",
                "interval": "1h",
                "time_start": int(now - (count + 2) * 3600),
                "time_end": int(now),
            },
        )
        out: dict[str, list[tuple[float, float]]] = {}
        for sym in symbols:
            entries = data.get(sym) or []
            if not entries:
                continue
            bars: list[tuple[float, float]] = []
            for q in entries[0].get("quotes") or []:
                cell = (q.get("quote") or {}).get(convert) or {}
                close = cell.get("close")
                ts = _parse_iso(q.get("time_close"))
                if ts is None or close is None:
                    continue
                close = float(close)
                # a glitched historical bar must not seed a poisoned baseline
                if not math.isfinite(close) or close <= 0:
                    continue
                bars.append((ts, close))
            # the API returns oldest-first today; sort so callers never rely on it
            bars.sort(key=lambda b: b[0])
            out[sym] = bars
        return out

    def fear_and_greed(self) -> int:
        """Latest CMC Fear & Greed index value (0-100)."""
        data = self._get("/v3/fear-and-greed/latest", {})
        return int(data["value"])
