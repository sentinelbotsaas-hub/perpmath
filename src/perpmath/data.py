"""Bybit public kline downloader and the local CSV cache.

Uses only the public market endpoint (no account, no key):

    GET https://api.bybit.com/v5/market/kline?category=linear&...

Bybit returns at most 1000 candles per request, newest first, so the
downloader walks backwards from the end of the requested range. Anything
empty or malformed raises ``DataError``; the CLI turns that into a non-zero
exit code.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"
CATEGORY = "linear"
PAGE_LIMIT = 1000

# interval name -> (Bybit API code, bar length in milliseconds)
INTERVALS: Dict[str, Tuple[str, int]] = {
    "1m": ("1", 60_000),
    "3m": ("3", 180_000),
    "5m": ("5", 300_000),
    "15m": ("15", 900_000),
    "30m": ("30", 1_800_000),
    "1h": ("60", 3_600_000),
    "2h": ("120", 7_200_000),
    "4h": ("240", 14_400_000),
    "6h": ("360", 21_600_000),
    "12h": ("720", 43_200_000),
    "1d": ("D", 86_400_000),
}

CSV_HEADER = ["open_time_ms", "open", "high", "low", "close", "volume", "turnover"]

Row = Tuple[int, str, str, str, str, str, str]
Fetch = Callable[[str, Dict[str, str]], dict]


class DataError(Exception):
    """Market data is missing, empty or malformed."""


@dataclass(frozen=True)
class Bars:
    """OHLCV bars, oldest first. ``ts`` is the bar open time in UTC milliseconds."""

    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray

    def __len__(self) -> int:
        return len(self.ts)

    def head(self, k: int) -> "Bars":
        """The first ``k`` bars."""
        return Bars(self.ts[:k], self.open[:k], self.high[:k],
                    self.low[:k], self.close[:k], self.volume[:k])


def interval_ms(interval: str) -> int:
    if interval not in INTERVALS:
        raise DataError(f"unsupported interval {interval!r}; choose one of {', '.join(INTERVALS)}")
    return INTERVALS[interval][1]


def iso_utc(ms: int) -> str:
    """Milliseconds since the epoch -> '2026-01-31T00:00:00Z'."""
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date_ms(text: str) -> int:
    """'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SSZ' (UTC) -> milliseconds since the epoch."""
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    raise DataError(f"cannot parse date {text!r}; use YYYY-MM-DD (UTC)")


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #

def http_get_json(url: str, params: Dict[str, str], timeout: float = 20.0, retries: int = 3) -> dict:
    """GET ``url`` with query ``params`` and decode JSON. Retries network errors only."""
    full = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(full, headers={"User-Agent": "perpmath"})
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
            break
        except urllib.error.HTTPError as exc:
            raise DataError(f"HTTP {exc.code} from {url}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(1.0 + attempt)
    else:
        raise DataError(f"network error calling {url}: {last_error}")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise DataError(f"response from {url} is not JSON") from exc


def _parse_page(payload: object, step: int) -> List[Row]:
    """Validate one API response and return its rows (any order)."""
    if not isinstance(payload, dict):
        raise DataError("malformed response: not a JSON object")
    if payload.get("retCode") != 0:
        raise DataError(f"Bybit error retCode={payload.get('retCode')!r} retMsg={payload.get('retMsg')!r}")
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("list"), list):
        raise DataError("malformed response: missing result.list")
    rows: List[Row] = []
    for raw in result["list"]:
        if not isinstance(raw, (list, tuple)) or len(raw) < 7:
            raise DataError(f"malformed candle row: {raw!r}")
        try:
            ts = int(raw[0])
            o, h, l, c, v = (float(x) for x in raw[1:6])
            float(raw[6])
        except (TypeError, ValueError) as exc:
            raise DataError(f"malformed candle row: {raw!r}") from exc
        if ts % step != 0:
            raise DataError(f"candle time {ts} is not aligned to the interval")
        if not all(np.isfinite(x) for x in (o, h, l, c, v)):
            raise DataError(f"non-finite value in candle row: {raw!r}")
        if min(o, h, l, c) <= 0 or v < 0 or h < max(o, c, l) or l > min(o, c, h):
            raise DataError(f"inconsistent candle row: {raw!r}")
        rows.append((ts, *(str(x) for x in raw[1:7])))
    return rows


def fetch_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    fetch: Optional[Fetch] = None,
    pause: float = 0.1,
) -> List[Row]:
    """Download closed candles whose open time is in ``[start_ms, end_ms)``.

    Pages backwards (newest page first). The candle that is still forming is
    never returned: a bar is kept only if it had already closed according to
    the exchange's own clock in the response.
    """
    fetch = fetch or http_get_json
    step = interval_ms(interval)
    code = INTERVALS[interval][0]
    start_ms = -(-start_ms // step) * step          # round up to a bar boundary
    cursor = (end_ms - 1) // step * step            # last bar open time wanted
    if cursor < start_ms:
        raise DataError("empty date range")

    collected: Dict[int, Row] = {}
    first_page = True
    while cursor >= start_ms:
        payload = fetch(BYBIT_KLINE_URL, {
            "category": CATEGORY,
            "symbol": symbol,
            "interval": code,
            "start": str(start_ms),
            "end": str(cursor),
            "limit": str(PAGE_LIMIT),
        })
        rows = _parse_page(payload, step)
        if not rows:
            if first_page:
                raise DataError(f"Bybit returned no candles for {symbol} {interval} in the requested range")
            break  # reached the start of the symbol's history
        first_page = False

        server_ms = payload.get("time")
        if not isinstance(server_ms, int):
            raise DataError("malformed response: missing server time")
        oldest = min(r[0] for r in rows)
        newest = max(r[0] for r in rows)
        if oldest < start_ms or newest > cursor:
            raise DataError("response contains candles outside the requested range")
        for row in rows:
            if row[0] + step > server_ms:
                continue  # still forming
            if row[0] in collected and collected[row[0]] != row:
                raise DataError(f"conflicting duplicate candle at {row[0]}")
            collected[row[0]] = row

        if len(rows) < PAGE_LIMIT:
            break  # nothing older inside the range
        cursor = oldest - step
        if pause:
            time.sleep(pause)

    if not collected:
        raise DataError(f"no closed candles for {symbol} {interval} in the requested range")
    return [collected[ts] for ts in sorted(collected)]


def cache_path(data_dir: str, symbol: str, interval: str) -> Path:
    return Path(data_dir) / f"{symbol}-{interval}.csv"


def write_csv(path: Path, rows: List[Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="ascii") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(CSV_HEADER)
        writer.writerows(rows)
    os.replace(tmp, path)


def download(symbol: str, interval: str, start_ms: int, end_ms: int,
             data_dir: str = "data", fetch: Optional[Fetch] = None) -> Tuple[Path, List[Row]]:
    """Download ``[start_ms, end_ms)`` and replace the cached CSV for this symbol/interval."""
    interval_ms(interval)
    rows = fetch_klines(symbol, interval, start_ms, end_ms, fetch=fetch)
    path = cache_path(data_dir, symbol, interval)
    write_csv(path, rows)
    return path, rows


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #

def load_bars(path, start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> Bars:
    """Read a cached CSV. Optionally keep only bars with open time in ``[start_ms, end_ms)``."""
    path = Path(path)
    if not path.is_file():
        raise DataError(f"no data file at {path}; run `perpmath download` first")
    columns: List[List[float]] = [[] for _ in range(6)]
    with open(path, newline="", encoding="ascii") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header != CSV_HEADER:
            raise DataError(f"{path}: unexpected CSV header {header!r}")
        for line_no, row in enumerate(reader, start=2):
            try:
                ts = int(row[0])
                values = [float(x) for x in row[1:6]]
            except (IndexError, ValueError) as exc:
                raise DataError(f"{path}:{line_no}: malformed row") from exc
            if start_ms is not None and ts < start_ms:
                continue
            if end_ms is not None and ts >= end_ms:
                continue
            columns[0].append(ts)
            for col, value in zip(columns[1:], values):
                col.append(value)
    if not columns[0]:
        raise DataError(f"{path}: no bars in the requested range")
    ts_arr = np.asarray(columns[0], dtype=np.int64)
    if len(ts_arr) > 1 and not np.all(np.diff(ts_arr) > 0):
        raise DataError(f"{path}: bar times are not strictly increasing")
    o, h, l, c, v = (np.asarray(col, dtype=np.float64) for col in columns[1:])
    return Bars(ts_arr, o, h, l, c, v)


def count_missing_bars(bars: Bars, step: int) -> int:
    """How many bars are missing between the first and last bar for a fixed ``step``."""
    if len(bars) < 2:
        return 0
    expected = int((bars.ts[-1] - bars.ts[0]) // step) + 1
    return expected - len(bars)


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
