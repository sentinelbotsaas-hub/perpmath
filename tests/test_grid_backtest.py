"""Grid runs end to end on synthetic data: config, look-ahead check, result document, CLI."""

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from conftest import STEP, make_bars, random_walk
from perpmath import __version__
from perpmath import grid_backtest as gb
from perpmath.backtest import ConfigError, LookaheadError, to_json
from perpmath.cli import main
from perpmath.data import Bars, DataError, write_csv
from perpmath.funding import FundingRates, write_funding_csv
from perpmath.grid import GridConfig, simulate_grid

CONFIGS = Path(__file__).resolve().parents[1] / "configs"
EIGHT_H = 8 * 3_600_000
TOP_LEVEL_KEYS = ["schema_version", "package", "package_version", "generated_at", "strategy", "symbol",
                  "interval", "config", "data", "costs", "timing", "assumptions", "grid", "split",
                  "in_sample", "out_of_sample", "comparisons", "warnings", "disclaimer"]
SAMPLE_KEYS = ["first_bar", "last_bar", "bars", "start_price", "metrics", "buy_and_hold",
               "excess_return_vs_buy_and_hold_pct", "equity_curve", "fills"]
METRIC_KEYS = ["total_return_pct", "max_drawdown_pct", "final_equity", "fills", "grid_fills", "market_fills",
               "round_trips", "insufficient_sample", "trading_pnl", "total_fees", "grid_fees", "market_fees",
               "funding_paid", "funding_events", "liquidated", "liquidation_time", "liquidation_loss",
               "range_exit_time", "bars_outside_range_pct", "max_position_notional", "avg_position_notional"]
FILL_KEYS = ["n", "time", "kind", "side", "cell", "level", "price", "quantity", "fee", "pnl"]


def synthetic_funding(bars, rate=0.0001):
    first = int(bars.ts[0]) // EIGHT_H * EIGHT_H
    times = np.arange(first, int(bars.ts[-1]) + STEP + EIGHT_H, EIGHT_H, dtype=np.int64)
    return FundingRates(times, np.full(len(times), rate))


@pytest.fixture
def walk_and_funding():
    bars = random_walk(3000)
    return bars, synthetic_funding(bars)


def shipped_config():
    return gb.load_grid_config(str(CONFIGS / "grid.json"))


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def test_shipped_config_writes_down_every_default():
    raw = json.loads((CONFIGS / "grid.json").read_text(encoding="utf-8"))
    assert raw["params"] == gb.GRID_PARAMS
    assert raw["execution"] == gb.GRID_EXECUTION
    config = shipped_config()
    assert config["execution"]["grid_fee_rate"] == 0.0002 and config["execution"]["taker_fee_rate"] == 0.00055
    assert config["execution"]["funding"] is True
    assert config["params"]["leverage"] == 1


@pytest.mark.parametrize("raw, message", [
    ({"strategy": "fvg"}, "not 'grid'"),
    ({"extra": 1}, "unknown config key"),
    ({"params": {"grids": 10}}, "unknown params key"),
    ({"execution": {"fee_rate": 0.001}}, "unknown execution key"),
    ({"params": {"bounds": "auto"}}, "bounds must be"),
    ({"params": {"bounds": {"lower": 1}}}, "bounds must be"),
    ({"params": {"bounds": {"lower": 120, "upper": 100}}}, "upper must be above lower"),
    ({"params": {"grid_count": 0}}, "grid_count"),
    ({"params": {"grid_count": 1}}, "grid_count"),
    ({"params": {"direction": "both"}}, "direction"),
    ({"params": {"leverage": 500}}, "leverage"),
    ({"execution": {"grid_fee_rate": 0.02}}, "grid_fee_rate"),
    ({"execution": {"funding": "yes"}}, "funding"),
    ({"execution": {"intrabar_path": "random"}}, "intrabar_path"),
    ({"split": {"in_sample_fraction": 1}}, "in_sample_fraction"),
    ([], "JSON object"),
])
def test_bad_grid_configs_are_rejected(raw, message):
    with pytest.raises(ConfigError, match=message):
        gb.normalize_grid_config(raw)


def test_funding_on_without_funding_data_is_an_error(walk_and_funding):
    bars, _ = walk_and_funding
    with pytest.raises(ConfigError, match="no funding data"):
        gb.run_grid_backtest(bars, shipped_config(), "TEST", "15m")


def test_funding_with_holes_is_an_error(walk_and_funding):
    bars, funding = walk_and_funding
    holed = FundingRates(funding.ts[::2], funding.rate[::2])
    with pytest.raises(DataError, match="gap"):
        gb.run_grid_backtest(bars, shipped_config(), "TEST", "15m", funding=holed)


# --------------------------------------------------------------------------- #
# Look-ahead
# --------------------------------------------------------------------------- #

def test_simulator_passes_the_lookahead_check(walk_and_funding):
    bars, funding = walk_and_funding
    config = GridConfig(lower=float(bars.low.min()), upper=float(bars.high.max()), grid_count=40,
                        out_of_range="close", leverage=5)
    report = gb.check_grid_no_lookahead(bars, config, funding, STEP)
    assert list(report) == ["passed", "method", "shortened_runs", "fill_bars", "fill_bars_checked",
                            "fill_bars_checked_pct", "other_bars", "other_bars_checked", "other_bars_checked_pct",
                            "leading_bars_all_checked", "sample_seed"]
    assert report["passed"] is True and report["method"] == "fill_cuts"
    assert report["leading_bars_all_checked"] == gb.GRID_LOOKAHEAD_LEADING_BARS == 500
    assert report["fill_bars"] + report["other_bars"] == len(bars) - 1          # the last bar cannot be tested
    assert report["shortened_runs"] == report["fill_bars_checked"] + report["other_bars_checked"]
    assert 500 + gb.GRID_LOOKAHEAD_FILL_CUTS <= report["shortened_runs"] <= 500 + gb.GRID_LOOKAHEAD_FILL_CUTS + 9
    assert report["fill_bars_checked"] < report["fill_bars"] and report["sample_seed"] is not None
    assert report == gb.check_grid_no_lookahead(bars, config, funding, STEP)    # same cuts on every run


@pytest.mark.parametrize("options", [
    dict(),
    dict(direction="long", leverage=3),
    dict(direction="short", out_of_range="stop"),
    dict(out_of_range="close", leverage=5),
    dict(intrabar_path="low_first", spacing="arithmetic"),
    dict(intrabar_path="best_of_two"),
])
def test_simulator_gives_the_same_fills_and_equity_at_every_cut_of_a_short_series(options, walk_and_funding):
    """The complete version of the check: a cut after every bar, nothing sampled."""
    bars, funding = walk_and_funding
    bars = bars.head(500)
    # The series drifts down: a lower bound above its low makes it leave the range after it has traded.
    config = GridConfig(lower=float(bars.low.min()) * 1.05, upper=float(bars.high.max()),
                        grid_count=20, **options)
    report = gb.check_grid_no_lookahead(bars, config, funding, STEP, leading_bars=None)
    assert report["shortened_runs"] == 499 and report["sample_seed"] is None
    assert report["fill_bars_checked"] == report["fill_bars"] > 10
    assert report["other_bars_checked"] == report["other_bars"]


def test_a_simulator_that_reads_the_last_bar_fails_the_check(walk_and_funding, monkeypatch):
    bars, funding = walk_and_funding
    real = gb.simulate_grid

    def peeking(b, config, start, end, fund, step, **options):
        result = real(b, config, start, end, fund, step, **options)
        result.equity = result.equity + float(b.close[-1])     # depends on the last bar given
        return result

    monkeypatch.setattr(gb, "simulate_grid", peeking)
    config = GridConfig(lower=float(bars.low.min()), upper=float(bars.high.max()))
    with pytest.raises(LookaheadError, match="future data"):
        gb.check_grid_no_lookahead(bars, config, funding, STEP)


def comparison_made_up_to_0_2_1(bars, config, funding, cuts):
    """The old check: fixed cuts, each copy closed at its end, and its last bar therefore left out.

    Returns the first cut at which it sees a difference, or None when it sees none.
    """
    n = len(bars)
    full = gb.simulate_grid(bars, config, 0, n, funding, STEP)
    points = sorted({n * j // (cuts + 1) for j in range(1, cuts + 1)} | {n - 1})
    for k in [k for k in points if 1 < k < n]:
        part = gb.simulate_grid(bars.head(k), config, 0, k, funding, STEP)
        settled = k - 1
        if not np.array_equal(part.equity[:settled], full.equity[:settled]):
            return k
        if [e for e in part.events if e[0] < settled] != [e for e in full.events if e[0] < settled]:
            return k
    return None


def one_bar_ahead(real):
    """A simulator that works every bar with the NEXT bar's prices: look-ahead of exactly one bar."""
    def simulate(bars, config, start, end, funding, step, **options):
        def following(values):
            return np.append(values[1:], values[-1:])
        shifted = Bars(bars.ts, following(bars.open), following(bars.high), following(bars.low),
                       following(bars.close), bars.volume)
        return real(shifted, config, start, end, funding, step, **options)
    return simulate


def test_one_bar_look_ahead_passed_the_old_comparison_at_every_cut_and_fails_now(walk_and_funding, monkeypatch):
    """Regression test for the blind spot: in the old comparison the bar looked at was always still there."""
    bars, funding = walk_and_funding
    config = GridConfig(lower=float(bars.low.min()), upper=float(bars.high.max()), grid_count=30)
    monkeypatch.setattr(gb, "simulate_grid", one_bar_ahead(gb.simulate_grid))
    assert comparison_made_up_to_0_2_1(bars, config, funding, cuts=8) is None
    short = bars.head(400)
    assert comparison_made_up_to_0_2_1(short, config, funding, cuts=398) is None      # a cut after every bar
    with pytest.raises(LookaheadError, match="future data"):
        gb.check_grid_no_lookahead(bars, config, funding, STEP)
    with pytest.raises(LookaheadError, match="future data"):                           # fill and fixed cuts alone
        gb.check_grid_no_lookahead(bars, config, funding, STEP, leading_bars=0)
    with pytest.raises(LookaheadError, match="future data"):                           # the nine fixed cuts alone
        gb.check_grid_no_lookahead(bars, config, funding, STEP, leading_bars=0, fill_cuts=0)


def test_without_the_end_close_the_last_bar_ends_as_it_does_in_a_longer_run(walk_and_funding):
    bars, funding = walk_and_funding
    config = GridConfig(lower=float(bars.low.min()), upper=float(bars.high.max()), grid_count=30)
    cut = 1700
    full = simulate_grid(bars, config, funding=funding, step_ms=STEP)
    closed = simulate_grid(bars.head(cut), config, funding=funding, step_ms=STEP)
    left_open = simulate_grid(bars.head(cut), config, funding=funding, step_ms=STEP, close_at_end=False)
    assert closed.events[-1][1] == "end" and closed.events[:-1] == left_open.events
    assert left_open.events == [e for e in full.events if e[0] < cut]
    assert np.array_equal(left_open.equity, full.equity[:cut])
    assert closed.equity[-1] < left_open.equity[-1]            # the close costs a fee and slippage
    assert np.array_equal(closed.equity[:-1], left_open.equity[:-1])


def test_fills_and_equity_do_not_change_when_future_bars_are_added(walk_and_funding):
    bars, funding = walk_and_funding
    config = GridConfig(lower=float(bars.low.min()), upper=float(bars.high.max()), grid_count=30)
    cut = 1700
    short = simulate_grid(bars.head(cut), config, funding=funding, step_ms=STEP)
    full = simulate_grid(bars, config, funding=funding, step_ms=STEP)
    settled = [e for e in short.events if e[0] < cut - 1]
    assert len(settled) > 20
    assert settled == [e for e in full.events if e[0] < cut - 1]
    assert np.array_equal(short.equity[:cut - 1], full.equity[:cut - 1])


def test_out_of_sample_starts_flat_and_only_trades_after_the_split(walk_and_funding):
    bars, funding = walk_and_funding
    doc = gb.run_grid_backtest(bars, shipped_config(), "TEST", "15m", funding=funding)
    oos_start = doc["split"]["oos_start"]
    assert doc["out_of_sample"]["first_bar"] == oos_start
    assert all(f["time"] >= oos_start for f in doc["out_of_sample"]["fills"])
    assert all(f["time"] < oos_start for f in doc["in_sample"]["fills"])
    assert doc["out_of_sample"]["equity_curve"][0] == {"t": oos_start, "equity": 10000.0}
    check = doc["timing"]["lookahead_check"]
    assert check["passed"] is True and check["method"] == "fill_cuts" and check["shortened_runs"] > 500


def test_in_sample_range_bounds_are_flagged_as_hindsight(walk_and_funding):
    bars, funding = walk_and_funding
    doc = gb.run_grid_backtest(bars, shipped_config(), "TEST", "15m", funding=funding)
    split = int(np.searchsorted(bars.ts, int(datetime.strptime(doc["split"]["oos_start"], "%Y-%m-%dT%H:%M:%SZ")
                                               .replace(tzinfo=timezone.utc).timestamp() * 1000)))
    assert doc["grid"]["lower"] == round(float(bars.low[:split].min()), 6)
    assert doc["grid"]["upper"] == round(float(bars.high[:split].max()), 6)
    assert any("optimistic by construction" in w for w in doc["warnings"])
    assert any("hindsight" in a for a in doc["assumptions"])


# --------------------------------------------------------------------------- #
# Result document
# --------------------------------------------------------------------------- #

def test_document_layout_and_accounting(walk_and_funding):
    bars, funding = walk_and_funding
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    doc = gb.run_grid_backtest(bars, shipped_config(), "TEST", "15m", funding=funding, now=now)
    assert list(doc) == TOP_LEVEL_KEYS
    assert (doc["schema_version"], doc["package_version"], doc["strategy"]) == (1, __version__, "grid")
    assert doc["costs"]["funding_modelled"] is True and "maker 0.0200%" in doc["costs"]["fee_source"]
    assert len(doc["grid"]["levels"]) == doc["grid"]["grid_count"] + 1
    for label in ("in_sample", "out_of_sample"):
        block = doc[label]
        assert list(block) == SAMPLE_KEYS
        m = block["metrics"]
        assert list(m) == METRIC_KEYS
        assert m["fills"] == len(block["fills"]) > 0 and list(block["fills"][0]) == FILL_KEYS
        assert m["fills"] == m["grid_fills"] + m["market_fills"]
        explained = m["trading_pnl"] - m["total_fees"] - m["funding_paid"] - m["liquidation_loss"]
        assert m["final_equity"] - 10000 == pytest.approx(explained, abs=0.02)
        assert block["equity_curve"][-1]["equity"] == m["final_equity"]
        assert m["funding_events"] > 0
    assert set(doc["comparisons"]) == {"intrabar_path=low_first", "intrabar_path=high_first",
                                       "intrabar_path=best_of_two", "grid_fills_at_taker_fee",
                                       "without_funding"}
    assert doc["comparisons"]["without_funding"]["in_sample"]["funding_paid"] is None
    again = gb.run_grid_backtest(bars, shipped_config(), "TEST", "15m", funding=funding, now=now)
    assert to_json(doc) == to_json(again)
    assert json.loads(to_json(doc)) == doc


def test_funding_off_is_stated_not_hidden(walk_and_funding):
    bars, _ = walk_and_funding
    config = gb.normalize_grid_config({"execution": {"funding": False}})
    doc = gb.run_grid_backtest(bars, config, "TEST", "15m")
    assert doc["costs"]["funding_modelled"] is False
    assert doc["in_sample"]["metrics"]["funding_paid"] is None
    assert "Funding payments are not modelled" in doc["disclaimer"]
    assert any("NOT modelled" in a for a in doc["assumptions"])
    assert "without_funding" not in doc["comparisons"]


def test_narrow_cells_and_liquidation_are_warned_about(walk_and_funding):
    bars, funding = walk_and_funding
    lower, upper = float(bars.close[0]) * 0.999, float(bars.close[0]) * 1.001
    config = gb.normalize_grid_config({"params": {"bounds": {"lower": lower, "upper": upper},
                                                  "grid_count": 20, "direction": "long", "leverage": 50}})
    doc = gb.run_grid_backtest(bars, config, "TEST", "15m", funding=funding)
    assert any("every pair loses money" in w for w in doc["warnings"])
    assert any("liquidated" in w for w in doc["warnings"])
    m = doc["in_sample"]["metrics"]
    assert m["liquidated"] is True and m["final_equity"] == 0.0
    assert m["fills"] == m["grid_fills"] + m["market_fills"]
    assert doc["in_sample"]["fills"][-1]["kind"] == "liquidation"
    explained = m["trading_pnl"] - m["total_fees"] - m["funding_paid"] - m["liquidation_loss"]
    assert m["final_equity"] - 10000 == pytest.approx(explained, abs=0.02)


def test_a_grid_with_no_fills_reports_null_free_metrics():
    bars = make_bars([(100, 100.5, 99.5, 100)] * 200)
    config = gb.normalize_grid_config({"params": {"bounds": {"lower": 50, "upper": 60}},
                                       "execution": {"funding": False}})
    doc = gb.run_grid_backtest(bars, config, "TEST", "15m")
    m = doc["in_sample"]["metrics"]
    assert (m["fills"], m["round_trips"], m["total_return_pct"]) == (0, 0, 0.0)
    assert m["insufficient_sample"] is True and m["bars_outside_range_pct"] == 100.0
    assert any("outside the grid range" in w for w in doc["warnings"])


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

@pytest.fixture
def data_dir(tmp_path):
    bars = random_walk(3000)
    rows = [(int(bars.ts[i]), repr(float(bars.open[i])), repr(float(bars.high[i])), repr(float(bars.low[i])),
             repr(float(bars.close[i])), "1", "100") for i in range(len(bars))]
    write_csv(tmp_path / "TESTUSDT-15m.csv", rows)
    funding = synthetic_funding(bars)
    write_funding_csv(tmp_path / "TESTUSDT-funding.csv", [(int(t), "0.0001") for t in funding.ts])
    return tmp_path


def test_cli_grid_writes_the_documented_json(data_dir, capsys):
    out = data_dir / "results" / "grid.json"
    code = main(["grid", "--symbol", "TESTUSDT", "--interval", "15m", "--config", str(CONFIGS / "grid.json"),
                 "--out", str(out), "--data-dir", str(data_dir)])
    assert code == 0
    printed = capsys.readouterr().out
    assert "out_of_sample" in printed and "funding modelled" in printed
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert list(doc) == TOP_LEVEL_KEYS
    assert doc["data"]["funding"]["file"] == "TESTUSDT-funding.csv"
    assert len(doc["data"]["funding"]["file_sha256"]) == 64 and len(doc["data"]["file_sha256"]) == 64


def test_cli_grid_fails_cleanly_without_funding_file(data_dir, capsys):
    (data_dir / "TESTUSDT-funding.csv").unlink()
    out = data_dir / "o.json"
    code = main(["grid", "--symbol", "TESTUSDT", "--interval", "15m", "--config", str(CONFIGS / "grid.json"),
                 "--out", str(out), "--data-dir", str(data_dir)])
    assert code == 1 and not out.exists()
    assert "perpmath funding" in capsys.readouterr().err
