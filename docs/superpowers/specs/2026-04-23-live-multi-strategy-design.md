# Live Multi-Strategy Design

**Date:** 2026-04-23

**Status:** Drafted after interactive design review

## Goal

Allow `live` mode to run multiple strategies within one runtime, using a shared wallet for real order submission while keeping strategy state, sizing, risk control, pending settlement, and parameter sets isolated per strategy.

The target behavior is to make `live` orchestration match current multi-strategy `paper` behavior as closely as possible:

- multiple enabled strategies may evaluate the same round
- more than one strategy may submit a live order in the same round
- each strategy keeps its own pnl and risk ledger
- shared wallet balance is the only cross-strategy execution constraint

## User-Approved Scope

The user explicitly wants:

- a dedicated `LIVE_STRATEGY_IDS` config instead of reusing `PAPER_STRATEGY_IDS`
- per-strategy independent live parameters, not one shared parameter set
- one shared wallet for all live strategies
- the same handling model as paper trading: allow multiple strategies to trade in the same round as long as wallet balance is sufficient

The user does not want:

- single-winner priority between strategies in the same round
- strategy-level wallet separation
- live multi-timeframe support in this change

## Problem

The current live runtime is built around a single active strategy.

Today that assumption is visible in several places:

- config exposes only one active live strategy through `STRATEGY_ID`
- `SessionState` stores only one set of `pending_live_*` fields
- pending live settlement logic only knows how to settle one live order at a time
- live runtime control flags reflect a single pending live trade
- dashboard live views assume one active live strategy rather than a set of isolated live strategy ledgers

Because of that, the current runtime cannot safely support the desired behavior where strategy 5 and strategy 7, for example, both evaluate the same round, both independently pass their own risk gates, and both submit live orders if the shared wallet can afford both orders.

## Approaches Considered

### Approach A: One live worker with an internal multi-strategy coordinator

Use one `live-trading-worker`, but make it iterate over a configured list of live strategy profiles inside each round.

Why this is chosen:

- it mirrors the current paper multi-strategy execution model
- it avoids wallet race conditions caused by multiple OS threads trying to spend the same balance
- it preserves existing mode-switching structure in `main.py`
- it keeps log writing, pending settlement, and runtime control aggregation inside one coherent state machine

### Approach B: One live worker thread per strategy

Each strategy would behave like an independent live bot with its own loop.

Why this is rejected for v1:

- shared wallet balance would need strong coordination and locking
- two workers could make decisions from the same stale balance snapshot
- mode switching and runtime aggregation would become much harder to reason about
- it solves a problem the current operator workflow does not require

### Approach C: Thin wrapper around the existing single-strategy live path

A higher-level loop could call the existing single-strategy logic multiple times.

Why this is rejected for v1:

- the inner live path is still built around one `pending_live_*` state bundle
- settlement and state persistence would remain structurally single-strategy
- hidden single-strategy assumptions would keep surfacing in follow-up fixes

## Decision Summary

Introduce a live-only multi-strategy runtime model with these rules:

1. `live` remains single-timeframe and continues to use `MARKET_TIMEFRAME`.
2. `LIVE_STRATEGY_IDS` becomes the explicit live strategy list.
3. If `LIVE_STRATEGY_IDS` is absent or empty, live mode stays backward-compatible and uses `STRATEGY_ID`.
4. Each enabled live strategy gets an isolated live profile with its own parameters.
5. One `live-trading-worker` coordinates all enabled live strategies in sequence.
6. Each strategy keeps its own state ledger and its own pending live order.
7. Strategies may all trade the same round if they independently pass their own gates and the shared wallet budget is sufficient.
8. Wallet balance becomes an explicit shared execution constraint for live multi-strategy trading.
9. Live redeem remains wallet-level, not strategy-level.

## Configuration Design

### Top-Level Runtime Semantics

Keep the current top-level live runtime model:

- `TRADE_MODE=live` still starts one live trading worker
- `MARKET_TIMEFRAME` remains the active live timeframe
- live credentials and live redeem settings remain top-level runtime config

Add one live-only strategy selector:

- `LIVE_STRATEGY_IDS=5,6,7`

Rules:

- If `LIVE_STRATEGY_IDS` is present and non-empty, live mode uses that ordered list.
- If `LIVE_STRATEGY_IDS` is absent, live mode falls back to `[STRATEGY_ID]`.
- Duplicates are removed while preserving order.
- Invalid entries are rejected with dashboard validation feedback.

### Live Profile Namespace

Each live strategy gets its own prefixed config namespace:

- `LIVE_STRATEGY_5_TARGET_PROFIT`
- `LIVE_STRATEGY_5_BET_SIZING_MODE`
- `LIVE_STRATEGY_5_BASE_ORDER_COST`
- `LIVE_STRATEGY_5_MAX_CONSECUTIVE_LOSSES`
- `LIVE_STRATEGY_5_MAX_STAKE`
- `LIVE_STRATEGY_5_OPEN_DELAY_SECONDS`
- `LIVE_STRATEGY_5_SIGNAL_MOMENTUM_THRESHOLD`
- `LIVE_STRATEGY_5_SIGNAL_FALLBACK_STRATEGY_ID`
- `LIVE_STRATEGY_6_OFI_THRESHOLD`
- `LIVE_STRATEGY_6_BINANCE_SIGNAL_STALE_SECONDS`
- `LIVE_STRATEGY_7_STRATEGY7_OFI_THRESHOLD`
- `LIVE_STRATEGY_7_STRATEGY7_MOMENTUM_THRESHOLD`
- `LIVE_STRATEGY_7_STRATEGY7_MAX_ENTRY_PRICE`
- `LIVE_STRATEGY_7_STRATEGY7_MIN_SIGNAL_GAP`
- `LIVE_STRATEGY_7_STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS`
- `LIVE_STRATEGY_7_STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP`
- `LIVE_STRATEGY_7_STRATEGY7_LATE_CONFIRM_RELAX_SECONDS`

The live profile should include every parameter the operator expects to vary per strategy, including:

- strategy selection
- target profit and sizing inputs
- max stake and loss reset behavior
- signal thresholds and fallback behavior
- OFI freshness thresholds
- strategy 7 confirmation thresholds
- entry timing fields that materially affect live execution timing

### Config Parsing Model

The config layer should expose:

- the existing top-level `AppConfig` for shared runtime and credential behavior
- a new `live_strategy_ids: list[int]`
- a derived `live_profiles: dict[int, LiveStrategyProfile]`

Conceptually:

```python
live_profiles = {
    5: LiveStrategyProfile(...),
    6: LiveStrategyProfile(...),
    7: LiveStrategyProfile(...),
}
```

Each profile should be convertible into an `AppConfig`-like runtime view so the existing strategy decision and sizing functions can continue consuming familiar fields through `replace(cfg, strategy_id=...)` or an equivalent derived config object.

## Runtime Architecture

### Chosen Direction

Use one `live-trading-worker` that acts as a coordinator over multiple live strategy profiles.

Within a single runtime pass:

- load or refresh the shared live config
- resolve enabled live strategy ids
- derive one strategy-specific config view per live strategy
- share one market/timeframe lookup for the current round
- evaluate settlement and new trade decisions per strategy in order

### Why This Direction

This keeps the new feature aligned with the current codebase:

- `main.py` already treats live mode as one runtime worker plus optional redeem worker
- `run_live_trading` already owns live safety semantics such as pending settlement and mode switching
- paper multi-strategy execution already established the pattern of one coordinator over isolated strategy state

This intentionally avoids turning live mode into several independent threads competing for one wallet.

## State Model

### New Live Strategy State

Replace the effective meaning of top-level single-live pending fields with a per-strategy map.

Add:

- `live_strategies: dict[int, LiveStrategyState]`

`LiveStrategyState` stores the same strategy-local ledger fields a single live strategy currently needs:

- `round_index`
- `cash_pnl`
- `recovery_loss`
- `consecutive_losses`
- `consecutive_max_stake_skips`
- `signal_round_slug`
- `signal_round_open_up_price`
- `signal_round_locked_side`
- `strategy6_last_ofi_score`
- `stop_loss_count`
- `daily_realized_pnl`
- `current_day`
- `pending_live_slug`
- `pending_live_side`
- `pending_live_price`
- `pending_live_order_size`
- `pending_live_order_cost`
- `pending_live_expected_profit`
- `pending_live_order_id`
- `pending_live_end_time`

This gives each live strategy its own independent:

- pnl ledger
- recovery path
- loss streak
- max-stake skip streak
- signal lock state
- pending settlement record

### SessionState Compatibility

`SessionState` should still load older single-strategy live files.

Compatibility rules:

- if `live_strategies` is present, use it
- if only legacy top-level `pending_live_*` fields exist, wrap them into `live_strategies[effective_strategy_id]`
- if there is no pending state but there are legacy single-strategy ledger fields, use them to seed the effective live strategy entry
- if multiple live strategies are enabled and no per-strategy state exists yet, create empty entries for all enabled strategy ids

This mirrors the backward-compatibility approach already used in paper multi-strategy migration.

## Shared Wallet Budget Model

### Design Goal

Allow multiple live strategies to submit in the same round while preventing the runtime from overspending a shared wallet.

### New Rule

Wallet balance becomes a first-class live execution gate.

The current code already computes each strategy's own order size and order cost from that strategy's independent risk state. That remains unchanged. The new cross-strategy rule is:

- a strategy may submit only if its own plan says it should trade
- and its own order cost is within that strategy's own `max_stake`
- and the shared wallet still has sufficient available balance

### Budget Evaluation

In each live runtime pass:

1. Read current available wallet balance through a dedicated helper.
2. Initialize `remaining_live_budget` from that value.
3. Evaluate strategies in the configured `LIVE_STRATEGY_IDS` order.
4. For each strategy that reaches the submission step:
   - require `plan.order_cost <= remaining_live_budget`
   - if submission succeeds, decrement `remaining_live_budget -= plan.order_cost`
   - if insufficient, skip only that strategy with a specific live skip reason

This achieves the user-approved behavior:

- multiple strategies may submit in one round
- all are allowed if the wallet can afford them
- later strategies do not pretend earlier submissions never consumed funds

### Why Sequential Budget Reservation

The wallet provider may not immediately expose a refreshed balance after each order. Relying on repeated live balance fetches alone would still risk overspending due to stale reads.

Local in-pass budget reservation solves that:

- one balance snapshot starts the pass
- successful submissions reserve part of that snapshot locally
- the next strategy sees the reduced local budget even before remote wallet state catches up

### Balance Read Failure

If the runtime cannot obtain a trustworthy available live balance, it should not enter the multi-strategy submission phase.

Behavior:

- no live orders are submitted in that pass
- runtime returns or logs a clear live error status
- the failure is treated as a shared runtime dependency failure, not as a fake strategy-specific skip

This is stricter than paper mode by design because real wallet spending is involved.

## Live Order and Settlement Semantics

### Strategy-Level Pending Orders

A single strategy may still have at most one pending live order at a time.

However:

- strategy 5 may have a pending live order
- strategy 7 may also have a pending live order
- both may remain pending until their respective rounds resolve

Pending settlement is therefore strategy-local, not runtime-global.

### Settlement Flow

At the start of each live runtime pass:

- iterate through enabled live strategies
- settle any pending live order that is ready for that strategy
- keep unresolved or unverified fills pending only for that strategy

The existing safety requirements remain unchanged:

- do not settle before round end
- do not settle without sufficient fill proof
- do not fabricate pnl for unverified fills

These rules simply move from one global pending record to several isolated strategy pending records.

### Multiple Orders in the Same Round

If strategies 5 and 7 both target the same round:

- both may independently compute a side and risk plan
- both may independently submit live orders
- both receive separate pending live records
- both settle independently when the round resolves

This matches the user requirement to make live handling align with paper handling.

## Logging and Persistence

### Shared Live Order Log

Keep using one shared `live_orders.csv`.

Why:

- the log already includes a `strategy` field
- historical continuity is preserved
- cross-strategy live comparison stays possible
- per-strategy filtering remains easy in the dashboard

### Live Session State File

Keep using one live session state file, but evolve its schema to include `live_strategies`.

This preserves the current operational model:

- one live runtime
- one live session state document
- many strategy entries within that document

No new per-strategy live state file is required for the first version.

## Runtime Control and Mode Switching

### Aggregated Live Runtime Flags

`RuntimeControl` should keep exposing one live runtime snapshot, but derive it from all live strategies.

Aggregation rules:

- `round_in_progress = true` if any live strategy has an active pending order or is still in a live round workflow that makes switching unsafe
- `pending_live_order = true` if any live strategy has a pending live order
- `safe_to_switch = true` only if all live strategies are safe to stop

This preserves the current safety guarantee that live mode cannot switch away while real live exposure is still unresolved.

### Live Reload Behavior

First version behavior:

- changing `LIVE_STRATEGY_IDS` triggers a normal live runtime restart
- changing any `LIVE_STRATEGY_<id>_*` parameter also triggers a normal live runtime restart
- no fine-grained hot replacement of one strategy profile inside a running live worker is required

This keeps the rollout simpler and consistent with existing restart semantics.

## Dashboard Experience

### Config Panel

Add live-only multi-strategy controls:

- `LIVE_STRATEGY_IDS` as a live multi-select field
- strategy-specific live profile views or expandable sections

Keep `STRATEGY_ID` visible as a compatibility field and as a focused strategy selector for places where the dashboard still expects a single inspected strategy.

### Live Strategy Views

The dashboard should support inspecting one live strategy at a time while acknowledging that multiple live strategies may be active underneath.

At minimum, the live payload should expose:

- the enabled `LIVE_STRATEGY_IDS`
- which strategy is currently selected for inspection
- the selected strategy's live state
- shared runtime state aggregated across all live strategies

### Live Runtime Display

The operator should be able to see:

- which live strategies are enabled
- whether each strategy has a pending live order
- each strategy's `cash_pnl`, `recovery_loss`, and `consecutive_losses`
- whether a strategy traded, skipped, or was blocked by wallet balance
- current available wallet balance
- current in-pass reserved budget if the runtime exposes round-level diagnostics

### Results Filtering

Recent live order views and summary metrics should remain filterable by the existing `strategy` field in the log.

This keeps live reporting aligned with paper reporting.

## Error Handling

- Invalid `LIVE_STRATEGY_IDS` values are rejected with clear dashboard validation feedback.
- Duplicate live strategy ids are de-duplicated while preserving order.
- If live balance cannot be read safely, the runtime blocks live submissions for that pass.
- If one strategy fails during evaluation, the runtime should isolate that failure as much as possible and avoid corrupting other strategies' persisted state.
- If a strategy's live order submission is rejected, no pending live state is persisted for that strategy.
- If a strategy is blocked by shared wallet budget, only that strategy is skipped; others continue evaluation if budget remains.

## Testing Strategy

Add or update tests for:

1. Parsing and normalization of `LIVE_STRATEGY_IDS`.
2. Parsing and override behavior for `LIVE_STRATEGY_<id>_*` parameters.
3. Backward-compatible loading of old single-strategy live session state.
4. Independent per-strategy live ledger progression in one runtime pass.
5. Same-round multi-strategy live submissions when balance is sufficient.
6. In-pass wallet budget decrement after successful strategy submission.
7. Later strategy skip with `insufficient_live_wallet_balance` after earlier strategies consume budget.
8. Independent pending settlement per live strategy.
9. Shared `live_orders.csv` output with correct `strategy` values.
10. Aggregated runtime control behavior for `round_in_progress`, `safe_to_switch`, and `pending_live_order`.
11. Dashboard payload support for live multi-strategy config and live strategy inspection.
12. Existing live safety guarantees: dry-run immutability, order-id validation, and unverified settlement blocking.

## Non-Goals

- No live multi-timeframe runtime in this change.
- No one-thread-per-strategy live architecture.
- No advanced portfolio optimization or capital allocation engine between strategies.
- No strategy-level wallet separation.
- No netting or hedging layer across live strategies.
- No redesign of strategy math itself.

## Recommended Implementation Order

1. Extend config parsing with `LIVE_STRATEGY_IDS` and per-strategy live profile overrides.
2. Introduce `LiveStrategyState` and migrate session-state loading/saving.
3. Refactor live settlement helpers to work against one strategy state at a time.
4. Refactor `run_live_trading` into a coordinator over live strategy profiles.
5. Add shared live wallet balance helper and in-pass budget reservation.
6. Update runtime control aggregation and dashboard payloads.
7. Add dashboard controls and live strategy inspection UI.
8. Add regression tests for config, state migration, runtime behavior, and dashboard payloads.
