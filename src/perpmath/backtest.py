"""Run a strategy over cached bars and build the result document.

The result document is the JSON the CLI writes. Its layout is described in
``docs/output-format.md`` and is versioned by ``schema_version``.
"""

from __future__ import annotations

import json
import os
import random
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    """A strategy's decisions changed when later bars were removed, or it could not run without them."""


# --------------------------------------------------------------------------- #
# Look-ahead check
# --------------------------------------------------------------------------- #

# Defaults of the check that runs before every backtest (``check_lookahead_at_signals``).
LOOKAHEAD_SIGNAL_CUTS = 4000   # at most this many cuts after bars that carry a decision
LOOKAHEAD_LEADING_BARS = 4000  # a cut after every one of the first this-many bars, decision or not
LOOKAHEAD_OTHER_CUTS = 500     # at most this many cuts after later bars that carry no decision
LOOKAHEAD_SEED = 20261122      # fixed, so the sampled cuts are the same on every run
LOOKAHEAD_FIXED_CUTS = 8       # evenly spaced cuts of the older check, always kept
# mode -> (max_signal_cuts, leading_bars, other_cuts); None = no limit
LOOKAHEAD_MODES: Dict[str, Tuple[Optional[int], Optional[int], Optional[int]]] = {
    "default": (LOOKAHEAD_SIGNAL_CUTS, LOOKAHEAD_LEADING_BARS, LOOKAHEAD_OTHER_CUTS),
    "signals": (None, LOOKAHEAD_LEADING_BARS, LOOKAHEAD_OTHER_CUTS),   # every signal bar, however many
    "every-bar": (None, None, None),                                   # a cut after every bar: complete, slow
}


def _same(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if a.dtype.kind == "f":
        return (a == b) | (np.isnan(a) & np.isnan(b))
    return a == b


def _compare_shortened(generate: Callable[[Bars, dict], Signals], bars: Bars, params: dict,
                       full: Dict[str, np.ndarray], k: int) -> None:
    """Run the strategy on the first ``k`` bars only; all ``k`` decisions must equal the full run's."""
    try:
        part = generate(bars.head(k), params).fields()
    except IndexError as exc:
        raise LookaheadError(f"strategy read past the last bar when given only {k} bars") from exc
    for name, values in part.items():
        diff = np.flatnonzero(~_same(values, full[name][:k]))
        if len(diff):
            bar = int(diff[0])
            raise LookaheadError(
                f"signals.{name} at bar {bar} ({iso_utc(int(bars.ts[bar]))}) changes when bars after "
                f"bar {k - 1} are removed: the strategy uses future data"
            )


def _fixed_cuts(n: int, cuts: int) -> List[int]:
    """Bar counts of the evenly spaced shortened copies, plus the one that drops only the last bar."""
    points = sorted({n * j // (cuts + 1) for j in range(1, cuts + 1)} | {n - 1})
    return [k for k in points if 0 < k < n]


def check_no_lookahead(generate: Callable[[Bars, dict], Signals], bars: Bars,
                       params: dict, cuts: int = 8) -> int:
    """The coarse check: shortened copies at a few fixed points.

    The strategy is run on the full data and again on shortened copies that
    end at ``cuts`` evenly spaced points (plus one that drops only the last
    bar). Every decision on a bar that exists in both runs must be identical.

    This finds a strategy whose decisions depend on bars far ahead, or on the
    whole series (a full-sample normalisation, a centred window). It usually
    does NOT find one that looks only a bar or a few bars ahead: such a
    strategy differs from the full run only on the last bars of a shortened
    copy, and with a handful of fixed cuts those are rarely bars it acts on.
    ``check_lookahead_at_signals`` puts the cuts where the decisions are and is
    what ``run_backtest`` uses.

    Returns the number of shortened runs compared. Raises ``LookaheadError``
    on the first difference.
    """
    full = generate(bars, params).fields()
    points = _fixed_cuts(len(bars), cuts)
    for k in points:
        _compare_shortened(generate, bars, params, full, k)
    return len(points)


def decision_bars(fields: Dict[str, np.ndarray]) -> np.ndarray:
    """True on every bar that carries a decision: an entry, an exit flag, a stop or a target."""
    return ((np.asarray(fields["side"]) != 0)
            | np.asarray(fields["exit_long"], dtype=bool) | np.asarray(fields["exit_short"], dtype=bool)
            | ~np.isnan(fields["stop"]) | ~np.isnan(fields["target_r"]))


def _spread(candidates: np.ndarray, limit: Optional[int], rng: random.Random) -> np.ndarray:
    """At most ``limit`` of ``candidates``: half evenly spaced (first and last included), half at random."""
    count = len(candidates)
    if limit is None or count <= limit:
        return candidates
    if limit == 0:
        return candidates[:0]
    even = np.unique(np.linspace(0, count - 1, (limit + 1) // 2).round().astype(np.int64))
    taken = np.zeros(count, dtype=bool)
    taken[even] = True
    rest = np.flatnonzero(~taken).tolist()
    # A partial shuffle built on random() alone: Python keeps that sequence
    # stable for a given seed, so the same bars are drawn on every version.
    for j in range(limit - len(even)):
        swap = j + int(rng.random() * (len(rest) - j))
        rest[j], rest[swap] = rest[swap], rest[j]
        taken[rest[j]] = True
    return candidates[taken]


def _pct(part: int, whole: int) -> Optional[float]:
    return round(part / whole * 100, 2) if whole else None


def check_lookahead_at_signals(generate: Callable[[Bars, dict], Signals], bars: Bars, params: dict,
                               max_signal_cuts: Optional[int] = LOOKAHEAD_SIGNAL_CUTS,
                               leading_bars: Optional[int] = LOOKAHEAD_LEADING_BARS,
                               other_cuts: Optional[int] = LOOKAHEAD_OTHER_CUTS,
                               seed: int = LOOKAHEAD_SEED) -> Dict[str, Any]:
    """Re-run the strategy on data that stops right after a bar, above all the bars where it decided something.

    The strategy is run once on the full data. A *signal bar* is a bar on
    which that run made any decision (entry side, stop, target or an exit
    flag). For a signal bar ``i`` the strategy is run again on bars ``0 .. i``
    only and its decision on bar ``i`` must be the same: side, stop, target and
    exit flags. The copy always starts at the first bar. Starting later would
    be cheaper but is not equivalent: an indicator with a long memory gives
    other values on a shorter history. The earlier bars of each shortened run
    are compared as well.

    What a cut after a signal bar establishes: the decision the full run made
    on that bar is the decision the strategy makes when nothing after the bar
    exists. Any difference stops the run. A strategy that makes its decisions
    one bar early fails on the first signal checked: the bar it peeked at is
    not in the copy. ``max_signal_cuts`` caps the number of signal bars checked;
    above the cap a subset is taken (half evenly spaced, half drawn with a
    fixed seed) and the report says so. ``None`` checks every signal bar.

    What it cannot establish. Look-ahead that only *removes* decisions leaves
    signal bars that all hold up: the bars where it acted have no signal in
    the full run, and would get one if the data stopped there. (Swing points
    used before they are confirmed do this to ``order_block``.) Only a cut
    after such a bar shows it, so bars without a signal are cut too:

    * every one of the first ``leading_bars`` bars, signal or not. A cut costs
      as much as the copy is long, so a complete stretch is cheapest at the
      start;
    * ``other_cuts`` of the later bars without a signal, chosen like the
      signal subset, plus the fixed points of ``check_no_lookahead``.

    Outside the leading stretch this part is a sample. It finds look-ahead
    that changes many bars and can miss look-ahead that changes a few.
    ``leading_bars=None`` cuts after every bar: nothing is sampled, and the
    cost is one ``generate`` call per bar.

    The last bar belongs to neither group: there is nothing after it to
    remove, and the engine never fills a signal from it.

    Returns the ``lookahead_check`` block of the result document. Raises
    ``LookaheadError`` on the first difference, and when the strategy cannot
    be run on a shortened copy at all.
    """
    for name, value in (("max_signal_cuts", max_signal_cuts), ("leading_bars", leading_bars),
                        ("other_cuts", other_cuts)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f"{name} must be a whole number >= 0 or None, got {value!r}")
    n = len(bars)
    full = generate(bars, params).fields()
    testable = max(n - 1, 0)                           # the last bar cannot be tested
    decided = decision_bars(full)[:testable]
    leading = testable if leading_bars is None else min(leading_bars, testable)
    later = np.arange(testable) >= leading
    signal_index = np.flatnonzero(decided)
    later_others = np.flatnonzero(~decided & later)

    rng = random.Random(seed)
    chosen = ~later                                    # the leading stretch, complete
    chosen[_spread(signal_index, max_signal_cuts, rng)] = True
    chosen[_spread(later_others, other_cuts, rng)] = True
    chosen[[k - 1 for k in _fixed_cuts(n, LOOKAHEAD_FIXED_CUTS)]] = True

    for i in np.flatnonzero(chosen).tolist():
        try:
            _compare_shortened(generate, bars, params, full, i + 1)
        except LookaheadError:
            raise
        except Exception as exc:
            raise LookaheadError(
                f"strategy failed when given only the first {i + 1} bars ({type(exc).__name__}: {exc}); "
                f"the look-ahead check needs it to run on any prefix of the data"
            ) from exc

    signal_bars, other_bars = len(signal_index), testable - len(signal_index)
    signals_checked = int(np.count_nonzero(chosen & decided))
    others_checked = int(np.count_nonzero(chosen & ~decided))
    sampled = signals_checked < signal_bars or others_checked < other_bars
    return {
        "passed": True,
        "method": "signal_cuts",
        "shortened_runs": signals_checked + others_checked,
        "signal_bars": signal_bars,
        "signal_bars_checked": signals_checked,
        "signal_bars_checked_pct": _pct(signals_checked, signal_bars),
        "signal_bars_sampled": signals_checked < signal_bars,
        "other_bars": other_bars,
        "other_bars_checked": others_checked,
        "other_bars_checked_pct": _pct(others_checked, other_bars),
        "other_bars_sampled": others_checked < other_bars,
        "leading_bars_all_checked": leading,
        "sample_seed": seed if sampled else None,
    }


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
                 now: Optional[datetime] = None,
                 lookahead: str = "default") -> Dict[str, Any]:
    """Check the strategy for look-ahead, simulate both samples, return the result document.

    ``config`` must come from ``load_config`` or ``normalize_config``.
    ``lookahead`` is one of ``LOOKAHEAD_MODES``: ``"default"`` (every signal
    bar up to ``LOOKAHEAD_SIGNAL_CUTS``, every one of the first
    ``LOOKAHEAD_LEADING_BARS`` bars, a sample of the other bars),
    ``"signals"`` (every signal bar, however many) or ``"every-bar"``.
    """
    step = interval_ms(interval)
    module = STRATEGIES[strategy]
    params = config["params"]
    execution = ExecutionConfig(**config["execution"])
    if lookahead not in LOOKAHEAD_MODES:
        raise ConfigError(f"lookahead must be one of {', '.join(LOOKAHEAD_MODES)}, got {lookahead!r}")

    # The split is validated first: the check below can take a while.
    oos_start_ms = _split_time(bars, step, config["split"])
    split_index = int(np.searchsorted(bars.ts, oos_start_ms))
    if split_index < 2 or len(bars) - split_index < 2:
        raise ConfigError(f"split at {iso_utc(oos_start_ms)} leaves an empty in-sample or out-of-sample period")

    lookahead_check = check_lookahead_at_signals(module.generate, bars, params, *LOOKAHEAD_MODES[lookahead])
    signals = module.generate(bars, params)

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
            "lookahead_check": lookahead_check,
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
