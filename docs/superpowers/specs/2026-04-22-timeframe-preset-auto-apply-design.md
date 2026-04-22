# Timeframe Preset Auto-Apply Design

**Date:** 2026-04-22

**Status:** Drafted after user-approved design direction

## Goal

When the operator switches `MARKET_TIMEFRAME` between `5m` and `15m` in the dashboard, automatically apply the corresponding recommended parameter preset to the relevant strategy and timing fields in the config form.

## Problem

The runtime already supports `5m` and `15m` markets, and the dashboard already exposes `MARKET_TIMEFRAME` as a selectable config field. However, the operator must currently remember and manually re-enter the matching parameter set after changing the timeframe.

That creates three operational problems:

- easy mismatch between timeframe and parameter profile
- slow switching during paper-trading experiments
- higher risk of running `5m` with `15m`-oriented settings or vice versa

The user explicitly wants timeframe changes in the frontend to automatically apply the recommended parameter configuration for that timeframe.

## Decision Summary

Add timeframe preset metadata to the dashboard config payload and have the frontend automatically overwrite a bounded set of timeframe-sensitive fields whenever `MARKET_TIMEFRAME` changes.

This is a frontend-visible auto-apply flow backed by a server-defined preset table so the recommended values come from one authoritative source.

## Non-Goals

- No confirmation dialog before applying presets.
- No “keep my custom values” mode in this change.
- No arbitrary user-defined preset editor.
- No change to live/runtime switching semantics beyond the existing `market_timeframe` reload behavior.
- No expansion beyond the supported `5m` and `15m` timeframes.

## User Experience

### Desired Behavior

When the operator changes the timeframe selector:

1. the form immediately updates the relevant parameter fields to the recommended values for that timeframe
2. the updated values are visible before save
3. save writes the changed timeframe and the auto-applied preset values into `.env.dashboard`
4. existing runtime reload behavior for `market_timeframe` continues to apply after save

### No Extra Confirmation

The user explicitly prefers the auto-apply behavior to happen by default without an extra confirmation step.

That means the form should behave deterministically:

- switching to `5m` always applies the `5m` recommended preset
- switching to `15m` always applies the `15m` recommended preset

## Preset Scope

Only timeframe-sensitive parameters should be auto-applied.

Recommended scope for this change:

- `OPEN_DELAY_SECONDS`
- `SIGNAL_LOCK_BEFORE_ENTRY_SECONDS`
- `STRATEGY7_OFI_THRESHOLD`
- `STRATEGY7_MOMENTUM_THRESHOLD`
- `STRATEGY7_MAX_ENTRY_PRICE`
- `STRATEGY7_MIN_SIGNAL_GAP`
- `STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS`
- `STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP`
- `STRATEGY7_LATE_CONFIRM_RELAX_SECONDS`

These are the values most directly tied to timeframe-specific entry timing and strategy 7 signal quality.

Do not auto-apply unrelated fields such as:

- API keys
- trade mode
- bankroll/sizing settings
- runtime credentials
- unrelated strategy defaults

## Preset Definitions

### `5m`

Use the existing documented Strategy 7 5-minute recommended profile as the preset source:

- `OPEN_DELAY_SECONDS=12`
- `SIGNAL_LOCK_BEFORE_ENTRY_SECONDS=10`
- `STRATEGY7_OFI_THRESHOLD=0.58`
- `STRATEGY7_MOMENTUM_THRESHOLD=0.008`
- `STRATEGY7_MAX_ENTRY_PRICE=0.54`
- `STRATEGY7_MIN_SIGNAL_GAP=0.015`
- `STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS=2`
- `STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP=0.035`
- `STRATEGY7_LATE_CONFIRM_RELAX_SECONDS=2`

### `15m`

Use the current recommended 15-minute operating profile as the preset source:

- `OPEN_DELAY_SECONDS=25`
- `SIGNAL_LOCK_BEFORE_ENTRY_SECONDS=20`
- `STRATEGY7_OFI_THRESHOLD=0.50`
- `STRATEGY7_MOMENTUM_THRESHOLD=0.005`
- `STRATEGY7_MAX_ENTRY_PRICE=0.55`
- `STRATEGY7_MIN_SIGNAL_GAP=0.01`
- `STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS=3`
- `STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP=0.03`
- `STRATEGY7_LATE_CONFIRM_RELAX_SECONDS=3`

## Architecture

### Single Source of Truth

The timeframe preset table should live on the server side rather than being duplicated inside dashboard JavaScript.

Recommended placement:

- a small constant mapping in `dashboard.py`, or
- a reusable mapping in `config.py` if that produces cleaner ownership

The important rule is:

- dashboard JS reads preset data from the config payload
- JS does not hard-code separate copies of the preset values

### Config Payload

Extend the dashboard config payload to include a `timeframe_presets` object shaped roughly like:

```json
{
  "5m": {
    "OPEN_DELAY_SECONDS": "12",
    "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "10",
    "STRATEGY7_OFI_THRESHOLD": "0.58"
  },
  "15m": {
    "OPEN_DELAY_SECONDS": "25",
    "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "20",
    "STRATEGY7_OFI_THRESHOLD": "0.50"
  }
}
```

String values are acceptable because the existing config form already treats form inputs as serialized env values.

### Frontend Behavior

Add a small handler in the existing dashboard JS form logic:

- detect `MARKET_TIMEFRAME` input changes
- look up the matching preset from `payload.timeframe_presets`
- overwrite the relevant form fields immediately
- leave non-preset fields untouched

This should update the visible form controls before the user clicks save.

### Save Semantics

No special backend merge rule is needed beyond the existing save flow.

Once the frontend has overwritten the form values, the existing save path can persist them as ordinary config values.

## Why This Is Reasonable

This is a good fit for the current architecture because:

- timeframe already exists as a first-class config field
- the dashboard already serializes config metadata from server to client
- saving already persists a complete env-value map
- runtime already knows how to reload on `market_timeframe` change

This avoids introducing another hidden rule at save time and lets the operator see exactly what values will be saved.

## Risks

### Risk: Auto-apply overwrites manual tuning

This is intentional for the first version because the user explicitly wants automatic preset application on timeframe switch.

Mitigation:

- keep the preset scope bounded to timeframe-sensitive fields
- make the behavior deterministic and visible in the form before save

### Risk: Preset values drift from docs or runtime recommendations

Mitigation:

- keep one authoritative server-side preset mapping
- align it with the documented 5m and 15m recommendations

### Risk: Hidden client-side logic becomes hard to debug

Mitigation:

- keep the frontend logic small
- load the presets from payload rather than duplicating values in JS
- add dashboard tests that verify the payload and the auto-apply behavior

## Testing Strategy

Add or extend tests to cover:

### Backend / Payload

- config payload includes `timeframe_presets`
- payload includes both `5m` and `15m`
- preset values match the expected recommended values

### Frontend

- changing `MARKET_TIMEFRAME` to `5m` updates the targeted fields to the `5m` preset
- changing `MARKET_TIMEFRAME` to `15m` updates the targeted fields to the `15m` preset
- unrelated fields remain unchanged

### Existing Runtime Integration

- saving after a timeframe change still triggers the existing `market_timeframe` reload behavior

## Files Expected To Change

- `dashboard.py`
- `tests/test_dashboard.py`

Additional files may be updated only if the preset table is extracted into shared config metadata, but the write scope should stay narrow.

## Success Criteria

This change counts as successful if:

1. The frontend automatically applies the matching preset when the timeframe selector changes.
2. The operator can see the preset values before saving.
3. Save persists the updated values through the existing config path.
4. `5m` and `15m` preset values are sourced from one authoritative mapping rather than duplicated in JS.
