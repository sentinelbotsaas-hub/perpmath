import numpy as np
import pytest

from perpmath.data import Bars

STEP = 900_000  # 15 minutes
T0 = 1_700_000_100_000 // STEP * STEP


def make_bars(rows, step=STEP, t0=T0):
    """rows: list of (open, high, low, close)."""
    arr = np.asarray(rows, dtype=np.float64)
    n = len(arr)
    return Bars(
        ts=t0 + step * np.arange(n, dtype=np.int64),
        open=arr[:, 0].copy(), high=arr[:, 1].copy(), low=arr[:, 2].copy(), close=arr[:, 3].copy(),
        volume=np.ones(n),
    )


def random_walk(n=3000, seed=7):
    """Deterministic synthetic OHLC. Each bar opens a little away from the previous close."""
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    open_ = np.empty(n)
    open_[0] = 100.0
    open_[1:] = close[:-1] * (1 + rng.normal(0, 0.0005, n - 1))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.002, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.002, n)))
    return make_bars(np.column_stack([open_, high, low, close]))


@pytest.fixture
def walk():
    return random_walk()
