"""Smoke test against the real Bybit TESTNET, public endpoints only. No key needed.

Skipped by default. Run it with:

    PERPMATH_TESTNET_SMOKE=1 pytest tests/test_live_testnet_smoke.py -v

It calls /v5/market/time, /v5/market/instruments-info and /v5/market/kline on
https://api-testnet.bybit.com and checks the answers have the shape the
runner relies on. It sends no signed request and places no order.
"""

import os

import pytest

from perpmath.live import bybit
from perpmath.live.config import LiveConfig
from perpmath.live.executor import LiveRunner

pytestmark = pytest.mark.skipif(os.environ.get("PERPMATH_TESTNET_SMOKE") != "1",
                                reason="network test; set PERPMATH_TESTNET_SMOKE=1 to run")


def test_public_endpoints_on_the_testnet(tmp_path):
    sent = []

    def recording(method, url, headers, body):
        sent.append((method, url, dict(headers)))
        return bybit.urllib_transport(method, url, headers, body)

    client = bybit.BybitClient(transport=recording)          # no key: cannot sign
    offset = client.sync_time()
    assert abs(offset) < 5_000, f"local clock is {offset} ms off Bybit's"

    runner = LiveRunner("ma_cross", {"fast_period": 20, "slow_period": 50, "atr_period": 14, "stop_atr_mult": 2.0},
                        LiveConfig(state_dir=str(tmp_path)), "BTCUSDT", "15m", client, dry_run=True,
                        log=lambda line: None)
    runner.start()
    assert runner.instrument.qty_step > 0 and runner.instrument.tick_size > 0
    bars = runner.fetch_closed_bars()
    assert len(bars) >= 100
    assert int(bars.ts[-1]) + runner.step <= client.now_ms()             # the last bar is closed
    print(f"offset {offset} ms, {len(bars)} bars, last {bars.ts[-1]}, gap: {runner.bars_problem(bars)}")

    assert sent and all(url.startswith(bybit.TESTNET_REST + "/v5/market/") for _, url, _ in sent)
    assert not any(h.startswith("X-BAPI") for _, _, headers in sent for h in headers)
