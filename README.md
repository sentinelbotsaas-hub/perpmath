# perpmath

A small backtester for crypto perpetual-futures strategies, built to be honest
about costs and sample size. It downloads public candles from Bybit, runs a
strategy with fixed rules, and writes one JSON file with everything needed to
check the result.

It is the companion to [perpmath.com](https://perpmath.com): every backtest
shown there can be reproduced with the commands below.

**This project ships no profitable strategy and makes no return claims.** The
three example strategies all lose money in the shipped results. They are here
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
downloader's pagination against a fake endpoint. They do not use the network.

## Origin and license

The engine is derived from the backtester of a discontinued private trading
platform and was reduced and rewritten for public use. MIT license; see
[LICENSE](LICENSE).

perpmath is an independent project. It uses Bybit's public market-data
endpoint and is not endorsed by Bybit.
