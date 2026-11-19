# Changelog

## 0.2.0 - unreleased

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
