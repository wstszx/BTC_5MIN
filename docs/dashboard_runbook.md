# Dashboard Runbook (PowerShell)

All dashboard work assumes you are in `D:\pythonProject\BTC_5MIN`. The dashboard runs as part of the single supported workflow, so follow the same steps whenever you need to inspect or tweak the UI.

## 1. Start the combined runtime

```powershell
cd D:\pythonProject\BTC_5MIN
python main.py
```

`python main.py` launches the paper-trading loop and immediately exposes the dashboard on `http://127.0.0.1:8787`. Look for `Dashboard running at http://127.0.0.1:8787` in the terminal output to confirm a successful start.

## 2. Access the dashboard

Open [http://127.0.0.1:8787](http://127.0.0.1:8787) in your browser. The UI shows the current quote, signal reasoning, risk gates, and the config editor that writes into `.env.dashboard`. Adjust any parameter in the editor and hit save to persist the change for the next run.

## 3. What you can do

- Edit strategy parameters (e.g., `STRATEGY_ID`, `TARGET_PROFIT`, `MAX_STAKE`) and save them back to `.env.dashboard`.
- Monitor websocket health via `ws_runtime` and confirm no `ws_stale_guard_triggered` messages are active.
- Review the right-hand paper summary and the scrolling list of recent trades to confirm the paper trading loop is still healthy.

## 4. Stop the dashboard

Press `Ctrl+C` in the terminal running `python main.py`. This shuts down the dashboard before exiting the trading loop, and the run data lands in `logs/` for later analysis.

## 5. Troubleshooting

- If the browser cannot connect, ensure `python main.py` is still running and that port 8787 is not occupied.
- If the config editor cannot save, check that `.env.dashboard` is writable and that no other process is holding the file.
