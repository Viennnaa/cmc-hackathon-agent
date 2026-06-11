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

Every tick writes a decision record to `data/journal.jsonl`
(inputs → signal → risk verdict → action) and every fill to
`data/ledger.jsonl` — the artifacts judges replay for rule adherence.

## Status

- [x] Paper-trading loop: CMC quotes + fear&greed → RSI/MACD momentum → risk gates → simulated fills
- [ ] TWAK execution on BSC testnet (Day 3–4)
- [ ] Backtest harness vs CMC historical data (Day 5–6)
- [ ] BNB AI Agent SDK: ERC-8004 identity (Day 7)
- [ ] Mainnet dry-run + dashboard (Day 8)
