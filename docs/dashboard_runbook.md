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

The runtime status card is the source of truth for live-switch progress and current worker state:

- `Target Mode` shows the mode saved in `.env.dashboard`.
- `Running Mode` shows the worker mode actually running now.
- `Switch Pending` shows whether the saved mode and the active mode still differ.
- `Live Ready` reports whether live mode currently passes validation.
- `Validation` shows the blocking validation error when live mode is not ready.
- `Auto Redeem` shows whether live auto redeem is enabled.
- `Redeem Auth Mode` shows whether auto redeem is configured through Builder credentials, Relayer key credentials, or is still unconfigured.
- `Pending Redeems` shows how many redeemable conditions are still pending in `logs/live_redeem_state.json`.
- `Last Result / Last Attempt / Last Submission Id / Last Tx Hash` expose the latest redeem worker result, attempt time, relayer submission id, and tx hash when available.
- In the config editor, `POLYMARKET_FUNDER` is the `0x...` wallet address corresponding to your live private key.
- In the config editor, `POLYMARKET_API_*` is for live CLOB trading, while `POLYMARKET_BUILDER_*` and `POLYMARKET_RELAYER_*` are for official gasless redeem.

## 3. What you can do

- Edit strategy parameters such as `STRATEGY_ID`, `TARGET_PROFIT`, and `MAX_STAKE`, then save them back to `.env.dashboard`.
- Use the `启用实盘` switch in the config editor while `python main.py` is still running. Turning it on maps to `TRADE_MODE=live` plus `LIVE_TRADING_ENABLED=true`; turning it off maps to paper mode. The runtime then waits for the current round to finish before switching.
- Watch the real-time connection health area for connection status, reconnect activity, quote freshness, and whether stale-trade protection has been triggered.
- Review the recent trade list to confirm the runtime is still healthy. The dashboard reads recent orders from the actual active mode, not just the saved target mode.
- Use the runtime status card to monitor live auto redeem discovery and retry progress without opening state files manually.
- When `LIVE_AUTO_REDEEM_DRY_RUN=true`, expect the dashboard to update redeem visibility and latest results while no real redeem transaction is sent.
- When dry-run is off, redeem attempts now go through the official Relayer / Gasless flow. `Last Submission Id` may appear before any chain tx hash is available.

## 4. Stop the dashboard

Press `Ctrl+C` in the terminal running `python main.py`. The runtime asks both services to stop cleanly, and the run data remains in `logs/` for later analysis.

## 5. Troubleshooting

- If the browser cannot connect, ensure `python main.py` is still running and that port 8787 is not occupied.
- If the config editor cannot save, check that `.env.dashboard` is writable and that no other process is holding the file.
- If `Target Mode` is `live` but `Running Mode` stays on `paper`, read `Validation` first. The runtime stays `pending` or `blocked` until credentials are valid and the current round is safe to switch.
- If auto redeem looks idle in live mode, check `logs/live_redeem_state.json` together with the `Auto Redeem / Pending Redeems / Last Result` rows in the runtime card.
- If `LIVE_AUTO_REDEEM_DRY_RUN=true`, the dashboard should still show discovery and retry state changes, but no real relayer submission is sent.
