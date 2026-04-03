# Live Runtime Safety Design

## Scope
- Respect configured entry windows in `live-trade`.
- Keep `--dry-run-once` read-only.
- Refuse to persist pending live state when order submission is not clearly accepted.
- Avoid settling pending live trades from purely theoretical sizing data when no fill proof is available.

## Design
- Gate live submissions on `entry_time` the same way paper trading waits for entry.
- Thread a `persist_state` flag through the live path so dry-run evaluation cannot mutate session files.
- Add explicit order-submission validation that requires a successful response and a stable order id before the runtime records a pending live trade.
- Extend pending live state with order verification fields and require verified fill details before applying `apply_round_outcome()`. If fill details are unavailable, keep the trade pending and surface an `awaiting_fill_confirmation` status instead of fabricating PnL.

## Files
- `trader.py`: live order gating, dry-run persistence guard, order-response validation, pending-settlement rules.
- `models.py`: pending live fields for verification metadata.
- `tests/test_trader_runtime_and_live.py`: regression tests for entry timing, dry-run immutability, rejected submissions, and unverified settlement behavior.
