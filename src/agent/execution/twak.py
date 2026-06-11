"""EXECUTE layer, live variant: Trust Wallet Agent Kit (TWAK) CLI wrapper.

TWAK handles local signing (keys in ~/.twak/wallet.json, AES-256-GCM) and
swap routing on BSC. We shell out to the CLI with --json rather than
reimplementing HMAC request signing — same interface as PaperExecutor so
the runner is mode-agnostic.

Docs: https://developer.trustwallet.com/developer/agent-sdk/cli-reference

Quotes and risk checks need no wallet password; swap execution resolves the
password from the OS keychain or TWAK_WALLET_PASSWORD (never passed by us
on the command line).
"""

import json
import subprocess
import time

from agent import config
from agent.execution.paper import Fill
from agent.execution.portfolio import Portfolio, Position

CHAIN = "bsc"
TWAK_CMD = ["twak"]  # assumes global install: npm install -g @trustwallet/cli

# CMC symbol -> token symbol twak understands on BSC.
# BTC trades as BTCB (Binance-pegged) on PancakeSwap.
TOKEN_MAP = {"BNB": "BNB", "BTC": "BTCB", "ETH": "ETH", "USDT": "USDT"}

# Trust Wallet asset IDs on BSC (c714 = SLIP44 714 = BNB Smart Chain).
# TODO(verify): confirm exact IDs via `twak search` once the CLI is installed.
ASSET_IDS = {
    "BNB": "c714",
    "BTC": "c714_t0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c",  # BTCB
    "ETH": "c714_t0x2170Ed0880ac9A755fd29B2688956BD959F933F8",  # Binance-pegged ETH
}


class TwakError(RuntimeError):
    pass


def _run(args: list[str], timeout: int = 120) -> dict:
    cmd = TWAK_CMD + args + ["--json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise TwakError(f"twak {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise TwakError(f"twak {' '.join(args)} returned non-JSON: {proc.stdout[:200]}") from e


def _amount(value) -> float:
    """Parse twak amount fields: 12.5, "12.5", or "12.5 BNB".

    Verified 2026-06-11 against a real --quote-only response:
    {"input": "30 USDT", "output": "0.0496... BNB", "minReceived": "...",
     "provider": "LiquidMesh", "priceImpact": "0"}
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).split()[0])


class TwakClient:
    """Thin typed surface over the twak CLI."""

    def auth_status(self) -> dict:
        return _run(["auth", "status"])

    def wallet_status(self) -> dict:
        return _run(["wallet", "status"])

    def wallet_address(self, chain: str = CHAIN) -> dict:
        return _run(["wallet", "address", "--chain", chain])

    def portfolio(self) -> dict:
        return _run(["wallet", "portfolio", "--chains", CHAIN])

    def risk(self, asset_id: str) -> dict:
        """Token security / rug-risk check — judged pre-entry gate."""
        return _run(["risk", asset_id])

    def quote(self, amount: float, from_token: str, to_token: str) -> dict:
        return _run([
            "swap", str(amount), from_token, to_token,
            "--chain", CHAIN, "--quote-only",
        ])

    def swap(self, amount: float, from_token: str, to_token: str,
             slippage_pct: float = 0.5) -> dict:
        return _run([
            "swap", str(amount), from_token, to_token,
            "--chain", CHAIN, "--slippage", str(slippage_pct),
        ])


class TwakExecutor:
    """Live executor: same buy/sell interface as PaperExecutor.

    Sizes are in USDT; buys swap USDT -> token, sells swap token -> USDT.
    Portfolio bookkeeping mirrors the paper path so risk gates and the
    journal see identical state shapes in both modes.
    """

    def __init__(self, client: TwakClient | None = None):
        self.client = client or TwakClient()

    def pre_entry_check(self, symbol: str) -> str | None:
        """TWAK token-risk gate: veto reason or None if safe to enter.

        Asset IDs use Trust Wallet format: c714 = BNB native; BEP-20 tokens
        are c714_t<contract>. A lookup failure vetoes the entry (fail closed).
        """
        asset_id = ASSET_IDS.get(symbol)
        if asset_id is None:
            return f"no asset id mapped for {symbol}"
        try:
            result = self.client.risk(asset_id)
        except TwakError as e:
            return f"risk check unavailable, failing closed: {e}"
        verdict = str(result.get("riskLevel") or result.get("risk") or "").lower()
        if verdict in ("high", "critical", "danger"):
            return f"twak risk check flagged {symbol}: {verdict}"
        return None

    def buy(self, portfolio: Portfolio, symbol: str, size_usdt: float, quote_price: float) -> Fill:
        token = TOKEN_MAP[symbol]
        result = self.client.swap(size_usdt, TOKEN_MAP["USDT"], token)
        # TODO(verify): executed-swap response shape on the Day 8 dry run;
        # quote-only confirmed to use "output". Fall back to minReceived.
        qty = _amount(result.get("output") or result.get("minReceived"))
        if qty <= 0:
            raise TwakError(f"swap returned no output amount: {result}")
        price = size_usdt / qty
        portfolio.cash -= size_usdt
        portfolio.positions[symbol] = Position(symbol, qty, price, time.time())
        return Fill(symbol, "buy", qty, price, 0.0, None, time.time())

    def sell(self, portfolio: Portfolio, symbol: str, quote_price: float) -> Fill:
        token = TOKEN_MAP[symbol]
        pos = portfolio.positions.pop(symbol)
        result = self.client.swap(pos.qty, token, TOKEN_MAP["USDT"])
        proceeds = _amount(result.get("output") or result.get("minReceived"))
        if proceeds <= 0:
            portfolio.positions[symbol] = pos  # restore; swap did not fill
            raise TwakError(f"swap returned no output amount: {result}")
        portfolio.cash += proceeds
        price = proceeds / pos.qty
        pnl = proceeds - pos.qty * pos.entry_price
        return Fill(symbol, "sell", pos.qty, price, 0.0, pnl, time.time())
