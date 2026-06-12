"""Operator alerts: best-effort Telegram ping on halts and risk-gate firings.

The agent's deliberate stops exit code 0 (so systemd does not restart-loop a
refusal) — which also means nothing external notices a halt mid-window. This
closes that gap with a fire-and-forget message: no Telegram config -> silent
no-op; any failure is swallowed. Alerting must never affect trading or block
a halt that is already in progress.

Config (.env): TELEGRAM_ALERT_BOT_TOKEN, TELEGRAM_ALERT_CHAT_ID.
"""

import json
import logging
import os
import urllib.request

log = logging.getLogger("agent.alerts")


def send(text: str) -> None:
    token = os.getenv("TELEGRAM_ALERT_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_ALERT_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat_id,
                             "text": f"[cmc-agent] {text}"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # noqa: BLE001 — alerting must never raise
        # a malformed token raises InvalidURL whose message embeds the full
        # URL (token included) — and laptop logs ship in data/ artifacts
        log.warning("telegram alert failed: %s", str(e).replace(token, "***"))
