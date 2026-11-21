"""Breaker block retest entry.

Idea: an order block that price closes straight through has failed. The same
zone is then expected to act the other way round (old support as resistance,
old resistance as support), and is called a breaker block. The strategy
enters on the first retest of the zone, in the direction of the close that
broke it.

Definition used here. The order blocks are exactly those of the
``order_block`` example (its rules 1 to 3, same parameters, same code). What
follows is described for a bullish order block that fails and becomes a
bearish breaker; the other case is the exact mirror image.

Rules (everything is evaluated on a bar's close; orders fill at the next
bar's open):

1. Order block: as in ``order_block``. A bullish block is the down bar
   before a qualifying up impulse; its zone runs from that bar's low (bottom)
   to its high (top).
2. Failure: a later bar closes below the bottom of the zone. From that close
   on the zone is a bearish breaker. The failure must happen within
   ``max_wait_bars`` bars of the block coming into existence; after that the
   block is forgotten. Whether price touched or respected the block before it
   failed does not matter.
3. The breaker waits for price to come back. It expires, unused, after
   ``max_wait_bars`` bars counted from the bar that broke it.
4. Entry: the first later bar whose high reaches the bottom of the zone. If
   that bar closes at or below the top of the zone, go short. If it closes
   above the top, the breaker has failed and is dropped.
5. Each breaker is used at most once, whether or not it leads to a trade.
6. Stop: the top of the zone plus ``stop_buffer_atr`` x ATR. For a long
   (bullish breaker, from a failed bearish block): the bottom of the zone
   minus the same buffer. ATR is the simple average of the last
   ``atr_period`` true ranges on the bar that broke the block.
7. Target: ``r_multiple`` x risk from the fill price, where risk is the
   distance between the fill price and the stop.
8. One position at a time. Signals that arrive while a position is open are
   ignored. If several breakers trigger on the same bar, the most recently
   broken one is used.

Judgement calls that the usual descriptions leave open, and how they are
fixed here:

* What "failed" means: one close beyond the far edge of the block. A wick
  through it does not count, and no minimum size is asked of the breaking bar.
* Some descriptions only call it a breaker when the failed block came from a
  move that first ran a prior high or low (a liquidity sweep). That condition
  is not applied: every failed order block is a breaker.
* Zone: the whole order-block candle, high to low, unchanged by the failure.
* Time limits: one parameter, ``max_wait_bars``, is used for both clocks
  (block to failure, failure to retest).
* No higher-timeframe bias, no session filter.

Approximation forced by the engine. As in ``order_block``, there is no
resting limit order at the edge of the zone: the entry is a market order at
the open after the retest bar has closed, so the fill can be anywhere
relative to the zone.
"""

from __future__ import annotations

from typing import List, NamedTuple

from ..data import Bars
from ..engine import Signals
from ..indicators import atr
from .order_block import OrderBlock, find_order_blocks

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


class Breaker(NamedTuple):
    direction: int   # trade direction: -1 = bearish breaker (failed bullish block), +1 = bullish
    bottom: float
    top: float
    stop: float
    broken: int      # index of the bar that closed through the block


def generate(bars: Bars, params: dict) -> Signals:
    n = len(bars)
    signals = Signals.empty(n)
    high, low, close = bars.high, bars.low, bars.close
    formed = find_order_blocks(bars, params)
    atr_values = atr(high, low, close, params["atr_period"])
    max_wait = params["max_wait_bars"]

    blocks: List[OrderBlock] = []    # order blocks that have not failed yet, oldest first
    breakers: List[Breaker] = []     # failed blocks waiting for a retest, oldest first
    for i in range(n):
        # Breakers from earlier bars: has price come back on this bar?
        still_waiting = []
        for breaker in breakers:
            if i - breaker.broken > max_wait:
                continue
            if breaker.direction == -1 and high[i] >= breaker.bottom:
                if close[i] <= breaker.top:
                    signals.side[i], signals.stop[i] = -1, breaker.stop
                continue
            if breaker.direction == 1 and low[i] <= breaker.top:
                if close[i] >= breaker.bottom:
                    signals.side[i], signals.stop[i] = 1, breaker.stop
                continue
            still_waiting.append(breaker)
        breakers = still_waiting
        if signals.side[i] != 0:
            signals.target_r[i] = params["r_multiple"]

        # Order blocks from earlier bars: does this bar close through one?
        buffer = params["stop_buffer_atr"] * atr_values[i]
        intact = []
        for block in blocks:
            if i - block.formed > max_wait:
                continue
            if block.direction == 1 and close[i] < block.bottom:
                breakers.append(Breaker(-1, block.bottom, block.top, block.top + buffer, i))
            elif block.direction == -1 and close[i] > block.top:
                breakers.append(Breaker(1, block.bottom, block.top, block.bottom - buffer, i))
            else:
                intact.append(block)
        blocks = intact

        if formed[i] is not None:
            blocks.append(formed[i])
    return signals
