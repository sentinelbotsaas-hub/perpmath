"""A fake Bybit V5 testnet for the live-runner tests. No network.

It answers the endpoints the runner uses, checks every signature with the
fake secret, and records every request so tests can assert what was sent.
The behaviour it imitates is taken from the Bybit V5 docs; it is a model of
the docs, not of the real exchange.
"""

import json
import urllib.parse
from decimal import Decimal

import numpy as np

from conftest import random_walk
from perpmath.engine import Signals
from perpmath.live import bybit
from perpmath.live.config import LiveConfig
from perpmath.live.executor import LiveRunner

# Obviously fake credentials. Nothing like a real Bybit key.
FAKE_KEY = "FAKE-KEY-FOR-TESTS-0001"
FAKE_SECRET = "FAKE-SECRET-FOR-TESTS-DO-NOT-USE-0001"

STEP = 900_000
SYMBOL = "BTCUSDT"

INSTRUMENT = {
    "symbol": SYMBOL, "status": "Trading", "contractType": "LinearPerpetual", "settleCoin": "USDT",
    "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "100",
                      "maxMktOrderQty": "50", "minNotionalValue": "5"},
    "priceFilter": {"tickSize": "0.01", "minPrice": "0.01", "maxPrice": "1000000"},
}

GOOD_KEY_INFO = {
    "apiKey": FAKE_KEY, "readOnly": 0, "secret": "", "ips": ["203.0.113.7"],
    "permissions": {"ContractTrade": ["Order", "Position"], "Spot": [], "Wallet": [], "Options": [],
                    "Derivatives": [], "Exchange": [], "Earn": [], "NFT": [], "CopyTrading": []},
}


class FakeBybit:
    def __init__(self, n_bars=600, settle_ms=5_000):
        walk = random_walk(n_bars)
        self.ts = [int(t) for t in walk.ts]
        self.ohlc = np.column_stack([walk.open, walk.high, walk.low, walk.close])
        self.local_ms = self.ts[-1] + STEP + settle_ms     # local clock, 5 s after the last close
        self.skew_ms = 150                                # server = local + skew
        self.requests = []
        self.key_info = json.loads(json.dumps(GOOD_KEY_INFO))
        self.equity = 1000.0
        self.position = {"positionIdx": 0, "side": "", "size": "0", "avgPrice": "0", "stopLoss": "",
                         "takeProfit": "", "positionValue": "0"}
        self.extra_positions = []
        self.orders = {}           # orderLinkId -> order
        self.open_orders = []      # unrelated open orders
        self.fill_ratio = Decimal(1)
        self.create_error = []     # queue of (retCode, msg) to answer /v5/order/create with
        self.create_timeout = []   # queue of bools: True = create the order, then raise a timeout
        self.create_lost = 0       # this many creates time out before the order exists
        self.next_kline_gap = False
        self.drop_stop_loss = False
        self.limit_headers = {}

    # -- clock --------------------------------------------------------------
    def clock(self):
        return self.local_ms / 1000

    def server_ms(self):
        return self.local_ms + self.skew_ms

    def add_bar(self):
        last_close = float(self.ohlc[-1, 3])
        row = [last_close, last_close * 1.001, last_close * 0.999, last_close * 1.0005]
        self.ts.append(self.ts[-1] + STEP)
        self.ohlc = np.vstack([self.ohlc, row])
        self.local_ms += STEP

    def last_close(self):
        return float(self.ohlc[-1, 3])

    # -- transport ------------------------------------------------------------
    def __call__(self, method, url, headers, body):
        parts = urllib.parse.urlsplit(url)
        assert parts.netloc == bybit.TESTNET_HOST, url
        query = dict(urllib.parse.parse_qsl(parts.query))
        payload = json.loads(body) if body else None
        self.requests.append({"method": method, "path": parts.path, "query": query, "body": payload,
                              "headers": dict(headers), "url": url})
        if "X-BAPI-SIGN" in headers:
            signed = parts.query if method == "GET" else body.decode("utf-8")
            expected = bybit.sign(FAKE_SECRET, bybit.signature_payload(
                int(headers["X-BAPI-TIMESTAMP"]), headers["X-BAPI-API-KEY"],
                int(headers["X-BAPI-RECV-WINDOW"]), signed))
            if headers["X-BAPI-API-KEY"] != FAKE_KEY or headers["X-BAPI-SIGN"] != expected:
                return self._reply(10004, "Error sign")
            ts = int(headers["X-BAPI-TIMESTAMP"])
            window = int(headers["X-BAPI-RECV-WINDOW"])
            if not self.server_ms() - window <= ts < self.server_ms() + 1000:   # rule from /docs/v5/guide
                return self._reply(10002, "The request time exceeds the time window range.")
        route = getattr(self, "_r" + parts.path.replace("/", "_").replace("-", "_"), None)
        if route is None:
            return 404, {}, b"not found"
        return route(query, payload)

    def _reply(self, code=0, msg="OK", result=None, headers=None):
        doc = {"retCode": code, "retMsg": msg, "result": result or {}, "retExtInfo": {}, "time": self.server_ms()}
        return 200, dict(headers or self.limit_headers), json.dumps(doc).encode()

    # -- public ----------------------------------------------------------------
    def _r_v5_market_time(self, q, b):
        s = self.server_ms()
        return self._reply(result={"timeSecond": str(s // 1000), "timeNano": str(s * 1_000_000)})

    def _r_v5_market_instruments_info(self, q, b):
        return self._reply(result={"category": "linear", "list": [INSTRUMENT] if q["symbol"] == SYMBOL else []})

    def _r_v5_market_kline(self, q, b):
        end, limit = int(q["end"]), int(q["limit"])
        rows = [(t, r) for t, r in zip(self.ts, self.ohlc) if t <= end]
        if self.next_kline_gap and len(rows) > 10:
            del rows[-5]
        rows = rows[-limit:][::-1]                       # newest first
        out = [[str(t), repr(float(r[0])), repr(float(r[1])), repr(float(r[2])), repr(float(r[3])), "1", "100"]
               for t, r in rows]
        return self._reply(result={"category": "linear", "symbol": q["symbol"], "list": out})

    # -- private ---------------------------------------------------------------
    def _r_v5_user_query_api(self, q, b):
        return self._reply(result=self.key_info)

    def _r_v5_account_wallet_balance(self, q, b):
        return self._reply(result={"list": [{"accountType": "UNIFIED", "coin": [
            {"coin": "USDT", "equity": str(self.equity), "walletBalance": str(self.equity)}]}]})

    def _r_v5_position_list(self, q, b):
        return self._reply(result={"category": "linear", "list": [dict(self.position, symbol=SYMBOL)]
                                   + self.extra_positions})

    def _r_v5_order_realtime(self, q, b):
        if "orderLinkId" in q:
            o = self.orders.get(q["orderLinkId"])
            return self._reply(result={"list": [o] if o else []})
        return self._reply(result={"list": list(self.open_orders)})

    def _r_v5_order_history(self, q, b):
        o = self.orders.get(q.get("orderLinkId"))
        return self._reply(result={"list": [o] if o else []})

    def _r_v5_order_create(self, q, body):
        if self.create_lost:
            self.create_lost -= 1
            raise TimeoutError("timed out before reaching the exchange (fake)")
        if self.create_error:
            code, msg = self.create_error.pop(0)
            return self._reply(code, msg)
        link = body["orderLinkId"]
        if link in self.orders:
            return self._reply(110072, "OrderLinkedID is duplicate")
        qty = Decimal(body["qty"])
        filled = (qty * self.fill_ratio).quantize(Decimal("0.001"))
        price = Decimal(repr(self.last_close()))
        self.orders[link] = {
            "orderId": f"fake-{len(self.orders) + 1}", "orderLinkId": link, "side": body["side"],
            "qty": body["qty"], "cumExecQty": str(filled), "leavesQty": "0", "avgPrice": str(price),
            "orderStatus": "Filled" if filled == qty else "PartiallyFilledCanceled",
            "stopOrderType": "", "reduceOnly": bool(body.get("reduceOnly")), "rejectReason": "EC_NoError",
        }
        self._apply_fill(body, filled, price)
        if self.create_timeout and self.create_timeout.pop(0):
            raise TimeoutError("timed out (fake)")
        return self._reply(result={"orderId": self.orders[link]["orderId"], "orderLinkId": link})

    def _apply_fill(self, body, filled, price):
        if filled == 0:
            return
        size = Decimal(self.position["size"])
        side = 1 if body["side"] == "Buy" else -1
        cur = {"Buy": 1, "Sell": -1}.get(self.position["side"], 0)
        if body.get("reduceOnly"):
            size = max(Decimal(0), size - filled)
        else:
            assert cur in (0, side)
            size += filled
            cur = side
            self.position["avgPrice"] = str(price)
            if not self.drop_stop_loss:
                self.position["stopLoss"] = body.get("stopLoss", "")
            self.position["takeProfit"] = body.get("takeProfit", "")
        self.position["size"] = str(size)
        self.position["side"] = "" if size == 0 else ("Buy" if cur == 1 else "Sell")
        self.position["positionValue"] = str(size * price)
        if size == 0:
            self.position.update(stopLoss="", takeProfit="", avgPrice="0")

    # -- helpers for tests -------------------------------------------------------
    def creates(self):
        return [r for r in self.requests if r["path"] == "/v5/order/create"]

    def signed_requests(self):
        return [r for r in self.requests if "X-BAPI-SIGN" in r["headers"]]


def signal_at(bar_ts, side, stop_frac=0.01, target_r=2.0, exit_long_at=(), exit_short_at=()):
    """A look-ahead-free stand-in strategy: an entry on the bar that opens at ``bar_ts``."""
    def generate(bars, params):
        s = Signals.empty(len(bars))
        for i, t in enumerate(bars.ts):
            if int(t) == bar_ts:
                s.side[i] = side
                s.stop[i] = float(bars.close[i]) * (1 - side * stop_frac)
                s.target_r[i] = target_r
            if int(t) in exit_long_at:
                s.exit_long[i] = True
            if int(t) in exit_short_at:
                s.exit_short[i] = True
        return s
    return generate


def make_runner(fake, tmp_path, dry_run=False, generate=None, logs=None, confirm="TESTNET", **live):
    config = LiveConfig(state_dir=str(tmp_path / "state"), **live)
    if dry_run:
        client = bybit.BybitClient(transport=fake, clock=fake.clock, sleep=lambda s: None)
    else:
        client = bybit.BybitClient(FAKE_KEY, FAKE_SECRET, transport=fake, clock=fake.clock,
                                   sleep=lambda s: None, broker_id=config.broker_id)
    logs = logs if logs is not None else []
    runner = LiveRunner("ma_cross", {"fast_period": 20, "slow_period": 50, "atr_period": 14, "stop_atr_mult": 2.0},
                        config, SYMBOL, "15m", client, dry_run, log=logs.append,
                        confirm=lambda prompt: confirm, sleep=lambda s: None)
    if generate is not None:
        runner.generate = generate
    return runner, logs
