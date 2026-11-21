"""Order block return entry.

Idea: the last candle against the direction of a strong move marks a price
zone (the "order block"). When price comes back to that zone, the strategy
enters in the direction of the move.

Definition used here. Order blocks are taught by example and the examples
disagree, so this module fixes one checkable definition. The bullish case is
described; the bearish case is the exact mirror image.

Rules (everything is evaluated on a bar's close; orders fill at the next
bar's open):

1. Up bar: close above open. Down bar: close below open. A bar that closes
   exactly at its open is neither.
2. Impulse: a run of consecutive up bars. The run qualifies on the first of
   its bars on which all of the following hold:
   a. the run is at least ``impulse_bars`` bars long;
   b. from the open of its first bar to this bar's close it has risen at
      least ``min_impulse_atr`` x ATR;
   c. it broke structure: this bar closes above the latest confirmed swing
      high, and the bar before the run closed at or below that level;
   d. this bar closes above the high of the bar before the run, so price has
      left the block.
   A swing high is a bar whose high is strictly above the highs of the
   ``swing_left`` bars before it and the ``swing_right`` bars after it. It is
   only known, and only used, once bar ``pivot + swing_right`` has closed.
3. Order block: the bar immediately before the run, provided it is a down
   bar. The zone runs from its low (bottom) to its high (top), wicks included.
   ATR is the simple average of the last ``atr_period`` true ranges, measured
   on the order-block bar, that is, before the impulse.
4. The block exists from the close of the bar on which the run qualified. It
   then waits for price to come back and expires, unused, after
   ``max_wait_bars`` bars.
5. Entry: the first later bar whose low reaches the top of the zone. If that
   bar closes at or above the bottom of the zone, go long. If it closes below
   the bottom, the block has failed and is dropped.
6. Each block is used at most once, whether or not it leads to a trade. A
   candle is never registered as a block twice.
7. Stop: the bottom of the zone minus ``stop_buffer_atr`` x ATR (ATR as in
   rule 3). For a short: the top of the zone plus the same buffer.
8. Target: ``r_multiple`` x risk from the fill price, where risk is the
   distance between the fill price and the stop.
9. One position at a time. Signals that arrive while a position is open are
   ignored. If several blocks trigger on the same bar, the most recently
   formed one is used.

Judgement calls that the usual descriptions leave open, and how they are
fixed here:

* Which candle: the single bar immediately before the run, not "the last
  down candle somewhere near the low".
* How big a move: ``impulse_bars`` same-direction closes in a row covering
  ``min_impulse_atr`` x ATR, plus a close beyond the latest confirmed swing
  point. A fair value gap inside the move is not required.
* Zone: the whole candle, high to low. Body-only zones and the candle's
  midpoint ("mean threshold") are also taught; they are not used.
* Mitigation: the first bar that touches the zone uses the block up.
* No higher-timeframe bias, no session filter, no premium/discount filter.

Approximation forced by the engine. The usual description rests a limit order
at the edge of the block. The engine has no limit orders, so the entry is a
market order at the open after the bar that came back to the zone, as in the
``fvg`` example. The fill can be anywhere relative to the zone: above it,
inside it, or below it but above the stop.
"""

from __future__ import annotations

import math
from typing import List, NamedTuple, Optional

from ..data import Bars
from ..engine import Signals
from ..indicators import atr, carry_forward, confirmed_swing_high, confirmed_swing_low

DEFAULTS = {
    "atr_period": 14,
    "swing_left": 5,
    "swing_right": 5,
    "impulse_bars": 3,
    "min_impulse_atr": 2.0,
    "max_wait_bars": 96,
    "stop_buffer_atr": 0.1,
    "r_multiple": 2.0,
}


class OrderBlock(NamedTuple):
    direction: int   # +1 = bullish (down bar before an up impulse), -1 = bearish
    bottom: float    # low of the order-block bar
    top: float       # high of the order-block bar
    atr: float       # ATR on the order-block bar
    candle: int      # index of the order-block bar
    formed: int      # index of the bar on which the impulse qualified


def find_order_blocks(bars: Bars, params: dict) -> List[Optional[OrderBlock]]:
    """Rules 1 to 3: for each bar, the order block that comes into existence on its close, or None.

    The entry for bar ``i`` uses bars up to ``i`` only. Also used by ``breaker_block``.
    """
    n = len(bars)
    open_, high, low, close = bars.open, bars.high, bars.low, bars.close
    atr_values = atr(high, low, close, params["atr_period"])
    # Latest swing level confirmed on or before each bar (pivot + swing_right <= bar).
    swing_high = carry_forward(confirmed_swing_high(high, params["swing_left"], params["swing_right"]))
    swing_low = carry_forward(confirmed_swing_low(low, params["swing_left"], params["swing_right"]))
    min_bars, min_move = params["impulse_bars"], params["min_impulse_atr"]

    formed: List[Optional[OrderBlock]] = [None] * n
    direction, run = 0, 0   # direction and length of the run of same-direction bars ending here
    last_candle = -1        # most recent bar already registered as a block
    for i in range(n):
        bar_direction = 1 if close[i] > open_[i] else -1 if close[i] < open_[i] else 0
        run = run + 1 if bar_direction == direction else 1
        direction = bar_direction
        candle = i - run    # the bar immediately before the run
        if direction == 0 or run < min_bars or candle < 0 or candle == last_candle:
            continue
        if not math.isfinite(atr_values[candle]):
            continue
        level = swing_high[i] if direction == 1 else swing_low[i]
        if not math.isfinite(level):
            continue
        # Multiplying by the direction turns every bearish test into the bullish one.
        opposite = direction * (close[candle] - open_[candle]) < 0
        big_enough = direction * (close[i] - open_[candle + 1]) >= min_move * atr_values[candle]
        broke_structure = direction * (close[candle] - level) <= 0 < direction * (close[i] - level)
        left_block = close[i] > high[candle] if direction == 1 else close[i] < low[candle]
        if opposite and big_enough and broke_structure and left_block:
            formed[i] = OrderBlock(direction, float(low[candle]), float(high[candle]),
                                   float(atr_values[candle]), candle, i)
            last_candle = candle
    return formed


def generate(bars: Bars, params: dict) -> Signals:
    n = len(bars)
    signals = Signals.empty(n)
    high, low, close = bars.high, bars.low, bars.close
    formed = find_order_blocks(bars, params)
    max_wait = params["max_wait_bars"]

    waiting: List[OrderBlock] = []   # oldest first
    for i in range(n):
        # Blocks formed on earlier bars: has price come back on this bar?
        still_waiting = []
        for block in waiting:
            if i - block.formed > max_wait:
                continue
            buffer = params["stop_buffer_atr"] * block.atr
            if block.direction == 1 and low[i] <= block.top:
                if close[i] >= block.bottom:
                    signals.side[i], signals.stop[i] = 1, block.bottom - buffer
                continue
            if block.direction == -1 and high[i] >= block.bottom:
                if close[i] <= block.top:
                    signals.side[i], signals.stop[i] = -1, block.top + buffer
                continue
            still_waiting.append(block)
        waiting = still_waiting
        if signals.side[i] != 0:
            signals.target_r[i] = params["r_multiple"]

        if formed[i] is not None:
            waiting.append(formed[i])
    return signals
