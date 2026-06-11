"""SENSE layer: CoinMarketCap Data API client.

Free tier gives quotes/latest and fear-and-greed but NOT historical OHLCV,
so the agent builds its own price series by sampling quotes every poll
(persisted via PriceStore). If the hackathon grants a higher tier we can
backfill from /v2/cryptocurrency/ohlcv/historical without touching callers.
"""

import time
from dataclasses import dataclass
from datetime import datetime

import requests

from agent import config

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
            out[sym] = Quote(
                symbol=sym,
                price=q["price"],
                volume_24h=q.get("volume_24h") or 0.0,
                percent_change_24h=q.get("percent_change_24h") or 0.0,
                timestamp=now,
            )
        return out

    def fear_and_greed(self) -> int:
        """Latest CMC Fear & Greed index value (0-100)."""
        data = self._get("/v3/fear-and-greed/latest", {})
        return int(data["value"])
