# Near-Entry Fast Polling Design

**Date:** 2026-04-22

**Status:** Drafted after user-approved design direction

## Goal

Reduce avoidable `entry_window_missed` skips by making the paper-trading runtime poll more frequently only when a target round is close to its configured entry time.

## Problem

Recent paper logs show that `entry_window_missed` is not isolated to strategy 7. Multiple strategies miss the same `OPEN` entry window in the same rounds, which points to a runtime timing issue rather than a strategy-specific signal problem.

The current runtime uses a single fixed `poll_interval_seconds` cadence. That is simple and efficient during most of a round, but it is too coarse near the configured entry boundary when the runtime needs to:

- detect the active round
- refresh quote data
- resolve the side
- pass trade guards
- queue or place the trade before the entry grace window closes

This is especially visible in `OPEN` mode, where the system targets `start_time + open_delay_seconds` and may arrive several seconds late.

## Decision Summary

Add a small near-entry polling mode to the paper runtime.

- Keep the existing base polling cadence for normal operation.
- When the current target round is within a short configurable window before entry, temporarily use a shorter sleep interval.
- After the entry boundary passes or the round is processed, return to the normal cadence.

This directly addresses runtime timing misses without changing strategy logic, thresholds, or trade-quality gates.

## Non-Goals

- No changes to strategy 7 thresholds or filtering logic.
- No strategy-specific `OPEN_DELAY` override in this change.
- No changes to `entry_window_missed` semantics.
- No broad scheduler or concurrency rewrite.
- No changes to live-order behavior unless the same helper is already shared and safe to reuse.

## Proposed Behavior

### Base Runtime

The runtime continues to use `poll_interval_seconds` as its default sleep interval.

### Near-Entry Window

For the selected target round, compute the configured entry time using the existing entry-timing rules.

If the remaining time until entry is less than or equal to a short near-entry window, switch to a shorter poll interval.

Recommended initial values:

- near-entry window: `10` seconds
- fast poll interval: `1` second

### Exit Conditions

The runtime returns to the normal poll interval when:

- the round has been processed
- the entry window has passed
- there is no active target round
- the current time is outside the near-entry window

## Configuration Design

Add two new runtime-facing config keys:

- `NEAR_ENTRY_POLL_WINDOW_SECONDS`
- `FAST_POLL_INTERVAL_SECONDS`

### Semantics

- `NEAR_ENTRY_POLL_WINDOW_SECONDS`
  The maximum remaining time before entry at which the runtime should switch from normal polling to fast polling.

- `FAST_POLL_INTERVAL_SECONDS`
  The temporary sleep interval used while the runtime is inside the near-entry window.

### Defaults

Defaults should be conservative and safe:

- `NEAR_ENTRY_POLL_WINDOW_SECONDS=10`
- `FAST_POLL_INTERVAL_SECONDS=1`

These values are large enough to improve timing reliability but small enough to avoid turning the entire runtime into a constant high-frequency loop.

## Runtime Design

### Sleep Selection

Introduce a small helper in `trader.py` that decides which poll interval to use based on:

- current time
- target round
- configured entry time
- base poll interval
- near-entry window
- fast poll interval

The helper should:

- return the base poll interval when no target round exists
- return the base poll interval when the round is not close to entry
- return the fast poll interval only when the runtime is within the near-entry window and entry has not yet been missed
- clamp all intervals to non-negative values

### Integration Points

Use this helper only in places where the paper runtime currently sleeps between polling cycles.

Do not change the trade-decision path itself. The only behavior change in this task is how long the runtime waits before the next cycle.

## Why This Is Reasonable

This change is justified by the observed evidence:

- `entry_window_missed` appears across multiple strategies in the same rounds
- the failure is aligned with runtime timing around the entry boundary
- a finer poll cadence near entry directly targets that failure mode

This is lower risk than strategy-specific entry-time overrides because it preserves the current round-selection and entry-time architecture.

## Risks

### Risk: More frequent API activity near entry

Mitigation:

- fast polling only applies inside a short window
- normal polling remains unchanged for the rest of the round

### Risk: No meaningful improvement if the real bottleneck is inside the per-cycle work

Mitigation:

- add tests for interval selection
- validate future logs to confirm whether missed entries shrink
- defer deeper runtime restructuring until evidence shows fast polling is insufficient

### Risk: Fast polling values become too aggressive

Mitigation:

- expose both values as config
- keep defaults conservative

## Testing Strategy

Add focused tests to cover:

- normal polling remains unchanged outside the near-entry window
- fast polling activates inside the near-entry window
- fast polling does not activate after the entry window is already missed
- no target round falls back to the base poll interval

Where possible, test the helper directly instead of relying only on large end-to-end runtime tests.

## Files Expected To Change

- `config.py`
- `trader.py`
- `tests/test_trader_runtime_and_live.py`

## Success Criteria

This change counts as successful if:

1. The runtime uses the normal poll interval during most of the round.
2. The runtime switches to the faster interval only near entry.
3. Existing strategy logic remains unchanged.
4. Future paper logs show fewer shared `entry_window_missed` events for multi-strategy runs.
