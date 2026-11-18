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
