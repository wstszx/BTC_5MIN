# Polymarket BTC 5m Trading Bot

This repository runs the BTC 5-minute paper-trading workflow from a single command. Operators should use the one supported flow below; legacy research modules that still exist in the repository are outside the supported runtime path.

Run the commands below from the repository root, for example `D:\pythonProject\BTC_5MIN`.

## 1. Install dependencies

```powershell
cd D:\pythonProject\BTC_5MIN
python -m pip install -r requirements.txt
```

## 2. Configure parameters

`python main.py` uses `.env.dashboard` as the primary operator config file. For overlapping keys, values in `.env.dashboard` win. If a key is missing there, the runtime can still read it from temporary environment variables for that launch; anything still missing falls back to the defaults in `config.py`. Environment-variable values do not get written back to `.env.dashboard`.

Dashboard saves do write back to `.env.dashboard` for the next run. Common fields include:

- `STRATEGY_ID`
- `TARGET_PROFIT`
- `MAX_STAKE`
- `MAX_CONSECUTIVE_LOSSES`
- `SIGNAL_MOMENTUM_THRESHOLD`
- `SIGNAL_WEAK_SIGNAL_MODE`

## 3. Run the supported runtime

```powershell
python main.py
```

This is the only supported public entrypoint. It starts the continuous paper-trading loop and the local dashboard together.

When startup succeeds, the terminal prints:

- `Runtime started: paper trading + dashboard`
- `Dashboard URL: http://127.0.0.1:8787/`

## 4. View the dashboard

Open [http://127.0.0.1:8787/](http://127.0.0.1:8787/) in your browser to inspect quotes, signals, risk checks, and the config editor that writes back to `.env.dashboard`.

## 5. Stop

Press `Ctrl+C` in the terminal where `python main.py` is running. The runtime asks both services to stop cleanly and leaves run data in `logs/` for later review.

## Additional resources

- [docs/operations_runbook.md](./docs/operations_runbook.md)
- [docs/dashboard_runbook.md](./docs/dashboard_runbook.md)
