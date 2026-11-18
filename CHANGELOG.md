# Changelog

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
