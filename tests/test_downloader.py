"""Downloader pagination and failure handling against a fake Bybit endpoint. No network."""

import pytest

from perpmath import data
from perpmath.cli import main
from perpmath.data import BYBIT_KLINE_URL, DataError, download, fetch_klines, load_bars

STEP = 900_000
T0 = 1_700_000_100_000 // STEP * STEP


class FakeBybit:
    """Serves candles T0, T0+STEP, ... the way the real endpoint does:
    newest first, at most ``limit`` rows, only rows with start <= time <= end."""

    def __init__(self, bars, now_ms=None):
        self.times = [T0 + i * STEP for i in range(bars)]
        self.now_ms = now_ms if now_ms is not None else self.times[-1] + STEP + 1
        self.calls = []

    def row(self, ts):
        price = 100 + (ts - T0) / STEP * 0.01
        return [str(ts), f"{price:.2f}", f"{price + 1:.2f}", f"{price - 1:.2f}", f"{price + 0.5:.2f}", "12.5", "1250"]

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        start, end, limit = int(params["start"]), int(params["end"]), int(params["limit"])
        hits = [t for t in self.times if start <= t <= end]
        page = sorted(hits, reverse=True)[:limit]
        return {"retCode": 0, "retMsg": "OK", "result": {"list": [self.row(t) for t in page]},
                "time": self.now_ms}


def test_pages_backwards_until_the_range_is_covered():
    fake = FakeBybit(bars=2500)
    rows = fetch_klines("BTCUSDT", "15m", T0, T0 + 2500 * STEP, fetch=fake, pause=0)

    assert len(fake.calls) == 3                                   # 1000 + 1000 + 500
    assert [r[0] for r in rows] == fake.times                     # complete, oldest first, no duplicates
    ends = [int(params["end"]) for _, params in fake.calls]
    assert ends == [T0 + 2499 * STEP, T0 + 1499 * STEP, T0 + 499 * STEP]
    for url, params in fake.calls:
        assert url == BYBIT_KLINE_URL
        assert params["category"] == "linear" and params["symbol"] == "BTCUSDT"
        assert params["interval"] == "15" and params["limit"] == "1000"
        assert set(params) == {"category", "symbol", "interval", "start", "end", "limit"}   # no key, no signature


def test_exactly_one_full_page_stops_without_a_wasted_request():
    fake = FakeBybit(bars=1000)
    rows = fetch_klines("BTCUSDT", "15m", T0, T0 + 1000 * STEP, fetch=fake, pause=0)
    assert len(rows) == 1000 and len(fake.calls) == 1


def test_only_the_requested_range_is_returned():
    fake = FakeBybit(bars=3000)
    rows = fetch_klines("BTCUSDT", "15m", T0 + 100 * STEP, T0 + 1300 * STEP, fetch=fake, pause=0)
    assert (rows[0][0], rows[-1][0], len(rows)) == (T0 + 100 * STEP, T0 + 1299 * STEP, 1200)


def test_candle_that_is_still_forming_is_dropped():
    fake = FakeBybit(bars=50, now_ms=T0 + 49 * STEP + 60_000)     # one minute into the last bar
    rows = fetch_klines("BTCUSDT", "15m", T0, T0 + 50 * STEP, fetch=fake, pause=0)
    assert len(rows) == 49 and rows[-1][0] == T0 + 48 * STEP


def test_symbol_with_less_history_than_requested_returns_what_exists():
    fake = FakeBybit(bars=1500)
    rows = fetch_klines("NEWUSDT", "15m", T0 - 5000 * STEP, T0 + 1500 * STEP, fetch=fake, pause=0)
    assert len(rows) == 1500 and len(fake.calls) == 2


def test_empty_response_fails():
    with pytest.raises(DataError, match="no candles"):
        fetch_klines("BTCUSDT", "15m", T0 - 100 * STEP, T0 - 10 * STEP, fetch=FakeBybit(bars=10), pause=0)


def test_only_a_forming_candle_fails():
    fake = FakeBybit(bars=1, now_ms=T0 + 1000)
    with pytest.raises(DataError, match="no closed candles"):
        fetch_klines("BTCUSDT", "15m", T0, T0 + STEP, fetch=fake, pause=0)


def good_row(ts=T0):
    return [str(ts), "100", "101", "99", "100.5", "12.5", "1250"]


@pytest.mark.parametrize("payload", [
    None,
    [],
    {"retCode": 10001, "retMsg": "params error: Symbol Is Invalid", "result": {}, "time": T0},
    {"retCode": 0, "result": {}, "time": T0},
    {"retCode": 0, "result": {"list": "nope"}, "time": T0},
    {"retCode": 0, "result": {"list": [good_row()[:5]]}, "time": T0 + 2 * STEP},          # short row
    {"retCode": 0, "result": {"list": [["x"] + good_row()[1:]]}, "time": T0 + 2 * STEP},  # bad time
    {"retCode": 0, "result": {"list": [[str(T0), "100", "abc", "99", "100", "1", "1"]]}, "time": T0 + 2 * STEP},
    {"retCode": 0, "result": {"list": [[str(T0), "100", "99", "101", "100", "1", "1"]]}, "time": T0 + 2 * STEP},  # high < low
    {"retCode": 0, "result": {"list": [[str(T0), "100", "nan", "99", "100", "1", "1"]]}, "time": T0 + 2 * STEP},
    {"retCode": 0, "result": {"list": [[str(T0), "0", "0", "0", "0", "1", "1"]]}, "time": T0 + 2 * STEP},         # zero price
    {"retCode": 0, "result": {"list": [[str(T0 + 1), "100", "101", "99", "100", "1", "1"]]}, "time": T0 + 2 * STEP},  # misaligned
    {"retCode": 0, "result": {"list": [good_row(T0 - 50 * STEP)]}, "time": T0 + 2 * STEP},  # outside the range
    {"retCode": 0, "result": {"list": [good_row()]}},                                       # no server time
])
def test_malformed_responses_fail(payload):
    with pytest.raises(DataError):
        fetch_klines("BTCUSDT", "15m", T0, T0 + STEP, fetch=lambda url, params: payload, pause=0)


def test_unsupported_interval_fails():
    with pytest.raises(DataError, match="unsupported interval"):
        fetch_klines("BTCUSDT", "7m", T0, T0 + STEP, fetch=FakeBybit(bars=1), pause=0)


def test_download_writes_a_csv_that_loads_back(tmp_path):
    path, rows = download("BTCUSDT", "15m", T0, T0 + 1200 * STEP, data_dir=str(tmp_path), fetch=FakeBybit(bars=1200))
    assert path == tmp_path / "BTCUSDT-15m.csv"
    bars = load_bars(path)
    assert len(bars) == 1200 == len(rows)
    assert bars.ts[0] == T0 and bars.ts[-1] == T0 + 1199 * STEP
    assert bars.open[0] == 100.0 and bars.high[0] == 101.0 and bars.low[0] == 99.0 and bars.close[0] == 100.5
    assert data.count_missing_bars(bars, STEP) == 0
    part = load_bars(path, start_ms=T0 + 10 * STEP, end_ms=T0 + 20 * STEP)
    assert len(part) == 10 and part.ts[0] == T0 + 10 * STEP


def test_load_rejects_missing_and_broken_files(tmp_path):
    with pytest.raises(DataError, match="perpmath download"):
        load_bars(tmp_path / "nope.csv")
    bad = tmp_path / "bad.csv"
    bad.write_text("time,open\n1,2\n", encoding="ascii")
    with pytest.raises(DataError, match="header"):
        load_bars(bad)
    bad.write_text(",".join(data.CSV_HEADER) + "\n900000,1,2,0.5,1,1,1\n0,1,2,0.5,1,1,1\n", encoding="ascii")
    with pytest.raises(DataError, match="strictly increasing"):
        load_bars(bad)


def test_cli_download_exits_non_zero_on_bad_data(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(data, "http_get_json", lambda url, params: {"retCode": 0, "result": {"list": []}, "time": T0})
    code = main(["download", "BTCUSDT", "15m", "--days", "1", "--data-dir", str(tmp_path)])
    assert code == 1
    assert "error:" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []                         # nothing half-written
