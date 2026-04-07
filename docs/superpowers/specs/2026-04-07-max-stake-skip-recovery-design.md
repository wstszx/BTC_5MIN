# Max Stake Skip Recovery Design

## Scope
- Eliminate the deadlock where `recovery_loss` keeps future stake sizing above `MAX_STAKE`, causing the runtime to skip forever.
- Keep paper and live state progression aligned when a round is skipped by shared risk gates.
- Preserve the existing `MAX_STAKE` hard cap and alerting behavior.

## Design
- Treat repeated `order_cost_above_max_stake` skips as a safety stop-loss condition instead of a passive alert-only condition.
- Reuse the existing `consecutive_max_stake_skips` counter and trigger an automatic state reset once the streak reaches `MAX_CONSECUTIVE_LOSSES`.
- Reset means:
  - `recovery_loss = 0.0`
  - `consecutive_losses = 0`
  - `consecutive_max_stake_skips = 0`
  - `stop_loss_count += 1`
- Apply the same state transition rules in both paper and live paths:
  - skipped rounds after the entry window advance `round_index`
  - stop-loss-triggered skips reset state before saving
  - successful tradable plans clear `consecutive_max_stake_skips`

## Files
- `trader.py`: shared helper(s) for skip-state transitions; align paper/live skip handling.
- `tests/test_trader_runtime_and_live.py`: regressions for repeated max-stake skips and paper/live consistency.

## Non-Goals
- No change to the formula that computes `order_cost`.
- No change to the `MAX_STAKE` cap itself.
- No new dashboard control in this batch.
