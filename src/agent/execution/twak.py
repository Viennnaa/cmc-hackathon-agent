"""EXECUTE layer, live variant: Trust Wallet Agent Kit (TWAK) CLI wrapper.

TWAK handles local signing (keys in ~/.twak/wallet.json, AES-256-GCM) and
swap routing on BSC. We shell out to the CLI with --json rather than
reimplementing HMAC request signing — same interface as PaperExecutor so
the runner is mode-agnostic.

Docs: https://developer.trustwallet.com/developer/agent-sdk/cli-reference

Quotes and risk checks need no wallet password; swap execution resolves the
password from the OS keychain or TWAK_WALLET_PASSWORD. Headless Linux needs
the --password flag (env passthrough unverified — dry-run checklist item),
so every error path MUST go through _redact/_sanitize: a raw argv in an
exception would propagate to the journal, the dashboard, and the narrator's
Anthropic calls, and data/ ships in the submission artifacts.
"""

import json
import os
import subprocess
import time

from agent import config
from agent.execution.paper import Fill
from agent.execution.portfolio import Portfolio, Position

CHAIN = "bsc"
TWAK_CMD = ["twak"]  # assumes global install: npm install -g @trustwallet/cli

# Trust Wallet asset IDs (risk gate), verified live 2026-06-11: native BNB is
# c714, but BEP-20 tokens use the legacy smartchain coin id c20000714_t<address>
# (c714_t... returns TOKEN_NOT_FOUND). Addresses must keep EIP-55 checksum case.
ASSET_IDS = {
    "BNB": "c714",
    "BTC": "c20000714_t0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c",   # BTCB
    "ETH": "c20000714_t0x2170Ed0880ac9A755fd29B2688956BD959F933F8",   # BNB pegged ETH
    "SOL": "c20000714_t0x570A5D26f7765Ecb712C0924E4De545B89fD43dF",   # BNB pegged SOL
    "XRP": "c20000714_t0x1D2F0da169ceB9fC7B3144628dB156f3F6c60dBE",   # BNB pegged XRP
    "CAKE": "c20000714_t0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82",  # PancakeSwap (native BSC)
}

# CMC symbol -> identifier passed to `twak swap`, verified live 2026-06-11:
# bare symbols are only reliable for USDT and native BNB. BEP-20 tokens MUST
# use asset ids — symbol resolution is dangerous ("SOL" silently quoted BNB,
# "CAKE" was unknown). Quotes for every entry below were verified by id.
SWAP_IDS = {
    "USDT": "USDT",
    "BNB": "BNB",
    "BTC": ASSET_IDS["BTC"],
    "ETH": ASSET_IDS["ETH"],
    "SOL": ASSET_IDS["SOL"],
    "XRP": ASSET_IDS["XRP"],
    "CAKE": ASSET_IDS["CAKE"],
}


class TwakError(RuntimeError):
    pass


def _redact(args: list[str]) -> list[str]:
    """argv copy safe for logs/exceptions: the value after --password is masked."""
    out = list(args)
    for i, a in enumerate(out):
        if a == "--password" and i + 1 < len(out):
            out[i + 1] = "***"
    return out


def _sanitize(text: str) -> str:
    """Mask the wallet password anywhere in CLI output (e.g. an argv echo)."""
    password = os.getenv("TWAK_WALLET_PASSWORD", "")
    return text.replace(password, "***") if password else text


def _run(args: list[str], timeout: int = 120) -> dict:
    cmd = TWAK_CMD + args + ["--json"]
    shown = " ".join(_redact(args))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # TimeoutExpired.cmd holds the unredacted argv — raising outside the
        # except block leaves no __context__/__cause__ chain to leak it
        # through tracebacks/journal via log.exception
        proc = None
    if proc is None:
        raise TwakError(f"twak {shown} timed out after {timeout}s")
    if proc.returncode != 0:
        detail = _sanitize(proc.stderr.strip() or proc.stdout.strip())
        raise TwakError(f"twak {shown} failed: {detail}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise TwakError(f"twak {shown} returned non-JSON: {_sanitize(proc.stdout)[:200]}") from e


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


# wallet-side symbols per universe symbol: pegged tokens may report under
# their BEP-20 name (BTC trades and shows up as BTCB)
WALLET_SYMBOLS = {
    "USDT": ("USDT",),
    "BNB": ("BNB",),
    "BTC": ("BTC", "BTCB"),
    "ETH": ("ETH",),
    "SOL": ("SOL",),
    "XRP": ("XRP",),
    "CAKE": ("CAKE",),
}


def find_balance(data, symbols: tuple[str, ...]) -> float | None:
    """Best-effort balance for any of `symbols` from `twak wallet portfolio` output.

    The exact shape is verified on the dry run; this walks the structure for
    any node claiming a matching symbol with a balance-like field. Callers
    fail closed (refuse to trade live) when this returns None.
    """
    wanted = {s.upper() for s in symbols}
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            sym = str(node.get("symbol") or node.get("token") or node.get("asset") or "").upper()
            if sym in wanted:
                for key in ("balance", "amount", "quantity", "value"):
                    if node.get(key) is not None:
                        try:
                            return _amount(node[key])
                        except (ValueError, IndexError):
                            continue
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def find_usdt_balance(data) -> float | None:
    return find_balance(data, WALLET_SYMBOLS["USDT"])


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
        args = [
            "swap", str(amount), from_token, to_token,
            "--chain", CHAIN, "--slippage", str(slippage_pct),
        ]
        # Executed swaps need the wallet password. macOS pulls it from the
        # keychain; headless Linux (VPS) must supply it via env.
        password = os.getenv("TWAK_WALLET_PASSWORD", "")
        if password:
            args += ["--password", password]
        return _run(args)


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
        # verified shape: {"securityInfo": {"riskLevel": "low", ...}, ...}
        level = str((result.get("securityInfo") or {}).get("riskLevel") or "").lower()
        if level in ("low", "medium"):
            return None
        if level == "":
            return f"risk level missing for {symbol}, failing closed: {result}"
        return f"twak risk check flagged {symbol}: {level}"

    def buy(self, portfolio: Portfolio, symbol: str, size_usdt: float, quote_price: float) -> Fill:
        token = SWAP_IDS[symbol]
        result = self.client.swap(size_usdt, SWAP_IDS["USDT"], token)
        # TODO(verify): executed-swap response shape on the Day 8 dry run;
        # quote-only confirmed to use "output". Fall back to minReceived.
        qty = _amount(result.get("output") or result.get("minReceived"))
        if qty <= 0:
            # sanitize: exit-0-but-malformed output is exactly where a CLI
            # might echo its own (password-bearing) invocation back
            raise TwakError(f"swap returned no output amount: {_sanitize(str(result))}")
        price = size_usdt / qty
        portfolio.cash -= size_usdt
        portfolio.positions[symbol] = Position(symbol, qty, price, time.time())
        return Fill(symbol, "buy", qty, price, 0.0, None, time.time())

    def sell(self, portfolio: Portfolio, symbol: str, quote_price: float) -> Fill:
        token = SWAP_IDS[symbol]
        pos = portfolio.positions.pop(symbol)
        result = self.client.swap(pos.qty, token, SWAP_IDS["USDT"])
        proceeds = _amount(result.get("output") or result.get("minReceived"))
        if proceeds <= 0:
            portfolio.positions[symbol] = pos  # restore; swap did not fill
            raise TwakError(f"swap returned no output amount: {_sanitize(str(result))}")
        portfolio.cash += proceeds
        price = proceeds / pos.qty
        pnl = proceeds - pos.qty * pos.entry_price
        return Fill(symbol, "sell", pos.qty, price, 0.0, pnl, time.time())
