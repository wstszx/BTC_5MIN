# Multi-Strategy Timeframe Preset Design

**Date:** 2026-04-22

**Status:** Drafted after user-approved design direction

## Goal

When the operator switches `MARKET_TIMEFRAME` between `5m` and `15m` in the dashboard, automatically apply the matching recommended preset values for Strategy 5, Strategy 6, Strategy 7, and shared timeframe-sensitive execution parameters.

## Problem

The dashboard already supports:

- switching the active timeframe between `5m` and `15m`
- automatically applying a limited preset for Strategy 7 and shared timing fields

That is no longer sufficient for the intended operator workflow. The user wants a full timeframe switch to bring along the appropriate configuration profile for the strategies that depend most on timeframe-specific signal behavior:

- Strategy 5
- Strategy 6
- Strategy 7

Without that, the operator can still end up in mismatched states such as:

- `5m` timeframe with `15m`-oriented Strategy 5 momentum settings
- `15m` timeframe with `5m`-oriented Strategy 6 OFI freshness settings

## Decision Summary

Extend the timeframe preset system so each timeframe exposes a structured preset made of:

- `shared`
- `strategy5`
- `strategy6`
- `strategy7`

When the user changes `MARKET_TIMEFRAME`, the frontend should merge these sections into one flat update set and immediately overwrite the corresponding form fields before save.

This keeps the UX simple while making the preset system explicit and maintainable.

## Non-Goals

- No confirmation dialog before preset application.
- No “keep custom values” mode in this change.
- No user-defined preset editor.
- No per-strategy preset buttons.
- No new backend save-time inference layer beyond the existing config save flow.

## User Experience

### Desired Behavior

When the operator changes `MARKET_TIMEFRAME`:

1. the dashboard immediately loads the corresponding timeframe preset
2. shared execution fields are updated
3. Strategy 5 fields are updated
4. Strategy 6 fields are updated
5. Strategy 7 fields are updated
6. the updated values are visible in the form before save
7. saving writes the timeframe and all auto-applied preset values into `.env.dashboard`

### No Extra Confirmation

The user explicitly wants timeframe switching to auto-apply recommended settings by default.

That means:

- switching to `5m` applies the `5m` full preset immediately
- switching to `15m` applies the `15m` full preset immediately

## Preset Structure

Each timeframe preset should be structured on the server like this:

```json
{
  "5m": {
    "shared": {
      "OPEN_DELAY_SECONDS": "12",
      "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "10"
    },
    "strategy5": {
      "SIGNAL_MOMENTUM_THRESHOLD": "0.020",
      "SIGNAL_FALLBACK_STRATEGY_ID": "2",
      "MAX_PRICE_THRESHOLD": "0.60",
      "TARGET_PROFIT": "0.8"
    },
    "strategy6": {
      "OFI_THRESHOLD": "0.72",
      "BINANCE_SIGNAL_STALE_SECONDS": "1.0",
      "TARGET_PROFIT": "0.8"
    },
    "strategy7": {
      "STRATEGY7_OFI_THRESHOLD": "0.58",
      "STRATEGY7_MOMENTUM_THRESHOLD": "0.008",
      "STRATEGY7_MAX_ENTRY_PRICE": "0.54",
      "STRATEGY7_MIN_SIGNAL_GAP": "0.015",
      "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "2",
      "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP": "0.035",
      "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS": "2"
    }
  }
}
```

The frontend should flatten the chosen timeframe’s sections into one update map before writing values into the form.

## Preset Contents

### Shared

Shared fields should contain execution and timing values that are not strategy-specific but still depend on timeframe.

Initial shared scope:

- `OPEN_DELAY_SECONDS`
- `SIGNAL_LOCK_BEFORE_ENTRY_SECONDS`

### Strategy 5

Initial Strategy 5 scope:

- `SIGNAL_MOMENTUM_THRESHOLD`
- `SIGNAL_FALLBACK_STRATEGY_ID`
- `MAX_PRICE_THRESHOLD`
- `TARGET_PROFIT`

### Strategy 6

Initial Strategy 6 scope:

- `OFI_THRESHOLD`
- `BINANCE_SIGNAL_STALE_SECONDS`
- `TARGET_PROFIT`

If a dedicated Strategy 6 entry-price field is introduced later, it can be added here without changing the overall preset structure.

### Strategy 7

Initial Strategy 7 scope:

- `STRATEGY7_OFI_THRESHOLD`
- `STRATEGY7_MOMENTUM_THRESHOLD`
- `STRATEGY7_MAX_ENTRY_PRICE`
- `STRATEGY7_MIN_SIGNAL_GAP`
- `STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS`
- `STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP`
- `STRATEGY7_LATE_CONFIRM_RELAX_SECONDS`

## Initial Recommended Values

### `5m`

#### Shared

- `OPEN_DELAY_SECONDS=12`
- `SIGNAL_LOCK_BEFORE_ENTRY_SECONDS=10`

#### Strategy 5

- `SIGNAL_MOMENTUM_THRESHOLD=0.020`
- `SIGNAL_FALLBACK_STRATEGY_ID=2`
- `MAX_PRICE_THRESHOLD=0.60`
- `TARGET_PROFIT=0.8`

#### Strategy 6

- `OFI_THRESHOLD=0.72`
- `BINANCE_SIGNAL_STALE_SECONDS=1.0`
- `TARGET_PROFIT=0.8`

#### Strategy 7

- `STRATEGY7_OFI_THRESHOLD=0.58`
- `STRATEGY7_MOMENTUM_THRESHOLD=0.008`
- `STRATEGY7_MAX_ENTRY_PRICE=0.54`
- `STRATEGY7_MIN_SIGNAL_GAP=0.015`
- `STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS=2`
- `STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP=0.035`
- `STRATEGY7_LATE_CONFIRM_RELAX_SECONDS=2`

### `15m`

#### Shared

- `OPEN_DELAY_SECONDS=25`
- `SIGNAL_LOCK_BEFORE_ENTRY_SECONDS=20`

#### Strategy 5

- `SIGNAL_MOMENTUM_THRESHOLD=0.015`
- `SIGNAL_FALLBACK_STRATEGY_ID=2`
- `MAX_PRICE_THRESHOLD=0.65`
- `TARGET_PROFIT=1.0`

#### Strategy 6

- `OFI_THRESHOLD=0.65`
- `BINANCE_SIGNAL_STALE_SECONDS=2.0`
- `TARGET_PROFIT=1.0`

#### Strategy 7

- `STRATEGY7_OFI_THRESHOLD=0.50`
- `STRATEGY7_MOMENTUM_THRESHOLD=0.005`
- `STRATEGY7_MAX_ENTRY_PRICE=0.55`
- `STRATEGY7_MIN_SIGNAL_GAP=0.01`
- `STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS=3`
- `STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP=0.03`
- `STRATEGY7_LATE_CONFIRM_RELAX_SECONDS=3`

## Architecture

### Single Source of Truth

The full preset mapping should stay on the server side, not inside the frontend JS.

Recommended placement:

- a constant mapping in `dashboard.py`

The frontend should consume the payload as metadata and should not hard-code the preset values.

### Config Payload

The dashboard config payload should include the structured preset mapping, for example:

```json
{
  "timeframe_presets": {
    "5m": {
      "shared": { "...": "..." },
      "strategy5": { "...": "..." },
      "strategy6": { "...": "..." },
      "strategy7": { "...": "..." }
    },
    "15m": {
      "shared": { "...": "..." },
      "strategy5": { "...": "..." },
      "strategy6": { "...": "..." },
      "strategy7": { "...": "..." }
    }
  }
}
```

### Frontend Behavior

Add a helper that:

1. reads the selected timeframe’s structured preset
2. merges `shared`, `strategy5`, `strategy6`, and `strategy7`
3. writes the merged values into the corresponding form fields

The merge order should be deterministic. A simple safe order is:

1. `shared`
2. `strategy5`
3. `strategy6`
4. `strategy7`

Since the intended fields do not overlap in this version, the merge order is mostly a guardrail for future changes.

### Save Semantics

No extra backend rewrite is needed after the frontend applies the preset into the form.

The existing save path should keep persisting the current form values exactly as shown to the operator.

## Why This Is Reasonable

This is a good fit for the current architecture because:

- timeframe already exists as a first-class config field
- the dashboard already sends config metadata from server to client
- save already persists explicit env values
- runtime reload already reacts to `market_timeframe`

The structured preset approach is cleaner than one large flat table because it preserves intent and keeps strategy ownership obvious.

## Risks

### Risk: Too many fields get overwritten on timeframe switch

This is intentional for this version because the user explicitly wants timeframe switching to auto-apply recommended strategy-specific settings.

Mitigation:

- limit the preset scope to fields with clear timeframe sensitivity
- keep unrelated credentials and runtime toggles out of the preset map

### Risk: Structured presets complicate the frontend

Mitigation:

- keep the frontend merge helper small
- keep the preset source server-side
- test both payload shape and JS auto-apply behavior

### Risk: Strategy-specific recommendations drift from paper-learned reality

Mitigation:

- store recommendations in one mapping
- revise the preset constants as paper-trading evidence improves

## Testing Strategy

Add or extend tests to cover:

### Backend / Payload

- payload includes `timeframe_presets`
- both `5m` and `15m` include `shared`, `strategy5`, `strategy6`, `strategy7`
- representative values match the expected recommendations

### Frontend / Assets

- JS includes helper logic to merge the structured preset sections
- JS auto-applies the selected timeframe preset on `MARKET_TIMEFRAME` change

### Save / Reload

- saving after timeframe-driven preset updates still triggers the existing `market_timeframe` reload path

## Files Expected To Change

- `dashboard.py`
- `tests/test_dashboard.py`

## Success Criteria

This change counts as successful if:

1. Switching `MARKET_TIMEFRAME` auto-applies Strategy 5, 6, 7, and shared timeframe parameters together.
2. The operator can see the updated values before saving.
3. Saving persists the updated values through the existing config flow.
4. The preset source remains server-defined and structured by strategy rather than duplicated in JS.
