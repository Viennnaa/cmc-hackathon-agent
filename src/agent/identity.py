"""ERC-8004 on-chain agent identity via the BNB Agent SDK.

One-time registration on BSC testnet (gas-free through the MegaFuel
paymaster). The identity wallet is separate from the TWAK trading wallet:
the SDK auto-generates a key and encrypts it to ~/.bnbagent/wallets/ using
WALLET_PASSWORD. The resulting agentId (ERC-721) is saved to
data/identity.json and referenced in the submission.

Run:  python -m agent.identity            (registers once, then idempotent)
      python -m agent.identity --show     (print saved identity)
"""

import argparse
import json
import os

from agent.config import DATA_DIR

IDENTITY_PATH = DATA_DIR / "identity.json"

AGENT_NAME = "cmc-disciplined-trader"
AGENT_DESCRIPTION = (
    "Autonomous BSC trading agent for the CMC x Trust Wallet x BNB Chain "
    "hackathon (Track 1). CMC Data API signals (quotes, Fear & Greed, 24h "
    "regime) drive a deterministic momentum strategy behind hard risk gates "
    "(20% position cap, -3% stop-loss, -5% daily halt, -10% kill switch, "
    "token-risk and sentiment vetoes). Executes self-custodially via the "
    "Trust Wallet Agent Kit on PancakeSwap; every decision is journaled "
    "for replay."
)
REPO_URL = "https://github.com/Viennnaa/cmc-hackathon-agent"


def register(debug: bool = False) -> dict:
    if IDENTITY_PATH.exists():
        identity = json.loads(IDENTITY_PATH.read_text())
        print(f"already registered: agentId {identity['agentId']} "
              f"(tx {identity['transactionHash']})")
        return identity

    from bnbagent import AgentEndpoint, ERC8004Agent, EVMWalletProvider

    password = os.getenv("WALLET_PASSWORD")
    if not password:
        raise SystemExit("WALLET_PASSWORD not set in .env (used to encrypt "
                         "the identity keystore in ~/.bnbagent/wallets/)")

    if debug:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    wallet = EVMWalletProvider(password=password)
    sdk = ERC8004Agent(network="bsc-testnet", wallet_provider=wallet, debug=debug)
    agent_uri = sdk.generate_agent_uri(
        name=AGENT_NAME,
        description=AGENT_DESCRIPTION,
        endpoints=[
            AgentEndpoint(name="repository", endpoint=REPO_URL, version="0.1.0"),
        ],
    )
    result = sdk.register_agent(agent_uri=agent_uri)

    identity = {
        "agentId": result["agentId"],
        "transactionHash": result["transactionHash"],
        "network": "bsc-testnet",
        "name": AGENT_NAME,
        "agentUri": agent_uri,
    }
    IDENTITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    IDENTITY_PATH.write_text(json.dumps(identity, indent=2))
    print(f"registered! agentId {identity['agentId']} tx {identity['transactionHash']}")
    return identity


def main() -> None:
    parser = argparse.ArgumentParser(description="ERC-8004 agent identity")
    parser.add_argument("--show", action="store_true", help="print saved identity")
    parser.add_argument("--debug", action="store_true", help="verbose SDK/paymaster logging")
    args = parser.parse_args()

    if args.show:
        if IDENTITY_PATH.exists():
            print(IDENTITY_PATH.read_text())
        else:
            print("not registered yet — run: python -m agent.identity")
        return
    register(debug=args.debug)


if __name__ == "__main__":
    main()
