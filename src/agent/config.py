"""Central configuration: env loading + the risk rules judges score adherence to.

The risk constants here ARE the "user-defined rules" for the hackathon rubric.
They are deliberately module-level constants (not env-tunable) so the judged
replay can point at one immutable definition.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

load_dotenv(PROJECT_ROOT / ".env")


# --- Risk rules (immutable, judged) -----------------------------------------
MAX_POSITION_PCT = 0.20          # max 20% of capital per position
STOP_LOSS_PCT = 0.03             # per-trade stop-loss at -3%
DAILY_LOSS_CAP_PCT = 0.05        # -5% daily loss -> flatten + halt 24h
KILL_SWITCH_DRAWDOWN_PCT = 0.10  # -10% drawdown from peak -> flatten + stop
HALT_HOURS = 24

# --- Trading universe --------------------------------------------------------
# CMC symbols we trade against USDT on PancakeSwap (BSC). BTCB tracks BTC.
UNIVERSE = ["BNB", "BTC", "ETH"]
QUOTE_ASSET = "USDT"

# --- Strategy parameters ------------------------------------------------------
RSI_PERIOD = 14
RSI_ENTRY_MIN = 50.0   # enter only with momentum confirmed...
RSI_ENTRY_MAX = 70.0   # ...but not overbought
RSI_EXIT = 75.0
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
MIN_HISTORY = MACD_SLOW + MACD_SIGNAL  # bars needed before signals are valid
FEAR_GREED_VETO_BELOW = 20  # extreme fear -> no new entries (sentiment veto)

# --- Paper execution model ----------------------------------------------------
PAPER_FEE_PCT = 0.0025      # PancakeSwap v2 LP fee
PAPER_SLIPPAGE_PCT = 0.001  # assumed slippage on top-liquidity pairs


@dataclass
class Settings:
    cmc_api_key: str = field(default_factory=lambda: os.getenv("CMC_API_KEY", ""))
    twak_access_id: str = field(default_factory=lambda: os.getenv("TWAK_ACCESS_ID", ""))
    twak_hmac_secret: str = field(default_factory=lambda: os.getenv("TWAK_HMAC_SECRET", ""))
    mode: str = field(default_factory=lambda: os.getenv("AGENT_MODE", "paper"))
    poll_interval: int = field(default_factory=lambda: int(os.getenv("POLL_INTERVAL_SECONDS", "60")))
    starting_capital: float = field(default_factory=lambda: float(os.getenv("STARTING_CAPITAL_USDT", "150")))


def get_settings() -> Settings:
    return Settings()
