"""Portfolio state shared by the paper broker and risk engine.

Persisted to JSON so the agent survives restarts mid-window (judges replay
the full ledger; losing state would corrupt rule-adherence evidence).
"""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float
    entry_ts: float


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    day_start_ts: float = 0.0
    last_prices: dict[str, float] = field(default_factory=dict)
    mode: str = ""  # "paper" | "live" — guards against mixing paper history into live baselines
    # judged return baseline: starting capital (paper) or the live_rebase wallet
    # amount. 0.0 = unknown (pre-upgrade state file); readers must fall back.
    baseline_equity: float = 0.0
    # host that entered live mode — two machines trading one wallet would
    # double-trade, so a live start on a different host refuses (runner)
    live_host: str = ""

    def __post_init__(self) -> None:
        if self.peak_equity == 0.0:
            self.peak_equity = self.cash
        if self.day_start_equity == 0.0:
            self.day_start_equity = self.cash
        if self.day_start_ts == 0.0:
            self.day_start_ts = time.time()

    def equity(self) -> float:
        held = sum(
            p.qty * self.last_prices.get(p.symbol, p.entry_price)
            for p in self.positions.values()
        )
        return self.cash + held

    def mark(self, prices: dict[str, float], ts: float | None = None) -> None:
        """Update marks, peak equity, and roll the daily window at UTC midnight.

        `ts` defaults to wall clock; backtests pass simulated time.
        """
        self.last_prices.update(prices)
        eq = self.equity()
        self.peak_equity = max(self.peak_equity, eq)
        now = ts or time.time()
        if time.gmtime(now).tm_yday != time.gmtime(self.day_start_ts).tm_yday:
            self.day_start_equity = eq
            self.day_start_ts = now

    # --- persistence ----------------------------------------------------------
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, path)  # atomic: a crash never leaves half-written state

    @classmethod
    def load(cls, path: Path, starting_capital: float) -> "Portfolio":
        if not path.exists():
            return cls(cash=starting_capital, baseline_equity=starting_capital)
        data = json.loads(path.read_text())
        data["positions"] = {
            sym: Position(**pos) for sym, pos in data["positions"].items()
        }
        return cls(**data)
