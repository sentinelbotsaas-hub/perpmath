"""End to end on synthetic data: config handling, the CLI, and the JSON result document."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import random_walk
from perpmath import __version__
from perpmath.backtest import ConfigError, normalize_config, run_backtest, to_json
from perpmath.cli import main
from perpmath.data import write_csv
from perpmath.strategies import STRATEGIES

CONFIGS = Path(__file__).resolve().parents[1] / "configs"
TOP_LEVEL_KEYS = ["schema_version", "package", "package_version", "generated_at", "strategy", "symbol",
                  "interval", "config", "data", "costs", "timing", "split", "in_sample", "out_of_sample",
                  "warnings", "disclaimer"]
METRIC_KEYS = ["trades", "insufficient_sample", "wins", "losses", "win_rate_pct", "avg_r",
               "avg_trade_return_pct", "t_stat", "t_stat_n", "total_return_pct", "max_drawdown_pct",
               "profit_factor", "total_fees", "avg_bars_held", "final_equity", "exit_reasons"]
TRADE_KEYS = ["n", "side", "entry_time", "entry_price", "exit_time", "exit_price", "stop_price",
              "target_price", "quantity", "fees", "pnl", "return_pct", "r_multiple", "exit_reason", "bars_held"]


@pytest.fixture
def data_dir(tmp_path):
    bars = random_walk(3000)
    rows = [(int(bars.ts[i]), repr(float(bars.open[i])), repr(float(bars.high[i])), repr(float(bars.low[i])),
             repr(float(bars.close[i])), "1", "100") for i in range(len(bars))]
    write_csv(tmp_path / "TESTUSDT-15m.csv", rows)
    return tmp_path


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_shipped_config_matches_the_strategy_defaults(name):
    raw = json.loads((CONFIGS / f"{name}.json").read_text(encoding="utf-8"))
    config = normalize_config(raw, name)
    assert config["params"] == STRATEGIES[name].DEFAULTS
    assert set(raw["params"]) == set(STRATEGIES[name].DEFAULTS)   # every parameter is written down
    assert config["execution"]["fee_rate"] == 0.00055
    assert config["execution"]["slippage"] == 0.0002


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_cli_backtest_writes_the_documented_json(name, data_dir, capsys):
    out = data_dir / "results" / f"{name}.json"
    code = main(["backtest", name, "--symbol", "TESTUSDT", "--interval", "15m",
                 "--config", str(CONFIGS / f"{name}.json"), "--out", str(out), "--data-dir", str(data_dir)])
    assert code == 0
    assert "in_sample" in capsys.readouterr().out

    doc = json.loads(out.read_text(encoding="utf-8"))
    assert list(doc) == TOP_LEVEL_KEYS
    assert (doc["schema_version"], doc["package"], doc["package_version"]) == (1, "perpmath", __version__)
    assert doc["strategy"] == name and doc["symbol"] == "TESTUSDT" and doc["interval"] == "15m"
    assert doc["costs"] == {"fee_rate_per_side": 0.00055, "slippage_per_fill": 0.0002, "funding_modelled": False}
    assert doc["data"]["bars"] == 3000 and len(doc["data"]["file_sha256"]) == 64
    datetime.strptime(doc["generated_at"], "%Y-%m-%dT%H:%M:%SZ")
    for label in ("in_sample", "out_of_sample"):
        block = doc[label]
        assert list(block) == ["first_bar", "last_bar", "bars", "metrics", "signals", "equity_curve", "trades"]
        assert list(block["metrics"]) == METRIC_KEYS
        assert block["metrics"]["trades"] == len(block["trades"]) > 0
        assert list(block["trades"][0]) == TRADE_KEYS
        assert block["equity_curve"][0] == {"t": block["first_bar"], "equity": 10000.0}
        assert block["equity_curve"][-1]["equity"] == block["metrics"]["final_equity"]
        times = [p["t"] for p in block["equity_curve"]]
        assert times == sorted(times) and len(set(times)) == len(times)
    assert doc["in_sample"]["bars"] + doc["out_of_sample"]["bars"] == 3000


def test_result_is_identical_on_a_second_run():
    bars = random_walk(3000)
    config = normalize_config({}, "liquidity_sweep")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = run_backtest("liquidity_sweep", bars, config, "TESTUSDT", "15m", now=now)
    second = run_backtest("liquidity_sweep", bars, config, "TESTUSDT", "15m", now=now)
    assert to_json(first) == to_json(second)
    assert json.loads(to_json(first)) == first                    # valid JSON, nothing lost
    assert first["generated_at"] == "2026-01-01T00:00:00Z"


def test_small_samples_are_flagged(data_dir):
    bars = random_walk(3000)
    doc = run_backtest("ma_cross", bars, normalize_config({}, "ma_cross"), "TESTUSDT", "15m")
    assert doc["in_sample"]["metrics"]["trades"] < 100
    assert doc["in_sample"]["metrics"]["insufficient_sample"] is True
    assert any("in_sample" in w and "insufficient sample" in w for w in doc["warnings"])


@pytest.mark.parametrize("raw, message", [
    ({"strategy": "ma_cross"}, "not 'fvg'"),
    ({"paramz": {}}, "unknown config key"),
    ({"params": {"lookback": 20}}, "unknown parameter"),
    ({"execution": {"fee_rate": 0.055}}, "fee_rate"),
    ({"execution": {"leverage": 10}}, "leverage"),
    ({"split": {"in_sample_fraction": 1.5}}, "in_sample_fraction"),
    ({"split": {"in_sample_fraction": 0.7, "oos_start": "2026-01-01"}}, "split must be"),
    ([], "JSON object"),
    ({"params": [1, 2]}, "must be a JSON object"),
])
def test_bad_configs_are_rejected(raw, message):
    with pytest.raises(ConfigError, match=message):
        normalize_config(raw, "fvg")


def test_split_by_explicit_date_and_empty_side_is_rejected():
    bars = random_walk(3000)
    first_day = datetime.fromtimestamp(bars.ts[0] / 1000, tz=timezone.utc)
    inside = first_day.replace(day=first_day.day + 10).strftime("%Y-%m-%d")
    doc = run_backtest("fvg", bars, normalize_config({"split": {"oos_start": inside}}, "fvg"), "T", "15m")
    assert doc["split"]["oos_start"] == inside + "T00:00:00Z"
    with pytest.raises(ConfigError, match="empty"):
        run_backtest("fvg", bars, normalize_config({"split": {"oos_start": "2099-01-01"}}, "fvg"), "T", "15m")


def test_cli_exits_non_zero_without_data_or_with_bad_config(tmp_path, capsys):
    args = ["backtest", "fvg", "--symbol", "NOPE", "--interval", "15m", "--out", str(tmp_path / "o.json"),
            "--data-dir", str(tmp_path)]
    assert main(args + ["--config", str(CONFIGS / "fvg.json")]) == 1          # no data file
    assert main(args + ["--config", str(tmp_path / "missing.json")]) == 1     # no config file
    assert not (tmp_path / "o.json").exists()
    assert capsys.readouterr().err.count("error:") == 2
