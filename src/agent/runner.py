"""Main loop: SENSE -> DECIDE -> RISK -> EXECUTE -> RECORD.

Run:  python -m agent.runner            (continuous paper trading)
      python -m agent.runner --once     (single tick, for testing)

Crash-safety contract (judged: rules must hold across restarts):
  - every order is bracketed by a pending-order intent (execution/pending.py);
    an in-flight order at crash time stops the agent until reconciled
  - risk state (kill switch, daily halt, cooldowns) persists to disk
  - portfolio state is saved immediately after every fill, atomically
  - a held position with stale/missing prices is exited protectively
"""

import argparse
import logging
import time

from agent import config, narrator, review
from agent.data.cmc import CMCClient, CMCError
from agent.data.store import PriceStore
from agent.execution import pending
from agent.execution.paper import PaperExecutor
from agent.execution.portfolio import Portfolio
from agent.record.journal import Journal
from agent.risk.engine import RiskEngine
from agent.strategy import STRATEGIES, momentum

log = logging.getLogger("agent")

PORTFOLIO_PATH = config.DATA_DIR / "portfolio.json"
PRICES_PATH = config.DATA_DIR / "prices.sqlite"
JOURNAL_PATH = config.DATA_DIR / "journal.jsonl"
LEDGER_PATH = config.DATA_DIR / "ledger.jsonl"
RISK_STATE_PATH = config.DATA_DIR / "risk_state.json"

# held symbol -> ts when its quotes first went missing (protective-exit timer)
_quote_gap_since: dict[str, float] = {}


class UnreconciledOrderError(RuntimeError):
    """A previous order may have executed without being recorded."""


def _buy(executor, journal, portfolio: Portfolio, sym: str, size_usdt: float, price: float):
    pending.write("buy", sym, size_usdt)
    fill = executor.buy(portfolio, sym, size_usdt, price)
    journal.fill(fill)
    portfolio.save(PORTFOLIO_PATH)
    pending.clear()
    return fill


def _sell(executor, journal, portfolio: Portfolio, sym: str, price: float):
    qty = portfolio.positions[sym].qty if sym in portfolio.positions else 0.0
    pending.write("sell", sym, qty)
    fill = executor.sell(portfolio, sym, price)
    journal.fill(fill)
    portfolio.save(PORTFOLIO_PATH)
    pending.clear()
    return fill


def tick(cmc: CMCClient, store: PriceStore, portfolio: Portfolio,
         risk: RiskEngine, executor: PaperExecutor, journal: Journal,
         strategy_state: review.StrategyState) -> None:
    if pending.read() is not None:
        raise UnreconciledOrderError(
            "pending_order.json exists — reconcile wallet vs portfolio.json "
            "and delete the file before trading resumes (deploy/DEPLOY.md)")

    quotes = cmc.quotes(config.UNIVERSE, config.QUOTE_ASSET)
    try:
        fear_greed = cmc.fear_and_greed()
    except CMCError as e:
        log.warning("fear&greed unavailable: %s", e)
        fear_greed = None

    for sym, q in quotes.items():
        store.append(sym, q.timestamp, q.price)
    portfolio.mark({s: q.price for s, q in quotes.items()})

    # Quote-gap guard: a held symbol with no fresh price has no working
    # stop-loss. Track the gap and exit protectively at the last mark
    # rather than sit exposed (fail closed).
    now = time.time()
    for sym in list(portfolio.positions):
        if sym in quotes:
            _quote_gap_since.pop(sym, None)
            continue
        since = _quote_gap_since.setdefault(sym, now)
        gap = now - since
        log.warning("%s held but unquoted for %.0fs (stop-loss blind)", sym, gap)
        if gap >= config.STALE_QUOTE_FLATTEN_SECONDS:
            journal.event("stale_data_exit",
                          f"{sym} unquoted {gap:.0f}s >= {config.STALE_QUOTE_FLATTEN_SECONDS}s "
                          "-> protective exit at last mark", portfolio.equity())
            price = portfolio.last_prices.get(sym, portfolio.positions[sym].entry_price)
            _sell(executor, journal, portfolio, sym, price)
            risk.note_exit(sym)
            risk.save(RISK_STATE_PATH)
            _quote_gap_since.pop(sym, None)

    # portfolio-level gates first (kill switch / daily cap flatten everything)
    flatten = risk.portfolio_gates(portfolio)
    if flatten:
        journal.event(flatten.rule, flatten.detail, portfolio.equity())
        log.warning("RISK GATE: %s — %s", flatten.rule, flatten.detail)
        risk.save(RISK_STATE_PATH)  # persist killed/halted BEFORE selling: a crash mid-flatten must not re-arm trading
        for sym in list(portfolio.positions):
            price = quotes[sym].price if sym in quotes else \
                portfolio.last_prices.get(sym, portfolio.positions[sym].entry_price)
            _sell(executor, journal, portfolio, sym, price)
            risk.note_exit(sym)
        risk.save(RISK_STATE_PATH)
        portfolio.save(PORTFOLIO_PATH)
        return

    for sym, q in quotes.items():
        # hard stop-loss outranks the strategy
        stop = risk.stop_loss_check(portfolio, sym, q.price)
        if stop:
            sig = momentum.Signal(sym, "exit", "overridden by stop_loss")
            journal.decision(sym, q, sig, stop, fear_greed, portfolio.equity())
            _sell(executor, journal, portfolio, sym, q.price)
            risk.note_exit(sym)
            risk.save(RISK_STATE_PATH)
            log.info("%s STOP-LOSS exit @ %.4f", sym, q.price)
            continue

        bars = store.bars(sym, config.BAR_SECONDS)
        evaluate = STRATEGIES[strategy_state.strategy]
        sig = evaluate(sym, bars, sym in portfolio.positions, fear_greed,
                       change_24h=q.percent_change_24h)
        verdict = risk.review(sig.action, sym, portfolio)
        journal.decision(sym, q, sig, verdict, fear_greed, portfolio.equity())

        if verdict.approved and verdict.action == "enter":
            veto = executor.pre_entry_check(sym)
            if veto:
                journal.event("token_risk_veto", f"{sym}: {veto}", portfolio.equity())
                log.warning("%s entry vetoed by token risk check: %s", sym, veto)
                continue
            # narrow-only: the self-review can shrink entries, never grow them
            size = verdict.size_usdt * min(strategy_state.size_factor, 1.0)
            fill = _buy(executor, journal, portfolio, sym, size, q.price)
            log.info("%s ENTER %.6f @ %.4f (%s)", sym, fill.qty, fill.price, sig.reason)
        elif verdict.approved and verdict.action == "exit" and sym in portfolio.positions:
            fill = _sell(executor, journal, portfolio, sym, q.price)
            risk.note_exit(sym)
            risk.save(RISK_STATE_PATH)
            log.info("%s EXIT @ %.4f pnl=%.4f (%s)", sym, fill.price, fill.pnl_usdt, sig.reason)

    portfolio.save(PORTFOLIO_PATH)
    log.info("equity %.2f USDT | cash %.2f | positions %s | f&g %s",
             portfolio.equity(), portfolio.cash,
             list(portfolio.positions) or "none", fear_greed)


def _protective_flatten(executor, journal, portfolio: Portfolio, risk: RiskEngine,
                        reason: str) -> None:
    """Exit everything at last marks when prices can no longer be trusted."""
    journal.event(reason, f"protective flatten of {list(portfolio.positions)} at last marks",
                  portfolio.equity())
    for sym in list(portfolio.positions):
        price = portfolio.last_prices.get(sym, portfolio.positions[sym].entry_price)
        _sell(executor, journal, portfolio, sym, price)
        risk.note_exit(sym)
    risk.save(RISK_STATE_PATH)
    portfolio.save(PORTFOLIO_PATH)


def _reconcile_live(client, portfolio: Portfolio, journal: Journal) -> None:
    """Live capital comes from the REAL wallet, never the local file.

    Sizing the judged 20%-per-position rule against stale local capital would
    silently violate it (e.g. $30 of a $100 wallet is 30%). Fail closed: live
    trading never starts on an unreadable balance.
    """
    from agent.execution.twak import TwakError, find_usdt_balance
    try:
        data = client.portfolio()
    except TwakError as e:
        raise SystemExit(f"live reconcile failed — cannot read wallet portfolio: {e}")
    usdt = find_usdt_balance(data)
    if usdt is None:
        raise SystemExit(
            "live reconcile failed — no USDT balance found in `twak wallet portfolio` "
            f"output (verify shape on the dry run): {str(data)[:300]}")

    if portfolio.mode != "live":
        # Entering the live window: the funded wallet is the judged baseline.
        # Paper history (cash, positions, peak) must not leak into live rules.
        journal.event("live_rebase",
                      f"live baseline from wallet: {usdt:.2f} USDT "
                      f"(replacing {portfolio.mode or 'fresh'} state)", usdt)
        log.warning("live rebase: wallet %.2f USDT is the new baseline", usdt)
        portfolio.cash = usdt
        portfolio.positions.clear()
        portfolio.peak_equity = usdt
        portfolio.day_start_equity = usdt
        portfolio.day_start_ts = time.time()
        portfolio.mode = "live"
    elif not portfolio.positions:
        if abs(usdt - portfolio.cash) > 0.5:
            log.warning("wallet USDT %.2f differs from local cash %.2f — adopting "
                        "wallet (peak/day baselines kept: fail closed)", usdt, portfolio.cash)
            journal.event("live_cash_reconcile",
                          f"cash {portfolio.cash:.2f} -> wallet {usdt:.2f}", usdt)
        portfolio.cash = usdt
    else:
        log.warning("live restart holding %s — wallet token balances not auto-verified; "
                    "check `twak wallet portfolio` matches portfolio.json",
                    list(portfolio.positions))
    portfolio.save(PORTFOLIO_PATH)


def main() -> None:
    parser = argparse.ArgumentParser(description="CMC hackathon paper-trading agent")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = config.get_settings()

    # Startup guards: never trade past an unreconciled order or an engaged
    # kill switch. Clean exit (code 0) so systemd Restart=on-failure does
    # not loop us straight back into the same refusal.
    if pending.read() is not None:
        log.critical("pending_order.json exists — a previous order may have executed "
                     "on-chain without being recorded. Reconcile the wallet against "
                     "portfolio.json, delete the file, then restart (deploy/DEPLOY.md).")
        raise SystemExit(0)

    risk = RiskEngine.load(RISK_STATE_PATH)
    if risk.killed:
        log.critical("kill switch is engaged (risk_state.json) — agent stays stopped. "
                     "This is the judged permanent stop; do not clear it mid-window.")
        raise SystemExit(0)

    portfolio = Portfolio.load(PORTFOLIO_PATH, settings.starting_capital)
    journal = Journal(JOURNAL_PATH, LEDGER_PATH)

    if settings.mode == "live":
        from agent.execution.twak import TwakClient, TwakExecutor
        client = TwakClient()
        auth = client.auth_status()
        wallet = client.wallet_status()
        log.info("twak auth: %s | wallet: %s", auth, wallet)
        executor = TwakExecutor(client)
        _reconcile_live(client, portfolio, journal)
    elif settings.mode == "paper":
        executor = PaperExecutor()
        if portfolio.mode != "paper":
            portfolio.mode = "paper"
            portfolio.save(PORTFOLIO_PATH)
    else:
        raise SystemExit(f"unknown AGENT_MODE={settings.mode!r} — use paper or live")

    cmc = CMCClient(settings.cmc_api_key)
    store = PriceStore(PRICES_PATH)
    strategy_state = review.StrategyState.load()

    log.info("starting in %s mode | capital %.2f USDT | universe %s | poll %ss | "
             "strategy %s (size factor %.2f)",
             settings.mode, portfolio.equity(), config.UNIVERSE, settings.poll_interval,
             strategy_state.strategy, strategy_state.size_factor)

    cmc_down_since: float | None = None
    while True:
        try:
            tick(cmc, store, portfolio, risk, executor, journal, strategy_state)
            cmc_down_since = None
            try:
                review.maybe_review(strategy_state, store, journal, portfolio.equity())
            except Exception as e:  # noqa: BLE001 — a failed review keeps yesterday's strategy
                log.warning("self-review failed (trading unaffected): %s", e)
            try:
                narrator.maybe_narrate(portfolio, risk)
            except Exception as e:  # noqa: BLE001 — narration must never affect trading
                log.warning("narrator failed (trading unaffected): %s", e)
        except CMCError as e:
            # Transient sense failure: tolerable while flat, not while exposed —
            # no prices means no stop-loss. Flatten after the staleness budget.
            log.error("tick failed: %s", e)
            cmc_down_since = cmc_down_since or time.time()
            outage = time.time() - cmc_down_since
            if portfolio.positions and outage >= config.STALE_QUOTE_FLATTEN_SECONDS:
                log.warning("CMC down %.0fs with open positions — protective flatten", outage)
                _protective_flatten(executor, journal, portfolio, risk, "cmc_outage_exit")
        except UnreconciledOrderError as e:
            log.critical("%s", e)
            break
        except Exception as e:  # noqa: BLE001 — a crash mid-trade must stop, not loop
            log.exception("tick crashed")
            try:
                journal.event("tick_error", f"{type(e).__name__}: {e}", portfolio.equity())
            except Exception:
                pass
            if pending.read() is not None:
                log.critical("crashed with an order in flight — stopping for manual "
                             "reconcile (deploy/DEPLOY.md). NOT auto-retrying.")
                break
            # state is consistent (no order was in flight): keep the loop alive
        if args.once:
            break
        if risk.killed:
            log.warning("kill switch engaged — agent stopped")
            break
        time.sleep(settings.poll_interval)


if __name__ == "__main__":
    main()
