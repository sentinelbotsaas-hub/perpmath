"""Funding-rate downloader and coverage check against a fake Bybit endpoint. No network."""

import numpy as np
import pytest

from perpmath.data import DataError
from perpmath.funding import (BYBIT_FUNDING_URL, FundingRates, check_coverage, download_funding,
                              fetch_funding, load_funding)

EIGHT_H = 8 * 3_600_000
F0 = 1_700_006_400_000 // EIGHT_H * EIGHT_H


class FakeFunding:
    """Newest first, at most ``limit`` records with startTime <= time <= endTime, like the real endpoint."""

    def __init__(self, count, symbol="BTCUSDT"):
        self.times = [F0 + i * EIGHT_H for i in range(count)]
        self.symbol = symbol
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        start, end, limit = int(params["startTime"]), int(params["endTime"]), int(params["limit"])
        hits = sorted((t for t in self.times if start <= t <= end), reverse=True)[:limit]
        return {"retCode": 0, "retMsg": "OK", "time": 0, "result": {"category": "linear", "list": [
            {"symbol": self.symbol, "fundingRate": f"{0.0001 * (1 + (t - F0) // EIGHT_H % 3)}",
             "fundingRateTimestamp": str(t)} for t in hits]}}


def test_pages_backwards_until_the_range_is_covered():
    fake = FakeFunding(450)
    rows = fetch_funding("BTCUSDT", F0, F0 + 450 * EIGHT_H, fetch=fake, pause=0)
    assert [r[0] for r in rows] == fake.times
    assert len(fake.calls) == 3                                   # 200 + 200 + 50
    for url, params in fake.calls:
        assert url == BYBIT_FUNDING_URL
        assert set(params) == {"category", "symbol", "startTime", "endTime", "limit"}   # no key
        assert params["category"] == "linear" and params["limit"] == "200"


def test_only_the_requested_range_is_kept(tmp_path):
    fake = FakeFunding(100)
    path, rows = download_funding("BTCUSDT", F0 + 10 * EIGHT_H, F0 + 20 * EIGHT_H, data_dir=str(tmp_path),
                                  fetch=fake)
    assert [r[0] for r in rows] == fake.times[10:20]
    loaded = load_funding(path)
    assert loaded.ts.tolist() == fake.times[10:20]
    assert loaded.rate[0] == pytest.approx(0.0002)


@pytest.mark.parametrize("payload, message", [
    ({"retCode": 10001, "retMsg": "bad"}, "retCode"),
    ({"retCode": 0, "result": {}}, "result.list"),
    ({"retCode": 0, "result": {"list": [{"symbol": "ETHUSDT", "fundingRate": "0.0001",
                                         "fundingRateTimestamp": str(F0)}]}}, "expected 'BTCUSDT'"),
    ({"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "fundingRate": "abc",
                                         "fundingRateTimestamp": str(F0)}]}}, "malformed"),
    ({"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "fundingRate": "0.5",
                                         "fundingRateTimestamp": str(F0)}]}}, "implausible"),
    ({"retCode": 0, "result": {"list": []}}, "no funding records"),
])
def test_bad_responses_raise(payload, message):
    with pytest.raises(DataError, match=message):
        fetch_funding("BTCUSDT", F0, F0 + EIGHT_H, fetch=lambda url, params: payload, pause=0)


def test_missing_file_explains_how_to_get_it(tmp_path):
    with pytest.raises(DataError, match="perpmath funding"):
        load_funding(tmp_path / "BTCUSDT-funding.csv")


def test_coverage_check_finds_holes():
    times = np.array([F0 + i * EIGHT_H for i in range(10)])
    rates = FundingRates(times, np.zeros(10))
    assert check_coverage(rates, F0 - 3_600_000, F0 + 9 * EIGHT_H + 3_600_000) == 10
    holed = FundingRates(np.delete(times, 4), np.zeros(9))
    with pytest.raises(DataError, match="gap of 16.0 h"):
        check_coverage(holed, F0, F0 + 9 * EIGHT_H)
    with pytest.raises(DataError, match="gap"):
        check_coverage(rates, F0, F0 + 20 * EIGHT_H)              # bars run past the last record
