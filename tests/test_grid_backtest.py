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
from perpmath.data import DataError, write_csv
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
    assert gb.check_grid_no_lookahead(bars, config, funding, STEP) == 9


def test_a_simulator_that_reads_the_last_bar_fails_the_check(walk_and_funding, monkeypatch):
    bars, funding = walk_and_funding
    real = gb.simulate_grid

    def peeking(b, config, start, end, fund, step):
        result = real(b, config, start, end, fund, step)
        result.equity = result.equity + float(b.close[-1])     # depends on the last bar given
        return result

    monkeypatch.setattr(gb, "simulate_grid", peeking)
    config = GridConfig(lower=float(bars.low.min()), upper=float(bars.high.max()))
    with pytest.raises(LookaheadError, match="future data"):
        gb.check_grid_no_lookahead(bars, config, funding, STEP)


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
    assert doc["timing"]["lookahead_check"] == {"passed": True, "shortened_runs": 9}


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
