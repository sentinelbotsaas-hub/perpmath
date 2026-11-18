"""Liquidity sweep fade.

Idea: price pokes through an obvious prior high or low, fails to hold beyond
it and closes back inside the old range. The strategy trades against the poke.

Rules (everything is evaluated on a bar's close; orders fill at the next
bar's open):

1. prior high = highest high of the ``lookback`` bars before this bar.
   prior low  = lowest low of the ``lookback`` bars before this bar.
   The current bar is not part of either.
2. High sweep: this bar's high is above the prior high and its close is below
   the prior high. Go short.
3. Low sweep: this bar's low is below the prior low and its close is above
   the prior low. Go long.
4. A bar that sweeps both sides gives no signal.
5. Stop: for a short, the sweep bar's high plus ``stop_buffer_atr`` x ATR;
   for a long, the sweep bar's low minus ``stop_buffer_atr`` x ATR.
   ATR is the simple average of the last ``atr_period`` true ranges.
6. Target: ``r_multiple`` x risk from the fill price, where risk is the
   distance between the fill price and the stop.
7. One position at a time. Signals that arrive while a position is open are
   ignored.
"""

from __future__ import annotations

import numpy as np

from ..data import Bars
from ..engine import Signals
from ..indicators import atr, prior_high, prior_low

DEFAULTS = {
    "lookback": 20,
    "atr_period": 14,
    "stop_buffer_atr": 0.1,
    "r_multiple": 2.0,
}


def generate(bars: Bars, params: dict) -> Signals:
    signals = Signals.empty(len(bars))
    range_high = prior_high(bars.high, params["lookback"])
    range_low = prior_low(bars.low, params["lookback"])
    buffer = params["stop_buffer_atr"] * atr(bars.high, bars.low, bars.close, params["atr_period"])

    # Comparisons against NaN (warm-up bars) are False, so no signal there.
    swept_high = (bars.high > range_high) & (bars.close < range_high)
    swept_low = (bars.low < range_low) & (bars.close > range_low)
    ready = np.isfinite(buffer)
    go_short = swept_high & ~swept_low & ready
    go_long = swept_low & ~swept_high & ready

    signals.side[go_long] = 1
    signals.side[go_short] = -1
    signals.stop[go_long] = bars.low[go_long] - buffer[go_long]
    signals.stop[go_short] = bars.high[go_short] + buffer[go_short]
    signals.target_r[go_long | go_short] = params["r_multiple"]
    return signals
