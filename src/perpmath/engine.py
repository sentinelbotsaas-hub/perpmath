"""Bar-by-bar simulator.

Timing rules (these are what make look-ahead impossible inside the engine):

* A strategy decides on the close of bar ``i``. The engine acts on that
  decision at the open of bar ``i + 1``, never earlier.
* From the entry bar onwards, the position's stop and target are checked
  against each bar's high and low. If a bar opens beyond a level, the fill is
  the open (gap-through), not the level.
* If both stop and target are touched inside the same bar, the stop is
  assumed to have been hit first. The real order is unknown from OHLC data,
  so the engine takes the worse outcome.

Cost rules:

* Every fill is moved against the trader by ``slippage`` (a fraction of price).
* Every fill pays ``fee_rate`` on its notional (quantity x fill price).

One position at a time. Position notional is ``notional_fraction`` of current
equity (compounding). There is no leverage and no liquidation model in 0.1,
and funding payments are not modelled.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .data import Bars


@dataclass
class ExecutionConfig:
    fee_rate: float = 0.00055          # taker fee per side, as a fraction of notional
    slippage: float = 0.0002           # adverse price move per fill, as a fraction of price
    initial_capital: float = 10_000.0
    notional_fraction: float = 1.0     # position notional as a fraction of equity (max 1.0)
    max_hold_bars: Optional[int] = None  # close at the next open after this many bars

    def validate(self) -> None:
        # The 1% ceiling catches percentages typed as fractions (0.055 instead of 0.00055).
        if not 0 <= self.fee_rate <= 0.01:
            raise ValueError("fee_rate must be a fraction between 0 and 0.01; 0.055% is 0.00055")
        if not 0 <= self.slippage <= 0.01:
            raise ValueError("slippage must be a fraction between 0 and 0.01; 0.02% is 0.0002")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if not 0 < self.notional_fraction <= 1.0:
            raise ValueError("notional_fraction must be in (0, 1]; leverage is not modelled in 0.1")
        if self.max_hold_bars is not None and self.max_hold_bars < 1:
            raise ValueError("max_hold_bars must be >= 1 or null")


@dataclass
class Signals:
    """What a strategy decided on each bar's close. All arrays have one entry per bar.

    side        +1 = enter long, -1 = enter short, 0 = nothing.
    stop        Absolute stop price for the entry signalled on that bar. Required
                wherever ``side`` is non-zero (R multiples are measured against it).
    target_r    Take-profit distance as a multiple of risk (|fill - stop|).
                NaN = no target.
    exit_long   True = close an open long at the next bar's open.
    exit_short  True = close an open short at the next bar's open.
    """

    side: np.ndarray
    stop: np.ndarray
    target_r: np.ndarray
    exit_long: np.ndarray
    exit_short: np.ndarray

    @classmethod
    def empty(cls, n: int) -> "Signals":
        return cls(
            side=np.zeros(n, dtype=np.int8),
            stop=np.full(n, np.nan),
            target_r=np.full(n, np.nan),
            exit_long=np.zeros(n, dtype=bool),
            exit_short=np.zeros(n, dtype=bool),
        )

    def fields(self) -> Dict[str, np.ndarray]:
        return {"side": self.side, "stop": self.stop, "target_r": self.target_r,
                "exit_long": self.exit_long, "exit_short": self.exit_short}


@dataclass
class Trade:
    side: str               # "long" | "short"
    entry_index: int
    exit_index: int
    entry_time_ms: int      # open time of the bar the entry filled in
    exit_time_ms: int       # open time of the bar the exit happened in
    entry_price: float      # fill price, slippage included
    exit_price: float       # fill price, slippage included
    stop_price: float
    target_price: Optional[float]
    quantity: float
    notional: float         # quantity x entry_price
    fees: float             # entry fee + exit fee
    pnl: float              # net of fees
    return_pct: float       # pnl / notional x 100
    r_multiple: float       # pnl / (quantity x |entry_price - stop_price|)
    exit_reason: str        # "stop" | "target" | "signal" | "time" | "end"
    bars_held: int


@dataclass
class SimResult:
    trades: List[Trade] = field(default_factory=list)
    equity_ts: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    equity: np.ndarray = field(default_factory=lambda: np.zeros(0))
    initial_capital: float = 0.0
    signals_seen: int = 0
    signals_skipped_in_position: int = 0
    signals_skipped_invalid_stop: int = 0
    signals_skipped_no_equity: int = 0


def simulate(bars: Bars, signals: Signals, config: ExecutionConfig,
             start: int = 0, end: Optional[int] = None) -> SimResult:
    """Simulate bars ``[start, end)``.

    Only decisions made on bars inside the window are acted on, so the first
    possible fill is the open of bar ``start + 1``. A position still open on
    the last bar is closed at that bar's close (reason ``"end"``).
    """
    config.validate()
    n = len(bars)
    end = n if end is None else end
    if not 0 <= start < end <= n:
        raise ValueError("invalid simulation window")
    for name, arr in signals.fields().items():
        if len(arr) != n:
            raise ValueError(f"signals.{name} has {len(arr)} entries for {n} bars")

    fee, slip = config.fee_rate, config.slippage
    cash = config.initial_capital
    pos: Optional[dict] = None
    result = SimResult(initial_capital=config.initial_capital)
    equity = np.empty(end - start)

    def close(index: int, raw_price: float, reason: str) -> None:
        nonlocal cash, pos
        side = pos["side"]
        fill = raw_price * (1 - side * slip)
        gross = side * pos["qty"] * (fill - pos["entry"])
        exit_fee = pos["qty"] * fill * fee
        cash += gross - exit_fee
        fees = pos["entry_fee"] + exit_fee
        pnl = gross - fees
        result.trades.append(Trade(
            side="long" if side == 1 else "short",
            entry_index=pos["index"],
            exit_index=index,
            entry_time_ms=int(bars.ts[pos["index"]]),
            exit_time_ms=int(bars.ts[index]),
            entry_price=pos["entry"],
            exit_price=fill,
            stop_price=pos["stop"],
            target_price=pos["target"],
            quantity=pos["qty"],
            notional=pos["notional"],
            fees=fees,
            pnl=pnl,
            return_pct=pnl / pos["notional"] * 100,
            r_multiple=pnl / (pos["qty"] * pos["risk"]),
            exit_reason=reason,
            bars_held=index - pos["index"],
        ))
        pos = None

    for i in range(start, end):
        o, h, l, c = float(bars.open[i]), float(bars.high[i]), float(bars.low[i]), float(bars.close[i])
        prev = i - 1

        # 1. Act at this bar's open on what was decided at the previous close.
        if prev >= start:
            if pos is not None:
                wants_out = signals.exit_long[prev] if pos["side"] == 1 else signals.exit_short[prev]
                if wants_out:
                    close(i, o, "signal")
                elif config.max_hold_bars is not None and i - pos["index"] >= config.max_hold_bars:
                    close(i, o, "time")

            side = int(signals.side[prev])
            if side != 0:
                result.signals_seen += 1
                stop = float(signals.stop[prev])
                if not math.isfinite(stop):
                    raise ValueError(f"strategy gave an entry without a stop price at bar {prev}")
                entry = o * (1 + side * slip)
                risk = side * (entry - stop)
                if pos is not None:
                    result.signals_skipped_in_position += 1
                elif risk <= 0:
                    result.signals_skipped_invalid_stop += 1   # opened beyond the stop
                elif cash <= 0:
                    result.signals_skipped_no_equity += 1
                else:
                    notional = cash * config.notional_fraction
                    entry_fee = notional * fee
                    cash -= entry_fee
                    target_r = float(signals.target_r[prev])
                    pos = {
                        "side": side, "index": i, "entry": entry, "stop": stop, "risk": risk,
                        "target": entry + side * target_r * risk if math.isfinite(target_r) else None,
                        "qty": notional / entry, "notional": notional, "entry_fee": entry_fee,
                    }

        # 2. Stop and target inside this bar. Stop first when both are touched.
        if pos is not None:
            side, stop, target = pos["side"], pos["stop"], pos["target"]
            if side == 1:
                if l <= stop:
                    close(i, min(o, stop), "stop")
                elif target is not None and h >= target:
                    close(i, max(o, target), "target")
            else:
                if h >= stop:
                    close(i, max(o, stop), "stop")
                elif target is not None and l <= target:
                    close(i, min(o, target), "target")

        # 3. Position left open on the last bar is closed at its close.
        if pos is not None and i == end - 1:
            close(i, c, "end")

        # 4. Mark to market at the close.
        if pos is None:
            equity[i - start] = cash
        else:
            equity[i - start] = cash + pos["side"] * pos["qty"] * (c - pos["entry"])

    result.equity_ts = bars.ts[start:end].copy()
    result.equity = equity
    return result
