# CMC × Trust Wallet × BNB Chain Hackathon — Track 1 Entry

**Hackathon:** https://coinmarketcap.com/api/hackathon/
**Register:** https://dorahacks.io/hackathon/bnbhack-twt-cmc/
**Builder Telegram:** https://t.me/+MhiOLT0YUnlmNWFk
**Submission lock:** June 21, 2026 · 12:00 UTC
**Live trading window:** June 22–28 (judged on real PnL replay)
**Judging:** returns, drawdown, risk-adjusted performance, **rule adherence**
**Live capital:** $100–200 in a fresh dedicated wallet

## Prize targets (stackable)

- Track 1 placement: $10k / $6k / $4k / $2k×2
- Best Use of CMC Data & Signal: $2k
- Best Use of Trust Wallet Agent Kit: $2k
- Best Use of BNB AI Agent SDK: $2k

Strategy: stack all three sponsors (judges score this highest), optimize for
**low drawdown + clean rule adherence**, not max returns. A disciplined bot with
modest gains beats a lucky degen bot on this rubric.

## Architecture (Python, matches bnbagent SDK)

```
┌─────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌─────────┐
│  SENSE  │──▶│  DECIDE  │──▶│   RISK   │──▶│ EXECUTE  │──▶│ RECORD  │
└─────────┘   └──────────┘   └──────────┘   └──────────┘   └─────────┘
 CMC Data API   Deterministic   Hard gates:    TWAK swap      Decision log
 + Data MCP:    strategy        - pos size     quote + local  + trade journal
 quotes, RSI/   rules (momentum - stop-loss    signing on     + PnL ledger
 MACD, funding, / mean-revert,  - daily loss   PancakeSwap    (for judges'
 sentiment      regime-aware)   - kill switch  (BSC mainnet)  replay)
```

### Sponsor coverage

| Layer | Sponsor capability | Our use |
|---|---|---|
| L1 Data | CMC Data API + Data MCP | quotes, technicals (RSI/MACD), funding rates, sentiment as strategy inputs |
| L2 Execution | TWAK | self-custody local signing, autonomous mode, swap quotes + execution, token-risk pre-check |
| L3 Chain | BNB AI Agent SDK | ERC-8004 on-chain agent identity (gas-free testnet); optional: expose strategy-as-a-service via ERC-8183/x402 |

### Strategy (v1, conservative)

- Universe: top-liquidity BSC pairs only (BNB, BTCB, ETH vs USDT on PancakeSwap)
- Signal: trend/momentum with RSI + MACD confirmation, funding-rate filter,
  sentiment as a veto (not a trigger)
- Risk rules (the "user-defined rules" judges score adherence to):
  - max 20% of capital per position
  - per-trade stop-loss −3%
  - daily loss cap −5% → flatten + halt for 24h
  - global kill switch at −10% drawdown → flatten + stop
  - min liquidity + TWAK token-risk check before any entry
- Every decision logged with inputs → rule fired → action, so the judged
  replay shows perfect rule adherence.

## Timeline (10 days to lock)

| Day | Date | Milestone |
|---|---|---|
| 0 | Jun 11 | Register DoraHacks, TW portal creds, CMC API key, join TG; scaffold repo |
| 1–2 | Jun 12–13 | CMC data layer + paper-trading loop (sense→decide→record, no execution) |
| 3–4 | Jun 14–15 | TWAK integration, first swaps on BSC **testnet** |
| 5–6 | Jun 16–17 | Strategy + risk engine hardened; backtest vs CMC historical data |
| 7 | Jun 18 | BNB SDK: ERC-8004 registration; x402 hookup |
| 8 | Jun 19 | Mainnet dry-run with ~$20; observability dashboard + decision log |
| 9–10 | Jun 20–21 | Polish, README, demo video, **submit before 12:00 UTC Jun 21** |
| — | Jun 22–28 | Live window with $100–200; monitor daily |

## Blockers only the user can clear (Day 0)

1. Register on DoraHacks: https://dorahacks.io/hackathon/bnbhack-twt-cmc/
2. Trust Wallet API credentials (TWAK_ACCESS_ID + TWAK_HMAC_SECRET): https://portal.trustwallet.com
3. CMC API key: https://coinmarketcap.com/api/ (free tier to start; hackathon may grant credits)
4. Join Builder Telegram: https://t.me/+MhiOLT0YUnlmNWFk
5. Later (Day 8): fund a fresh dedicated wallet with $100–200 + gas BNB
