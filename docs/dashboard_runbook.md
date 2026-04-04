# Dashboard Runbook (PowerShell)

All dashboard work assumes you are running from the repository root, for example `D:\pythonProject\BTC_5MIN`. The dashboard is part of the single supported workflow, so use the same startup path whenever you need to inspect or adjust the UI.

## 1. Start the combined runtime

```powershell
cd D:\pythonProject\BTC_5MIN
python main.py
```

`python main.py` launches the paper-trading loop and exposes the dashboard on `http://127.0.0.1:8787/`. A successful start prints:

- `Runtime started: paper trading + dashboard`
- `Dashboard URL: http://127.0.0.1:8787/`

## 2. Access the dashboard

Open [http://127.0.0.1:8787/](http://127.0.0.1:8787/) in your browser. The UI shows the current quote, signal reasoning, risk gates, and the config editor that writes into `.env.dashboard`. Adjust any parameter in the editor and save it to persist the change for the next run.

## 3. What you can do

- Edit strategy parameters such as `STRATEGY_ID`, `TARGET_PROFIT`, and `MAX_STAKE`, then save them back to `.env.dashboard`.
- Watch the real-time connection health area for connection status, reconnect activity, quote freshness, and whether stale-trade protection has been triggered.
- Review the paper-trading summary and recent trade list to confirm the runtime is still healthy.

## 4. Stop the dashboard

Press `Ctrl+C` in the terminal running `python main.py`. The runtime asks both services to stop cleanly, and the run data remains in `logs/` for later analysis.

## 5. Troubleshooting

- If the browser cannot connect, ensure `python main.py` is still running and that port 8787 is not occupied.
- If the config editor cannot save, check that `.env.dashboard` is writable and that no other process is holding the file.
