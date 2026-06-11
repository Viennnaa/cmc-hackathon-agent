"""RISK layer: hard gates with final say over every strategy intent.

Implements the judged rules from config:
  1. max 20% of capital per position
  2. per-trade stop-loss -3%
  3. daily loss cap -5% -> flatten everything + halt 24h
  4. kill switch at -10% drawdown from peak equity -> flatten + permanent stop

Every verdict carries the rule that fired so the journal shows
inputs -> rule -> action for the judges' replay.
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from agent import config
from agent.execution.portfolio import Portfolio


@dataclass
class Verdict:
    approved: bool
    action: str          # "enter" | "exit" | "flatten_all" | "none"
    rule: str            # which rule fired / authorized
    detail: str
    size_usdt: float = 0.0


class RiskEngine:
    def __init__(self) -> None:
        self.halted_until: float = 0.0
        self.killed: bool = False
        self.last_exit: dict[str, float] = {}

    def note_exit(self, symbol: str, now: float | None = None) -> None:
        """Record an exit so re-entries respect the cooldown (anti-churn)."""
        self.last_exit[symbol] = now or time.time()

    # --- persistence: judged halts MUST survive restarts ----------------------
    # Without this, systemd restarting the process would silently void the
    # kill switch and 24h halt — the exact rules judges score adherence to.
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "halted_until": self.halted_until,
            "killed": self.killed,
            "last_exit": self.last_exit,
        }))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> "RiskEngine":
        eng = cls()
        if path.exists():
            data = json.loads(path.read_text())
            eng.halted_until = float(data.get("halted_until") or 0.0)
            eng.killed = bool(data.get("killed") or False)
            eng.last_exit = {k: float(v) for k, v in (data.get("last_exit") or {}).items()}
        return eng

    # --- portfolio-level checks, run BEFORE strategy intents -----------------
    def portfolio_gates(self, portfolio: Portfolio, now: float | None = None) -> Verdict | None:
        """Returns a flatten verdict if a portfolio-level rule fires, else None."""
        now = now or time.time()
        equity = portfolio.equity()

        if self.killed:
            return None  # already flat and stopped; nothing more to do

        drawdown = (portfolio.peak_equity - equity) / portfolio.peak_equity
        if drawdown >= config.KILL_SWITCH_DRAWDOWN_PCT:
            self.killed = True
            return Verdict(
                True, "flatten_all", "kill_switch",
                f"drawdown {drawdown:.2%} >= {config.KILL_SWITCH_DRAWDOWN_PCT:.0%} from peak "
                f"{portfolio.peak_equity:.2f} -> flatten and permanent stop",
            )

        daily_loss = (portfolio.day_start_equity - equity) / portfolio.day_start_equity
        if daily_loss >= config.DAILY_LOSS_CAP_PCT and now >= self.halted_until:
            self.halted_until = now + config.HALT_HOURS * 3600
            return Verdict(
                True, "flatten_all", "daily_loss_cap",
                f"daily loss {daily_loss:.2%} >= {config.DAILY_LOSS_CAP_PCT:.0%} "
                f"-> flatten and halt {config.HALT_HOURS}h",
            )
        return None

    def stop_loss_check(self, portfolio: Portfolio, symbol: str, price: float) -> Verdict | None:
        """Per-position stop: exit if price fell 3% below entry."""
        pos = portfolio.positions.get(symbol)
        if not pos:
            return None
        loss = (pos.entry_price - price) / pos.entry_price
        if loss >= config.STOP_LOSS_PCT:
            return Verdict(
                True, "exit", "stop_loss",
                f"{symbol} {loss:.2%} below entry {pos.entry_price:.4f} "
                f">= {config.STOP_LOSS_PCT:.0%} stop",
            )
        return None

    # --- gate on strategy intents --------------------------------------------
    def review(self, intent: str, symbol: str, portfolio: Portfolio, now: float | None = None) -> Verdict:
        now = now or time.time()

        if self.killed:
            return Verdict(False, "none", "kill_switch", "agent killed; no further trading")
        if intent == "exit":
            return Verdict(True, "exit", "strategy_exit", "exits always allowed")
        if intent != "enter":
            return Verdict(False, "none", "no_action", "hold")

        if now < self.halted_until:
            remaining = (self.halted_until - now) / 3600
            return Verdict(False, "none", "daily_halt", f"halted for another {remaining:.1f}h")
        if symbol in portfolio.positions:
            return Verdict(False, "none", "single_position", f"already holding {symbol}")
        since_exit = now - self.last_exit.get(symbol, float("-inf"))
        if since_exit < config.REENTRY_COOLDOWN_SECONDS:
            remaining = (config.REENTRY_COOLDOWN_SECONDS - since_exit) / 60
            return Verdict(False, "none", "reentry_cooldown",
                           f"{symbol} exited {since_exit / 60:.0f}m ago; {remaining:.0f}m cooldown left")

        size = portfolio.equity() * config.MAX_POSITION_PCT
        if size > portfolio.cash:
            return Verdict(False, "none", "insufficient_cash",
                           f"need {size:.2f} USDT, have {portfolio.cash:.2f}")
        return Verdict(True, "enter", "position_sizing",
                       f"approved {size:.2f} USDT = {config.MAX_POSITION_PCT:.0%} of equity",
                       size_usdt=size)
