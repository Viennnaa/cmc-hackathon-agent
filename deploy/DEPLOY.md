# VPS deployment — live window Jun 22–28

Moves the agent off the laptop so sleep/reboot/Wi-Fi can't kill the loop while
a position is open (stop-loss and kill switch only run while the loop runs).

Target: any small Ubuntu 24.04 box (≥1 GB RAM). Dashboard stays loopback-only;
the only open port is SSH.

## 1. Provision

Create the VPS (Ubuntu 24.04, ≥1 GB), add your SSH key, note the IP.

## 2. Ship the code (from the laptop)

```bash
rsync -av --exclude .venv --exclude __pycache__ --exclude .git \
  --exclude .gstack --exclude 'data/agent.log' \
  ~/projects/cmc-hackathon-agent/ root@<IP>:/home/agent/cmc-hackathon-agent/
```

(`data/` rides along — keeps the paper journal continuous. `.env` rides along
too; it is rewritten in step 4. `.gstack/` must NOT ship — it holds a local
tool token. Ownership is fixed by setup-vps.sh in step 3, which creates the
`agent` user and chowns the tree.)

## 3. Provision the box (as root)

```bash
ssh root@<IP> 'bash /home/agent/cmc-hackathon-agent/deploy/setup-vps.sh'
```

Installs node 20 + twak CLI, uv, the `agent` user, swap, ufw (SSH only), and
the two systemd units (enabled, not yet started).

## 4. Secrets + wallet (as agent)

```bash
ssh root@<IP>
su - agent && cd cmc-hackathon-agent
~/.local/bin/uv sync

# twak API credentials: copy ~/.twak/credentials.json from the laptop,
# or re-auth with TWAK_ACCESS_ID / TWAK_HMAC_SECRET.

# FRESH trading wallet (the old one's password leaked into shell history —
# this retires it; the old wallet was never funded):
twak wallet create        # store the new password in a password manager
twak wallet address       # -> new BSC funding address, replaces 0x2c90…736F

# .env on the VPS needs (the laptop copy is NOT sufficient):
#   CMC_API_KEY=...               # as on the laptop
#   TWAK_ACCESS_ID=...            # as on the laptop
#   TWAK_HMAC_SECRET=...          # as on the laptop
#   TWAK_WALLET_PASSWORD=...      # VPS-ONLY: headless signing has no keychain;
#                                 # without it every live swap fails
#   AGENT_MODE=paper              # keep paper until Jun 22
#   TELEGRAM_ALERT_BOT_TOKEN=...  # optional but recommended: halt/kill-switch
#   TELEGRAM_ALERT_CHAT_ID=...    # alerts for the unattended window
#   ANTHROPIC_API_KEY=...         # optional: dashboard narrator
```

## 5. Start

The agent warm-starts itself: on boot it backfills ~155 hourly bars per symbol
from CMC (`runner._warm_start`), so indicators are valid from the first tick —
no manual seed step, and it works on the VPS (CMC is not geo-blocked). Watch
for the `warm-start: backfilled N hourly bars …` log line right after start.

```bash
exit   # back to root
systemctl start cmc-agent cmc-dashboard
journalctl -u cmc-agent -f          # expect: "starting …" then "warm-start: backfilled …"
```

## 6. Watch the dashboard

```bash
ssh -L 8765:127.0.0.1:8765 agent@<IP>   # then open http://localhost:8765
```

## Updating code on a deployed box

```bash
rsync -av --delete --exclude .venv --exclude __pycache__ --exclude .git \
  --exclude '/data/' --exclude .env --exclude .pytest_cache --exclude .gstack \
  ~/projects/cmc-hackathon-agent/ root@<IP>:/home/agent/cmc-hackathon-agent/
ssh root@<IP> 'chown -R agent:agent /home/agent/cmc-hackathon-agent \
  && sudo -u agent bash -c "cd ~/cmc-hackathon-agent \
       && ~/.local/bin/uv run python -m pytest -q" \
  && systemctl restart cmc-agent cmc-dashboard'
```

(`--delete` keeps the VPS an exact mirror — without it a module deleted
locally lingers importable on the box. Excluded paths — `/data/`, `.env`,
`.venv` — are protected from deletion by rsync's default exclude semantics.)

Gotchas learned the hard way (2026-06-12):
- `/data/` must be ANCHORED (leading slash). A bare `--exclude data` also
  matches `src/agent/data/` and silently ships a half-updated package.
- `restart`, never `start` — `start` on a running unit is a silent no-op
  and the old process keeps trading the old strategy.
- The live log is `journalctl -u cmc-agent`. `data/agent.log` on the VPS is
  a stale copy of the laptop's nohup log from the initial rsync (the systemd
  unit logs to journald only) — do not tail it to verify a deploy.

## Timeline

- **~Jun 18 — dry run.** Fund the NEW wallet address with **$20 USDT + ≥0.01
  BNB gas** (BSC — the agent refuses a live start below 0.005 BNB: a
  stop-loss exit must always be fundable). Flip `AGENT_MODE=live`,
  `systemctl restart cmc-agent`, execute one real swap, then flip back to
  paper. The dry run MUST answer these before the live window (do not skip —
  each one can corrupt live accounting if assumed wrong):
  1. **Receipt or broadcast?** Does `twak swap` block until the tx receipt,
     or return at broadcast? If broadcast, a reverted tx books phantom USDT
     into portfolio.cash and the next buy double-spends.
  2. **`output` semantics.** Is the executed swap's `output` the actual
     post-execution amount or just the quote? (TODO(verify) in twak.py —
     fallback is `minReceived`.)
  3. **Exit code on revert.** Run/observe a failing swap if possible; a
     reverted tx must surface as non-zero exit (-> TwakError), not success.
  4. **Password via env?** Check whether the twak CLI reads
     `TWAK_WALLET_PASSWORD` from the environment itself; if yes, drop the
     `--password` flag in twak.py (currently passed but redacted from all
     error paths).
  5. **Alert path.** Confirm a Telegram alert arrives (e.g. stop the agent
     with a hand-made `data/pending_order.json` and restart).
- **Jun 22 before window** — set `AGENT_MODE=live`, restart, confirm the log
  shows `warm-start: backfilled …` and the dashboard shows LIVE.
  The laptop stays `AGENT_MODE=paper` for the whole window, and
  `~/.twak/wallet.json` lives on exactly one box (the VPS) — the `live_host`
  guard only catches a *synced* state file; a second box with independent
  state would double-trade the wallet.
  (No manual price-store seeding any more: the runner warm-starts from CMC on
  the VPS itself. The old Binance seed-store + rsync dance is retired — Binance
  geo-blocked US VPS IPs with HTTP 451; CMC does not.)
- **Jun 28** — after window close: flatten if holding, stop services, pull
  `data/` back to the laptop for the submission artifacts.

## If the agent stops itself

The agent exits cleanly (and systemd leaves it stopped — `Restart=on-failure`)
in exactly three cases. All three are deliberate; do not blindly restart.

1. **`pending_order.json exists`** — an order was in flight when the process
   died; it may or may not have executed on-chain. Compare
   `twak wallet portfolio` against `data/portfolio.json`:
   - trade executed → edit portfolio.json to record it (cash, position),
   - trade absent → no edit needed.
   Then `rm data/pending_order.json` and `systemctl start cmc-agent`.
2. **`kill switch is engaged`** — the judged −25% drawdown stop fired.
   This is permanent for the window by design. Leave it stopped.
3. **`live reconcile failed`** — fail-closed live startup. One of: wallet
   balance unreadable (fix auth/shape via `twak wallet portfolio`), BNB gas
   below the start minimum (fund gas), wallet holdings contradicting
   portfolio.json after a restart (reconcile manually), or portfolio.json
   marked live on a different host (`live_host` double-trade guard — edit the
   field only if the migration is intentional).

All deliberate halts send a Telegram alert when `TELEGRAM_ALERT_BOT_TOKEN` /
`TELEGRAM_ALERT_CHAT_ID` are set — configure them before the unattended
window so a mid-window stop is noticed in minutes, not days.

State files in `data/`: `portfolio.json` (positions/cash), `risk_state.json`
(kill switch, 24h halt, cooldowns — survives restarts), `strategy_state.json`
(active strategy + size factor from the nightly self-review; safe to delete,
regenerates with the adaptive default), `pending_order.json` (only present
while an order is unreconciled).

## Rollback

Stop the services, rsync `data/` back, resume on the laptop with
`nohup uv run python -m agent.runner >> data/agent.log 2>&1 &`. Never run
both machines in live mode at once — two loops would double-trade one wallet.
(Code-enforced since the live_host guard: a live start on a host other than
the one recorded in portfolio.json refuses; edit `live_host` in
portfolio.json as part of an intentional migration.)
