"""Command line: ``perpmath download``, ``perpmath backtest`` and ``perpmath live``.

Exit codes: 0 = success, 1 = bad data, bad config or failed look-ahead check,
2 = bad command-line usage, 3 = ``live`` stopped by a circuit breaker or a
failed reconciliation.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional

from . import __version__
from .backtest import ConfigError, LookaheadError, load_config, run_backtest, write_result
from .data import (INTERVALS, DataError, cache_path, download, file_sha256, interval_ms, iso_utc,
                   load_bars, parse_date_ms)
from .strategies import STRATEGIES

DAY_MS = 86_400_000


def _cmd_download(args: argparse.Namespace) -> int:
    step = interval_ms(args.interval)
    if args.start:
        start_ms = parse_date_ms(args.start)
        end_ms = parse_date_ms(args.end) if args.end else int(time.time() * 1000)
    else:
        if args.end:
            raise DataError("--end needs --start")
        end_ms = int(time.time() * 1000) // step * step
        start_ms = end_ms - args.days * DAY_MS
    path, rows = download(args.symbol, args.interval, start_ms, end_ms, data_dir=args.data_dir)
    wanted = (end_ms - 1) // step - (-(-start_ms // step)) + 1
    print(f"saved {len(rows)} bars to {path}")
    print(f"range {iso_utc(rows[0][0])} to {iso_utc(rows[-1][0])} (bar open times, UTC)")
    if len(rows) < wanted:
        print(f"warning: expected up to {wanted} bars for the requested range, got {len(rows)}",
              file=sys.stderr)
    return 0


def _cmd_backtest(args: argparse.Namespace) -> int:
    config = load_config(args.config, args.strategy)
    path = cache_path(args.data_dir, args.symbol, args.interval)
    bars = load_bars(
        path,
        parse_date_ms(args.start) if args.start else None,
        parse_date_ms(args.end) if args.end else None,
    )
    document = run_backtest(
        args.strategy, bars, config, args.symbol, args.interval,
        data_file={"file": path.name, "file_sha256": file_sha256(path)},
    )
    write_result(args.out, document)

    print(f"{args.strategy} {args.symbol} {args.interval}  "
          f"{document['data']['first_bar']} to {document['data']['last_bar']}")
    print(f"fee {config['execution']['fee_rate'] * 100:.4f}% per side, "
          f"slippage {config['execution']['slippage'] * 100:.4f}% per fill, funding not modelled")
    header = f"{'':14}{'trades':>7}{'win %':>8}{'avg R':>8}{'t-stat':>8}{'return %':>10}{'max DD %':>10}"
    print(header)
    for label in ("in_sample", "out_of_sample"):
        m = document[label]["metrics"]
        cells = [m["win_rate_pct"], m["avg_r"], m["t_stat"], m["total_return_pct"], m["max_drawdown_pct"]]
        text = [f"{'n/a' if v is None else format(v, '.2f'):>{w}}" for v, w in zip(cells, (8, 8, 8, 10, 10))]
        print(f"{label:14}{m['trades']:>7}" + "".join(text))
    for warning in document["warnings"]:
        print(f"warning: {warning}")
    print(f"wrote {args.out}")
    return 0


def _cmd_live(args: argparse.Namespace) -> int:
    # Imported here so that `download` and `backtest` never load the order code.
    from .live import (BybitClient, Halted, KeyPermissionError, LiveConfigError, LiveRunner, StartupError,
                       TestnetOnlyError, load_live_config, read_credentials)
    from .live.bybit import MAINNET_REFUSAL, BybitError, NetworkError

    if args.mainnet:
        print(f"error: {MAINNET_REFUSAL}", file=sys.stderr)
        return 1
    try:
        params, config = load_live_config(args.config, args.strategy)
        interval_ms(args.interval)
        if args.dry_run:
            client = BybitClient(recv_window_ms=config.recv_window_ms)   # no key: cannot sign anything
        else:
            key, secret = read_credentials()
            client = BybitClient(key, secret, recv_window_ms=config.recv_window_ms, broker_id=config.broker_id)
        runner = LiveRunner(args.strategy, params, config, args.symbol, args.interval, client, args.dry_run)
        runner.start(clear_halt=args.clear_halt)
        runner.run(once=args.once)
    except Halted as exc:
        print(f"HALTED: {exc}", file=sys.stderr)
        print("Any open position keeps the stop-loss that is on Bybit. Nothing else was closed.", file=sys.stderr)
        return 3
    except (LiveConfigError, KeyPermissionError, StartupError, TestnetOnlyError, BybitError, NetworkError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stopped by user. Any open position keeps its stop-loss on Bybit.", file=sys.stderr)
        return 0
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perpmath",
        description="Backtest crypto perpetual-futures strategies on Bybit public data, and run them "
                    "on the Bybit testnet (0.2: testnet only, never mainnet).",
    )
    parser.add_argument("--version", action="version", version=f"perpmath {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    dl = sub.add_parser("download", help="download Bybit USDT-perpetual candles to the local cache")
    dl.add_argument("symbol", help="for example BTCUSDT")
    dl.add_argument("interval", choices=list(INTERVALS))
    dl.add_argument("--days", type=int, default=365, help="days back from now (default 365)")
    dl.add_argument("--start", help="YYYY-MM-DD (UTC); use with --end for an exact, repeatable range")
    dl.add_argument("--end", help="YYYY-MM-DD (UTC), exclusive")
    dl.add_argument("--data-dir", default="data")
    dl.set_defaults(func=_cmd_download)

    bt = sub.add_parser("backtest", help="run one strategy with one config and write a JSON result")
    bt.add_argument("strategy", choices=sorted(STRATEGIES))
    bt.add_argument("--symbol", required=True)
    bt.add_argument("--interval", required=True)
    bt.add_argument("--config", required=True, help="JSON file with params, execution and split")
    bt.add_argument("--out", required=True, help="where to write the JSON result")
    bt.add_argument("--start", help="use only bars from this date (YYYY-MM-DD, UTC)")
    bt.add_argument("--end", help="use only bars before this date (YYYY-MM-DD, UTC)")
    bt.add_argument("--data-dir", default="data")
    bt.set_defaults(func=_cmd_backtest)

    lv = sub.add_parser("live", help="run a strategy on the Bybit TESTNET (or --dry-run without any key)")
    lv.add_argument("strategy", choices=sorted(STRATEGIES))
    lv.add_argument("--symbol", required=True, help="USDT perpetual, for example BTCUSDT")
    lv.add_argument("--interval", required=True, choices=list(INTERVALS))
    lv.add_argument("--config", required=True, help="JSON file with params and a 'live' section")
    lv.add_argument("--dry-run", action="store_true",
                    help="no key, no signed request, no order: print the orders it would send")
    lv.add_argument("--once", action="store_true", help="handle the latest closed bar, then exit")
    lv.add_argument("--clear-halt", action="store_true",
                    help="clear a halt recorded by a circuit breaker (after you have checked why)")
    lv.add_argument("--mainnet", action="store_true", help=argparse.SUPPRESS)
    lv.set_defaults(func=_cmd_live)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (DataError, ConfigError, LookaheadError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
