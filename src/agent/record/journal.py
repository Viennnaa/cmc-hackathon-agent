"""RECORD layer: append-only JSONL decision journal + trade ledger.

One journal line per symbol per tick: inputs -> signal -> risk verdict ->
action. This is the artifact judges replay to verify rule adherence, so it
must capture *why* even when the action is "hold".
"""

import json
import os
import time
from dataclasses import asdict
from pathlib import Path


def read_jsonl_tail(path: Path, limit: int) -> list[dict]:
    """Last `limit` records, reading blocks from EOF — the journal grows
    unbounded over the window, so hot paths (narrator, dashboard) must not
    slurp the whole file every poll. Torn/corrupt lines are skipped."""
    if not path.exists():
        return []
    chunk = 64 * 1024
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        # accumulate chunks and join once: prepending to one buffer re-copies
        # it per chunk (quadratic — ~3.6x slower at a 20k-line tail)
        parts: list[bytes] = []
        newlines = 0
        while pos > 0 and newlines <= limit:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            part = f.read(step)
            parts.append(part)
            newlines += part.count(b"\n")
    buf = b"".join(reversed(parts))
    out = []
    for line in buf.splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # torn/corrupt line must not cost a valid record below
    return out[-limit:]


class Journal:
    def __init__(self, journal_path: Path, ledger_path: Path):
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._journal = journal_path
        self._ledger = ledger_path

    def _append(self, path: Path, record: dict) -> None:
        record["ts"] = record.get("ts") or time.time()
        record["iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record["ts"]))
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def decision(self, symbol: str, quote: object, signal: object, verdict: object,
                 fear_greed: int | None, equity: float) -> None:
        self._append(self._journal, {
            "symbol": symbol,
            "inputs": {"quote": asdict(quote), "fear_greed": fear_greed},
            "signal": asdict(signal),
            "risk_verdict": asdict(verdict),
            "equity": round(equity, 4),
        })

    def event(self, kind: str, detail: str, equity: float | None = None,
              extra: dict | None = None) -> None:
        record = {"event": kind, "detail": detail, "equity": equity}
        if extra:
            record.update(extra)  # structured fields for machine-readable replay
        self._append(self._journal, record)

    def fill(self, fill: object) -> None:
        self._append(self._ledger, asdict(fill))
