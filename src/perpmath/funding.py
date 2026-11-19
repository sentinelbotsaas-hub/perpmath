"""Bybit public funding-rate history: downloader and local CSV cache.

Uses only the public market endpoint (no account, no key):

    GET https://api.bybit.com/v5/market/funding/history?category=linear&...

Bybit returns at most 200 records per request, newest first, so the
downloader walks backwards from the end of the requested range, like the
candle downloader in ``data.py``. Anything empty or malformed raises
``DataError``.

A record is ``(funding_time_ms, rate)``. At that moment a position with
signed quantity ``q`` (positive = long) at price ``p`` pays
``q x p x rate``: longs pay shorts when the rate is positive, shorts pay
longs when it is negative. BTCUSDT settles every 8 hours (00:00, 08:00 and
16:00 UTC), but the code uses the timestamps Bybit returns rather than
assuming a schedule.
"""

from __future__ import annotations

import csv
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from .data import CATEGORY, DataError, Fetch, http_get_json, iso_utc

BYBIT_FUNDING_URL = "https://api.bybit.com/v5/market/funding/history"
FUNDING_PAGE_LIMIT = 200
FUNDING_CSV_HEADER = ["funding_time_ms", "funding_rate"]
EIGHT_HOURS_MS = 8 * 3_600_000
# Sanity bound only: Bybit caps BTCUSDT funding far below this. A larger value
# means the response was misread (for example a percentage instead of a fraction).
MAX_ABS_RATE = 0.05

FundingRow = Tuple[int, str]


@dataclass(frozen=True)
class FundingRates:
    """Funding settlements, oldest first. ``ts`` in UTC milliseconds, ``rate`` as a fraction."""

    ts: np.ndarray
    rate: np.ndarray

    def __len__(self) -> int:
        return len(self.ts)


def _parse_funding_page(payload: object, symbol: str) -> List[FundingRow]:
    if not isinstance(payload, dict):
        raise DataError("malformed funding response: not a JSON object")
    if payload.get("retCode") != 0:
        raise DataError(f"Bybit error retCode={payload.get('retCode')!r} retMsg={payload.get('retMsg')!r}")
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("list"), list):
        raise DataError("malformed funding response: missing result.list")
    rows: List[FundingRow] = []
    for raw in result["list"]:
        if not isinstance(raw, dict):
            raise DataError(f"malformed funding record: {raw!r}")
        if raw.get("symbol") != symbol:
            raise DataError(f"funding record for {raw.get('symbol')!r}, expected {symbol!r}")
        try:
            ts = int(raw["fundingRateTimestamp"])
            rate = float(raw["fundingRate"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DataError(f"malformed funding record: {raw!r}") from exc
        if not math.isfinite(rate) or abs(rate) > MAX_ABS_RATE:
            raise DataError(f"implausible funding rate in record: {raw!r}")
        if ts % 60_000 != 0:
            raise DataError(f"funding time {ts} is not on a whole minute")
        rows.append((ts, str(raw["fundingRate"])))
    return rows


def fetch_funding(symbol: str, start_ms: int, end_ms: int,
                  fetch: Optional[Fetch] = None, pause: float = 0.1) -> List[FundingRow]:
    """Download funding settlements with time in ``[start_ms, end_ms)``, oldest first."""
    fetch = fetch or http_get_json
    if end_ms <= start_ms:
        raise DataError("empty date range")
    cursor = end_ms - 1
    collected: Dict[int, FundingRow] = {}
    first_page = True
    while cursor >= start_ms:
        payload = fetch(BYBIT_FUNDING_URL, {
            "category": CATEGORY,
            "symbol": symbol,
            "startTime": str(start_ms),
            "endTime": str(cursor),
            "limit": str(FUNDING_PAGE_LIMIT),
        })
        rows = _parse_funding_page(payload, symbol)
        if not rows:
            if first_page:
                raise DataError(f"Bybit returned no funding records for {symbol} in the requested range")
            break
        first_page = False
        oldest = min(r[0] for r in rows)
        newest = max(r[0] for r in rows)
        if oldest < start_ms or newest > cursor:
            raise DataError("funding response contains records outside the requested range")
        for row in rows:
            if row[0] in collected and collected[row[0]] != row:
                raise DataError(f"conflicting duplicate funding record at {row[0]}")
            collected[row[0]] = row
        if len(rows) < FUNDING_PAGE_LIMIT:
            break
        cursor = oldest - 1
        if pause:
            time.sleep(pause)
    return [collected[ts] for ts in sorted(collected)]


def funding_cache_path(data_dir: str, symbol: str) -> Path:
    return Path(data_dir) / f"{symbol}-funding.csv"


def write_funding_csv(path: Path, rows: List[FundingRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="ascii") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(FUNDING_CSV_HEADER)
        writer.writerows(rows)
    os.replace(tmp, path)


def download_funding(symbol: str, start_ms: int, end_ms: int, data_dir: str = "data",
                     fetch: Optional[Fetch] = None) -> Tuple[Path, List[FundingRow]]:
    """Download ``[start_ms, end_ms)`` and replace the cached funding CSV for this symbol."""
    rows = fetch_funding(symbol, start_ms, end_ms, fetch=fetch)
    path = funding_cache_path(data_dir, symbol)
    write_funding_csv(path, rows)
    return path, rows


def load_funding(path) -> FundingRates:
    path = Path(path)
    if not path.is_file():
        raise DataError(f"no funding file at {path}; run `perpmath funding` first, "
                        f"or set \"funding\": false in the config")
    ts: List[int] = []
    rates: List[float] = []
    with open(path, newline="", encoding="ascii") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header != FUNDING_CSV_HEADER:
            raise DataError(f"{path}: unexpected CSV header {header!r}")
        for line_no, row in enumerate(reader, start=2):
            try:
                ts.append(int(row[0]))
                rates.append(float(row[1]))
            except (IndexError, ValueError) as exc:
                raise DataError(f"{path}:{line_no}: malformed row") from exc
    if not ts:
        raise DataError(f"{path}: no funding records")
    ts_arr = np.asarray(ts, dtype=np.int64)
    if len(ts_arr) > 1 and not np.all(np.diff(ts_arr) > 0):
        raise DataError(f"{path}: funding times are not strictly increasing")
    return FundingRates(ts_arr, np.asarray(rates, dtype=np.float64))


def check_coverage(funding: FundingRates, start_ms: int, end_ms: int,
                   max_gap_ms: int = EIGHT_HOURS_MS) -> int:
    """Raise ``DataError`` unless funding records cover ``[start_ms, end_ms)`` without holes.

    Covered means: a record within ``max_gap_ms`` of the start, of the end, and
    of each other. Returns the number of records inside the window.
    """
    inside = funding.ts[(funding.ts >= start_ms) & (funding.ts < end_ms)]
    if end_ms - start_ms <= max_gap_ms and len(inside) == 0:
        return 0                                   # window shorter than one funding interval
    if len(inside) == 0:
        raise DataError(f"no funding records between {iso_utc(start_ms)} and {iso_utc(end_ms)}")
    points = np.concatenate(([start_ms], inside, [end_ms]))
    gaps = np.diff(points)
    worst = int(np.argmax(gaps))
    if gaps[worst] > max_gap_ms:
        raise DataError(
            f"funding history has a gap of {gaps[worst] / 3_600_000:.1f} h after "
            f"{iso_utc(int(points[worst]))}; download funding for the whole bar range"
        )
    return len(inside)
