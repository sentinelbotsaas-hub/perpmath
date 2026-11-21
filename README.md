# perpmath

A small backtester for crypto perpetual-futures strategies, built to be honest
about costs and sample size. It downloads public candles from Bybit, runs a
strategy with fixed rules, and writes one JSON file with everything needed to
check the result.

It is the companion to [perpmath.com](https://perpmath.com): every backtest
shown there can be reproduced with the commands below.

**This project ships no profitable strategy and makes no return claims.** The
six example strategies all lose money in the shipped results, in-sample and
out-of-sample. The grid
example shows a small gain in its shipped run; the [grid section](#grid-backtest)
explains why one range-bound year is not evidence that it works. They are here
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

`backtest` spends most of its time in that check: it runs the strategy
several thousand times on shortened data, which takes seconds to tens of
seconds on a year of 15-minute bars. `--lookahead every-bar` asks for the
complete version (minutes). What the check does and does not establish is
under [How results are computed](#how-results-are-computed).

## Example strategies

| Name | What it does | Rules |
|---|---|---|
| `fvg` | Enters on the first return into a three-candle fair value gap, stop beyond the gap, target at 2R | [fvg.py](src/perpmath/strategies/fvg.py) |
| `liquidity_sweep` | Fades a bar that takes out the prior 20-bar high or low and closes back inside, target at 2R. A bar that takes out both gives no signal if it closes inside the old range; if it closes beyond one side it is traded as a sweep of the other side, in the direction of its close (rule 4) | [liquidity_sweep.py](src/perpmath/strategies/liquidity_sweep.py) |
| `ma_cross` | 20/50 simple moving-average cross with an ATR stop; the boring baseline | [ma_cross.py](src/perpmath/strategies/ma_cross.py) |
| `order_block` | Enters on the first return to the last opposite candle before a three-bar, 2 x ATR move that closed beyond the latest swing point; stop beyond the candle, target at 2R | [order_block.py](src/perpmath/strategies/order_block.py) |
| `break_of_structure` | Follows a close beyond the latest confirmed swing high or low (5 bars either side), stop beyond the opposite swing, target at 2R | [break_of_structure.py](src/perpmath/strategies/break_of_structure.py) |
| `breaker_block` | Takes an order block that price has closed through and enters on its first retest, in the direction of that close; target at 2R | [breaker_block.py](src/perpmath/strategies/breaker_block.py) |

The exact rules are in each file's docstring. Parameters live in
`configs/*.json`. They were fixed once, before the first run on real data, and
have not been adjusted since. Fair value gaps and liquidity sweeps have no
agreed standard parameters, so these are one reasonable choice, not the
definition. There is no optimizer in 0.1.

One `liquidity_sweep` rule does something its one-line summary does not
suggest. A bar that trades through both the prior high and the prior low and
then closes *outside* the old range (not back inside it) still gives a
signal: a close at or above the prior high is counted as a sweep of the low
and goes long, a close at or below the prior low is counted as a sweep of
the high and goes short. That follows from rules 2 and 3 as they are written
and is what the shipped results contain (10 of the 3,374 entry signals in
the shipped year). It is spelled out in rule 4 of the module and pinned by a
test; it has not been changed, because that would change published numbers.

Order blocks, breaks of structure and breaker blocks are looser still: they
are taught from chart examples, and two teachers will mark the same chart
differently. Each of those three modules therefore states the one definition
it tests and lists the judgement calls it had to fix (which candle is the
block, how big a move counts, wick or close, how many bars make a swing). A
result for `order_block` is a result for that definition, not for every
reading of the idea. Their parameters reuse the values the first three
strategies already had (ATR 14, 96 bars of waiting, 0.1 x ATR stop buffer,
2R) plus three new ones written down before the first run: swings of 5 bars
either side, an impulse of 3 bars, and a minimum move of 2 x ATR. No other
values were run.

A swing high or low is only known after the bars to its right have closed.
The strategies use it from that bar on and not before; this is tested
separately (see [Tests](#tests)).

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
| order_block | in-sample | 325 | 30.77% | -0.61 | -3.77 | -42.33% | 46.92% |
| order_block | out-of-sample | 141 | 24.11% | -0.98 | -2.49 | -18.71% | 24.94% |
| break_of_structure | in-sample | 149 | 31.54% | -0.17 | -1.57 | -43.34% | 53.72% |
| break_of_structure | out-of-sample | 42\* | 40.48% | 0.06 | -0.11 | -3.35% | 23.57% |
| breaker_block | in-sample | 243 | 33.74% | -0.56 | -1.56 | -18.44% | 21.23% |
| breaker_block | out-of-sample | 120 | 30.83% | -0.79 | -2.88 | -15.07% | 16.62% |

\* Fewer than 100 trades. The result file marks this block
`insufficient_sample`: treat every number in the row as noise, the positive
average R included. `break_of_structure` holds a position for about 150 to
230 bars on average because its stop sits at the opposite swing, and it
ignores new breaks while it is in one, so a year of 15-minute bars gives it
few trades. Its parameters were not changed to get more.

All six lose, in both periods. The full output, including every trade, is in
`results/`. The average trade loses between 0.05% and 0.35% of its notional.
In eleven of the twelve rows it is 0.18% or less, which is the same size as
the round-trip cost assumed here (0.11% in fees plus 0.04% in slippage). The
exception is `break_of_structure` in-sample at 0.35%, more than twice that
cost, so cost alone does not account for it. You can see how much of a loss
is cost by copying a config, setting `fee_rate` and `slippage` to 0 and
running it again.

Average R and total return can point in different directions, as in the
`break_of_structure` out-of-sample row (+0.06 R, -3.35%). R counts every
trade equally, whatever the distance to its stop; the return is compounded on
the full position, so trades with a far stop move it more.

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

**Look-ahead check.** Before simulating, the strategy is run once on the full
data and then again on copies of the data that stop early. A *signal bar* is
a bar on which the full run decided something: an entry, a stop, a target or
an exit flag. The copies stop

- right after every signal bar (all of them up to 4,000; above that a subset,
  half evenly spaced and half drawn with a fixed seed);
- right after every one of the first 4,000 bars, signal or not;
- right after 500 of the later bars that have no signal, chosen the same
  way, and at nine fixed points spread over the data.

Every copy starts at the first bar, so indicators see the history they would
have seen. The decisions of a copy must equal the full run's on every bar the
copy contains. The first difference stops the run with an error that names
the bar.

What a passed check establishes: on every signal bar that was checked, the
decision in the result (side, stop, target, exit flags) is the decision the
strategy makes when no later bar exists. A strategy that makes its decisions
one bar early (acting on bar `i` with what is only known on bar `i + 1`)
fails at its first signal bar: the bar it needs is not in the copy.

What it does not establish:

- **Signals that later bars removed.** Look-ahead can also be used to *drop*
  a decision: skip the entries the next bar goes against, or treat a swing
  point as known before it is confirmed (the tests do this to `order_block`,
  and every entry it still gives holds up). The signals that remain pass;
  the trace is on bars that have no signal. Those bars are all cut in the
  first 4,000 bars and sampled after that (about 500 of 30,000 on a year of
  15-minute bars), so on that year roughly 13% of them are cut. A look-ahead
  of this kind that changes many bars is found; one that changes a few can
  be missed. As a guide, if the bars it changes were spread evenly, one
  changing 20 bars would get through about 6 times in 100 and one changing
  50 about once in 1,000. `--lookahead every-bar` cuts after every bar and
  leaves no such gap, at one strategy run per bar.
- **Signal bars that were not checked.** Above 4,000 signal bars the default
  checks a subset. `--lookahead signals` checks all of them.
- **Data from outside.** The check controls the bars it hands to the
  strategy and nothing else. A strategy that takes later prices from a file,
  a global variable or a closure gives the same answers on every copy and
  passes.
- **Hindsight in the design.** Rules or parameters chosen after looking at
  the results are not something a shortened copy can show.
- **The signal on the last bar.** Nothing comes after it, so it is not
  checked. The engine never fills it either.

The check needs a strategy that gives the same output for the same input and
runs on any number of bars from one upwards; one that raises on a short copy
fails.

The result file records what was done (`timing.lookahead_check`): how many
signal bars and other bars there were, how many of each were cut, and the
seed if anything was sampled. On the shipped year (35,040 bars) every signal
bar of every example is checked, in about 4,900 to 7,500 runs per strategy.
Measured on one desktop PC:

| | Signal bars | default | `--lookahead every-bar` |
|---|---:|---:|---:|
| fvg | 1,592 | 30 s | about 7 min |
| liquidity_sweep | 3,374 | 8 s | about 1 min |
| ma_cross | 835 | 2 s | about 30 s |
| order_block | 530 | 21 s | about 7 min |
| break_of_structure | 1,631 | 18 s | about 4 min |
| breaker_block | 384 | 23 s | about 9 min |

All six pass both. Up to 0.2.1 the check cut the data at nine fixed points
only. That found look-ahead reaching far ahead; look-ahead of one or a few
bars was found or missed depending on where the cuts fell. Result files
written by those versions say `"shortened_runs": 9` and nothing more.

## What is not modelled

This list is for the strategy engine above. The grid simulator below does
model funding, maker fees, leverage and liquidation; its own limits are in
its section.

- **Funding payments.** Perpetuals pay or charge funding, usually every eight
  hours. Strategies that hold for long periods, `ma_cross` in particular, would
  show different results with funding included.
- Leverage, margin and liquidation. Notional is capped at 100% of equity.
- **Resting limit orders.** `fvg`, `order_block` and `breaker_block` are
  usually described with a limit order waiting at the edge of the zone. The
  engine only has market orders at the next open, so these three enter at the
  open after the bar that came back to the zone. The fill can be better or
  worse than the zone edge, and it pays the taker fee.
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
on shortened copies. Every fill and every equity value of a copy must equal
the full run's, the copy's last bar included; the copies are simulated
without the end-of-sample close so that this bar can be compared. The copies
stop after each of the first 500 bars, after 40 of the later bars on which
the full run has a fill (half evenly spaced, half drawn with a fixed seed)
and at nine fixed points: about 550 simulations on the shipped year, about
7 seconds on the same PC.

This is a spot check of the simulator on your data, not a proof. On the
shipped year it cuts after about 6% of the bars that have a fill. A
simulator that looked ahead on most bars would be stopped at once; one that
did so on a few bars could get through. The complete version, a cut after
every bar, runs in the test suite on short synthetic series for each
direction, range-exit mode and path.

Up to 0.2.1 the last bar of each copy was left out of the comparison,
because the position is closed there. That made the check blind to a
simulator looking exactly one bar ahead, at any number of cuts: the bar it
looked at was always still in the copy. The simulator had no such
look-ahead; the check could not have shown it. This is fixed and has a
regression test.

The check cannot show that the *bounds* were chosen without hindsight, which
is why `in_sample_range` is flagged.

### Grid limits

- The intrabar path is estimated (see above); fills on a strict cross at the
  level are an assumption, not order-book data. No queue position, partial
  fills or latency.
- Funding uses the bar close, not the mark price. Liquidation uses risk tier
  1 only and ignores the liquidation fee beyond losing the remaining margin.
- Exchange grid bots differ in details (how the start position is bought,
  quantity per grid, trailing ranges). This simulator follows the rules above,
  not any particular exchange's bot.

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

The look-ahead check calls `generate` thousands of times on the first `k`
bars of the data, for many values of `k` down to 1. It must return the same
output for the same input and must not raise when there is too little
history; give no signal there instead.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The tests cover look-ahead (including four strategies that cheat by one to
five bars: the old fixed-cut check lets two of them through on the test
series and the current check stops all four, and a test shows which of them
the cuts after signal bars cannot see on their own), fee and slippage
arithmetic, metrics on a hand-computed example, each strategy's rules on
hand-made bars (entry, no
entry, stop, target, expiry and failure of a zone), swing points not being
used before the bars to their right have closed (by hand, and by cutting a
synthetic series at every bar and comparing the decisions), the
downloader's pagination against a fake endpoint, the grid simulator (start
rules, fill prices, fees and funding by hand, both intrabar paths on a bar
where they disagree, the three range-exit modes, the liquidation threshold,
the look-ahead check and the funding downloader), and the testnet runner
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
