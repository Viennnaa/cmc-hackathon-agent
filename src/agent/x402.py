"""x402 autonomous micropayment loop (special-prize demonstration).

Once an hour, when enabled, the agent pays CMC's x402 endpoint $0.01 in USDC on
Base (EIP-3009, gasless) for a fresh quote — proving it makes real on-chain
micropayments autonomously, the hook for the Best-TWAK / CMC-Hub / x402 prizes.
The paid quote is cross-checked against the agent's primary header-API feed, so
the payment buys a real data-integrity signal, not just a demo transaction.

Hard guarantees (it spends real money):
  - OFF unless settings.x402_enabled (X402_ENABLED in the env)
  - never spends past settings.x402_max_spend_usd (persisted running total)
  - throttled to one payment / X402_INTERVAL_SECONDS, restart-safe via state
  - every failure is contained here; the trading loop never sees it

The exact shape of `twak x402 request --json` is confirmed by the first real
payment (TODO(verify), like the swap dry-run); _extract tolerates the likely
wrappers so a shape drift degrades to "tx logged, no cross-check" not a crash.
"""
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from agent import config

log = logging.getLogger("agent")

X402_STATE_PATH = config.DATA_DIR / "x402_state.json"

_TX_KEYS = ("transaction", "transactionHash", "txHash", "hash", "explorer", "transactionUrl", "tx")


@dataclass
class X402State:
    spent_usd: float = 0.0
    calls: int = 0
    last_ts: float = 0.0
    budget_logged: bool = False
    # last-payment summary, read O(1) by the dashboard (an hourly event would
    # fall outside the bounded journal tail late in the day)
    last_tx: str | None = None
    last_price: float | None = None
    last_primary_price: float | None = None
    last_delta_pct: float | None = None
    last_iso: str | None = None

    def save(self, path: Path | None = None) -> None:
        path = path or X402_STATE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path | None = None) -> "X402State":
        path = path or X402_STATE_PATH
        state = cls()
        if path.exists():
            data = json.loads(path.read_text())
            for k, v in data.items():
                if hasattr(state, k):
                    setattr(state, k, v)
        return state


def _find_tx(resp: dict) -> str | None:
    """On-chain payment reference from the paid response, searching the top
    level and a few likely payment sub-blocks."""
    blocks = [resp]
    for k in ("payment", "settlement", "paymentResponse", "receipt", "x402"):
        v = resp.get(k)
        if isinstance(v, dict):
            blocks.append(v)
    for b in blocks:
        for k in _TX_KEYS:
            v = b.get(k)
            if v:
                return str(v)
    return None


def _find_cmc_payload(resp: dict) -> dict | None:
    """The raw CMC response ({"data": ..., "status": ...}) inside whatever
    envelope twak returns. The x402 v3 body IS that response at the top level."""
    if not isinstance(resp, dict):
        return None
    if "data" in resp and "status" in resp:
        return resp
    for k in ("data", "body", "response", "result", "json", "payload"):
        cand = resp.get(k)
        if isinstance(cand, dict) and "data" in cand:
            return cand
    return None


def _quote_price(payload: dict) -> float | None:
    """Convert-currency price for X402_QUOTE_SYMBOL from a CMC quotes payload.

    Confirmed against a real x402 v3 response (2026-06-16): `data` is a LIST of
    coins sharing the symbol (scam dupes included) and each coin's `quote` is a
    LIST of convert objects — pick the canonical coin by lowest cmc_rank, then
    the convert entry by symbol. The legacy v2 dict shapes (data/quote keyed by
    symbol/convert) are tolerated too.
    """
    sym, convert = config.X402_QUOTE_SYMBOL, config.QUOTE_ASSET
    data = payload.get("data")
    if isinstance(data, list):
        matches = [c for c in data if isinstance(c, dict) and c.get("symbol") == sym] or data
        # real coins carry a numeric cmc_rank; meme dupes are None -> sorted last
        coin = min(matches, key=lambda c: (c.get("cmc_rank") is None, c.get("cmc_rank") or 0),
                   default=None)
    elif isinstance(data, dict):
        node = data.get(sym)
        coin = node[0] if isinstance(node, list) else node
    else:
        coin = None
    if not isinstance(coin, dict):
        return None
    quote = coin.get("quote")
    if isinstance(quote, list):
        cell = next((q for q in quote if isinstance(q, dict) and q.get("symbol") == convert), None)
    elif isinstance(quote, dict):
        cell = quote.get(convert)
    else:
        cell = None
    try:
        return float(cell["price"])
    except (KeyError, TypeError, ValueError):
        return None


def _extract(resp: dict) -> tuple[float | None, str | None]:
    """(paid quote price for X402_QUOTE_SYMBOL, on-chain tx ref or None).

    eip3009 settles server-side, so the x402 v3 body carries no tx hash (tx is
    None; on-chain proof is the wallet's USDC transfers on BaseScan). The tx
    search is kept for forward-compat in case twak ever surfaces one.
    """
    payload = _find_cmc_payload(resp)
    price = _quote_price(payload) if payload else None
    return price, _find_tx(resp)


def _latest_price(store, symbol: str) -> float | None:
    series = store.series(symbol, 1)
    return series[-1] if series else None


def maybe_pay(client, store, journal, settings, state: X402State,
              now: float | None = None) -> bool:
    """Make the hourly x402 payment if due and within budget. Returns True only
    when a payment was actually attempted (the runner gates the call on
    settings.x402_enabled; the throttle/budget short-circuits live here so the
    state survives restarts)."""
    now = now or time.time()
    if now - state.last_ts < config.X402_INTERVAL_SECONDS:
        return False
    if state.spent_usd + config.X402_COST_USD > settings.x402_max_spend_usd + 1e-9:
        if not state.budget_logged:
            log.info("x402 budget reached ($%.2f / $%.2f over %d calls) — pausing payments",
                     state.spent_usd, settings.x402_max_spend_usd, state.calls)
            state.budget_logged = True
            state.save()
        return False

    # advance the throttle BEFORE paying so a failure waits a full hour rather
    # than retrying (and possibly re-charging) every tick
    state.last_ts = now
    state.save()
    try:
        resp = client.x402_request(config.X402_QUOTE_URL, config.X402_NETWORK,
                                   config.X402_ASSET, config.X402_METHOD,
                                   config.X402_MAX_PAYMENT_ATOMIC)
    except Exception as e:  # noqa: BLE001 — journaled here; the trading loop never sees it
        log.warning("x402 payment failed: %s", e)
        journal.event("x402_error", f"x402 payment failed: {e}", None)
        return False

    paid_price, tx = _extract(resp)
    primary = _latest_price(store, config.X402_QUOTE_SYMBOL)
    delta = ((paid_price - primary) / primary) if (paid_price and primary) else None

    state.spent_usd = round(state.spent_usd + config.X402_COST_USD, 4)
    state.calls += 1
    state.last_tx = tx
    state.last_price = paid_price
    state.last_primary_price = primary
    state.last_delta_pct = round(delta * 100, 4) if delta is not None else None
    state.last_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    state.save()

    sym = config.X402_QUOTE_SYMBOL
    detail = f"paid ${config.X402_COST_USD:.2f} USDC on Base for a {sym} quote"
    if tx:
        detail += f"; tx {tx}"
    if delta is not None:
        detail += (f"; paid {paid_price:.2f} vs feed {primary:.2f} "
                   f"({state.last_delta_pct:+.3f}%)")
    detail += f"; total ${state.spent_usd:.2f} over {state.calls} calls"
    journal.event("x402_payment", detail, None, extra={
        "tx": tx, "symbol": sym, "paid_price": paid_price, "primary_price": primary,
        "delta_pct": state.last_delta_pct, "cost_usd": config.X402_COST_USD,
        "spent_usd": state.spent_usd, "calls": state.calls,
    })
    log.info("x402: %s", detail)
    return True
