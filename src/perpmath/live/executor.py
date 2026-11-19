"""Testnet runner: the backtest's strategy and timing, with real orders on the Bybit testnet.

Timing is the engine's (see ``perpmath.engine``):

* The strategy decides on a bar's close, using closed bars only.
* The decision is acted on straight after that close, which is the next bar's
  open. A decision found more than ``max_signal_delay_seconds`` after the close
  (for example after a restart) is not acted on: it would no longer be a fill
  at the next open.
* Exit flags are handled before entries, as in the engine. An entry signal
  while a position is open is ignored (one position at a time).
* Stop and target are attached to the entry order as Bybit-side conditional
  orders (``stopLoss`` / ``takeProfit``, full position, triggered by last
  price). They work while this program is not running.

Differences from the backtest, stated plainly:

* Sizing is a fixed ``order_notional_usdt``, not a fraction of equity.
* The target is computed from the signal bar's close (the reference price),
  because the order is sent before its fill price is known. The backtest
  computes it from the fill price.
* Fills, fees and slippage are whatever the testnet does.

Order safety:

* Every order has a deterministic ``orderLinkId`` built from the strategy, a
  hash of the run's settings, the bar's open time, the direction and the
  action. Before sending, the runner looks the ID up on Bybit; if an order with
  that ID exists it is adopted, not sent again. Bybit itself rejects a second
  order with the same ID (retCode 110072), which is the last line of defence.
* A request that times out has an unknown outcome. The runner looks the order
  up by its ID before trying again, and retries only with the same ID.
* On start, the state file is compared with Bybit's positions and open orders.
  Any disagreement stops the runner with a report; it does not guess.

The planner is separate from the plumbing so that a resting-order mode (a
grid, for instance) can produce limit-order intents and reuse the same
submission, idempotency, reconciliation and breakers.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from .. import data as data_mod
from ..backtest import check_no_lookahead
from ..data import INTERVALS, Bars, DataError, count_missing_bars, interval_ms
from ..strategies import STRATEGIES
from .breaker import CircuitBreaker, Halted
from .bybit import (DUPLICATE_ORDER_LINK_ID, AuthError, BybitClient, BybitError, NetworkError,
                    RateLimitError, mask)
from .config import LiveConfig
from .state import StateFile, empty_state

CATEGORY = "linear"
CONFIRM_WORD = "TESTNET"

# https://bybit-exchange.github.io/docs/v5/enum#orderstatus
FINAL_STATUSES = frozenset({"Rejected", "PartiallyFilledCanceled", "Filled", "Cancelled", "Triggered", "Deactivated"})
# Conditional orders Bybit creates for a position's own stop/target.
# https://bybit-exchange.github.io/docs/v5/enum#stopordertype
POSITION_TPSL_TYPES = frozenset({"StopLoss", "TakeProfit", "TrailingStop", "PartialStopLoss",
                                 "PartialTakeProfit", "tpslOrder"})

_ID_SAFE = re.compile(r"[^A-Za-z0-9]")


class KeyPermissionError(Exception):
    """The API key has permissions 0.2 refuses to run with, or lacks trade permission."""


class StartupError(Exception):
    """Something needed to start is missing or wrong (instrument, confirmation...)."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise BybitError(f"not a number: {value!r}") from exc


def fmt(value: Decimal) -> str:
    """Decimal -> plain string Bybit accepts ('0.001', '65000.5', '100')."""
    text = format(value.normalize(), "f")
    return text


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def round_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_HALF_EVEN) * tick


def run_hash(strategy: str, symbol: str, interval: str, params: Dict[str, Any]) -> str:
    """Six hex characters identifying (strategy, symbol, interval, params)."""
    text = json.dumps([strategy, symbol, interval, params], sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:6]


def order_link_id(strategy: str, rhash: str, bar_ms: int, side: int, kind: str, attempt: int = 1) -> str:
    """Deterministic Bybit ``orderLinkId``: same inputs, same ID, across restarts.

    Format ``pm-<strategy, 8 chars>-<run hash>-<bar open, unix s>-<L|S><E|X><attempt>``,
    for example ``pm-macross-3fa9c1-1763467200-LE1``. At most 33 characters;
    Bybit allows 36 of ``[A-Za-z0-9_-]``.
    https://bybit-exchange.github.io/docs/v5/order/create-order (orderLinkId)
    """
    if not 1 <= attempt <= 9:
        raise ValueError("attempt must be 1..9")
    name = _ID_SAFE.sub("", strategy)[:8] or "strat"
    direction = "L" if side > 0 else "S"
    action = "E" if kind == "entry" else "X"
    link = f"pm-{name}-{rhash}-{int(bar_ms) // 1000}-{direction}{action}{attempt}"
    assert len(link) <= 36
    return link


@dataclass(frozen=True)
class Instrument:
    """Order-size and price rules. https://bybit-exchange.github.io/docs/v5/market/instrument"""
    symbol: str
    qty_step: Decimal
    min_qty: Decimal
    max_market_qty: Decimal
    tick_size: Decimal
    min_notional: Decimal

    @classmethod
    def from_api(cls, item: Dict[str, Any]) -> "Instrument":
        lot, price = item.get("lotSizeFilter") or {}, item.get("priceFilter") or {}
        return cls(
            symbol=item["symbol"],
            qty_step=dec(lot["qtyStep"]),
            min_qty=dec(lot["minOrderQty"]),
            max_market_qty=dec(lot.get("maxMktOrderQty") or lot["maxOrderQty"]),
            tick_size=dec(price["tickSize"]),
            min_notional=dec(lot.get("minNotionalValue") or "0"),
        )


@dataclass
class OrderIntent:
    """One order the planner wants. ``side`` is the order's direction: +1 buy, -1 sell."""
    kind: str                      # "entry" | "exit"
    side: int
    qty: Decimal
    order_link_id: str
    ref_price: Decimal
    reduce_only: bool = False
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    order_type: str = "Market"     # a resting-order mode would use "Limit" ...
    price: Optional[Decimal] = None  # ... with a price

    def body(self, symbol: str) -> Dict[str, Any]:
        """Request body for POST /v5/order/create.
        https://bybit-exchange.github.io/docs/v5/order/create-order"""
        body: Dict[str, Any] = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": "Buy" if self.side > 0 else "Sell",
            "orderType": self.order_type,
            "qty": fmt(self.qty),
            "positionIdx": 0,                      # one-way mode
            "orderLinkId": self.order_link_id,
        }
        if self.price is not None:
            body["price"] = fmt(self.price)
        if self.reduce_only:
            body["reduceOnly"] = True              # cannot carry TP/SL, per the docs
        if self.stop_loss is not None:
            body.update({"stopLoss": fmt(self.stop_loss), "slTriggerBy": "LastPrice",
                         "tpslMode": "Full", "slOrderType": "Market"})
        if self.take_profit is not None:
            body.update({"takeProfit": fmt(self.take_profit), "tpTriggerBy": "LastPrice"})
            body.setdefault("tpslMode", "Full")
        return body


@dataclass
class Decision:
    """What the strategy decided on the close of bar ``bar_ms``."""
    bar_ms: int
    close: float
    side: int
    stop: float
    target_r: float
    exit_long: bool
    exit_short: bool

    @classmethod
    def last(cls, bars: Bars, signals) -> "Decision":
        i = len(bars) - 1
        return cls(int(bars.ts[i]), float(bars.close[i]), int(signals.side[i]), float(signals.stop[i]),
                   float(signals.target_r[i]), bool(signals.exit_long[i]), bool(signals.exit_short[i]))


class SignalPlanner:
    """Market-order mode: turns a ``Decision`` into order intents with the engine's rules."""

    def __init__(self, strategy: str, rhash: str, config: LiveConfig):
        self.strategy, self.rhash, self.config = strategy, rhash, config

    def link(self, bar_ms: int, side: int, kind: str, attempt: int = 1) -> str:
        return order_link_id(self.strategy, self.rhash, bar_ms, side, kind, attempt)

    def plan(self, decision: Decision, position: Optional[Dict[str, Any]], instrument: Instrument,
             log: Callable[[str], None]) -> List[OrderIntent]:
        intents: List[OrderIntent] = []
        ref = dec(decision.close)
        open_side = int(position["side"]) if position else 0

        # 1. Exits first, as in the engine.
        if open_side == 1 and decision.exit_long or open_side == -1 and decision.exit_short:
            intents.append(OrderIntent(
                kind="exit", side=-open_side, qty=dec(position["qty"]), reduce_only=True, ref_price=ref,
                order_link_id=self.link(decision.bar_ms, open_side, "exit")))
            open_side = 0

        # 2. Entry only when flat after the exit.
        if decision.side == 0:
            return intents
        if open_side != 0:
            log("entry signal ignored: a position is already open (one position at a time)")
            return intents
        side = decision.side
        if not np.isfinite(decision.stop):
            raise ValueError("strategy gave an entry without a stop price")
        stop = round_to_tick(dec(decision.stop), instrument.tick_size)
        risk = side * (ref - stop)
        if risk <= 0:
            log(f"entry skipped: stop {fmt(stop)} is on the wrong side of the reference price {fmt(ref)}")
            return intents
        qty = floor_to_step(dec(self.config.order_notional_usdt) / ref, instrument.qty_step)
        qty = min(qty, floor_to_step(instrument.max_market_qty, instrument.qty_step))
        if qty < instrument.min_qty or qty * ref < instrument.min_notional or qty <= 0:
            log(f"entry skipped: {self.config.order_notional_usdt} USDT is below the minimum order "
                f"({fmt(instrument.min_qty)} {instrument.symbol} / {fmt(instrument.min_notional)} USDT)")
            return intents
        target = None
        if np.isfinite(decision.target_r):
            target = round_to_tick(ref + side * dec(decision.target_r) * risk, instrument.tick_size)
        intents.append(OrderIntent(
            kind="entry", side=side, qty=qty, ref_price=ref, stop_loss=stop, take_profit=target,
            order_link_id=self.link(decision.bar_ms, side, "entry")))
        return intents


# --------------------------------------------------------------------------- #
# API key check
# --------------------------------------------------------------------------- #

# https://bybit-exchange.github.io/docs/v5/user/apikey-info (permissions object)
FORBIDDEN_PERMISSIONS = {
    "Wallet": {"Withdraw", "AccountTransfer", "SubMemberTransfer", "SubMemberTransferList"},
}


def check_api_key(info: Dict[str, Any]) -> List[str]:
    """Refuse keys that can move money or cannot trade. Returns warnings for the rest.

    Rules (0.2):
    * read-only key (``readOnly`` = 1): refused, it cannot place orders;
    * any withdrawal or transfer permission: refused, a trading bot never needs it;
    * no ``ContractTrade: Order`` permission: refused, it cannot trade USDT perpetuals.
    """
    perms = info.get("permissions") or {}
    if not isinstance(perms, dict):
        raise KeyPermissionError("unexpected /v5/user/query-api response: permissions missing")
    bad = []
    for group, names in FORBIDDEN_PERMISSIONS.items():
        bad += [f"{group}:{p}" for p in (perms.get(group) or []) if p in names]
    for group, granted in perms.items():
        bad += [f"{group}:{p}" for p in (granted or []) if "withdraw" in str(p).lower() and f"{group}:{p}" not in bad]
    if bad:
        raise KeyPermissionError(
            "this API key can withdraw or transfer funds (" + ", ".join(sorted(bad)) + "). "
            "Refusing to start: create a testnet key with trade permission only, no withdrawal or transfer.")
    if str(info.get("readOnly")) == "1":
        raise KeyPermissionError("this API key is read-only and cannot place orders; "
                                 "create a testnet key with Contract trade (Order + Position) permission")
    contract = perms.get("ContractTrade") or []
    if "Order" not in contract:
        raise KeyPermissionError("this API key has no ContractTrade 'Order' permission, so it cannot trade "
                                 "USDT perpetuals; enable Contract -> Orders and Positions on the testnet key")
    warnings = []
    if "Position" not in contract:
        warnings.append("key has no ContractTrade 'Position' permission; position queries may fail")
    extra = sorted(g for g, granted in perms.items() if granted and g not in ("ContractTrade", "Derivatives"))
    if extra:
        warnings.append("key has permissions this runner does not need: " + ", ".join(extra))
    if not info.get("ips"):
        warnings.append("key is not bound to an IP address; Bybit recommends binding one")
    return warnings


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def default_log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} {message}", flush=True)


def default_confirm(prompt: str) -> str:
    if not sys.stdin or not sys.stdin.isatty():
        raise StartupError(f"confirmation needed: run in a terminal and type {CONFIRM_WORD}")
    return input(prompt)


class LiveRunner:
    def __init__(self, strategy: str, params: Dict[str, Any], config: LiveConfig, symbol: str,
                 interval: str, client: BybitClient, dry_run: bool, *,
                 log: Callable[[str], None] = default_log,
                 confirm: Callable[[str], str] = default_confirm,
                 sleep: Callable[[float], None] = time.sleep):
        if strategy not in STRATEGIES:
            raise StartupError(f"unknown strategy {strategy!r}")
        if interval not in INTERVALS:
            raise StartupError(f"unsupported interval {interval!r}")
        config.validate()
        if not dry_run and not client.has_credentials:
            raise StartupError("a real testnet run needs API credentials; use --dry-run to run without them")
        self.strategy, self.params, self.config = strategy, params, config
        self.symbol, self.interval = symbol.upper(), interval
        self.step = interval_ms(interval)
        self.client, self.dry_run = client, dry_run
        self.log, self._confirm, self._sleep = log, confirm, sleep
        self.generate = STRATEGIES[strategy].generate
        self.rhash = run_hash(strategy, self.symbol, interval, params)
        self.run_id = f"{strategy}-{self.symbol}-{interval}-{self.rhash}"
        self.planner = SignalPlanner(strategy, self.rhash, config)
        self.state_file = StateFile.for_run(config.state_dir, self.run_id, dry_run)
        self.state: Dict[str, Any] = {}
        self.breaker: Optional[CircuitBreaker] = None
        self.instrument: Optional[Instrument] = None
        self._cycle_clean = True

    # ------------------------------------------------------------------ #
    # Plumbing
    # ------------------------------------------------------------------ #

    def save(self) -> None:
        self.state_file.save(self.state)

    def _call(self, what: str, fn: Callable[[], Dict[str, Any]], tries: int = 3) -> Dict[str, Any]:
        """Run one API call, counting failures for the consecutive-error breaker.

        Rate limits: wait until Bybit's reset time and try again (the request
        was refused, so repeating it is safe). Auth errors halt at once.
        Everything else is counted and re-raised.
        """
        for attempt in range(tries):
            try:
                result = fn()
            except RateLimitError as exc:
                self._cycle_clean = False
                self.breaker.api_error(f"{what}: {exc}", self.client.now_ms())
                wait = 1.0 if exc.retry_at_ms is None else max(0.2, (exc.retry_at_ms - self.client.now_ms()) / 1000)
                if attempt + 1 >= tries or wait > 120:
                    raise
                self.log(f"rate limited on {what}; waiting {wait:.1f}s")
                self._sleep(wait)
                continue
            except AuthError as exc:
                self.breaker.trip(f"API key rejected during {what}: {exc}", self.client.now_ms())
            except (BybitError, NetworkError) as exc:
                self._cycle_clean = False
                self.breaker.api_error(f"{what}: {exc}", self.client.now_ms())
                raise
            return result
        raise AssertionError("unreachable")

    def sync_clock(self) -> int:
        skew = self._call("server time", self.client.sync_time)
        self.breaker.check_clock(skew, self.client.now_ms())
        return skew

    def fetch_instrument(self) -> Instrument:
        # https://bybit-exchange.github.io/docs/v5/market/instrument (public)
        result = self._call("instruments-info", lambda: self.client.public_get(
            "/v5/market/instruments-info", {"category": CATEGORY, "symbol": self.symbol}))
        items = result.get("list") or []
        if not items:
            raise StartupError(f"{self.symbol} is not a linear contract on the Bybit testnet")
        item = items[0]
        if item.get("status") != "Trading":
            raise StartupError(f"{self.symbol} status on the testnet is {item.get('status')!r}, not Trading")
        if item.get("contractType") != "LinearPerpetual" or item.get("settleCoin") != "USDT":
            raise StartupError(f"{self.symbol} is not a USDT perpetual; 0.2 supports USDT perpetuals only")
        return Instrument.from_api(item)

    def fetch_closed_bars(self) -> Bars:
        """The last ``history_bars`` closed bars, oldest first.
        https://bybit-exchange.github.io/docs/v5/market/kline (newest first; the last one may be forming)"""
        now = self.client.now_ms()
        last_closed = now // self.step * self.step - self.step
        result = self._call("kline", lambda: self.client.public_get("/v5/market/kline", {
            "category": CATEGORY, "symbol": self.symbol, "interval": INTERVALS[self.interval][0],
            "end": str(last_closed), "limit": str(self.config.history_bars)}))
        rows = data_mod._parse_page({"retCode": 0, "result": result}, self.step)
        rows = sorted(r for r in rows if r[0] + self.step <= now)
        if not rows:
            raise DataError("no closed bars returned")
        arr = np.asarray([[float(x) for x in r[1:6]] for r in rows])
        ts = np.asarray([r[0] for r in rows], dtype=np.int64)
        if len(ts) > 1 and not np.all(np.diff(ts) > 0):
            raise DataError("bar times are not strictly increasing")
        return Bars(ts, arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4])

    def bars_problem(self, bars: Bars) -> Optional[str]:
        """Why these bars must not be traded on, or None."""
        expected_last = self.client.now_ms() // self.step * self.step - self.step
        if int(bars.ts[-1]) != expected_last:
            return (f"latest closed bar is {data_mod.iso_utc(int(bars.ts[-1]))}, expected "
                    f"{data_mod.iso_utc(expected_last)}")
        missing = count_missing_bars(bars, self.step)
        if missing:
            return f"{missing} bar(s) missing inside the last {len(bars)} bars"
        return None

    def fetch_position(self) -> Dict[str, Any]:
        """Current one-way position. https://bybit-exchange.github.io/docs/v5/position
        (with ``symbol`` set, the entry is returned even when flat; ``side`` is "" then)."""
        result = self._call("position list", lambda: self.client.signed_get(
            "/v5/position/list", {"category": CATEGORY, "symbol": self.symbol}))
        items = [p for p in (result.get("list") or []) if p.get("symbol") == self.symbol]
        if any(int(p.get("positionIdx") or 0) != 0 for p in items):
            self.breaker.trip(f"{self.symbol} is in hedge mode on Bybit; 0.2 needs one-way mode "
                              "(switch it in the testnet UI with no position open)", self.client.now_ms())
        if not items:
            return {"side": 0, "qty": Decimal(0), "avg_price": None, "stop_loss": None, "value": 0.0}
        p = items[0]
        qty = dec(p.get("size") or "0")
        side = {"Buy": 1, "Sell": -1}.get(p.get("side") or "", 0) if qty > 0 else 0
        stop = p.get("stopLoss") or ""
        return {
            "side": side, "qty": qty,
            "avg_price": p.get("avgPrice") or None,
            "stop_loss": stop if stop not in ("", "0", "0.0") else None,
            "take_profit": (p.get("takeProfit") or None) if p.get("takeProfit") not in ("0", "") else None,
            "value": float(p.get("positionValue") or 0.0),
        }

    def fetch_equity(self) -> float:
        """USDT equity (wallet balance plus unrealised P&L), unified account.
        https://bybit-exchange.github.io/docs/v5/account/wallet-balance"""
        result = self._call("wallet balance", lambda: self.client.signed_get(
            "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"}))
        for account in result.get("list") or []:
            for coin in account.get("coin") or []:
                if coin.get("coin") == "USDT" and coin.get("equity") not in (None, ""):
                    return float(coin["equity"])
        raise BybitError("wallet balance has no USDT equity (is this a unified trading account?)")

    def fetch_open_orders(self) -> List[Dict[str, Any]]:
        # https://bybit-exchange.github.io/docs/v5/order/open-order (openOnly=0: open orders only)
        result = self._call("open orders", lambda: self.client.signed_get(
            "/v5/order/realtime", {"category": CATEGORY, "symbol": self.symbol, "openOnly": "0", "limit": "50"}))
        return list(result.get("list") or [])

    def find_order(self, link: str) -> Optional[Dict[str, Any]]:
        """Look an order up by orderLinkId: recent orders first, then the 7-day history.
        https://bybit-exchange.github.io/docs/v5/order/open-order (openOnly=1 includes recent closed)
        https://bybit-exchange.github.io/docs/v5/order/order-list"""
        for path, extra in (("/v5/order/realtime", {"openOnly": "1"}), ("/v5/order/history", {})):
            params = {"category": CATEGORY, "symbol": self.symbol, "orderLinkId": link, **extra}
            result = self._call(f"order lookup {link}", lambda p=path, q=params: self.client.signed_get(p, q))
            for order in result.get("list") or []:
                if order.get("orderLinkId") == link:
                    return order
        return None

    # ------------------------------------------------------------------ #
    # Start-up
    # ------------------------------------------------------------------ #

    def start(self, clear_halt: bool = False) -> None:
        loaded = self.state_file.load()
        self.state = loaded or empty_state(self.run_id, self.strategy, self.symbol, self.interval, self.dry_run)
        self.breaker = CircuitBreaker(self.config, self.state, self.save)
        if clear_halt and self.state.get("halt"):
            old = self.breaker.clear()
            self.log(f"cleared earlier halt: {old['reason']}")
        self.breaker.raise_if_halted()
        self.save()

        mode = "DRY RUN (no signed requests, no orders)" if self.dry_run else "TESTNET ORDERS"
        self.log(f"perpmath live {self.strategy} {self.symbol} {self.interval} | {mode} | state {self.state_file.path}")
        self.log(f"limits: order {self.config.order_notional_usdt} USDT, max position "
                 f"{self.config.max_position_notional_usdt} USDT, max daily loss {self.config.max_daily_loss_usdt} USDT, "
                 f"max {self.config.max_consecutive_api_errors} consecutive API errors, "
                 f"max clock skew {self.config.max_clock_skew_ms} ms")
        if self.config.broker_id:
            self.log(f"broker_id is set; orders carry Bybit broker attribution {self.config.broker_id!r}")

        skew = self.sync_clock()
        self.log(f"clock offset to Bybit: {skew} ms (request timestamps and bar timing use Bybit's clock)")
        self.instrument = self.fetch_instrument()

        bars = self.fetch_closed_bars()
        check_no_lookahead(self.generate, bars, self.params, cuts=4)

        if self.dry_run:
            return
        info = self._call("api key info", lambda: self.client.signed_get("/v5/user/query-api", {}))
        for warning in check_api_key(info):
            self.log(f"warning: {warning}")
        self.log(f"API key {mask(str(info.get('apiKey') or ''))} accepted: trade permission, "
                 "no withdrawal or transfer permission")
        self.reconcile()
        equity = self.fetch_equity()
        loss = self.breaker.check_daily_loss(equity, self.client.now_ms())
        self.log(f"equity {equity:.2f} USDT, today's loss so far {loss:.2f} USDT")
        answer = self._confirm(f"This will place orders on the Bybit TESTNET for {self.symbol}. "
                               f"Type {CONFIRM_WORD} to continue: ")
        if (answer or "").strip() != CONFIRM_WORD:
            raise StartupError("not confirmed; no orders were placed")

    def _resolve_pending(self) -> bool:
        """An order sent just before a crash or a lost answer: find out what became of it.

        Only an order carrying this run's own orderLinkId counts as evidence;
        nothing else is assumed. Returns True if it was an exit that filled."""
        now = self.client.now_ms()
        exit_filled = False
        pending = self.state.get("pending_order")
        if pending:
            order = self.find_order(pending["order_link_id"])
            if order is None:
                self.log(f"unconfirmed order {pending['order_link_id']} never reached Bybit")
            elif order.get("orderStatus") not in FINAL_STATUSES:
                self.breaker.trip(f"order {pending['order_link_id']} is still {order.get('orderStatus')} on Bybit; "
                                  "cancel it on the testnet or wait, then restart", now)
            else:
                filled = dec(order.get("cumExecQty") or "0")
                self.log(f"unconfirmed order {pending['order_link_id']} ended {order.get('orderStatus')}, "
                         f"filled {fmt(filled)}")
                self._record_order(pending["kind"], order)
                if pending["kind"] == "entry" and filled > 0 and not self.state.get("position"):
                    self.state["position"] = {"side": 1 if order.get("side") == "Buy" else -1,
                                              "qty": fmt(filled), "order_link_id": pending["order_link_id"]}
                elif pending["kind"] == "exit" and filled > 0 and self.state.get("position"):
                    exit_filled = True
            self.state["pending_order"] = None
            self.save()
        return exit_filled

    def reconcile(self) -> None:
        """Compare the state file with Bybit. Stop on any disagreement."""
        now = self.client.now_ms()
        exit_filled = self._resolve_pending()
        pos = self.fetch_position()
        local = self.state.get("position")
        state_path = self.state_file.path
        if local is None and pos["qty"] > 0:
            self.breaker.trip(
                f"Bybit shows a {'long' if pos['side'] > 0 else 'short'} position of {fmt(pos['qty'])} "
                f"{self.symbol} that this runner's state does not know about. It may be manual or from "
                "another program. Close it on the testnet (or stop the other program), then restart.", now)
        if local is not None and pos["qty"] == 0:
            if not exit_filled:
                self.breaker.trip(
                    f"state file expects a {'long' if local['side'] > 0 else 'short'} position of {local['qty']} "
                    f"{self.symbol}, but Bybit shows none. It may have been closed by its stop or target while "
                    f"the runner was off, or by hand. Check the testnet order history, then delete {state_path} "
                    "to start flat.", now)
            self.state["position"] = local = None   # our own exit order closed it
        if local is not None and pos["qty"] > 0:
            same_qty = dec(local["qty"]) == pos["qty"] or exit_filled   # a partly filled exit leaves less
            if int(local["side"]) != pos["side"] or not same_qty:
                self.breaker.trip(
                    f"state file expects side {local['side']} qty {local['qty']}, Bybit shows side "
                    f"{pos['side']} qty {fmt(pos['qty'])}. Resolve on the testnet, then restart.", now)
            local.update({"qty": fmt(pos["qty"]), "avg_price": pos["avg_price"], "stop": pos["stop_loss"],
                          "take_profit": pos["take_profit"]})
        if pos["qty"] > 0 and not pos["stop_loss"]:
            self.breaker.trip(f"{self.symbol} position has no stop-loss on Bybit; set one or close it, then restart", now)
        if pos["qty"] > 0:
            self.breaker.check_position_value(pos["value"], now)

        foreign = [o for o in self.fetch_open_orders() if o.get("stopOrderType") not in POSITION_TPSL_TYPES]
        if foreign:
            ids = ", ".join(f"{o.get('orderLinkId') or o.get('orderId')} ({o.get('orderStatus')})" for o in foreign[:5])
            self.breaker.trip(f"{len(foreign)} open order(s) on {self.symbol} that this runner does not expect: "
                              f"{ids}. Cancel them on the testnet, then restart.", now)
        self.save()
        self.log("reconciled: state file and Bybit agree "
                 f"({'flat' if pos['qty'] == 0 else ('long ' if pos['side'] > 0 else 'short ') + fmt(pos['qty'])})")

    # ------------------------------------------------------------------ #
    # One bar
    # ------------------------------------------------------------------ #

    def process_latest_bar(self) -> str:
        """Handle the decision of the latest closed bar once. Returns a short status word.

        The consecutive-error count is reset only when a whole bar is handled
        without any API error, so an endpoint that keeps failing still trips the
        breaker even if other calls in between succeed."""
        self._cycle_clean = True
        status = self._process_latest_bar()
        if self._cycle_clean:
            self.breaker.api_success()
        return status

    def _process_latest_bar(self) -> str:
        self.breaker.raise_if_halted()
        self.sync_clock()
        bars = self.fetch_closed_bars()
        bar_ms = int(bars.ts[-1])
        last = self.state.get("last_bar_ms")
        if last is not None and bar_ms <= int(last):
            return "already-handled"

        problem = self.bars_problem(bars)
        if problem:
            self.log(f"bar {data_mod.iso_utc(bar_ms)}: NOT trading, data incomplete: {problem}")
            self.state["last_bar_ms"] = bar_ms
            self.save()
            return "gap"

        signals = self.generate(bars, self.params)
        decision = Decision.last(bars, signals)
        delay_s = (self.client.now_ms() - (bar_ms + self.step)) / 1000
        what = ("long" if decision.side > 0 else "short" if decision.side < 0 else "no entry")
        flags = "".join([" exit-long" if decision.exit_long else "", " exit-short" if decision.exit_short else ""])
        self.log(f"bar {data_mod.iso_utc(bar_ms)} close {decision.close}: {what}{flags}")

        if self.dry_run:
            position = self._paper_update(bars, last)
        else:
            self._resolve_pending()      # an order whose answer was lost on an earlier bar
            position = self._sync_position()
            equity = self.fetch_equity()
            loss = self.breaker.check_daily_loss(equity, self.client.now_ms())
            self.log(f"equity {equity:.2f} USDT, today's loss {loss:.2f} USDT")

        intents = self.planner.plan(decision, position, self.instrument, self.log)
        if intents and delay_s > self.config.max_signal_delay_seconds:
            self.log(f"not acting: {delay_s:.0f}s after the bar close (max_signal_delay_seconds="
                     f"{self.config.max_signal_delay_seconds}); the backtest would have filled at that bar's open")
            for intent in intents:
                body = json.dumps(intent.body(self.symbol), separators=(",", ":"))
                self.log(f"  order that was not sent: {body}")
            intents = []

        for intent in intents:
            notional = float(intent.qty * intent.ref_price)
            if intent.kind == "entry":
                self.breaker.check_order_notional(notional, self.client.now_ms())
            if self.dry_run:
                self._paper_execute(intent, decision)
                continue
            ok = self.execute(intent)
            if intent.kind == "exit" and not ok:
                self.log("exit did not complete; skipping the entry on this bar")
                break

        self.state["last_bar_ms"] = bar_ms
        self.save()
        return "done"

    def _sync_position(self) -> Optional[Dict[str, Any]]:
        """Between bars: Bybit is the truth for what the stop/target did."""
        pos = self.fetch_position()
        local = self.state.get("position")
        now = self.client.now_ms()
        if pos["qty"] > 0:
            self.breaker.check_position_value(pos["value"], now)
            if not pos["stop_loss"]:
                self.breaker.trip(f"{self.symbol} position has no stop-loss on Bybit", now)
        if local is None and pos["qty"] > 0:
            self.breaker.trip(f"unexpected {fmt(pos['qty'])} {self.symbol} position on Bybit "
                              "(not opened by this runner)", now)
        if local is not None and pos["qty"] == 0:
            self.log("position is closed on Bybit (its stop or target filled)")
            self.state["position"] = None
            self.save()
            return None
        if local is not None:
            if pos["side"] != int(local["side"]) or dec(local["qty"]) != pos["qty"]:
                self.breaker.trip(f"position changed outside this runner: expected side {local['side']} qty "
                                  f"{local['qty']}, Bybit shows side {pos['side']} qty {fmt(pos['qty'])}", now)
            return local
        return None

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #

    def _record_order(self, kind: str, order: Dict[str, Any]) -> None:
        self.state.setdefault("orders", []).append({
            "kind": kind, "order_link_id": order.get("orderLinkId"), "order_id": order.get("orderId"),
            "side": order.get("side"), "qty": order.get("qty"), "filled": order.get("cumExecQty"),
            "avg_price": order.get("avgPrice"), "status": order.get("orderStatus"),
            "reject_reason": order.get("rejectReason"),
        })

    def submit(self, intent: OrderIntent) -> Optional[Dict[str, Any]]:
        """Send one order at most once. Returns the order as Bybit reports it, or None if refused."""
        link = intent.order_link_id
        existing = self.find_order(link)
        if existing is not None:
            self.log(f"order {link} already exists on Bybit ({existing.get('orderStatus')}); not sending again")
            return existing
        self.state["pending_order"] = {"order_link_id": link, "kind": intent.kind}
        self.save()
        body = intent.body(self.symbol)
        self.log(f"placing {intent.kind}: {body['side']} {body['qty']} {self.symbol} {body['orderType']}"
                 + (f", stop {body['stopLoss']}" if "stopLoss" in body else "")
                 + (f", target {body['takeProfit']}" if "takeProfit" in body else "")
                 + f", id {link}")
        for attempt in range(3):
            try:
                self._call(f"create order {link}", lambda: self.client.signed_post("/v5/order/create", body))
                break
            except RateLimitError:
                # Refused by the limiter, so it was not placed. Try again on a later bar.
                self.state["pending_order"] = None
                self.save()
                raise
            except BybitError as exc:
                if exc.ret_code == DUPLICATE_ORDER_LINK_ID:
                    self.log(f"Bybit says {link} already exists; adopting it")
                    break
                self.log(f"order {link} refused: {exc}")
                self.state["pending_order"] = None
                self.state.setdefault("orders", []).append({"kind": intent.kind, "order_link_id": link,
                                                            "status": "RefusedOnCreate", "error": str(exc)[:200]})
                self.save()
                return None
            except NetworkError as exc:
                # Outcome unknown. Look it up before anything else; retry only with the same ID.
                self.log(f"no answer creating {link} ({exc}); checking whether it arrived")
                if self.find_order(link) is not None:
                    break
                if attempt == 2:
                    raise
        return self.wait_final(link)

    def wait_final(self, link: str) -> Dict[str, Any]:
        """Poll the order until it reaches a final status. Bybit's create call is asynchronous:
        "please use the websocket to confirm the order status"
        (https://bybit-exchange.github.io/docs/v5/order/create-order)."""
        polls = max(1, int(self.config.order_timeout_seconds / 0.5))
        order = None
        for poll in range(polls):
            order = self.find_order(link)
            if order is not None and order.get("orderStatus") in FINAL_STATUSES:
                return order
            if poll + 1 < polls:
                self._sleep(0.5)
        status = order.get("orderStatus") if order else "not found"
        self.breaker.trip(f"order {link} not final after {self.config.order_timeout_seconds}s "
                          f"(status: {status}); check it on the testnet", self.client.now_ms())
        raise AssertionError("unreachable")

    def execute(self, intent: OrderIntent) -> bool:
        """Submit an intent and update local state from what Bybit reports. True if it took effect."""
        order = self.submit(intent)
        self.state["pending_order"] = None
        if order is None:
            self.save()
            return False
        self._record_order(intent.kind, order)
        filled = dec(order.get("cumExecQty") or "0")
        status = order.get("orderStatus")
        now = self.client.now_ms()
        if filled == 0:
            self.log(f"order {intent.order_link_id} ended {status} with nothing filled"
                     + (f" ({order.get('rejectReason')})" if order.get("rejectReason") else ""))
            self.save()
            return False

        pos = self.fetch_position()
        if intent.kind == "entry":
            if pos["side"] != intent.side or pos["qty"] != filled:
                self.breaker.trip(f"after entry {intent.order_link_id} filled {fmt(filled)}, Bybit shows side "
                                  f"{pos['side']} qty {fmt(pos['qty'])}; expected side {intent.side} qty {fmt(filled)}", now)
            if filled < intent.qty:
                self.log(f"partial fill: {fmt(filled)} of {fmt(intent.qty)} ({status}); not chasing the rest. "
                         "The stop applies to the whole position (tpslMode Full).")
            if not pos["stop_loss"]:
                self.breaker.trip(f"entry filled but Bybit shows no stop-loss on the position", now)
            self.breaker.check_position_value(pos["value"], now)
            self.state["position"] = {"side": intent.side, "qty": fmt(pos["qty"]), "avg_price": pos["avg_price"],
                                      "stop": pos["stop_loss"], "take_profit": pos["take_profit"],
                                      "order_link_id": intent.order_link_id}
            self.log(f"position open: {'long' if intent.side > 0 else 'short'} {fmt(pos['qty'])} at "
                     f"{pos['avg_price']}, stop {pos['stop_loss']} on Bybit")
            self.save()
            return True

        # Exit
        if pos["qty"] == 0:
            self.state["position"] = None
            self.log(f"position closed by {intent.order_link_id} at {order.get('avgPrice')}")
            self.save()
            return True
        self.state["position"]["qty"] = fmt(pos["qty"])
        self.save()
        attempt = int(intent.order_link_id[-1]) + 1
        if attempt > 3:
            self.breaker.trip(f"exit incomplete after 3 orders; {fmt(pos['qty'])} {self.symbol} still open "
                              "(its stop is still on Bybit)", now)
        self.log(f"exit partly filled; {fmt(pos['qty'])} left, sending another reduce-only order")
        retry = OrderIntent(kind="exit", side=intent.side, qty=pos["qty"], reduce_only=True,
                            ref_price=intent.ref_price,
                            order_link_id=intent.order_link_id[:-1] + str(attempt))
        return self.execute(retry)

    # ------------------------------------------------------------------ #
    # Dry run: a paper position with the engine's stop/target rules
    # ------------------------------------------------------------------ #

    def _paper_update(self, bars: Bars, last_handled: Optional[int]) -> Optional[Dict[str, Any]]:
        pos = self.state.get("position")
        if not pos:
            return None
        side, stop = int(pos["side"]), float(pos["stop"])
        target = float(pos["take_profit"]) if pos.get("take_profit") else None
        for i in range(len(bars)):
            t = int(bars.ts[i])
            if t < int(pos["entry_bar_ms"]) or (last_handled is not None and t <= int(last_handled)):
                continue   # before the fill, or already checked on an earlier pass
            o, h, l = float(bars.open[i]), float(bars.high[i]), float(bars.low[i])
            hit = None
            if side == 1:
                if l <= stop:
                    hit = ("stop", min(o, stop))
                elif target is not None and h >= target:
                    hit = ("target", max(o, target))
            else:
                if h >= stop:
                    hit = ("stop", max(o, stop))
                elif target is not None and l <= target:
                    hit = ("target", min(o, target))
            if hit:
                self.log(f"[dry run] paper position would have closed by its {hit[0]} at {hit[1]} "
                         f"in bar {data_mod.iso_utc(t)}")
                self.state["position"] = None
                self.save()
                return None
        return pos

    def _paper_execute(self, intent: OrderIntent, decision: Decision) -> None:
        body = intent.body(self.symbol)
        self.log(f"[dry run] would send POST /v5/order/create {json.dumps(body, separators=(',', ':'))}")
        if intent.kind == "exit":
            self.state["position"] = None
        else:
            self.state["position"] = {"side": intent.side, "qty": fmt(intent.qty), "avg_price": fmt(intent.ref_price),
                                      "stop": fmt(intent.stop_loss),
                                      "take_profit": fmt(intent.take_profit) if intent.take_profit is not None else None,
                                      "order_link_id": intent.order_link_id,
                                      "entry_bar_ms": decision.bar_ms + self.step}
        self.save()

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #

    def run(self, once: bool = False) -> None:
        """Process bars until stopped. Each bar is handled ``settle_seconds`` after it closes."""
        while True:
            try:
                self.process_latest_bar()
            except (BybitError, NetworkError, DataError) as exc:
                # Counted by the breaker already; the next bar tries again.
                self.log(f"error on this bar, no order placed: {exc}")
            if once:
                return
            now = self.client.now_ms()
            next_handle = (now // self.step + 1) * self.step + int(self.config.settle_seconds * 1000)
            self._sleep(max(0.0, (next_handle - now) / 1000))
