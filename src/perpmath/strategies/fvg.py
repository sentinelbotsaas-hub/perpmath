"""Fair value gap (FVG) return entry.

A fair value gap is a three-candle pattern in which the first and third
candles do not overlap, leaving a price zone that only the middle candle
traded through.

Rules (everything is evaluated on a bar's close; orders fill at the next
bar's open):

1. Bullish gap: the low of bar 3 is above the high of bar 1.
   The gap zone runs from the high of bar 1 (bottom) to the low of bar 3 (top).
   Bearish gap: the high of bar 3 is below the low of bar 1.
   The gap zone runs from the high of bar 3 (bottom) to the low of bar 1 (top).
2. Size filter: the zone must be at least ``min_gap_atr`` x ATR tall, with
   ATR measured on bar 3 as the simple average of the last ``atr_period``
   true ranges.
3. The gap then waits for price to come back. It expires, unused, after
   ``max_wait_bars`` bars.
4. Bullish entry: the first later bar whose low reaches the top of the zone.
   If that bar closes at or above the bottom of the zone, go long.
   If it closes below the bottom, the gap has failed and is dropped.
   Bearish entry: the first later bar whose high reaches the bottom of the
   zone. If that bar closes at or below the top of the zone, go short.
   If it closes above the top, the gap has failed and is dropped.
5. Each gap is used at most once, whether or not it leads to a trade.
6. Stop: for a long, the bottom of the zone minus ``stop_buffer_atr`` x ATR;
   for a short, the top of the zone plus ``stop_buffer_atr`` x ATR
   (ATR as measured when the gap formed).
7. Target: ``r_multiple`` x risk from the fill price, where risk is the
   distance between the fill price and the stop.
8. One position at a time. Signals that arrive while a position is open are
   ignored. If several gaps trigger on the same bar, the most recently formed
   one is used.

Note that the entry is a market order after the return bar has closed, not a
resting limit order at the edge of the gap. The fill can therefore be
anywhere relative to the zone.
"""

from __future__ import annotations

import math

from ..data import Bars
from ..engine import Signals
from ..indicators import atr

DEFAULTS = {
    "atr_period": 14,
    "min_gap_atr": 0.5,
    "max_wait_bars": 96,
    "stop_buffer_atr": 0.1,
    "r_multiple": 2.0,
}


def generate(bars: Bars, params: dict) -> Signals:
    n = len(bars)
    signals = Signals.empty(n)
    high, low, close = bars.high, bars.low, bars.close
    atr_values = atr(high, low, close, params["atr_period"])
    max_wait = params["max_wait_bars"]

    waiting = []  # (direction, bottom, top, stop, bar index the gap formed on), oldest first
    for i in range(n):
        # Gaps formed on earlier bars: has price come back on this bar?
        still_waiting = []
        for gap in waiting:
            direction, bottom, top, stop, born = gap
            if i - born > max_wait:
                continue
            if direction == 1 and low[i] <= top:
                if close[i] >= bottom:
                    signals.side[i], signals.stop[i] = 1, stop
                continue
            if direction == -1 and high[i] >= bottom:
                if close[i] <= top:
                    signals.side[i], signals.stop[i] = -1, stop
                continue
            still_waiting.append(gap)
        waiting = still_waiting
        if signals.side[i] != 0:
            signals.target_r[i] = params["r_multiple"]

        # Does this bar complete a new gap with the two bars before it?
        if i < 2 or not math.isfinite(atr_values[i]):
            continue
        min_size = params["min_gap_atr"] * atr_values[i]
        buffer = params["stop_buffer_atr"] * atr_values[i]
        if low[i] > high[i - 2] and low[i] - high[i - 2] >= min_size:
            bottom, top = float(high[i - 2]), float(low[i])
            waiting.append((1, bottom, top, bottom - buffer, i))
        elif high[i] < low[i - 2] and low[i - 2] - high[i] >= min_size:
            bottom, top = float(high[i]), float(low[i - 2])
            waiting.append((-1, bottom, top, top + buffer, i))
    return signals
