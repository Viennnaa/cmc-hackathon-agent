# CMC Hackathon Agent

Autonomous BSC trading agent for the CMC × Trust Wallet × BNB Chain hackathon
(Track 1). Architecture, prize strategy, and timeline: see [PLAN.md](PLAN.md).

```
SENSE (CMC Data API) → DECIDE (momentum strategy) → RISK (hard gates)
                     → EXECUTE (paper now, TWAK later) → RECORD (JSONL journal)
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
| Stop-loss | −3% per trade | forced exit, outranks strategy |
| Daily loss cap | −5% on the day | flatten all + halt 24h |
| Kill switch | −10% drawdown from peak | flatten all + permanent stop |
| Token risk | TWAK security check (fail closed) | entry vetoed |
| Re-entry cooldown | 8h after any exit per symbol | entry rejected (anti-churn) |
| Sentiment veto | CMC Fear & Greed < 20 | no new entries |
| Regime filter | CMC 24h change < +1% | no new entries (long-only) |

## Backtest findings (Binance klines through the live components)

Active trading on 15m bars lost ~10% to fee churn (48 round trips/14d at
~0.7% round-trip cost). The locked config — 1h bars, crossover-confirmed
entries, regime filter, cooldown — trades 5×/14d with max drawdown 1.36%
and no risk-rule firings. These runs exclude the Fear & Greed veto (no
historical data on the free tier); in the current extreme-fear regime the
live agent would sit fully flat, i.e. strictly safer than the backtest.

```bash
uv run python -m agent.backtest --days 14 --interval 1h
uv run python -m agent.backtest --days 60 --interval 4h --strategy mean_revert
uv run python -m agent.backtest --days 3 --interval 1h --seed-store  # pre-warm live indicators
```

Every tick writes a decision record to `data/journal.jsonl`
(inputs → signal → risk verdict → action) and every fill to
`data/ledger.jsonl` — the artifacts judges replay for rule adherence.

## Status

- [x] Paper-trading loop: CMC quotes + fear&greed → RSI/MACD momentum → risk gates → simulated fills
- [x] TWAK execution layer: CLI wrapper, swap quotes verified live, token-risk gate, agent wallet created
- [x] Backtest harness (Binance klines through the live strategy/risk/execution components)
- [x] BNB AI Agent SDK: ERC-8004 identity — **agentId 1365** on bsc-testnet
      ([tx](https://testnet.bscscan.com/tx/0x401e212d1e58ca4e2f5623cf9494071788f65833fcf9103c3aa65e8002eb2313));
      note: MegaFuel paymaster dropped sponsored txs, registered via `--no-paymaster` + faucet gas
- [x] Observability dashboard: `uv run python -m agent.dashboard` → http://localhost:8765
- [ ] Mainnet dry-run with ~$20 (Day 8)
- [ ] Fund the agent wallet with $100–200 + gas BNB — use the address from
      `twak wallet address` on the VPS (a fresh wallet is created during deploy;
      see deploy/DEPLOY.md — the original 0x2c90… wallet is retired, do not fund it)
