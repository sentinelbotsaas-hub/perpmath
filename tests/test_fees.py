"""Fee, slippage and fill arithmetic, checked against numbers worked out by hand."""

import pytest

from conftest import make_bars
from perpmath.engine import ExecutionConfig, Signals, simulate

FLAT = (100, 100, 100, 100)


def one_trade(bars, side, stop, target_r=None, exit_bar=None, **config):
    signals = Signals.empty(len(bars))
    signals.side[0], signals.stop[0] = side, stop
    if target_r is not None:
        signals.target_r[0] = target_r
    if exit_bar is not None:
        (signals.exit_long if side == 1 else signals.exit_short)[exit_bar] = True
    result = simulate(bars, signals, ExecutionConfig(**config))
    assert len(result.trades) == 1
    return result.trades[0], result


def test_long_with_fee_and_slippage():
    # Enter at the open of bar 1 (100), exit at the open of bar 2 (110).
    bars = make_bars([FLAT, (100, 105, 99, 104), (110, 111, 109, 110), (110, 110, 110, 110)])
    trade, result = one_trade(bars, 1, 90.0, exit_bar=1,
                              fee_rate=0.001, slippage=0.01, initial_capital=1000)
    # entry fill  = 100 * 1.01          = 101
    # quantity    = 1000 / 101
    # entry fee   = 1000 * 0.001        = 1
    # exit fill   = 110 * 0.99          = 108.9
    # gross       = (108.9 - 101) * 1000 / 101 = 7900 / 101
    # exit fee    = 108.9 * 0.001 * 1000 / 101 = 108.9 / 101
    # net         = (7900 - 101 - 108.9) / 101 = 7690.1 / 101
    assert trade.entry_price == pytest.approx(101.0)
    assert trade.exit_price == pytest.approx(108.9)
    assert trade.quantity == pytest.approx(1000 / 101)
    assert trade.fees == pytest.approx(1 + 108.9 / 101)
    assert trade.pnl == pytest.approx(7690.1 / 101)
    assert trade.return_pct == pytest.approx(7.6139604, abs=1e-6)
    # risk per unit = 101 - 90 = 11, so R = net / (quantity * 11) = 7690.1 / 11000
    assert trade.r_multiple == pytest.approx(0.6991, abs=1e-4)
    assert result.equity[-1] == pytest.approx(1000 + 7690.1 / 101)


def test_short_with_fee_and_slippage():
    # Enter short at the open of bar 1 (100), exit at the open of bar 2 (90).
    bars = make_bars([FLAT, (100, 101, 95, 96), (90, 91, 89, 90), (90, 90, 90, 90)])
    trade, _ = one_trade(bars, -1, 110.0, exit_bar=1,
                         fee_rate=0.001, slippage=0.01, initial_capital=1000)
    # entry fill = 100 * 0.99 = 99 (sold lower); exit fill = 90 * 1.01 = 90.9 (bought higher)
    # gross = (99 - 90.9) * 1000 / 99 = 8100 / 99; fees = 1 + 90.9 / 99
    # net   = (8100 - 99 - 90.9) / 99 = 7910.1 / 99 = 79.9
    assert trade.entry_price == pytest.approx(99.0)
    assert trade.exit_price == pytest.approx(90.9)
    assert trade.pnl == pytest.approx(79.9)
    assert trade.side == "short"


def test_round_trip_at_the_same_price_costs_two_fees():
    bars = make_bars([FLAT, FLAT, FLAT, FLAT])
    trade, result = one_trade(bars, 1, 90.0, exit_bar=1, fee_rate=0.00055, slippage=0)
    assert trade.return_pct == pytest.approx(-0.11)            # 2 x 0.055%
    assert trade.fees == pytest.approx(11.0)                   # on 10,000 notional
    assert result.equity[-1] == pytest.approx(10_000 - 11.0)


def test_slippage_alone_always_hurts():
    bars = make_bars([FLAT, FLAT, FLAT, FLAT])
    for side, stop in ((1, 90.0), (-1, 110.0)):
        trade, _ = one_trade(bars, side, stop, exit_bar=1, fee_rate=0, slippage=0.001)
        assert trade.pnl < 0
        assert trade.return_pct == pytest.approx(-0.2, abs=1e-3)   # about 2 x 0.1%


def test_no_costs_means_raw_price_move():
    bars = make_bars([FLAT, (100, 105, 99, 104), (110, 111, 109, 110), (110, 110, 110, 110)])
    trade, _ = one_trade(bars, 1, 90.0, exit_bar=1, fee_rate=0, slippage=0)
    assert trade.return_pct == pytest.approx(10.0)
    assert trade.r_multiple == pytest.approx(1.0)              # +10 on a risk of 10


def test_stop_fills_at_the_stop_price():
    bars = make_bars([FLAT, FLAT, (99, 100, 94, 96), FLAT])
    trade, _ = one_trade(bars, 1, 95.0, fee_rate=0, slippage=0)
    assert (trade.exit_reason, trade.exit_price, trade.exit_index) == ("stop", 95.0, 2)
    assert trade.r_multiple == pytest.approx(-1.0)


def test_stop_gapped_through_fills_at_the_open():
    bars = make_bars([FLAT, FLAT, (93, 94, 92, 93), FLAT])
    trade, _ = one_trade(bars, 1, 95.0, fee_rate=0, slippage=0)
    assert (trade.exit_reason, trade.exit_price) == ("stop", 93.0)
    assert trade.r_multiple == pytest.approx(-1.4)             # worse than -1R


def test_target_is_measured_from_the_actual_fill():
    # Risk = 100 - 95 = 5, target = 100 + 2 x 5 = 110.
    bars = make_bars([FLAT, FLAT, (101, 111, 100, 108), FLAT])
    trade, _ = one_trade(bars, 1, 95.0, target_r=2.0, fee_rate=0, slippage=0)
    assert (trade.exit_reason, trade.exit_price, trade.target_price) == ("target", 110.0, 110.0)
    assert trade.r_multiple == pytest.approx(2.0)


def test_stop_wins_when_stop_and_target_are_hit_in_the_same_bar():
    bars = make_bars([FLAT, FLAT, (100, 112, 94, 111), FLAT])
    trade, _ = one_trade(bars, 1, 95.0, target_r=2.0, fee_rate=0, slippage=0)
    assert trade.exit_reason == "stop"


def test_stop_can_be_hit_on_the_entry_bar():
    bars = make_bars([FLAT, (100, 101, 94, 95), FLAT])
    trade, _ = one_trade(bars, 1, 95.0, fee_rate=0, slippage=0)
    assert (trade.entry_index, trade.exit_index, trade.bars_held) == (1, 1, 0)


def test_short_stop_and_target():
    stop_bars = make_bars([FLAT, FLAT, (101, 106, 100, 104), FLAT])
    trade, _ = one_trade(stop_bars, -1, 105.0, target_r=2.0, fee_rate=0, slippage=0)
    assert (trade.exit_reason, trade.exit_price) == ("stop", 105.0)
    target_bars = make_bars([FLAT, FLAT, (99, 100, 89, 92), FLAT])
    trade, _ = one_trade(target_bars, -1, 105.0, target_r=2.0, fee_rate=0, slippage=0)
    assert (trade.exit_reason, trade.exit_price) == ("target", 90.0)


def test_entry_is_skipped_when_the_open_is_already_beyond_the_stop():
    bars = make_bars([FLAT, (94, 95, 93, 94), FLAT])
    signals = Signals.empty(3)
    signals.side[0], signals.stop[0] = 1, 95.0
    result = simulate(bars, signals, ExecutionConfig())
    assert result.trades == [] and result.signals_skipped_invalid_stop == 1


def test_signals_while_in_a_position_are_ignored_and_counted():
    bars = make_bars([FLAT] * 5)
    signals = Signals.empty(5)
    signals.side[[0, 1, 2]] = 1
    signals.stop[[0, 1, 2]] = 90.0
    result = simulate(bars, signals, ExecutionConfig())
    assert len(result.trades) == 1 and result.trades[0].exit_reason == "end"
    assert (result.signals_seen, result.signals_skipped_in_position) == (3, 2)


def test_max_hold_bars_closes_at_the_next_open():
    bars = make_bars([FLAT, FLAT, FLAT, (103, 104, 102, 103), FLAT])
    trade, _ = one_trade(bars, 1, 90.0, fee_rate=0, slippage=0, max_hold_bars=2)
    assert (trade.exit_reason, trade.exit_index, trade.exit_price) == ("time", 3, 103.0)


def test_equity_compounds_between_trades():
    bars = make_bars([FLAT, FLAT, (110, 110, 110, 110), (110, 110, 110, 110), (121, 121, 121, 121), (121, 121, 121, 121)])
    signals = Signals.empty(6)
    signals.side[[0, 2]] = 1
    signals.stop[[0, 2]] = 50.0
    signals.exit_long[[1, 3]] = True
    result = simulate(bars, signals, ExecutionConfig(fee_rate=0, slippage=0, initial_capital=1000))
    assert [round(t.pnl, 6) for t in result.trades] == [100.0, 110.0]   # second trade is sized on 1,100
    assert result.equity[-1] == pytest.approx(1210.0)


def test_entry_without_a_stop_is_an_error():
    bars = make_bars([FLAT] * 3)
    signals = Signals.empty(3)
    signals.side[0] = 1
    with pytest.raises(ValueError, match="without a stop"):
        simulate(bars, signals, ExecutionConfig())


@pytest.mark.parametrize("bad", [{"fee_rate": 0.055}, {"slippage": -0.1}, {"notional_fraction": 2.0},
                                 {"initial_capital": 0}, {"max_hold_bars": 0}])
def test_bad_execution_settings_are_rejected(bad):
    with pytest.raises(ValueError):
        ExecutionConfig(**bad).validate()
