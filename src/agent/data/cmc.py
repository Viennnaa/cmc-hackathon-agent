"""SENSE layer: CoinMarketCap Data API client.

Free tier gives quotes/latest and fear-and-greed but NOT historical OHLCV,
so the agent builds its own price series by sampling quotes every poll
(persisted via PriceStore). If the hackathon grants a higher tier we can
backfill from /v2/cryptocurrency/ohlcv/historical without touching callers.
"""

import time
from dataclasses import dataclass

import requests

BASE_URL = "https://pro-api.coinmarketcap.com"


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
        resp = self._session.get(f"{BASE_URL}{path}", params=params, timeout=30)
        body = resp.json()
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
