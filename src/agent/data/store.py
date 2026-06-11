"""Price history store: SQLite-backed series of sampled quotes per symbol.

Indicators (RSI/MACD) are computed over this self-built series since the
free CMC tier has no historical OHLCV endpoint.
"""

import sqlite3
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

    def count(self, symbol: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM prices WHERE symbol = ?", (symbol,)
        ).fetchone()[0]

    def close(self) -> None:
        self._conn.close()
