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
strategy will make money. **Nothing here promises or implies a profit.**
Nothing in this repository is financial advice.

**Version 0.2 places orders on the Bybit testnet only**, with play money. It
refuses to start against mainnet; live trading is planned for 0.3, after a
position guard exists. The backtester still needs no account and no key.

**Broker ID disclosure.** The order runner has a `broker_id` setting for
Bybit's API Broker programme, which lets the project receive a share of the
trading fees of orders sent with it. It is **empty by default, and while it is
empty no broker header is sent**. It will stay empty until Bybit has approved
the project, and you can always set it to `""` yourself. See
[Broker ID](#broker-id).

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

## Testnet runner (0.2)

`perpmath live` runs one of the strategies on the **Bybit testnet**, with the
same `generate(bars, params)` and the same timing as the backtest: it decides
on a bar's close from closed bars only and acts straight away, which is the
next bar's open. The stop and target are attached to the entry order on
Bybit's side, so they keep working while perpmath is not running.

It is a learning tool for seeing how a strategy behaves with real order
handling. The example strategies lose money in backtests; running them on the
testnet will not change that.

### Dry run first (no account needed)

```bash
perpmath live ma_cross --symbol BTCUSDT --interval 15m --config configs/live/ma_cross.json --dry-run --once
```

`--dry-run` sends no signed request and needs no key. It reads public testnet
candles, runs the strategy and prints the exact order body it would send,
keeping a paper position (with the backtest's stop and target rules) in its
own state file. Drop `--once` to keep it running bar after bar.

### Getting a testnet key

1. Create a testnet account at [testnet.bybit.com](https://testnet.bybit.com)
   (separate from bybit.com) and request test funds from its assets page, as
   described in Bybit's [API FAQ](https://bybit-exchange.github.io/docs/faq).
2. Create an API key there with **Contract: Orders and Positions** only. Do
   not tick withdrawal or transfer; perpmath refuses to start with a key that
   has them, and with a read-only key. Bind it to your IP if you can.
3. Use one-way position mode for the symbol (the default).
4. Put the key in the environment, never in a file in the repo:

```bash
cp .env.example .env            # .env is ignored by git
# fill in BYBIT_TESTNET_API_KEY and BYBIT_TESTNET_API_SECRET, then
set -a; . ./.env; set +a
perpmath live ma_cross --symbol BTCUSDT --interval 15m --config configs/live/ma_cross.json
```

perpmath reads only `BYBIT_TESTNET_API_KEY` and `BYBIT_TESTNET_API_SECRET`. It
never prints them; at most it shows their length and first and last four
characters. Before the first order it asks you to type `TESTNET`.

### What it checks before trading

- The endpoint is the testnet, fixed in the code. A config with
  `"network": "mainnet"`, a URL setting, or `--mainnet` is refused.
- The key, via `/v5/user/query-api`: refused if it can withdraw or transfer,
  is read-only, or lacks contract-order permission.
- The clock: Bybit's time versus yours (request timestamps use Bybit's).
- Reconciliation: positions and open orders on Bybit against the runner's
  state file (`.perpmath-live/`). If they disagree in any way (a position it
  did not open, a position it expected but Bybit no longer has, a different
  size, an unknown open order, no stop-loss on the position, hedge mode) it
  stops and says what it found. It does not guess.

### Circuit breakers

All are in the `live` section of the config. When one trips, the runner
stops, writes the reason into its state file and exits with code 3. It stays
stopped across restarts until you run it again with `--clear-halt`, which
re-checks the condition. Tripping does not close the position: the stop-loss
on Bybit stays in place.

| Setting | Default | Trips when |
|---|---:|---|
| `max_daily_loss_usdt` | 20 | USDT equity has fallen this much since the runner's first reading of the UTC day |
| `max_position_notional_usdt` | 200 | an order (quantity x last close), or the position value on Bybit, is larger |
| `max_consecutive_api_errors` | 5 | this many API errors in a row, with no fully clean bar in between |
| `max_clock_skew_ms` | 5000 | your clock and Bybit's differ by more |

It also does not trade on a bar when candles are missing in the history it
was given, and it does not act on a decision more than
`max_signal_delay_seconds` (default 60) after the bar closed.

Other settings: `order_notional_usdt` (100, size of each entry),
`history_bars` (500), `settle_seconds` (3), `order_timeout_seconds` (20),
`recv_window_ms` (5000), `state_dir` (`.perpmath-live`), `broker_id` (`""`).

### Orders

- Market entries with `stopLoss` and, if the strategy has one, `takeProfit`,
  for the full position, triggered by last price. Exits are reduce-only
  market orders. One position at a time, one-way mode.
- Every order has a deterministic `orderLinkId`
  (`pm-<strategy>-<settings hash>-<bar time>-<side><action><n>`). The runner
  looks it up before sending, so a restart cannot send the same order twice,
  and Bybit refuses a duplicate ID anyway.
- A request that times out is looked up by its ID before any retry.
- Partial fills: the filled part is kept and protected by the stop, the rest is
  not chased. A partly filled exit is followed by another reduce-only order.
- Rejections are logged and counted as errors. Rate limits are waited out
  using Bybit's reset time.

### Known limitations

- Testnet only. Testnet prices and liquidity are not mainnet's, so fills and
  results there say little about mainnet.
- Sizing is a fixed `order_notional_usdt`, not a fraction of equity as in the
  backtest. Leverage is whatever your testnet account has set; perpmath does
  not change it.
- The target is computed from the signal bar's close, because the order is
  sent before its fill price is known. The backtest uses the fill price.
- Daily loss is measured from the first equity reading of the UTC day the
  runner made, and deposits or transfers move it too.
- Order status is polled over REST, not streamed over WebSocket.
- `max_hold_bars` from the backtest config is not applied.
- Only USDT perpetuals, unified trading account, one-way mode.
- If the runner is stopped and the stop or target fills meanwhile, the next
  start stops with a reconciliation report; delete the state file it names to
  start flat after you have checked.
- The signed endpoints were tested against a fake server built from Bybit's
  documentation, not against a real testnet account.

### Broker ID

`broker_id` is for Bybit's API Broker programme: orders sent with a broker
attribution header (`Referer`, see Bybit's
[API guide](https://bybit-exchange.github.io/docs/v5/guide)) let a registered
broker receive part of the trading fees. perpmath sends that header
**only when `broker_id` is not empty**. The default is `""`, and no ID will be
set by default before Bybit has approved the project. You can leave it empty
or clear it at any time.

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
hand-computed example, each strategy's rules on hand-made bars, the
downloader's pagination against a fake endpoint, and the testnet runner
(signing, key checks, breakers, order IDs, reconciliation, dry run) against a
fake Bybit. They do not use the network.

An optional smoke test calls the testnet's public endpoints (no key, no
order):

```bash
PERPMATH_TESTNET_SMOKE=1 pytest tests/test_live_testnet_smoke.py -v
```

## Origin and license

The engine is derived from the backtester of a discontinued private trading
platform and was reduced and rewritten for public use. MIT license; see
[LICENSE](LICENSE).

perpmath is an independent project. It uses Bybit's public market-data
endpoint and is not endorsed by Bybit.
