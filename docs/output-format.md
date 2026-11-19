# Result file format (schema version 1)

`perpmath backtest ... --out FILE` writes one UTF-8 JSON document. Keys are
always present and always in the order shown. A value that cannot be computed
is `null`. All times are UTC in the form `2026-01-31T00:00:00Z`. Running the
same command on the same data file gives the same document except for
`generated_at`.

If a key is renamed, removed or changes meaning, `schema_version` goes up.
New keys may be added without a version change.

## Top level

| Key | Type | Meaning |
|---|---|---|
| `schema_version` | integer | `1` |
| `package` | string | `"perpmath"` |
| `package_version` | string | Version that produced the file |
| `generated_at` | string | When the file was written |
| `strategy` | string | Strategy name |
| `symbol` | string | For example `"BTCUSDT"` |
| `interval` | string | For example `"15m"` |
| `config` | object | The config actually used, defaults filled in: `strategy`, `params`, `execution`, `split` |
| `data` | object | See below |
| `costs` | object | `fee_rate_per_side` and `slippage_per_fill` as fractions (0.00055 = 0.055%); `funding_modelled` is always `false` in 0.1 |
| `timing` | object | `signal`, `fill`, `same_bar_stop_and_target` describe the engine's rules; `lookahead_check` is `{"passed": true, "shortened_runs": N}` |
| `split` | object | `oos_start`: open time of the first out-of-sample bar |
| `in_sample` | object | Sample block, see below |
| `out_of_sample` | object | Sample block, see below |
| `warnings` | array of strings | For example missing bars or an insufficient sample |
| `disclaimer` | string | Fixed text |

## `data`

| Key | Meaning |
|---|---|
| `exchange`, `category`, `source` | `"bybit"`, `"linear"` and the public endpoint URL |
| `first_bar`, `last_bar` | Open times of the first and last bar used |
| `bars` | Number of bars used |
| `missing_bars` | Bars absent between the first and last bar |
| `file`, `file_sha256` | Name and SHA-256 of the cached CSV, to confirm two runs used the same data |

## Sample block (`in_sample`, `out_of_sample`)

| Key | Meaning |
|---|---|
| `first_bar`, `last_bar` | Open times of the first and last bar of the sample |
| `bars` | Number of bars |
| `metrics` | See below |
| `signals` | `seen`: entry signals; `skipped_in_position`: ignored because a position was open; `skipped_invalid_stop`: the next bar opened beyond the stop; `skipped_no_equity`: no equity left |
| `equity_curve` | Array of `{"t", "equity"}`. First point: the sample's first bar open with the starting capital. Then one point at the last bar close of each UTC day, open positions marked to market. The last point is the final equity. |
| `trades` | Array of trade records, in entry order |

### `metrics`

| Key | Meaning |
|---|---|
| `trades` | Number of closed trades |
| `insufficient_sample` | `true` when `trades` < 100 |
| `wins`, `losses` | Trades with net profit above / below zero |
| `win_rate_pct` | `wins / trades x 100` |
| `avg_r` | Mean of `r_multiple` |
| `avg_trade_return_pct` | Mean of `return_pct` |
| `t_stat` | Mean of `return_pct` divided by (its sample standard deviation / square root of `trades`); `null` with fewer than 2 trades |
| `t_stat_n` | Number of trades behind `t_stat` |
| `total_return_pct` | `(final_equity / initial_capital - 1) x 100` |
| `max_drawdown_pct` | Largest fall from a previous equity peak, as a percentage of that peak, measured at every bar close (not only at the points in `equity_curve`) |
| `profit_factor` | Sum of winning profits / sum of losses; `null` when there are no losses |
| `total_fees` | Fees paid, in account currency |
| `avg_bars_held` | Mean bars between entry and exit |
| `final_equity` | Equity at the end of the sample |
| `exit_reasons` | Count per reason: `stop`, `target`, `signal`, `time`, `end` |

### Trade record

| Key | Meaning |
|---|---|
| `n` | 1-based number within the sample |
| `side` | `"long"` or `"short"` |
| `entry_time` | Open time of the bar the entry filled in (the fill is at that open) |
| `entry_price` | Fill price, slippage included |
| `exit_time` | Open time of the bar the exit happened in |
| `exit_price` | Fill price, slippage included |
| `stop_price` | Stop level |
| `target_price` | Target level, or `null` |
| `quantity` | Position size in the base asset |
| `fees` | Entry fee plus exit fee |
| `pnl` | Profit after fees |
| `return_pct` | `pnl` / entry notional x 100, where entry notional = `quantity` x `entry_price` |
| `r_multiple` | `pnl` / (`quantity` x \|`entry_price` - `stop_price`\|) |
| `exit_reason` | `stop`, `target`, `signal` (strategy exit), `time` (`max_hold_bars`), `end` (sample ended) |
| `bars_held` | Exit bar index minus entry bar index |

Numbers are rounded for output: prices to 6 decimals, quantity to 8, money to
2 or 4, percentages, R and t to 4.

# Grid result file (schema version 1)

`perpmath grid ... --out FILE` writes a document with `"strategy": "grid"`.
It follows the rules above (key order fixed, `null` for undefined values, UTC
times, identical on a re-run except `generated_at`) but has its own layout.

## Top level

| Key | Type | Meaning |
|---|---|---|
| `schema_version`, `package`, `package_version`, `generated_at` | | As above |
| `strategy` | string | `"grid"` |
| `symbol`, `interval` | string | As above |
| `config` | object | The config used, defaults filled in: `strategy`, `params`, `execution`, `split` |
| `data` | object | As above, plus `funding`: `source`, `records_in_range`, `first`, `last`, `file`, `file_sha256` of the funding CSV, or `null` when funding is off |
| `costs` | object | `grid_fee_rate` (grid limit fills), `taker_fee_rate` (market orders), `fee_source`, `slippage_per_fill`, `round_trip_cost_pct` (2 x (grid fee + slippage), in percent), `funding_modelled`, `maintenance_margin_rate`, `maintenance_margin_source` |
| `timing` | object | `grid_start`, `grid_orders` (fill rule), `intrabar_path`, `lookahead_check` `{"passed": true, "shortened_runs": N}` |
| `assumptions` | array of strings | Every modelling assumption of this run, in words |
| `grid` | object | See below |
| `split` | object | `oos_start` |
| `in_sample`, `out_of_sample` | object | Grid sample block, see below |
| `comparisons` | object | The same run with one thing changed, see below |
| `warnings` | array of strings | Hindsight bounds, missing bars, liquidation, start outside the range, cells narrower than the round-trip cost, fewer than 100 round trips |
| `disclaimer` | string | Fixed text; says so when funding is not modelled |

## `grid`

| Key | Meaning |
|---|---|
| `lower`, `upper` | Grid bounds actually used |
| `bounds_source` | `"in-sample lowest low and highest high"` or `"fixed in config"` |
| `grid_count`, `spacing`, `direction`, `leverage`, `out_of_range` | As configured |
| `cell_notional` | USDT notional each cell opens: `initial_capital x leverage / grid_count` |
| `cell_width_pct_min`, `cell_width_pct_max` | Narrowest and widest cell, percent of its lower level |
| `levels` | All `grid_count + 1` level prices, low to high |

## Grid sample block

| Key | Meaning |
|---|---|
| `first_bar`, `last_bar`, `bars` | As above |
| `start_price` | Open of the first bar, where the grid was placed |
| `metrics` | See below |
| `buy_and_hold` | `return_pct` and `max_drawdown_pct` of a 1x long perpetual bought at the first open and sold at the last close with market orders; `funding_paid` and `return_with_funding_pct` include the funding it would have paid (`null` when funding is off) |
| `excess_return_vs_buy_and_hold_pct` | Grid `total_return_pct` minus buy and hold (with funding when funding is on), in percentage points |
| `equity_curve` | As above |
| `fills` | Every fill, in time order, see below |

### Grid `metrics`

| Key | Meaning |
|---|---|
| `total_return_pct`, `max_drawdown_pct`, `final_equity` | As for strategies |
| `fills` | All fills: `grid_fills` (limit orders at levels) + `market_fills` (start, range close, end close, liquidation) |
| `round_trips` | Cell positions closed by their own grid order (completed grid pairs) |
| `insufficient_sample` | `true` when `round_trips` < 100 |
| `trading_pnl` | Price profit of all closed positions, slippage included, before fees and funding |
| `total_fees` | `grid_fees` + `market_fees` |
| `funding_paid` | Net funding paid; negative = received. `null` when funding is off |
| `funding_events` | Settlements at which a position was held; `null` when funding is off |
| `liquidated`, `liquidation_time` | Whether and when (bar open time) the grid was liquidated |
| `liquidation_loss` | Equity lost at liquidation (0 when not liquidated) |
| `range_exit_time` | When `stop` or `close` fired; `null` otherwise |
| `bars_outside_range_pct` | Share of bar closes outside `[lower, upper]` |
| `max_position_notional`, `avg_position_notional` | Absolute net position x close, over bar closes |

`final_equity - initial_capital = trading_pnl - total_fees - funding_paid - liquidation_loss`,
up to rounding.

### Fill record

| Key | Meaning |
|---|---|
| `n` | 1-based number within the sample |
| `time` | Open time of the bar the fill happened in |
| `kind` | `grid`, `initial` (start position), `range_close`, `end`, `liquidation` |
| `side` | `"buy"` or `"sell"` |
| `cell` | Cell index (0 = lowest) for grid fills, otherwise `null` |
| `level` | Level price for grid fills, otherwise `null` |
| `price` | Fill price, slippage included |
| `quantity` | Base-asset quantity |
| `fee` | Fee paid on this fill |
| `pnl` | Profit before fees of the position this fill closed; `null` when it opened one |

## `comparisons`

One key per variant, each with `in_sample` and `out_of_sample` holding
`total_return_pct`, `max_drawdown_pct`, `round_trips`, `grid_fills`,
`total_fees`, `funding_paid`, `liquidated`. Variants: every
`intrabar_path=...` other than the one used, `grid_fills_at_taker_fee`
(present when the grid fee differs from the taker fee) and `without_funding`
(present when funding is on).
