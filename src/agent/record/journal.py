"""RECORD layer: append-only JSONL decision journal + trade ledger.

One journal line per symbol per tick: inputs -> signal -> risk verdict ->
action. This is the artifact judges replay to verify rule adherence, so it
must capture *why* even when the action is "hold".
"""

import json
import time
from dataclasses import asdict
from pathlib import Path


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

    def event(self, kind: str, detail: str, equity: float | None = None) -> None:
        self._append(self._journal, {"event": kind, "detail": detail, "equity": equity})

    def fill(self, fill: object) -> None:
        self._append(self._ledger, asdict(fill))
