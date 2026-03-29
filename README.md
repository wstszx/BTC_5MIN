# Polymarket BTC 5m Trading Bot

Pure Python trading bot for Polymarket's rolling BTC 5 minute `Up / Down` markets.

## Current Scope

This project currently supports:

- historical BTC 5m round export to CSV
- backtesting with shared strategy and sizing logic
- paper-trading runtime with persisted session state
- disabled-by-default live-trading placeholder hooks

This project does not enable real order placement by default. Live trading remains blocked until credentials and wallet-specific signing are intentionally added.

## Install

```bash
python -m pip install -r requirements.txt
```

## Configuration

Main runtime settings live in `config.py`:

- Polymarket API base URLs
- `series_id = 10684`
- `series_slug = btc-up-or-down-5m`
- strategy id
- target profit
- max consecutive losses
- max stake
- max buy price threshold
- daily loss cap
- polling interval
- entry timing
- open delay and pre-close offset

## Commands

Export history:

```bash
python main.py fetch-history --limit 100
```

Backtest a CSV:

```bash
python main.py backtest --csv tests/fixtures/sample_history.csv
```

Run paper trading once and exit:

```bash
python main.py paper-trade --dry-run-once
```

Attempt live trading:

```bash
python main.py live-trade --enable-live-trading
```

The live-trade command still refuses to run unless live trading is enabled in code and credentials are added later.

## Data and Logs

- historical exports go under `data/`
- paper trading state is saved to `logs/session_state.json`
- paper trading logs are appended under `logs/`

## Notes

- `websocket-client` and `tenacity` are included as optional helpers for future real-time and retry enhancements.
- Historical entry prices are approximations based on official Polymarket snapshots, not reconstructed full execution.
