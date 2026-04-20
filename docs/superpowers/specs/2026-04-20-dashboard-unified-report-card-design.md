# Dashboard Unified Report Card Design

**Date:** 2026-04-20

**Status:** Approved by user for spec review

## Goal

Restructure the dashboard report area so `报告视图`、`纸面交易汇总`、`最近交易明细` become one unified report card with a single visible strategy selector that drives both summary and recent-trade data.

## Problem

The current dashboard splits the report area into three separate panels:

1. `报告视图`
2. `纸面交易汇总`
3. `最近交易明细`

That separation makes the filtering story harder to follow than it needs to be.

Even after previous improvements, the operator still experiences the report area as a set of related-but-separate widgets instead of one coherent report surface. The user wants these sections visually and structurally grouped so the strategy selector clearly belongs to both the summary and detail output below it.

## Confirmed User Preference

The user explicitly approved the following interaction and layout choices:

1. Put the report controls and outputs together in one vertically stacked report area.
2. Keep the top control as the current dropdown selector.
3. Make the whole area look like one unified report card rather than three separate panels.
4. Use one strategy selector so `纸面交易汇总` and `最近交易明细` always follow the same selected strategy.

## Decision Summary

1. Replace the current three-panel report presentation with one outer `report` card.
2. Convert the old standalone `报告视图` panel into the header/control area of that card.
3. Render `纸面交易汇总` as the first internal section inside the unified card.
4. Render `最近交易明细` as the second internal section inside the unified card.
5. Keep the existing dropdown selector UI at the top of the unified card.
6. Make that dropdown the single visible strategy source for both summary and recent views.
7. Preserve the existing summary and recent backend endpoints and payload shapes.

## Layout Structure

### Outer Card

The report area should become one unified container with:

- one shared outer border/background
- one top header area
- two internal content sections separated by lighter dividers

This should visually read as one report surface instead of multiple independent cards.

### Header / Control Area

The top area of the unified card should contain:

- title: `报告视图`
- subtitle describing that the selector controls both summary and detail
- existing strategy dropdown

This header replaces the old standalone `报告视图` panel.

### Section 1: Paper Summary

The current `纸面交易汇总` content remains, but moves inside the unified report card as its first internal section.

It should keep:

- summary KPI grid
- recent day table
- existing data behavior

It should lose the redundant standalone outer panel shell.

### Section 2: Recent Trades

The current `最近交易明细` content remains, but moves inside the unified report card as the second internal section.

It should keep:

- table structure
- current strategy label in the subtitle
- pending/highlight behavior
- current detail columns

It should lose the redundant standalone outer panel shell.

## Filtering Model

### Single Visible Strategy Selector

The visible dropdown at the top of the unified report card becomes the only operator-facing strategy selector for this report area.

When the operator changes it:

- `纸面交易汇总` refreshes using the selected strategy
- `最近交易明细` refreshes using the same selected strategy

### Unified Source Of Truth

The UI should stop behaving as though summary and recent have independent active strategy state.

Implementation preference:

- one effective visible filter source for the report card
- summary and recent both derive from that source

If compatibility helpers remain temporarily in code, they should still resolve to the same selected value for this report card.

### Current Strategy Copy

The `最近交易明细` subtitle should continue to show the current strategy, but that text must come directly from the unified report selector state.

Examples:

- `按时间倒序显示最近 80 条记录 · 当前策略：全部`
- `按时间倒序显示最近 80 条记录 · 当前策略：策略 7`

## Visual Language

### Unified Card Appearance

The unified report card should feel intentional and structured:

- one outer report shell
- compact, clear header
- section titles inside the card
- lighter internal separators than the outer border

The goal is not a dramatic redesign. The goal is to make the relationship between control, summary, and detail obvious at a glance.

### Internal Section Treatment

Each internal section should still have its own heading, but those headings should behave like in-card section labels rather than full standalone panel heads.

That means:

- less repeated chrome
- clearer parent-child hierarchy
- more consistent spacing

## Implementation Scope

Primary file:

- `D:/python/BTC_5MIN/dashboard.py`

Areas to update:

1. Dashboard HTML structure
2. Dashboard CSS for unified report-card styling
3. Dashboard JS state/rendering for unified strategy control behavior
4. Dashboard asset tests that currently assume the old three-panel presentation

## Backend / Data Constraints

No backend API redesign is required.

Keep existing endpoints:

- paper summary endpoint
- recent trades endpoint

Keep existing payload shapes and query semantics. The change is in how the front-end organizes the report area and chooses the strategy parameter passed to those endpoints.

## Testing Requirements

At minimum, tests should confirm:

1. The report area markup reflects a unified card structure.
2. The old standalone `报告视图` panel shell is no longer rendered as a separate report card.
3. The shared report strategy selector remains present.
4. Summary and recent continue to use the same selected strategy source.
5. The recent panel subtitle still shows the current strategy correctly.

## Non-Goals

- No change to trading strategy logic.
- No change to report APIs.
- No new selector style such as tabs or button groups.
- No added folding/accordion behavior inside the report card.
- No pagination or column redesign for recent trades.

## Risks

- If the old summary/recent split filter state is only partially cleaned up, the UI could display one strategy label while requesting another strategy’s data.
- If too much visual chrome is removed, the summary and recent sections may feel collapsed together instead of intentionally grouped.
- Dashboard asset tests may fail if they still assert the old separate panel structure.

## Mitigations

- Keep one explicit visible selector and derive both report requests from the same effective strategy value.
- Preserve clear internal headings for both summary and recent sections.
- Update tests alongside the structural changes instead of treating them as follow-up cleanup.

## Recommendation

Implement the report area as one unified report card with a single top dropdown and two internal sections for summary and recent trades.

This directly matches the user’s preferred mental model:

- one report control
- one report card
- one selected strategy
- two synchronized outputs
