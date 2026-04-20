# Strategy 7 Efficiency Tuning Design

**Date:** 2026-04-20

**Status:** Approved by user for implementation

## Goal

Improve strategy 7 order efficiency without materially weakening its win-quality bias.

## Problem

Recent paper logs show strategy 7 is skipping too many rounds for two dominant reasons:

- `strategy7_ofi_too_weak`
- `strategy7_entry_too_late`

The user does not want an aggressive frequency increase. The preferred outcome is modestly higher trade count while preserving the existing conservative quality profile.

## Decision Summary

Tune strategy 7 by relaxing only the confirmation-timing gate, and only for clearly stronger-than-threshold consensus signals.

Keep all of these filters unchanged:

- OFI threshold gate
- momentum threshold gate
- OFI and momentum directional agreement
- strategy-7 price ceiling
- minimum signal gap / confidence gate

## Chosen Approach

Add a strategy-7-specific fast path for late confirmations:

1. Keep the current base `STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS` behavior for ordinary signals.
2. Detect when both OFI and momentum exceed their thresholds by an additional configurable margin.
3. For those stronger signals only, reduce the effective confirmation requirement by a configurable number of seconds.
4. Never reduce the requirement below zero or below the already clamped window availability.

This approach targets the largest actionable skip bucket without broadly lowering signal quality.

## Config Additions

Add two new strategy-7-only settings:

- `STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP`
- `STRATEGY7_LATE_CONFIRM_RELAX_SECONDS`

### Semantics

- `STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP`
  Additional strength required above both the OFI threshold and the momentum threshold before the late-confirmation fast path is allowed.

- `STRATEGY7_LATE_CONFIRM_RELAX_SECONDS`
  Number of seconds to subtract from the effective confirmation requirement when the strong-signal condition is met.

### Default Behavior

Defaults must preserve current behavior until explicitly tuned:

- default strong-signal gap: conservative positive value
- default relax seconds: `0`

That makes the change backward compatible and safe for staged rollout.

## Runtime Behavior

In strategy 7 decision resolution:

1. Compute the normal effective confirmation window.
2. Evaluate whether the current OFI score and momentum delta both clear the new strong-signal gap.
3. If yes, reduce the confirmation requirement by the configured relax value.
4. Use the reduced requirement only for the timing gate.

All other skip reasons remain unchanged.

## Testing Strategy

Add focused runtime tests that cover:

- ordinary signals still skip when confirmation is too late
- strong signals can pass with the same late timing when relaxation is configured
- zero relaxation preserves prior behavior

## Risks

- If relaxation is too large, strategy 7 may start admitting low-quality last-second entries.
- If defaults are not neutral, the behavior change could be broader than intended.

## Non-Goals

- No changes to strategy-7 price filtering
- No changes to OFI or momentum thresholds in this pass
- No optimizer search-space expansion in this pass
