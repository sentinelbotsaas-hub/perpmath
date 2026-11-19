"""Run the grid simulator over cached bars and build its result document.

Also defines the ``perpmath grid`` and ``perpmath funding`` commands. The
document follows the conventions of ``backtest.py`` (same split, same JSON
writer, ``null`` for undefined values); its layout is described in the grid
section of ``docs/output-format.md``.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np

from . import __version__
from .backtest import (DAY_MS, SCHEMA_VERSION, ConfigError, LookaheadError, _equity_points, _num,
                       _split_time, write_result)
from .data import (BYBIT_KLINE_URL, CATEGORY, Bars, DataError, cache_path, count_missing_bars,
                   file_sha256, interval_ms, iso_utc, load_bars, parse_date_ms)
from .funding import (BYBIT_FUNDING_URL, FundingRates, check_coverage, download_funding,
                      funding_cache_path, load_funding)
from .grid import (PATHS, GridConfig, GridResult, buy_and_hold, grid_levels, narrowest_cell_pct,
                   simulate_grid)
from .metrics import MIN_TRADES, max_drawdown_pct

GRID_PARAMS = {
    "bounds": "in_sample_range",
    "grid_count": 50,
    "spacing": "geometric",
    "direction": "neutral",
    "leverage": 1,
    "out_of_range": "continue",
}
GRID_EXECUTION = {
    "grid_fee_rate": 0.0002,
    "taker_fee_rate": 0.00055,
    "slippage": 0.0002,
    "initial_capital": 10000,
    "maintenance_margin_rate": 0.0033,
    "intrabar_path": "worst_of_two",
    "funding": True,
}
FEE_SOURCE = ("Bybit Trading Fee Structure, VIP 0, perpetual and futures contracts: maker 0.0200%, "
              "taker 0.0550% (https://www.bybit.com/en/help-center/article/Trading-Fee-Structure, "
              "read 2026-11-19). Rates depend on account tier and change; set your own.")
MMR_SOURCE = ("Bybit risk limit, BTCUSDT tier 1 (positions up to 300,000 USDT): 0.33% "
              "(https://api.bybit.com/v5/market/risk-limit?category=linear&symbol=BTCUSDT, "
              "snapshot 2026-11-18). Other symbols and larger positions have higher rates.")
TIER_ONE_LIMIT = 300_000
DISCLAIMER = (
    "Backtest on historical data. Not investment advice. Past results do not predict future "
    "results. Grid fills inside a bar are estimated from OHLC data under the stated path assumption."
)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def load_grid_config(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    return normalize_grid_config(raw)


def _bounds(value: Any) -> Any:
    if value == "in_sample_range":
        return value
    if isinstance(value, dict) and set(value) == {"lower", "upper"}:
        for key in ("lower", "upper"):
            if isinstance(value[key], bool) or not isinstance(value[key], (int, float)):
                raise ConfigError(f"bounds.{key} must be a number")
        return dict(value)
    raise ConfigError('bounds must be "in_sample_range" or {"lower": price, "upper": price}')


def normalize_grid_config(raw: Any) -> Dict[str, Any]:
    """Validate a grid run configuration and fill in defaults. Unknown keys are errors."""
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    unknown = sorted(set(raw) - {"strategy", "params", "execution", "split"})
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(unknown)}")
    if raw.get("strategy", "grid") != "grid":
        raise ConfigError(f"config is for strategy {raw['strategy']!r}, not 'grid'")
    sections = {}
    for name, defaults in (("params", GRID_PARAMS), ("execution", GRID_EXECUTION)):
        given = raw.get(name, {})
        if not isinstance(given, dict):
            raise ConfigError(f"config section {name!r} must be a JSON object")
        bad = sorted(set(given) - set(defaults))
        if bad:
            raise ConfigError(f"unknown {name} key(s) for grid: {', '.join(bad)}")
        merged = dict(defaults)
        merged.update(given)
        sections[name] = merged
    params, execution = sections["params"], sections["execution"]
    params["bounds"] = _bounds(params["bounds"])
    if not isinstance(execution["funding"], bool):
        raise ConfigError("execution.funding must be true or false")
    probe = params["bounds"] if isinstance(params["bounds"], dict) else {"lower": 1.0, "upper": 2.0}
    try:
        _grid_config(params, execution, probe["lower"], probe["upper"]).validate()
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc

    split = raw.get("split", {"in_sample_fraction": 0.7})
    if not isinstance(split, dict) or len(split) != 1 or next(iter(split)) not in ("in_sample_fraction", "oos_start"):
        raise ConfigError('split must be {"in_sample_fraction": x} or {"oos_start": "YYYY-MM-DD"}')
    if "in_sample_fraction" in split:
        fraction = split["in_sample_fraction"]
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 < fraction < 1:
            raise ConfigError("in_sample_fraction must be between 0 and 1")
    return {"strategy": "grid", "params": params, "execution": execution, "split": dict(split)}


def _grid_config(params: Dict[str, Any], execution: Dict[str, Any], lower: float, upper: float) -> GridConfig:
    return GridConfig(
        lower=float(lower), upper=float(upper),
        grid_count=params["grid_count"], spacing=params["spacing"], direction=params["direction"],
        leverage=params["leverage"], out_of_range=params["out_of_range"],
        grid_fee_rate=execution["grid_fee_rate"], taker_fee_rate=execution["taker_fee_rate"],
        slippage=execution["slippage"], initial_capital=execution["initial_capital"],
        maintenance_margin_rate=execution["maintenance_margin_rate"],
        intrabar_path=execution["intrabar_path"],
    )


# --------------------------------------------------------------------------- #
# Look-ahead check
# --------------------------------------------------------------------------- #

def check_grid_no_lookahead(bars: Bars, config: GridConfig, funding: Optional[FundingRates],
                            step_ms: int, cuts: int = 8) -> int:
    """Prove the simulator never uses a bar before it happens.

    The grid is simulated on the full data and again on shortened copies.
    Every fill and every equity value before the last bar of a shortened
    copy must be identical (the last bar differs only because open
    positions are closed there). Returns the number of shortened runs.

    This checks the simulator with fixed bounds. It cannot tell whether the
    bounds themselves were chosen with hindsight; ``in_sample_range`` is, for
    the in-sample period, and the result says so.
    """
    n = len(bars)
    full = simulate_grid(bars, config, 0, n, funding, step_ms)
    points = sorted({n * j // (cuts + 1) for j in range(1, cuts + 1)} | {n - 1})
    points = [k for k in points if 1 < k < n]
    for k in points:
        part = simulate_grid(bars.head(k), config, 0, k, funding, step_ms)
        settled = k - 1
        diff = np.flatnonzero(part.equity[:settled] != full.equity[:settled])
        if len(diff):
            raise LookaheadError(f"grid equity at bar {int(diff[0])} changes when bars after bar {k - 1} "
                                 f"are removed: the simulator uses future data")
        if [e for e in part.events if e[0] < settled] != [e for e in full.events if e[0] < settled]:
            raise LookaheadError(f"grid fills before bar {settled} change when bars after bar {k - 1} "
                                 f"are removed: the simulator uses future data")
    return len(points)


# --------------------------------------------------------------------------- #
# Result document
# --------------------------------------------------------------------------- #

def grid_metrics(result: GridResult, bars: Bars) -> Dict[str, Any]:
    """Summary of one grid run. Money in account currency, rates in percent."""
    fills = [e for e in result.events if e[1] != "range_stop"]
    final_equity = float(result.equity[-1])
    liq = result.liquidation_index
    exit_index = result.range_exit_index
    return {
        "total_return_pct": (final_equity / result.initial_capital - 1) * 100,
        "max_drawdown_pct": max_drawdown_pct(result.equity, result.initial_capital),
        "final_equity": final_equity,
        "fills": len(fills),
        "grid_fills": result.grid_fills,
        "market_fills": result.market_fills,
        "round_trips": result.round_trips,
        "insufficient_sample": result.round_trips < MIN_TRADES,
        "trading_pnl": result.trading_pnl,
        "total_fees": result.grid_fees + result.market_fees,
        "grid_fees": result.grid_fees,
        "market_fees": result.market_fees,
        "funding_paid": result.funding_paid if result.funding_modelled else None,
        "funding_events": result.funding_events if result.funding_modelled else None,
        "liquidated": result.liquidated,
        "liquidation_time": iso_utc(int(bars.ts[liq])) if liq is not None else None,
        "liquidation_loss": result.liquidation_loss,
        "range_exit_time": iso_utc(int(bars.ts[exit_index])) if exit_index is not None else None,
        "bars_outside_range_pct": result.bars_outside_range / len(result.equity) * 100,
        "max_position_notional": float(np.max(result.position_notional)),
        "avg_position_notional": float(np.mean(result.position_notional)),
    }


_DIGITS = {"final_equity": 2, "trading_pnl": 2, "total_fees": 2, "grid_fees": 2, "market_fees": 2,
           "funding_paid": 2, "liquidation_loss": 2, "max_position_notional": 2, "avg_position_notional": 2}


def _round_block(block: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (_num(v, _DIGITS.get(k, 4)) if isinstance(v, float) else v) for k, v in block.items()}


def _fill_rows(result: GridResult, bars: Bars) -> List[Dict[str, Any]]:
    rows = []
    for e in result.events:
        index, kind, side, cell, level, price, qty, fee, pnl = e
        if kind == "range_stop":
            continue
        rows.append({
            "n": len(rows) + 1,
            "time": iso_utc(int(bars.ts[index])),
            "kind": kind,
            "side": side,
            "cell": cell,
            "level": _num(level, 6),
            "price": _num(price, 6),
            "quantity": _num(qty, 8),
            "fee": _num(fee, 4),
            "pnl": _num(pnl, 4),
        })
    return rows


def _segment(bars: Bars, config: GridConfig, funding: Optional[FundingRates], start: int, end: int,
             step: int) -> Dict[str, Any]:
    result = simulate_grid(bars, config, start, end, funding, step)
    metrics = grid_metrics(result, bars)
    hold = buy_and_hold(bars, start, end, config.initial_capital, config.taker_fee_rate,
                        config.slippage, funding, step)
    hold_block = {
        "return_pct": hold.return_pct,
        "max_drawdown_pct": hold.max_drawdown_pct,
        "funding_paid": hold.funding_paid if funding is not None else None,
        "return_with_funding_pct": hold.return_with_funding_pct if funding is not None else None,
    }
    comparable = hold.return_with_funding_pct if funding is not None else hold.return_pct
    return {
        "first_bar": iso_utc(int(bars.ts[start])),
        "last_bar": iso_utc(int(bars.ts[end - 1])),
        "bars": end - start,
        "start_price": _num(result.start_price, 6),
        "metrics": _round_block(metrics),
        "buy_and_hold": _round_block(hold_block),
        "excess_return_vs_buy_and_hold_pct": _num(metrics["total_return_pct"] - comparable, 4),
        "equity_curve": _equity_points(result, step, int(bars.ts[start])),
        "fills": _fill_rows(result, bars),
    }


def _variant(bars: Bars, config: GridConfig, funding: Optional[FundingRates], start: int, end: int,
             step: int) -> Dict[str, Any]:
    m = grid_metrics(simulate_grid(bars, config, start, end, funding, step), bars)
    keep = ("total_return_pct", "max_drawdown_pct", "round_trips", "grid_fills", "total_fees",
            "funding_paid", "liquidated")
    return _round_block({k: m[k] for k in keep})


def _comparisons(bars: Bars, config: GridConfig, funding: Optional[FundingRates],
                 windows: Dict[str, tuple], step: int) -> Dict[str, Any]:
    variants = {}
    for path in PATHS:
        if path != config.intrabar_path:
            variants[f"intrabar_path={path}"] = (replace(config, intrabar_path=path), funding)
    if config.grid_fee_rate != config.taker_fee_rate:
        variants["grid_fills_at_taker_fee"] = (replace(config, grid_fee_rate=config.taker_fee_rate), funding)
    if funding is not None:
        variants["without_funding"] = (config, None)
    return {
        name: {label: _variant(bars, cfg, fund, s, e, step) for label, (s, e) in windows.items()}
        for name, (cfg, fund) in variants.items()
    }


def _assumptions(config: GridConfig, bounds_mode: Any, funding_on: bool) -> List[str]:
    path_text = {
        "worst_of_two": "each bar is walked twice, open-low-high-close and open-high-low-close; the path "
                        "that leaves lower equity at the bar close is kept (pessimistic about the order "
                        "of high and low, chosen bar by bar)",
        "low_first": "each bar is walked open-low-high-close",
        "high_first": "each bar is walked open-high-low-close",
        "best_of_two": "each bar is walked both ways and the path with higher equity at the close is "
                       "kept (optimistic; for comparison only)",
    }[config.intrabar_path]
    items = [
        f"intrabar path: {path_text}; the move from the previous close to the open is walked first",
        "a cell that fills can trade again later in the same bar if the path crosses its other level",
        "grid orders fill only when price trades strictly through their level, at the level price "
        "(never better), then slippage is applied against the trader",
        "grid fills pay grid_fee_rate (maker); the initial position, range closes and the end-of-sample "
        "close are market orders and pay taker_fee_rate",
        "the grid is placed at the open of each sample's first bar; each sample starts flat with the "
        "same capital, and anything open at its last bar is closed at that bar's close",
        "isolated margin: the grid's whole equity backs its net position; liquidation when equity <= "
        "maintenance_margin_rate x |net quantity| x price, checked at every path end point; on "
        "liquidation the remaining equity is lost",
    ]
    if funding_on:
        items.append("funding: net quantity x bar close x Bybit's settled rate at every funding time "
                     "(Bybit uses the mark price; the difference is ignored)")
    else:
        items.append("funding is NOT modelled in this run")
    if bounds_mode == "in_sample_range":
        items.append("bounds are the lowest low and highest high of the in-sample period: known only "
                     "in hindsight during the in-sample period, so the in-sample result is optimistic "
                     "by construction; the out-of-sample period is the test")
    return items


def run_grid_backtest(bars: Bars, config: Dict[str, Any], symbol: str, interval: str,
                      funding: Optional[FundingRates] = None,
                      data_file: Optional[Dict[str, Any]] = None,
                      funding_file: Optional[Dict[str, Any]] = None,
                      now: Optional[datetime] = None,
                      lookahead_cuts: int = 8) -> Dict[str, Any]:
    """Simulate both samples and return the grid result document.

    ``config`` must come from ``load_grid_config`` or ``normalize_grid_config``.
    ``funding`` is required when ``config["execution"]["funding"]`` is true.
    """
    step = interval_ms(interval)
    params, execution = config["params"], config["execution"]
    use_funding = execution["funding"]
    if use_funding and funding is None:
        raise ConfigError('execution.funding is true but no funding data was given; run `perpmath funding` '
                          'or set "funding": false')
    if not use_funding:
        funding = None

    oos_start_ms = _split_time(bars, step, config["split"])
    split_index = int(np.searchsorted(bars.ts, oos_start_ms))
    if split_index < 2 or len(bars) - split_index < 2:
        raise ConfigError(f"split at {iso_utc(oos_start_ms)} leaves an empty in-sample or out-of-sample period")

    if params["bounds"] == "in_sample_range":
        lower = float(np.min(bars.low[:split_index]))
        upper = float(np.max(bars.high[:split_index]))
    else:
        lower, upper = float(params["bounds"]["lower"]), float(params["bounds"]["upper"])
    grid_config = _grid_config(params, execution, lower, upper)
    try:
        grid_config.validate()
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    funding_records = 0
    if funding is not None:
        funding_records = check_coverage(funding, int(bars.ts[0]), int(bars.ts[-1]) + step)

    lookahead_runs = check_grid_no_lookahead(bars, grid_config, funding, step, lookahead_cuts)
    windows = {"in_sample": (0, split_index), "out_of_sample": (split_index, len(bars))}
    blocks = {label: _segment(bars, grid_config, funding, s, e, step) for label, (s, e) in windows.items()}
    comparisons = _comparisons(bars, grid_config, funding, windows, step)

    levels = grid_levels(lower, upper, grid_config.grid_count, grid_config.spacing)
    cell_widths = np.diff(levels) / levels[:-1] * 100
    cell_notional = grid_config.initial_capital * grid_config.leverage / grid_config.grid_count
    round_trip_cost_pct = 2 * (grid_config.grid_fee_rate + grid_config.slippage) * 100

    missing = count_missing_bars(bars, step)
    warnings = []
    if missing:
        warnings.append(f"data: {missing} bar(s) missing between the first and last bar")
    if params["bounds"] == "in_sample_range":
        warnings.append("in_sample: the grid bounds come from the in-sample period itself, so this "
                        "block is optimistic by construction; use out_of_sample as the test")
    if narrowest_cell_pct(levels) <= round_trip_cost_pct:
        warnings.append(f"the narrowest cell ({narrowest_cell_pct(levels):.4f}%) is not wider than the "
                        f"round-trip cost of a grid pair ({round_trip_cost_pct:.4f}%): every pair loses money")
    if grid_config.initial_capital * grid_config.leverage > TIER_ONE_LIMIT:
        warnings.append("the grid's full notional exceeds 300,000 USDT, above BTCUSDT risk tier 1: the "
                        "maintenance margin rate used here is too low for that size")
    for label, block in blocks.items():
        m = block["metrics"]
        if not lower <= block["start_price"] <= upper:
            warnings.append(f"{label}: the start price {block['start_price']} is outside the grid range")
        if m["insufficient_sample"]:
            warnings.append(f"{label}: {m['round_trips']} round trips, fewer than {MIN_TRADES}: insufficient sample")
        if m["liquidated"]:
            warnings.append(f"{label}: liquidated at {m['liquidation_time']}")

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
    if funding is not None:
        inside = funding.ts[(funding.ts >= bars.ts[0]) & (funding.ts < bars.ts[-1] + step)]
        data["funding"] = {
            "source": BYBIT_FUNDING_URL,
            "records_in_range": funding_records,
            "first": iso_utc(int(inside[0])) if len(inside) else None,
            "last": iso_utc(int(inside[-1])) if len(inside) else None,
        }
        data["funding"].update(funding_file or {})
    else:
        data["funding"] = None

    generated = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": SCHEMA_VERSION,
        "package": "perpmath",
        "package_version": __version__,
        "generated_at": generated,
        "strategy": "grid",
        "symbol": symbol,
        "interval": interval,
        "config": config,
        "data": data,
        "costs": {
            "grid_fee_rate": grid_config.grid_fee_rate,
            "taker_fee_rate": grid_config.taker_fee_rate,
            "fee_source": FEE_SOURCE,
            "slippage_per_fill": grid_config.slippage,
            "round_trip_cost_pct": _num(round_trip_cost_pct, 4),
            "funding_modelled": funding is not None,
            "maintenance_margin_rate": grid_config.maintenance_margin_rate,
            "maintenance_margin_source": MMR_SOURCE,
        },
        "timing": {
            "grid_start": "open of the first bar of each sample",
            "grid_orders": "fill when price trades strictly through the level, at the level",
            "intrabar_path": grid_config.intrabar_path,
            "lookahead_check": {"passed": True, "shortened_runs": lookahead_runs},
        },
        "assumptions": _assumptions(grid_config, params["bounds"], funding is not None),
        "grid": {
            "lower": _num(lower, 6),
            "upper": _num(upper, 6),
            "bounds_source": "in-sample lowest low and highest high" if params["bounds"] == "in_sample_range"
                             else "fixed in config",
            "grid_count": grid_config.grid_count,
            "spacing": grid_config.spacing,
            "direction": grid_config.direction,
            "leverage": grid_config.leverage,
            "out_of_range": grid_config.out_of_range,
            "cell_notional": _num(cell_notional, 2),
            "cell_width_pct_min": _num(float(np.min(cell_widths)), 4),
            "cell_width_pct_max": _num(float(np.max(cell_widths)), 4),
            "levels": [_num(float(x), 6) for x in levels],
        },
        "split": {"oos_start": iso_utc(oos_start_ms)},
        "in_sample": blocks["in_sample"],
        "out_of_sample": blocks["out_of_sample"],
        "comparisons": comparisons,
        "warnings": warnings,
        "disclaimer": DISCLAIMER if funding is not None else DISCLAIMER + " Funding payments are not modelled.",
    }


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def _cmd_funding(args: argparse.Namespace) -> int:
    if args.start:
        start_ms = parse_date_ms(args.start)
        end_ms = parse_date_ms(args.end) if args.end else int(time.time() * 1000)
    else:
        if args.end:
            raise DataError("--end needs --start")
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - args.days * DAY_MS
    path, rows = download_funding(args.symbol, start_ms, end_ms, data_dir=args.data_dir)
    if not rows:
        raise DataError(f"no funding records for {args.symbol} in the requested range")
    print(f"saved {len(rows)} funding records to {path}")
    print(f"range {iso_utc(rows[0][0])} to {iso_utc(rows[-1][0])} (settlement times, UTC)")
    return 0


def _cmd_grid(args: argparse.Namespace) -> int:
    config = load_grid_config(args.config)
    path = cache_path(args.data_dir, args.symbol, args.interval)
    bars = load_bars(
        path,
        parse_date_ms(args.start) if args.start else None,
        parse_date_ms(args.end) if args.end else None,
    )
    funding = funding_file = None
    if config["execution"]["funding"]:
        fpath = funding_cache_path(args.data_dir, args.symbol)
        funding = load_funding(fpath)
        funding_file = {"file": fpath.name, "file_sha256": file_sha256(fpath)}
    document = run_grid_backtest(
        bars, config, args.symbol, args.interval, funding=funding,
        data_file={"file": path.name, "file_sha256": file_sha256(path)},
        funding_file=funding_file,
    )
    write_result(args.out, document)

    g, costs = document["grid"], document["costs"]
    print(f"grid {args.symbol} {args.interval}  {document['data']['first_bar']} to {document['data']['last_bar']}")
    print(f"{g['direction']} {g['spacing']}, {g['grid_count']} cells, {g['lower']} to {g['upper']}, "
          f"leverage {g['leverage']}, path {document['timing']['intrabar_path']}")
    print(f"grid fee {costs['grid_fee_rate'] * 100:.4f}%, taker fee {costs['taker_fee_rate'] * 100:.4f}%, "
          f"slippage {costs['slippage_per_fill'] * 100:.4f}% per fill, "
          f"funding {'modelled' if costs['funding_modelled'] else 'NOT modelled'}")
    print(f"{'':14}{'pairs':>7}{'return %':>10}{'max DD %':>10}{'fees':>10}{'funding':>10}{'hold %':>9}")
    for label in ("in_sample", "out_of_sample"):
        block = document[label]
        m, hold = block["metrics"], block["buy_and_hold"]
        hold_ret = hold["return_with_funding_pct"] if hold["return_with_funding_pct"] is not None else hold["return_pct"]
        funding_text = "n/a" if m["funding_paid"] is None else format(m["funding_paid"], ".2f")
        print(f"{label:14}{m['round_trips']:>7}{m['total_return_pct']:>10.2f}{m['max_drawdown_pct']:>10.2f}"
              f"{m['total_fees']:>10.2f}{funding_text:>10}{hold_ret:>9.2f}")
    for warning in document["warnings"]:
        print(f"warning: {warning}")
    print(f"wrote {args.out}")
    return 0


def add_grid_commands(sub: Any) -> None:
    """Register ``perpmath funding`` and ``perpmath grid`` on the CLI's subparsers."""
    fd = sub.add_parser("funding", help="download Bybit USDT-perpetual funding-rate history to the local cache")
    fd.add_argument("symbol", help="for example BTCUSDT")
    fd.add_argument("--days", type=int, default=365, help="days back from now (default 365)")
    fd.add_argument("--start", help="YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ (UTC)")
    fd.add_argument("--end", help="exclusive, same format as --start")
    fd.add_argument("--data-dir", default="data")
    fd.set_defaults(func=_cmd_funding)

    gr = sub.add_parser("grid", help="run a grid-trading backtest with one config and write a JSON result")
    gr.add_argument("--symbol", required=True)
    gr.add_argument("--interval", required=True)
    gr.add_argument("--config", required=True, help="JSON file with params, execution and split")
    gr.add_argument("--out", required=True, help="where to write the JSON result")
    gr.add_argument("--start", help="use only bars from this date (YYYY-MM-DD, UTC)")
    gr.add_argument("--end", help="use only bars before this date (YYYY-MM-DD, UTC)")
    gr.add_argument("--data-dir", default="data")
    gr.set_defaults(func=_cmd_grid)


__all__ = ["GRID_EXECUTION", "GRID_PARAMS", "add_grid_commands", "check_grid_no_lookahead",
           "grid_metrics", "load_grid_config", "normalize_grid_config", "run_grid_backtest"]
