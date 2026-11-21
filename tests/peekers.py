"""Strategies that cheat on purpose, for the look-ahead tests. None of them is registered in ``STRATEGIES``.

Each one looks only a few bars ahead, which is the kind of look-ahead a check
with a handful of fixed cuts lets through.
"""

import numpy as np

from perpmath.engine import Signals
from perpmath.strategies import break_of_structure, liquidity_sweep, ma_cross, order_block


class EarlyMaCross:
    """``ma_cross`` with every decision made one bar early.

    Bar ``i`` carries the entry, stop and exit flags that the honest strategy
    only has on the close of bar ``i + 1``, so the order fills at the open of
    the bar whose close decided it.
    """

    DEFAULTS = ma_cross.DEFAULTS

    @staticmethod
    def generate(bars, params):
        honest = ma_cross.generate(bars, params)
        early = Signals.empty(len(bars))
        for name, values in honest.fields().items():
            getattr(early, name)[:-1] = values[1:]
        return early


def _on_the_pivot_bar(confirmed):
    """Swing levels written on the pivot bar itself, ``right`` bars before they can be known."""
    def peeking(values, left, right):
        known = confirmed(values, left, right)
        moved = np.full(len(known), np.nan)
        if len(known) > right:
            moved[:-right] = known[right:]
        return moved
    return peeking


class _SwingPeeker:
    """A structure strategy that uses each swing point from its pivot bar on, not from its confirming bar."""

    module = None

    @classmethod
    def generate(cls, bars, params):
        module = cls.module
        honest = module.confirmed_swing_high, module.confirmed_swing_low
        module.confirmed_swing_high, module.confirmed_swing_low = map(_on_the_pivot_bar, honest)
        try:
            return module.generate(bars, params)
        finally:
            module.confirmed_swing_high, module.confirmed_swing_low = honest


class PivotOrderBlock(_SwingPeeker):
    module = order_block
    DEFAULTS = order_block.DEFAULTS


class PivotBreakOfStructure(_SwingPeeker):
    module = break_of_structure
    DEFAULTS = break_of_structure.DEFAULTS


class SignalDropper:
    """Look-ahead used only to drop signals: ``liquidity_sweep`` entries that the next bar goes against.

    An entry is kept when the next bar closes in its favour, and on the last
    bar of the data, where there is no next bar. Every signal this strategy
    gives on the full data is therefore also given when the data stops on that
    bar: cutting after signal bars alone can never show the cheat. Only a cut
    after one of the bars whose signal was dropped does.
    """

    module = liquidity_sweep
    DEFAULTS = module.DEFAULTS

    @classmethod
    def generate(cls, bars, params):
        signals = cls.module.generate(bars, params)
        next_close = np.append(bars.close[1:], np.nan)
        against = ((signals.side == 1) & (next_close < bars.close)) | ((signals.side == -1) & (next_close > bars.close))
        signals.side[against] = 0
        signals.stop[against] = np.nan
        signals.target_r[against] = np.nan
        return signals
