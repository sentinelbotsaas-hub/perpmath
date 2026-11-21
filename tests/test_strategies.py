"""The example strategies do exactly what their docstrings say, on hand-made bars."""

import numpy as np
import pytest

from conftest import make_bars
from perpmath.indicators import (atr, carry_forward, confirmed_swing_high, confirmed_swing_low, prior_high,
                                 prior_low, sma)
from perpmath.strategies.order_block import OrderBlock, find_order_blocks
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


def test_swing_points_by_hand():
    nan = np.nan
    # index 2 (5) is above two bars either side; it is written at index 4, the bar that completes its right side.
    highs = np.array([1.0, 2, 5, 3, 2, 4, 1])
    np.testing.assert_array_equal(confirmed_swing_high(highs, 2, 2), [nan, nan, nan, nan, 5, nan, nan])
    np.testing.assert_array_equal(confirmed_swing_high(highs, 1, 1), [nan, nan, nan, 5, nan, nan, 4])
    np.testing.assert_array_equal(confirmed_swing_low(-highs, 2, 2), [nan, nan, nan, nan, -5, nan, nan])
    # Two equal highs side by side: a tie, so neither is a swing.
    assert np.isnan(confirmed_swing_high(np.array([1.0, 2, 5, 5, 2, 1, 0]), 2, 2)).all()
    np.testing.assert_array_equal(carry_forward(np.array([nan, 3, nan, nan, 7, nan])), [nan, 3, 3, 3, 7, 7])
    with pytest.raises(ValueError):
        confirmed_swing_high(highs, 0, 2)


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


# Rule 4, cases b and c: the bar trades through the prior high (12) AND the prior low (9.5) and closes
# outside the old range. Pinned as shipped: changing it would change the published results.
BOTH_SIDES = (11.5, 12.5, 9.0)              # open, high, low; true range 3.5, so the stop buffer is 0.35


@pytest.mark.parametrize("close, side, stop", [
    (12.2, 1, 9.0 - 0.35),      # closes above the prior high: counted as a low sweep, long, stop under the low
    (12.0, 1, 9.0 - 0.35),      # exactly on the prior high: the same
    (9.2, -1, 12.5 + 0.35),     # closes below the prior low: counted as a high sweep, short, stop over the high
    (9.5, -1, 12.5 + 0.35),     # exactly on the prior low: the same
])
def test_bar_through_both_sides_that_closes_outside_is_traded_as_a_sweep_of_the_far_side(close, side, stop):
    signals = run("liquidity_sweep", RANGE + [BOTH_SIDES + (close,)], **SWEEP)
    assert signals.side.tolist() == [0, 0, 0, side]
    assert signals.stop[3] == pytest.approx(stop)
    assert signals.target_r[3] == 2.0


@pytest.mark.parametrize("close", [9.6, 10.0, 11.9])
def test_bar_through_both_sides_that_closes_inside_gives_no_signal(close):
    assert not run("liquidity_sweep", RANGE + [BOTH_SIDES + (close,)], **SWEEP).side.any()


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


# --------------------------------------------------------- break of structure

BOS = dict(swing_left=2, swing_right=2, atr_period=1, stop_buffer_atr=0.1, r_multiple=2.0)
#            open  high  low   close
STRUCTURE = [(10, 10.5, 9.5, 10),
             (10, 10.2, 9, 9.2),
             (9.2, 9.5, 8, 9),         # bar 2: low 8, below the two lows either side: swing low
             (9, 11, 8.8, 10.8),
             (10.8, 12.5, 10.5, 12),   # bar 4: two bars after bar 2, the swing low at 8 is confirmed
             (12, 14, 11.8, 13),       # bar 5: high 14, above the two highs either side: swing high
             (13, 13.5, 12, 12.2),
             (12.2, 13, 11.5, 12.5)]   # bar 7: the swing high at 14 is confirmed. Low 11.5: a swing low
                                       #        if the next two lows stay above it


def test_bos_long_on_a_close_above_the_latest_swing_high():
    rows = STRUCTURE + [(12.5, 14.5, 12.4, 13.8),    # bar 8: wick above 14, close below it: no break
                        (13.8, 14.9, 13.6, 14.6),    # bar 9: closes above 14: long. Also confirms bar 7
                        (14.6, 15.2, 14.3, 15)]      # still above 14: that level has been used
    signals = run("break_of_structure", rows, **BOS)
    assert signals.side.tolist() == [0] * 9 + [1, 0]
    # Latest confirmed swing low is bar 7 (11.5), confirmed by this very close. True range of bar 9 is 1.3.
    assert signals.stop[9] == pytest.approx(11.5 - 0.1 * 1.3)
    assert signals.target_r[9] == 2.0
    assert np.isnan(signals.stop[:9]).all() and np.isnan(signals.target_r[:9]).all()


def test_bos_short_is_the_mirror_image():
    rows = mirror(STRUCTURE + [(12.5, 14.5, 12.4, 13.8), (13.8, 14.9, 13.6, 14.6), (14.6, 15.2, 14.3, 15)])
    signals = run("break_of_structure", rows, **BOS)
    assert signals.side.tolist() == [0] * 9 + [-1, 0]
    assert signals.stop[9] == pytest.approx(18.5 + 0.1 * 1.3)   # swing high plus 0.1 x ATR


def test_bos_a_high_taken_out_before_it_is_confirmed_is_not_a_swing():
    rows = STRUCTURE[:7] + [(12.2, 14.8, 12.1, 14.6),   # bar 7 trades above bar 5's high: bar 5 is no swing
                            (14.6, 15, 14.4, 14.9)]
    assert not run("break_of_structure", rows, **BOS).side.any()


def test_bos_needs_a_confirmed_swing_on_the_stop_side_and_uses_each_level_once():
    rows = [(10, 10.5, 9.5, 10),
            (10, 11, 9.8, 10.8),
            (10.8, 13, 10.6, 12),       # bar 2: swing high 13
            (12, 12.5, 11.5, 11.8),
            (11.8, 12.2, 10.2, 11.6),   # bar 4: confirms the swing high. Its low 10.2 is a swing low,
            (11.6, 13.5, 11.4, 13.4),   # bar 5: closes above 13, but no swing low is confirmed yet
            (13.4, 13.8, 13, 13.6)]     # bar 6: confirms the swing low at 10.2; the level 13 is already used
    assert not run("break_of_structure", rows, **BOS).side.any()


def test_bos_no_signal_when_the_latest_swing_low_is_above_the_close():
    rows = [(21, 22, 20.5, 21.5),
            (21.5, 22, 20.8, 21),
            (21, 21.5, 20, 21.2),       # bar 2: swing low 20, confirmed on bar 4
            (21.2, 23, 21, 22.5),
            (22.5, 23.5, 22, 22.2),     # bar 4: swing high 23.5, confirmed on bar 6
            (22.2, 22.5, 15, 15.5),     # bar 5: closes below 20, but the swing high is not confirmed yet
            (15.5, 16, 10, 10.5),
            (10.5, 10.8, 10, 10.6),     # two equal lows at 10: a tie, so neither bar is a swing low
            (10.6, 10.9, 10.3, 10.7),
            (10.7, 11.5, 10.5, 11.2),   # bar 9: swing high 11.5, confirmed on bar 11
            (11.2, 11.3, 10.8, 11),
            (11, 11.2, 10.7, 10.9),
            (10.9, 12, 10.85, 11.9)]    # bar 12: closes above 11.5; the latest swing low is still 20
    assert not run("break_of_structure", rows, **BOS).side.any()


# ---------------------------------------------------------------- order block

OB = dict(atr_period=1, swing_left=2, swing_right=2, impulse_bars=2, min_impulse_atr=1.0, max_wait_bars=96,
          stop_buffer_atr=0.1, r_multiple=2.0)
#           open  high  low   close
OB_SETUP = [(10, 10.5, 9.5, 10.2),
            (10.2, 11, 10, 10.8),
            (10.8, 12, 10.6, 11.5),     # bar 2: swing high 12
            (11.5, 11.8, 11, 11.2),
            (11.2, 11.6, 10.4, 10.6),   # bar 4: the swing high is confirmed
            (10.6, 11, 10, 10.2),       # bar 5: last down bar before the move: zone 10..11, ATR(1) = 1
            (10.2, 11.4, 10.1, 11.3),   # bar 6: up
            (11.3, 12.8, 11.2, 12.6)]   # bar 7: up, +2.4 from 10.2, closes above 12 and above 11: block formed
ABOVE = (12.6, 13, 12.2, 12.8)          # bar 8: stays above the zone
OB_RETURN = OB_SETUP + [ABOVE,
                        (12.8, 12.9, 10.8, 11.2),   # bar 9: low reaches 11, closes above 10: signal
                        (11.2, 11.5, 10.5, 10.9)]   # bar 10: in the zone again: block already used


def test_order_block_is_the_bar_before_the_impulse():
    formed = find_order_blocks(make_bars(OB_RETURN), resolve_params("order_block", OB))
    assert formed[7] == OrderBlock(direction=1, bottom=10.0, top=11.0, atr=1.0, candle=5, formed=7)
    assert [i for i, block in enumerate(formed) if block is not None] == [7]   # bar 8 extends the run: no repeat


def test_order_block_long_on_first_return_into_the_block():
    signals = run("order_block", OB_RETURN, **OB)
    assert signals.side.tolist() == [0] * 9 + [1, 0]
    assert signals.stop[9] == pytest.approx(10 - 0.1 * 1)       # zone bottom minus 0.1 x ATR
    assert signals.target_r[9] == 2.0


def test_order_block_short_is_the_mirror_image():
    signals = run("order_block", mirror(OB_RETURN), **OB)
    assert signals.side.tolist() == [0] * 9 + [-1, 0]
    assert signals.stop[9] == pytest.approx(20 + 0.1 * 1)       # zone top plus 0.1 x ATR


def test_order_block_fails_when_the_return_bar_closes_through_it():
    rows = OB_SETUP + [ABOVE,
                       (12.8, 12.9, 9.5, 9.8),      # closes below 10: block dropped
                       (9.8, 10.8, 9.7, 10.5)]      # back in the old zone: nothing
    assert not run("order_block", rows, **OB).side.any()


def test_order_block_expires():
    rows = OB_RETURN[:10]                           # formed on bar 7, return on bar 9
    assert run("order_block", rows, **{**OB, "max_wait_bars": 2}).side[9] == 1
    assert not run("order_block", rows, **{**OB, "max_wait_bars": 1}).side.any()


def test_order_block_needs_a_big_enough_impulse():
    assert not run("order_block", OB_RETURN, **{**OB, "min_impulse_atr": 3.0}).side.any()   # needs 3, move is 2.4
    assert not run("order_block", OB_RETURN, **{**OB, "impulse_bars": 4}).side.any()        # run is 3 bars long
    assert run("order_block", OB_RETURN, **{**OB, "impulse_bars": 3}).side[9] == 1          # qualifies on bar 8


def test_order_block_needs_a_close_beyond_the_swing_high():
    rows = OB_SETUP[:7] + [(11.3, 11.95, 11.2, 11.9),   # bar 7: +1.7 and above the block, but not above 12
                           (11.9, 12.0, 11.5, 11.6),
                           (11.6, 11.7, 10.8, 11.2)]    # back at 10..11: there is no block to return to
    assert not run("order_block", rows, **OB).side.any()


def test_order_block_impulse_must_close_beyond_the_block():
    def rows(block_high):
        return OB_SETUP[:5] + [(10.6, block_high, 10, 10.2),     # bar 5: the down bar
                               (10.2, 12.7, 10.1, 12.6),         # bar 6: one-bar impulse, closes above 12
                               (12.6, 12.9, 12.2, 12.8),
                               (12.8, 12.9, 10.8, 11.2)]         # bar 8: back to 11
    one_bar = {**OB, "impulse_bars": 1, "min_impulse_atr": 0.5}
    assert run("order_block", rows(11), **one_bar).side.tolist() == [0] * 8 + [1]
    assert not run("order_block", rows(13), **one_bar).side.any()   # 12.6 is still inside 10..13


def test_order_block_bar_before_the_impulse_must_close_against_it():
    rows = OB_SETUP[:5] + [(10.6, 11, 10, 10.6),        # bar 5 closes at its open: neither up nor down
                           (10.6, 11.4, 10.5, 11.3),
                           (11.3, 12.8, 11.2, 12.6),
                           ABOVE,
                           (12.8, 12.9, 10.8, 11.2)]
    assert not run("order_block", rows, **OB).side.any()


# -------------------------------------------------------------- breaker block

BREAK = (12.8, 12.9, 9.2, 9.4)          # bar 9: closes below the block's bottom (10); true range 3.7
BREAKER = OB_SETUP + [ABOVE, BREAK,
                      (9.4, 9.8, 9, 9.6),           # bar 10: high 9.8, has not come back to 10
                      (9.6, 10.5, 9.5, 10.3),       # bar 11: high reaches 10, closes below 11: signal
                      (10.3, 10.8, 10, 10.6)]       # bar 12: in the zone again: breaker already used


def test_breaker_short_on_first_retest_of_a_failed_bullish_block():
    signals = run("breaker_block", BREAKER, **OB)
    assert signals.side.tolist() == [0] * 11 + [-1, 0]
    assert signals.stop[11] == pytest.approx(11 + 0.1 * 3.7)    # zone top plus 0.1 x ATR of the breaking bar
    assert signals.target_r[11] == 2.0
    assert not run("order_block", BREAKER, **OB).side.any()     # for order_block the block simply failed


def test_breaker_long_is_the_mirror_image():
    signals = run("breaker_block", mirror(BREAKER), **OB)
    assert signals.side.tolist() == [0] * 11 + [1, 0]
    assert signals.stop[11] == pytest.approx(19 - 0.1 * 3.7)    # zone bottom minus 0.1 x ATR


def test_breaker_needs_a_close_through_the_block_not_a_wick():
    rows = OB_SETUP + [ABOVE,
                       (12.8, 12.9, 9.2, 10.4),     # low 9.2 is below the block, the close is not
                       (10.4, 10.9, 10.1, 10.6),
                       (10.6, 10.9, 10.2, 10.5)]
    assert not run("breaker_block", rows, **OB).side.any()


def test_breaker_fails_when_the_retest_bar_closes_back_through_it():
    rows = OB_SETUP + [ABOVE, BREAK,
                       (9.4, 9.8, 9, 9.6),
                       (9.6, 11.6, 9.5, 11.4),      # closes above 11: breaker dropped
                       (11.4, 11.5, 10.5, 10.8)]    # back in the old zone: nothing
    assert not run("breaker_block", rows, **OB).side.any()


def test_breaker_does_not_care_whether_the_block_was_touched_first():
    rows = OB_SETUP + [(12.6, 13, 10.5, 12.4),      # bar 8: dips into the block and holds (order_block buys)
                       (12.4, 12.5, 9.2, 9.4),      # bar 9: closes through it; true range 3.3
                       (9.4, 9.8, 9, 9.6),
                       (9.6, 10.5, 9.5, 10.3)]      # bar 11: retest
    signals = run("breaker_block", rows, **OB)
    assert signals.side.tolist() == [0] * 11 + [-1]
    assert signals.stop[11] == pytest.approx(11 + 0.1 * 3.3)
    assert run("order_block", rows, **OB).side.tolist() == [0] * 8 + [1, 0, 0, 0]


def test_breaker_both_clocks_expire():
    # Clock 1: the block (formed on bar 7) must fail within max_wait_bars. It fails on bar 9.
    assert not run("breaker_block", BREAKER, **{**OB, "max_wait_bars": 1}).side.any()
    # Clock 2: the breaker (bar 9) must be retested within max_wait_bars. Here the retest is on bar 12.
    rows = OB_SETUP + [ABOVE, BREAK, (9.4, 9.8, 9, 9.6), (9.6, 9.9, 9.3, 9.5), (9.5, 10.5, 9.4, 10.3)]
    assert run("breaker_block", rows, **{**OB, "max_wait_bars": 3}).side.tolist() == [0] * 12 + [-1]
    assert not run("breaker_block", rows, **{**OB, "max_wait_bars": 2}).side.any()


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
