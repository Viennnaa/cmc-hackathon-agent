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


# --- Risk rules ---------------------------------------------------------------
# Tuned for the BNB Hack PnL competition: ranked by TOTAL RETURN, with a ~30%
# drawdown DISQUALIFICATION gate (not risk-adjusted scoring) and a >=1 trade/day
# qualification rule. So these run aggressive within a safety margin of the DQ
# line, not for capital preservation. The only externally-binding constraints
# are the ~30% drawdown gate and the daily-trade rule; the rest are guardrails
# (they still earn the "autonomous execution & guardrails" special-prize points).
MAX_POSITION_PCT = 0.15          # ~15%/name: diversify across many names rather than big single bets;
                                 # not smaller because fixed BNB gas would dominate tiny $ positions
MAX_CONCURRENT_POSITIONS = 6     # up to 6 concurrent names (~90% max deployed, ~10% reserve)
STOP_LOSS_PCT = 0.08             # per-trade stop -8% (looser: -3% whipsawed out on noise)
DAILY_LOSS_CAP_PCT = 0.15        # -15% in a UTC day -> flatten + short cool-off
KILL_SWITCH_DRAWDOWN_PCT = 0.25  # -25% drawdown from peak -> flatten + stop (margin under ~30% DQ)
HALT_HOURS = 6                   # cool-off after the daily cap; short so the next UTC day can still trade

# --- Trading universe --------------------------------------------------------
# Competition-ELIGIBLE BEP-20 tokens only (the 149-token CMC list; trades in
# anything off-list do not count). Curated 2026-06-15 to 15 liquid majors/
# mid-caps with deep PancakeSwap pools — dropped BNB/BTC/SOL (NOT on the
# eligible list). USDT is the cash base (also eligible). BNB stays a gas/native
# asset for fees but is never traded (see execution/twak.py WALLET_SYMBOLS).
UNIVERSE = ["ETH", "XRP", "DOGE", "ADA", "LINK", "AVAX", "LTC", "AAVE",
            "DOT", "UNI", "SHIB", "FET", "INJ", "CAKE", "TWT"]
QUOTE_ASSET = "USDT"

# --- Strategy parameters ------------------------------------------------------
# Bar size chosen empirically: 15m churned 38-48 round trips/14d and lost
# ~10% to fees; 1h cut that to 5-10 trips with max drawdown under 2.1%.
BAR_SECONDS = 3600           # indicators run on 1h bar closes (live + backtest)
REENTRY_COOLDOWN_SECONDS = 1 * BAR_SECONDS  # 1h: allow quick re-entries (was 8h) for activity/PnL
RSI_PERIOD = 14
RSI_ENTRY_MIN = 50.0   # enter only with momentum confirmed...
RSI_ENTRY_MAX = 70.0   # ...but not overbought
RSI_EXIT = 75.0
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
MIN_HISTORY = MACD_SLOW + MACD_SIGNAL  # bars needed before signals are valid
FEAR_GREED_VETO_BELOW = 20  # DISABLED in the engine + momentum (a PnL race must trade
                            # through fear); retained only for the backtest --fng-compare tool
REGIME_MIN_24H_CHANGE = 0.0  # was +1%; relaxed so uptrend momentum crosses can enter (activity/PnL)

# --- Adaptive regime router ----------------------------------------------------
# The 24h filter above is blind to anything longer — bear-market bounces clear
# +1%/24h constantly (2026-06 backtests: momentum alone -7.12%/30d, every entry
# a failed bounce). The router classifies each symbol against a 5-day SMA and
# changes BEHAVIOR per regime instead of just muting entries:
#   uptrend  (px > sma*(1+band)) -> momentum entries
#   downtrend(px < sma*(1-band)) -> cash: no entries, exits still honored
#   chop     (inside the band)   -> mean-reversion dip buying
REGIME_SMA_BARS = 120   # 5 days of 1h bars
REGIME_BAND_PCT = 0.01  # +/-1% neutral band around the SMA

# --- Daily-trade floor (qualification) ------------------------------------------
# The competition disqualifies any agent that does not make >=1 trade per UTC
# day. The strategy trades most days on its own; this is a safety net: if a UTC
# day reaches the cutoff hour with zero executed trades, the runner forces one
# minimal compliant swap in the most liquid eligible token.
DAILY_TRADE_FLOOR_HOUR_UTC = 22   # force a floor trade after this hour if the day is still flat
DAILY_FLOOR_TRADE_USDT = 2.0      # minimal size, kept tiny to limit fee/PnL drag
DAILY_FLOOR_SYMBOL = "ETH"        # deepest-liquidity eligible token

# --- Nightly self-review --------------------------------------------------------
# Once per UTC day the agent replays its own sampled prices through every
# strategy in the menu and adopts the best trailing performer. NARROW-ONLY:
# the review may shrink entry sizes (factor < 1) but can never raise any risk
# limit — the judged rules above stay immutable.
SELF_REVIEW_TRAILING_DAYS = 14
SELF_REVIEW_MIN_BARS = 48                # no review until 2 days of bars exist
SELF_REVIEW_DEFENSIVE_RETURN = -0.05     # best trailing return under -5% ...
SELF_REVIEW_DEFENSIVE_SIZE_FACTOR = 0.5  # ... -> halve entry sizes (never raise)

# --- Data-staleness protection (fail closed) -----------------------------------
# A held position with no fresh price gets no stop-loss enforcement, so stale
# or missing quotes trigger a protective exit rather than silent exposure.
STALE_QUOTE_MAX_AGE_SECONDS = 300     # CMC quote older than this is not a fresh price
STALE_QUOTE_FLATTEN_SECONDS = 600     # held symbol unpriced this long -> protective exit

# --- Quote-plausibility protection (fail closed) --------------------------------
# One glitched CMC sample must not poison the rules: an up-spike permanently
# inflates peak_equity (arming the kill switch against honest quotes), a
# down-spike fires every stop-loss at once. A quote jumping more than the
# bound vs the last accepted sample is quarantined (routes into the
# quote-gap protection) until a second consecutive poll agrees.
QUOTE_MAX_JUMP_PCT = 0.15        # poll-to-poll move beyond this needs confirmation
QUOTE_CONFIRM_TOLERANCE_PCT = 0.05  # second poll within this of the suspect = real move

# --- Live gas safety (fail closed) ----------------------------------------------
# Exits must always be fundable: running out of BNB mid-window would strand a
# position with a stop-loss that cannot execute.
MIN_GAS_BNB_START = 0.005        # refuse to start live below this BNB balance
LOW_GAS_BNB_WARN = 0.006         # warn ABOVE the start threshold: top up before a
                                 # restart would be refused, not after (no dead zone)
GAS_CHECK_INTERVAL_SECONDS = 3600
LIVE_QTY_MISMATCH_TOLERANCE = 0.02  # wallet vs portfolio.json qty drift allowed on restart

# --- Paper execution model ----------------------------------------------------
PAPER_FEE_PCT = 0.0025      # PancakeSwap v2 LP fee
PAPER_SLIPPAGE_PCT = 0.001  # assumed slippage on top-liquidity pairs

# --- x402 autonomous micropayment (special-prize demo, OFF by default) ---------
# The agent pays CMC's x402 endpoint $0.01/call to prove autonomous on-chain
# payment. CMC documents exactly ONE route: USDC on Base via EIP-3009, which is
# GASLESS (the facilitator submits the transfer; pay-only-on-success). The wallet
# must hold USDC on Base, funded separately from the BSC trading capital — so
# x402 spend never touches the judged PnL. Pinned to that route; never spends
# unless X402_ENABLED, and never past X402_MAX_SPEND_USD.
X402_INTERVAL_SECONDS = 3600     # one payment/hour (~$1.68 across the 7-day window)
X402_QUOTE_SYMBOL = "ETH"        # buy a quote for this + cross-check vs the live feed
X402_QUOTE_URL = ("https://pro-api.coinmarketcap.com/x402/v3/cryptocurrency/"
                  "quotes/latest?symbol=ETH&convert=USDT")
X402_NETWORK = "base"                                       # Base (eip155:8453)
X402_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"   # USDC on Base (6 decimals)
X402_METHOD = "eip3009"                                     # gasless; no approval tx
X402_MAX_PAYMENT_ATOMIC = "10000"  # 0.01 USDC @ 6dp — caps auto-approval to the exact charge
X402_COST_USD = 0.01               # per-call cost, for budget accounting
# eip3009 settles server-side (no per-call tx hash), so the dashboard links to
# the payer wallet's USDC transfers on BaseScan as aggregate on-chain proof.
# Public address; override with X402_PAYER if the wallet changes.
X402_PAYER = os.getenv("X402_PAYER", "0x1e75d8e9039Cd9DE389CB696df52c46d44c85279")


@dataclass
class Settings:
    cmc_api_key: str = field(default_factory=lambda: os.getenv("CMC_API_KEY", ""))
    twak_access_id: str = field(default_factory=lambda: os.getenv("TWAK_ACCESS_ID", ""))
    twak_hmac_secret: str = field(default_factory=lambda: os.getenv("TWAK_HMAC_SECRET", ""))
    mode: str = field(default_factory=lambda: os.getenv("AGENT_MODE", "paper"))
    poll_interval: int = field(default_factory=lambda: int(os.getenv("POLL_INTERVAL_SECONDS", "60")))
    starting_capital: float = field(default_factory=lambda: float(os.getenv("STARTING_CAPITAL_USDT", "150")))
    # x402 spends real USDC: opt-in only, hard-capped. Empty/unset env -> disabled.
    x402_enabled: bool = field(default_factory=lambda:
                               os.getenv("X402_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"))
    x402_max_spend_usd: float = field(default_factory=lambda: float(os.getenv("X402_MAX_SPEND_USD", "2.50")))


def get_settings() -> Settings:
    return Settings()
