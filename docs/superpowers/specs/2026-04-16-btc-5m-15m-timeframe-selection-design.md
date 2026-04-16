# BTC 5m/15m Timeframe Selection Design

## Summary

Extend the current BTC prediction runtime so operators can choose between Polymarket's BTC 5-minute and BTC 15-minute "Up or Down" markets from the dashboard.

The current codebase is hard-wired to the BTC 5-minute series through fixed config defaults, slug filters, and UI copy. This design introduces a single explicit market-timeframe setting while keeping the existing trading, risk, paper/live mode, and settlement flows intact.

## Goals

- Support BTC 5-minute and BTC 15-minute Polymarket rounds from the same runtime.
- Let operators choose the active timeframe from the dashboard config form.
- Keep one shared runtime path for paper trading, live trading, dashboard display, and history export.
- Preserve backward compatibility for existing `.env.dashboard` files that do not yet contain the new setting.
- Avoid speculative support for unsupported frequencies such as 10 minutes.

## Non-Goals

- No 10-minute option in the UI or config.
- No automatic discovery of arbitrary BTC timeframe series at runtime.
- No strategy redesign based on timeframe-specific behavior.
- No new state migration tool or separate log/state files per timeframe.
- No changes to live safety rules beyond applying them to the selected timeframe.

## Verified Market Definitions

The supported timeframes are defined explicitly in code:

- `5m`
  - `series_id = 10684`
  - `series_slug = btc-up-or-down-5m`
- `15m`
  - `series_id = 10192`
  - `series_slug = btc-up-or-down-15m`

Observed slug compatibility requirements:

- 5-minute events may appear as `btc-updown-5m-*`
- 15-minute events may appear as `btc-up-or-down-15m-*`
- 15-minute events have also historically appeared as `btc-updown-15m-*`

The runtime should therefore treat `seriesSlug` as the primary event-matching signal and keep slug-prefix compatibility only as a fallback.

## User Experience

Operators will continue using the existing single dashboard and runtime flow, but the config form gains a new market-timeframe selector with exactly two options:

- `5 分钟`
- `15 分钟`

When the setting changes and is saved:

- the value is written to `.env.dashboard`
- the runtime target configuration updates to the selected timeframe
- all current-round, next-round, countdown, quote, signal, and trade-plan displays begin referencing the chosen market family once the runtime reaches a safe boundary

The dashboard must not display any 10-minute choice, placeholder, or hidden setting.

## Configuration Design

### New Config Key

Add a new env/config key:

- `MARKET_TIMEFRAME`

Allowed values:

- `5m`
- `15m`

Default:

- `5m`

### `AppConfig` Changes

`AppConfig` should stop treating `series_id` and `series_slug` as hard-coded BTC 5-minute constants. Instead, it should expose them from a small explicit timeframe-definition mapping derived from `MARKET_TIMEFRAME`.

Recommended shape:

- keep `market_timeframe` as the stored config field
- derive `series_id`
- derive `series_slug`
- optionally derive compatible slug prefixes if a helper is useful for API filtering

This keeps the operator-facing setting simple while minimizing invasive changes to the rest of the codebase, since downstream consumers can continue reading `cfg.series_id` and `cfg.series_slug`.

## Backend Design

### Market Definition Mapping

Centralize supported timeframe metadata in `config.py` so all runtime surfaces use the same source of truth. The mapping should include:

- timeframe key
- series id
- series slug
- supported fallback slug prefixes
- operator-facing label if useful for UI serialization

### Event Filtering

`polymarket_api.py` currently assumes BTC 5-minute events. Replace that assumption with logic that:

1. queries by `cfg.series_id`
2. accepts events whose `seriesSlug` matches `cfg.series_slug`
3. falls back to compatible slug-prefix matching only when necessary

This preserves resilience against historical slug variation without making the runtime depend on title parsing.

### Runtime Behavior

No trading-state-machine redesign is needed. Existing flows in `trader.py` continue to work against whatever `MarketWindow` the selected timeframe yields:

- current round discovery
- next round discovery
- entry countdowns
- quote lookup
- paper trade queuing and settlement
- live trade placement
- redeem flow
- history export

The selected timeframe changes which Polymarket rounds are targeted, but not how the bot evaluates a round once discovered.

## Dashboard Design

### Config Form

Add `MARKET_TIMEFRAME` to the editable config payload in `dashboard.py` with:

- label for operators
- select options limited to `5m` and `15m`
- validation that rejects any other value
- persistence back to `.env.dashboard`

### Dynamic Copy

The dashboard should stop presenting itself as permanently "BTC 5分钟". Copy that references the active market family should be generated from the selected timeframe, including:

- page title
- top brand/subtitle where relevant
- market panel descriptive text

The copy should remain concise and operator-focused. This is a wording update, not a visual redesign.

### Safe Runtime Switching

Changing timeframe should follow the same runtime target-update pattern already used for saved config changes:

- save updates the desired configuration
- active runtime only pivots when it reaches a safe round boundary
- no mid-round forced switch

This avoids mixing 5-minute and 15-minute round handling inside one active cycle.

## State and Compatibility

### Existing `.env.dashboard`

Existing configs remain valid because missing `MARKET_TIMEFRAME` falls back to `5m`.

### Runtime State and Logs

Do not introduce new state files or per-timeframe directories in this change.

Existing runtime history remains usable because:

- rows already contain `event_slug`
- the selected timeframe determines which new rounds are discovered
- recent-trade views can continue interpreting old and new rows from stored event identifiers

### Backward Compatibility Principle

Prefer additive changes:

- new config key with default
- derived series metadata
- expanded UI options

Avoid broad renames such as changing repository/module names away from `BTC_5MIN` in this task. Cosmetic repository-wide renaming is outside scope.

## Testing Strategy

Add or extend tests in the existing suites to cover the new behavior.

### Config Tests

Verify that:

- default config uses `MARKET_TIMEFRAME=5m`
- `MARKET_TIMEFRAME=15m` produces `series_id=10192`
- `MARKET_TIMEFRAME=15m` produces `series_slug=btc-up-or-down-15m`
- invalid values normalize or reject in a deterministic way

### API Tests

Verify that Polymarket event filtering:

- still accepts BTC 5-minute events
- accepts BTC 15-minute events by `seriesSlug`
- accepts compatible 15-minute fallback slug forms when needed
- does not leak unsupported timeframes into the filtered set

### Dashboard Tests

Verify that:

- config payload includes `MARKET_TIMEFRAME`
- UI select options contain only `5m` and `15m`
- labels render the operator-facing timeframe field correctly
- 10-minute options never appear
- dashboard display text reflects the selected timeframe

### Runtime Tests

Verify that:

- current/next round lookup works under `15m`
- runtime snapshots and trade-plan generation use the selected timeframe's market windows
- switching the saved timeframe updates the runtime target safely rather than forcing a mid-round pivot

## Risks and Mitigations

### Risk: Historical slug inconsistency for 15m

Mitigation:

- use `seriesSlug` as primary matcher
- keep explicit slug-prefix compatibility for known historical variants

### Risk: Mid-round configuration change causing mixed market state

Mitigation:

- reuse existing safe-boundary runtime-switch behavior

### Risk: UI still implies fixed 5-minute operation

Mitigation:

- update key copy surfaces to render the active timeframe dynamically

## Files Expected To Change

- `config.py`
- `polymarket_api.py`
- `dashboard.py`
- `tests/test_config_encoding.py`
- `tests/test_polymarket_api.py`
- `tests/test_dashboard.py`
- `tests/test_trader_runtime_and_live.py`

Additional test files may be updated if coverage fits better elsewhere, but the write scope should remain focused on timeframe selection.

## Implementation Outline

1. Add the new timeframe config key and central timeframe-definition mapping.
2. Refactor config accessors so downstream code still reads derived `series_id` and `series_slug`.
3. Update Polymarket event filtering to use timeframe-aware matching.
4. Add dashboard config support and dynamic timeframe copy.
5. Extend tests for config, API, dashboard, and runtime switching behavior.
6. Verify existing 5-minute behavior remains intact while 15-minute selection works.
