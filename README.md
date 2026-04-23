# Polymarket BTC 5m Trading Bot

This repository runs the BTC 5-minute paper-trading workflow from a single command. Operators should use the one supported flow below; legacy research modules that still exist in the repository are outside the supported runtime path.

Run the commands below from the repository root, for example `D:\pythonProject\BTC_5MIN`.

## 1. Install dependencies

```powershell
cd D:\pythonProject\BTC_5MIN
python -m pip install -r requirements.txt
```

## 2. Configure parameters

`python main.py` uses `.env.dashboard` as the primary operator config file. You can copy `.env.dashboard.example` to create a commented starter template. For overlapping keys, values in `.env.dashboard` win. If a key is missing there, the runtime can still read it from temporary environment variables for that launch; anything still missing falls back to the defaults in `config.py`. Environment-variable values do not get written back to `.env.dashboard`.

Dashboard saves write back to `.env.dashboard`, and mode changes also update the running `RuntimeManager` target mode. Common fields include:

- `STRATEGY_ID`
- `TARGET_PROFIT`
- `MAX_STAKE`
- `MAX_CONSECUTIVE_LOSSES`
- `SIGNAL_MOMENTUM_THRESHOLD`
- `SIGNAL_WEAK_SIGNAL_MODE`
- `TRADE_MODE`
- `LIVE_TRADING_ENABLED`
- `POLYMARKET_API_KEY`
- `POLYMARKET_API_SECRET`
- `POLYMARKET_API_PASSPHRASE`
- `POLYMARKET_BUILDER_API_KEY`
- `POLYMARKET_BUILDER_SECRET`
- `POLYMARKET_BUILDER_PASSPHRASE`
- `POLYMARKET_RELAYER_API_KEY`
- `POLYMARKET_RELAYER_API_KEY_ADDRESS`

Trading mode safety rules:

- `TRADE_MODE=paper` keeps the runtime in paper trading.
- Real trading requires both `TRADE_MODE=live` and `LIVE_TRADING_ENABLED=true`.
- Saving a new mode updates the desired target mode immediately, but the runtime only switches after the current round reaches a safe boundary.
- `paper -> live` still requires confirmation in the dashboard before the save completes.
- If live credentials are incomplete or a live order is still unsettled, the runtime stays pending or blocked instead of switching unsafely.
- The dashboard shows the saved mode, current running mode, desired target mode, switch state, and live readiness so you can tell whether the switch is complete.

Credential split:

- `POLYMARKET_API_*` is for CLOB live trading only.
- `POLYMARKET_BUILDER_*` and `POLYMARKET_RELAYER_*` are for official gasless live redeem only.
- Direct Polygon `web3` redeem is not a supported runtime path.

### Paper multi-timeframe profiles

Paper mode can now enable more than one timeframe at once with:

- `PAPER_TIMEFRAMES=5m,15m`

Each timeframe can keep its own paper strategy list and thresholds. Example fields:

- `PAPER_5M_STRATEGY_ID=5`
- `PAPER_5M_STRATEGY_IDS=5,6`
- `PAPER_5M_TARGET_PROFIT=0.8`
- `PAPER_15M_STRATEGY_ID=2`
- `PAPER_15M_STRATEGY_IDS=1,2,7`
- `PAPER_15M_TARGET_PROFIT=1.0`

Paper runtime files are isolated by timeframe under `logs/paper/<timeframe>/`.

Live mode remains single-timeframe and continues to use `MARKET_TIMEFRAME`.

### Live multi-strategy profiles

Live mode can run more than one strategy in the same session with:

- `LIVE_STRATEGY_IDS=5,7`

When `LIVE_STRATEGY_IDS` is empty, live mode falls back to `STRATEGY_ID` and behaves like the original single-strategy setup.

Each live strategy can keep its own parameter overrides with `LIVE_STRATEGY_<id>_<FIELD>` keys. Example fields:

- `LIVE_STRATEGY_5_BASE_ORDER_COST=1.5`
- `LIVE_STRATEGY_5_TARGET_PROFIT=0.8`
- `LIVE_STRATEGY_7_BASE_ORDER_COST=2.0`
- `LIVE_STRATEGY_7_STRATEGY7_MAX_ENTRY_PRICE=0.53`

Live multi-strategy mode still uses one `MARKET_TIMEFRAME`, one wallet, one live session state file, and one shared `live_orders.csv`.

Strategy ledger state and pending live orders remain isolated per strategy inside that shared live session.

## 3. Run the supported runtime

```powershell
python main.py
```

This is the only supported public entrypoint. It starts the configured trading runtime and the local dashboard together.

When startup succeeds, the terminal prints:

- `Runtime started: paper trading + dashboard`
- `Dashboard URL: http://127.0.0.1:8787/`

## 4. View the dashboard

Open [http://127.0.0.1:8787/](http://127.0.0.1:8787/) in your browser to inspect quotes, signals, risk checks, and the config editor that writes back to `.env.dashboard`.

The dashboard runtime panel shows whether live trading is ready, whether a switch is pending or blocked, and whether recent orders are being read from the actual active mode logs.

## 5. Stop

Press `Ctrl+C` in the terminal where `python main.py` is running. The runtime asks both services to stop cleanly and leaves run data in `logs/` for later review.

## 6. Run The Strategy Optimizer

The repository now includes an offline paper-strategy optimization pipeline in [optimizer.py](./optimizer.py). It is separate from `python main.py` and is meant to help you:

- generate challenger candidates from historical CSV
- persist optimizer state into `logs/optimizer_state.json`
- refresh challenger promotion decisions from real paper-trading logs

### One-Shot Optimization

Run a single offline optimization pass from historical CSV:

```powershell
py optimizer.py ^
  --csv tests/fixtures/sample_history.csv ^
  --paper-log logs/paper_trades.csv ^
  --env-file .env.dashboard ^
  --output logs/optimizer_state.json ^
  --champion-id champion-paper
```

Expected terminal output:

- `Optimizer state written to ...`
- `Champion: ...`
- `Active challengers: ...`
- `Promotable candidates: ...`

### Watch Mode

Run the optimizer as a low-frequency loop that periodically:

- regenerates offline challengers
- refreshes promotion decisions from real paper results

```powershell
py optimizer.py ^
  --csv tests/fixtures/sample_history.csv ^
  --paper-log logs/paper_trades.csv ^
  --env-file .env.dashboard ^
  --output logs/optimizer_state.json ^
  --champion-id champion-paper ^
  --watch ^
  --optimize-interval-seconds 3600 ^
  --refresh-interval-seconds 900 ^
  --poll-interval-seconds 30
```

This mode does not touch live trading. It only updates optimizer/challenger state for paper evaluation.

## Additional resources

- [docs/operations_runbook.md](./docs/operations_runbook.md)
- [docs/dashboard_runbook.md](./docs/dashboard_runbook.md)
- [docs/optimizer_runbook.md](./docs/optimizer_runbook.md)
