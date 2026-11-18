"""The example strategies do exactly what their docstrings say, on hand-made bars."""

import numpy as np
import pytest

from conftest import make_bars
from perpmath.indicators import atr, prior_high, prior_low, sma
from perpmath.strategies import STRATEGIES, resolve_params


def mirror(rows, around=30):
    """Flip prices upside down, turning a bullish example into a bearish one."""
    return [(around - o, around - l, around - h, around - c) for o, h, l, c in rows]


def run(name, rows, **overrides):
    return STRATEGIES[name].generate(make_bars(rows), resolve_params(name, overrides))


# ------------------------------------------------------------------ indicators

def test_indicators_by_hand():
    nan = np.nan
    np.testing.assert_array_equal(sma(np.array([1.0, 2, 3, 4]), 2), [nan, 1.5, 2.5, 3.5])
    np.testing.assert_array_equal(prior_high(np.array([1.0, 3, 2, 5, 4]), 2), [nan, nan, 3, 3, 5])
    np.testing.assert_array_equal(prior_low(np.array([4.0, 2, 3, 1, 5]), 2), [nan, nan, 2, 2, 1])
    # true ranges: 2 (first bar: high - low), then max(1, |12 - 10|, |11 - 10|) = 2, then max(1, 3, 4) = 4
    high, low, close = np.array([11.0, 12, 8]), np.array([9.0, 11, 7]), np.array([10.0, 11, 7])
    np.testing.assert_array_equal(atr(high, low, close, 2), [nan, 2.0, 3.0])


# ------------------------------------------------------------------------ fvg

FVG = dict(atr_period=1, min_gap_atr=0.5, stop_buffer_atr=0.1, r_multiple=2.0, max_wait_bars=96)
BULLISH_GAP = [(10, 11, 9, 10),        # bar 1 of the pattern: high 11
               (10, 14, 10, 14),       # bar 2: the impulse
               (14, 15, 13, 14.5)]     # bar 3: low 13 > 11, so the zone is 11..13; ATR(1) = 2


def test_fvg_long_on_first_return_into_the_gap():
    rows = BULLISH_GAP + [(14.5, 15, 13.5, 14),      # stays above the zone
                          (14, 14.2, 12.5, 13.2),    # low reaches 13, closes above 11: signal
                          (13.2, 13.5, 12, 12.4)]    # in the zone again: gap already used
    signals = run("fvg", rows, **FVG)
    assert signals.side.tolist() == [0, 0, 0, 0, 1, 0]
    assert signals.stop[4] == pytest.approx(11 - 0.1 * 2)      # zone bottom minus 0.1 x ATR
    assert signals.target_r[4] == 2.0


def test_fvg_short_is_the_mirror_image():
    rows = mirror(BULLISH_GAP + [(14.5, 15, 13.5, 14), (14, 14.2, 12.5, 13.2), (13.2, 13.5, 12, 12.4)])
    signals = run("fvg", rows, **FVG)
    assert signals.side.tolist() == [0, 0, 0, 0, -1, 0]
    assert signals.stop[4] == pytest.approx(19 + 0.1 * 2)      # zone top plus 0.1 x ATR


def test_fvg_gap_fails_when_the_return_bar_closes_through_it():
    rows = BULLISH_GAP + [(14.5, 15, 13.5, 14),
                          (14, 14.2, 10.5, 10.6),    # closes below 11: gap dropped
                          (10.6, 12.5, 10.5, 12)]    # back in the old zone: nothing
    assert not run("fvg", rows, **FVG).side.any()


def test_fvg_gap_expires():
    rows = BULLISH_GAP + [(14.5, 15, 13.5, 14), (14, 14.2, 12.5, 13.2)]
    assert run("fvg", rows, **{**FVG, "max_wait_bars": 2}).side[4] == 1
    assert not run("fvg", rows, **{**FVG, "max_wait_bars": 1}).side.any()


def test_fvg_ignores_gaps_smaller_than_the_filter():
    rows = BULLISH_GAP + [(14.5, 15, 13.5, 14), (14, 14.2, 12.5, 13.2)]
    assert not run("fvg", rows, **{**FVG, "min_gap_atr": 1.5}).side.any()   # needs 3, gap is 2


# ------------------------------------------------------------ liquidity sweep

SWEEP = dict(lookback=3, atr_period=1, stop_buffer_atr=0.1, r_multiple=2.0)
RANGE = [(10, 10.5, 9.5, 10), (10, 11, 9.8, 10.8), (10.8, 12, 10.5, 11.5)]   # prior high 12, prior low 9.5


def test_sweep_short_when_high_is_taken_and_close_is_back_inside():
    rows = RANGE + [(11.5, 12.5, 11.2, 11.8),     # high 12.5 > 12, close 11.8 < 12: sweep
                    (11.8, 13, 11.7, 12.9)]       # closes above the prior high: breakout, no fade
    signals = run("liquidity_sweep", rows, **SWEEP)
    assert signals.side.tolist() == [0, 0, 0, -1, 0]
    assert signals.stop[3] == pytest.approx(12.5 + 0.1 * 1.3)  # sweep high plus 0.1 x ATR (true range 1.3)
    assert signals.target_r[3] == 2.0


def test_sweep_long_is_the_mirror_image():
    rows = mirror(RANGE + [(11.5, 12.5, 11.2, 11.8), (11.8, 13, 11.7, 12.9)])
    signals = run("liquidity_sweep", rows, **SWEEP)
    assert signals.side.tolist() == [0, 0, 0, 1, 0]
    assert signals.stop[3] == pytest.approx(17.5 - 0.1 * 1.3)


def test_sweep_of_both_sides_gives_no_signal():
    rows = RANGE + [(11.5, 12.5, 9.0, 11.0)]
    assert not run("liquidity_sweep", rows, **SWEEP).side.any()


def test_sweep_does_not_count_the_current_bar_in_the_prior_range():
    rows = RANGE + [(11.5, 11.9, 11.2, 11.8)]     # high 11.9 is below the prior high of 12
    assert not run("liquidity_sweep", rows, **SWEEP).side.any()


# ------------------------------------------------------------------- ma cross

def test_ma_cross_signals_and_stops():
    closes = [10, 10, 10, 9, 8, 11, 14]
    opens = [10] + closes[:-1]
    rows = [(o, max(o, c), min(o, c), c) for o, c in zip(opens, closes)]
    # SMA(2): -, 10, 10, 9.5, 8.5, 9.5, 12.5
    # SMA(3): -, -, 10, 9.67, 9, 9.33, 11
    # bar 3: fast drops below slow -> short.  bar 5: fast rises above slow -> long.
    signals = run("ma_cross", rows, fast_period=2, slow_period=3, atr_period=1, stop_atr_mult=2.0)
    assert signals.side.tolist() == [0, 0, 0, -1, 0, 1, 0]
    assert signals.exit_long.tolist() == [False, False, False, True, False, False, False]
    assert signals.exit_short.tolist() == [False, False, False, False, False, True, False]
    assert signals.stop[3] == pytest.approx(9 + 2 * 1)         # close + 2 x ATR (true range 1)
    assert signals.stop[5] == pytest.approx(11 - 2 * 3)        # close - 2 x ATR (true range 3)
    assert np.isnan(signals.target_r).all()                    # no profit target


def test_ma_cross_rejects_fast_not_below_slow():
    with pytest.raises(ValueError):
        run("ma_cross", [(1, 1, 1, 1)] * 5, fast_period=50, slow_period=20)


# -------------------------------------------------------------------- params

def test_unknown_or_invalid_parameters_are_rejected():
    with pytest.raises(ValueError, match="unknown parameter"):
        resolve_params("fvg", {"r_multiplee": 3})
    with pytest.raises(ValueError, match="positive"):
        resolve_params("fvg", {"r_multiple": 0})
    with pytest.raises(ValueError, match="whole number"):
        resolve_params("liquidity_sweep", {"lookback": 20.5})
    with pytest.raises(ValueError, match="unknown strategy"):
        resolve_params("martingale", {})
