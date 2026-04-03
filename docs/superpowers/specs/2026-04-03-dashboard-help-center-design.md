# Dashboard Help Center Design

**Date:** 2026-04-03

**Status:** Proposed and validated with user through iterative review

## Goal

Add a drawer-based help center to the dashboard so operators can understand the page, strategy settings, and common troubleshooting paths without leaving the current screen or reading raw markdown docs.

## Problem

The dashboard already exposes many runtime concepts, but the operator has to infer meaning from labels, raw values, and numeric strategy ids. Existing docs explain the system, but they live outside the dashboard and are organized like runbooks rather than in-product guidance. This makes first-time use slower and increases the chance of configuration mistakes or misreading runtime state.

## Design Principles

1. Keep users in context. Help must open inside the dashboard, not redirect away from it by default.
2. Prefer operational guidance over documentation dumps. The first screen should tell the user what to do, not just what things are called.
3. Reuse live dashboard metadata whenever possible so help text stays aligned with real config behavior.
4. Make the first version easy to maintain. No markdown parser, no search system, no secondary docs renderer.

## Proposed Experience

### Entry Point

- Add a `帮助` button to the existing top-right action area beside `立即刷新`.
- Clicking the button opens a right-side drawer overlay.
- The drawer closes via:
  - top-right close button
  - clicking the backdrop
  - pressing `Escape`

### Drawer Layout

The help center is a single right-side drawer with:

- header:
  - title: `帮助中心`
  - subtitle: `快速上手与元素说明`
  - close button
- tab navigation:
  - `快速上手`
  - `页面说明`
  - `配置字典`
  - `策略说明`
  - `常见问题`
- scrollable content body
- footer links to source docs

Default open tab: `快速上手`

### Responsive Behavior

- Desktop: fixed right drawer, width about `440px`
- Narrow screens: drawer uses nearly full width
- Background content remains visible but blocked by a backdrop while the drawer is open

## Information Architecture

### 1. 快速上手

This is the default landing tab and is written as an operations guide, not reference material.

Sections:

1. `先看哪里`
   - `行情与信号`
   - `下注计划与风控`
   - `会话状态`
   - `实时连接状态`
2. `怎么安全改参数`
   - confirm current strategy first
   - change one category at a time
   - save and trust effective values, not just raw input
   - fix validation warnings before continuing
3. `怎么判断当前能不能跑`
   - how to read `是否下单`
   - acceptable skip reasons vs. reasons that need intervention
4. `出问题先看哪里`
   - saved but no effect
   - always skipping
   - direction not understood
   - websocket trouble
   - daily pnl confusion

This tab also includes a compact operator tip:

> 建议先确认基础策略、下注模式、最大下注金额，再观察 3~5 个轮次，不要频繁同时改动多项参数。

### 2. 页面说明

Explain the meaning of each major dashboard area in the order it appears on screen.

Blocks:

- `参数引擎`
- `行情与信号`
- `下注计划与风控`
- `会话状态`
- `实时连接状态`
- `纸面交易汇总`
- `最近纸面交易明细`

Each block uses a repeated structure:

- what this area is for
- what the operator should focus on
- key fields explained in plain language

### 3. 配置字典

Explain editable configuration in grouped form using dashboard metadata instead of a hardcoded duplicate list.

Groups:

- `基础策略`
- `动量信号`
- `风险与告警`
- `实时连接保护`

Each field row shows:

- Chinese label
- internal key name
- one-sentence explanation
- whether it is only relevant to strategy 5

Behavior:

- `动量信号` is still visible when strategy 5 is not selected, but marked as not currently active
- this tab reuses:
  - `field_groups`
  - `field_help`
  - `field_scope`
  - current `env_values`

### 4. 策略说明

Explain all strategies as cards instead of numeric ids.

Cards:

- `1 | 单轮交替`
- `2 | 双轮分组交替`
- `3 | 三轮分组交替`
- `4 | 四轮分组交替`
- `5 | 动量信号 V2`

Each card contains:

- strategy name
- one-line summary
- preview pills
- suitable use case
- associated parameters

Special handling for strategy 5:

- show that it uses momentum rather than a fixed UP/DOWN rhythm
- show current weak-signal mode
- show current fallback strategy
- show current threshold and lock timing

Current active strategy card should be visually highlighted.

### 5. 常见问题

Show short operational Q&A entries for fast troubleshooting.

Initial FAQ list:

1. Why did save succeed but behavior still looks wrong?
2. Why is the plan skipping this round?
3. Why does strategy 5 often show no signal?
4. Why is the direction different from what I expected?
5. Why did websocket stale protection trigger?
6. Why did daily realized pnl reset?
7. Why does max stake keep causing skips?
8. What do new users most often misunderstand?

## Content Sources

### Dynamic Dashboard Metadata

Reuse existing dashboard metadata so the help center stays aligned with actual behavior:

- `strategy_catalog`
- `field_groups`
- `field_scope`
- `field_help`
- current `env_values`

This content should be rendered directly from the same payload already used by the config panel.

### Static Guidance

Store tab copy in dashboard-side JavaScript data structures for the first version. Do not render raw markdown files inside the drawer.

### Source Document Links

The drawer footer should link to:

- `docs/dashboard_runbook.md`
- `docs/operations_runbook.md`
- `docs/daily_ops_checklist.md`

These are supporting references, not the primary drawer content.

## Technical Design

### Backend

No new standalone HTTP endpoint is required for v1.

The existing `/api/config` payload already carries strategy and field metadata after the recent config-UX changes. The help center should consume those existing fields rather than adding a separate docs API.

If a small additional payload key is needed for footer links or static labels, it may be added to `get_config_payload()`, but v1 should avoid turning help content into server-side HTML.

### Frontend

Implement help center UI inside the existing dashboard HTML/CSS/JS generated by `dashboard.py`.

Add:

- a topbar `帮助` button
- backdrop layer
- help drawer container
- tab navigation
- content rendering functions
- open/close keyboard handlers

Suggested frontend state additions:

- `helpOpen: boolean`
- `helpTab: string`

Suggested renderer split:

- `renderHelpDrawer()`
- `renderHelpQuickStart()`
- `renderHelpPageGuide()`
- `renderHelpConfigDictionary()`
- `renderHelpStrategyGuide()`
- `renderHelpFaq()`

These renderers should compose strings or DOM nodes in the same style as the rest of the dashboard JS.

## Accessibility

The drawer should:

- use a real button for open and close controls
- trap focus only lightly if feasible in the current no-framework setup, but at minimum:
  - focus the drawer on open
  - return focus to the help button on close
- support `Escape`
- use meaningful section headings
- preserve readable contrast in the current visual theme

## Non-Goals For V1

Do not add:

- markdown rendering for docs
- full-text search
- inline tooltips for every field on the page
- auto-routing from runtime errors into the help center
- analytics or tracking

## Testing Plan

Extend `tests/test_dashboard.py` to verify:

1. HTML includes:
   - help button
   - drawer container
   - tab anchors
2. JS includes:
   - help state
   - drawer render functions
   - default tab set to `快速上手`
3. Existing config payload still provides:
   - strategy metadata
   - field grouping metadata
   - current effective config values
4. Full regression remains green with `pytest -q`

## Implementation Notes

- Keep all help copy centralized so future edits do not require searching through multiple rendering branches.
- Reuse the existing strategy preview and field-help vocabulary added to the config panel.
- Keep the drawer visually quieter than the main market panels so it reads as support UI, not a competing dashboard surface.

## Recommended First Implementation Order

1. Add failing dashboard tests for help button, drawer shell, and default tab.
2. Add drawer HTML and CSS shell.
3. Add JS state and open/close interactions.
4. Implement `快速上手` and `页面说明`.
5. Implement dynamic `配置字典` and `策略说明`.
6. Implement `常见问题` and footer doc links.
7. Run focused dashboard tests, then full `pytest`.
