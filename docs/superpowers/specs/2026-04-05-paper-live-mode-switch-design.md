# Paper/Live Mode Switch Design

**Date:** 2026-04-05

**Status:** Proposed and validated with user through iterative review

## Goal

Add a front-end controlled trading mode switch that lets the operator choose between paper trading and real trading, while keeping mode activation safe by requiring a runtime restart before the new mode takes effect.

## Problem

The current project already contains two uneven halves:

- the supported runtime path is a single-command paper-trading workflow launched by `python main.py`
- the codebase also contains real-order submission logic in `trader.py`, but that path is not wired into the supported runtime or dashboard configuration flow

That leaves the operator without a safe, trustworthy way to move between simulation and real execution. A simple UI toggle is not enough on its own because accidental mode changes could create hidden live-trading risk.

The requested behavior is therefore not hot-switching. The requested behavior is a configuration-driven mode switch that is saved from the dashboard and only becomes active after the operator restarts the runtime.

## Confirmed Scope

The user confirmed the following boundaries:

1. The project should support both paper mode and real trading mode.
2. The operator should control the desired mode from the front end.
3. Turning live trading on should not hot-switch the already running worker.
4. The saved mode should take effect only after the runtime is restarted.
5. The implementation should prefer the safer restart-based model over immediate runtime switching.

## Design Principles

1. Safety is more important than convenience when live orders are possible.
2. The UI may request a mode, but the backend startup path remains the final authority on whether live trading can actually run.
3. Paper and live runtime state must stay isolated so one mode cannot corrupt the other.
4. Startup must fail loudly when live mode is requested but the required credentials or parameters are missing.
5. The dashboard should make it obvious which mode is saved and which mode the current process is actually running.

## Chosen Approach

Use a saved runtime trading mode plus a second live-trading safety gate.

The operator-facing mode switch will be represented by a new config value, `TRADE_MODE`, with supported values:

- `paper`
- `live`

The runtime should only enter real trading when both of the following are true:

1. `TRADE_MODE=live`
2. `LIVE_TRADING_ENABLED=true`

In addition, live runtime startup must verify that required credentials and live-order settings are present and valid.

`main.py` will stop assuming that the supported runtime is always paper trading. Instead, it will load the shared configuration from `.env.dashboard`, decide which trading worker to start, and then launch:

- dashboard + paper worker when `TRADE_MODE=paper`
- dashboard + live worker when `TRADE_MODE=live`

The dashboard remains always available in both modes, but saving a new mode only updates the next launch configuration. It does not rewire the already running worker.

## Alternatives Considered

### Option 1: Startup-selected paper/live worker with restart required

This is the selected approach.

Benefits:

- safest operator experience for real money paths
- clear runtime ownership and easier debugging
- minimal ambiguity about when the mode actually changes

Trade-off:

- switching modes requires an intentional restart

### Option 2: One long-running worker that branches between paper/live inside the loop

Benefits:

- more code appears shared at first glance

Trade-offs:

- paper and live state machines become harder to reason about
- mode transitions become more fragile
- future debugging becomes more expensive

### Option 3: UI toggle only, with no runtime architecture change

Benefits:

- smallest immediate edit count

Trade-offs:

- the supported runtime would still only run paper mode
- the UI could imply live capability without actually owning the runtime behavior
- high risk of operator confusion

## Runtime Architecture

### Configuration Model

`.env.dashboard` remains the shared operator-facing runtime config source.

`AppConfig` should gain first-class support for:

- `trade_mode`, defaulting to `paper`
- the existing live-trading settings already present in the model

Recommended rules:

1. `trade_mode` accepts only `paper` or `live`.
2. `live_trading_enabled` remains a separate boolean guard and defaults to `false`.
3. Live-only secrets are optional in paper mode but required during live startup validation.

### Worker Selection in `main.py`

`main.py` should continue to act as a thin launcher, but it should no longer hardcode `run_paper_trading(...)`.

Recommended startup flow:

1. Load `.env.dashboard`.
2. Build `AppConfig` from those values.
3. Start the dashboard runtime.
4. Select the trading worker based on `cfg.trade_mode`.
5. If `paper`, start `run_paper_trading(...)`.
6. If `live`, validate live configuration and start a new continuous `run_live_trading(...)` loop.
7. Print startup summary including the configured mode and dashboard URL.

The runtime should not silently downgrade from `live` to `paper`. If live mode is requested but cannot start safely, the process should fail fast with an explicit startup error.

### Live Runtime Loop

`trader.py` already contains `place_live_order(...)`, which currently evaluates and submits one live order attempt. The supported runtime needs a continuous live worker equivalent to the existing paper loop.

Recommended behavior for `run_live_trading(...)`:

1. Reuse the existing stop-event pattern used by `run_paper_trading(...)`.
2. Poll at the configured cadence.
3. Call `place_live_order(...)` at safe runtime boundaries.
4. Preserve existing pending-live-settlement behavior rather than inventing a second live state path.
5. Use live-specific session and order log files.
6. Reload shared config from `.env.dashboard` between safe cycles so non-mode settings still update after save.

Mode changes themselves do not hot-apply. If the saved `TRADE_MODE` stops matching the worker that is already running, the dashboard should report that restart is required, but the worker should continue in its current mode until the process exits.

## Dashboard Design

### Config Surface

The dashboard config editor should expose the new operator controls:

- `TRADE_MODE`
- `LIVE_TRADING_ENABLED`
- `POLYMARKET_PRIVATE_KEY`
- `POLYMARKET_FUNDER`
- `POLYMARKET_CHAIN_ID`
- `POLYMARKET_SIGNATURE_TYPE`
- `POLYMARKET_ORDER_TYPE`

`TRADE_MODE` should be the primary UI switch with these meanings:

- `paper`: paper test mode, no real orders
- `live`: real trading mode, requires restart after save

### Conditional UI

When `TRADE_MODE=paper`, the live-only settings can be hidden or visually muted.

When `TRADE_MODE=live`, the dashboard should surface the live configuration section and show a clear warning that:

- real orders are possible in this mode
- saving the change does not affect the already running worker
- restarting `python main.py` is required before live mode becomes active

### Save Protection

Switching into `live` should require an explicit UI confirmation before save completes.

The exact interaction can stay lightweight, but it should force a deliberate action rather than allowing an accidental dropdown change to become the next startup mode.

Recommended behavior:

1. If the submitted config changes `TRADE_MODE` from `paper` to `live`, show a confirmation dialog.
2. If the user cancels, do not send the config update.
3. If the user confirms, continue with the normal save request.

### Sensitive Field Handling

The private key must not be echoed back to the browser in full plaintext.

Recommended rules:

1. The private key input uses a password-style field.
2. Config payloads return a masked placeholder for an already saved private key instead of the raw value.
3. Saving unrelated fields must not erase the saved private key.
4. If the user leaves the masked value untouched, the backend keeps the existing secret.
5. Only an explicit replacement or explicit clear action should modify the stored secret.

### Runtime Status Messaging

The dashboard should separate these concepts clearly:

- saved mode in `.env.dashboard`
- mode of the currently running worker
- whether restart is required for the saved mode to take effect
- whether the live configuration is complete enough to start safely next time

This prevents the operator from assuming that a saved `live` setting means the current process is already live.

## Validation and Safety Rules

### Startup Validation for Live Mode

When `TRADE_MODE=live`, startup must refuse to continue unless the live runtime can be initialized safely.

At minimum, validation should require:

- `LIVE_TRADING_ENABLED=true`
- non-empty private key
- non-empty funder
- valid chain id
- valid signature type
- valid supported order type

The exact order-type validation can reuse the existing live-order resolution logic where possible.

### Save-Time Validation

The dashboard config API should reject malformed values the same way it already rejects invalid numeric and enum inputs.

Additional validation should ensure:

- `TRADE_MODE` is one of the supported values
- `LIVE_TRADING_ENABLED` is a valid boolean
- live-only numeric fields are parseable when supplied

Save-time validation does not need to require all live credentials when the operator is still in `paper` mode. Full credential requirements belong to live startup validation.

### Runtime Failure Handling

The live runtime must never silently downgrade into paper mode after an error.

If live mode is running and experiences transient API or order-submission failures, it may retry or skip a round according to existing logic, but it must stay semantically live mode. If it encounters a fatal startup or invariant violation, the process should surface the error and stop rather than changing operating mode behind the operator's back.

## State and Log Isolation

Paper and live runtime state must remain separate.

Recommended file usage:

- paper mode:
  - `logs/session_state.json`
  - `logs/paper_trades.csv`
- live mode:
  - `logs/live_session_state.json`
  - `logs/live_orders.csv`

This prevents:

- paper recovery state from influencing live sizing
- live pending-order settlement from interfering with paper reporting
- dashboard summaries from mixing test and real orders into one record set

## Dashboard Data and Reporting

The current paper summaries should remain available for paper history.

The dashboard should also gain live-oriented read-only status, such as:

- current process mode
- saved mode
- live config completeness or validation status
- recent live orders
- whether a pending live order is awaiting settlement or verification

Paper reporting should stay focused on `paper_trades.csv`. Live reporting should read from the live-specific files rather than reusing the paper summary path.

## Non-Goals

This change does not:

- support hot-switching an already running worker between `paper` and `live`
- silently fall back from `live` mode to `paper` mode
- redesign the underlying strategy logic
- change the dashboard host or port
- expand the project into multi-account or multi-market orchestration
- add exchange support beyond the existing Polymarket integration

## Testing Plan

Validation should cover both configuration behavior and runtime selection.

### Config and Dashboard Tests

1. `TRADE_MODE` appears in config payloads and accepts only `paper` or `live`.
2. Live config fields are exposed with the expected metadata.
3. The private key is masked in GET responses.
4. Saving unrelated fields preserves an already stored private key.
5. Invalid mode or live-field values are rejected with field errors.

### Launcher Tests

1. `main.py` starts the paper worker when `TRADE_MODE=paper`.
2. `main.py` starts the live worker when `TRADE_MODE=live` and validation passes.
3. `main.py` fails startup when `TRADE_MODE=live` but live validation fails.
4. The startup summary reports the selected mode.

### Trader Tests

1. The new continuous live runtime respects the shared stop event.
2. The live runtime uses live-specific state and log files.
3. The live runtime calls `place_live_order(...)` through safe loop boundaries instead of submitting uncontrolled repeated orders.
4. Existing pending live trade settlement behavior remains intact.

### Dashboard Status Tests

1. The dashboard distinguishes saved mode from current process mode.
2. The dashboard indicates when restart is required after a mode change.
3. Live status endpoints read from live-specific files.

## Files Expected to Change

- `config.py`: add `trade_mode` parsing and supporting validation helpers.
- `main.py`: choose paper or live worker at startup and surface mode-aware startup errors.
- `trader.py`: add the continuous live runtime entrypoint and shared live startup validation.
- `dashboard.py`: expose the new config fields, protect secrets, show saved-vs-running mode, and add live status/reporting endpoints as needed.
- `README.md` and runbooks: document the restart-based paper/live switching behavior once implementation is complete.
- tests covering launcher, dashboard config, and live runtime behavior.

## Recommended Implementation Order

1. Add config-model support for `TRADE_MODE` and live startup validation primitives.
2. Add a continuous `run_live_trading(...)` runtime path in `trader.py`.
3. Teach `main.py` to select the worker based on the saved mode.
4. Extend the dashboard config API and UI for mode switching and secret-safe live settings.
5. Add dashboard status/reporting for saved mode versus current runtime mode.
6. Update `README.md` and runbooks after runtime behavior is verified.
