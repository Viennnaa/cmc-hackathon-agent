"""Strategy menu: every variant shares momentum's Signal interface.

The registry is what the nightly self-review selects from and what the
backtest CLI exposes — add a strategy here and both pick it up.
"""

from agent.strategy import mean_revert, momentum  # noqa: F401 (registry deps first)
from agent.strategy import adaptive

STRATEGIES = {
    "momentum": momentum.evaluate,
    "mean_revert": mean_revert.evaluate,
    "adaptive": adaptive.evaluate,
}

DEFAULT_STRATEGY = "adaptive"
