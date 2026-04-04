# Polymarket BTC 5m Trading Bot

This repository runs the end-to-end BTC 5-minute paper-trading workflow that ships both the trading engine and the local dashboard from a single command. Operators should center on the one supported flow described below; other commands exist for debugging but are not part of the normal path.

All commands assume you are working inside `D:\pythonProject\BTC_5MIN`.

## 1. Install dependencies

```powershell
cd D:\pythonProject\BTC_5MIN
python -m pip install -r requirements.txt
```

## 2. Configure parameters

`python main.py` references `.env.dashboard` first and then `config.py`, so edit that file (or use the dashboard UI once it is running) to tune the values operators care about. Common overrides include:

- `STRATEGY_ID`
- `TARGET_PROFIT`
- `MAX_STAKE`
- `MAX_CONSECUTIVE_LOSSES`
- `SIGNAL_MOMENTUM_THRESHOLD`
- `SIGNAL_WEAK_SIGNAL_MODE`

You can set these before launch via environment variables or update them live from the dashboard; both approaches write back to `.env.dashboard` so the next run inherits the changes.

## 3. Run the supported runtime

`python main.py`

This is the only supported public entrypoint. It launches the continuous paper-trading loop along with the local dashboard binding to `http://127.0.0.1:8787/`. Leave it running until you are ready to stop.

## 4. View the dashboard

Open [http://127.0.0.1:8787/](http://127.0.0.1:8787/) in your browser to inspect current quotes, signals, risk checks, and the same config editor that writes to `.env.dashboard`.

## 5. Stop

Press `Ctrl+C` in the terminal where `python main.py` is running to shut down both the paper trader and the dashboard.

## Additional resources

[docs/operations_runbook.md](./docs/operations_runbook.md) · [docs/dashboard_runbook.md](./docs/dashboard_runbook.md)
