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
  ~/projects/cmc-hackathon-agent/ root@<IP>:/home/agent/cmc-hackathon-agent/
```

(`data/` rides along — keeps the paper journal continuous. `.env` rides along
too; it is rewritten in step 4. Ownership is fixed by setup-vps.sh in step 3,
which creates the `agent` user and chowns the tree.)

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

# .env: same keys as the laptop copy; keep AGENT_MODE=paper until Jun 22.
```

## 5. Warm up + start

```bash
~/.local/bin/uv run python -m agent.backtest --days 3 --interval 1h --seed-store
exit   # back to root
systemctl start cmc-agent cmc-dashboard
journalctl -u cmc-agent -f          # expect: "starting in paper mode | …"
```

## 6. Watch the dashboard

```bash
ssh -L 8765:127.0.0.1:8765 agent@<IP>   # then open http://localhost:8765
```

## Updating code on a deployed box

```bash
rsync -av --exclude .venv --exclude __pycache__ --exclude .git \
  --exclude '/data/' --exclude .env --exclude .pytest_cache \
  ~/projects/cmc-hackathon-agent/ root@<IP>:/home/agent/cmc-hackathon-agent/
ssh root@<IP> 'chown -R agent:agent /home/agent/cmc-hackathon-agent \
  && sudo -u agent bash -c "cd ~/cmc-hackathon-agent \
       && ~/.local/bin/uv run python -m pytest -q" \
  && systemctl restart cmc-agent cmc-dashboard'
```

Gotchas learned the hard way (2026-06-12):
- `/data/` must be ANCHORED (leading slash). A bare `--exclude data` also
  matches `src/agent/data/` and silently ships a half-updated package.
- `restart`, never `start` — `start` on a running unit is a silent no-op
  and the old process keeps trading the old strategy.
- The live log is `journalctl -u cmc-agent`. `data/agent.log` on the VPS is
  a stale copy of the laptop's nohup log from the initial rsync (the systemd
  unit logs to journald only) — do not tail it to verify a deploy.

## Timeline

- **~Jun 18** — fund the NEW wallet address with $20 USDT (BSC), flip
  `AGENT_MODE=live`, `systemctl restart cmc-agent`, verify one real swap's
  JSON shape (TODO(verify) in twak.py), flip back to paper.
- **Jun 22 before window** — re-run the seed-store warm-up, set
  `AGENT_MODE=live`, restart, confirm dashboard shows LIVE.
  NOTE: Binance geo-blocks US VPS IPs (HTTP 451), so seed-store must run on
  the laptop, then: stop cmc-agent on the VPS → rsync `data/prices.sqlite`
  over → start cmc-agent. (Live CMC sampling on the VPS is unaffected.)
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
2. **`kill switch is engaged`** — the judged −10% drawdown stop fired.
   This is permanent for the window by design. Leave it stopped.
3. **`live reconcile failed`** — wallet balance unreadable at live startup
   (fail closed). Run `twak wallet portfolio` manually and fix auth/shape.

State files in `data/`: `portfolio.json` (positions/cash), `risk_state.json`
(kill switch, 24h halt, cooldowns — survives restarts), `strategy_state.json`
(active strategy + size factor from the nightly self-review; safe to delete,
regenerates with the adaptive default), `pending_order.json` (only present
while an order is unreconciled).

## Rollback

Stop the services, rsync `data/` back, resume on the laptop with
`nohup uv run python -m agent.runner >> data/agent.log 2>&1 &`. Never run
both machines in live mode at once — two loops would double-trade one wallet.
