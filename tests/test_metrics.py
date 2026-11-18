"""Metrics on a tiny example small enough to check with pencil and paper."""

from types import SimpleNamespace

import pytest

from conftest import make_bars
from perpmath.engine import ExecutionConfig, Signals, simulate
from perpmath.metrics import compute_metrics, max_drawdown_pct, t_statistic


def trade(return_pct, r, pnl=None, fees=1.0, bars_held=4, exit_reason="stop"):
    return SimpleNamespace(return_pct=return_pct, r_multiple=r, pnl=return_pct if pnl is None else pnl,
                           fees=fees, bars_held=bars_held, exit_reason=exit_reason)


def test_hand_computed_example():
    # Four trades with net returns of +2%, -1%, +1%, -1% and R of +2, -1, +1, -1.
    trades = [trade(2.0, 2.0, pnl=20, exit_reason="target"), trade(-1.0, -1.0, pnl=-10),
              trade(1.0, 1.0, pnl=10, exit_reason="target"), trade(-1.0, -1.0, pnl=-10)]
    equity = [100, 120, 110, 120, 90, 110]
    m = compute_metrics(trades, equity, initial_capital=100)

    assert m["trades"] == 4 and m["t_stat_n"] == 4
    assert (m["wins"], m["losses"]) == (2, 2)
    assert m["win_rate_pct"] == pytest.approx(50.0)
    assert m["avg_r"] == pytest.approx(0.25)                   # (2 - 1 + 1 - 1) / 4
    assert m["avg_trade_return_pct"] == pytest.approx(0.25)
    # deviations from the mean 0.25: 1.75, -1.25, 0.75, -1.25
    # squares: 3.0625 + 1.5625 + 0.5625 + 1.5625 = 6.75; variance = 6.75 / 3 = 2.25; sd = 1.5
    # t = 0.25 / (1.5 / sqrt(4)) = 1/3
    assert m["t_stat"] == pytest.approx(1 / 3)
    assert m["total_return_pct"] == pytest.approx(10.0)        # 100 -> 110
    assert m["max_drawdown_pct"] == pytest.approx(25.0)        # 120 -> 90
    assert m["profit_factor"] == pytest.approx(1.5)            # 30 won / 20 lost
    assert m["total_fees"] == pytest.approx(4.0)
    assert m["avg_bars_held"] == pytest.approx(4.0)
    assert m["exit_reasons"] == {"stop": 2, "target": 2}
    assert m["insufficient_sample"] is True


def test_insufficient_sample_flag_flips_at_100_trades():
    equity = [100.0]
    assert compute_metrics([trade(1.0, 1.0)] * 99, equity, 100)["insufficient_sample"] is True
    assert compute_metrics([trade(1.0, 1.0)] * 100, equity, 100)["insufficient_sample"] is False


def test_no_trades_gives_none_not_zero():
    m = compute_metrics([], [100.0, 100.0], 100)
    assert m["trades"] == 0 and m["insufficient_sample"] is True
    assert m["win_rate_pct"] is None and m["avg_r"] is None and m["t_stat"] is None
    assert m["profit_factor"] is None
    assert m["total_return_pct"] == 0.0 and m["max_drawdown_pct"] == 0.0


def test_t_statistic_edge_cases():
    assert t_statistic([]) is None
    assert t_statistic([1.0]) is None              # one trade: no spread to measure
    assert t_statistic([1.0, 1.0, 1.0]) is None    # no variation
    assert t_statistic([1.0, 3.0]) == pytest.approx(2.0)       # mean 2, sd sqrt(2), n 2
    assert t_statistic([-1.0, -3.0]) == pytest.approx(-2.0)


def test_drawdown_counts_a_fall_from_the_starting_capital():
    assert max_drawdown_pct([90, 95, 80], initial_capital=100) == pytest.approx(20.0)
    assert max_drawdown_pct([110, 120, 130], initial_capital=100) == 0.0


def test_drawdown_sees_open_position_losses_between_trades():
    # Long from 100. Price dips to 80 at one close, then recovers to 100.
    bars = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 79, 80),
                      (80, 100, 80, 100), (100, 100, 100, 100)])
    signals = Signals.empty(5)
    signals.side[0], signals.stop[0] = 1, 50.0
    result = simulate(bars, signals, ExecutionConfig(fee_rate=0, slippage=0, initial_capital=1000))
    m = compute_metrics(result.trades, result.equity, 1000)
    assert m["total_return_pct"] == pytest.approx(0.0)
    assert m["max_drawdown_pct"] == pytest.approx(20.0)        # invisible in the trade list
