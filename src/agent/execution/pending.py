"""Crash-safe order intents.

A live swap and the portfolio save that records it cannot be atomic, so a
pending-order file bridges the gap: written immediately before every order,
cleared only after the post-fill portfolio state is on disk. If the process
dies in between, the file survives and the runner refuses to trade until a
human reconciles the wallet against portfolio.json (see deploy/DEPLOY.md).
Without this, a crash after an on-chain buy would replay as a duplicate buy.
"""

import json
import os
import time

from agent import config

PENDING_PATH = config.DATA_DIR / "pending_order.json"


def write(side: str, symbol: str, amount: float) -> None:
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PENDING_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"side": side, "symbol": symbol,
                               "amount": amount, "ts": time.time()}))
    os.replace(tmp, PENDING_PATH)


def clear() -> None:
    PENDING_PATH.unlink(missing_ok=True)


def read() -> dict | None:
    """The recorded intent, {"corrupt": True} if unreadable, None if absent."""
    if not PENDING_PATH.exists():
        return None
    try:
        return json.loads(PENDING_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"corrupt": True}
