# Dashboard Column Width And Text Overflow Design

**Date:** 2026-04-20

**Status:** Approved by user for spec review

## Goal

Adjust the main dashboard column widths so the `参数引擎` column is widened to roughly match `运行与连接监控`, while `行情与信号` is compressed moderately. At the same time, remove the left-column `状态 / 最近保存` block and ensure strategy titles, descriptions, and badges display fully instead of overflowing.

## Problem

The current dashboard still shows two related UI problems:

1. The `参数引擎` column is too narrow for strategy labels and helper copy.
2. Strategy rows and strategy explanation cards can overflow horizontally instead of wrapping cleanly.

The most visible symptoms are:

- the deleted-but-still-low-value `状态 / 最近保存` area consumes left-column space
- strategy rows such as `7 | OFI+动量共识` get squeezed
- the green `统一策略` badge text can be clipped
- explanatory text below the title does not have enough room to breathe

The user explicitly prefers solving this by widening the left column rather than forcing more aggressive truncation.

## Confirmed User Preference

The user explicitly approved the following design direction:

1. Use width option B:
   - left column widened to near the same width as the monitoring column
   - center column compressed moderately
   - right column remains strong
2. Remove the on-screen `状态 / 最近保存` block entirely.
3. Let strategy explanation text display fully instead of truncating it.
4. Keep the green strategy badge, but make its layout robust enough that it no longer clips.
5. Prefer wrapping over ellipsis for strategy labels, subtitles, and badges.

## Decision Summary

1. Remove the rendered left-column `config-status-inline` block.
2. Widen the left column to approximately match the right column.
3. Keep the center column as the primary decision area, but compress it slightly.
4. Allow strategy titles, subtitles, and badges to wrap cleanly.
5. Preserve current strategy logic, backend payloads, and report interactions.

## Width Strategy

### Desktop Direction

Adopt the approved B-direction width balance:

- left column: approximately `380px`
- center column: approximately `minmax(520px, 1.15fr)`
- right column: approximately `380px`

The exact values can be tuned slightly during implementation, but the principle must hold:

- left and right should feel close in width
- center should still be the largest column
- center should no longer dominate the width as strongly as it does now

### Why This Direction

This resolves the user's real pain directly:

- gives the configuration area enough horizontal room for strategy content
- preserves the monitoring column's current utility
- avoids shrinking the decision cockpit so much that it becomes hard to use

## Left Column Changes

### Remove Status Block

Remove the visible block that shows:

- `状态`
- `最近保存`

This means the current `config-status-inline` UI should disappear from the rendered interface.

If underlying ids or values remain useful internally, implementation may preserve them in code, but they should no longer consume visible layout space in the left column.

### Keep Left Column Focused

The left column should visually read as:

- strategy explanation
- basic strategy selection and config
- advanced settings entry

It should not reintroduce low-value informational chrome after the status block is removed.

## Strategy Content Display Rules

### Titles

Strategy titles such as:

- `7 | OFI+动量共识`

must be allowed to wrap instead of clipping or forcing adjacent controls out of view.

### Badge

The green badge such as:

- `统一策略`

should remain visible, but must no longer compete for the same fixed horizontal line in a way that causes clipping.

Acceptable outcomes:

- badge wraps to the next line
- badge sits below the title
- title and badge wrap naturally in the same flex/grid container

Unacceptable outcome:

- badge text cut off
- badge pushes other controls out of the viewport

### Subtitle / Description

Strategy description text such as:

- `只有 Binance OFI 和 Polymarket 动量同向时才允许交易。`

should render completely, typically over multiple lines if needed.

Do not solve this by truncating to ellipsis.

## Overflow Policy

For this feature area, the preferred policy is:

- wrap text
- preserve readability
- avoid horizontal overflow

This applies to:

- strategy row labels
- strategy summary/explanation text
- green badges and status-like inline tokens near the strategy title

## Center Column Protection

The center `行情与信号` column may be narrowed moderately, but the following must remain comfortably readable:

- round/timer area
- quote boxes
- final decision card

Implementation should prefer reducing outer spacing and redistributing column width before allowing these key decision widgets to feel cramped.

## Responsive Behavior

### Desktop

Use the new near-symmetric left/right width balance.

### Mid-Width

The monitoring column may still fall into a lower row or span layout if that remains the best responsive behavior, but the new width strategy should be respected before that breakpoint is reached.

### Narrow Screens

Keep the existing single-column stacking model.

This spec does not introduce a new mobile interaction pattern; it only changes the desktop/mid-width weighting and overflow behavior.

## Implementation Scope

Primary file:

- `D:/python/BTC_5MIN/dashboard.py`

Likely change areas:

1. Top-level dashboard grid width values
2. Left-column status block rendering
3. Strategy panel row layout
4. Strategy guide title/subtitle/badge wrapping behavior
5. Dashboard asset tests covering the new width and wrapping expectations

## Backend / Data Constraints

No backend API or strategy logic changes are required.

This is a UI layout and presentation adjustment only.

## Testing Requirements

At minimum, verification should cover:

1. The left-column status block is no longer rendered.
2. The desktop grid reflects the new near-symmetric left/right widths.
3. Strategy panel rows wrap instead of overflowing.
4. Strategy guide title/subtitle/badge layout no longer clips long text.
5. Existing decision and monitoring panels still render correctly after the width change.

## Non-Goals

- No change to strategy logic.
- No change to backend payloads.
- No redesign of the unified report card.
- No broad cleanup of unrelated dashboard copy.

## Risks

- Widening the left column too much could make the center column feel cramped.
- Allowing wrapping everywhere could make strategy cards taller than expected.
- Removing the status block could hide information some workflows still rely on if no alternative signal remains.

## Mitigations

- Keep the center column as the largest column, even after widening the left side.
- Prefer multi-line readable wrapping over clipped one-line layout.
- Remove only the visible status block, not necessarily the underlying data fields if future reuse is needed.

## Recommendation

Implement the approved B-direction refinement:

- widen the left column to near the monitoring column width
- remove the visible left status block
- allow strategy titles, subtitles, and the `统一策略` badge to wrap cleanly

This directly addresses the specific visual problems the user called out without undoing the broader decision/monitoring rebalance already approved.
