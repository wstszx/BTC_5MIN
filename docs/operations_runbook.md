# BTC_5MIN Operations Runbook (PowerShell)

This runbook is built around the single supported workflow: install dependencies, tune `.env.dashboard`, run `python main.py`, open [http://127.0.0.1:8787/](http://127.0.0.1:8787/), and stop with `Ctrl+C`. Daily operations should follow the steps below; legacy research modules in the repository are outside the supported runtime path.

Run the commands below from the repository root, for example `D:\pythonProject\BTC_5MIN`.

## 1. Preparation

```powershell
cd D:\pythonProject\BTC_5MIN
python -m pip install -r requirements.txt
```

## 2. Configure parameters

All supported runtime knobs are surfaced through `.env.dashboard`. Edit that file before launch, or use the dashboard editor after launch to save changes for the next run.

Key fields to review:

- `STRATEGY_ID`
- `TARGET_PROFIT`
- `MAX_STAKE`
- `MAX_CONSECUTIVE_LOSSES`
- `SIGNAL_MOMENTUM_THRESHOLD`
- `SIGNAL_WEAK_SIGNAL_MODE`
- `TRADE_MODE`
- `LIVE_TRADING_ENABLED`
- `POLYMARKET_FUNDER`
- `POLYMARKET_API_KEY`
- `POLYMARKET_API_SECRET`
- `POLYMARKET_API_PASSPHRASE`
- `POLYMARKET_BUILDER_API_KEY`
- `POLYMARKET_BUILDER_SECRET`
- `POLYMARKET_BUILDER_PASSPHRASE`
- `POLYMARKET_RELAYER_API_KEY`
- `POLYMARKET_RELAYER_API_KEY_ADDRESS`
- `LIVE_AUTO_REDEEM_ENABLED`
- `LIVE_AUTO_REDEEM_POLL_SECONDS`
- `LIVE_AUTO_REDEEM_MAX_RETRIES`
- `LIVE_AUTO_REDEEM_INITIAL_BACKOFF_SECONDS`
- `LIVE_AUTO_REDEEM_MAX_BACKOFF_SECONDS`
- `LIVE_AUTO_REDEEM_DRY_RUN`

Credential note:

- `POLYMARKET_API_*` is only for live CLOB trading.
- `POLYMARKET_BUILDER_*` and `POLYMARKET_RELAYER_*` are only for official gasless live redeem.
- Direct Polygon `web3` redeem is no longer a supported runtime path.

Trading mode rules:

- Use `TRADE_MODE=paper` for paper trading.
- Use `TRADE_MODE=live` only when live credentials are present and `LIVE_TRADING_ENABLED=true` is also set.
- Set `POLYMARKET_FUNDER` to the wallet address (`0x...`) corresponding to `POLYMARKET_PRIVATE_KEY`.
- A dashboard save updates `.env.dashboard` and the runtime manager target mode.
- The current worker finishes its current round before the runtime switches modes.
- `paper -> live` still requires confirmation and valid live credentials.
- If a live order is unresolved or live validation fails, the runtime remains pending or blocked until it is safe to continue.

For overlapping keys, `.env.dashboard` is the source of truth. If a key is missing there, `python main.py` can still read it from temporary environment variables for that launch, and anything still missing falls back to the defaults in `config.py`. Environment-variable values do not get written back to `.env.dashboard`.

## 3. Launch the single entrypoint

```powershell
python main.py
```

This command starts the configured trading loop and the local dashboard together. A successful launch prints these lines in the terminal:

- `Runtime started: paper trading + dashboard`
- `Dashboard URL: http://127.0.0.1:8787/`

## 4. Monitor and interact

Open [http://127.0.0.1:8787/](http://127.0.0.1:8787/) in your browser to inspect the current quote, signal reasoning, risk controls, and the live config editor. Every save from the editor updates `.env.dashboard`. The editor shows a single `启用实盘` switch, and it labels `POLYMARKET_FUNDER` as `实盘钱包地址`.

The runtime status card now also includes live auto redeem status when the runtime is in live mode or auto redeem is enabled:

- `Target Mode`: the mode saved in `.env.dashboard`.
- `Running Mode`: the worker mode that is actually running now.
- `Switch Pending`: whether the saved mode and running mode still differ.
- `Live Ready`: whether live mode passes credential validation.
- `Validation`: the current validation error, if live mode is blocked.
- `Auto Redeem`: whether live auto redeem is enabled for the active config.
- `Redeem Auth Mode`: whether redeem is configured through Builder credentials, Relayer key credentials, or still unconfigured.
- `Pending Redeems`: current redeemable positions waiting in `live_redeem_state.json`.
- `Last Result / Last Attempt / Last Submission Id / Last Tx Hash`: the latest redeem worker outcome, attempt time, relayer submission id, and tx hash when available.

Supporting files and directories:

- `logs/paper_trades.csv`: paper trade records for later inspection or offline analysis.
- `logs/session_state.json`: tracks rounds, cumulative PnL, and streak counters. Delete it to reset paper-trading state.
- `logs/live_orders.csv`: live order log used when the active worker is in live mode.
- `logs/live_session_state.json`: live-mode runtime state used when the active worker is in live mode.
- `logs/live_redeem_state.json`: live auto redeem worker state, including retry scheduling, latest result, relayer submission metadata, and tx hash tracking when available.
- `data/`: stores history exports and research outputs.

## 5. Stop

Press `Ctrl+C` to stop `python main.py`. The runtime asks both the dashboard and the paper-trading loop to stop cleanly, while leaving logs in `logs/` for later review.

## 6. Troubleshooting

Legacy research and analysis modules still live in the repository, but the supported operator workflow is only the single `python main.py` runtime described above. If the dashboard cannot be reached, first verify the process is still running and port 8787 is available, then repeat the launch step above.
