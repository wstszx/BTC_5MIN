# Multi-Strategy Paper Trading Design

**Date:** 2026-04-11

**Status:** Proposed and validated with user through iterative review

## Goal

Allow paper trading to run multiple strategies at the same time, while keeping each strategy isolated and making dashboard results filterable by strategy.

## Problem

The current paper trading flow assumes exactly one active strategy. The runtime state, pending paper settlement queue, and dashboard summaries all behave as if paper trading is a single-strategy process. That makes it impossible to compare strategies side-by-side under the same market conditions.

## Decision Summary

1. Use one paper trading runtime that manages multiple independent strategy workers.
2. Give each strategy its own isolated paper session state and pending settlement queue.
3. Keep a shared paper trade log, but preserve the existing `strategy` field on every record.
4. Add dashboard support for selecting multiple paper strategies to run and filtering results by strategy.
5. Keep backward compatibility with existing single-strategy settings and old session state files.

## Architecture

### Runtime Model

The paper trading runtime will become a coordinator over a set of strategy-specific workers.

- Input market data remains shared for the current round.
- Each enabled strategy computes its own side decision, risk plan, settlement result, and state transition.
- Strategies do not share round index, pnl, recovery loss, consecutive loss count, pending paper trades, or strategy-specific signal fields.
- Strategy 5 and strategy 6 continue to use their existing signal logic, but only inside their own state scope.

### Configuration Model

Add a new paper-only multi-select configuration value:

- `PAPER_STRATEGY_IDS=1,2,6`

Rules:

- If `PAPER_STRATEGY_IDS` is set, paper trading uses that list.
- If `PAPER_STRATEGY_IDS` is empty or missing, paper trading falls back to the existing `STRATEGY_ID` setting for backward compatibility.
- Live trading remains single-strategy and continues to use `STRATEGY_ID` only.

## Data Model

### Paper Session State

Replace the single paper session structure with a per-strategy map.

New shape:

```json
{
  strategies: {
    1: { round_index: 0, cash_pnl: 0.0 },
    5: { round_index: 0, cash_pnl: 0.0 },
    6: { round_index: 0, cash_pnl: 0.0 }
  }
}
```

Each strategy entry stores the same per-strategy state fields that the current single paper session stores today, including pending paper trades and strategy-specific signal state.

### Trade Log

Keep using the existing shared `paper_trades.csv` log.

Why:

- It already records `strategy` per row.
- It avoids migration of historical paper records.
- It makes cross-strategy comparison possible while preserving per-strategy filtering.

## Dashboard Experience

### Config Panel

For paper trading only:

- Add a multi-select strategy control backed by `PAPER_STRATEGY_IDS`.
- Allow choosing any subset of strategies `1` through `6`.
- Preserve the existing single `STRATEGY_ID` control for live trading compatibility.

### Results Filtering

Add a strategy filter to paper results views.

Behavior:

- Default filter is `all`.
- The user can switch to a single strategy view.
- Paper summary metrics recalculate from filtered rows.
- Recent paper trades list shows only rows matching the selected strategy when filtered.

### Strategy-Specific Details

Strategy 6 OFI details remain visible only when strategy 6 is the active strategy in the current market payload. They should not be shown as static empty fields for other strategies.

## Backward Compatibility

### Old Config

- Existing single-strategy paper setups keep working through `STRATEGY_ID` when `PAPER_STRATEGY_IDS` is unset.
- Existing live setups are unchanged.

### Old Session State Files

If the old paper session file is still in single-strategy format, load it and wrap it into the new multi-strategy structure using the effective paper strategy id.

### Historical Logs

No migration is required for `paper_trades.csv`.

## Error Handling

- Invalid `PAPER_STRATEGY_IDS` entries are ignored with validation feedback in the dashboard.
- Duplicate strategy ids are de-duplicated while preserving a stable display order.
- If one strategy fails during a paper round, the runtime should isolate and report that strategy error without corrupting the state of the other strategies.

## Testing Strategy

Add or update tests for:

1. Parsing and normalizing `PAPER_STRATEGY_IDS`.
2. Backward-compatible loading of old single-strategy paper session state.
3. Independent per-strategy state progression in one paper trading runtime pass.
4. Shared log output with correct `strategy` values.
5. Dashboard payload support for multi-strategy config and strategy filtering.
6. Dashboard assets and behavior for paper strategy multi-select and result filtering.

## Non-Goals

- Running multiple live strategies in parallel.
- Splitting paper trading into multiple OS processes.
- Adding advanced portfolio comparison visualizations in the first version.
