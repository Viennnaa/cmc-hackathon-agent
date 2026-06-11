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
import time
from dataclasses import dataclass, field

import requests

from agent import config
from agent.execution.paper import PaperExecutor
from agent.execution.portfolio import Portfolio
from agent.risk.engine import RiskEngine
from agent.strategy import mean_revert, momentum

STRATEGIES = {"momentum": momentum.evaluate, "mean_revert": mean_revert.evaluate}

BINANCE_URL = "https://api.binance.com/api/v3/klines"
PAIRS = {"BNB": "BNBUSDT", "BTC": "BTCUSDT", "ETH": "ETHUSDT",
         "SOL": "SOLUSDT", "XRP": "XRPUSDT", "CAKE": "CAKEUSDT"}
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


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
                 strategy: str = "momentum") -> Report:
    evaluate = STRATEGIES[strategy]
    portfolio = Portfolio(cash=starting_capital)
    risk = RiskEngine()
    executor = PaperExecutor()
    report = Report(starting_capital=starting_capital)

    # align on the shortest series; assume identical candle grid across pairs
    n = min(len(s) for s in series.values())
    window: dict[str, list[float]] = {sym: [] for sym in series}

    for i in range(n):
        ts = series[next(iter(series))][i][0]
        prices = {sym: series[sym][i][1] for sym in series}
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
            sig = evaluate(
                sym, w[-config.MIN_HISTORY * 4:],
                sym in portfolio.positions, fear_greed=None,
                change_24h=change_24h,
            )
            verdict = risk.review(sig.action, sym, portfolio, now=ts)
            if verdict.approved and verdict.action == "enter":
                executor.buy(portfolio, sym, verdict.size_usdt, px)
            elif verdict.approved and verdict.action == "exit" and sym in portfolio.positions:
                fill = executor.sell(portfolio, sym, px)
                risk.note_exit(sym, now=ts)
                report.trades += 1
                report.wins += fill.pnl_usdt > 0
            elif not verdict.approved and verdict.rule in ("daily_halt", "kill_switch"):
                report._fired(verdict.rule)

        eq = portfolio.equity()
        dd = (portfolio.peak_equity - eq) / portfolio.peak_equity
        report.max_drawdown_pct = max(report.max_drawdown_pct, dd)

    # liquidate remainder for a clean final number
    last = {sym: series[sym][n - 1][1] for sym in series}
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

    report = run_backtest(series, args.capital,
                          window_bar_seconds=INTERVAL_MS[args.interval] / 1000,
                          strategy=args.strategy)
    print(f"\n=== {args.strategy} {args.days}d @ {args.interval} ===")
    print(report.summary())


if __name__ == "__main__":
    main()
