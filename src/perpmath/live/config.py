"""Live-runner configuration: limits, sizing and the testnet-only rule.

A live config file is the backtest config with one more section, ``live``.
The ``execution`` and ``split`` sections are backtest assumptions; they are
accepted (so one file can serve both commands) and ignored here.

    {
      "strategy": "ma_cross",
      "params": {...},
      "live": {"order_notional_usdt": 100, "max_daily_loss_usdt": 20, ...}
    }

Unknown keys are errors, as in the backtest config.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, fields
from typing import Any, Dict, Tuple

from ..strategies import resolve_params
from .bybit import MAINNET_REFUSAL

API_KEY_ENV = "BYBIT_TESTNET_API_KEY"
API_SECRET_ENV = "BYBIT_TESTNET_API_SECRET"

_BROKER_ID = re.compile(r"^[A-Za-z0-9_-]{0,64}$")


class LiveConfigError(Exception):
    """The live configuration is invalid or asks for something 0.2 does not allow."""


@dataclass
class LiveConfig:
    network: str = "testnet"               # the only accepted value in 0.2
    order_notional_usdt: float = 100.0     # size of each entry, quantity = this / last close
    # Circuit breakers. Each one stops the runner and records why.
    max_position_notional_usdt: float = 200.0  # largest position value allowed (USDT)
    max_daily_loss_usdt: float = 20.0          # equity drop since the first reading of the UTC day
    max_consecutive_api_errors: int = 5
    # |Bybit clock - local clock|. Request timestamps are corrected by the measured
    # offset, so this is a sanity limit on the machine's clock, not Bybit's 1 s rule.
    max_clock_skew_ms: int = 5000
    # Plumbing
    recv_window_ms: int = 5000
    history_bars: int = 500                # closed bars given to the strategy each time (max 1000)
    settle_seconds: float = 3.0            # wait after a bar closes before asking for it
    max_signal_delay_seconds: float = 60.0  # act only this soon after the close ("next bar's open")
    order_timeout_seconds: float = 20.0    # how long to wait for a market order to reach a final state
    state_dir: str = ".perpmath-live"
    # Bybit API Broker attribution. Empty = no broker header is sent. See README.
    broker_id: str = ""

    def validate(self) -> None:
        if self.network != "testnet":
            raise LiveConfigError(f"network {self.network!r} refused. {MAINNET_REFUSAL}")
        if not _BROKER_ID.match(self.broker_id or ""):
            raise LiveConfigError("broker_id may contain only letters, digits, '-' and '_' (max 64)")
        positive = ("order_notional_usdt", "max_position_notional_usdt", "max_daily_loss_usdt",
                    "max_consecutive_api_errors", "max_clock_skew_ms", "recv_window_ms",
                    "history_bars", "order_timeout_seconds", "max_signal_delay_seconds")
        for name in positive:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise LiveConfigError(f"{name} must be a positive number, got {value!r}")
        for name in ("max_consecutive_api_errors", "max_clock_skew_ms", "recv_window_ms", "history_bars"):
            if not isinstance(getattr(self, name), int):
                raise LiveConfigError(f"{name} must be a whole number")
        if self.settle_seconds < 0:
            raise LiveConfigError("settle_seconds must be >= 0")
        if self.order_notional_usdt > self.max_position_notional_usdt:
            raise LiveConfigError("order_notional_usdt is larger than max_position_notional_usdt")
        if not 50 <= self.history_bars <= 1000:
            raise LiveConfigError("history_bars must be between 50 and 1000 (one kline request)")
        if self.recv_window_ms > 60_000:
            raise LiveConfigError("recv_window_ms above 60000 is not useful; Bybit's default is 5000")


# Keys that would let a file choose where requests go. They do not exist on
# purpose: the testnet URL is a constant in bybit.py.
_URL_KEYS = {"base_url", "url", "endpoint", "host", "api_url", "rest_url", "mainnet", "testnet"}


def parse_live_config(raw: Any, strategy: str) -> Tuple[Dict[str, Any], LiveConfig]:
    """Validate a live config document. Returns (strategy params, LiveConfig)."""
    if not isinstance(raw, dict):
        raise LiveConfigError("config must be a JSON object")
    unknown = sorted(set(raw) - {"strategy", "params", "execution", "split", "live"})
    if unknown:
        raise LiveConfigError(f"unknown config key(s): {', '.join(unknown)}")
    if raw.get("strategy", strategy) != strategy:
        raise LiveConfigError(f"config is for strategy {raw['strategy']!r}, not {strategy!r}")
    params_raw = raw.get("params", {})
    live_raw = raw.get("live", {})
    if not isinstance(params_raw, dict) or not isinstance(live_raw, dict):
        raise LiveConfigError("config sections 'params' and 'live' must be JSON objects")
    url_like = sorted(set(live_raw) & _URL_KEYS)
    if url_like:
        raise LiveConfigError(
            f"live.{url_like[0]} is not a setting: the endpoint is fixed to the Bybit testnet. {MAINNET_REFUSAL}")
    allowed = {f.name for f in fields(LiveConfig)}
    unknown_live = sorted(set(live_raw) - allowed)
    if unknown_live:
        raise LiveConfigError(f"unknown live setting(s): {', '.join(unknown_live)}")
    try:
        params = resolve_params(strategy, params_raw)
    except ValueError as exc:
        raise LiveConfigError(str(exc)) from exc
    config = LiveConfig(**live_raw)
    config.validate()
    return params, config


def load_live_config(path: str, strategy: str) -> Tuple[Dict[str, Any], LiveConfig]:
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise LiveConfigError(f"cannot read config {path}: {exc}") from exc
    return parse_live_config(raw, strategy)


def read_credentials(environ: Dict[str, str] = None) -> Tuple[str, str]:
    """API key and secret from the environment only. Never from a file argument."""
    env = os.environ if environ is None else environ
    key = env.get(API_KEY_ENV, "").strip()
    secret = env.get(API_SECRET_ENV, "").strip()
    if not key or not secret:
        raise LiveConfigError(
            f"set {API_KEY_ENV} and {API_SECRET_ENV} (testnet keys, trade permission only), "
            "or use --dry-run, which needs no key")
    return key, secret
