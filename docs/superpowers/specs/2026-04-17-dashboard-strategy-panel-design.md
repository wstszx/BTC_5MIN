# Dashboard Strategy Panel Design

**Date:** 2026-04-17

**Status:** Approved by user for implementation

## Goal

Simplify the dashboard strategy controls by replacing the current split "基础策略 + 全选全部策略 + 多选列表" interaction with one strategy panel that keeps the existing config semantics intact.

## Problem

The current dashboard already treats `STRATEGY_ID` and `PAPER_STRATEGY_IDS` as a unified area, but the operator still has to understand three separate interaction concepts:

1. Choose a single base strategy.
2. Choose multiple paper strategies.
3. Use a dedicated "select all" button for the paper strategy list.

This creates extra cognitive load even though the underlying data model is valid.

## Confirmed Constraints

- `STRATEGY_ID` and `PAPER_STRATEGY_IDS` remain separate configuration values.
- The user explicitly wants to allow cases such as `STRATEGY_ID=5` while `PAPER_STRATEGY_IDS=1,2,5,6`.
- Live trading stays single-strategy and continues to rely on `STRATEGY_ID`.
- Paper trading continues to use the multi-strategy set from `PAPER_STRATEGY_IDS`.

## Decision Summary

1. Replace the existing split controls with one "策略面板" style editor.
2. Show every strategy as one row inside that panel.
3. Each row has:
   - a checkbox-like paper selection control for whether the strategy is included in `PAPER_STRATEGY_IDS`
   - a single-select "主策略" control for whether the strategy is the active `STRATEGY_ID`
4. Remove the standalone "全选全部策略" button.
5. Add lightweight panel actions instead: "全选", "清空", and an explicit rule that the main strategy is auto-included in the paper set when collecting values.

## Interaction Model

### Strategy Rows

Each strategy row should expose the same three pieces of information:

- strategy id
- strategy short label
- selection state for paper/runtime and main/focus

The paper selection control is the operator-facing representation of `PAPER_STRATEGY_IDS`.
The main strategy control is the operator-facing representation of `STRATEGY_ID`.

### Main Strategy Inclusion Rule

If the operator marks a strategy as the main strategy, saving the form must ensure that strategy is included in `PAPER_STRATEGY_IDS` even if its paper checkbox is currently off.

This preserves the current behavior from the unified selection helper and avoids invalid or confusing config states.

### Bulk Actions

The panel should include small inline actions:

- `全选`: select all strategies for paper trading
- `清空`: clear paper selections, then rely on the main-strategy inclusion rule so the saved payload still includes the chosen main strategy

These actions replace the current dedicated full-width button and keep the control set visually grouped.

## Non-Goals

- No backend config schema changes.
- No runtime behavior changes.
- No changes to the independent report filters for summary and recent trades.
- No changes to the strategy guide card beyond reading the same `STRATEGY_ID`.

## Testing Notes

- Dashboard asset tests should stop asserting the old dedicated select-all button and old dual-select wording.
- New tests should assert that the strategy panel markup and helper functions exist.
- New tests should assert that the bulk actions and main-strategy auto-inclusion rule are wired into the config collection path.
