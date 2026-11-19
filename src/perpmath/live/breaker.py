"""Circuit breakers.

Each breaker, once tripped, records a reason in the state file and the
runner stops. It stays stopped across restarts until the user clears it with
``--clear-halt``; the conditions are then checked again, so a daily-loss halt
trips again straight away if the loss is still there.

Tripping does not close the position. The position keeps the stop-loss that
was attached to it on Bybit's side when it was opened, so it stays protected
while the runner is down. (Closing automatically after, say, five failed API
calls would mean trusting the same failing API to close correctly.)

Breakers:

* Daily loss: equity now versus the first equity reading of the current UTC
  day. Equity is Bybit's USDT ``equity``, which includes unrealised P&L.
* Position size: an order whose notional (quantity x reference price) is above
  the limit is refused and halts the runner; a position on Bybit whose value is
  above the limit also halts it.
* Consecutive API errors: every failed request counts (rate limits and
  refused orders included); the count goes back to zero only when a whole bar
  has been handled without any error.
* Clock skew: |Bybit clock - local clock| above the limit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from .config import LiveConfig


class Halted(Exception):
    """A breaker tripped or reconciliation failed. The runner must stop."""


def utc_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


class CircuitBreaker:
    def __init__(self, config: LiveConfig, state: Dict[str, Any], save: Callable[[], None]):
        self.config = config
        self.state = state
        self._save = save

    # -- tripping --------------------------------------------------------- #

    def trip(self, reason: str, now_ms: int) -> None:
        self.state["halt"] = {"reason": reason, "at_ms": int(now_ms)}
        self._save()
        raise Halted(reason)

    def raise_if_halted(self) -> None:
        halt = self.state.get("halt")
        if halt:
            raise Halted(f"halted earlier: {halt['reason']} (run again with --clear-halt after checking)")

    # -- conditions -------------------------------------------------------- #

    def check_clock(self, skew_ms: int, now_ms: int) -> None:
        if abs(skew_ms) > self.config.max_clock_skew_ms:
            self.trip(f"clock skew {skew_ms} ms exceeds max_clock_skew_ms={self.config.max_clock_skew_ms}; "
                      "fix the system clock (NTP) before running", now_ms)

    def check_daily_loss(self, equity: float, now_ms: int) -> float:
        """Returns today's loss so far (positive = loss)."""
        today = utc_date(now_ms)
        day = self.state.get("day")
        if not day or day.get("date") != today:
            self.state["day"] = {"date": today, "start_equity": float(equity)}
            self._save()
            return 0.0
        loss = float(day["start_equity"]) - float(equity)
        if loss > self.config.max_daily_loss_usdt:
            self.trip(f"daily loss {loss:.2f} USDT since {today} start equity {day['start_equity']:.2f} "
                      f"exceeds max_daily_loss_usdt={self.config.max_daily_loss_usdt}", now_ms)
        return loss

    def check_order_notional(self, notional: float, now_ms: int) -> None:
        if notional > self.config.max_position_notional_usdt:
            self.trip(f"order notional {notional:.2f} USDT exceeds "
                      f"max_position_notional_usdt={self.config.max_position_notional_usdt}", now_ms)

    def check_position_value(self, value: float, now_ms: int) -> None:
        # 2% tolerance: the fill price differs from the reference price used for sizing.
        if value > self.config.max_position_notional_usdt * 1.02:
            self.trip(f"position value {value:.2f} USDT on Bybit exceeds "
                      f"max_position_notional_usdt={self.config.max_position_notional_usdt}", now_ms)

    def api_success(self) -> None:
        if self.state.get("consecutive_api_errors"):
            self.state["consecutive_api_errors"] = 0
            self._save()

    def api_error(self, what: str, now_ms: int) -> None:
        count = int(self.state.get("consecutive_api_errors") or 0) + 1
        self.state["consecutive_api_errors"] = count
        self._save()
        if count >= self.config.max_consecutive_api_errors:
            self.trip(f"{count} consecutive API errors (max_consecutive_api_errors="
                      f"{self.config.max_consecutive_api_errors}); last: {what}", now_ms)

    def clear(self) -> Optional[Dict[str, Any]]:
        old = self.state.get("halt")
        self.state["halt"] = None
        self.state["consecutive_api_errors"] = 0
        self._save()
        return old
