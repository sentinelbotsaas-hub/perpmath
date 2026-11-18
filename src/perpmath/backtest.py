"""Run a strategy over cached bars and build the result document.

The result document is the JSON the CLI writes. Its layout is described in
``docs/output-format.md`` and is versioned by ``schema_version``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from . import __version__
from .data import BYBIT_KLINE_URL, CATEGORY, Bars, count_missing_bars, interval_ms, iso_utc, parse_date_ms
from .engine import ExecutionConfig, Signals, SimResult, simulate
from .metrics import MIN_TRADES, compute_metrics
from .strategies import STRATEGIES, resolve_params

SCHEMA_VERSION = 1
DAY_MS = 86_400_000
DISCLAIMER = (
    "Backtest on historical data. Not investment advice. Past results do not "
    "predict future results. Funding payments are not modelled."
)


class ConfigError(Exception):
    """The run configuration is invalid."""


class LookaheadError(Exception):
    """A strategy's decisions changed when later bars were removed."""


# --------------------------------------------------------------------------- #
# Look-ahead check
# --------------------------------------------------------------------------- #

def _same(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if a.dtype.kind == "f":
        return (a == b) | (np.isnan(a) & np.isnan(b))
    return a == b


def check_no_lookahead(generate: Callable[[Bars, dict], Signals], bars: Bars,
                       params: dict, cuts: int = 8) -> int:
    """Prove that decisions do not depend on later bars.

    The strategy is run on the full data and again on shortened copies that
    end at ``cuts`` evenly spaced points (plus one that drops only the last
    bar). Every decision on a bar that exists in both runs must be identical.
    A strategy that reads bar ``i + 1`` while deciding bar ``i`` cannot pass:
    on the shortened copy that bar is not there.

    Returns the number of shortened runs compared. Raises ``LookaheadError``
    on the first difference.
    """
    n = len(bars)
    full = generate(bars, params).fields()
    points = sorted({n * j // (cuts + 1) for j in range(1, cuts + 1)} | {n - 1})
    points = [k for k in points if 0 < k < n]
    for k in points:
        try:
            part = generate(bars.head(k), params).fields()
        except IndexError as exc:
            raise LookaheadError(f"strategy read past the last bar when given only {k} bars") from exc
        for name, values in part.items():
            diff = np.flatnonzero(~_same(values, full[name][:k]))
            if len(diff):
                raise LookaheadError(
                    f"signals.{name} at bar {int(diff[0])} changes when bars after "
                    f"bar {k - 1} are removed: the strategy uses future data"
                )
    return len(points)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def load_config(path: str, strategy: str) -> Dict[str, Any]:
    """Read a JSON run configuration and fill in defaults. Unknown keys are errors."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    return normalize_config(raw, strategy)


def normalize_config(raw: Any, strategy: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    unknown = sorted(set(raw) - {"strategy", "params", "execution", "split"})
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(unknown)}")
    if raw.get("strategy", strategy) != strategy:
        raise ConfigError(f"config is for strategy {raw['strategy']!r}, not {strategy!r}")
    for section in ("params", "execution"):
        if not isinstance(raw.get(section, {}), dict):
            raise ConfigError(f"config section {section!r} must be a JSON object")
    try:
        params = resolve_params(strategy, raw.get("params", {}))
        execution = ExecutionConfig(**raw.get("execution", {}))
        execution.validate()
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc

    split = raw.get("split", {"in_sample_fraction": 0.7})
    if not isinstance(split, dict) or len(split) != 1 or next(iter(split)) not in ("in_sample_fraction", "oos_start"):
        raise ConfigError('split must be {"in_sample_fraction": x} or {"oos_start": "YYYY-MM-DD"}')
    if "in_sample_fraction" in split:
        fraction = split["in_sample_fraction"]
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 < fraction < 1:
            raise ConfigError("in_sample_fraction must be between 0 and 1")
    return {"strategy": strategy, "params": params, "execution": vars(execution), "split": dict(split)}


def _split_time(bars: Bars, step: int, split: Dict[str, Any]) -> int:
    """Open time (ms) of the first out-of-sample bar. Always a UTC midnight."""
    if "oos_start" in split:
        try:
            return parse_date_ms(split["oos_start"])
        except Exception as exc:
            raise ConfigError(str(exc)) from exc
    first, end = int(bars.ts[0]), int(bars.ts[-1]) + step
    return int(first + split["in_sample_fraction"] * (end - first)) // DAY_MS * DAY_MS


# --------------------------------------------------------------------------- #
# Result document
# --------------------------------------------------------------------------- #

def _num(value: Optional[float], digits: int) -> Optional[float]:
    if value is None or not np.isfinite(value):
        return None
    return round(float(value), digits)


def _equity_points(result: SimResult, step: int, first_ts: int) -> List[Dict[str, Any]]:
    """Starting capital, then equity at the last bar close of each UTC day."""
    points = [{"t": iso_utc(first_ts), "equity": _num(result.initial_capital, 2)}]
    close_ts = result.equity_ts + step
    days = (close_ts - 1) // DAY_MS
    last_of_day = np.flatnonzero(np.append(days[1:] != days[:-1], True))
    for i in last_of_day:
        points.append({"t": iso_utc(int(close_ts[i])), "equity": _num(result.equity[i], 2)})
    return points


def _segment(bars: Bars, signals: Signals, execution: ExecutionConfig,
             start: int, end: int, step: int) -> Dict[str, Any]:
    result = simulate(bars, signals, execution, start, end)
    metrics = compute_metrics(result.trades, result.equity, result.initial_capital)
    digits = {"total_fees": 2, "final_equity": 2, "avg_bars_held": 2}
    metrics = {k: (_num(v, digits.get(k, 4)) if isinstance(v, float) else v) for k, v in metrics.items()}
    trades = [{
        "n": number,
        "side": t.side,
        "entry_time": iso_utc(t.entry_time_ms),
        "entry_price": _num(t.entry_price, 6),
        "exit_time": iso_utc(t.exit_time_ms),
        "exit_price": _num(t.exit_price, 6),
        "stop_price": _num(t.stop_price, 6),
        "target_price": _num(t.target_price, 6),
        "quantity": _num(t.quantity, 8),
        "fees": _num(t.fees, 4),
        "pnl": _num(t.pnl, 4),
        "return_pct": _num(t.return_pct, 4),
        "r_multiple": _num(t.r_multiple, 4),
        "exit_reason": t.exit_reason,
        "bars_held": t.bars_held,
    } for number, t in enumerate(result.trades, start=1)]
    return {
        "first_bar": iso_utc(int(bars.ts[start])),
        "last_bar": iso_utc(int(bars.ts[end - 1])),
        "bars": end - start,
        "metrics": metrics,
        "signals": {
            "seen": result.signals_seen,
            "skipped_in_position": result.signals_skipped_in_position,
            "skipped_invalid_stop": result.signals_skipped_invalid_stop,
            "skipped_no_equity": result.signals_skipped_no_equity,
        },
        "equity_curve": _equity_points(result, step, int(bars.ts[start])),
        "trades": trades,
    }


def run_backtest(strategy: str, bars: Bars, config: Dict[str, Any], symbol: str, interval: str,
                 data_file: Optional[Dict[str, Any]] = None,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """Check the strategy for look-ahead, simulate both samples, return the result document.

    ``config`` must come from ``load_config`` or ``normalize_config``.
    """
    step = interval_ms(interval)
    module = STRATEGIES[strategy]
    params = config["params"]
    execution = ExecutionConfig(**config["execution"])

    lookahead_runs = check_no_lookahead(module.generate, bars, params)
    signals = module.generate(bars, params)

    oos_start_ms = _split_time(bars, step, config["split"])
    split_index = int(np.searchsorted(bars.ts, oos_start_ms))
    if split_index < 2 or len(bars) - split_index < 2:
        raise ConfigError(f"split at {iso_utc(oos_start_ms)} leaves an empty in-sample or out-of-sample period")

    in_sample = _segment(bars, signals, execution, 0, split_index, step)
    out_of_sample = _segment(bars, signals, execution, split_index, len(bars), step)

    missing = count_missing_bars(bars, step)
    warnings = []
    if missing:
        warnings.append(f"data: {missing} bar(s) missing between the first and last bar")
    for label, block in (("in_sample", in_sample), ("out_of_sample", out_of_sample)):
        if block["metrics"]["insufficient_sample"]:
            warnings.append(f"{label}: {block['metrics']['trades']} trades, fewer than {MIN_TRADES}: insufficient sample")

    data = {
        "exchange": "bybit",
        "category": CATEGORY,
        "source": BYBIT_KLINE_URL,
        "first_bar": iso_utc(int(bars.ts[0])),
        "last_bar": iso_utc(int(bars.ts[-1])),
        "bars": len(bars),
        "missing_bars": missing,
    }
    data.update(data_file or {})
    generated = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": SCHEMA_VERSION,
        "package": "perpmath",
        "package_version": __version__,
        "generated_at": generated,
        "strategy": strategy,
        "symbol": symbol,
        "interval": interval,
        "config": config,
        "data": data,
        "costs": {
            "fee_rate_per_side": execution.fee_rate,
            "slippage_per_fill": execution.slippage,
            "funding_modelled": False,
        },
        "timing": {
            "signal": "bar close",
            "fill": "next bar open",
            "same_bar_stop_and_target": "stop first",
            "lookahead_check": {"passed": True, "shortened_runs": lookahead_runs},
        },
        "split": {"oos_start": iso_utc(oos_start_ms)},
        "in_sample": in_sample,
        "out_of_sample": out_of_sample,
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------- #
# JSON output
# --------------------------------------------------------------------------- #

def _is_row(value: Any) -> bool:
    return isinstance(value, dict) and not any(isinstance(v, (dict, list)) for v in value.values())


def to_json(value: Any, indent: int = 0) -> str:
    """Indented JSON, except that flat records (trades, equity points) take one line each."""
    pad, inner = "  " * indent, "  " * (indent + 1)
    if isinstance(value, dict) and value:
        items = [f"{inner}{json.dumps(k)}: {to_json(v, indent + 1)}" for k, v in value.items()]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(value, list) and value:
        items = [inner + (json.dumps(v, allow_nan=False) if _is_row(v) else to_json(v, indent + 1))
                 for v in value]
        return "[\n" + ",\n".join(items) + "\n" + pad + "]"
    return json.dumps(value, allow_nan=False)


def write_result(path: str, document: Dict[str, Any]) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(to_json(document) + "\n")
