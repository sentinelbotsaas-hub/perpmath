# Changelog

## 0.2.2 (2026-11-22)

A stronger look-ahead check, and one `liquidity_sweep` rule written out. No
result number changes: the six strategy results and the grid result in
`results/` were re-run with the new check and are identical outside
`package_version`, `generated_at` and `timing.lookahead_check`.

### Look-ahead check

- **The old check let short look-ahead through.** It cut the data at nine
  fixed points. A strategy that looks one or a few bars ahead differs from
  the full run only on the last bars of a shortened copy, so whether it was
  caught depended on where the cuts fell. `ma_cross` with every decision
  moved one bar early passed on the shipped year with 9 cuts, failed with
  17, passed with 65 and failed with 129, and a passing run was written with
  `lookahead_check: passed`. The README sentence "if any decision on a bar
  changes when later bars are removed, the run stops" claimed more than the
  check did.
- New check, `check_lookahead_at_signals`, used by `run_backtest` and the
  CLI. The data is cut right after every bar on which the full run decided
  something (entry, stop, target or exit flag) and the decision there must
  be the same without the later bars. A strategy that decides one bar early
  now fails at its first signal bar on any data.
- Look-ahead that only *removes* signals cannot be seen from signal bars
  (the signals that remain all hold up). An `order_block` that uses swing
  points from the pivot bar on is of this kind: on the shipped year all 374
  of its signal bars pass. So the data is also cut after every one of the
  first 4,000 bars and after 500 sampled later bars without a signal (half
  evenly spaced, half drawn with a fixed seed), plus the old nine points.
  This part is complete for the first 4,000 bars and a sample after that;
  the README says what that covers.
- Defaults check every signal bar up to 4,000. On the shipped year that is
  100% of the signal bars of all six examples and 13% of the other bars, in
  2 to 30 seconds per strategy (the old check took under a second).
  `perpmath backtest --lookahead signals` never samples signal bars;
  `--lookahead every-bar` cuts after every bar (half a minute to about nine
  minutes per strategy on the shipped year). All six examples pass it there.
- `timing.lookahead_check` in the result file now records the method, the
  number of signal bars and other bars, how many of each were cut, and the
  seed when anything was sampled. Keys were added and none changed meaning,
  so `schema_version` stays 1 (see `docs/output-format.md`).
- A strategy that raises on a shortened copy now fails the check with a
  message instead of a traceback. `generate` must accept any number of bars
  from one upwards.
- `check_no_lookahead` (fixed cuts) is unchanged and still exported; its
  docstring now says what it misses. The testnet runner still calls it at
  start-up.

### Grid look-ahead check

- The grid check had a blind spot of its own: the last bar of each shortened
  copy was left out of the comparison, so a simulator looking exactly one
  bar ahead passed at any number of cuts, a cut after every bar included.
  The simulator has no such look-ahead; the check could not have shown it.
- `simulate_grid(..., close_at_end=False)` leaves the position open on the
  last bar. The check uses it to compare every bar of a copy. Results always
  close at the end, as before.
- Cuts: every one of the first 500 bars, 40 sampled later bars with a fill,
  and the nine fixed points (about 550 simulations and 7 seconds on the
  shipped year, up from 9 and about 1 second). `check_grid_no_lookahead` now
  returns the `lookahead_check` block instead of a count.

### `liquidity_sweep`

- Rule 4 said "a bar that sweeps both sides gives no signal". What the code
  does, and has always done: a bar that trades through both the prior high
  and the prior low gives no signal only if it closes inside the old range.
  If it closes at or above the prior high it is counted as a sweep of the
  low and goes long; at or below the prior low, short. On the shipped year
  15 bars trade through both sides and 10 of them give such a signal (10 of
  3,374). The behaviour is **not changed**; the docstring and the README now
  describe it and a test pins it.

### Tests

- Four strategies that cheat by one to five bars (`tests/peekers.py`), the
  old check's misses kept as regression tests, the new check stopping all
  four, and the limit of signal-bar cuts shown on two of them.
- The grid simulator at every cut of a short series for each direction,
  range-exit mode and path; a one-bar-ahead simulator that the old
  comparison passes and the new one stops.
- `liquidity_sweep` on hand-made bars that trade through both sides.

## 0.2.1 (2026-11-22)

Three more example strategies. Nothing else changes: the engine's timing and
cost rules, the grid simulator and the testnet runner are as in 0.2, and
mainnet trading is still refused.

- `order_block`, `break_of_structure`, `breaker_block`: three ICT/SMC ideas,
  each written as one checkable rule set. These ideas are taught from chart
  examples and have no agreed definition, so each module's docstring states
  the definition it uses and lists the judgement calls it fixes.
  - `order_block`: the bar immediately before a run of at least 3
    same-direction bars that covers 2 x ATR and closes beyond the latest
    confirmed swing point and beyond that bar. Entry on the first return to
    the bar's range, stop beyond it, target 2R.
  - `break_of_structure`: a close beyond the latest confirmed swing high or
    low (5 bars either side), each level used once. Stop beyond the latest
    opposite swing, target 2R. Breaks with and against the trend (BOS and
    CHoCH) are not told apart.
  - `breaker_block`: an order block (same detection code) that a later bar
    closes through; entry on the first retest, in the direction of that close.
- `indicators.confirmed_swing_high`, `confirmed_swing_low`, `carry_forward`.
  A swing is reported on the bar that completes its right-hand side, never on
  the pivot bar.
- Tests: every rule on hand-made bars; swing points are not used before they
  are confirmed (by hand, and by cutting a synthetic series at every bar);
  the look-ahead tests now run for all six strategies.
- `configs/` and `results/` for the three, on the same data file, period,
  split and costs as the existing results (BTCUSDT 15m, 2025-11-18 to
  2026-11-18). All three lose money in both periods.
  `break_of_structure` has 42 out-of-sample trades and is flagged
  `insufficient_sample`.
- Entries into a zone are market orders at the open after the bar that
  returned to it, as in `fvg`. The engine has no resting limit orders; this
  is now listed under "What is not modelled".

Default parameters for the three were written down on 2026-11-21 (UTC),
before their first run on real data. They were run once and not changed.
Existing result files are unchanged.

## 0.2.0 (2026-11-19)

Grid backtest and a testnet order runner.

### Grid backtest

- `perpmath grid`: a separate simulator for grid trading (`grid.py`), since a
  grid holds many positions at once. Neutral, long and short grids;
  arithmetic or geometric levels; leverage with isolated-margin liquidation
  (maintenance margin rule as on perpmath.com); `continue`, `stop` or `close`
  when price leaves the range.
- Intrabar path stated and tested: by default each bar is walked both
  low-first and high-first and the path with lower equity is kept. The
  result file reports the other paths, taker-fee grid fills and a run without
  funding next to the headline numbers.
- `perpmath funding`: downloader for Bybit public funding-rate history.
  Grid runs charge or credit funding at every settlement and fail if the
  funding file has gaps.
- Maker fee (0.02%) for grid fills, taker fee (0.055%) for market orders,
  both Bybit VIP 0 rates read 2026-11-19.
- `configs/grid.json` and `results/grid-btcusdt.json`: neutral, geometric,
  50 cells, leverage 1, bounds from the in-sample range, fixed once and not
  searched. The in-sample block uses hindsight bounds and is flagged as such.
- Grid result file documented in `docs/output-format.md`.

### Testnet order runner

Testnet order runner. **Bybit testnet only**; mainnet is refused (planned for
0.3, after a position guard).

- `perpmath live STRATEGY --symbol --interval --config [--dry-run] [--once]
  [--clear-halt]`: runs the same strategies with the backtest's timing
  (decide on close, act at the next open) and places market orders on the
  Bybit testnet, with the stop and target attached on Bybit's side.
- `perpmath.live`: standard-library Bybit V5 REST client (HMAC-SHA256
  signing, no new dependency, no CCXT), runner, circuit breakers, state file.
- Testnet URL is a constant; `"network": "mainnet"`, URL settings and
  `--mainnet` are refused.
- Keys only from `BYBIT_TESTNET_API_KEY` / `BYBIT_TESTNET_API_SECRET`; never
  logged (at most length and first/last 4 characters). `.env.example` added.
- Start-up key check via `/v5/user/query-api`: refuses keys with withdrawal
  or transfer permission, read-only keys, and keys without contract-order
  permission.
- Circuit breakers with defaults: daily loss (20 USDT), largest position
  (200 USDT notional), consecutive API errors (5), clock skew (5000 ms). No
  trading on bars with missing candles.
- Deterministic `orderLinkId`, lookup before sending and after timeouts,
  start-up reconciliation that stops on any mismatch, partial fills,
  rejections and rate limits handled.
- `--dry-run`: no key, no signed request; prints the orders it would send.
- `broker_id` setting, empty by default; no broker header is sent while empty.
- Optional public-endpoint smoke test against the testnet
  (`PERPMATH_TESTNET_SMOKE=1`).

Not included: mainnet trading, position guard, grid order mode, WebSocket.

## 0.1.0 (unreleased)

First public version. Backtest only.

- Bar-by-bar engine: decisions on bar close, fills at the next open, stop
  before target inside a bar, fee and slippage on every fill.
- Look-ahead check that runs before every backtest.
- Downloader for Bybit public USDT-perpetual candles with a local CSV cache.
- Three example strategies with fixed parameters: `fvg`, `liquidity_sweep`,
  `ma_cross`.
- In-sample / out-of-sample reporting, t-statistic with trade count,
  "insufficient sample" flag below 100 trades.
- JSON result document, schema version 1.
- Shipped results for BTCUSDT 15m, 2025-11-18 to 2026-11-18. All three
  strategies lose money after costs.

Not included: live trading, order placement, API keys, leverage, liquidation,
funding payments, parameter optimization.

Default parameters and cost assumptions were written down on 2026-11-18,
before the first run on real data, and were not changed afterwards.
