# CMC Hackathon Agent

Autonomous BSC trading agent for the CMC × Trust Wallet × BNB Chain hackathon
(Track 1). Architecture, prize strategy, and timeline: see [PLAN.md](PLAN.md).

**Agent wallet (BSC):** `0x1e75d8e9039Cd9DE389CB696df52c46d44c85279` — registered
on the BNB Hack competition contract; ERC-8004 **agentId 1365** (bsc-testnet).

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

## Risk rules & guardrails (in `src/agent/config.py`)

The competition is ranked by **total return**, with two externally-binding
constraints: a ~30% max-drawdown disqualification gate and a ≥1 trade/UTC-day
qualification rule. The agent runs aggressively *within* a safety margin of the
DQ line — the gates below are self-imposed guardrails (they also earn the
"autonomous execution & guardrails" special-prize points), tuned for a PnL race
rather than capital preservation.

| Rule | Threshold | Consequence |
|---|---|---|
| Position sizing | max 15% of equity per name | entry rejected/resized |
| Concurrent positions | up to 6 names (~90% deployed, ~10% reserve) | further entries rejected |
| Stop-loss | −8% trigger per trade | forced exit, outranks strategy |
| Daily loss cap | −15% on the day | flatten all + 6h cool-off |
| Kill switch | −25% drawdown from peak | flatten all + permanent stop (margin under the ~30% DQ gate) |
| Daily-trade floor | no trade by 22:00 UTC | one minimal compliant swap (ETH, $2) to satisfy the qualification rule |
| Token risk | TWAK security check (fail closed) | entry vetoed |
| Re-entry cooldown | 1h after any exit per symbol | entry rejected (anti-churn) |
| Regime router | per-symbol 5-day SMA | downtrend → cash; uptrend → momentum entries; chop → mean-reversion |

Stop-loss note for the replay: −8% is the *trigger*; the realized loss on a
stopped trade runs slightly higher after slippage and swap fees. The journal
records both the trigger decision and the fill.

The Fear & Greed < 20 sentiment veto from the conservative build is **disabled
in the live engine** (a PnL race has to trade through fear) and retained only
for the `--fng-compare` backtest tool.

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
validation on the 15-token eligible universe (adaptive, 1h bars): 14d −7.9%
(DD 9.6%), 30d −15.8% (DD 16.2%), 60d −17.7% (DD 18.8%). Every window stays
well inside the ~30% drawdown DQ gate with no kill-switch or guardrail firing,
and 42–169 round trips means the ≥1-trade/day qualification rule is met on its
own. Long-only spot cannot print a profit in a market falling this hard — the
win condition is to track-and-cushion the fall, never blow up past the gate,
qualify on activity, and let the nightly self-review adapt the strategy. The
regime router's refusal of dead-cat-bounce entries is the edge over
momentum-alone, which whipsaws harder on the same tape.

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
- [x] Mainnet dry-run: real `twak` swap executed end-to-end (received-amount and on-chain receipt parsing verified)
- [x] x402 autonomous micropayments: opt-in, gasless USDC-on-Base (EIP-3009) CMC data calls (`src/agent/x402.py`)
- [x] On-chain competition registration (`twak compete register`)
- [ ] Top up the agent wallet to the $100 live target + gas BNB before the Jun 22 window
