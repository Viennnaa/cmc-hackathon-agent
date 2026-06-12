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
import json
import logging
import os
import socket
import time

from agent import alerts, config, narrator, review
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
QUOTE_GAPS_PATH = config.DATA_DIR / "quote_gaps.json"

# held symbol -> ts when its quotes first went missing (protective-exit timer).
# Persisted: a restart mid-outage must not reset the stop-loss-blind clock.
_quote_gap_since: dict[str, float] = {}


def _load_quote_gaps() -> dict[str, float]:
    """Fail open to {} on ANY malformed content: a crash here happens before
    the loop's try/except, and systemd would turn it into a 10s restart loop
    with no ticks, no stop-losses, and no alert. Worst case of {} is the
    stop-loss-blind clock restarting, which the gap guard re-arms next tick."""
    try:
        data = json.loads(QUOTE_GAPS_PATH.read_text())
        if not isinstance(data, dict):
            return {}
        return {k: float(v) for k, v in data.items()
                if isinstance(v, (int, float, str))}
    except (OSError, ValueError, TypeError):
        return {}


def _save_quote_gaps() -> None:
    QUOTE_GAPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUOTE_GAPS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(_quote_gap_since))
    os.replace(tmp, QUOTE_GAPS_PATH)


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
    gaps_before = dict(_quote_gap_since)
    stale_exits: list[str] = []
    for sym in list(_quote_gap_since):
        if sym not in portfolio.positions:
            _quote_gap_since.pop(sym)
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
            stale_exits.append(sym)
    if _quote_gap_since != gaps_before:
        _save_quote_gaps()
    if stale_exits:  # alert last: never delay the exits or the gap-state persist
        alerts.send(f"stale_data_exit: protective exit of {stale_exits} at last marks")

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
        # alert LAST: a hung network call before risk.save/the sells could
        # leave the kill switch unpersisted or delay exits while exposed
        alerts.send(f"RISK GATE {flatten.rule}: {flatten.detail}")
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
        verdict = risk.review(sig.action, sym, portfolio, fear_greed=fear_greed)
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
    held = list(portfolio.positions)
    journal.event(reason, f"protective flatten of {held} at last marks",
                  portfolio.equity())
    for sym in held:
        price = portfolio.last_prices.get(sym, portfolio.positions[sym].entry_price)
        _sell(executor, journal, portfolio, sym, price)
        risk.note_exit(sym)
    risk.save(RISK_STATE_PATH)
    portfolio.save(PORTFOLIO_PATH)
    alerts.send(f"{reason}: protective flatten of {held}")  # alert last: never delay the exits


def _refuse_live(msg: str) -> None:
    alerts.send(f"LIVE START REFUSED: {msg}")
    raise SystemExit(f"live reconcile failed — {msg}")


def _reconcile_live(client, portfolio: Portfolio, journal: Journal) -> None:
    """Live capital comes from the REAL wallet, never the local file.

    Sizing the judged 20%-per-position rule against stale local capital would
    silently violate it (e.g. $30 of a $100 wallet is 30%). Fail closed: live
    trading never starts on an unreadable balance, an unfundable exit (no
    gas), a wallet that contradicts recorded positions, or a state file that
    was live on another machine (double-trading guard).
    """
    from agent.execution.twak import WALLET_SYMBOLS, TwakError, find_balance, find_usdt_balance

    host = socket.gethostname()
    if portfolio.mode == "live" and portfolio.live_host and portfolio.live_host != host:
        _refuse_live(f"portfolio.json was live on '{portfolio.live_host}' but this is "
                     f"'{host}' — two machines trading one wallet would double-trade. "
                     "If the migration is intentional, edit live_host in portfolio.json.")

    try:
        data = client.portfolio()
    except TwakError as e:
        _refuse_live(f"cannot read wallet portfolio: {e}")
    usdt = find_usdt_balance(data)
    if usdt is None:
        _refuse_live("no USDT balance found in `twak wallet portfolio` "
                     f"output (verify shape on the dry run): {str(data)[:300]}")

    # Gas check: a stop-loss exit must always be fundable. BNB pays gas, so
    # an empty gas tank means halting while still holding a position.
    bnb = find_balance(data, WALLET_SYMBOLS["BNB"])
    if bnb is None or bnb < config.MIN_GAS_BNB_START:
        _refuse_live(f"gas check: BNB balance {bnb} below {config.MIN_GAS_BNB_START} "
                     "minimum — exits would become unfundable mid-window. Fund gas BNB "
                     "and restart.")

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
        portfolio.baseline_equity = usdt
        portfolio.live_host = host
        portfolio.mode = "live"
    elif not portfolio.positions:
        if not portfolio.baseline_equity:  # pre-upgrade live state: backfill once
            portfolio.baseline_equity = usdt
        if abs(usdt - portfolio.cash) > 0.5:
            log.warning("wallet USDT %.2f differs from local cash %.2f — adopting "
                        "wallet (peak/day baselines kept: fail closed)", usdt, portfolio.cash)
            journal.event("live_cash_reconcile",
                          f"cash {portfolio.cash:.2f} -> wallet {usdt:.2f}", usdt)
        portfolio.cash = usdt
    else:
        # Live restart while holding: the wallet must actually hold what
        # portfolio.json claims, or the eventual sell fails mid-window.
        for sym, pos in portfolio.positions.items():
            bal = find_balance(data, WALLET_SYMBOLS.get(sym, (sym,)))
            if bal is None:
                _refuse_live(f"wallet shows no {sym} balance but portfolio.json holds "
                             f"{pos.qty} — reconcile manually before restarting.")
            if abs(bal - pos.qty) > pos.qty * config.LIVE_QTY_MISMATCH_TOLERANCE:
                _refuse_live(f"wallet {sym} balance {bal} vs recorded qty {pos.qty} differs "
                             f"beyond {config.LIVE_QTY_MISMATCH_TOLERANCE:.0%} — reconcile "
                             "manually before restarting.")
        log.info("live restart holding %s — wallet balances match portfolio.json",
                 list(portfolio.positions))
    portfolio.save(PORTFOLIO_PATH)


def _check_gas(client, portfolio: Portfolio, journal: Journal) -> None:
    """Hourly low-gas warning while live; failures never touch trading."""
    from agent.execution.twak import WALLET_SYMBOLS, find_balance
    try:
        bnb = find_balance(client.portfolio(), WALLET_SYMBOLS["BNB"])
        if bnb is None or bnb < config.LOW_GAS_BNB_WARN:
            msg = (f"low gas: BNB balance {bnb} < {config.LOW_GAS_BNB_WARN} — exits may "
                   "soon be unfundable, top up gas")
            journal.event("low_gas_warning", msg, portfolio.equity())
            log.warning(msg)
            alerts.send(msg)
    except Exception as e:  # noqa: BLE001 — a failed gas probe must not stop trading
        log.warning("gas check failed: %s", e)


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
        alerts.send("start refused: pending_order.json exists — manual reconcile needed "
                    "(deploy/DEPLOY.md)")
        raise SystemExit(0)

    risk = RiskEngine.load(RISK_STATE_PATH)
    if risk.killed:
        log.critical("kill switch is engaged (risk_state.json) — agent stays stopped. "
                     "This is the judged permanent stop; do not clear it mid-window.")
        alerts.send("start refused: kill switch is engaged — agent stays stopped")
        raise SystemExit(0)

    _quote_gap_since.update(_load_quote_gaps())

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
    last_gas_check = time.time()
    while True:
        try:
            tick(cmc, store, portfolio, risk, executor, journal, strategy_state)
            cmc_down_since = None
            if settings.mode == "live" and \
                    time.time() - last_gas_check >= config.GAS_CHECK_INTERVAL_SECONDS:
                last_gas_check = time.time()
                _check_gas(client, portfolio, journal)
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
            alerts.send(f"agent halted: {e}")
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
                alerts.send(f"agent halted: crashed with an order in flight "
                            f"({type(e).__name__}: {e}) — manual reconcile needed")
                break
            # state is consistent (no order was in flight): keep the loop alive
        if args.once:
            break
        if risk.killed:
            log.warning("kill switch engaged — agent stopped")
            alerts.send("agent stopped: kill switch engaged (judged permanent stop)")
            break
        time.sleep(settings.poll_interval)


if __name__ == "__main__":
    main()
