"""Performance metrics.

Values that cannot be computed (for example a t-statistic from one trade) are
``None`` rather than zero, so that "undefined" is never mistaken for "flat".
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Sequence

import numpy as np

MIN_TRADES = 100  # below this the sample is flagged as insufficient


def t_statistic(values: Sequence[float]) -> Optional[float]:
    """One-sample t-statistic of ``values`` against a mean of zero.

    t = mean / (sample standard deviation / sqrt(n)), with the n - 1 denominator
    in the standard deviation. ``None`` when n < 2 or the values do not vary.
    """
    arr = np.asarray(values, dtype=np.float64)
    if len(arr) < 2:
        return None
    sd = float(np.std(arr, ddof=1))
    if sd == 0.0:
        return None
    return float(np.mean(arr)) / (sd / math.sqrt(len(arr)))


def max_drawdown_pct(equity: Sequence[float], initial_capital: float) -> float:
    """Largest peak-to-trough fall of the equity curve, as a percentage of the peak.

    The starting capital counts as the first peak.
    """
    arr = np.asarray(equity, dtype=np.float64)
    if len(arr) == 0:
        return 0.0
    peaks = np.maximum.accumulate(np.concatenate(([initial_capital], arr)))[1:]
    return float(np.max((peaks - arr) / peaks) * 100)


def compute_metrics(trades: Sequence[Any], equity: Sequence[float],
                    initial_capital: float) -> Dict[str, Any]:
    """Summarise a list of trades and the bar-by-bar equity curve they produced.

    A trade needs ``pnl``, ``return_pct``, ``r_multiple``, ``fees``,
    ``bars_held`` and ``exit_reason`` attributes. A win is a trade with
    ``pnl > 0`` after fees.
    """
    n = len(trades)
    final_equity = float(equity[-1]) if len(equity) else float(initial_capital)
    returns = [t.return_pct for t in trades]
    pnls = [t.pnl for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    reasons: Dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

    return {
        "trades": n,
        "insufficient_sample": n < MIN_TRADES,
        "wins": wins,
        "losses": sum(1 for p in pnls if p < 0),
        "win_rate_pct": wins / n * 100 if n else None,
        "avg_r": float(np.mean([t.r_multiple for t in trades])) if n else None,
        "avg_trade_return_pct": float(np.mean(returns)) if n else None,
        "t_stat": t_statistic(returns),
        "t_stat_n": n,
        "total_return_pct": (final_equity / initial_capital - 1) * 100,
        "max_drawdown_pct": max_drawdown_pct(equity, initial_capital),
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "total_fees": float(sum(t.fees for t in trades)),
        "avg_bars_held": float(np.mean([t.bars_held for t in trades])) if n else None,
        "final_equity": final_equity,
        "exit_reasons": dict(sorted(reasons.items())),
    }
