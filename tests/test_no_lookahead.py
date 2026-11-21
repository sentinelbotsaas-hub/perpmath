"""No look-ahead: decisions use closed bars only, fills happen at the next open."""

import random

import numpy as np
import pytest

from conftest import make_bars, random_walk
from peekers import EarlyMaCross, PivotBreakOfStructure, PivotOrderBlock, SignalDropper
from perpmath import backtest as bt
from perpmath.backtest import (ConfigError, LookaheadError, check_lookahead_at_signals, check_no_lookahead,
                               decision_bars, normalize_config, run_backtest)
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
    report = check_lookahead_at_signals(generate, walk, {}, leading_bars=40, other_cuts=40)
    assert report["passed"] and report["signal_bars_checked"] == report["signal_bars"] > 1000


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
    # The check run_backtest uses. The series is shorter than the leading stretch, so every bar is cut.
    report = check_lookahead_at_signals(STRATEGIES[name].generate, walk, params)
    assert report["passed"] is True
    assert report["signal_bars_checked"] == report["signal_bars"] > 20
    assert report["other_bars_checked"] == report["other_bars"]
    assert report["shortened_runs"] == len(walk) - 1 and report["sample_seed"] is None


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_strategies_pass_the_check_when_it_samples(name):
    bars = random_walk(2400, seed=3)
    params = resolve_params(name, {})
    report = check_lookahead_at_signals(STRATEGIES[name].generate, bars, params,
                                        max_signal_cuts=8, leading_bars=150, other_cuts=60)
    assert report["passed"] is True and report["sample_seed"] == bt.LOOKAHEAD_SEED
    assert report["signal_bars_sampled"] is True and report["other_bars_sampled"] is True
    assert 8 <= report["signal_bars_checked"] < report["signal_bars"]
    assert report["leading_bars_all_checked"] == 150


# The weakness of the fixed-cut check, kept as a regression test, and what replaced it.
# Each cheat looks one to five bars ahead (see peekers.py).

@pytest.mark.parametrize("cheat", [EarlyMaCross, PivotOrderBlock])
def test_fixed_cut_check_lets_a_short_look_ahead_through(cheat, walk):
    assert check_no_lookahead(cheat.generate, walk, dict(cheat.DEFAULTS)) == 9      # nine cuts, no complaint


def test_fixed_cut_check_depends_on_where_the_cuts_happen_to_fall(walk):
    early, params = EarlyMaCross.generate, dict(EarlyMaCross.DEFAULTS)
    assert check_no_lookahead(early, walk, params, cuts=16) == 17
    with pytest.raises(LookaheadError):
        check_no_lookahead(early, walk, params, cuts=64)
    # The other way round for the swing peek in break_of_structure: caught with 9 cuts, missed with 17.
    pivot, params = PivotBreakOfStructure.generate, dict(PivotBreakOfStructure.DEFAULTS)
    with pytest.raises(LookaheadError):
        check_no_lookahead(pivot, walk, params)
    assert check_no_lookahead(pivot, walk, params, cuts=16) == 17


@pytest.mark.parametrize("cheat", [EarlyMaCross, PivotOrderBlock, PivotBreakOfStructure, SignalDropper])
@pytest.mark.parametrize("bars", [3000, 9000])      # shorter and longer than the leading stretch
def test_signal_cut_check_catches_it_with_default_settings(cheat, bars):
    series = random_walk(bars)
    with pytest.raises(LookaheadError, match="future data"):
        check_lookahead_at_signals(cheat.generate, series, dict(cheat.DEFAULTS))


@pytest.mark.parametrize("cheat", [EarlyMaCross, PivotOrderBlock, PivotBreakOfStructure, SignalDropper])
def test_run_backtest_refuses_the_short_look_ahead_cheats(cheat, walk, monkeypatch):
    monkeypatch.setitem(STRATEGIES, "cheat", cheat)
    with pytest.raises(LookaheadError, match="future data"):
        run_backtest("cheat", walk, normalize_config({}, "cheat"), "TEST", "15m")


@pytest.mark.parametrize("cheat", [EarlyMaCross, PivotOrderBlock, PivotBreakOfStructure, SignalDropper])
def test_signal_cut_check_catches_it_without_the_leading_stretch(cheat, walk):
    """The signal cuts and the sampled cuts on their own, as on the part of a long series after the first bars."""
    with pytest.raises(LookaheadError, match="future data"):
        check_lookahead_at_signals(cheat.generate, walk, dict(cheat.DEFAULTS), leading_bars=0)


@pytest.mark.parametrize("seed", range(8))
def test_a_decision_made_one_bar_early_fails_on_its_first_signal_bar(seed):
    """No sampling and no luck involved: the cuts after signal bars are enough, on any series."""
    bars = random_walk(1500, seed=seed)
    params = dict(EarlyMaCross.DEFAULTS)
    first = int(np.flatnonzero(decision_bars(EarlyMaCross.generate(bars, params).fields()))[0])
    with pytest.raises(LookaheadError, match=rf"at bar {first} \(.*\) changes when bars after bar {first} are removed"):
        check_lookahead_at_signals(EarlyMaCross.generate, bars, params, leading_bars=0, other_cuts=0)


@pytest.mark.parametrize("cheat", [PivotOrderBlock, SignalDropper])
def test_known_limit_look_ahead_that_only_removes_signals_survives_every_signal_cut(cheat, walk):
    """Why bars without a signal are cut as well, and why that part is only as good as its coverage.

    Both cheats use later bars to withhold entries. Every signal they still
    give is also given when the data stops on that bar (by construction for
    SignalDropper, as measured on this series for the swing peek), so no cut
    after a signal bar shows the cheat. A cut after a bar whose signal was
    withheld does.
    """
    params = dict(cheat.DEFAULTS)
    full = cheat.generate(walk, params).fields()
    signal_bars = np.flatnonzero(decision_bars(full)[:-1])
    assert len(signal_bars) > 20
    for i in signal_bars.tolist():
        bt._compare_shortened(cheat.generate, walk, params, full, i + 1)        # none of them raises
    with pytest.raises(LookaheadError):                                         # the other bars do it
        check_lookahead_at_signals(cheat.generate, walk, params, max_signal_cuts=0)


def test_cuts_that_are_sampled_are_the_same_on_every_run_and_keep_both_ends():
    candidates = np.arange(100, 1100)
    first = bt._spread(candidates, 50, random.Random(bt.LOOKAHEAD_SEED))
    again = bt._spread(candidates, 50, random.Random(bt.LOOKAHEAD_SEED))
    assert len(first) == 50 and first.tolist() == again.tolist()
    assert first[0] == 100 and first[-1] == 1099 and len(set(first.tolist())) == 50
    evenly = np.linspace(0, 999, 25).round().astype(int) + 100
    assert set(evenly.tolist()) <= set(first.tolist())                      # half evenly spaced, half drawn
    assert bt._spread(candidates, None, random.Random(1)) is candidates
    assert bt._spread(candidates, 5000, random.Random(1)) is candidates
    assert len(bt._spread(candidates, 0, random.Random(1))) == 0


def test_report_says_what_was_checked(walk):
    generate, params = STRATEGIES["liquidity_sweep"].generate, resolve_params("liquidity_sweep", {})
    report = check_lookahead_at_signals(generate, walk, params, max_signal_cuts=40, leading_bars=100, other_cuts=20)
    assert list(report) == ["passed", "method", "shortened_runs", "signal_bars", "signal_bars_checked",
                            "signal_bars_checked_pct", "signal_bars_sampled", "other_bars", "other_bars_checked",
                            "other_bars_checked_pct", "other_bars_sampled", "leading_bars_all_checked",
                            "sample_seed"]
    assert report == check_lookahead_at_signals(generate, walk, params, max_signal_cuts=40, leading_bars=100,
                                                other_cuts=20)
    decided = decision_bars(generate(walk, params).fields())[:-1]        # the last bar cannot be tested
    assert report["signal_bars"] == int(decided.sum()) and report["other_bars"] == int((~decided).sum())
    assert report["shortened_runs"] == report["signal_bars_checked"] + report["other_bars_checked"]
    in_stretch = int(decided[:100].sum())
    assert in_stretch + 40 - 5 <= report["signal_bars_checked"] <= in_stretch + 40 + 9      # overlap, fixed cuts
    assert 100 - in_stretch + 20 <= report["other_bars_checked"] <= 100 - in_stretch + 20 + 9
    assert report["signal_bars_checked_pct"] == round(report["signal_bars_checked"] / report["signal_bars"] * 100, 2)
    assert report["method"] == "signal_cuts" and report["sample_seed"] == bt.LOOKAHEAD_SEED
    with pytest.raises(ValueError, match="other_cuts"):
        check_lookahead_at_signals(generate, walk, params, other_cuts=-1)


def test_strategy_that_cannot_run_on_a_short_copy_stops_the_run(walk):
    def needs_500_bars(bars, params):
        if len(bars) < 500:
            raise ValueError("not enough history")
        return STRATEGIES["ma_cross"].generate(bars, params)

    with pytest.raises(LookaheadError, match="first 1 bars .ValueError: not enough history.*any prefix"):
        check_lookahead_at_signals(needs_500_bars, walk, resolve_params("ma_cross", {}))


def test_lookahead_mode_is_chosen_by_name(walk):
    config = normalize_config({}, "ma_cross")
    with pytest.raises(ConfigError, match="lookahead must be one of default, signals, every-bar"):
        run_backtest("ma_cross", walk, config, "TEST", "15m", lookahead="none")
    assert bt.LOOKAHEAD_MODES["default"] == (4000, 4000, 500)
    assert bt.LOOKAHEAD_MODES["signals"] == (None, 4000, 500)
    assert bt.LOOKAHEAD_MODES["every-bar"] == (None, None, None)
    bars = random_walk(4600)
    reports = {mode: run_backtest("ma_cross", bars, config, "TEST", "15m", lookahead=mode)["timing"]["lookahead_check"]
               for mode in bt.LOOKAHEAD_MODES}
    assert reports["default"] == reports["signals"]                    # far fewer than 4000 signal bars
    assert reports["default"]["other_bars_sampled"] and reports["default"]["leading_bars_all_checked"] == 4000
    assert reports["every-bar"]["shortened_runs"] == 4599 and reports["every-bar"]["sample_seed"] is None
    assert not reports["every-bar"]["other_bars_sampled"] and not reports["every-bar"]["signal_bars_sampled"]


@pytest.mark.parametrize("name", ["order_block", "break_of_structure", "breaker_block"])
def test_structure_strategies_decide_every_bar_the_same_with_or_without_later_bars(name):
    """Every possible cut, spelled out. The engine's check does this for the first bars of a long series only.

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
    check = document["timing"]["lookahead_check"]
    assert check["passed"] is True and check["method"] == "signal_cuts"
    assert check["shortened_runs"] == len(walk) - 1                   # 3000 bars: every bar but the last is cut
    assert check["signal_bars_checked"] == check["signal_bars"] > 0
    assert check["signal_bars_checked_pct"] == check["other_bars_checked_pct"] == 100.0
    assert bt.SCHEMA_VERSION == document["schema_version"] == 1     # keys were added, none changed meaning
