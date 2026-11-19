"""The runner's state file: what it believes it has done.

One JSON file per (strategy, symbol, interval, params) run, written
atomically (temporary file, then rename) after every change. It never holds
credentials. On restart it is compared with what Bybit reports; if the two
disagree the runner stops instead of guessing (see ``executor.reconcile``).

Dry runs use their own file, so a dry run can never change what a real run
believes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

STATE_VERSION = 1


def empty_state(run_id: str, strategy: str, symbol: str, interval: str, dry_run: bool) -> Dict[str, Any]:
    return {
        "state_version": STATE_VERSION,
        "run_id": run_id,
        "strategy": strategy,
        "symbol": symbol,
        "interval": interval,
        "dry_run": dry_run,
        "last_bar_ms": None,      # open time of the last bar whose decision was handled
        "position": None,         # {"side": 1|-1, "qty": str, "stop": str, "take_profit": str|None, ...}
        "pending_order": None,    # {"order_link_id": ..., "kind": "entry"|"exit"} while unconfirmed
        "day": None,              # {"date": "YYYY-MM-DD", "start_equity": float}
        "consecutive_api_errors": 0,
        "halt": None,             # {"reason": ..., "at_ms": ...} once a breaker has tripped
        "orders": [],             # last 50 orders this run placed, newest last
    }


class StateFile:
    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def for_run(cls, state_dir: str, run_id: str, dry_run: bool) -> "StateFile":
        suffix = "-dryrun" if dry_run else ""
        return cls(Path(state_dir) / f"{run_id}{suffix}.json")

    def load(self) -> Optional[Dict[str, Any]]:
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"state file {self.path} is not valid JSON; inspect or delete it") from exc
        if not isinstance(data, dict) or data.get("state_version") != STATE_VERSION:
            raise ValueError(f"state file {self.path} has an unknown layout; inspect or delete it")
        return data

    def save(self, state: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state["orders"] = state.get("orders", [])[-50:]
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
