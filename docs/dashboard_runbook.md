# Dashboard Runbook (PowerShell)

All dashboard work assumes you are running from the repository root, for example `D:\pythonProject\BTC_5MIN`. The dashboard is part of the single supported workflow, so use the same startup path whenever you need to inspect or adjust the UI.

## 1. Start the combined runtime

```powershell
cd D:\pythonProject\BTC_5MIN
python main.py
```

`python main.py` launches the configured trading loop and exposes the dashboard on `http://127.0.0.1:8787/`. A successful start prints:

- `Runtime started: paper trading + dashboard`
- `Dashboard URL: http://127.0.0.1:8787/`

## 2. Access the dashboard

Open [http://127.0.0.1:8787/](http://127.0.0.1:8787/) in your browser. The UI shows the current quote, signal reasoning, risk gates, and the config editor that writes into `.env.dashboard`. Adjust any parameter in the editor and save it to persist the change for the next run.

The runtime mode card is the source of truth for trade mode changes:

- `Saved Mode` shows what `.env.dashboard` currently stores.
- `Running Mode` shows what the active worker is using right now.
- `Desired Mode` shows the manager target mode.
- `Switch State` shows whether the runtime is `idle`, `pending`, `switching`, or `blocked`.
- `Live Ready` and the live validation message show whether the saved live configuration is complete enough to start real trading safely.

## 3. What you can do

- Edit strategy parameters such as `STRATEGY_ID`, `TARGET_PROFIT`, and `MAX_STAKE`, then save them back to `.env.dashboard`.
- Switch `TRADE_MODE` between `paper` and `live` while `python main.py` is still running. A move into live trading also requires `LIVE_TRADING_ENABLED=true`, and the UI asks for confirmation before saving that change. The runtime then waits for the current round to finish before switching.
- Watch the real-time connection health area for connection status, reconnect activity, quote freshness, and whether stale-trade protection has been triggered.
- Review the recent trade list to confirm the runtime is still healthy. The dashboard reads recent orders from the actual active mode, not just the saved target mode.

## 4. Stop the dashboard

Press `Ctrl+C` in the terminal running `python main.py`. The runtime asks both services to stop cleanly, and the run data remains in `logs/` for later analysis.

## 5. Troubleshooting

- If the browser cannot connect, ensure `python main.py` is still running and that port 8787 is not occupied.
- If the config editor cannot save, check that `.env.dashboard` is writable and that no other process is holding the file.
- If `Saved Mode` and `Running Mode` do not match, check `Desired Mode` and `Switch State` before assuming the switch is stuck. `pending` means the runtime is still waiting for a safe boundary; `blocked` means it needs operator action.
- If live mode is selected but `Live Ready` is false, fix the missing live credentials or switch back to paper mode before expecting the live activation to complete.
