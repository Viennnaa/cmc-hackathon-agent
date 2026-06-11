"""Main loop: SENSE -> DECIDE -> RISK -> EXECUTE -> RECORD.

Run:  python -m agent.runner            (continuous paper trading)
      python -m agent.runner --once     (single tick, for testing)
"""

import argparse
import logging
import time

from agent import config
from agent.data.cmc import CMCClient, CMCError
from agent.data.store import PriceStore
from agent.execution.paper import PaperExecutor
from agent.execution.portfolio import Portfolio
from agent.record.journal import Journal
from agent.risk.engine import RiskEngine
from agent.strategy import momentum

log = logging.getLogger("agent")

PORTFOLIO_PATH = config.DATA_DIR / "portfolio.json"
PRICES_PATH = config.DATA_DIR / "prices.sqlite"
JOURNAL_PATH = config.DATA_DIR / "journal.jsonl"
LEDGER_PATH = config.DATA_DIR / "ledger.jsonl"


def tick(cmc: CMCClient, store: PriceStore, portfolio: Portfolio,
         risk: RiskEngine, executor: PaperExecutor, journal: Journal) -> None:
    quotes = cmc.quotes(config.UNIVERSE, config.QUOTE_ASSET)
    try:
        fear_greed = cmc.fear_and_greed()
    except CMCError as e:
        log.warning("fear&greed unavailable: %s", e)
        fear_greed = None

    for sym, q in quotes.items():
        store.append(sym, q.timestamp, q.price)
    portfolio.mark({s: q.price for s, q in quotes.items()})

    # portfolio-level gates first (kill switch / daily cap flatten everything)
    flatten = risk.portfolio_gates(portfolio)
    if flatten:
        journal.event(flatten.rule, flatten.detail, portfolio.equity())
        log.warning("RISK GATE: %s — %s", flatten.rule, flatten.detail)
        for sym in list(portfolio.positions):
            if sym in quotes:
                journal.fill(executor.sell(portfolio, sym, quotes[sym].price))
        return

    for sym, q in quotes.items():
        # hard stop-loss outranks the strategy
        stop = risk.stop_loss_check(portfolio, sym, q.price)
        if stop:
            sig = momentum.Signal(sym, "exit", "overridden by stop_loss")
            journal.decision(sym, q, sig, stop, fear_greed, portfolio.equity())
            journal.fill(executor.sell(portfolio, sym, q.price))
            log.info("%s STOP-LOSS exit @ %.4f", sym, q.price)
            continue

        sig = momentum.evaluate(sym, store.series(sym), sym in portfolio.positions, fear_greed)
        verdict = risk.review(sig.action, sym, portfolio)
        journal.decision(sym, q, sig, verdict, fear_greed, portfolio.equity())

        if verdict.approved and verdict.action == "enter":
            fill = executor.buy(portfolio, sym, verdict.size_usdt, q.price)
            journal.fill(fill)
            log.info("%s ENTER %.6f @ %.4f (%s)", sym, fill.qty, fill.price, sig.reason)
        elif verdict.approved and verdict.action == "exit" and sym in portfolio.positions:
            fill = executor.sell(portfolio, sym, q.price)
            journal.fill(fill)
            log.info("%s EXIT @ %.4f pnl=%.4f (%s)", sym, fill.price, fill.pnl_usdt, sig.reason)

    portfolio.save(PORTFOLIO_PATH)
    log.info("equity %.2f USDT | cash %.2f | positions %s | f&g %s",
             portfolio.equity(), portfolio.cash,
             list(portfolio.positions) or "none", fear_greed)


def main() -> None:
    parser = argparse.ArgumentParser(description="CMC hackathon paper-trading agent")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = config.get_settings()
    if settings.mode != "paper":
        raise SystemExit("live mode not implemented yet — set AGENT_MODE=paper")

    cmc = CMCClient(settings.cmc_api_key)
    store = PriceStore(PRICES_PATH)
    portfolio = Portfolio.load(PORTFOLIO_PATH, settings.starting_capital)
    risk = RiskEngine()
    executor = PaperExecutor()
    journal = Journal(JOURNAL_PATH, LEDGER_PATH)

    log.info("starting in %s mode | capital %.2f USDT | universe %s | poll %ss",
             settings.mode, portfolio.equity(), config.UNIVERSE, settings.poll_interval)

    while True:
        try:
            tick(cmc, store, portfolio, risk, executor, journal)
        except CMCError as e:
            log.error("tick failed: %s", e)
        if args.once:
            break
        if risk.killed:
            log.warning("kill switch engaged — agent stopped")
            break
        time.sleep(settings.poll_interval)


if __name__ == "__main__":
    main()
