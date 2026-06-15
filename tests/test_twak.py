import pytest

from agent.execution.portfolio import Portfolio
from agent.execution.twak import SWAP_IDS, TwakError, TwakExecutor


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
    # medium-risk tokens (e.g. a mint function) stay tradeable; only high is vetoed
    ex = TwakExecutor(FakeClient(risk_level="medium"))
    assert ex.pre_entry_check("ETH") is None


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
    fill = ex.buy(p, "ETH", 30.0, 3000.0)
    # BEP-20 legs route by asset id, never bare symbol (symbol resolution is
    # unreliable: "SOL" once quoted BNB)
    assert client.swaps == [(30.0, "USDT", SWAP_IDS["ETH"])]
    assert SWAP_IDS["ETH"].startswith("c20000714_t0x")
    assert p.cash == 120.0
    assert p.positions["ETH"].qty == 0.05
    assert abs(fill.price - 600.0) < 1e-9  # 30 USDT / 0.05


def test_sell_books_pnl():
    client = FakeClient(swap_out=0.05)
    ex = TwakExecutor(client)
    p = Portfolio(cash=150.0)
    ex.buy(p, "ETH", 30.0, 3000.0)
    client.swap_out = 33.0  # sell proceeds in USDT
    fill = ex.sell(p, "ETH", 3150.0)
    assert p.cash == 120.0 + 33.0
    assert abs(fill.pnl_usdt - 3.0) < 1e-9
    assert "ETH" not in p.positions


def test_failed_sell_restores_position():
    client = FakeClient(swap_out=0.05)
    ex = TwakExecutor(client)
    p = Portfolio(cash=150.0)
    ex.buy(p, "ETH", 30.0, 3000.0)
    client.swap_out = 0  # broken swap response
    with pytest.raises(TwakError):
        ex.sell(p, "ETH", 3150.0)
    assert "ETH" in p.positions  # position restored, not silently lost
