"""Minimal Bybit V5 REST client for the testnet. Standard library only.

Why not CCXT: it would be a new dependency, and its Bybit class can attach
its own broker tag to orders. This client sends exactly the headers written
below and nothing else.

Version 0.2 talks to the testnet only. The base URL is a constant and every
request checks the host again before anything is sent, so neither a config
file nor a caller can point it at mainnet.

Documentation this module relies on:

* Base URLs, signing, headers, response layout:
  https://bybit-exchange.github.io/docs/v5/guide
* Error codes: https://bybit-exchange.github.io/docs/v5/error
* Rate limits and the X-Bapi-Limit-* headers:
  https://bybit-exchange.github.io/docs/v5/rate-limit
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

# https://bybit-exchange.github.io/docs/v5/guide ("Testnet" base URL)
TESTNET_REST = "https://api-testnet.bybit.com"
TESTNET_HOST = "api-testnet.bybit.com"

# Hosts that place real orders with real money. Listed only so that they can be
# refused with a clear message. https://bybit-exchange.github.io/docs/v5/guide
MAINNET_HOSTS = frozenset({
    "api.bybit.com", "api.bytick.com", "api.bybit.tr", "api.bybit.kz",
    "api.bybitgeorgia.ge", "api.bybit.ae", "api.bybit.eu", "api.bybit.id",
    "api.manepa.jp", "api.spark-fintech.com",
})

MAINNET_REFUSAL = (
    "perpmath 0.2 only connects to the Bybit testnet. Live (mainnet) trading is "
    "planned for 0.3 and will not be enabled before the position guard exists."
)

DEFAULT_RECV_WINDOW_MS = 5000   # Bybit's default, https://bybit-exchange.github.io/docs/v5/guide

# retCodes, https://bybit-exchange.github.io/docs/v5/error
RATE_LIMIT_CODES = frozenset({10006, 10018})   # "Too many visits" / "Exceeded the IP Rate Limit"
TIMESTAMP_CODE = 10002                         # "The request time exceeds the time window range."
AUTH_CODES = frozenset({10003, 10004, 10005, 10010, 33004})  # bad key, bad sign, no permission, IP, expired
DUPLICATE_ORDER_LINK_ID = 110072               # "OrderLinkedID is duplicate"

# (method, url, headers, body) -> (HTTP status, response headers, body bytes)
Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, Dict[str, str], bytes]]


class BybitError(Exception):
    """Bybit answered with a non-zero retCode or an HTTP error."""

    def __init__(self, message: str, ret_code: Optional[int] = None, http_status: Optional[int] = None):
        super().__init__(message)
        self.ret_code = ret_code
        self.http_status = http_status


class RateLimitError(BybitError):
    """Rate limited. ``retry_at_ms`` is when Bybit says the limit resets (local clock), if known."""

    def __init__(self, message: str, ret_code: Optional[int] = None, http_status: Optional[int] = None,
                 retry_at_ms: Optional[int] = None):
        super().__init__(message, ret_code, http_status)
        self.retry_at_ms = retry_at_ms


class AuthError(BybitError):
    """The key is invalid, expired, lacks permission, or the signature is wrong."""


class NetworkError(Exception):
    """No usable answer arrived. For an order request the outcome is UNKNOWN:
    the order may or may not exist. Callers must look it up before retrying."""


class TestnetOnlyError(Exception):
    """Something tried to reach a host other than the Bybit testnet."""


def mask(secret: Optional[str]) -> str:
    """Safe description of a credential: its length and at most the first and last 4 characters."""
    if not secret:
        return "(empty)"
    if len(secret) < 12:
        return f"len={len(secret)}"
    return f"len={len(secret)}, {secret[:4]}...{secret[-4:]}"


def sign(secret: str, payload: str) -> str:
    """HMAC-SHA256 of ``payload`` with ``secret``, lowercase hex.

    ``payload`` is ``timestamp + api_key + recv_window + queryString`` for GET
    and ``timestamp + api_key + recv_window + jsonBodyString`` for POST.
    https://bybit-exchange.github.io/docs/v5/guide (Authentication)
    """
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def signature_payload(timestamp_ms: int, api_key: str, recv_window_ms: int, params_or_body: str) -> str:
    return f"{timestamp_ms}{api_key}{recv_window_ms}{params_or_body}"


def assert_testnet_url(url: str) -> None:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in MAINNET_HOSTS or host.endswith(".bybit.com") and host != TESTNET_HOST:
        raise TestnetOnlyError(f"refusing to contact {host}: {MAINNET_REFUSAL}")
    if host != TESTNET_HOST or not url.startswith(TESTNET_REST + "/"):
        raise TestnetOnlyError(f"refusing to contact {host or url!r}: only {TESTNET_REST} is allowed in 0.2")


def urllib_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes],
                     timeout: float = 10.0) -> Tuple[int, Dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read() or b""


class BybitClient:
    """Bybit V5 REST, testnet only.

    Without credentials only public endpoints work; ``signed_*`` raise. This
    is what ``--dry-run`` uses, so a dry run can never send a signed request.
    """

    def __init__(self, api_key: str = "", api_secret: str = "", *,
                 recv_window_ms: int = DEFAULT_RECV_WINDOW_MS, broker_id: str = "",
                 transport: Optional[Transport] = None,
                 clock: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 get_retries: int = 2):
        self._api_key = api_key
        self._api_secret = api_secret
        self.recv_window_ms = int(recv_window_ms)
        self.broker_id = broker_id
        self._transport = transport or urllib_transport
        self._clock = clock or time.time
        self._sleep = sleep or time.sleep
        self.get_retries = get_retries
        self.time_offset_ms = 0      # server minus local, set by sync_time()
        self._blocked_until_ms = 0   # from X-Bapi-Limit-* headers

    def __repr__(self) -> str:   # never show the secret, not even by accident in a traceback
        return f"BybitClient(testnet, key {mask(self._api_key)}, secret {mask(self._api_secret)})"

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def now_ms(self) -> int:
        """Best estimate of Bybit's clock: local clock plus the last measured offset."""
        return int(self._clock() * 1000) + self.time_offset_ms

    # ------------------------------------------------------------------ #
    # Public endpoints
    # ------------------------------------------------------------------ #

    def sync_time(self) -> int:
        """Measure server-minus-local clock offset in ms and remember it.

        https://bybit-exchange.github.io/docs/v5/market/time (result.timeNano)
        """
        before = self._clock()
        result, payload = self._request("GET", "/v5/market/time", {}, signed=False)
        after = self._clock()
        if "timeNano" in result:
            server_ms = int(result["timeNano"]) // 1_000_000
        else:
            server_ms = int(payload["time"])
        local_mid_ms = int((before + after) / 2 * 1000)
        self.time_offset_ms = server_ms - local_mid_ms
        return self.time_offset_ms

    def public_get(self, path: str, params: Mapping[str, Any]) -> Dict[str, Any]:
        return self._request("GET", path, params, signed=False)[0]

    # ------------------------------------------------------------------ #
    # Signed endpoints
    # ------------------------------------------------------------------ #

    def signed_get(self, path: str, params: Mapping[str, Any]) -> Dict[str, Any]:
        return self._request("GET", path, params, signed=True)[0]

    def signed_post(self, path: str, body: Mapping[str, Any]) -> Dict[str, Any]:
        return self._request("POST", path, body, signed=True)[0]

    # ------------------------------------------------------------------ #

    def _headers(self, signed: bool, to_sign: str) -> Dict[str, str]:
        headers = {"User-Agent": "perpmath", "Content-Type": "application/json"}
        if not signed:
            return headers
        if not self.has_credentials:
            raise AuthError("this client has no API key; signed requests are not possible (dry run?)")
        timestamp = self.now_ms()
        headers.update({
            "X-BAPI-API-KEY": self._api_key,
            "X-BAPI-TIMESTAMP": str(timestamp),
            "X-BAPI-RECV-WINDOW": str(self.recv_window_ms),
            "X-BAPI-SIGN": sign(self._api_secret, signature_payload(timestamp, self._api_key,
                                                                     self.recv_window_ms, to_sign)),
        })
        # Broker attribution header, sent only when broker_id is set (default: empty, not sent).
        # https://bybit-exchange.github.io/docs/v5/guide ("X-Referer or Referer: broker users only")
        if self.broker_id:
            headers["Referer"] = self.broker_id
        return headers

    def _request(self, method: str, path: str, params: Mapping[str, Any], signed: bool
                 ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if method == "GET":
            # The query string is signed exactly as it is sent.
            query = urllib.parse.urlencode([(k, str(v)) for k, v in params.items()])
            url = TESTNET_REST + path + (("?" + query) if query else "")
            body = None
            to_sign = query
        else:
            text = json.dumps(dict(params), separators=(",", ":"))
            url = TESTNET_REST + path
            body = text.encode("utf-8")
            to_sign = text
        assert_testnet_url(url)

        attempts = 1 + (self.get_retries if method == "GET" else 0)   # POSTs are never retried here
        last: Optional[Exception] = None
        for attempt in range(attempts):
            self._respect_limit()
            headers = self._headers(signed, to_sign)   # fresh timestamp each attempt
            try:
                status, resp_headers, raw = self._transport(method, url, headers, body)
            except (urllib.error.URLError, TimeoutError, OSError, ConnectionError) as exc:
                last = NetworkError(f"{method} {path}: {type(exc).__name__}: {exc}")
                if attempt + 1 < attempts:
                    self._sleep(1.0 + attempt)
                continue
            return self._decode(method, path, status, resp_headers, raw)
        assert last is not None
        raise last

    def _respect_limit(self) -> None:
        wait_ms = self._blocked_until_ms - int(self._clock() * 1000)
        if wait_ms > 0:
            self._sleep(min(wait_ms, 60_000) / 1000)

    def _note_limit_headers(self, headers: Mapping[str, str]) -> Optional[int]:
        """X-Bapi-Limit-Status = requests left; X-Bapi-Limit-Reset-Timestamp = reset time (server ms).
        https://bybit-exchange.github.io/docs/v5/rate-limit"""
        lower = {k.lower(): v for k, v in headers.items()}
        reset = lower.get("x-bapi-limit-reset-timestamp")
        left = lower.get("x-bapi-limit-status")
        try:
            reset_local = int(reset) - self.time_offset_ms if reset is not None else None
        except ValueError:
            reset_local = None
        if left is not None and reset_local is not None and left.strip() == "0":
            self._blocked_until_ms = max(self._blocked_until_ms, reset_local)
        return reset_local

    def _decode(self, method: str, path: str, status: int, headers: Mapping[str, str], raw: bytes
                ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        reset_local = self._note_limit_headers(headers)
        where = f"{method} {path}"
        if status in (403, 429):
            # 403: IP rate limit (or a blocked region); 429: system frequency protection.
            # https://bybit-exchange.github.io/docs/v5/error (HTTP codes) and /docs/v5/rate-limit
            # ("403, access too frequent" -> stop and wait at least 10 minutes).
            retry = reset_local if status == 429 else int(self._clock() * 1000) + 600_000
            self._blocked_until_ms = max(self._blocked_until_ms, retry or 0)
            raise RateLimitError(f"{where}: HTTP {status} (rate limited or forbidden)",
                                 http_status=status, retry_at_ms=retry)
        if status == 401:
            raise AuthError(f"{where}: HTTP 401 (key not accepted)", http_status=status)
        if status != 200:
            raise BybitError(f"{where}: HTTP {status}", http_status=status)
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise BybitError(f"{where}: response is not JSON", http_status=status) from exc
        if not isinstance(payload, dict) or "retCode" not in payload:
            raise BybitError(f"{where}: malformed response", http_status=status)
        code = payload.get("retCode")
        msg = str(payload.get("retMsg", ""))[:200]
        if code != 0:
            text = f"{where}: retCode={code} retMsg={msg!r}"
            if code in RATE_LIMIT_CODES:
                retry = reset_local or int(self._clock() * 1000) + 1000
                self._blocked_until_ms = max(self._blocked_until_ms, retry)
                raise RateLimitError(text, ret_code=code, retry_at_ms=retry)
            if code in AUTH_CODES:
                raise AuthError(text, ret_code=code)
            raise BybitError(text, ret_code=code)
        result = payload.get("result")
        if not isinstance(result, dict):
            result = {}
        return result, payload
