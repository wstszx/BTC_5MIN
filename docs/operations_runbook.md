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

For overlapping keys, `.env.dashboard` is the source of truth. If a key is missing there, `python main.py` can still read it from temporary environment variables for that launch, and anything still missing falls back to the defaults in `config.py`. Environment-variable values do not get written back to `.env.dashboard`.

## 3. Launch the single entrypoint

```powershell
python main.py
```

This command starts the continuous paper-trading loop and the local dashboard together. A successful launch prints these lines in the terminal:

- `Runtime started: paper trading + dashboard`
- `Dashboard URL: http://127.0.0.1:8787/`

## 4. Monitor and interact

Open [http://127.0.0.1:8787/](http://127.0.0.1:8787/) in your browser to inspect the current quote, signal reasoning, risk controls, and the live config editor. Every save from the editor updates `.env.dashboard`.

Supporting files and directories:

- `logs/paper_trades.csv`: paper trade records for later inspection or offline analysis.
- `logs/session_state.json`: tracks rounds, cumulative PnL, and streak counters. Delete it to reset paper-trading state.
- `data/`: stores history exports and research outputs.

## 5. Stop

Press `Ctrl+C` to stop `python main.py`. The runtime asks both the dashboard and the paper-trading loop to stop cleanly, while leaving logs in `logs/` for later review.

## 6. Troubleshooting

Legacy research and analysis modules still live in the repository, but the supported operator workflow is only the single `python main.py` runtime described above. If the dashboard cannot be reached, first verify the process is still running and port 8787 is available, then repeat the launch step above.
