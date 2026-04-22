# Paper Multi-Timeframe Runtime Design

**Date:** 2026-04-22

**Status:** Drafted after interactive design review

## Goal

Allow `paper` mode to run multiple BTC market timeframes in parallel, with each timeframe owning its own paper strategy list and its own paper parameter set, while `live` mode continues to run exactly one timeframe through the existing `MARKET_TIMEFRAME` flow.

The immediate target is parallel `5m` and `15m` paper trading, but the design should use a selectable list so the runtime model stays extensible instead of hard-coding a single pair.

## User-Approved Scope

The user explicitly wants:

- multi-timeframe paper trading only
- `live` to remain single-timeframe
- dashboard presentation as separate runtime cards such as `Paper 5m` and `Paper 15m`
- each timeframe to have its own paper strategy list
- each timeframe to have its own paper parameter set, not just its own strategy selection

## Problem

The current repository supports:

- a single active `MARKET_TIMEFRAME` in `AppConfig`
- a single paper runtime worker
- multi-strategy paper execution within that single timeframe
- timeframe-aware dashboard copy and presets for the single config form

That is not enough for the desired workflow. If the operator wants to evaluate both `5m` and `15m` at the same time, the current system forces one of these bad options:

- manually switching the whole runtime between timeframes
- running one timeframe and ignoring the other
- trying to share one parameter set across two different market rhythms

The last point is especially harmful because the user wants `5m` and `15m` to keep different paper strategy lists and different paper thresholds.

## Decision Summary

Introduce a paper-only multi-timeframe runtime model with these rules:

1. `live` remains single-timeframe and continues to use top-level `MARKET_TIMEFRAME`.
2. `paper` can enable one or more timeframes through a new list-style config field.
3. Each enabled paper timeframe gets its own paper profile containing:
   - strategy list
   - focused strategy for dashboard inspection
   - threshold/risk/execution parameters
4. Runtime execution is split by timeframe:
   - one supervisor for paper mode
   - one independent paper worker per enabled timeframe
5. Persistence is split by timeframe:
   - separate session state file
   - separate paper trades CSV
6. Dashboard runtime display is split into independent cards:
   - `Paper 5m`
   - `Paper 15m`
7. Existing single-timeframe paper configuration remains supported as a compatibility path when the new paper-timeframe list is absent.

## Recommended Architecture

### Chosen Direction

Use a paper supervisor that launches one paper worker per enabled timeframe.

Each worker should still operate as a mostly normal single-timeframe paper runtime. The major change is that the worker receives a timeframe-specific `AppConfig` view plus timeframe-specific state/log paths.

### Why This Direction

This keeps the new feature aligned with the existing structure:

- current `run_paper_trading` already assumes one cfg equals one timeframe
- current dashboard querying code also assumes one config view per timeframe
- per-timeframe isolation avoids cross-contaminating pending settlement queues, recent trades, and summary metrics

This is intentionally different from turning one paper loop into a giant “all timeframes in one state machine” function. That alternative would entangle:

- state management
- runtime switching
- dashboard payload logic
- CSV log filtering

and would make future maintenance much harder.

## Non-Goals

- No multi-timeframe `live` runtime.
- No support for arbitrary new market durations beyond the existing supported timeframe definitions in this change.
- No attempt to unify `5m` and `15m` into one merged paper summary file at write time.
- No breaking migration that requires users to rewrite their entire `.env.dashboard` before the feature can run.
- No redesign of strategy logic itself; this feature changes orchestration and configuration boundaries, not strategy math.

## Configuration Design

### Top-Level Runtime Semantics

Keep the current top-level single-timeframe semantics for `live`:

- `MARKET_TIMEFRAME` remains the active live timeframe
- top-level live credentials and live runtime controls remain unchanged

Add paper-only multi-timeframe controls:

- `PAPER_TIMEFRAMES=5m,15m`

If `PAPER_TIMEFRAMES` is present and non-empty, paper mode runs the listed timeframes in parallel.

If `PAPER_TIMEFRAMES` is absent, paper mode remains backward-compatible and uses the old single-timeframe interpretation driven by existing fields.

### Per-Timeframe Paper Profile Keys

Each paper timeframe gets its own prefixed config namespace. For example:

- `PAPER_5M_STRATEGY_ID`
- `PAPER_5M_STRATEGY_IDS`
- `PAPER_5M_TARGET_PROFIT`
- `PAPER_5M_OPEN_DELAY_SECONDS`
- `PAPER_5M_SIGNAL_MOMENTUM_THRESHOLD`
- `PAPER_5M_OFI_THRESHOLD`
- `PAPER_5M_STRATEGY7_OFI_THRESHOLD`

and the same pattern for `15m`:

- `PAPER_15M_STRATEGY_ID`
- `PAPER_15M_STRATEGY_IDS`
- `PAPER_15M_TARGET_PROFIT`
- `PAPER_15M_OPEN_DELAY_SECONDS`
- `PAPER_15M_SIGNAL_MOMENTUM_THRESHOLD`
- `PAPER_15M_OFI_THRESHOLD`
- `PAPER_15M_STRATEGY7_OFI_THRESHOLD`

The profile should include all paper-trading parameters that the user expects to vary by timeframe, including:

- strategy selection
- target profit
- base order cost
- max stake and loss reset settings
- timeframe-sensitive strategy thresholds
- entry timing parameters
- signal freshness / staleness thresholds
- strategy 7 confirmation parameters

### Focus Strategy vs Strategy List

Each paper timeframe profile keeps both:

- a focused strategy id for dashboard inspection
- a paper strategy list for concurrent paper execution

This preserves the existing UI concept where one strategy is the currently inspected strategy while multiple strategies can still run.

### Config Parsing Model

The config layer should expose:

- the existing top-level `AppConfig` for shared and live behavior
- a derived paper profile map keyed by timeframe

Conceptually:

```python
paper_profiles = {
    "5m": PaperTimeframeProfile(...),
    "15m": PaperTimeframeProfile(...),
}
```

Each profile should be convertible into a timeframe-specific `AppConfig`-like runtime view so the existing trading logic can keep consuming familiar fields.

## Runtime Architecture

### Paper Supervisor

When `TRADE_MODE=paper`:

- load the shared config
- resolve the enabled paper timeframes
- derive one paper runtime config per timeframe
- start one worker thread per enabled paper timeframe

Each worker should use:

- its own config view
- its own Polymarket client bound to its timeframe
- its own session state file
- its own paper trade log path

The supervisor is responsible for:

- launching all paper workers
- collecting worker failures
- stopping all workers together on shutdown
- coordinating paper-only config reloads

### Live Runtime

When `TRADE_MODE=live`, the current behavior remains:

- exactly one live trading worker
- optional live redeem worker
- single `MARKET_TIMEFRAME`

This boundary is important. The new paper functionality must not force `RuntimeControl` or live switching semantics to pretend that live also manages multiple timeframes.

### Reload Behavior

Paper config reload should become profile-aware:

- changing enabled paper timeframes should trigger a paper runtime restart
- changing a profile field for `5m` should restart only the paper supervisor flow, which then recreates the `5m` and `15m` workers from fresh config

This can still be implemented with a single paper-runtime reload request at first. Fine-grained hot-swapping one worker without restarting its siblings is not required for the first version.

## Persistence Design

### Per-Timeframe State Paths

Store paper runtime files under timeframe-specific directories:

- `logs/paper/5m/session_state.json`
- `logs/paper/5m/paper_trades.csv`
- `logs/paper/15m/session_state.json`
- `logs/paper/15m/paper_trades.csv`

This avoids mixing:

- round queues
- pending settlement
- per-strategy paper state
- CSV summaries

between unrelated market durations.

### Why Not One Shared CSV

A shared paper CSV would force every reader to filter by timeframe and would make operational review noisier. Since the runtime already has natural per-timeframe boundaries, the write path should stay simple and isolated.

If a combined overview is needed, the dashboard can aggregate reads across per-timeframe CSV files instead of forcing the writers to produce a merged file.

### Legacy Compatibility

Existing legacy single-timeframe paper files can continue to exist:

- `logs/session_state.json`
- `logs/paper_trades.csv`

Compatibility mode uses them only when `PAPER_TIMEFRAMES` is absent.

Once the operator saves the new multi-timeframe paper config through the dashboard, the runtime should start using the new per-timeframe paths.

No automatic migration of old paper history into the new directories is required in this change.

## Dashboard Design

### Configuration UI

The current single config form should evolve into:

1. shared runtime section
   - trade mode
   - live-only controls
   - any truly shared infrastructure settings
2. paper multi-timeframe section
   - enabled paper timeframes selector
3. one paper profile editor per enabled timeframe
   - `Paper 5m`
   - `Paper 15m`

Each paper profile editor includes:

- focused strategy
- paper strategy list
- parameter fields for that timeframe
- timeframe-specific preset application

### Runtime Display

The dashboard runtime area should render one independent paper card per enabled timeframe.

Example:

- `Paper 5m`
- `Paper 15m`

Each card should show its own:

- current/next round
- quote/signal/plan snapshot
- strategy selector
- pending settlement state
- recent trades
- summary metrics
- runtime health

This follows the user’s explicit preference for separate visual runtimes instead of one merged paper block with mixed internals.

### API Shape

Keep the internal service model single-timeframe per request even though the page is multi-timeframe overall.

Recommended API direction:

- config payload adds `paper_profiles`
- market endpoint accepts `timeframe`
- paper summary endpoint accepts `timeframe`
- recent paper trades endpoint accepts `timeframe`
- add a paper runtime overview payload returning lightweight card summaries for all enabled paper timeframes

This lets the frontend render multiple cards while the backend handlers remain straightforward and focused.

## Timeframe Presets

The recently added single-form timeframe preset system should evolve into profile-aware presets.

Instead of one global `MARKET_TIMEFRAME` preset application controlling one set of form fields, the dashboard should apply presets inside each paper timeframe profile editor.

Example:

- `Paper 5m` profile loads `5m` preset defaults
- `Paper 15m` profile loads `15m` preset defaults

Preset application should remain deterministic and server-defined, but it must now target the correct profile namespace instead of overwriting one shared form.

## Backward Compatibility Rules

### Old Mode

If `PAPER_TIMEFRAMES` is missing:

- paper runtime behaves exactly like the old single-timeframe flow
- top-level `MARKET_TIMEFRAME`
- top-level `PAPER_STRATEGY_IDS`
- top-level paper parameter fields

remain valid for paper mode.

### New Mode

If `PAPER_TIMEFRAMES` is present:

- paper runtime uses the new per-timeframe profiles
- top-level `MARKET_TIMEFRAME` is no longer the source of paper timeframe selection
- top-level legacy paper fields become compatibility-only fallbacks, not the primary config model

### Fallback Rules

For a profile field in new mode:

1. use explicit `PAPER_<TF>_*` value if present
2. otherwise fall back to the matching timeframe preset default if defined
3. otherwise fall back to the old top-level value for compatibility
4. otherwise use the existing code default

This minimizes upgrade friction while still allowing the dashboard to progressively write a complete per-timeframe config set.

## Testing Strategy

### Config Tests

Add tests proving that:

- `PAPER_TIMEFRAMES=5m,15m` parses correctly
- each timeframe profile resolves its own strategy list and parameters
- legacy single-timeframe paper config still parses
- live config still reads the top-level single `MARKET_TIMEFRAME`

### Runtime Launcher Tests

Add tests proving that:

- paper mode starts one worker per enabled timeframe
- `5m` and `15m` workers run together
- live mode still starts only one live worker
- paper reload recreates the enabled paper workers from fresh config

### Paper Runtime Tests

Add tests proving that:

- per-timeframe state paths are isolated
- per-timeframe CSV logs are isolated
- pending settlement in `5m` does not block `15m`
- different strategy lists can run for `5m` and `15m`
- different thresholds can apply to `5m` and `15m`

### Dashboard Tests

Add tests proving that:

- config payload includes `paper_profiles`
- enabled paper timeframes are exposed to the frontend
- market/summary/recent payloads accept timeframe selection
- overview payload returns one card per enabled paper timeframe
- frontend assets include per-profile rendering and per-card refresh logic

## Risks And Mitigations

### Risk: Shared runtime assumptions leak into multi-timeframe paper

Mitigation:

- keep each request and worker bound to one timeframe at a time
- isolate persistence by timeframe
- avoid reusing one mutable cfg object across timeframes

### Risk: Dashboard form becomes confusing

Mitigation:

- visually separate shared runtime settings from paper profile settings
- label each profile clearly as `Paper 5m` or `Paper 15m`
- only show enabled paper timeframe editors

### Risk: Live mode accidentally inherits paper multi-timeframe semantics

Mitigation:

- keep live selection on `MARKET_TIMEFRAME`
- keep live worker orchestration unchanged
- do not overload `RuntimeControl` with fake live multi-timeframe state

### Risk: Backward compatibility becomes ambiguous

Mitigation:

- explicit mode gate on presence of `PAPER_TIMEFRAMES`
- documented fallback order
- tests covering old and new config styles

## Implementation Outline

The implementation should proceed in this order:

1. config parsing and profile model
2. paper supervisor and per-timeframe worker startup
3. per-timeframe state/log path plumbing
4. dashboard backend payload changes
5. dashboard frontend profile editors and runtime cards
6. regression coverage for legacy single-timeframe paper and unchanged live mode

## Acceptance Criteria

This design is satisfied when all of the following are true:

- paper mode can run both `5m` and `15m` in parallel
- `5m` and `15m` can use different paper strategy lists
- `5m` and `15m` can use different paper parameter values
- dashboard shows separate `Paper 5m` and `Paper 15m` runtime cards
- live mode still runs exactly one timeframe through top-level `MARKET_TIMEFRAME`
- old single-timeframe paper config still works without forced migration
