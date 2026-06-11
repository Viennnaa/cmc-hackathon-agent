"""EXECUTE layer, paper variant: fills at quoted price with fee + slippage.

Same interface the TWAK live executor will implement (Day 3-4), so the
runner doesn't change when we go live.
"""

import time
from dataclasses import dataclass

from agent import config
from agent.execution.portfolio import Portfolio, Position


@dataclass
class Fill:
    symbol: str
    side: str  # "buy" | "sell"
    qty: float
    price: float          # effective price incl. slippage
    fee_usdt: float
    pnl_usdt: float | None  # realized PnL on sells
    ts: float


class PaperExecutor:
    def buy(self, portfolio: Portfolio, symbol: str, size_usdt: float, quote_price: float) -> Fill:
        price = quote_price * (1 + config.PAPER_SLIPPAGE_PCT)
        fee = size_usdt * config.PAPER_FEE_PCT
        qty = (size_usdt - fee) / price
        portfolio.cash -= size_usdt
        portfolio.positions[symbol] = Position(symbol, qty, price, time.time())
        return Fill(symbol, "buy", qty, price, fee, None, time.time())

    def sell(self, portfolio: Portfolio, symbol: str, quote_price: float) -> Fill:
        pos = portfolio.positions.pop(symbol)
        price = quote_price * (1 - config.PAPER_SLIPPAGE_PCT)
        gross = pos.qty * price
        fee = gross * config.PAPER_FEE_PCT
        portfolio.cash += gross - fee
        cost = pos.qty * pos.entry_price
        pnl = (gross - fee) - cost
        return Fill(symbol, "sell", pos.qty, price, fee, pnl, time.time())
