# Single-Command Paper Trading Dashboard Design

**Date:** 2026-04-04

**Status:** Proposed and validated with user through iterative review

## Goal

Simplify the project so the operator only needs one command, `python main.py`, to continuously run paper trading and start the local dashboard at the same time.

## Problem

The current project exposes many CLI subcommands and multiple operating modes. That flexibility has become a usability problem for this operator:

- the normal workflow is unclear because too many commands are visible
- paper trading and the dashboard feel like separate services instead of one tool
- shutdown behavior is confusing because background dashboard processes can remain reachable after the operator thinks the program has stopped
- the README and runbooks emphasize many modes the operator does not want

The requested change is not to redesign the trading logic itself. The change is to make the runtime experience single-purpose and predictable.

## Confirmed Scope

The user confirmed the following boundaries:

1. The only public way to run the program should be `python main.py`.
2. Running that command should start continuous paper trading and the local web dashboard together.
3. The dashboard should remain on `http://127.0.0.1:8787/`.
4. `Ctrl+C` should stop both parts cleanly so no leftover dashboard process keeps serving the page.
5. Old modes can remain internally for now, but they must stop being the normal visible interface.
6. Documentation should focus on the single-command workflow instead of teaching multiple operating modes.

## Design Principles

1. Prefer one obvious path over flexibility.
2. Keep the paper-trading engine and dashboard logic separate internally, even though they launch together.
3. Make startup and shutdown symmetrical so runtime leftovers are much less likely.
4. Minimize risk by keeping non-target modules in place until a later cleanup pass.
5. Update docs to match the real operator workflow, not the full internal capability set.

## Chosen Approach

Use `main.py` as a thin runtime orchestrator.

`main.py` will stop acting like a public multi-command CLI and instead become the single launcher for:

- configuration bootstrap
- dashboard startup
- continuous paper-trading startup
- process lifecycle management
- clean coordinated shutdown

The existing `trader.py` and `dashboard.py` modules remain the primary owners of trading and HTTP behavior. The new launcher only coordinates them.

## Alternatives Considered

### Option 1: Single public command, keep old internals hidden

This is the selected approach.

Benefits:

- lowest-risk path to the requested user experience
- minimal disruption to backtest and utility modules
- easier rollback if runtime integration reveals issues

Trade-off:

- the repository still contains legacy modules internally until a later cleanup

### Option 2: Keep hidden compatibility paths in `main.py`

This would preserve internal subcommand parsing while making the default path launch both services.

Benefits:

- strong backward compatibility

Trade-off:

- the main entrypoint still carries the complexity the user wants removed

### Option 3: Delete all extra modes now

This would remove the old CLI surface and related documentation immediately.

Benefits:

- the cleanest code surface

Trade-off:

- larger edit scope and higher regression risk

## Runtime Architecture

### Entry Behavior

Running `python main.py` should:

1. Build `AppConfig`.
2. Ensure required runtime directories exist.
3. Start the dashboard service on `127.0.0.1:8787`.
4. Start the continuous paper-trading loop.
5. Print a compact startup summary to the terminal, including the dashboard URL.
6. Block in a coordinator loop until shutdown is requested or a fatal runtime error occurs.

### Component Boundaries

- `main.py`
  - owns orchestration only
  - wires together configuration, worker startup, and shutdown
- `dashboard.py`
  - continues to own HTTP serving and dashboard data reads
  - gains a callable server lifecycle path that can be shut down programmatically by the launcher
- `trader.py`
  - continues to own paper-trading logic
  - gains a stoppable runtime path so the launcher can request a clean exit

### Startup Model

The launcher should run both services concurrently in-process.

Recommended structure:

- dashboard runs in a dedicated background thread
- paper trading runs in a dedicated background thread
- the main thread manages lifecycle, error propagation, and interrupt handling

This avoids requiring the operator to open multiple shells while still keeping each runtime concern isolated.

### Configuration Source of Truth

The combined runtime must not allow the dashboard and the paper-trading loop to drift onto different effective configs.

The current dashboard already reads and writes `.env.dashboard`, while `AppConfig` normally reads process environment variables. If that behavior is left unchanged, the operator could edit parameters in the dashboard while the paper trader keeps using stale values.

The single-command runtime should therefore adopt one effective config source:

- `.env.dashboard` becomes the operator-facing runtime config file
- the launcher loads initial config from that file before starting workers
- the dashboard continues to edit that same file
- the paper-trading runtime reads from the same effective values rather than a separate environment snapshot

Recommended behavior:

1. Startup loads `.env.dashboard` if present, then builds `AppConfig` from those values.
2. Dashboard saves continue writing to `.env.dashboard`.
3. Paper trading refreshes config from the same source at a safe boundary, preferably between polling cycles or before entering the next round.
4. If a dashboard save is invalid, the existing validated config remains active and the error stays visible in the UI.

This keeps the dashboard's editable settings and the trader's actual behavior aligned in one-process operation.

## Shutdown Design

Shutdown behavior is a core requirement.

The launcher should:

1. Listen for `KeyboardInterrupt` in the main thread.
2. Signal the paper-trading loop to stop at a safe polling boundary.
3. Call dashboard shutdown explicitly instead of relying on interpreter exit.
4. Join worker threads with a bounded timeout.
5. Print a short shutdown summary and exit.

If either worker fails during startup or hits a fatal unrecoverable error, the launcher should begin the same coordinated shutdown sequence rather than leaving the other half running alone.

## Error Handling

### Dashboard Port Busy

If `127.0.0.1:8787` is already occupied, startup should fail loudly and exit. The program should not silently choose another port because the operator expects one stable URL.

### Trading API Failures

The paper-trading loop should keep its current retry and backoff behavior for transient Polymarket failures. A brief upstream issue should not kill the combined runtime.

### Invalid Runtime Config Update

If the operator saves invalid values through the dashboard, the runtime should reject the update without switching the paper trader onto a broken configuration. The last valid config should remain active until a valid save replaces it.

### Partial Startup Failure

If one component starts and the other does not, the launcher should stop the component that did start and exit with a clear error message.

### Runtime Fatal Error

If a worker encounters an unrecoverable exception, the launcher should record the error, trigger global shutdown, and avoid leaving a half-running local service behind.

## User-Facing Behavior

After this change, the operator workflow becomes:

1. Install dependencies.
2. Adjust config if needed.
3. Run `python main.py`.
4. Open `http://127.0.0.1:8787/`.
5. Press `Ctrl+C` when done.

The project should no longer teach separate primary commands for:

- paper trading
- dashboard-only mode
- backtesting
- history export
- analysis helpers

Those capabilities may still exist internally, but they are no longer part of the main operating story.

## Documentation Changes

### README

Refocus the README around one path:

1. install dependencies
2. edit configuration
3. run `python main.py`
4. open the dashboard
5. stop with `Ctrl+C`

The README should stop presenting a command catalog as the default way to understand the project.

### Local Runbooks

Existing runbooks should be tightened so the primary instructions reinforce the same one-command workflow. If older commands remain mentioned for maintenance reasons, they should be clearly de-emphasized.

## Non-Goals

This change does not:

- redesign strategy logic
- remove paper-trading logs or session state files
- replace the current dashboard UI
- delete all legacy modules immediately
- change the dashboard host or port from the expected local default
- expand live-trading support

## Testing Plan

Validation should cover:

1. `python main.py` starts without requiring a subcommand.
2. The dashboard becomes reachable at `http://127.0.0.1:8787/`.
3. Paper trading begins running automatically.
4. `Ctrl+C` stops both the dashboard and the trading loop.
5. No stale local dashboard process remains after shutdown.
6. README and local usage docs describe only the supported single-command workflow as the primary path.

## Implementation Notes

- Keep orchestration code small and focused.
- Reuse the existing `AppConfig`, dashboard state, session-state file, and paper-trade CSV flow.
- Prefer adding explicit stop hooks over relying on daemon-thread termination.
- Treat `.env.dashboard` as the shared runtime config source for the combined launcher path.
- Avoid deleting older tools until the single-command runtime is verified stable.

## Recommended Implementation Order

1. Replace the public `main.py` CLI surface with a single launcher path.
2. Add explicit dashboard server lifecycle support.
3. Add stoppable paper-trading loop support.
4. Wire coordinated startup and shutdown through the launcher.
5. Update README and local runbooks to match the new operating model.
6. Verify startup, page access, and clean shutdown behavior end to end.
