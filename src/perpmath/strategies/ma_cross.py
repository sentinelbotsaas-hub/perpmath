"""Moving-average cross: the boring baseline.

Rules (everything is evaluated on a bar's close; orders fill at the next
bar's open):

1. fast = simple moving average of the last ``fast_period`` closes.
   slow = simple moving average of the last ``slow_period`` closes.
2. Cross up: fast > slow on this bar and fast <= slow on the previous bar.
   Close any short and go long.
3. Cross down: fast < slow on this bar and fast >= slow on the previous bar.
   Close any long and go short.
4. Stop: for a long, the signal bar's close minus ``stop_atr_mult`` x ATR;
   for a short, the signal bar's close plus ``stop_atr_mult`` x ATR.
   ATR is the simple average of the last ``atr_period`` true ranges.
5. There is no profit target. A position ends at the opposite cross or at the
   stop, whichever comes first. After a stop the strategy waits, flat, for
   the next cross.
"""

from __future__ import annotations

import numpy as np

from ..data import Bars
from ..engine import Signals
from ..indicators import atr, sma

DEFAULTS = {
    "fast_period": 20,
    "slow_period": 50,
    "atr_period": 14,
    "stop_atr_mult": 2.0,
}


def generate(bars: Bars, params: dict) -> Signals:
    if params["fast_period"] >= params["slow_period"]:
        raise ValueError("fast_period must be smaller than slow_period")
    n = len(bars)
    signals = Signals.empty(n)
    fast = sma(bars.close, params["fast_period"])
    slow = sma(bars.close, params["slow_period"])
    stop_distance = params["stop_atr_mult"] * atr(bars.high, bars.low, bars.close, params["atr_period"])

    up = np.zeros(n, dtype=bool)
    down = np.zeros(n, dtype=bool)
    up[1:] = (fast[1:] > slow[1:]) & (fast[:-1] <= slow[:-1])
    down[1:] = (fast[1:] < slow[1:]) & (fast[:-1] >= slow[:-1])

    signals.exit_short[:] = up
    signals.exit_long[:] = down
    has_stop = np.isfinite(stop_distance)
    go_long, go_short = up & has_stop, down & has_stop
    signals.side[go_long] = 1
    signals.side[go_short] = -1
    signals.stop[go_long] = bars.close[go_long] - stop_distance[go_long]
    signals.stop[go_short] = bars.close[go_short] + stop_distance[go_short]
    return signals
