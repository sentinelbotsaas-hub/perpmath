"""Grid trading simulator.

A grid holds many small positions at different prices at once, so it does
not fit the one-position ``Signals`` model of ``engine.py``. This module is a
separate simulator. It shares the bar data (``data.py``), the drawdown
definition (``metrics.py``) and the result-file conventions (``backtest.py``).

Grid rules
==========

* ``grid_count`` cells between ``lower`` and ``upper``: ``grid_count + 1``
  price levels ``p_0 < p_1 < ... < p_N``. ``arithmetic`` spacing makes every
  cell the same width in price, ``geometric`` the same width in percent.
* Each cell trades between its lower level ``p_k`` and its upper level
  ``p_k+1``. A *long cell* buys at ``p_k`` and sells at ``p_k+1``; a *short
  cell* sells at ``p_k+1`` and buys back at ``p_k``. Each cell holds at most
  one position. When a cell's position is closed by its own grid order, that
  is one completed round trip (one "grid pair").
* Size: every cell opens a position worth ``initial_capital x leverage /
  grid_count`` (USDT) at the price where the cell opens (``p_k`` for a long
  cell, ``p_k+1`` for a short cell). With ``leverage`` 1 a fully filled long
  grid holds about the starting capital in notional.
* Start: the grid is placed at the open of the first bar of the sample. With
  ``P0`` that open price and ``m_k`` the midpoint of cell ``k``:

  - ``long``: every cell is a long cell. Cells with ``m_k > P0`` are bought
    at market at ``P0`` (they wait to sell at ``p_k+1``); the others wait to
    buy at ``p_k``.
  - ``short``: every cell is a short cell. Cells with ``m_k < P0`` are sold
    short at market at ``P0`` (they wait to buy back at ``p_k``); the others
    wait to sell at ``p_k+1``.
  - ``neutral``: cells with ``m_k < P0`` are long cells, the rest short
    cells. Nothing is bought or sold at the start.

* Fills: a grid order fills only when price trades strictly through its
  level (touching it is not enough), and it fills at its level, never at a
  better price, even when price gapped past it. Then the slippage is applied
  against the trader on top of that.
* Leaving the range (``out_of_range``), meaning price trades strictly below
  ``lower`` or above ``upper``:

  - ``continue``: nothing happens. Orders stay, positions stay, the grid
    trades again if price comes back. This is how exchange grid bots behave
    unless a stop is set.
  - ``stop``: all grid orders are cancelled for the rest of the sample; the
    position is kept (and keeps paying or receiving funding) until the end.
  - ``close``: all grid orders are cancelled and the whole position is
    closed with a market order at the bound (at the bar's open if the price
    gapped past the bound between bars).

  ``stop`` and ``close`` only fire after price has been inside the range at
  least once, so a grid started outside its range waits until price enters.
* Leverage and liquidation, isolated margin: the grid's whole equity (starting
  capital plus realised profit, minus fees and funding) is the margin of its
  net position. The position is liquidated when equity falls to the
  maintenance margin, ``maintenance_margin_rate x |net quantity| x price``.
  This is the same simplified rule as the liquidation price calculator on
  perpmath.com (tier 1, no maintenance-margin deduction, closing fee left
  out). On liquidation every position is closed and the remaining equity is
  counted as lost, so the account ends the sample at zero. Liquidation is
  checked at the end points of every price segment of the bar path below;
  between two end points equity cannot be lower than at one of them, because
  the grid only adds to its position in the direction price is moving away
  from.
* End of sample: whatever is still open is closed at the last bar's close
  with a market order.

Intrabar path (the main modelling assumption)
=============================================

OHLC bars do not say whether the high or the low came first. Grid results
depend on it, because an order filled on the way down can be closed on the
way back up inside the same bar. The simulator walks each bar along a path
of straight segments, filling orders in the order price would meet them and
letting a cell that has just filled trade again later in the same bar:

* ``low_first``:  previous close -> open -> low -> high -> close
* ``high_first``: previous close -> open -> high -> low -> close
* ``worst_of_two`` (default): both paths are simulated for every bar and the
  one that leaves *lower* equity at the bar's close is kept (on a tie, the
  low-first one). This is the more pessimistic of the two simplest paths,
  chosen greedily bar by bar.
* ``best_of_two``: the one that leaves higher equity. Only for showing how
  wide the uncertainty is; not for headline results.

Paths with more swings inside a bar (likely in reality) would fill more grid
orders, so even ``best_of_two`` is not an upper bound on what a real grid
might have done, and ``worst_of_two`` is not a guaranteed lower bound either:
it is pessimistic about the order of the high and the low, and it fills
orders only on a strict cross at their level.

Costs
=====

* Grid orders are resting limit orders, charged ``grid_fee_rate`` (default
  0.02%, Bybit's VIP 0 maker rate for perpetuals). Market orders (initial
  position, ``close`` on leaving the range, the end-of-sample close) are
  charged ``taker_fee_rate`` (default 0.055%, Bybit's VIP 0 taker rate).
* ``slippage`` moves every fill against the trader, limit fills included.
* Funding: at each funding time the net position pays
  ``net quantity x price x rate`` (longs pay when the rate is positive),
  where price is the close of the bar that ends at the funding time. Bybit
  uses the mark price; the difference is ignored. Funding is applied when a
  ``FundingRates`` series is passed, and never silently skipped: without one
  the result says ``funding_modelled: false``.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

from .data import Bars
from .funding import FundingRates
from .metrics import max_drawdown_pct

DIRECTIONS = ("neutral", "long", "short")
SPACINGS = ("arithmetic", "geometric")
OUT_OF_RANGE = ("continue", "stop", "close")
PATHS = ("worst_of_two", "low_first", "high_first", "best_of_two")

# Event tuple layout: (bar index, kind, side, cell, level, price, quantity, fee, pnl)
# kind: "initial" | "grid" | "range_close" | "end" | "liquidation" | "range_stop"
Event = Tuple[int, str, str, Optional[int], Optional[float], float, float, float, Optional[float]]


@dataclass
class GridConfig:
    lower: float
    upper: float
    grid_count: int = 50
    spacing: str = "geometric"
    direction: str = "neutral"
    leverage: float = 1.0
    out_of_range: str = "continue"
    grid_fee_rate: float = 0.0002            # resting limit orders (maker)
    taker_fee_rate: float = 0.00055          # market orders
    slippage: float = 0.0002                 # adverse price move per fill, fraction of price
    initial_capital: float = 10_000.0
    maintenance_margin_rate: float = 0.0033  # Bybit BTCUSDT risk tier 1
    intrabar_path: str = "worst_of_two"

    def validate(self) -> None:
        for name in ("lower", "upper", "leverage", "initial_capital"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be a number, got {value!r}")
        if self.lower <= 0:
            raise ValueError("lower must be a positive price")
        if self.upper <= self.lower:
            raise ValueError("upper must be above lower")
        if isinstance(self.grid_count, bool) or not isinstance(self.grid_count, int):
            raise ValueError(f"grid_count must be a whole number, got {self.grid_count!r}")
        if not 2 <= self.grid_count <= 1000:
            raise ValueError("grid_count must be between 2 and 1000")
        if self.spacing not in SPACINGS:
            raise ValueError(f"spacing must be one of {', '.join(SPACINGS)}")
        if self.direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {', '.join(DIRECTIONS)}")
        if self.out_of_range not in OUT_OF_RANGE:
            raise ValueError(f"out_of_range must be one of {', '.join(OUT_OF_RANGE)}")
        if self.intrabar_path not in PATHS:
            raise ValueError(f"intrabar_path must be one of {', '.join(PATHS)}")
        for name in ("grid_fee_rate", "taker_fee_rate", "slippage"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 0.01:
                raise ValueError(f"{name} must be a fraction between 0 and 0.01; 0.02% is 0.0002")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        mmr = self.maintenance_margin_rate
        if isinstance(mmr, bool) or not isinstance(mmr, (int, float)) or not 0 <= mmr < 0.5:
            raise ValueError("maintenance_margin_rate must be a fraction between 0 and 0.5; 0.33% is 0.0033")
        if not 1 <= self.leverage <= 100:
            raise ValueError("leverage must be between 1 and 100")
        if 1 / self.leverage <= mmr:
            raise ValueError("leverage too high: 1/leverage must be above the maintenance margin rate")


def grid_levels(lower: float, upper: float, count: int, spacing: str) -> np.ndarray:
    """``count + 1`` prices from ``lower`` to ``upper`` inclusive."""
    k = np.arange(count + 1, dtype=np.float64)
    if spacing == "arithmetic":
        levels = lower + (upper - lower) * k / count
    elif spacing == "geometric":
        levels = lower * (upper / lower) ** (k / count)
    else:
        raise ValueError(f"spacing must be one of {', '.join(SPACINGS)}")
    levels[0], levels[-1] = lower, upper
    return levels


def narrowest_cell_pct(levels: np.ndarray) -> float:
    """Width of the narrowest cell, in percent of its lower level."""
    return float(np.min(np.diff(levels) / levels[:-1]) * 100)


@dataclass
class GridResult:
    config: GridConfig
    levels: np.ndarray
    start_price: float
    equity_ts: np.ndarray
    equity: np.ndarray
    position_notional: np.ndarray   # |net quantity| x close, at every bar close
    initial_capital: float
    events: List[Event] = field(default_factory=list)
    round_trips: int = 0
    grid_fills: int = 0
    market_fills: int = 0
    trading_pnl: float = 0.0
    grid_fees: float = 0.0
    market_fees: float = 0.0
    funding_paid: float = 0.0
    funding_events: int = 0
    liquidated: bool = False
    liquidation_index: Optional[int] = None
    liquidation_loss: float = 0.0
    range_exit_index: Optional[int] = None
    bars_outside_range: int = 0
    funding_modelled: bool = False


class _State:
    """Everything that changes while the grid runs. Copied once per path per bar."""

    __slots__ = ("held", "n_held", "entry", "cash", "qty", "cost", "active", "armed", "liquidated",
                 "round_trips", "grid_fills", "market_fills", "trading_pnl", "grid_fees",
                 "market_fees", "liquidation_loss", "range_exit")

    def copy(self) -> "_State":
        other = _State.__new__(_State)
        for name in _State.__slots__:
            setattr(other, name, getattr(self, name))
        other.held = list(self.held)
        other.entry = list(self.entry)
        return other

    def equity(self, price: float) -> float:
        return self.cash + self.qty * price - self.cost


class _Grid:
    def __init__(self, bars: Bars, config: GridConfig, funding: Optional[FundingRates]):
        self.bars = bars
        self.c = config
        self.funding = funding
        self.levels = grid_levels(config.lower, config.upper, config.grid_count, config.spacing)
        self.level_list = [float(x) for x in self.levels]
        self.n_cells = config.grid_count

    # ---------------------------------------------------------------- setup --
    def setup(self, index: int, p0: float, events: List[Event]) -> _State:
        c, lv = self.c, self.level_list
        cell_notional = c.initial_capital * c.leverage / self.n_cells
        self.kind: List[int] = []
        self.qty: List[float] = []
        st = _State()
        st.held = [False] * self.n_cells
        st.n_held = 0
        st.entry = [0.0] * self.n_cells
        st.cash = c.initial_capital
        st.qty = st.cost = 0.0
        st.active, st.liquidated = True, False
        st.armed = lv[0] <= p0 <= lv[-1]
        st.round_trips = st.grid_fills = st.market_fills = 0
        st.trading_pnl = st.grid_fees = st.market_fees = st.liquidation_loss = 0.0
        st.range_exit = None
        initial: List[int] = []
        for k in range(self.n_cells):
            mid = (lv[k] + lv[k + 1]) / 2
            if c.direction == "long":
                kind = 1
            elif c.direction == "short":
                kind = -1
            else:
                kind = 1 if mid < p0 else -1
            self.kind.append(kind)
            self.qty.append(cell_notional / (lv[k] if kind == 1 else lv[k + 1]))
            if (c.direction == "long" and mid > p0) or (c.direction == "short" and mid < p0):
                initial.append(k)
        if initial:
            side = self.kind[initial[0]]
            fill = p0 * (1 + side * c.slippage)
            total = 0.0
            for k in initial:
                q = self.qty[k]
                st.held[k], st.entry[k] = True, fill
                st.n_held += 1
                st.qty += side * q
                st.cost += side * q * fill
                total += q
            fee = total * fill * c.taker_fee_rate
            st.cash -= fee
            st.market_fees += fee
            st.market_fills += 1
            events.append((index, "initial", "buy" if side == 1 else "sell", None, None,
                           fill, total, fee, None))
        return st

    # ------------------------------------------------------------- fills --
    def _grid_fill(self, st: _State, index: int, k: int, level: float, side: int,
                   events: List[Event]) -> None:
        c = self.c
        q, kind = self.qty[k], self.kind[k]
        fill = level * (1 + side * c.slippage)
        fee = q * fill * c.grid_fee_rate
        st.cash -= fee
        st.grid_fees += fee
        st.grid_fills += 1
        pnl: Optional[float] = None
        if st.held[k]:                       # this fill closes the cell's position
            pnl = kind * q * (fill - st.entry[k])
            st.cash += pnl
            st.trading_pnl += pnl
            st.qty -= kind * q
            st.cost -= kind * q * st.entry[k]
            st.held[k] = False
            st.n_held -= 1
            if st.n_held == 0:
                st.qty = st.cost = 0.0           # no rounding dust once every cell is flat
            st.round_trips += 1
        else:                                # this fill opens it
            st.held[k], st.entry[k] = True, fill
            st.n_held += 1
            st.qty += kind * q
            st.cost += kind * q * fill
        events.append((index, "grid", "buy" if side == 1 else "sell", k, level, fill, q, fee, pnl))

    def close_all(self, st: _State, index: int, price: float, kind: str, events: List[Event]) -> None:
        """Close the net position with one market order at ``price`` (before slippage)."""
        if st.n_held == 0:
            return
        c = self.c
        side = -1 if st.qty > 0 else 1                    # sell to close a long, buy to close a short
        fill = price * (1 + side * c.slippage)
        pnl = st.qty * fill - st.cost
        fee = abs(st.qty) * fill * c.taker_fee_rate
        st.cash += pnl - fee
        st.trading_pnl += pnl
        st.market_fees += fee
        st.market_fills += 1
        events.append((index, kind, "buy" if side == 1 else "sell", None, None, fill, abs(st.qty), fee, pnl))
        st.qty = st.cost = 0.0
        st.held = [False] * self.n_cells
        st.n_held = 0

    def _liquidate_if_needed(self, st: _State, index: int, price: float, events: List[Event]) -> None:
        if st.liquidated or st.n_held == 0:
            return
        if st.equity(price) > self.c.maintenance_margin_rate * abs(st.qty) * price:
            return
        pnl = st.qty * price - st.cost
        st.cash += pnl
        st.trading_pnl += pnl
        st.market_fills += 1
        events.append((index, "liquidation", "sell" if st.qty > 0 else "buy", None, None,
                       price, abs(st.qty), 0.0, pnl))
        st.liquidation_loss += st.cash     # what was left of the margin is lost
        st.cash = 0.0
        st.qty = st.cost = 0.0
        st.held = [False] * self.n_cells
        st.n_held = 0
        st.active, st.liquidated = False, True

    # -------------------------------------------------------------- path --
    def move(self, st: _State, index: int, a: float, b: float, gap: bool, events: List[Event]) -> None:
        """Price moves in a straight line from ``a`` to ``b``."""
        if a == b or st.liquidated:
            return
        lv, held, kind = self.level_list, st.held, self.kind
        if st.active:
            if b < a:
                # Levels with b < p_j <= a, met from the top down. Moving down
                # through p_j can only fill cell j's buy order.
                for j in range(bisect_right(lv, a) - 1, bisect_right(lv, b) - 1, -1):
                    if j < self.n_cells and ((kind[j] == 1) != held[j]):
                        self._grid_fill(st, index, j, lv[j], 1, events)
            else:
                # Levels with a <= p_j < b, met from the bottom up. Moving up
                # through p_j can only fill the sell order of cell j - 1.
                for j in range(bisect_left(lv, a), bisect_left(lv, b)):
                    k = j - 1
                    if k >= 0 and ((kind[k] == 1) == held[k]):
                        self._grid_fill(st, index, k, lv[j], -1, events)
            outside = b < lv[0] or b > lv[-1]
            if outside and st.armed and self.c.out_of_range != "continue":
                st.active = False
                st.range_exit = index
                if self.c.out_of_range == "close":
                    bound = lv[0] if b < lv[0] else lv[-1]
                    self.close_all(st, index, b if gap else bound, "range_close", events)
                else:
                    events.append((index, "range_stop", "none", None, None, b if gap else
                                   (lv[0] if b < lv[0] else lv[-1]), 0.0, 0.0, None))
            elif not outside:
                st.armed = True
        self._liquidate_if_needed(st, index, b, events)

    def run_bar(self, st: _State, index: int, prev_close: Optional[float], low_first: bool,
                events: List[Event]) -> None:
        bars = self.bars
        o, h, l, c = (float(bars.open[index]), float(bars.high[index]),
                      float(bars.low[index]), float(bars.close[index]))
        if prev_close is not None:
            self.move(st, index, prev_close, o, True, events)
        points = (o, l, h, c) if low_first else (o, h, l, c)
        for a, b in zip(points[:-1], points[1:]):
            self.move(st, index, a, b, False, events)


def _bar_step_ms(bars: Bars, step_ms: Optional[int]) -> int:
    if step_ms is not None:
        return int(step_ms)
    return int(np.min(np.diff(bars.ts))) if len(bars) > 1 else 0


def _rates_at_closes(bars: Bars, start: int, end: int, step_ms: int,
                     funding: Optional[FundingRates]) -> np.ndarray:
    """Funding rate settled at each bar close in ``[start, end)``, NaN where none.

    A funding time is assigned to the first bar whose close is at or after
    it. A settlement at the first bar's open (when the grid is only just
    being placed) is not counted.
    """
    rates = np.full(end - start, np.nan)
    if funding is None or not len(funding) or not step_ms:
        return rates
    close_ts = bars.ts[start:end] + step_ms
    window = (funding.ts > bars.ts[start]) & (funding.ts <= close_ts[-1])
    where = np.searchsorted(close_ts, funding.ts[window], side="left")
    for i, rate in zip(where, funding.rate[window]):
        rates[i] = rate if np.isnan(rates[i]) else rates[i] + rate
    return rates


def simulate_grid(bars: Bars, config: GridConfig, start: int = 0, end: Optional[int] = None,
                  funding: Optional[FundingRates] = None, step_ms: Optional[int] = None,
                  close_at_end: bool = True) -> GridResult:
    """Run the grid on bars ``[start, end)``. The grid is placed at the open of bar ``start``.

    ``step_ms`` is the bar length, used to put funding at bar closes; by
    default the shortest gap between bar open times.

    ``close_at_end=False`` leaves the position open after the last bar, so
    that bar ends exactly as it would in a longer run. Only the look-ahead
    check uses it; a result always closes at the end.
    """
    config.validate()
    n = len(bars)
    end = n if end is None else end
    if not 0 <= start < end <= n:
        raise ValueError("invalid simulation window")
    grid = _Grid(bars, config, funding)
    rate_at_close = _rates_at_closes(bars, start, end, _bar_step_ms(bars, step_ms), funding)

    events: List[Event] = []
    p0 = float(bars.open[start])
    st = grid.setup(start, p0, events)
    equity = np.empty(end - start)
    notional = np.empty(end - start)
    funding_paid, funding_events, outside = 0.0, 0, 0
    lower, upper = grid.level_list[0], grid.level_list[-1]
    mode = config.intrabar_path

    for i in range(start, end):
        prev_close = float(bars.close[i - 1]) if i > start else None
        c = float(bars.close[i])
        if mode in ("low_first", "high_first"):
            grid.run_bar(st, i, prev_close, mode == "low_first", events)
        else:
            low_state, low_events = st.copy(), []
            grid.run_bar(low_state, i, prev_close, True, low_events)
            high_state, high_events = st.copy(), []
            grid.run_bar(high_state, i, prev_close, False, high_events)
            low_eq, high_eq = low_state.equity(c), high_state.equity(c)
            take_high = high_eq < low_eq if mode == "worst_of_two" else high_eq > low_eq
            st, bar_events = (high_state, high_events) if take_high else (low_state, low_events)
            events.extend(bar_events)

        rate = rate_at_close[i - start]
        if not np.isnan(rate) and st.qty != 0.0:
            payment = st.qty * c * rate
            st.cash -= payment
            funding_paid += payment
            funding_events += 1
            grid._liquidate_if_needed(st, i, c, events)

        if i == end - 1 and close_at_end:
            grid.close_all(st, i, c, "end", events)
        if c < lower or c > upper:
            outside += 1
        equity[i - start] = st.equity(c)
        notional[i - start] = abs(st.qty) * c

    liquidation_index = next((e[0] for e in events if e[1] == "liquidation"), None)
    return GridResult(
        config=config,
        levels=grid.levels,
        start_price=p0,
        equity_ts=bars.ts[start:end].copy(),
        equity=equity,
        position_notional=notional,
        initial_capital=config.initial_capital,
        events=events,
        round_trips=st.round_trips,
        grid_fills=st.grid_fills,
        market_fills=st.market_fills,
        trading_pnl=st.trading_pnl,
        grid_fees=st.grid_fees,
        market_fees=st.market_fees,
        funding_paid=funding_paid,
        funding_events=funding_events,
        liquidated=st.liquidated,
        liquidation_index=liquidation_index,
        liquidation_loss=st.liquidation_loss,
        range_exit_index=st.range_exit,
        bars_outside_range=outside,
        funding_modelled=funding is not None,
    )


@dataclass
class BuyAndHold:
    return_pct: float
    max_drawdown_pct: float
    funding_paid: float
    return_with_funding_pct: float


def buy_and_hold(bars: Bars, start: int, end: int, initial_capital: float, taker_fee_rate: float,
                 slippage: float, funding: Optional[FundingRates] = None,
                 step_ms: Optional[int] = None) -> BuyAndHold:
    """1x long perpetual: market buy at the first open, market sell at the last close.

    ``return_pct`` leaves funding out (like holding spot without a spot fee
    difference); ``return_with_funding_pct`` charges the same funding a 1x
    long perpetual would have paid.
    """
    entry = float(bars.open[start]) * (1 + slippage)
    qty = initial_capital / entry
    cash = initial_capital - initial_capital * taker_fee_rate
    close = bars.close[start:end].astype(np.float64)
    marked = cash + qty * (close - entry)
    exit_fill = float(close[-1]) * (1 - slippage)
    final = cash + qty * (exit_fill - entry) - qty * exit_fill * taker_fee_rate
    marked[-1] = final
    rates = _rates_at_closes(bars, start, end, _bar_step_ms(bars, step_ms), funding)
    settled = ~np.isnan(rates)
    paid = float(np.sum(qty * close[settled] * rates[settled]))
    return BuyAndHold(
        return_pct=(final / initial_capital - 1) * 100,
        max_drawdown_pct=max_drawdown_pct(marked, initial_capital),
        funding_paid=paid,
        return_with_funding_pct=((final - paid) / initial_capital - 1) * 100,
    )
