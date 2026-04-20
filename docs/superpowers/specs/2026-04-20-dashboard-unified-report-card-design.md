# Dashboard Unified Report Card Design

**Date:** 2026-04-20

**Status:** Approved by user for spec review

## Goal

Restructure the dashboard report area so `报告视图`、`纸面交易汇总`、`最近交易明细` become one unified full-width report card with a single visible strategy selector that controls both summary and recent-trade content.

## Problem

The current dashboard presents the report area as three separate surfaces:

1. `报告视图`
2. `纸面交易汇总`
3. `最近交易明细`

The selector logic is already shared, but the layout still makes the operator read these as separate widgets rather than one report workflow. That weakens the relationship between:

- choosing a strategy
- reviewing the aggregated paper result
- checking the matching recent trade rows

The user wants these pieces to read as one coherent report area while keeping the current data behavior intact.

## Confirmed User Preference

The user explicitly approved the following decisions during brainstorming:

1. Only keep the `报告视图` dropdown in the new unified full-width report header.
2. Merge the old report selector panel, paper summary panel, and recent trades panel into one report surface.
3. Use layout option B: a single outer panel with `纸面交易汇总` on the left and `最近交易明细` on the right on wide screens.
4. Keep one shared selector so strategy changes affect both summary and recent detail together.
5. Preserve familiar inner section labels so the operator can still quickly locate summary versus recent trades.
6. Keep the current recent-trade table fields, empty states, and filtering behavior unless layout integration requires small wiring changes.

## Decision Summary

1. Replace the old three-panel report presentation with one outer full-width `交易报告` card.
2. Move the old `报告视图` selector into the new card header as the only visible report control.
3. Render `纸面交易汇总` as the left in-card section.
4. Render `最近交易明细` as the right in-card section.
5. Keep the existing shared selector wiring as the single visible strategy source for both sections.
6. Preserve the existing summary and recent backend endpoints and payload shapes.
7. Preserve responsive usability by collapsing the left-right layout into a vertical stack on narrower widths.

## Layout Structure

### Outer Card

The report area should become one full-width container that spans the dashboard grid and visually reads as one report surface.

The outer card should provide:

- one shared border and background
- one header row for report controls and status
- one content area with two internal sections
- clear separation between the left summary section and the right recent-trades section

This should feel like a unified report card, not like three adjacent cards merely placed close together.

### Header / Control Area

The unified card header should contain:

- title: `交易报告`
- subtitle: `策略筛选同时作用于纸面交易汇总与最近交易明细`
- the existing `paperReportStrategy` dropdown
- one shared status area showing both summary and recent refresh state

The old standalone `报告视图` panel shell should disappear.

### Left Section: Paper Summary

The left section should keep the current `纸面交易汇总` content, but as an internal section rather than a standalone panel.

It should include:

- an internal section label or title for `纸面交易汇总`
- the existing six KPI cells
- the existing by-day summary table
- the current empty-state behavior

It should not keep a separate outer panel head and outer border once moved inside the unified report card.

### Right Section: Recent Trades

The right section should keep the current `最近交易明细` content, but as an internal section rather than a standalone panel.

It should include:

- an internal section label or title for `最近交易明细`
- the existing subtitle text area driven by the effective shared strategy
- the existing table columns and row styling
- the current pending-settlement and missed-entry highlighting behavior
- the current empty-state behavior for paper versus live runtime mode

It should not keep a separate outer panel head and outer border once moved inside the unified report card.

## Interaction Model

### Single Visible Strategy Selector

The visible dropdown in the report header becomes the only operator-facing strategy selector for the report area.

When the operator changes it:

- `纸面交易汇总` refreshes using the selected strategy
- `最近交易明细` refreshes using the same selected strategy

### Shared Effective Filter State

The existing logic already resolves a shared report filter via:

- `state.paperReportStrategyFilter`
- `effectivePaperSummaryStrategyFilter()`
- `effectivePaperRecentStrategyFilter()`

That model should stay in place unless cleanup is trivial. The key requirement is that the unified card behaves as one strategy-controlled report surface.

### Current Strategy Copy

The current strategy text should remain in the recent-trades subtitle rather than being repeated throughout the card.

Examples:

- `按时间倒序显示最近 80 条记录 · 当前策略：全部`
- `按时间倒序显示最近 80 条记录 · 当前策略：策略 7`

The outer report subtitle should describe the shared control relationship, not duplicate the current strategy name.

## Status Presentation

The header should show one shared status area with two independently meaningful states:

- summary status, derived from the summary refresh result
- recent status, derived from the recent-trades refresh result

Examples of acceptable presentation:

- `汇总: 已更新`
- `明细: 80 行`
- `汇总: 刷新失败`
- `明细: 0 行`

If one side fails and the other succeeds, both states should still remain visible so the operator can tell which half needs attention.

## Responsive Behavior

### Wide Layout

On wide screens, the unified report card should use a two-column layout:

- left column for summary
- right column for recent trades

This is the user-approved layout direction because it supports quick comparison between aggregated performance and raw recent rows.

### Narrow Layout

At narrower breakpoints, the card should collapse into a vertical stack to preserve table readability and avoid compressing the recent-trades table too aggressively.

The fallback order should be:

1. summary section
2. recent trades section

## Visual Language

### Unified Card Appearance

The card should feel like a grouped operational report, not a redesign for its own sake.

Desired traits:

- one outer shell
- one clear header
- internal section titles instead of duplicated heavy panel chrome
- consistent spacing across both internal sections

### Internal Section Styling

The internal sections should remain distinct without looking like fully separate cards.

Recommended treatment:

- section titles inside the card body
- light dividers or spacing between summary and recent
- reduced repeated borders versus the old layout

## Implementation Scope

Primary file:

- `D:/python/BTC_5MIN/dashboard.py`

Likely areas to update:

1. Dashboard HTML structure for the report area
2. Dashboard CSS for unified-card and two-column report styling
3. Dashboard JS wiring where summary and recent status text are rendered in the new shared header area
4. Dashboard tests or assertions that depend on the old three-panel structure

## Backend / Data Constraints

No backend API redesign is required.

Keep existing endpoints and payload semantics for:

- paper summary
- recent trades

This change is explicitly a front-end structure and presentation refactor.

## Testing Requirements

At minimum, verification should cover:

1. The report area renders as one full-width unified card.
2. The old standalone `报告视图` panel shell is removed.
3. The old standalone summary and recent outer shells are removed or absorbed into the unified structure.
4. The shared `paperReportStrategy` dropdown remains present in the new report header.
5. Changing the shared dropdown still triggers both summary and recent refresh behavior.
6. The recent subtitle still shows the current strategy correctly.
7. Summary and recent status text both render in the header without masking each other.
8. Wide screens use left-right layout and narrower screens stack vertically.

## Non-Goals

- No change to trading strategy logic.
- No change to report API contracts.
- No new selector type such as tabs, pills, or segmented buttons.
- No redesign of recent-trade columns.
- No pagination, sorting redesign, or historical drill-down additions.
- No unrelated cleanup of other dashboard areas.

## Risks

- DOM restructuring could break existing render functions if expected node ids disappear.
- Summary and recent status could overwrite each other if the new header area is not wired carefully.
- The recent table may become too cramped if the wide-layout column split is too aggressive.
- Tests that assert the previous three-panel markup may fail after the layout merge.

## Mitigations

- Preserve existing ids where practical, including `paperReportStrategy`, `daysTbody`, `recentTbody`, `recentPanelDesc`, `paperStatus`, and `recentStatus`.
- Prefer moving existing content blocks into the new layout over rewriting render logic from scratch.
- Keep responsive fallbacks simple: left-right on wide screens, vertical stack on narrow screens.
- Update layout-sensitive tests alongside the structural change instead of deferring them.

## Recommendation

Implement the report area as one unified full-width `交易报告` card with:

- one header
- one visible strategy selector
- one shared status area
- `纸面交易汇总` on the left
- `最近交易明细` on the right

This directly matches the user-approved workflow:

- one report control
- one selected strategy
- one grouped report surface
- two synchronized outputs
