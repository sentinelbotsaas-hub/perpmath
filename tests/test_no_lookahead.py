"""No look-ahead: decisions use closed bars only, fills happen at the next open."""

import numpy as np
import pytest

from conftest import make_bars, random_walk
from perpmath import backtest as bt
from perpmath.backtest import LookaheadError, check_no_lookahead, normalize_config, run_backtest
from perpmath.engine import ExecutionConfig, Signals, simulate
from perpmath.strategies import STRATEGIES, resolve_params


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


@pytest.mark.parametrize("name", ["fvg", "liquidity_sweep", "ma_cross"])
def test_example_strategies_pass_the_check(name, walk):
    params = resolve_params(name, {})
    signals = STRATEGIES[name].generate(walk, params)
    assert np.count_nonzero(signals.side) > 20, "the check would be empty without signals"
    assert check_no_lookahead(STRATEGIES[name].generate, walk, params) == 9


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


@pytest.mark.parametrize("name", ["fvg", "liquidity_sweep", "ma_cross"])
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
