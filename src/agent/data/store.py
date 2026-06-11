"""Price history store: SQLite-backed series of sampled quotes per symbol.

Indicators (RSI/MACD) are computed over this self-built series since the
free CMC tier has no historical OHLCV endpoint.
"""

import sqlite3
import time
from pathlib import Path


class PriceStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS prices (
                symbol TEXT NOT NULL,
                ts REAL NOT NULL,
                price REAL NOT NULL,
                PRIMARY KEY (symbol, ts)
            )"""
        )
        self._conn.commit()

    def append(self, symbol: str, ts: float, price: float) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO prices (symbol, ts, price) VALUES (?, ?, ?)",
            (symbol, ts, price),
        )
        self._conn.commit()

    def series(self, symbol: str, limit: int = 500) -> list[float]:
        """Most recent `limit` prices, oldest first."""
        rows = self._conn.execute(
            "SELECT price FROM prices WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
        return [r[0] for r in reversed(rows)]

    def bars(self, symbol: str, bar_seconds: int, limit: int = 200) -> list[float]:
        """Bar closes (last sample per bucket), oldest first.

        The current, still-forming bucket is excluded so signals only ever
        fire on completed bars — same semantics as backtest candles.
        """
        rows = self._conn.execute(
            """SELECT CAST(ts / ? AS INTEGER) AS bucket, price, MAX(ts)
               FROM prices WHERE symbol = ?
               GROUP BY bucket ORDER BY bucket DESC LIMIT ?""",
            (bar_seconds, symbol, limit + 1),
        ).fetchall()
        current_bucket = int(time.time() // bar_seconds)
        closes = [r[1] for r in rows if r[0] != current_bucket]
        return list(reversed(closes[:limit]))

    def count(self, symbol: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM prices WHERE symbol = ?", (symbol,)
        ).fetchone()[0]

    def close(self) -> None:
        self._conn.close()
