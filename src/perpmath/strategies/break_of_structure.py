"""Break of structure (BOS): follow a close beyond the latest swing point.

Idea: price makes swing highs and swing lows. A close above the latest swing
high is read as the market "breaking structure" upwards, and the strategy
follows it; the mirror image for a close below the latest swing low.

Definition used here. "Structure" is taught with a chart and a marker pen;
this module pins it down as follows.

Rules (everything is evaluated on a bar's close; orders fill at the next
bar's open):

1. Swing high: a bar whose high is strictly above the highs of the
   ``swing_left`` bars before it and of the ``swing_right`` bars after it.
   Swing low: a bar whose low is strictly below the lows of the same
   neighbours. A tie with a neighbour means no swing.
2. Confirmation: a swing at bar ``p`` is only known when bar
   ``p + swing_right`` has closed. It does not exist for the strategy before
   that close, neither as a level to break nor as a stop.
3. Latest swing high / low: the most recently confirmed one. A newly
   confirmed swing replaces the older one, whether it is higher or lower.
4. Break up: this bar closes above the latest swing high. Go long.
   Break down: this bar closes below the latest swing low. Go short.
   A wick through the level without a close beyond it is not a break.
5. Each swing level can be broken once. After a close beyond it, it gives no
   further signals; the next signal on that side needs a newly confirmed
   swing. This holds whether or not the break led to a trade.
6. Stop: for a long, the latest swing low minus ``stop_buffer_atr`` x ATR;
   for a short, the latest swing high plus ``stop_buffer_atr`` x ATR. "Latest"
   is as in rule 3, on the bar of the break. ATR is the simple average of the
   last ``atr_period`` true ranges on that bar.
7. No signal when there is no confirmed swing on the stop side yet, or when
   the stop from rule 6 is not on the losing side of the breaking bar's close
   (not below it for a long, not above it for a short).
8. Target: ``r_multiple`` x risk from the fill price, where risk is the
   distance between the fill price and the stop.
9. One position at a time. Signals that arrive while a position is open are
   ignored.

Judgement calls that the usual descriptions leave open, and how they are
fixed here:

* How many bars make a swing: ``swing_left`` and ``swing_right``, 5 each.
  The three-candle swing of ICT teaching (1 each side) and the five-bar
  fractal (2 each side) are also common. Fewer bars give more, smaller swings.
* Wick or close: a close beyond the level is required.
* BOS versus CHoCH: the usual teaching calls a break in the direction of the
  trend a break of structure and the first break against it a change of
  character. Telling them apart needs a definition of trend. This module does
  not make one: every break of the latest swing is traded the same way.
* No higher-timeframe bias, no session filter, no retest entry. The entry is
  a market order at the open after the breaking bar.
* The stop distance is whatever the distance to the opposite swing is. It is
  not capped, so risk per trade in price terms varies a lot.
"""

from __future__ import annotations

import math

from ..data import Bars
from ..engine import Signals
from ..indicators import atr, confirmed_swing_high, confirmed_swing_low

DEFAULTS = {
    "swing_left": 5,
    "swing_right": 5,
    "atr_period": 14,
    "stop_buffer_atr": 0.1,
    "r_multiple": 2.0,
}


def generate(bars: Bars, params: dict) -> Signals:
    n = len(bars)
    signals = Signals.empty(n)
    close = bars.close
    # Both arrays hold a level only on the bar that confirms the swing (pivot + swing_right).
    new_high = confirmed_swing_high(bars.high, params["swing_left"], params["swing_right"])
    new_low = confirmed_swing_low(bars.low, params["swing_left"], params["swing_right"])
    atr_values = atr(bars.high, bars.low, close, params["atr_period"])

    swing_high = swing_low = math.nan      # latest confirmed levels
    high_open = low_open = False           # True until a bar has closed beyond the level
    for i in range(n):
        if math.isfinite(new_high[i]):
            swing_high, high_open = float(new_high[i]), True
        if math.isfinite(new_low[i]):
            swing_low, low_open = float(new_low[i]), True

        # A bar cannot break both: an unbroken swing high is above every close
        # since it was confirmed, an unbroken swing low is below every one.
        broke_up = high_open and close[i] > swing_high
        broke_down = low_open and close[i] < swing_low
        if broke_up:
            high_open = False
        if broke_down:
            low_open = False
        if not (broke_up or broke_down) or not math.isfinite(atr_values[i]):
            continue

        buffer = params["stop_buffer_atr"] * atr_values[i]
        if broke_up and math.isfinite(swing_low) and swing_low - buffer < close[i]:
            signals.side[i], signals.stop[i] = 1, swing_low - buffer
        elif broke_down and math.isfinite(swing_high) and swing_high + buffer > close[i]:
            signals.side[i], signals.stop[i] = -1, swing_high + buffer
        if signals.side[i] != 0:
            signals.target_r[i] = params["r_multiple"]
    return signals
