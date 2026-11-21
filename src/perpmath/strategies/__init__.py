"""Example strategies.

A strategy is a module with:

* ``DEFAULTS``: dict of parameter names and default values.
* ``generate(bars, params) -> Signals``: decisions per bar, each using only
  that bar and earlier bars.

These are teaching examples with fixed, conventional parameters. None of them
is claimed to be profitable.
"""

from __future__ import annotations

from typing import Any, Dict

from . import break_of_structure, breaker_block, fvg, liquidity_sweep, ma_cross, order_block

STRATEGIES = {
    "fvg": fvg,
    "liquidity_sweep": liquidity_sweep,
    "ma_cross": ma_cross,
    "order_block": order_block,
    "break_of_structure": break_of_structure,
    "breaker_block": breaker_block,
}


def resolve_params(name: str, overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Defaults for strategy ``name`` with ``overrides`` applied. Unknown names are errors."""
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy {name!r}; choose one of {', '.join(STRATEGIES)}")
    defaults = STRATEGIES[name].DEFAULTS
    unknown = sorted(set(overrides) - set(defaults))
    if unknown:
        raise ValueError(f"unknown parameter(s) for {name}: {', '.join(unknown)}")
    params = dict(defaults)
    params.update(overrides)
    for key, value in params.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"parameter {key} must be a positive number, got {value!r}")
        if isinstance(defaults[key], int) and not isinstance(value, int):
            raise ValueError(f"parameter {key} must be a whole number, got {value!r}")
    return params
