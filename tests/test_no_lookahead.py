"""No look-ahead: decisions use closed bars only, fills happen at the next open."""

import numpy as np
import pytest

from conftest import make_bars, random_walk
from perpmath import backtest as bt
from perpmath.backtest import LookaheadError, check_no_lookahead, normalize_config, run_backtest
from perpmath.engine import ExecutionConfig, Signals, simulate
from perpmath.indicators import carry_forward, confirmed_swing_high, confirmed_swing_low
from perpmath.strategies import STRATEGIES, resolve_params
from perpmath.strategies.order_block import OrderBlock, find_order_blocks

EXAMPLES = ["fvg", "liquidity_sweep", "ma_cross", "order_block", "break_of_structure", "breaker_block"]


def test_every_shipped_strategy_is_in_the_lookahead_tests():
    assert sorted(EXAMPLES) == sorted(STRATEGIES)


class Peeker:
    """Cheats: goes long on bar i when bar i + 1 closes higher."""

    DEFAULTS = {"stop_pct": 10.0}

    @staticmethod
    def generate(bars, params):
        signals = Signals.empty(len(bars))
        next_close = np.roll(bars.close, -1)
        next_close[-1] = bars.close[-1]
        up = next_close > bars.close
        signals.side[up] = 1
        signals.stop[up] = bars.close[up] * (1 - params["stop_pct"] / 100)
        signals.exit_long[:] = ~up
        return signals


def test_peeking_strategy_fails_the_check(walk):
    with pytest.raises(LookaheadError, match="future data"):
        check_no_lookahead(Peeker.generate, walk, Peeker.DEFAULTS)


def test_known_limit_data_smuggled_in_from_outside_is_not_detected(walk):
    """The check only controls the bars it hands to the strategy.

    A strategy that reads future prices from somewhere else (here a closure
    over the full series) gives the same answers on shortened copies, so the
    check passes. This is documented in the README as a limit of the check.
    """
    full_length = len(walk)

    def generate(bars, params):
        signals = Signals.empty(len(bars))
        for i in range(len(bars)):
            if walk.close[min(i + 1, full_length - 1)] > bars.close[i]:
                signals.side[i], signals.stop[i] = 1, bars.close[i] * 0.9
        return signals

    assert check_no_lookahead(generate, walk, {}) > 0


def test_run_backtest_refuses_a_peeking_strategy(walk, monkeypatch):
    monkeypatch.setitem(STRATEGIES, "peeker", Peeker)
    config = normalize_config({}, "peeker")
    with pytest.raises(LookaheadError):
        run_backtest("peeker", walk, config, "TEST", "15m")


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_strategies_pass_the_check(name, walk):
    params = resolve_params(name, {})
    signals = STRATEGIES[name].generate(walk, params)
    assert np.count_nonzero(signals.side) > 20, "the check would be empty without signals"
    assert check_no_lookahead(STRATEGIES[name].generate, walk, params) == 9


@pytest.mark.parametrize("name", ["order_block", "break_of_structure", "breaker_block"])
def test_structure_strategies_decide_every_bar_the_same_with_or_without_later_bars(name):
    """Stricter than the engine's check: every possible cut, not nine of them.

    These three wait for swing points and zones to be confirmed by later bars,
    which is where a peek hides most easily. The decision on bar ``k - 1`` when
    the data stops there must equal the decision on the same bar with the
    whole series present.
    """
    bars = random_walk(700, seed=5)
    params = resolve_params(name, {})
    generate = STRATEGIES[name].generate
    full = generate(bars, params).fields()
    assert np.count_nonzero(full["side"]) >= 5, "the comparison would be empty without signals"
    for k in range(1, len(bars)):
        part = generate(bars.head(k), params).fields()
        for field, values in part.items():
            np.testing.assert_array_equal(values, full[field][:k], err_msg=f"{field} with {k} bars")


# Swing points: a swing at bar p needs `right` later bars, so it exists from bar p + right, not from bar p.

def test_swing_point_does_not_exist_until_its_right_side_is_complete():
    #                 0  1  2  3  4  5
    highs = np.array([1.0, 2, 5, 3, 2, 1])      # index 2 is a swing high with 2 bars either side
    for bars_seen in (3, 4):                    # the pivot bar itself, then one bar to its right
        assert np.isnan(confirmed_swing_high(highs[:bars_seen], 2, 2)).all()
        assert np.isnan(confirmed_swing_low(-highs[:bars_seen], 2, 2)).all()
    confirmed = confirmed_swing_high(highs[:5], 2, 2)             # two bars to its right
    assert np.isnan(confirmed[:4]).all() and confirmed[4] == 5    # reported on the confirming bar, not on bar 2
    assert np.isnan(carry_forward(confirmed_swing_high(highs, 2, 2))[:4]).all()
    # A later bar can still undo a pivot that looked good after one bar: then it never appears.
    assert np.isnan(confirmed_swing_high(np.array([1.0, 2, 5, 3, 6, 1]), 2, 2)).all()


BOS = dict(swing_left=2, swing_right=2, atr_period=1, stop_buffer_atr=0.1, r_multiple=2.0)
#            open  high  low   close
STRUCTURE = [(10, 10.5, 9.5, 10),
             (10, 10.2, 9, 9.2),
             (9.2, 9.5, 8, 9),         # bar 2: swing low 8, confirmed on bar 4
             (9, 11, 8.8, 10.8),
             (10.8, 12.5, 10.5, 12),
             (12, 14, 11.8, 13),       # bar 5: swing high 14, confirmed on bar 7
             (13, 13.5, 12, 12.2),
             (12.2, 13, 11.5, 12.5)]   # bar 7: low 11.5, the lowest for two bars back. A swing low only if
                                       #        the lows of bars 8 AND 9 stay above it.


def _bos(rows):
    return STRATEGIES["break_of_structure"].generate(make_bars(rows), resolve_params("break_of_structure", BOS))


def test_bos_stop_ignores_a_swing_low_whose_right_side_is_not_complete():
    break_bar = (12.5, 14.5, 12.4, 14.4)    # bar 8: closes above 14. Only ONE bar to the right of bar 7 so far.
    becomes_a_swing = STRUCTURE + [break_bar,
                                   (14.4, 15, 14, 14.8),         # bar 9: low 14 > 11.5: bar 7 is now confirmed
                                   (14.8, 16, 14.5, 15.5),       # bar 10: swing high 16, confirmed on bar 12
                                   (15.5, 15.8, 15, 15.2),
                                   (15.2, 15.6, 14.9, 15.4),
                                   (15.4, 16.5, 15.3, 16.2)]     # bar 13: closes above 16
    never_a_swing = STRUCTURE + [break_bar, (14.4, 14.5, 11, 11.2)]   # bar 9 trades below 11.5
    ends_here = STRUCTURE + [break_bar]

    older_swing_low = 8 - 0.1 * 2.1         # true range of bar 8 is 2.1
    peeked = 11.5 - 0.1 * 2.1               # what a strategy that already "knew" bar 7 would hold would use
    for rows in (becomes_a_swing, never_a_swing, ends_here):
        signals = _bos(rows)
        assert signals.side[8] == 1
        assert signals.stop[8] == pytest.approx(older_swing_low)
        assert signals.stop[8] != pytest.approx(peeked)

    # Once bars 8 and 9 have closed above it, bar 7 is a swing low and the next break uses it.
    signals = _bos(becomes_a_swing)
    assert signals.side.tolist() == [0] * 8 + [1, 0, 0, 0, 0, 1]
    assert signals.stop[13] == pytest.approx(11.5 - 0.1 * 1.2)


def test_bos_breaks_the_confirmed_high_while_a_newer_high_is_still_unconfirmed():
    rows = STRUCTURE + [(12.5, 15, 12.3, 13.5),      # bar 8: new high 15 on a wick, close below 14
                        (13.5, 14.8, 13.4, 14.5)]    # bar 9: closes above 14, the latest CONFIRMED swing high
    later = [(14.5, 14.9, 14.2, 14.7),               # bar 10: high below 15: bar 8 is confirmed as a swing high
             (14.7, 15.6, 14.6, 15.4)]               # bar 11: closes above 15
    # A strategy that treated bar 8 as a swing high before bar 10 closed would see nothing to break on bar 9.
    assert _bos(rows).side.tolist() == [0] * 9 + [1]
    signals = _bos(rows + later)
    assert signals.side.tolist() == [0] * 9 + [1, 0, 1]
    assert signals.stop[9] == _bos(rows).stop[9]


def test_order_blocks_appear_on_the_same_bar_with_or_without_later_bars():
    """The zones themselves, not only the entries: an entry needs a return to the zone, which hides most peeks."""
    bars = random_walk(700, seed=5)
    params = resolve_params("order_block", {})
    full = find_order_blocks(bars, params)
    assert sum(block is not None for block in full) >= 5
    for k in range(1, len(bars)):
        assert find_order_blocks(bars.head(k), params) == full[:k], f"with {k} bars"


def test_order_block_forms_on_an_impulse_bar_that_later_turns_out_to_be_a_swing_high():
    params = resolve_params("order_block", dict(atr_period=1, swing_left=2, swing_right=2, impulse_bars=3,
                                                min_impulse_atr=1.0))
    #        open  high  low   close
    rows = [(10, 10.5, 9.5, 10.2),
            (10.2, 11, 10, 10.8),
            (10.8, 12, 10.6, 11.5),     # bar 2: swing high 12, confirmed on bar 4
            (11.5, 11.8, 11, 11.2),
            (11.2, 11.6, 10.4, 10.6),
            (10.6, 11, 10, 10.2),       # bar 5: the down bar before the move
            (10.2, 11.4, 10.1, 11.3),
            (11.3, 12.8, 11.2, 12.6),
            (12.6, 13, 12.2, 12.8),     # bar 8: third up bar, closes above 12: the block exists from here
            (12.8, 12.9, 10.8, 11.2),   # bars 9 and 10 stay below 13, so bar 8 becomes a swing high (13),
            (11.2, 11.5, 10.5, 10.9)]   # known on bar 10. Judged against 13, bar 8 broke nothing.
    expected = OrderBlock(direction=1, bottom=10.0, top=11.0, atr=1.0, candle=5, formed=8)
    for bars_seen in (9, 10, 11):
        assert find_order_blocks(make_bars(rows[:bars_seen]), params)[8] == expected
    signals = STRATEGIES["order_block"].generate(make_bars(rows), params)
    assert signals.side.tolist() == [0] * 9 + [1, 0]


def test_fill_is_the_next_bar_open_not_the_signal_bar_close():
    #                 open  high  low  close
    bars = make_bars([(100, 101, 99, 100),
                      (100, 101, 99, 100),
                      (100, 106, 99, 105),    # bar 2: decision made on this close (105)
                      (107, 108, 106, 107),   # bar 3: fill here, at the open (107)
                      (107, 109, 106, 108),
                      (108, 110, 107, 109)])
    signals = Signals.empty(len(bars))
    signals.side[2], signals.stop[2] = 1, 90.0
    result = simulate(bars, signals, ExecutionConfig(fee_rate=0, slippage=0))
    trade = result.trades[0]
    assert trade.entry_index == 3
    assert trade.entry_price == 107.0          # not 105, the close the strategy saw
    assert trade.entry_time_ms == bars.ts[3]
    assert result.equity[2] == 10_000.0        # nothing happened on the signal bar


def test_signal_on_the_last_bar_is_never_filled():
    bars = make_bars([(100, 101, 99, 100)] * 4)
    signals = Signals.empty(4)
    signals.side[3], signals.stop[3] = 1, 90.0
    assert simulate(bars, signals, ExecutionConfig()).trades == []


def test_exit_signal_also_waits_for_the_next_open():
    bars = make_bars([(100, 101, 99, 100),
                      (100, 101, 99, 100),
                      (100, 121, 99, 120),    # exit decided on this close (120)
                      (110, 111, 109, 110)])  # exit fills at this open (110)
    signals = Signals.empty(4)
    signals.side[0], signals.stop[0] = 1, 50.0
    signals.exit_long[2] = True
    trade = simulate(bars, signals, ExecutionConfig(fee_rate=0, slippage=0)).trades[0]
    assert (trade.exit_index, trade.exit_price, trade.exit_reason) == (3, 110.0, "signal")


@pytest.mark.parametrize("name", EXAMPLES)
def test_past_trades_do_not_change_when_future_bars_are_added(name):
    bars = random_walk(2000, seed=11)
    params = resolve_params(name, {})
    generate = STRATEGIES[name].generate
    config = ExecutionConfig()
    cut = 1200
    short = simulate(bars.head(cut), generate(bars.head(cut), params), config)
    full = simulate(bars, generate(bars, params), config)
    settled_short = [t for t in short.trades if t.exit_index < cut - 1]
    settled_full = [t for t in full.trades if t.exit_index < cut - 1]
    assert len(settled_short) > 5
    assert settled_short == settled_full
    assert np.array_equal(short.equity[:cut - 1], full.equity[:cut - 1])


def test_out_of_sample_starts_flat_and_only_uses_its_own_signals(walk):
    config = normalize_config({"split": {"in_sample_fraction": 0.5}}, "liquidity_sweep")
    document = run_backtest("liquidity_sweep", walk, config, "TEST", "15m")
    oos_start = document["split"]["oos_start"]
    assert document["out_of_sample"]["first_bar"] == oos_start
    assert document["in_sample"]["last_bar"] < oos_start
    assert all(t["entry_time"] > oos_start for t in document["out_of_sample"]["trades"])
    assert all(t["exit_time"] < oos_start for t in document["in_sample"]["trades"])
    assert document["timing"]["lookahead_check"] == {"passed": True, "shortened_runs": 9}
    assert bt.SCHEMA_VERSION == document["schema_version"] == 1
