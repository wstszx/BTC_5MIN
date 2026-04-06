# Paper Pending Settlement Queue Design

**Goal:** Allow paper trading to keep participating in each eligible round without blocking on prior round settlement, while preserving correct final settlement and CSV reporting.

## Context

The current `run_paper_trading()` implementation in [trader.py](D:/pythonProject/BTC_5MIN/trader.py) is serial. After entering a paper trade, it waits for round end, then loops on `pending resolution` until Polymarket exposes final metadata, and only then proceeds to the next round. This means later rounds are missed whenever settlement lags.

The requested behavior is to allow every round to remain eligible for participation, even if previous paper rounds have not settled yet.

## Chosen Approach

Use an in-process pending-settlement queue stored inside `SessionState`.

The main paper loop will do two things on each poll:

1. Scan previously entered paper rounds that are still unresolved and settle any that are now resolvable.
2. Continue evaluating the current/next target round for fresh participation.

This keeps the current single-threaded structure and avoids background concurrency, while removing settlement from the critical path of new entries.

## Explicit Behavior Choice

New paper rounds will size from the most recently settled `SessionState` only.

Unsettled paper rounds will **not** affect:

- `cash_pnl`
- `recovery_loss`
- `consecutive_losses`
- `daily_realized_pnl`
- stop-loss reset decisions

This matches the requested mode: maximize round participation first, defer PnL/accounting impact until the round actually settles.

## Data Model Changes

Add `pending_paper_trades` to `SessionState` in [models.py](D:/pythonProject/BTC_5MIN/models.py).

Each pending item should store a frozen snapshot of the trade-at-entry, not just identifiers. Required fields:

- `round_index`
- `event_slug`
- `start_time`
- `end_time`
- `side`
- `price`
- `order_size`
- `order_cost`
- `expected_profit`
- `strategy`
- `entry_timing`
- `signal_open_up_price`
- `signal_current_up_price`
- `signal_threshold`
- `signal_delta`
- `signal_locked`
- `signal_reason`
- `queued_at`

Rationale:

- Settlement must not recompute the trade plan from a later `SessionState`.
- The final CSV row must reflect the actual trade snapshot that was entered.
- Restart recovery must be deterministic.

## Trader Changes

### 1. Queue, do not block

In [trader.py](D:/pythonProject/BTC_5MIN/trader.py), once a paper trade passes risk checks and reaches entry time:

- build the paper trade plan as today
- enqueue a frozen pending paper trade item
- increment `state.round_index`
- persist `session_state.json`
- continue the main polling loop immediately

The blocking sequence below will be removed from the critical path:

- sleep until round end
- loop on `pending resolution`
- settle before continuing

### 2. Settle pending paper trades opportunistically

At the top of each loop iteration, before selecting the next target round:

- iterate current `pending_paper_trades`
- for each item whose event metadata is resolved, settle it
- update `SessionState`
- append one final CSV record
- remove it from the pending queue
- persist state after any settlement batch

If a round is still unresolved, leave it in the queue and continue.

### 3. Frozen-plan settlement

Current paper settlement uses `_settle_paper_trade(...)`, which rebuilds a trade plan from the current `SessionState`. That is no longer valid once multiple unsettled rounds can overlap.

Replace or refactor settlement so it uses the frozen pending item snapshot.

Recommended shape:

- add a helper that converts a pending paper item into a frozen `TradePlan`
- add a helper that applies outcome using that frozen plan
- use current event metadata only to derive final `UP/DOWN` result

This prevents later settlements from corrupting earlier intended sizing.

## CSV Logging Behavior

Keep one final row per paper round in `paper_trades.csv`.

Do not write an intermediate ?entered but unsettled? row for trades that actually entered. The final row should still contain:

- trade fields from the frozen pending item
- final `result`
- `trade_pnl`
- post-settlement `cash_pnl`
- post-settlement `recovery_loss`
- post-settlement `consecutive_losses`

Skip rows remain immediate as today.

## Restart / Recovery Behavior

Because `pending_paper_trades` is stored inside `session_state.json`, restarting `py .\main.py` should preserve unresolved paper rounds.

After restart, the next loop iteration should:

- load `pending_paper_trades`
- attempt to settle whatever is now resolved
- continue fresh round evaluation normally

## Guardrails

### No duplicate queueing for one slug

Before queueing a paper trade, ensure the same `event_slug` is not already present in `pending_paper_trades`.

This prevents repeated entry on the same round during repeated polling inside the same entry window.

### No settlement-thread concurrency

Keep everything single-threaded inside the main loop.

This avoids lock management around `SessionState` and reduces regression risk.

### Runtime-control semantics

For this paper-only change, runtime control should stay simple:

- paper mode remains switchable
- no new pending-paper lock is required for switching in this iteration unless existing control logic truly depends on ?no open paper round? semantics

If switching safety later needs to consider pending paper settlements, that should be a separate decision.

## Known Tradeoff

Because unsettled rounds do not immediately affect sizing state, multiple consecutive rounds can be entered based on stale settled state.

This is intentional for the requested behavior, but it means:

- recovery sizing reacts later
- stop-loss resets react later
- short-term paper risk is effectively more aggressive

This is acceptable for the requested paper-mode behavior and should be documented in tests and any operator-facing notes.

## Tests Required

Update and extend paper-trading tests in [tests/test_trader_runtime_and_live.py](D:/pythonProject/BTC_5MIN/tests/test_trader_runtime_and_live.py) or adjacent test files to cover:

- a pending paper round does not block a later round from being evaluated
- one slug cannot be queued twice
- pending paper trades survive restart via `session_state.json`
- settlement uses frozen entry snapshot, not recomputed plan from later state
- CSV still contains one final row per entered paper round
- old serial settlement expectations are removed or rewritten to match queued settlement behavior

## Files Expected To Change

- [models.py](D:/pythonProject/BTC_5MIN/models.py)
- [trader.py](D:/pythonProject/BTC_5MIN/trader.py)
- [tests/test_trader_runtime_and_live.py](D:/pythonProject/BTC_5MIN/tests/test_trader_runtime_and_live.py)
- possibly [dashboard.py](D:/pythonProject/BTC_5MIN/dashboard.py) only if pending-paper state is surfaced again, which is out of scope for this change
