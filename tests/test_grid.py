"""Grid simulator: rules, costs, intrabar path, boundaries and liquidation, checked by hand."""

from dataclasses import replace

import numpy as np
import pytest

from conftest import STEP, make_bars, random_walk
from perpmath.funding import FundingRates
from perpmath.grid import GridConfig, buy_and_hold, grid_levels, simulate_grid

FLAT = (100, 100, 100, 100)

# Two cells, 90-100 and 100-110. Started at 100, a neutral grid has a long
# cell 0 (buy 90, sell 100) and a short cell 1 (sell 110, buy back 100).
BASE = GridConfig(lower=90, upper=110, grid_count=2, spacing="arithmetic", direction="neutral",
                  grid_fee_rate=0, taker_fee_rate=0, slippage=0, initial_capital=1000,
                  intrabar_path="low_first")
Q0 = 500 / 90       # cell notional 1000 / 2 = 500, long cell opens at its lower level 90
Q1 = 500 / 110      # short cell opens at its upper level 110


def run(rows, funding=None, **changes):
    return simulate_grid(make_bars(rows), replace(BASE, **changes), funding=funding, step_ms=STEP)


def kinds(result):
    return [(e[1], e[2], e[3]) for e in result.events]


# --------------------------------------------------------------------------- #
# Levels and start
# --------------------------------------------------------------------------- #

def test_arithmetic_levels_are_evenly_spaced_in_price():
    levels = grid_levels(100, 200, 4, "arithmetic")
    assert levels.tolist() == [100, 125, 150, 175, 200]


def test_geometric_levels_are_evenly_spaced_in_percent():
    levels = grid_levels(100, 400, 4, "geometric")
    assert levels[0] == 100 and levels[-1] == 400
    assert np.allclose(levels[1:] / levels[:-1], np.sqrt(2))
    assert np.allclose(levels, [100, 100 * 2 ** 0.5, 200, 200 * 2 ** 0.5, 400])


def test_neutral_grid_starts_flat():
    result = run([FLAT, FLAT, FLAT])
    assert result.events == [] and result.equity.tolist() == [1000, 1000, 1000]


def test_long_grid_buys_the_cells_above_the_start_price_at_market():
    result = run([FLAT, FLAT], direction="long", taker_fee_rate=0.001, slippage=0.01)
    index, kind, side, cell, level, price, qty, fee, pnl = result.events[0]
    # Cell 1 (100-110, midpoint 105 > 100) is bought at the open; cell 0 waits to buy at 90.
    assert (index, kind, side, cell) == (0, "initial", "buy", None)
    assert price == pytest.approx(101.0)                  # 100 plus 1% slippage
    assert qty == pytest.approx(500 / 100)                # a long cell sizes on its lower level, 100
    assert fee == pytest.approx(5 * 101 * 0.001)          # market order: taker fee
    assert [e[1] for e in result.events] == ["initial", "end"]          # end: closed at the last close
    assert result.market_fills == 2 and result.grid_fills == 0


def test_short_grid_sells_the_cells_below_the_start_price_at_market():
    result = run([FLAT, FLAT], direction="short")
    _, kind, side, _, _, price, qty, _, _ = result.events[0]
    # Cell 0 (90-100, midpoint 95 < 100) is sold short; a short cell sizes on its upper level, 100.
    assert (kind, side, price, qty) == ("initial", "sell", 100.0, pytest.approx(5.0))


# --------------------------------------------------------------------------- #
# Fills and costs
# --------------------------------------------------------------------------- #

DOWN_AND_BACK = [FLAT,
                 (100, 100, 89, 95),     # through 90: cell 0 buys
                 (95, 101, 95, 100),     # through 100: cell 0 sells, one round trip
                 FLAT]


def test_a_round_trip_without_costs_earns_one_grid_step():
    result = run(DOWN_AND_BACK)
    assert kinds(result) == [("grid", "buy", 0), ("grid", "sell", 0)]
    assert result.round_trips == 1
    assert result.equity[-1] == pytest.approx(1000 + Q0 * 10)
    assert result.events[1][8] == pytest.approx(Q0 * 10)          # realised on the sell


def test_grid_fills_pay_the_grid_fee_on_their_notional():
    result = run(DOWN_AND_BACK, grid_fee_rate=0.001)
    fees = Q0 * 90 * 0.001 + Q0 * 100 * 0.001
    assert result.grid_fees == pytest.approx(fees)
    assert result.equity[-1] == pytest.approx(1000 + Q0 * 10 - fees)


def test_slippage_moves_both_grid_fills_against_the_trader():
    result = run(DOWN_AND_BACK, slippage=0.001)
    buy, sell = result.events
    assert buy[5] == pytest.approx(90 * 1.001) and sell[5] == pytest.approx(100 * 0.999)
    assert result.equity[-1] == pytest.approx(1000 + Q0 * (99.9 - 90.09))


def test_maker_versus_taker_fee_on_the_same_fills():
    maker = run(DOWN_AND_BACK, grid_fee_rate=0.0002)
    taker = run(DOWN_AND_BACK, grid_fee_rate=0.00055)
    assert taker.grid_fees / maker.grid_fees == pytest.approx(0.00055 / 0.0002)
    assert maker.equity[-1] - taker.equity[-1] == pytest.approx(Q0 * 190 * 0.00035)


def test_touching_a_level_is_not_a_fill():
    result = run([FLAT, (100, 100, 90, 95), FLAT])           # low is exactly 90
    assert result.events == []


def test_a_gap_through_a_level_fills_at_the_level_not_at_the_better_open():
    result = run([FLAT, (85, 86, 84, 85), FLAT])
    _, kind, side, cell, level, price, _, _, _ = result.events[0]
    assert (kind, side, cell, level, price) == ("grid", "buy", 0, 90.0, 90.0)
    assert result.equity[1] == pytest.approx(1000 + Q0 * (85 - 90))


def test_end_of_sample_closes_the_rest_at_the_last_close_with_a_market_order():
    result = run([FLAT, (100, 100, 89, 95), (95, 96, 94, 95)], taker_fee_rate=0.001)
    end = result.events[-1]
    assert (end[1], end[2], end[5], end[6]) == ("end", "sell", 95.0, pytest.approx(Q0))
    assert end[7] == pytest.approx(Q0 * 95 * 0.001)
    assert result.equity[-1] == pytest.approx(1000 + Q0 * 5 - Q0 * 95 * 0.001)


def test_profit_and_loss_add_up():
    bars = random_walk(2000, seed=3)
    rates = FundingRates(bars.ts[::32] + STEP * 32, np.full(len(bars.ts[::32]), 0.0002))
    config = GridConfig(lower=float(bars.low.min()), upper=float(bars.high.max()), grid_count=30,
                        leverage=3)
    r = simulate_grid(bars, config, funding=rates, step_ms=STEP)
    assert r.round_trips > 50 and r.funding_events > 10
    explained = r.trading_pnl - r.grid_fees - r.market_fees - r.funding_paid - r.liquidation_loss
    assert r.equity[-1] - r.initial_capital == pytest.approx(explained)


def test_a_grid_that_never_crosses_a_level_has_no_fills():
    result = run([FLAT, (100, 104, 96, 101), (101, 103, 99, 100)])
    assert result.events == [] and result.round_trips == 0
    assert result.equity.tolist() == [1000, 1000, 1000]


# --------------------------------------------------------------------------- #
# Intrabar path
# --------------------------------------------------------------------------- #

WIDE_BAR = [FLAT, (100, 111, 89, 95), FLAT]


def test_the_intrabar_path_changes_the_result():
    # Bar: open 100, high 111, low 89, close 95.
    # Low first (100 -> 89 -> 111 -> 95): buy 90, sell 100 (round trip, cell 0),
    #   sell short 110, buy back 100 on the way to the close (round trip, cell 1). Flat.
    # High first (100 -> 111 -> 89 -> 95): sell short 110, buy back 100 (round trip,
    #   cell 1), buy 90. At the close (95) the long from 90 is up 5, not 10.
    low = run(WIDE_BAR, intrabar_path="low_first")
    high = run(WIDE_BAR, intrabar_path="high_first")
    assert kinds(low)[:4] == [("grid", "buy", 0), ("grid", "sell", 0), ("grid", "sell", 1), ("grid", "buy", 1)]
    assert kinds(high)[:3] == [("grid", "sell", 1), ("grid", "buy", 1), ("grid", "buy", 0)]
    assert (low.round_trips, high.round_trips) == (2, 1)
    assert low.equity[1] == pytest.approx(1000 + Q0 * 10 + Q1 * 10)
    assert high.equity[1] == pytest.approx(1000 + Q1 * 10 + Q0 * 5)
    assert low.equity[1] - high.equity[1] == pytest.approx(Q0 * 5)


def test_worst_of_two_keeps_the_path_with_lower_equity_and_best_of_two_the_other():
    low = run(WIDE_BAR, intrabar_path="low_first")
    high = run(WIDE_BAR, intrabar_path="high_first")
    worst = run(WIDE_BAR, intrabar_path="worst_of_two")
    best = run(WIDE_BAR, intrabar_path="best_of_two")
    assert worst.equity[1] == high.equity[1] < low.equity[1] == best.equity[1]
    assert kinds(worst) == kinds(high) and kinds(best) == kinds(low)


def test_on_a_random_walk_the_pessimistic_path_earns_less():
    bars = random_walk(3000, seed=5)
    config = GridConfig(lower=float(bars.low.min()), upper=float(bars.high.max()), grid_count=40)
    results = {p: simulate_grid(bars, replace(config, intrabar_path=p), step_ms=STEP)
               for p in ("worst_of_two", "low_first", "high_first", "best_of_two")}
    finals = {p: r.equity[-1] for p, r in results.items()}
    assert finals["worst_of_two"] < min(finals["low_first"], finals["high_first"])
    assert finals["best_of_two"] > max(finals["low_first"], finals["high_first"])
    assert results["worst_of_two"].round_trips < results["best_of_two"].round_trips


# --------------------------------------------------------------------------- #
# Leaving the range
# --------------------------------------------------------------------------- #

BREAK_DOWN_AND_RETURN = [FLAT,
                         (100, 100, 85, 88),     # through 90 (buy), then below the range
                         (88, 101, 88, 100),     # back up through 100
                         FLAT]


def test_out_of_range_continue_keeps_trading_when_price_returns():
    result = run(BREAK_DOWN_AND_RETURN, out_of_range="continue")
    assert kinds(result) == [("grid", "buy", 0), ("grid", "sell", 0)]
    assert result.range_exit_index is None


def test_out_of_range_close_closes_everything_at_the_bound():
    result = run(BREAK_DOWN_AND_RETURN, out_of_range="close", taker_fee_rate=0.001)
    assert kinds(result) == [("grid", "buy", 0), ("range_close", "sell", None)]
    close = result.events[1]
    assert close[5] == 90.0 and close[0] == 1 and close[7] == pytest.approx(Q0 * 90 * 0.001)
    assert result.range_exit_index == 1
    assert result.equity[-1] == pytest.approx(1000 - Q0 * 90 * 0.001)    # bought 90, sold 90


def test_out_of_range_stop_cancels_orders_but_keeps_the_position():
    result = run(BREAK_DOWN_AND_RETURN, out_of_range="stop")
    assert kinds(result) == [("grid", "buy", 0), ("range_stop", "none", None), ("end", "sell", None)]
    assert result.events[-1][5] == 100.0                              # held to the last close
    assert result.equity[-1] == pytest.approx(1000 + Q0 * 10)


def test_a_gap_past_the_bound_closes_at_the_open():
    result = run([FLAT, (80, 81, 79, 80), FLAT], out_of_range="close")
    close = result.events[1]
    assert (close[1], close[5]) == ("range_close", 80.0)
    assert result.equity[-1] == pytest.approx(1000 + Q0 * (80 - 90))


def test_a_grid_started_outside_its_range_waits_until_price_enters():
    above = (120, 120, 120, 120)
    result = run([above, (120, 125, 119, 121), (108, 108, 105, 106), (106, 106, 85, 88), above],
                 out_of_range="close")
    # Bar 1 goes further above the range: no trigger. Bar 2 enters the range.
    # Bar 3 buys 100 and 90 (both cells are long cells), then leaves below: close.
    assert kinds(result) == [("grid", "buy", 1), ("grid", "buy", 0), ("range_close", "sell", None)]
    assert result.range_exit_index == 3


# --------------------------------------------------------------------------- #
# Leverage and liquidation
# --------------------------------------------------------------------------- #

def liquidation_case(low, leverage=10):
    # Long grid, levels 40 / 70 / 100, start 80: cell 1 (70-100) is bought at 80.
    # Cell notional 1000 x 10 / 2 = 5000, quantity 5000 / 70. Cell 0 buys only at 40.
    rows = [(80, 80, 80, 80), (80, 80, low, 75), (75, 75, 75, 75)]
    return simulate_grid(make_bars(rows), replace(BASE, lower=40, upper=100, direction="long",
                                                  leverage=leverage), step_ms=STEP)


def expected_liquidation_price():
    q, mmr = 5000 / 70, 0.0033
    # equity 1000 + q (P - 80) equals maintenance margin mmr x q x P
    return (q * 80 - 1000) / (q * (1 - mmr))


def test_liquidation_uses_the_maintenance_margin_rule():
    p = expected_liquidation_price()
    assert 66 < p < 67
    alive = liquidation_case(p + 0.01)
    dead = liquidation_case(p - 0.01)
    assert not alive.liquidated and alive.equity[-1] > 0
    assert dead.liquidated and dead.liquidation_index == 1
    assert dead.equity[1:].tolist() == [0.0, 0.0]                    # the margin is gone
    assert [e[1] for e in dead.events] == ["initial", "liquidation"]  # nothing trades afterwards


def test_the_same_drop_does_not_liquidate_without_leverage():
    result = liquidation_case(expected_liquidation_price() - 0.01, leverage=1)
    assert not result.liquidated and result.equity[-1] > 900


def test_leverage_above_the_maintenance_margin_limit_is_rejected():
    with pytest.raises(ValueError, match="leverage"):
        replace(BASE, leverage=100, maintenance_margin_rate=0.02).validate()


# --------------------------------------------------------------------------- #
# Funding
# --------------------------------------------------------------------------- #

def test_funding_is_charged_on_the_net_position_at_each_settlement():
    bars = make_bars([FLAT] * 4)
    # Long grid: 5 units held at 100 from the first open.
    times = np.array([bars.ts[0], bars.ts[1] + STEP, bars.ts[3] + STEP])
    rates = FundingRates(times, np.array([0.01, 0.0001, -0.0003]))
    result = simulate_grid(bars, replace(BASE, direction="long"), funding=rates, step_ms=STEP)
    # The settlement at the very first open is before the grid holds anything: not charged.
    assert result.funding_events == 2
    assert result.funding_paid == pytest.approx(5 * 100 * 0.0001 + 5 * 100 * -0.0003)
    assert result.equity[1] == pytest.approx(1000 - 0.05)             # long pays a positive rate
    assert result.equity[3] == pytest.approx(1000 - 0.05 + 0.15)      # and receives a negative one


def test_a_short_receives_positive_funding_and_a_flat_grid_pays_nothing():
    bars = make_bars([FLAT] * 3)
    rates = FundingRates(np.array([bars.ts[1] + STEP]), np.array([0.001]))
    short = simulate_grid(bars, replace(BASE, direction="short"), funding=rates, step_ms=STEP)
    flat = simulate_grid(bars, BASE, funding=rates, step_ms=STEP)
    assert short.funding_paid == pytest.approx(-5 * 100 * 0.001)
    assert flat.funding_paid == 0.0 and flat.funding_events == 0


def test_buy_and_hold_benchmark():
    bars = make_bars([FLAT, (100, 111, 99, 110), (110, 111, 109, 110)])
    rates = FundingRates(np.array([bars.ts[1] + STEP]), np.array([0.001]))
    hold = buy_and_hold(bars, 0, 3, 1000, 0.001, 0.0, rates, STEP)
    qty = 1000 / 100
    final = 1000 - 1 + qty * 10 - qty * 110 * 0.001
    assert hold.return_pct == pytest.approx((final / 1000 - 1) * 100)
    assert hold.funding_paid == pytest.approx(qty * 110 * 0.001)
    assert hold.return_with_funding_pct == pytest.approx(((final - qty * 0.11) / 1000 - 1) * 100)


# --------------------------------------------------------------------------- #
# Bad settings
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", [
    {"lower": 0}, {"upper": 90}, {"upper": 80}, {"grid_count": 1}, {"grid_count": 0},
    {"grid_count": 2.5}, {"spacing": "log"}, {"direction": "up"}, {"out_of_range": "panic"},
    {"intrabar_path": "random"}, {"grid_fee_rate": 0.02}, {"slippage": -0.001},
    {"leverage": 0.5}, {"initial_capital": 0}, {"maintenance_margin_rate": 0.5}, {"lower": float("nan")},
])
def test_bad_grid_settings_are_rejected(bad):
    with pytest.raises(ValueError):
        replace(BASE, **bad).validate()


def test_bad_window_is_rejected():
    with pytest.raises(ValueError, match="window"):
        simulate_grid(make_bars([FLAT] * 3), BASE, 2, 2)
