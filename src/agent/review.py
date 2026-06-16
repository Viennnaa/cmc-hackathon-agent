"""ADAPT layer: nightly self-review — the agent re-fits itself to the market.

Once per UTC day the agent replays its OWN sampled price history (no
external data fetch — Binance is geo-blocked on the VPS anyway) through
every strategy in the menu and adopts the best trailing performer for
the next day. The full scorecard is journaled so the judged replay shows
inputs -> evaluation -> decision for every switch.

NARROW-ONLY invariant: the review can reduce exposure (size_factor < 1
when even the best strategy lost badly) but can never raise it past 1.0
or touch any risk constant. The judged rules in config stay immutable;
clamps here are belt-and-braces.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from agent import config
from agent.data.store import PriceStore
from agent.record.journal import Journal
from agent.strategy import DEFAULT_STRATEGY, STRATEGIES

log = logging.getLogger("agent")

STRATEGY_STATE_PATH = config.DATA_DIR / "strategy_state.json"
# Latest scorecard snapshot for the dashboard to read O(1). The full history
# lives in the journal (self_review events); a once-a-day event sits far
# outside any bounded journal tail, so the dashboard reads this file instead —
# same view-only pattern as strategy_state.json / risk_state.json.
SELF_REVIEW_PATH = config.DATA_DIR / "self_review.json"

# replay capital is nominal — only relative return/drawdown matter here
_REPLAY_CAPITAL = 150.0


@dataclass
class StrategyState:
    strategy: str = DEFAULT_STRATEGY
    size_factor: float = 1.0
    reviewed_day: int = 0  # UTC day number of the last completed review

    def save(self, path: Path | None = None) -> None:
        path = path or STRATEGY_STATE_PATH  # resolved at call time (tests repoint it)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "strategy": self.strategy,
            "size_factor": self.size_factor,
            "reviewed_day": self.reviewed_day,
        }))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path | None = None) -> "StrategyState":
        path = path or STRATEGY_STATE_PATH
        state = cls()
        if path.exists():
            data = json.loads(path.read_text())
            strategy = data.get("strategy")
            if strategy in STRATEGIES:
                state.strategy = strategy
            # narrow-only survives even a hand-edited state file
            state.size_factor = min(float(data.get("size_factor", 1.0)), 1.0)
            state.reviewed_day = int(data.get("reviewed_day", 0))
        return state


def _score(ret: float, drawdown: float) -> float:
    """PnL-first ranking for the competition (judged on total return, with
    drawdown only as a ~30% disqualification gate). Pick the highest-return
    strategy that stays clear of the DQ line: a trailing drawdown that would
    have tripped the kill switch is penalized so the review never adopts a
    DQ-prone variant; otherwise return is what wins.
    """
    if drawdown >= config.KILL_SWITCH_DRAWDOWN_PCT:
        return ret - 10.0  # would have been killed -> never adopt
    return ret


def maybe_review(state: StrategyState, store: PriceStore, journal: Journal,
                 equity: float | None = None, now: float | None = None) -> bool:
    """Run the nightly review if a new UTC day has started. Returns True if run."""
    now = now or time.time()
    today = int(now // 86_400)
    if today <= state.reviewed_day:
        return False

    limit = config.SELF_REVIEW_TRAILING_DAYS * 86_400 // config.BAR_SECONDS
    series = {sym: store.bar_series(sym, config.BAR_SECONDS, limit)
              for sym in config.UNIVERSE}
    n_bars = min((len(s) for s in series.values()), default=0)
    if n_bars < config.SELF_REVIEW_MIN_BARS:
        log.info("self-review skipped: %d bars < %d minimum",
                 n_bars, config.SELF_REVIEW_MIN_BARS)
        state.reviewed_day = today  # one attempt per day, not one per tick
        state.save()
        return False

    from agent.backtest import run_backtest  # deferred: keeps runner import light

    # NOTE: run_backtest aligns symbols by timestamp intersection (2026-06-12);
    # the first review after that change replays a different bar set than the
    # old index alignment, so a one-time scorecard jump is expected, not state
    # corruption.
    results = {}
    for name in STRATEGIES:
        report = run_backtest(series, _REPLAY_CAPITAL,
                              window_bar_seconds=config.BAR_SECONDS, strategy=name)
        ret = (report.final_equity - _REPLAY_CAPITAL) / _REPLAY_CAPITAL
        results[name] = {"return": round(ret, 4),
                         "max_drawdown": round(report.max_drawdown_pct, 4),
                         "trades": report.trades,
                         "score": round(_score(ret, report.max_drawdown_pct), 4)}

    # ties are common (quiet stretches where nothing trades = identical zero
    # scores) — never switch on a tie: prefer the incumbent, then the default
    best = max(results, key=lambda k: (results[k]["score"],
                                       k == state.strategy,
                                       k == DEFAULT_STRATEGY))
    factor = (config.SELF_REVIEW_DEFENSIVE_SIZE_FACTOR
              if results[best]["return"] < config.SELF_REVIEW_DEFENSIVE_RETURN
              else 1.0)

    switched = best != state.strategy or factor != state.size_factor
    state.strategy = best
    state.size_factor = min(factor, 1.0)  # narrow-only, enforced
    state.reviewed_day = today
    state.save()

    detail = (f"trailing {n_bars} bars: "
              + "; ".join(f"{k} ret {v['return']:+.2%} dd {v['max_drawdown']:.2%} "
                          f"score {v['score']:+.4f}" for k, v in results.items())
              + f" -> {'SWITCH to' if switched else 'keep'} {best}"
              + f" @ size factor {state.size_factor}")
    snapshot = {
        "reviewed_day": today,
        "ts": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "trailing_bars": n_bars,
        "adopted": best,
        "size_factor": state.size_factor,
        "switched": switched,
        "scorecard": results,
    }
    _write_snapshot(snapshot)
    # structured fields ride alongside the human-readable detail so the judged
    # replay (and the dashboard's history, if it ever needs more than the latest)
    # is machine-readable, not a string to re-parse
    journal.event("self_review", detail, equity, extra={k: snapshot[k] for k in
                  ("reviewed_day", "trailing_bars", "adopted", "size_factor",
                   "switched", "scorecard")})
    log.info("self-review: %s", detail)
    return True


def _write_snapshot(data: dict, path: Path | None = None) -> None:
    """Atomically write the latest scorecard (tmp + os.replace), so a reader
    never sees a half-written file — same discipline as StrategyState.save."""
    path = path or SELF_REVIEW_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)
