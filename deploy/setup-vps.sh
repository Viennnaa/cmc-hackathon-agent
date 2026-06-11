#!/usr/bin/env bash
# One-time VPS provisioning for the live trading window (Ubuntu 24.04, run as root).
# Idempotent: safe to re-run. Code/env/wallet setup happens after, as the
# `agent` user — see deploy/DEPLOY.md.
set -euo pipefail

echo "== packages =="
apt-get update -qq
apt-get install -y -qq git curl ufw ca-certificates

echo "== node 20 (twak CLI needs modern node) =="
if ! command -v node >/dev/null || [ "$(node -e 'console.log(process.versions.node.split(".")[0])')" -lt 20 ]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y -qq nodejs
fi
npm install -g @trustwallet/cli

echo "== agent user =="
id agent >/dev/null 2>&1 || adduser --disabled-password --gecos "" agent
# the runbook rsyncs the repo BEFORE this user exists; fix ownership now
[ -d /home/agent/cmc-hackathon-agent ] && chown -R agent:agent /home/agent/cmc-hackathon-agent

echo "== uv for agent user =="
sudo -u agent bash -c 'command -v ~/.local/bin/uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh'

echo "== 1G swap (small boxes OOM during uv sync otherwise) =="
if ! swapon --show | grep -q .; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== firewall: ssh only (dashboard stays on loopback) =="
ufw allow OpenSSH
ufw --force enable

echo "== systemd units =="
cp /home/agent/cmc-hackathon-agent/deploy/cmc-agent.service /etc/systemd/system/
cp /home/agent/cmc-hackathon-agent/deploy/cmc-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable cmc-agent cmc-dashboard

echo "== done =="
echo "Next (as agent): uv sync, twak auth, twak wallet create, .env, seed store,"
echo "then: systemctl start cmc-agent cmc-dashboard   (see deploy/DEPLOY.md)"
