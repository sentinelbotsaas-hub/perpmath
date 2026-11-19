"""Testnet runner: signing, safety defaults, breakers, idempotency, reconciliation, dry run.

Everything here runs against the fake in ``live_fakes.py``. No network, and no
real API key exists anywhere in these tests.
"""

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from perpmath.engine import Signals
from live_fakes import FAKE_KEY, FAKE_SECRET, GOOD_KEY_INFO, STEP, SYMBOL, FakeBybit, make_runner, signal_at
from perpmath.cli import main
from perpmath.live import bybit
from perpmath.live.breaker import Halted
from perpmath.live.config import LiveConfig, LiveConfigError, parse_live_config, read_credentials
from perpmath.live.executor import KeyPermissionError, StartupError, check_api_key, order_link_id, run_hash

LIVE_CONFIG = str(Path(__file__).resolve().parents[1] / "configs" / "live" / "ma_cross.json")


# --------------------------------------------------------------------------- #
# Signing (https://bybit-exchange.github.io/docs/v5/guide)
# --------------------------------------------------------------------------- #

def test_signature_payload_matches_the_bybit_docs_examples():
    # The two "string to sign" examples on the authentication page, character for character.
    assert bybit.signature_payload(1658384314791, "XXXXXXXXXX", 5000,
                                   "category=option&symbol=BTC-29JUL22-25000-C") == \
        "1658384314791XXXXXXXXXX5000category=option&symbol=BTC-29JUL22-25000-C"
    assert bybit.signature_payload(1658385579423, "XXXXXXXXXX", 5000, '{"category": "option"}') == \
        '1658385579423XXXXXXXXXX5000{"category": "option"}'


def test_hmac_is_sha256_lowercase_hex():
    # The docs' example signatures are made with a secret the page does not publish, so they cannot
    # be reproduced. This checks the algorithm and encoding instead: RFC 4231 test case 2.
    assert bybit.sign("Jefe", "what do ya want for nothing?") == \
        "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"


def _recording_transport(calls, payload=None):
    def transport(method, url, headers, body):
        calls.append((method, url, dict(headers), body))
        return 200, {}, json.dumps(payload or {"retCode": 0, "retMsg": "OK", "result": {}, "time": 1}).encode()
    return transport


def test_signed_get_signs_the_query_string_it_sends():
    calls = []
    client = bybit.BybitClient(FAKE_KEY, FAKE_SECRET, transport=_recording_transport(calls), clock=lambda: 1700000000.0)
    client.signed_get("/v5/order/realtime", {"category": "linear", "symbol": "BTCUSDT"})
    method, url, headers, body = calls[0]
    assert method == "GET" and body is None
    assert url == "https://api-testnet.bybit.com/v5/order/realtime?category=linear&symbol=BTCUSDT"
    assert headers["X-BAPI-API-KEY"] == FAKE_KEY
    assert headers["X-BAPI-TIMESTAMP"] == "1700000000000" and headers["X-BAPI-RECV-WINDOW"] == "5000"
    assert headers["X-BAPI-SIGN"] == bybit.sign(
        FAKE_SECRET, "1700000000000" + FAKE_KEY + "5000" + "category=linear&symbol=BTCUSDT")


def test_signed_post_signs_the_exact_body_bytes():
    calls = []
    client = bybit.BybitClient(FAKE_KEY, FAKE_SECRET, transport=_recording_transport(calls), clock=lambda: 1700000000.0)
    client.time_offset_ms = 250        # server clock 250 ms ahead: the timestamp follows the server
    client.signed_post("/v5/order/create", {"category": "linear", "qty": "0.001"})
    _, url, headers, body = calls[0]
    assert url == "https://api-testnet.bybit.com/v5/order/create"
    assert body == b'{"category":"linear","qty":"0.001"}'
    assert headers["X-BAPI-TIMESTAMP"] == "1700000000250"
    assert headers["X-BAPI-SIGN"] == bybit.sign(FAKE_SECRET, "1700000000250" + FAKE_KEY + "5000" + body.decode())


def test_public_requests_carry_no_credentials():
    calls = []
    client = bybit.BybitClient(FAKE_KEY, FAKE_SECRET, transport=_recording_transport(calls))
    client.public_get("/v5/market/kline", {"symbol": "BTCUSDT"})
    assert not any(h.startswith("X-BAPI") for h in calls[0][2])


def test_client_without_key_cannot_sign():
    calls = []
    client = bybit.BybitClient(transport=_recording_transport(calls))
    with pytest.raises(bybit.AuthError):
        client.signed_get("/v5/position/list", {})
    assert calls == []


def test_error_codes_are_classified():
    def answer(code, status=200, headers=None):
        doc = {"retCode": code, "retMsg": "x", "result": {}, "time": 1}
        return bybit.BybitClient(FAKE_KEY, FAKE_SECRET, sleep=lambda s: None,
                                 transport=lambda *a: (status, headers or {}, json.dumps(doc).encode()))
    with pytest.raises(bybit.RateLimitError):
        answer(10006).signed_get("/v5/position/list", {})
    with pytest.raises(bybit.RateLimitError):
        answer(0, status=429).signed_get("/v5/position/list", {})
    with pytest.raises(bybit.RateLimitError):
        answer(0, status=403).signed_get("/v5/position/list", {})
    for code in (10003, 10004, 10005, 33004):
        with pytest.raises(bybit.AuthError):
            answer(code).signed_get("/v5/position/list", {})
    with pytest.raises(bybit.BybitError) as info:
        answer(110007).signed_post("/v5/order/create", {})
    assert info.value.ret_code == 110007


def test_get_is_retried_on_network_errors_but_post_is_not():
    attempts = []

    def flaky(method, url, headers, body):
        attempts.append(method)
        raise TimeoutError("fake timeout")
    client = bybit.BybitClient(FAKE_KEY, FAKE_SECRET, transport=flaky, sleep=lambda s: None)
    with pytest.raises(bybit.NetworkError):
        client.signed_get("/v5/position/list", {})
    assert attempts == ["GET"] * 3
    attempts.clear()
    with pytest.raises(bybit.NetworkError):
        client.signed_post("/v5/order/create", {})
    assert attempts == ["POST"]                     # an order is never re-sent blindly


# --------------------------------------------------------------------------- #
# Testnet only
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", ["https://api.bybit.com/v5/market/time", "https://api.bytick.com/v5/order/create",
                                 "https://api.bybit.eu/v5/order/create", "https://example.com/v5/market/time",
                                 "http://api-testnet.bybit.com/v5/market/time"])
def test_any_host_but_the_testnet_is_refused(url):
    with pytest.raises(bybit.TestnetOnlyError):
        bybit.assert_testnet_url(url)


def test_client_refuses_mainnet_even_if_the_constant_is_changed(monkeypatch):
    calls = []
    monkeypatch.setattr(bybit, "TESTNET_REST", "https://api.bybit.com")
    client = bybit.BybitClient(FAKE_KEY, FAKE_SECRET, transport=_recording_transport(calls))
    with pytest.raises(bybit.TestnetOnlyError, match="0.3"):
        client.signed_post("/v5/order/create", {})
    assert calls == []


@pytest.mark.parametrize("live, message", [
    ({"network": "mainnet"}, "0.3"),
    ({"network": "live"}, "position guard"),
    ({"base_url": "https://api.bybit.com"}, "fixed to the Bybit testnet"),
    ({"endpoint": "https://api.bybit.com"}, "fixed to the Bybit testnet"),
    ({"leverage": 10}, "unknown live setting"),
    ({"order_notional_usdt": 500, "max_position_notional_usdt": 200}, "larger than"),
    ({"max_daily_loss_usdt": 0}, "positive"),
    ({"broker_id": "has spaces"}, "broker_id"),
])
def test_live_config_refusals(live, message):
    with pytest.raises(LiveConfigError, match=message):
        parse_live_config({"strategy": "ma_cross", "live": live}, "ma_cross")


def test_shipped_live_config_is_valid_and_has_empty_broker_id():
    with open(LIVE_CONFIG, encoding="utf-8") as handle:
        params, config = parse_live_config(json.load(handle), "ma_cross")
    assert config.network == "testnet" and config.broker_id == ""
    assert LiveConfig().broker_id == ""


def test_cli_mainnet_flag_is_refused(capsys):
    code = main(["live", "ma_cross", "--symbol", "BTCUSDT", "--interval", "15m",
                 "--config", LIVE_CONFIG, "--mainnet"])
    assert code == 1
    assert "0.3" in capsys.readouterr().err


def test_credentials_come_from_the_environment_only():
    with pytest.raises(LiveConfigError, match="BYBIT_TESTNET_API_KEY"):
        read_credentials({})
    assert read_credentials({"BYBIT_TESTNET_API_KEY": " k \n", "BYBIT_TESTNET_API_SECRET": "s\n"}) == ("k", "s")


def test_cli_without_keys_and_without_dry_run_fails(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("BYBIT_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_TESTNET_API_SECRET", raising=False)
    code = main(["live", "ma_cross", "--symbol", "BTCUSDT", "--interval", "15m",
                 "--config", LIVE_CONFIG])
    assert code == 1
    assert "--dry-run" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Broker ID
# --------------------------------------------------------------------------- #

def test_no_broker_header_by_default_and_only_when_set():
    calls = []
    bybit.BybitClient(FAKE_KEY, FAKE_SECRET, transport=_recording_transport(calls)).signed_post("/v5/order/create", {})
    assert not {"Referer", "X-Referer"} & set(calls[0][2])
    bybit.BybitClient(FAKE_KEY, FAKE_SECRET, transport=_recording_transport(calls),
                      broker_id="example-id").signed_post("/v5/order/create", {})
    assert calls[1][2]["Referer"] == "example-id"


# --------------------------------------------------------------------------- #
# API key permissions (https://bybit-exchange.github.io/docs/v5/user/apikey-info)
# --------------------------------------------------------------------------- #

def _key(**changes):
    info = json.loads(json.dumps(GOOD_KEY_INFO))
    for group, perms in changes.items():
        if group == "readOnly":
            info["readOnly"] = perms
        else:
            info["permissions"][group] = perms
    return info


@pytest.mark.parametrize("info, message", [
    (_key(Wallet=["Withdraw"]), "withdraw or transfer"),
    (_key(Wallet=["AccountTransfer"]), "withdraw or transfer"),
    (_key(Wallet=["SubMemberTransfer"]), "withdraw or transfer"),
    (_key(readOnly=1), "read-only"),
    (_key(ContractTrade=[]), "no ContractTrade 'Order'"),
    (_key(ContractTrade=["Position"]), "no ContractTrade 'Order'"),
])
def test_dangerous_or_useless_keys_are_refused(info, message):
    with pytest.raises(KeyPermissionError, match=message):
        check_api_key(info)


def test_good_key_passes_with_warnings_for_extras():
    assert check_api_key(_key()) == []
    warnings = check_api_key(_key(Spot=["SpotTrade"]))
    assert any("Spot" in w for w in warnings)


def test_runner_refuses_to_start_with_a_withdrawal_key(tmp_path):
    fake = FakeBybit()
    fake.key_info = _key(Wallet=["Withdraw"])
    runner, _ = make_runner(fake, tmp_path)
    with pytest.raises(KeyPermissionError):
        runner.start()
    assert fake.creates() == []


# --------------------------------------------------------------------------- #
# orderLinkId
# --------------------------------------------------------------------------- #

def test_order_link_id_is_deterministic_and_valid():
    h = run_hash("liquidity_sweep", "BTCUSDT", "15m", {"lookback": 20})
    a = order_link_id("liquidity_sweep", h, 1763467200000, -1, "entry")
    assert a == order_link_id("liquidity_sweep", h, 1763467200000, -1, "entry")
    assert a == f"pm-liquidit-{h}-1763467200-SE1"
    assert len(a) <= 36 and all(c.isalnum() or c in "-_" for c in a)
    assert a != order_link_id("liquidity_sweep", h, 1763467200000, 1, "entry")
    assert a != order_link_id("liquidity_sweep", h, 1763467200000 + STEP, -1, "entry")
    assert a != order_link_id("liquidity_sweep", h, 1763467200000, -1, "exit")
    assert h != run_hash("liquidity_sweep", "ETHUSDT", "15m", {"lookback": 20})


# --------------------------------------------------------------------------- #
# Placing orders
# --------------------------------------------------------------------------- #

def _start_with_long_signal(tmp_path, fake=None, **live):
    fake = fake or FakeBybit()
    runner, logs = make_runner(fake, tmp_path, generate=signal_at(fake.ts[-1], 1), **live)
    runner.start()
    return fake, runner, logs


def test_entry_sends_a_market_order_with_exchange_side_stop_and_target(tmp_path):
    fake, runner, logs = _start_with_long_signal(tmp_path)
    assert runner.process_latest_bar() == "done"
    (create,) = fake.creates()
    body = create["body"]
    close = fake.last_close()
    assert body["side"] == "Buy" and body["orderType"] == "Market" and body["positionIdx"] == 0
    assert body["category"] == "linear" and body["symbol"] == SYMBOL
    assert body["qty"] == f"{int(100 / close * 1000) / 1000:g}"        # 100 USDT, floored to the 0.001 step
    stop = float(body["stopLoss"])
    assert stop == pytest.approx(close * 0.99, abs=0.01)
    assert float(body["takeProfit"]) == pytest.approx(close + 2 * (close - stop), abs=0.011)
    assert body["tpslMode"] == "Full" and body["slTriggerBy"] == "LastPrice"
    assert "reduceOnly" not in body
    assert body["orderLinkId"] == order_link_id("ma_cross", runner.rhash, fake.ts[-1], 1, "entry")
    assert runner.state["position"]["side"] == 1
    assert runner.state["position"]["stop"] == body["stopLoss"]


def test_the_same_bar_is_never_traded_twice(tmp_path):
    fake, runner, _ = _start_with_long_signal(tmp_path)
    runner.process_latest_bar()
    assert runner.process_latest_bar() == "already-handled"
    assert len(fake.creates()) == 1


def test_restart_after_crash_before_state_update_does_not_resend(tmp_path):
    fake, runner, _ = _start_with_long_signal(tmp_path)
    runner.process_latest_bar()
    # Simulate a crash right after sending: the state file still says "pending" and has no position.
    state = json.loads(runner.state_file.path.read_text())
    link = fake.creates()[0]["body"]["orderLinkId"]
    state.update(position=None, pending_order={"order_link_id": link, "kind": "entry"}, last_bar_ms=None)
    runner.state_file.path.write_text(json.dumps(state))

    again, logs = make_runner(fake, tmp_path, generate=signal_at(fake.ts[-1], 1))
    again.start()                                  # reconciles: adopts its own filled order
    assert again.state["position"]["side"] == 1
    assert again.process_latest_bar() == "done"     # same bar, same orderLinkId -> found, not sent
    assert len(fake.creates()) == 1


def test_order_already_on_bybit_is_adopted_not_resent(tmp_path):
    fake, runner, logs = _start_with_long_signal(tmp_path)
    link = order_link_id("ma_cross", runner.rhash, fake.ts[-1], 1, "entry")
    fake.orders[link] = {"orderLinkId": link, "orderId": "earlier", "orderStatus": "Rejected", "side": "Buy",
                         "qty": "1", "cumExecQty": "0", "rejectReason": "EC_NoEnoughQtyToFill"}
    runner.process_latest_bar()
    assert fake.creates() == []                        # found by its orderLinkId: not sent again
    assert any("already exists on Bybit" in line for line in logs)
    assert runner.state["position"] is None


def test_duplicate_order_link_id_from_bybit_is_adopted(tmp_path):
    fake, runner, logs = _start_with_long_signal(tmp_path)
    original_lookup = runner.find_order
    calls = {"n": 0}

    def blind_first_lookup(link):    # the pre-check misses it (e.g. not visible yet)
        calls["n"] += 1
        return None if calls["n"] == 1 else original_lookup(link)
    runner.find_order = blind_first_lookup
    link = order_link_id("ma_cross", runner.rhash, fake.ts[-1], 1, "entry")
    fake.orders[link] = {"orderLinkId": link, "orderStatus": "Filled", "cumExecQty": "0", "side": "Buy",
                         "qty": "1", "orderId": "x"}
    runner.process_latest_bar()
    assert any("already exists; adopting" in line for line in logs)
    assert len(fake.creates()) == 1                 # Bybit answered 110072; nothing else sent


def test_timeout_on_create_looks_the_order_up_instead_of_resending(tmp_path):
    fake, runner, logs = _start_with_long_signal(tmp_path)
    fake.create_timeout = [True]                    # the order lands, the answer is lost
    runner.process_latest_bar()
    assert len(fake.creates()) == 1
    assert runner.state["position"]["side"] == 1
    assert any("checking whether it arrived" in line for line in logs)


def test_request_lost_before_arrival_is_retried_with_the_same_id(tmp_path):
    fake, runner, logs = _start_with_long_signal(tmp_path)
    fake.create_lost = 1
    runner.process_latest_bar()
    creates = fake.creates()
    assert len(creates) == 2 and len({c["body"]["orderLinkId"] for c in creates}) == 1
    assert len(fake.orders) == 1 and runner.state["position"]["side"] == 1


def test_answer_lost_every_time_leaves_it_pending_and_resolves_it_next_bar(tmp_path):
    fake, runner, logs = _start_with_long_signal(tmp_path)
    fake.create_lost = 3
    runner.run(once=True)                              # error logged, nothing placed, still pending
    assert fake.orders == {} and runner.state["pending_order"] is not None
    fake.add_bar()
    runner.process_latest_bar()                        # not found on Bybit -> cleared, no guessing
    assert runner.state["pending_order"] is None and runner.state["position"] is None
    assert any("never reached Bybit" in line for line in logs)


def test_partial_fill_keeps_the_filled_part_and_does_not_chase(tmp_path):
    fake = FakeBybit()
    fake.fill_ratio = Decimal("0.5")
    fake, runner, logs = _start_with_long_signal(tmp_path, fake)
    runner.process_latest_bar()
    sent = Decimal(fake.creates()[0]["body"]["qty"])
    filled = (sent * Decimal("0.5")).quantize(Decimal("0.001"))
    assert Decimal(runner.state["position"]["qty"]) == filled
    assert runner.state["orders"][-1]["status"] == "PartiallyFilledCanceled"
    assert len(fake.creates()) == 1
    assert any("partial fill" in line for line in logs)


def test_rejected_order_leaves_no_position_and_counts_as_an_error(tmp_path):
    fake, runner, logs = _start_with_long_signal(tmp_path)
    fake.create_error = [(110007, "Available balance is insufficient")]
    runner.process_latest_bar()
    assert runner.state["position"] is None
    assert runner.state["consecutive_api_errors"] == 1
    assert any("refused" in line for line in logs)


def test_rate_limit_waits_and_retries_with_the_same_id(tmp_path):
    fake, runner, logs = _start_with_long_signal(tmp_path)
    fake.create_error = [(10006, "Too many visits!")]
    runner.process_latest_bar()
    creates = fake.creates()
    assert len(creates) == 2 and creates[0]["body"]["orderLinkId"] == creates[1]["body"]["orderLinkId"]
    assert runner.state["position"]["side"] == 1
    assert any("rate limited" in line for line in logs)


def test_missing_stop_on_the_exchange_halts(tmp_path):
    fake = FakeBybit()
    fake.drop_stop_loss = True
    fake, runner, _ = _start_with_long_signal(tmp_path, fake)
    with pytest.raises(Halted, match="no stop-loss"):
        runner.process_latest_bar()


def test_exit_then_reverse_on_the_same_bar(tmp_path):
    fake = FakeBybit()
    first = fake.ts[-1]
    runner, logs = make_runner(fake, tmp_path, generate=signal_at(first, 1))
    runner.start()
    runner.process_latest_bar()
    fake.add_bar()
    second = fake.ts[-1]

    def flip(bars, params):          # long on the first bar; on the second, exit the long and go short
        s = Signals.empty(len(bars))
        for i, t in enumerate(bars.ts):
            close = float(bars.close[i])
            if int(t) == first:
                s.side[i], s.stop[i], s.target_r[i] = 1, close * 0.99, 2.0
            if int(t) == second:
                s.side[i], s.stop[i], s.target_r[i], s.exit_long[i] = -1, close * 1.01, 2.0, True
        return s
    runner.generate = flip
    runner.process_latest_bar()
    kinds = [(c["body"]["side"], bool(c["body"].get("reduceOnly"))) for c in fake.creates()]
    assert kinds == [("Buy", False), ("Sell", True), ("Sell", False)]
    assert runner.state["position"]["side"] == -1


def test_position_closed_by_its_stop_between_bars_is_accepted(tmp_path):
    fake, runner, logs = _start_with_long_signal(tmp_path)
    runner.process_latest_bar()
    fake.position.update(side="", size="0", stopLoss="", positionValue="0")   # stop filled on Bybit
    fake.add_bar()
    runner.process_latest_bar()
    assert runner.state["position"] is None
    assert any("closed on Bybit" in line for line in logs)


# --------------------------------------------------------------------------- #
# Reconciliation at start
# --------------------------------------------------------------------------- #

def test_unknown_position_on_bybit_halts_at_start(tmp_path):
    fake = FakeBybit()
    fake.position.update(side="Buy", size="0.5", stopLoss="90", positionValue="50")
    runner, _ = make_runner(fake, tmp_path)
    with pytest.raises(Halted, match="does not know about"):
        runner.start()


def test_expected_position_missing_on_bybit_halts_at_start(tmp_path):
    fake, runner, _ = _start_with_long_signal(tmp_path)
    runner.process_latest_bar()
    fake.position.update(side="", size="0", stopLoss="", positionValue="0")
    again, _ = make_runner(fake, tmp_path)
    with pytest.raises(Halted, match="Bybit shows none"):
        again.start()


def test_different_size_on_bybit_halts_at_start(tmp_path):
    fake, runner, _ = _start_with_long_signal(tmp_path)
    runner.process_latest_bar()
    fake.position["size"] = "9.999"
    again, _ = make_runner(fake, tmp_path)
    with pytest.raises(Halted, match="state file expects"):
        again.start()


def test_foreign_open_order_halts_at_start(tmp_path):
    fake = FakeBybit()
    fake.open_orders = [{"orderId": "123", "orderLinkId": "someone-else", "orderStatus": "New", "stopOrderType": ""}]
    runner, _ = make_runner(fake, tmp_path)
    with pytest.raises(Halted, match="does not expect"):
        runner.start()


def test_hedge_mode_halts(tmp_path):
    fake = FakeBybit()
    fake.position["positionIdx"] = 1
    runner, _ = make_runner(fake, tmp_path)
    with pytest.raises(Halted, match="hedge mode"):
        runner.start()


def test_start_needs_the_typed_confirmation(tmp_path):
    fake = FakeBybit()
    runner, _ = make_runner(fake, tmp_path, confirm="yes")
    with pytest.raises(StartupError, match="not confirmed"):
        runner.start()
    assert fake.creates() == []


# --------------------------------------------------------------------------- #
# Circuit breakers
# --------------------------------------------------------------------------- #

def test_daily_loss_halts_persists_and_can_be_cleared(tmp_path):
    fake, runner, _ = _start_with_long_signal(tmp_path, max_daily_loss_usdt=20)
    fake.equity -= 25
    with pytest.raises(Halted, match="daily loss 25.00"):
        runner.process_latest_bar()
    assert fake.creates() == []
    again, _ = make_runner(fake, tmp_path)
    with pytest.raises(Halted, match="halted earlier: daily loss"):
        again.start()
    third, logs = make_runner(fake, tmp_path)
    with pytest.raises(Halted, match="daily loss"):     # cleared, checked again, still too much
        third.start(clear_halt=True)
    fake.equity += 25
    fourth, logs = make_runner(fake, tmp_path)
    fourth.start(clear_halt=True)
    assert any("cleared earlier halt" in line for line in logs)


def test_daily_loss_resets_on_a_new_utc_day(tmp_path):
    fake, runner, _ = _start_with_long_signal(tmp_path)
    runner.state["day"]["date"] = "2000-01-01"
    fake.equity -= 500
    runner.process_latest_bar()                      # new day: new start equity, no halt
    assert runner.state["day"]["start_equity"] == fake.equity


def test_order_above_the_position_limit_halts_before_sending(tmp_path):
    fake = FakeBybit()
    runner, _ = make_runner(fake, tmp_path, generate=signal_at(fake.ts[-1], 1),
                            order_notional_usdt=100, max_position_notional_usdt=100)
    runner.start()
    runner.config.order_notional_usdt = 150          # bypasses config validation to reach the runtime check
    with pytest.raises(Halted, match="max_position_notional_usdt"):
        runner.process_latest_bar()
    assert fake.creates() == []


def test_oversized_position_on_bybit_halts(tmp_path):
    fake, runner, _ = _start_with_long_signal(tmp_path)
    runner.process_latest_bar()
    fake.position["positionValue"] = "5000"
    fake.add_bar()
    with pytest.raises(Halted, match="position value"):
        runner.process_latest_bar()


def test_consecutive_api_errors_halt(tmp_path):
    fake, runner, logs = _start_with_long_signal(tmp_path, max_consecutive_api_errors=3)
    fake._r_v5_position_list = lambda q, b: fake._reply(10016, "Server error.")
    runner.run(once=True)
    runner.run(once=True)
    assert runner.state["consecutive_api_errors"] == 2
    with pytest.raises(Halted, match="3 consecutive API errors"):
        runner.run(once=True)
    assert json.loads(runner.state_file.path.read_text())["halt"]["reason"].startswith("3 consecutive")


def test_success_resets_the_error_count(tmp_path):
    fake, runner, _ = _start_with_long_signal(tmp_path)
    runner.state["consecutive_api_errors"] = 4
    runner.process_latest_bar()
    assert runner.state["consecutive_api_errors"] == 0


def test_clock_skew_halts(tmp_path):
    fake = FakeBybit()
    fake.skew_ms = 6_000
    runner, _ = make_runner(fake, tmp_path)                 # default limit 5000 ms
    with pytest.raises(Halted, match="clock skew"):
        runner.start()


def test_missing_bars_means_no_order(tmp_path):
    fake, runner, logs = _start_with_long_signal(tmp_path)
    fake.next_kline_gap = True
    assert runner.process_latest_bar() == "gap"
    assert fake.creates() == []
    assert any("NOT trading" in line for line in logs)


def test_late_decision_is_not_acted_on(tmp_path):
    fake, runner, logs = _start_with_long_signal(tmp_path)
    fake.local_ms += 120_000                          # two minutes after the close
    runner.process_latest_bar()
    assert fake.creates() == []
    assert any("not acting" in line for line in logs)


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #

def test_dry_run_needs_no_key_and_sends_no_signed_request(tmp_path):
    fake = FakeBybit()
    runner, logs = make_runner(fake, tmp_path, dry_run=True, generate=signal_at(fake.ts[-1], 1))
    runner.start()
    runner.process_latest_bar()
    assert fake.signed_requests() == []
    assert {r["path"] for r in fake.requests} <= {"/v5/market/time", "/v5/market/kline", "/v5/market/instruments-info"}
    assert not any(h.startswith("X-BAPI") for r in fake.requests for h in r["headers"])
    assert any("[dry run] would send POST /v5/order/create" in line for line in logs)
    assert runner.state_file.path.name.endswith("-dryrun.json")
    assert runner.state["position"]["side"] == 1


def test_dry_run_paper_position_closes_on_its_stop(tmp_path):
    fake = FakeBybit()
    runner, logs = make_runner(fake, tmp_path, dry_run=True, generate=signal_at(fake.ts[-1], 1))
    runner.start()
    runner.process_latest_bar()
    stop = float(runner.state["position"]["stop"])
    fake.add_bar()
    fake.ohlc[-1] = [stop * 1.002, stop * 1.003, stop * 0.995, stop * 0.998]   # trades through the stop
    runner.process_latest_bar()
    assert runner.state["position"] is None
    assert any("closed by its stop" in line for line in logs)


def test_cli_dry_run_end_to_end_without_keys(monkeypatch, tmp_path, capsys):
    fake = FakeBybit()
    monkeypatch.delenv("BYBIT_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_TESTNET_API_SECRET", raising=False)
    monkeypatch.setattr(bybit, "urllib_transport", fake)
    monkeypatch.setattr(bybit, "time", SimpleNamespace(time=fake.clock, sleep=lambda s: None))
    config = tmp_path / "c.json"
    config.write_text(json.dumps({"strategy": "ma_cross", "live": {"state_dir": str(tmp_path / "s")}}))
    code = main(["live", "ma_cross", "--symbol", SYMBOL, "--interval", "15m", "--config", str(config),
                 "--dry-run", "--once"])
    assert code == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "bar " in out
    assert fake.signed_requests() == []


# --------------------------------------------------------------------------- #
# Secrets never leave the process
# --------------------------------------------------------------------------- #

def test_key_and_secret_never_appear_in_logs_state_or_errors(tmp_path):
    fake = FakeBybit()
    runner, logs = make_runner(fake, tmp_path, generate=signal_at(fake.ts[-1], 1))
    runner.start()
    runner.process_latest_bar()
    fake.create_error = [(110007, "Available balance is insufficient")]
    fake.add_bar()
    runner.process_latest_bar()
    errors = []
    try:
        bybit.BybitClient(FAKE_KEY, FAKE_SECRET, transport=lambda *a: (401, {}, b"")).signed_get("/v5/x", {})
    except bybit.BybitError as exc:
        errors.append(str(exc))
    everything = "\n".join(logs + errors + [repr(runner.client), runner.state_file.path.read_text()])
    assert FAKE_SECRET not in everything
    assert FAKE_KEY not in everything
    assert bybit.mask(FAKE_KEY) in everything          # only the masked form: length and 4 + 4 characters
    assert bybit.mask(FAKE_SECRET) == f"len={len(FAKE_SECRET)}, FAKE...0001"
