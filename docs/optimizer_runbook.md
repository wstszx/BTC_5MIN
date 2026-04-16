# BTC_5MIN Optimizer Runbook (PowerShell)

This runbook explains how to operate the paper-strategy optimization pipeline safely. The optimizer is designed to improve paper-trading performance conservatively. It does not auto-switch live trading.

Run the commands below from the repository root, for example `D:\pythonProject\BTC_5MIN`.

## 1. Inputs And Outputs

Required inputs:

- historical CSV with round data
- `.env.dashboard` for current runtime defaults
- `logs/paper_trades.csv` for real paper challenger comparison

Primary output:

- `logs/optimizer_state.json`

That file stores:

- current champion id
- active challengers
- promotable challengers
- last optimization timestamp
- challenger paper metrics
- promotion decisions

## 2. One-Shot Optimization

Use this when you want to generate or refresh optimizer state manually:

```powershell
cd D:\pythonProject\BTC_5MIN
py optimizer.py ^
  --csv tests/fixtures/sample_history.csv ^
  --paper-log logs/paper_trades.csv ^
  --env-file .env.dashboard ^
  --output logs/optimizer_state.json ^
  --champion-id champion-paper
```

What this does:

- loads the current base config from `.env.dashboard`
- builds offline candidates for strategies 5 and 6
- runs walk-forward validation using the historical CSV
- writes top challengers into `logs/optimizer_state.json`

## 3. Watch Mode

Use this when you want a low-frequency optimizer loop:

```powershell
cd D:\pythonProject\BTC_5MIN
py optimizer.py ^
  --csv tests/fixtures/sample_history.csv ^
  --paper-log logs/paper_trades.csv ^
  --env-file .env.dashboard ^
  --output logs/optimizer_state.json ^
  --champion-id champion-paper ^
  --watch ^
  --optimize-interval-seconds 3600 ^
  --refresh-interval-seconds 900 ^
  --poll-interval-seconds 30
```

Meaning of the timing flags:

- `--optimize-interval-seconds`
  - how often to rebuild offline challenger candidates
- `--refresh-interval-seconds`
  - how often to recompute challenger-vs-champion paper decisions from `logs/paper_trades.csv`
- `--poll-interval-seconds`
  - scheduler loop sleep interval between checks

Recommended first settings:

- optimize every `3600` seconds
- refresh every `900` seconds
- poll every `30` seconds

## 4. How To Read The Result

The dashboard runtime panel now shows:

- optimizer enabled
- champion id
- number of active challengers
- number of promotable challengers
- last run time
- challenger detail list
- promotable detail list

The challenger detail lines include:

- candidate id
- base strategy id
- validation score
- decision state
- decision reason

## 5. Safe Operating Rules

- Keep the optimizer focused on paper trading only.
- Do not auto-promote into live trading.
- Treat `promotable` as a recommendation, not an irreversible action.
- Prefer low-frequency updates over rapid retuning.
- If `logs/paper_trades.csv` is sparse, promotion decisions may remain conservative due to insufficient trade counts.

## 6. Troubleshooting

If `logs/optimizer_state.json` is missing:

- verify the historical CSV path is correct
- verify `py optimizer.py ...` completed without argument errors
- verify the output path directory is writable

If challengers appear but never become promotable:

- check whether `paper_trades.csv` contains rows with challenger `experiment_id`
- check whether challenger trade count is below the promotion threshold
- check whether challenger advantage is too small or drawdown is too large

If dashboard shows optimizer enabled but no challengers:

- run a one-shot optimization pass first
- inspect `logs/optimizer_state.json`
- confirm the historical CSV produced at least one ranked candidate
