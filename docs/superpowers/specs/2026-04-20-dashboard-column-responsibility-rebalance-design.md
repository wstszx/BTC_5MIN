# Dashboard Column Responsibility Rebalance Design

**Date:** 2026-04-20

**Status:** Approved by user for spec review

## Goal

Rebalance the three main dashboard columns so the primary screen emphasizes `交易决策` and `运行监控`, while `参数配置` becomes a lighter secondary sidebar.

## Problem

The current dashboard distributes responsibility unevenly:

1. The left `参数引擎` column has become too tall and mixes unrelated concerns.
2. The center `行情与信号` column is reasonably balanced, but could become a clearer decision cockpit.
3. The right `实时连接状态` column is too small and underpowered relative to its operational importance.

This creates the wrong visual priority. The heaviest column is configuration, while the user explicitly wants the main screen to privilege:

- trade decision-making
- runtime and connection monitoring

The problem is not only spacing. It is also responsibility confusion:

- config UI contains runtime information
- config UI contains diagnostic entry points
- runtime monitoring is split between the left and right sides

## Confirmed User Preference

The user explicitly approved the following during brainstorming:

1. Allow a noticeable redistribution of responsibility across the three columns.
2. Prioritize `交易决策` and `运行监控` as always-visible, first-glance information.
3. De-emphasize `参数配置` as a secondary but still accessible function.
4. Choose restructuring option B:
   - left column becomes a light configuration sidebar
   - center column becomes the decision cockpit
   - right column becomes a stronger runtime and connection monitoring column

## Decision Summary

1. Recast the left column as `轻配置侧栏`.
2. Recast the center column as `交易决策驾驶舱`.
3. Recast the right column as `运行与连接监控列`.
4. Move runtime status out of configuration and into monitoring.
5. Move the diagnostics entry point out of configuration and into the decision column.
6. Compress or remove low-value status blocks in the configuration column.
7. Preserve backend behavior and mostly preserve existing DOM ids while changing container ownership and presentation hierarchy.

## Column Responsibilities

### Left Column: Light Configuration Sidebar

The left column should become a compact control sidebar for changing configuration, not a catch-all information stack.

It should keep:

- strategy guide summary card
- core parameter form
- advanced settings toggle
- save button

It should stop being the place where operators read runtime state or diagnostics context.

### Center Column: Decision Cockpit

The center column should become the strongest and most visually dominant area.

Its job is to support the decision flow:

1. what round are we in
2. what does the market look like
3. what is the final trading decision
4. what is the session state right now
5. if needed, how do we inspect diagnostic explanation

This is the user's highest-priority screen region.

### Right Column: Runtime And Connection Monitoring

The right column should become a real monitoring column rather than a single lightweight card.

It should unify:

- runtime mode summary
- live readiness / hot-switch state
- connection and websocket health
- redeem / optimizer / background runtime details

This column should be consistently useful even when the user is not changing configuration.

## Element Reassignment

### Keep In Left Column

The left column should continue to own:

- `strategyGuideCard`
- `configForm`
- `advancedConfigToggle`
- `advancedConfigPanel`
- `btnSaveConfig`

These are configuration-facing elements and still belong together.

### Remove From Left Column

The left column should stop owning:

- standalone `读取状态` / `最近保存` blocks as independent visual sections
- `runtimeSummaryBar`
- `runtimeDetailsToggle`
- `runtimeDetailsPanel`
- `diagnosticsToggle`
- `diagnosticsPanel`

These items either describe runtime or explain a decision. They are not configuration.

### Move To Center Column

Move the diagnostics entry point into the center column, below the core decision and session content.

Recommended placement:

- below `会话状态`
- collapsed by default
- described as explanation/diagnostics for the current decision, not as config metadata

The detailed strategy 6 / strategy 7 diagnostic panels may remain folded behind this entry point.

### Move To Right Column

Move runtime status ownership into the right column.

Recommended grouping:

- top: runtime mode summary
- middle: live readiness / switch state
- bottom: websocket connection detail and background runtime rows

This makes the right side a coherent monitoring surface.

## Compression And Removal Strategy

### Compress Configuration Status

`读取状态` and `最近保存` should no longer occupy their own stacked blocks near the top of the configuration column.

Instead:

- merge them into a lighter status treatment near the header or save area
- keep the information available
- remove their current disproportionate vertical cost

### Keep Advanced Settings Folded

The configuration column should still support advanced settings, but behind the existing folded interaction.

That preserves editing power without forcing the full parameter surface into the first screenful.

### Reduce Repeated Time Signals In Center Column

The center column currently includes multiple time-oriented indicators that partially overlap.

Implementation should remove or visually weaken at least one repeated “recent refresh” style field so the decision cockpit stays focused.

The point is not to remove important timing context. The point is to reduce scan noise.

## Layout And Width Strategy

### New Priority Order

The visual priority should become:

1. center column
2. right column
3. left column

That should be visible both in width allocation and in information density.

### Width Direction

Recommended direction:

- left column: approximately 300-320px
- center column: dominant flexible main column
- right column: approximately 360-400px or moderately expanded relative to today

The exact numbers can flex, but the principle must hold: configuration should no longer be equal to or visually heavier than monitoring.

## Fold Strategy

Each column should have one clear fold behavior aligned with its role:

- left column: `高级参数`
- center column: `诊断区`
- right column: `运行详情`

This makes folding understandable:

- configuration folds configuration detail
- decision folds explanatory detail
- monitoring folds operational detail

## Visual Language

### Left Column

The left column should read as a control sidebar:

- lighter perceived importance
- tighter spacing
- fewer non-form informational cards

### Center Column

The center column should read as the primary operational cockpit:

- strongest hierarchy
- least clutter
- most immediate action relevance

### Right Column

The right column should read as an active monitoring rail:

- more substantial than the current `实时连接状态` card
- clearly separated runtime and connection sections
- useful even when untouched

## Implementation Scope

Primary file:

- `D:/python/BTC_5MIN/dashboard.py`

Likely change areas:

1. Main dashboard HTML layout structure
2. CSS width allocation and spacing for the three main columns
3. Re-homing folded sections and status cards into new columns
4. Small JS adjustments where moved DOM sections retain existing toggle or render behavior
5. Dashboard asset tests that assume old layout ownership of runtime and diagnostics blocks

## Backend / Data Constraints

No backend API redesign is required.

This change is about:

- structural hierarchy
- container ownership
- layout weighting
- visibility and fold behavior

It should not change the underlying runtime, market, summary, or diagnostics data contracts.

## Testing Requirements

At minimum, verification should cover:

1. The left column no longer permanently hosts runtime and diagnostics sections.
2. The center column includes the diagnostics entry point.
3. The right column includes runtime summary plus connection/runtime monitoring content.
4. The left column remains usable for editing and saving config.
5. The updated width allocation and responsive layout reflect the new priority order.
6. Existing JS hooks still find the expected ids after containers move.

## Non-Goals

- No change to trading logic.
- No change to backend payloads.
- No redesign of the unified report card completed earlier.
- No new monitoring features beyond reorganizing current runtime information.
- No attempt to solve every dashboard clutter issue in one pass.

## Risks

- Moving runtime and diagnostics blocks could break existing DOM lookups or toggle handlers if ids or section ownership change carelessly.
- If the left column is compressed too aggressively, parameter editing could become frustrating.
- If the right column becomes too dense without hierarchy, it may replace one imbalance with another.
- If repeated timing fields are removed too aggressively, operators may lose useful temporal context.

## Mitigations

- Preserve existing ids wherever practical and change parent containers rather than rewriting logic from scratch.
- Keep the left column “lighter” rather than “minimal”; preserve core edit flows.
- Give the right column a clear internal hierarchy: summary first, detail second.
- Remove or weaken only clearly redundant timing copy, not unique timing signals.

## Recommendation

Implement the approved B-direction rebalance:

- left column as a light configuration sidebar
- center column as the decision cockpit
- right column as the runtime/connection monitoring column

This most directly matches the user's chosen main-screen priority:

- decision first
- monitoring second
- configuration third
