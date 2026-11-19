"""perpmath: a small, honest backtester for crypto perpetual-futures strategies.

Backtest only. No live trading, no order placement, no API keys.
"""

__version__ = "0.2.0"

from .backtest import LookaheadError, check_no_lookahead, run_backtest
from .data import Bars, DataError, load_bars
from .engine import ExecutionConfig, Signals, simulate
from .metrics import compute_metrics

__all__ = [
    "__version__",
    "Bars",
    "DataError",
    "ExecutionConfig",
    "LookaheadError",
    "Signals",
    "check_no_lookahead",
    "compute_metrics",
    "load_bars",
    "run_backtest",
    "simulate",
]
