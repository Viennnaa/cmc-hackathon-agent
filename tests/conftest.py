import pytest


@pytest.fixture(autouse=True)
def _no_real_alerts(monkeypatch):
    """Tests must never ping the operator: config.py loads .env at import, and
    alerts.send reads the Telegram env at call time — on a token-bearing box
    (the VPS) the refusal tests would otherwise send real false alarms."""
    monkeypatch.delenv("TELEGRAM_ALERT_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID", raising=False)
