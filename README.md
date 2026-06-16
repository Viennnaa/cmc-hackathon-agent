# CMC Hackathon Agent

Autonomous BSC trading agent for the CMC × Trust Wallet × BNB Chain hackathon
(Track 1). Architecture, prize strategy, and timeline: see [PLAN.md](PLAN.md).

```
SENSE (CMC Data API) → DECIDE (adaptive regime router) → RISK (hard gates)
                     → EXECUTE (paper now, TWAK later) → RECORD (JSONL journal)
                                    ↑
            ADAPT (nightly self-review picks the strategy, narrow-only)
```

## Quickstart

```bash
uv sync --extra dev
cp .env.example .env   # fill in CMC_API_KEY
uv run python -m agent.runner --once   # single tick
uv run python -m agent.runner          # continuous paper trading
uv run pytest                          # tests
```

## Judged risk rules (immutable, in `src/agent/config.py`)

| Rule | Threshold | Consequence |
|---|---|---|
| Position sizing | max 20% of equity | entry rejected/resized |
| Stop-loss | −3% trigger per trade | forced exit, outranks strategy |
| Daily loss cap | −5% on the day | flatten all + halt 24h |
| Kill switch | −10% drawdown from peak | flatten all + permanent stop |
| Token risk | TWAK security check (fail closed) | entry vetoed |
| Re-entry cooldown | 8h after any exit per symbol | entry rejected (anti-churn) |
| Sentiment veto | CMC Fear & Greed < 20 *or unavailable* | no new entries — enforced in the risk engine for every strategy |
| Regime filter | CMC 24h change < +1% | no new *momentum* entries (mean-reversion deliberately buys red days; its downside is bounded by the stop-loss and knife filter) |

Stop-loss note for the replay: −3% is the *trigger*; the realized loss on a
stopped trade is ~3.4–3.8% after slippage and swap fees. The journal records
both the trigger decision and the fill.

## Adaptation: regime router + nightly self-review

A fixed strategy can't survive a regime change — the 24h filter is blind to
anything longer, and bear-market bounces clear +1%/24h constantly. Two
adaptation layers fix this, both deterministic and fully journaled:

1. **Regime router** (`strategy/adaptive.py`, per bar): classifies each
   symbol against its 5-day SMA — uptrend → momentum entries, chop →
   mean-reversion dip buying, downtrend → cash (exits and stops still run).
   Long-only on spot means cash IS the bearish position.
2. **Nightly self-review** (`review.py`, per UTC day): replays the agent's
   own sampled prices (trailing 14d) through every strategy in the menu and
   adopts the best risk-adjusted performer (score = return − max drawdown).
   **Narrow-only:** the review may shrink entry sizes when even the best
   trailing strategy lost badly, but can never raise a limit — the judged
   risk rules stay immutable. Scorecard journaled as a `self_review` event.

## Backtest findings (CMC hourly OHLCV through the live components)

Active trading on 15m bars lost ~10% to fee churn (48 round trips/14d at
~0.7% round-trip cost), which set the 1h-bar config. June 2026 bear-tape
validation (30d @ 1h, equal-weight buy & hold −20.1%): momentum −7.1%
(DD 7.0%), mean_revert −9.9% (DD 10.0%, kill-switched), **adaptive −4.05%
(DD 3.85%, no kill switch)** — the router's refusal of dead-cat-bounce
entries is worth ~3 points of return and ~3 of drawdown vs momentum alone.

Data is CMC hourly OHLCV (the Professional tier enabled `ohlcv/historical`),
the same path the live agent samples and warm-starts from — so the backtest
validates on the data it actually trades, with no source drift.

```bash
uv run python -m agent.backtest --days 30 --strategy adaptive
uv run python -m agent.backtest --days 30 --fng-compare  # sentiment-rule variants
uv run python -m agent.backtest --days 3 --seed-store     # manual pre-warm (runner also warm-starts on its own)
```

Every tick writes a decision record to `data/journal.jsonl`
(inputs → signal → risk verdict → action) and every fill to
`data/ledger.jsonl` — the artifacts judges replay for rule adherence.

## Adversarial reviews & hardening

The agent went through two independent adversarial reviews before the live
window: an OpenAI Codex pass focused on crash safety, and a full independent
Claude review (secrets across git history, real-money loss paths, LLM
containment) on 2026-06-12. Everything found was fixed and pinned by tests:
crash-safe order intents that survive restarts, the sentiment veto and quote
plausibility quarantine enforced in the risk engine / data layer rather than
by convention, wallet-password redaction on every error path, gas-balance
gates so exits stay fundable, wallet-vs-state verification on live restarts,
and Telegram alerts on every deliberate halt. The review reports themselves
stay out of the repo (they narrate deployment internals); the guard tests in
`tests/test_safety_guards.py` are the durable artifact.

## Status

- [x] Paper-trading loop: CMC quotes + fear&greed → adaptive regime router → risk gates → simulated fills
- [x] Adaptation layers: per-bar regime router + nightly narrow-only self-review (`data/strategy_state.json`)
- [x] TWAK execution layer: CLI wrapper, swap quotes verified live, token-risk gate, agent wallet created
- [x] Backtest harness (CMC hourly OHLCV through the live strategy/risk/execution components)
- [x] Startup warm-start: backfills ~155 hourly bars from CMC so regime/MACD indicators are valid from tick 1 (restart-proof through the live window)
- [x] BNB AI Agent SDK: ERC-8004 identity — **agentId 1365** on bsc-testnet
      ([tx](https://testnet.bscscan.com/tx/0x401e212d1e58ca4e2f5623cf9494071788f65833fcf9103c3aa65e8002eb2313));
      note: MegaFuel paymaster dropped sponsored txs, registered via `--no-paymaster` + faucet gas
- [x] Observability dashboard: `uv run python -m agent.dashboard` → http://localhost:8765
- [ ] Mainnet dry-run with ~$20 (Day 8)
- [ ] Fund the agent wallet with $100–200 + gas BNB — use the address from
      `twak wallet address` on the VPS (a fresh wallet is created during deploy;
      see deploy/DEPLOY.md — the original 0x2c90… wallet is retired, do not fund it)
