# perpmath

A small backtester for crypto perpetual-futures strategies, built to be honest
about costs and sample size. It downloads public candles from Bybit, runs a
strategy with fixed rules, and writes one JSON file with everything needed to
check the result.

It is the companion to [perpmath.com](https://perpmath.com): every backtest
shown there can be reproduced with the commands below.

**This project ships no profitable strategy and makes no return claims.** The
three example strategies all lose money in the shipped results. The grid
example shows a small gain in its shipped run; the [grid section](#grid-backtest)
explains why one range-bound year is not evidence that it works. They are here
to show how the accounting works, not to be traded.

## Risk statement

Trading perpetual futures can lose all the money you put in, quickly. A
backtest is a calculation on past prices under simplifying assumptions. It
does not predict future results, and a good backtest is not evidence that a
strategy will make money. Nothing in this repository is financial advice.

Version 0.1 is backtest only. It cannot place orders and it never asks for an
API key.

## Install

Python 3.10 or newer. The only dependency is NumPy.

```bash
git clone https://github.com/sentinelbotsaas-hub/perpmath.git
cd perpmath
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
```

## Use

Download one year of 15-minute candles (public endpoint, no account needed):

```bash
perpmath download BTCUSDT 15m --days 365
```

Run a strategy with a fixed config and write the result:

```bash
perpmath backtest fvg --symbol BTCUSDT --interval 15m --config configs/fvg.json --out results/fvg-btcusdt.json
```

`python -m perpmath ...` does the same as `perpmath ...`.

For a range that someone else can reproduce exactly, give dates instead of
`--days`:

```bash
perpmath download BTCUSDT 15m --start 2025-11-18T11:30:00Z --end 2026-11-18T11:30:00Z
```

Candles are cached in `data/` (ignored by git). Both commands exit with a
non-zero code if the data is empty or malformed, the config is invalid, or
the look-ahead check fails.

## Example strategies

| Name | What it does | Rules |
|---|---|---|
| `fvg` | Enters on the first return into a three-candle fair value gap, stop beyond the gap, target at 2R | [fvg.py](src/perpmath/strategies/fvg.py) |
| `liquidity_sweep` | Fades a bar that takes out the prior 20-bar high or low and closes back inside, target at 2R | [liquidity_sweep.py](src/perpmath/strategies/liquidity_sweep.py) |
| `ma_cross` | 20/50 simple moving-average cross with an ATR stop; the boring baseline | [ma_cross.py](src/perpmath/strategies/ma_cross.py) |

The exact rules are in each file's docstring. Parameters live in
`configs/*.json`. They were fixed once, before the first run on real data, and
have not been adjusted since. Fair value gaps and liquidity sweeps have no
agreed standard parameters, so these are one reasonable choice, not the
definition. There is no optimizer in 0.1.

## Shipped results

BTCUSDT, 15-minute bars, 2025-11-18 11:30 UTC to 2026-11-18 11:30 UTC
(35,040 bars, none missing). Fee 0.055% per side, slippage 0.02% per fill,
position size 100% of equity, no leverage. Funding is not included.
In-sample is the first 70% of the period (to 2026-07-31), out-of-sample the rest.

| Strategy | Sample | Trades | Win rate | Avg R | t-stat | Total return | Max drawdown |
|---|---|---:|---:|---:|---:|---:|---:|
| fvg | in-sample | 801 | 31.46% | -0.57 | -4.93 | -66.94% | 66.97% |
| fvg | out-of-sample | 304 | 27.96% | -0.83 | -5.23 | -42.86% | 43.19% |
| liquidity_sweep | in-sample | 1,639 | 29.16% | -0.67 | -10.91 | -93.98% | 94.35% |
| liquidity_sweep | out-of-sample | 688 | 28.20% | -0.79 | -8.48 | -69.03% | 69.31% |
| ma_cross | in-sample | 572 | 20.80% | -0.31 | -2.95 | -62.35% | 63.33% |
| ma_cross | out-of-sample | 263 | 19.01% | -0.05 | -0.68 | -18.46% | 31.18% |

All three lose. The full output, including every trade, is in `results/`.
The average trade loses between 0.07% and 0.18% of its notional, which is the
same size as the round-trip cost assumed here (0.11% in fees plus 0.04% in
slippage). You can see how much of the loss is cost by copying a config,
setting `fee_rate` and `slippage` to 0 and running it again.

## How results are computed

**Timing.** A strategy decides on the close of a bar. The order fills at the
open of the next bar. Stops and targets are checked against the high and low
of every bar from the entry bar onwards. If a bar opens beyond a stop or
target, the fill is that open. If a bar touches both the stop and the target,
the stop is assumed to have come first.

**Costs.** Every fill pays `fee_rate` on its notional (quantity x fill
price). Every fill, including take-profit fills, is also moved against you by
`slippage`. The default fee is 0.055% per side, meant to stand for Bybit's
base taker rate on USDT perpetuals. **Check Bybit's current fee table and set
`fee_rate` yourself**: rates depend on your account tier and change over
time, and this default is not kept in sync with Bybit. The default slippage,
0.02% per fill, is an assumption, not a measurement.

**Position size.** One position at a time. Each trade's notional is
`notional_fraction` (default 1.0) of current equity, so results compound.

**In-sample and out-of-sample.** The data is cut at a UTC midnight, either a
date you give (`"oos_start"`) or a fraction of the period
(`"in_sample_fraction"`). Each part is simulated on its own starting from the
same capital. Positions open at the end of a part are closed at its last
close. Because nothing is fitted in 0.1, both parts are really just two
consecutive periods; the split is there so that if you change parameters by
hand you have a period you did not look at.

**Metrics.** Reported separately for each part:

- `trades`, `win_rate_pct`: a win is a trade with positive profit after costs.
- `avg_r`: mean R multiple. R is a trade's net profit divided by what it
  would have lost, before costs, had it exited exactly at its stop.
- `t_stat`, `t_stat_n`: one-sample t-statistic of per-trade net returns (profit
  divided by entry notional) against zero, and the number of trades it is based
  on. Roughly, an absolute value below 2 means the average cannot be told apart
  from zero. Trades are treated as independent, which flatters the figure when
  they are not.
- `total_return_pct`: change in equity over the period.
- `max_drawdown_pct`: largest peak-to-trough fall in equity, measured at every
  bar close with open positions marked to market.
- `insufficient_sample`: `true` when there are fewer than 100 trades. Treat
  every other number in that block as noise.

Values that cannot be computed are `null`, never 0.

**Look-ahead check.** Before simulating, the strategy is run again on
shortened copies of the data. If any decision on a bar changes when later
bars are removed, the run stops with an error. This catches a strategy that
reads future bars from the data it is given. It cannot catch a strategy that
gets future prices from somewhere else, such as a file or a global variable.

## What is not modelled

This list is for the strategy engine above. The grid simulator below does
model funding, maker fees, leverage and liquidation; its own limits are in
its section.

- **Funding payments.** Perpetuals pay or charge funding, usually every eight
  hours. Strategies that hold for long periods, `ma_cross` in particular, would
  show different results with funding included.
- Leverage, margin and liquidation. Notional is capped at 100% of equity.
- Maker fees, partial fills, latency, order-book depth and market impact.
- The order of events inside a bar, beyond the stop-first rule above.

## Config file

```json
{
  "strategy": "fvg",
  "params": {"atr_period": 14, "min_gap_atr": 0.5, "max_wait_bars": 96, "stop_buffer_atr": 0.1, "r_multiple": 2.0},
  "execution": {"fee_rate": 0.00055, "slippage": 0.0002, "initial_capital": 10000, "notional_fraction": 1.0, "max_hold_bars": null},
  "split": {"in_sample_fraction": 0.7}
}
```

Rates are fractions, not percentages: 0.055% is `0.00055`. Unknown keys and
unknown parameters are errors, so a typo cannot silently fall back to a
default.

## Output file

One JSON document per run: the config used, data range and file hash, costs,
both metric blocks, daily equity points, every trade, the package version and
a timestamp. The layout is described in
[docs/output-format.md](docs/output-format.md) and versioned by
`schema_version`.

## Grid backtest

A grid bot keeps buy and sell limit orders at many price levels and holds a
small position for every level that has filled. That does not fit the
one-position strategy engine, so grids have their own simulator,
[grid.py](src/perpmath/grid.py). It uses the same candle files, split,
drawdown definition and JSON conventions.

```bash
perpmath download BTCUSDT 15m --start 2025-11-18T11:30:00Z --end 2026-11-18T11:30:00Z
perpmath funding BTCUSDT --start 2025-11-18T00:00:00Z --end 2026-11-18T12:00:00Z
perpmath grid --symbol BTCUSDT --interval 15m --config configs/grid.json --out results/grid-btcusdt.json
```

### Rules

- **Levels.** `grid_count` cells between `lower` and `upper`, so
  `grid_count + 1` levels. `arithmetic`: equal width in price. `geometric`:
  equal width in percent.
- **Cells.** A long cell buys at its lower level and sells at its upper
  level. A short cell sells at its upper level and buys back at its lower
  level. One position per cell. A position closed by its own grid order is
  one *round trip* (a completed grid pair).
- **Size.** Every cell opens `initial_capital x leverage / grid_count` USDT
  of notional, at the price where it opens. With leverage 1 a fully filled
  long grid holds about the starting capital.
- **Start.** The grid is placed at the open of the first bar of each sample,
  at price `P0`. `long`: all cells are long cells, and the ones whose
  midpoint is above `P0` are bought at market straight away (so the sell
  orders above the price have something to sell). `short`: the mirror image,
  cells below `P0` are sold short at market. `neutral`: cells below `P0` are
  long cells, cells above are short cells, nothing is traded at the start.
- **Fills.** A grid order fills only if price trades *strictly through* its
  level, and it fills *at the level*, never at a better price, even after a
  gap. Slippage is then applied against you on every fill, limit fills
  included.
- **Leaving the range** (`out_of_range`): `continue` keeps all orders and
  positions (what exchange grid bots do unless you set a stop); `stop`
  cancels the orders and keeps the position to the end of the sample;
  `close` cancels the orders and closes everything with a market order at the
  bound (at the open if price gapped past it). `stop` and `close` only fire
  after price has been inside the range once.
- **Leverage and liquidation.** Isolated margin: the grid's equity is the
  margin of its net position. It is liquidated when equity falls to
  `maintenance_margin_rate x |net quantity| x price`, the same simplified
  rule as the [liquidation price calculator](https://perpmath.com/tools/liquidation-price-calculator/)
  (tier 1, no deduction, closing fee left out). The default 0.33% is Bybit's
  BTCUSDT tier-1 rate from the public risk-limit endpoint (snapshot
  2026-11-18), valid for positions up to 300,000 USDT. On liquidation every
  position is closed and the rest of the equity is counted as lost.
- **End.** Anything still open at a sample's last bar is closed at its close
  with a market order. Each sample starts flat with the same capital.

### Intrabar path: the main assumption

A 15-minute bar does not say whether its high or its low came first, and for
a grid it matters: an order bought on the way down can be sold on the way
back up inside the same bar. The simulator walks each bar as straight
segments, previous close to open, then either open-low-high-close
(`low_first`) or open-high-low-close (`high_first`), filling orders in the
order price meets them. The default, `worst_of_two`, runs both for every bar
and keeps the one that leaves lower equity at the close. `best_of_two` keeps
the other; it exists only to show the width of the uncertainty.

Neither is a hard bound. Real prices swing many times inside a bar, which
would fill more orders than any of these paths, and the choice is made bar by
bar, not over the whole run. To see how far off 15-minute bars are, the same
config was run on one year of public 1-minute bars
(`perpmath download BTCUSDT 1m ...`, then `perpmath grid --interval 1m ...`):

| Path | In-sample 15m | Out-of-sample 15m | In-sample 1m | Out-of-sample 1m |
|---|---:|---:|---:|---:|
| `worst_of_two` (default) | +3.17% | +4.37% | +3.30% | +4.37% |
| `low_first` | +4.70% | +4.78% | +3.36% | +4.49% |
| `high_first` | +4.60% | +4.76% | +3.42% | +4.41% |
| `best_of_two` | +6.09% | +5.15% | +3.48% | +4.52% |

Total return; round trips in-sample go from 669 (worst) to 818 (best) on
15-minute bars. On 1-minute bars the four paths agree within 0.2 points, and
the 15-minute `worst_of_two` figure sits at or just below them. The other
15-minute paths overstate the result by up to 2.8 points in-sample compared
with the 1-minute runs. This is
why the default is the pessimistic path. The 1-minute run is a check, not a
shipped result; the file is too large for the repository.

### Costs and funding

- Grid orders are resting limit orders and pay `grid_fee_rate`, default
  0.02%: Bybit's VIP 0 maker rate for perpetual and futures contracts
  ([fee table](https://www.bybit.com/en/help-center/article/Trading-Fee-Structure),
  read 2026-11-19). Market orders (start position, range close, end close)
  pay `taker_fee_rate`, 0.055%, the VIP 0 taker rate. As with the strategy
  engine, check your own rate. The result also shows the run with every grid
  fill charged at the taker rate.
- Slippage 0.02% per fill. A round trip therefore costs 0.08% of the cell's
  notional; the narrowest cell must be wider than that or every pair loses
  money (the result warns when it is not).
- **Funding is included.** `perpmath funding` downloads settled rates from
  Bybit's public `/v5/market/funding/history` endpoint into
  `data/SYMBOL-funding.csv`. At every settlement (every 8 hours for BTCUSDT)
  the net position pays `net quantity x price x rate`, where price is the
  close of the bar ending at the settlement time (Bybit uses the mark price;
  the difference is ignored). The run stops with an error if the funding file
  has a gap longer than 8 hours inside the bar range. Setting
  `"funding": false` is allowed, but the result then says
  `"funding_modelled": false` and the disclaimer says so.

### Shipped result

BTCUSDT, 15-minute bars, same period and split as the strategies above. Config
[configs/grid.json](configs/grid.json): neutral, geometric, 50 cells,
leverage 1, `continue` outside the range, bounds = the lowest low and highest
high of the in-sample period (57,755.8 to 97,963.2). These are ordinary
defaults chosen once, not searched: neutral is the plain range grid, geometric
spacing keeps every cell the same percentage wide over a wide range, and 50
cells make each cell 1.06% wide, about 13 times the 0.08% round-trip cost.
No other grid settings were run to pick these.

| Sample | Round trips | Fills | Total return | Max drawdown | Fees | Funding paid | Buy and hold | Buy and hold, with funding |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| in-sample | 669 | 1,371 | +3.17% | 12.52% | 58.04 | 38.09 | -29.30% | -30.39% |
| out-of-sample | 197 | 396 | +4.37% | 7.31% | 15.85 | -53.98 | -1.12% | -2.96% |

Money in USDT on 10,000 starting capital. Negative funding paid means the
grid received funding. Buy and hold is a 1x long perpetual bought at the first
open and sold at the last close with market orders.

How to read it:

- **The in-sample row uses hindsight.** Its bounds are that period's own
  extremes, so price never left the grid. It is optimistic by construction;
  the result file says so in `warnings`.
- **The out-of-sample row is one lucky shape.** Price stayed inside the
  in-sample range for the whole period (`bars_outside_range_pct` is 0) and
  ended close to where it started (64,751 to 64,124), so almost no inventory
  was left at a loss. A neutral grid earns small amounts while price ranges
  and carries a growing position against a trend: in-sample it ended holding
  0.084 BTC bought on the way down, closed for -954 USDT, against +1,367
  USDT (before fees) from 669 round trips. A period that trends out of the range would
  look very different, and the range-exit rules were not exercised here.
- Each round trip earns about 2 USDT on a 200 USDT cell. Charging grid fills
  at the taker rate instead of maker cuts the return to +2.22% in-sample and
  +4.09% out-of-sample. Leaving funding out changes it to +3.55% and +3.83%.
- One period, one market, one config. There is no t-statistic because round
  trips of a grid are not independent bets. Treat the numbers as an
  accounting example.

### Grid config file

```json
{
  "strategy": "grid",
  "params": {"bounds": "in_sample_range", "grid_count": 50, "spacing": "geometric",
             "direction": "neutral", "leverage": 1, "out_of_range": "continue"},
  "execution": {"grid_fee_rate": 0.0002, "taker_fee_rate": 0.00055, "slippage": 0.0002,
                "initial_capital": 10000, "maintenance_margin_rate": 0.0033,
                "intrabar_path": "worst_of_two", "funding": true},
  "split": {"in_sample_fraction": 0.7}
}
```

`bounds` is `"in_sample_range"` or `{"lower": price, "upper": price}` (fixed
prices carry no hindsight in either sample). `direction`: `neutral`, `long`,
`short`. `spacing`: `arithmetic`, `geometric`. `out_of_range`: `continue`,
`stop`, `close`. `intrabar_path`: `worst_of_two`, `low_first`, `high_first`,
`best_of_two`. Leverage 1 to 100, and `1 / leverage` must be above the
maintenance margin rate. Unknown keys are errors.

### Look-ahead check for grids

Before the results are computed, the grid is simulated on the full data and
on shortened copies, as for strategies; every fill and equity value before the
cut must be identical. This shows the simulator does not read later bars. It
cannot show that the *bounds* were chosen without hindsight, which is why
`in_sample_range` is flagged.

### Grid limits

- The intrabar path is estimated (see above); fills on a strict cross at the
  level are an assumption, not order-book data. No queue position, partial
  fills or latency.
- Funding uses the bar close, not the mark price. Liquidation uses risk tier
  1 only and ignores the liquidation fee beyond losing the remaining margin.
- Exchange grid bots differ in details (how the start position is bought,
  quantity per grid, trailing ranges). This simulator follows the rules above,
  not any particular exchange's bot.

## Writing a strategy

A strategy is a module with a `DEFAULTS` dict and a function
`generate(bars, params)` that returns a `Signals` object: for each bar, an
entry side, a stop price, an optional target in R, and optional exit flags.
Use only that bar and earlier bars. Add the module to `STRATEGIES` in
`src/perpmath/strategies/__init__.py`.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The tests cover look-ahead, fee and slippage arithmetic, metrics on a
hand-computed example, each strategy's rules on hand-made bars, and the
downloader's pagination against a fake endpoint. For grids they cover the
start rules, fill prices, fees and funding by hand, both intrabar paths on a
bar where they disagree, the three range-exit modes, the liquidation
threshold, the look-ahead check and the funding downloader. They do not use
the network.

## Origin and license

The engine is derived from the backtester of a discontinued private trading
platform and was reduced and rewritten for public use. MIT license; see
[LICENSE](LICENSE).

perpmath is an independent project. It uses Bybit's public market-data
endpoint and is not endorsed by Bybit.
