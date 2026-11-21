"""Indicators used by the example strategies.

Every function returns an array the same length as its input. Positions that
do not yet have a full window are NaN; nothing is back-filled, so a value at
index ``i`` never depends on data after ``i``.
"""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def _rolling(arr: np.ndarray, window: int, func) -> np.ndarray:
    if window < 1:
        raise ValueError("window must be >= 1")
    arr = np.asarray(arr, dtype=np.float64)
    out = np.full(len(arr), np.nan)
    if len(arr) >= window:
        out[window - 1:] = func(sliding_window_view(arr, window), axis=1)
    return out


def sma(arr: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average of the last ``period`` values, current bar included."""
    return _rolling(arr, period, np.mean)


def rolling_max(arr: np.ndarray, window: int) -> np.ndarray:
    """Highest of the last ``window`` values, current bar included."""
    return _rolling(arr, window, np.max)


def rolling_min(arr: np.ndarray, window: int) -> np.ndarray:
    """Lowest of the last ``window`` values, current bar included."""
    return _rolling(arr, window, np.min)


def _shift1(arr: np.ndarray) -> np.ndarray:
    out = np.full(len(arr), np.nan)
    out[1:] = arr[:-1]
    return out


def prior_high(high: np.ndarray, window: int) -> np.ndarray:
    """Highest high of the ``window`` bars before the current bar (current bar excluded)."""
    return _shift1(rolling_max(high, window))


def prior_low(low: np.ndarray, window: int) -> np.ndarray:
    """Lowest low of the ``window`` bars before the current bar (current bar excluded)."""
    return _shift1(rolling_min(low, window))


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """True range. The first bar has no previous close, so it is just high - low."""
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    close = np.asarray(close, dtype=np.float64)
    tr = high - low
    if len(tr) > 1:
        prev_close = close[:-1]
        tr[1:] = np.maximum(
            tr[1:],
            np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)),
        )
    return tr


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Average true range as a simple mean of the last ``period`` true ranges.

    This is the plain-average ATR, not Wilder's smoothed version.
    """
    return sma(true_range(high, low, close), period)


def _confirmed_swing(arr: np.ndarray, left: int, right: int, sign: float) -> np.ndarray:
    if left < 1 or right < 1:
        raise ValueError("left and right must be >= 1")
    arr = np.asarray(arr, dtype=np.float64)
    out = np.full(len(arr), np.nan)
    window = left + right + 1
    if len(arr) >= window:
        views = sliding_window_view(sign * arr, window)   # each window ends on the confirming bar
        pivot = views[:, left]
        is_swing = (pivot > views[:, :left].max(axis=1)) & (pivot > views[:, left + 1:].max(axis=1))
        out[window - 1:] = np.where(is_swing, sign * pivot, np.nan)
    return out


def confirmed_swing_high(high: np.ndarray, left: int, right: int) -> np.ndarray:
    """Swing highs, each reported on the bar that confirms it.

    Bar ``p`` is a swing high when its high is strictly above the highs of the
    ``left`` bars before it and of the ``right`` bars after it. That is only
    known once bar ``p + right`` has closed, so the level is written at index
    ``p + right``, never at ``p``. Every other position is NaN. A tie with a
    neighbouring bar means no swing.
    """
    return _confirmed_swing(high, left, right, 1.0)


def confirmed_swing_low(low: np.ndarray, left: int, right: int) -> np.ndarray:
    """Swing lows, each reported on the bar that confirms it (see ``confirmed_swing_high``)."""
    return _confirmed_swing(low, left, right, -1.0)


def carry_forward(arr: np.ndarray) -> np.ndarray:
    """The most recent non-NaN value at or before each index; NaN until the first one."""
    arr = np.asarray(arr, dtype=np.float64)
    seen = np.where(np.isnan(arr), -1, np.arange(len(arr)))
    seen = np.maximum.accumulate(seen) if len(arr) else seen
    return np.where(seen >= 0, arr[np.maximum(seen, 0)], np.nan)
