"""Backtest harness: replay historical candles through the LIVE components.

Uses the same momentum.evaluate, RiskEngine, and PaperExecutor as the
runner — the point is validating the code that will trade, not a parallel
reimplementation that can drift.

Historical data comes from Binance public klines (no API key; free CMC
tier has no OHLCV). This is offline validation only — the judged live
data path stays 100% CMC.

Run:  python -m agent.backtest --days 14 --interval 15m
"""

import argparse
import os
import time
from dataclasses import dataclass, field

import requests

from agent import config
from agent.execution.paper import PaperExecutor
from agent.execution.portfolio import Portfolio
from agent.risk.engine import RiskEngine
from agent.strategy import STRATEGIES

BINANCE_URL = "https://api.binance.com/api/v3/klines"
CMC_FNG_URL = "https://pro-api.coinmarketcap.com/v3/fear-and-greed/historical"
# Binance klines (offline backtest only) for the eligible universe. Order
# mirrors config.UNIVERSE; all have liquid USDT pairs on Binance.
PAIRS = {"ETH": "ETHUSDT", "XRP": "XRPUSDT", "DOGE": "DOGEUSDT", "ADA": "ADAUSDT",
         "LINK": "LINKUSDT", "AVAX": "AVAXUSDT", "LTC": "LTCUSDT", "AAVE": "AAVEUSDT",
         "DOT": "DOTUSDT", "UNI": "UNIUSDT", "SHIB": "SHIBUSDT", "FET": "FETUSDT",
         "INJ": "INJUSDT", "CAKE": "CAKEUSDT", "TWT": "TWTUSDT"}
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


def fetch_fng_history(days: int) -> dict[int, int]:
    """{utc_day_number: index_value} from CMC's daily Fear & Greed history.

    Keyed by ts // 86400 so any intraday bar maps to its day's reading —
    the live runner samples the same daily index, just at poll time.
    """
    resp = requests.get(CMC_FNG_URL, params={"limit": min(days + 5, 500)},
                        headers={"X-CMC_PRO_API_KEY": os.environ["CMC_API_KEY"]},
                        timeout=30)
    resp.raise_for_status()
    return {int(r["timestamp"]) // 86_400: int(r["value"])
            for r in resp.json()["data"]}


def fetch_klines(pair: str, interval: str, days: int) -> list[tuple[float, float]]:
    """[(close_ts_seconds, close_price)] oldest first, paginated past the 1000 cap."""
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    out: list[tuple[float, float]] = []
    while start < end:
        resp = requests.get(BINANCE_URL, params={
            "symbol": pair, "interval": interval,
            "startTime": start, "limit": 1000,
        }, timeout=30)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        # close time, close — drop the still-open current candle: its close
        # time is in the future and seeding it would fake a completed bar
        out.extend((r[6] / 1000.0, float(r[4])) for r in rows if r[6] / 1000.0 <= time.time())
        start = rows[-1][6] + 1
    return out


@dataclass
class Report:
    starting_capital: float
    final_equity: float = 0.0
    peak_equity: float = 0.0
    max_drawdown_pct: float = 0.0
    trades: int = 0
    wins: int = 0
    rule_firings: dict = field(default_factory=dict)

    def _fired(self, rule: str) -> None:
        self.rule_firings[rule] = self.rule_firings.get(rule, 0) + 1

    def summary(self) -> str:
        ret = (self.final_equity - self.starting_capital) / self.starting_capital
        win_rate = self.wins / self.trades if self.trades else 0.0
        lines = [
            f"final equity   {self.final_equity:.2f} USDT  ({ret:+.2%})",
            f"max drawdown   {self.max_drawdown_pct:.2%}",
            f"round trips    {self.trades}  (win rate {win_rate:.0%})",
            f"rule firings   {self.rule_firings or 'none'}",
        ]
        return "\n".join(lines)


def run_backtest(series: dict[str, list[tuple[float, float]]],
                 starting_capital: float,
                 window_bar_seconds: float = 900,
                 strategy: str = "momentum",
                 fng_by_day: dict[int, int] | None = None,
                 fng_mode: str = "veto",
                 fng_threshold: int = config.FEAR_GREED_VETO_BELOW) -> Report:
    """fng_mode applies only when fng_by_day is given:
    "veto"     — feed F&G to the strategy so its sentiment veto fires as live
                 (threshold below 20 narrows the veto to deeper fear only)
    "halfsize" — no veto; entries during F&G < threshold sized at 50%
    """
    evaluate = STRATEGIES[strategy]
    portfolio = Portfolio(cash=starting_capital)
    risk = RiskEngine()
    executor = PaperExecutor()
    report = Report(starting_capital=starting_capital)

    # Align bars across symbols by timestamp, not position: the live store
    # has per-symbol gaps (dropped stale quotes), so the i-th bars of two
    # symbols can be hours apart. Replay only timestamps every symbol has.
    if not series or any(not s for s in series.values()):
        report.final_equity = portfolio.equity()
        return report
    common_ts = sorted(set.intersection(*({ts for ts, _ in s} for s in series.values())))
    if not common_ts:
        report.final_equity = portfolio.equity()
        return report
    price_at = {sym: dict(s) for sym, s in series.items()}
    window: dict[str, list[float]] = {sym: [] for sym in series}

    for ts in common_ts:
        prices = {sym: price_at[sym][ts] for sym in series}
        for sym, px in prices.items():
            window[sym].append(px)
        portfolio.mark(prices, ts=ts)

        flatten = risk.portfolio_gates(portfolio, now=ts)
        if flatten:
            report._fired(flatten.rule)
            for sym in list(portfolio.positions):
                fill = executor.sell(portfolio, sym, prices[sym])
                risk.note_exit(sym, now=ts)
                report.trades += 1
                report.wins += fill.pnl_usdt is not None and fill.pnl_usdt > 0
            if risk.killed:
                break
            continue

        for sym, px in prices.items():
            stop = risk.stop_loss_check(portfolio, sym, px)
            if stop:
                report._fired(stop.rule)
                fill = executor.sell(portfolio, sym, px)
                risk.note_exit(sym, now=ts)
                report.trades += 1
                report.wins += fill.pnl_usdt > 0
                continue

            bars_per_day = 86_400 // int(window_bar_seconds)
            w = window[sym]
            change_24h = ((px / w[-bars_per_day - 1]) - 1) * 100 if len(w) > bars_per_day else None
            fng = fng_by_day.get(int(ts) // 86_400) if fng_by_day else None
            # the strategy's veto constant is 20; passing the reading only when
            # it is under OUR threshold lets one constant serve both variants
            # (threshold must be <= config.FEAR_GREED_VETO_BELOW)
            veto_fng = fng if (fng_mode == "veto" and fng is not None
                               and fng < fng_threshold) else None
            sig = evaluate(
                sym, w[-config.MIN_HISTORY * 4:],
                sym in portfolio.positions, fear_greed=veto_fng,
                change_24h=change_24h,
            )
            if sig.reason.startswith("sentiment veto"):
                report._fired("sentiment_veto")
            # The engine fails closed on a missing F&G reading (live rule), but
            # historical F&G is variant-controlled here: feed it the same
            # veto_fng the strategy saw, neutral 50 otherwise, so each
            # fng_mode variant keeps its intended semantics.
            verdict = risk.review(sig.action, sym, portfolio, now=ts,
                                  fear_greed=veto_fng if veto_fng is not None else 50)
            if verdict.approved and verdict.action == "enter":
                size = verdict.size_usdt
                if (fng_mode == "halfsize" and fng is not None
                        and fng < fng_threshold):
                    size *= 0.5
                    report._fired("fng_halfsize")
                executor.buy(portfolio, sym, size, px)
            elif verdict.approved and verdict.action == "exit" and sym in portfolio.positions:
                fill = executor.sell(portfolio, sym, px)
                risk.note_exit(sym, now=ts)
                report.trades += 1
                report.wins += fill.pnl_usdt > 0
            elif not verdict.approved and verdict.rule in ("daily_halt", "kill_switch",
                                                           "sentiment_veto"):
                report._fired(verdict.rule)

        eq = portfolio.equity()
        dd = (portfolio.peak_equity - eq) / portfolio.peak_equity
        report.max_drawdown_pct = max(report.max_drawdown_pct, dd)

    # liquidate remainder for a clean final number
    last = {sym: price_at[sym][common_ts[-1]] for sym in series}
    for sym in list(portfolio.positions):
        fill = executor.sell(portfolio, sym, last[sym])
        report.trades += 1
        report.wins += fill.pnl_usdt > 0
    report.final_equity = portfolio.equity()
    report.peak_equity = portfolio.peak_equity
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the live strategy on Binance klines")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--interval", choices=list(INTERVAL_MS), default="15m")
    parser.add_argument("--capital", type=float, default=150.0)
    parser.add_argument("--strategy", choices=list(STRATEGIES), default="momentum")
    parser.add_argument("--seed-store", action="store_true",
                        help="write fetched candles into the live price store "
                             "(pre-warms indicators before the trading window; "
                             "during the window the agent samples CMC only)")
    parser.add_argument("--fng-compare", action="store_true",
                        help="replay CMC Fear & Greed history and run the same "
                             "candles under four sentiment rules: no veto, "
                             "veto <20 (live), veto <10, half-size <20")
    args = parser.parse_args()

    if args.seed_store:
        from agent.data.store import PriceStore
        from agent.runner import PRICES_PATH
        store = PriceStore(PRICES_PATH)
        for sym, pair in PAIRS.items():
            candles = fetch_klines(pair, args.interval, args.days)
            for ts, price in candles:
                store.append(sym, ts, price)
            print(f"seeded {sym}: {len(candles)} candles -> {PRICES_PATH}")
        return

    series = {}
    for sym, pair in PAIRS.items():
        series[sym] = fetch_klines(pair, args.interval, args.days)
        print(f"{sym}: {len(series[sym])} candles")

    if args.fng_compare:
        fng = fetch_fng_history(args.days)
        days_under_20 = sum(1 for v in fng.values() if v < 20)
        print(f"F&G history: {len(fng)} days fetched, {days_under_20} under 20")
        variants = [
            ("no veto (locked-config baseline)", None, "veto", 20),
            ("veto <20 (live behavior)", fng, "veto", 20),
            ("veto <10", fng, "veto", 10),
            ("half-size <20", fng, "halfsize", 20),
        ]
        for label, fng_arg, mode, threshold in variants:
            report = run_backtest(series, args.capital,
                                  window_bar_seconds=INTERVAL_MS[args.interval] / 1000,
                                  strategy=args.strategy, fng_by_day=fng_arg,
                                  fng_mode=mode, fng_threshold=threshold)
            print(f"\n=== {label} | {args.strategy} {args.days}d @ {args.interval} ===")
            print(report.summary())
        return

    report = run_backtest(series, args.capital,
                          window_bar_seconds=INTERVAL_MS[args.interval] / 1000,
                          strategy=args.strategy)
    print(f"\n=== {args.strategy} {args.days}d @ {args.interval} ===")
    print(report.summary())


if __name__ == "__main__":
    main()
