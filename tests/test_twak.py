import pytest

from agent.execution.portfolio import Portfolio
from agent.execution.twak import TwakError, TwakExecutor


class FakeClient:
    def __init__(self, risk_level="low", swap_out=0.05):
        self.risk_level = risk_level
        self.swap_out = swap_out
        self.swaps = []

    def risk(self, asset_id):
        if self.risk_level == "error":
            raise TwakError("boom")
        if self.risk_level is None:
            return {"assetId": asset_id}  # no securityInfo at all
        return {"assetId": asset_id, "securityInfo": {"riskLevel": self.risk_level}}

    def swap(self, amount, from_token, to_token, slippage_pct=0.5):
        self.swaps.append((amount, from_token, to_token))
        # real shape verified 2026-06-11: amounts are "0.0496 BNB" strings
        return {
            "input": f"{amount} {from_token}",
            "output": f"{self.swap_out} {to_token}",
            "minReceived": f"{self.swap_out * 0.99} {to_token}",
            "provider": "LiquidMesh",
            "priceImpact": "0",
        }


def test_pre_entry_passes_on_low_risk():
    ex = TwakExecutor(FakeClient(risk_level="low"))
    assert ex.pre_entry_check("BNB") is None


def test_pre_entry_vetoes_high_risk():
    ex = TwakExecutor(FakeClient(risk_level="high"))
    assert "flagged" in ex.pre_entry_check("BNB")


def test_pre_entry_passes_on_medium_risk():
    # BTCB is Blockaid-audited but rated medium (mint function); tradeable
    ex = TwakExecutor(FakeClient(risk_level="medium"))
    assert ex.pre_entry_check("BTC") is None


def test_pre_entry_fails_closed_on_error():
    ex = TwakExecutor(FakeClient(risk_level="error"))
    assert "failing closed" in ex.pre_entry_check("BNB")


def test_pre_entry_fails_closed_on_missing_risk_level():
    ex = TwakExecutor(FakeClient(risk_level=None))
    assert "failing closed" in ex.pre_entry_check("BNB")


def test_buy_routes_usdt_to_token_and_books_position():
    client = FakeClient(swap_out=0.05)
    ex = TwakExecutor(client)
    p = Portfolio(cash=150.0)
    fill = ex.buy(p, "BTC", 30.0, 100000.0)
    assert client.swaps == [(30.0, "USDT", "BTCB")]
    assert p.cash == 120.0
    assert p.positions["BTC"].qty == 0.05
    assert abs(fill.price - 600.0) < 1e-9  # 30 USDT / 0.05 BTCB


def test_sell_books_pnl():
    client = FakeClient(swap_out=0.05)
    ex = TwakExecutor(client)
    p = Portfolio(cash=150.0)
    ex.buy(p, "BTC", 30.0, 100000.0)
    client.swap_out = 33.0  # sell proceeds in USDT
    fill = ex.sell(p, "BTC", 105000.0)
    assert p.cash == 120.0 + 33.0
    assert abs(fill.pnl_usdt - 3.0) < 1e-9
    assert "BTC" not in p.positions


def test_failed_sell_restores_position():
    client = FakeClient(swap_out=0.05)
    ex = TwakExecutor(client)
    p = Portfolio(cash=150.0)
    ex.buy(p, "BTC", 30.0, 100000.0)
    client.swap_out = 0  # broken swap response
    with pytest.raises(TwakError):
        ex.sell(p, "BTC", 105000.0)
    assert "BTC" in p.positions  # position restored, not silently lost
