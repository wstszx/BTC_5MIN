# BTC_5MIN Operations Runbook (PowerShell)

This runbook is built around the single supported workflow: install dependencies, adjust dashboard parameters, run `python main.py`, open http://127.0.0.1:8787/, and stop with `Ctrl+C`. Daily operations should follow the steps below; legacy research modules in the repository are outside the supported runtime path.

Applicable path: `D:\pythonProject\BTC_5MIN`

## 1. Preparation

```powershell
cd D:\pythonProject\BTC_5MIN
python -m pip install -r requirements.txt
```

## 2. Configure parameters

All runtime knobs are surfaced through `.env.dashboard`. Edit that file before launching, or use the dashboard editor after launching—either way the changes are persisted for the next run.

Key fields to review:

- `STRATEGY_ID`
- `TARGET_PROFIT`
- `MAX_STAKE`
- `MAX_CONSECUTIVE_LOSSES`
- `SIGNAL_MOMENTUM_THRESHOLD`
- `SIGNAL_WEAK_SIGNAL_MODE`
- `LIVE_TRADING_ENABLED` (keep `false` to stay in paper trading)

These values can also be overridden via environment variables; `python main.py` prefers environment variables, then `.env.dashboard`, then the defaults in `config.py`.

## 3. Launch the single entrypoint

```powershell
python main.py
```

This command starts the continuous paper-trading loop and the local dashboard simultaneously. A successful launch prints `Dashboard running at http://127.0.0.1:8787` and `Config file: .env.dashboard` in the terminal.

## 4. Monitor and interact

Open http://127.0.0.1:8787 in your browser to inspect the current quote, signal reasoning, risk controls, and the live config editor. Every change written through the editor updates `.env.dashboard`.

Supporting files and directories:

- `logs/paper_trades.csv`: paper trade records kept for later inspection or offline analysis.
- `logs/session_state.json`: keeps track of rounds, cumulative PnL, and streak counters. Delete it to reset the paper trading state.
- `data/`: where history exports and research outputs live.

## 5. Stop

Press `Ctrl+C` to terminate `python main.py`. It shuts down the dashboard first and then the trading loop, while leaving logs in `logs/` for later review.

## 6. Troubleshooting

Legacy research and analysis modules still live in the repository, but the supported operator workflow is only the single `python main.py` runtime described above. If the dashboard cannot be reached, first verify the process is still running and port 8787 is available, then repeat the launch step above.
