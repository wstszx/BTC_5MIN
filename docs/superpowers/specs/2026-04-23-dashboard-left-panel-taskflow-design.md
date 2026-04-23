# Dashboard Left Panel Taskflow Redesign

**Date:** 2026-04-23

**Status:** Drafted after interactive design review

## Goal

Restructure the left-side dashboard configuration area so it is organized around the operator's actual workflow instead of exposing one long mixed parameter list.

The redesign should make the configuration area easy to understand for both:

- `paper` mode operation
- `live` mode operation

while keeping a single top-level mode selector as the primary way to switch context.

## User-Approved Direction

The user explicitly approved these constraints:

- `paper` and `live` are both important, neither should be hidden as a rare-use mode
- the operator should switch between them through a single dropdown
- the left panel should be reorganized to be more reasonable and easier to understand
- the preferred design direction is a taskflow-style layout

## Problem

The current left panel is hard to read because it mixes several different kinds of information into one long editing column:

- mode switching
- live credentials
- paper timeframe profiles
- strategy selection
- core sizing/risk parameters
- advanced signal thresholds
- WS protection and operational guardrails

Even after the paper multi-timeframe work, the left panel still feels like a parameter registry more than an operator console. Users have to remember which parameters matter for:

- `paper`
- `live`
- the current timeframe
- the current strategy

That creates cognitive overload and makes the panel harder to trust.

## Decision Summary

Redesign the left panel as a single-mode taskflow workspace.

Key rules:

1. The top of the left panel contains one primary dropdown: `Paper / Live`.
2. The rest of the left panel switches entirely based on that selection.
3. `Paper` mode uses a step-by-step taskflow:
   - choose run scope
   - choose strategy
   - edit core parameters
   - optionally expand advanced parameters
4. `Live` mode uses its own step-by-step taskflow:
   - choose market and activation state
   - review credentials
   - review redeem/safety settings
   - optionally expand advanced parameters
5. The currently edited paper timeframe is selected separately inside `Paper` mode.
6. Only one paper timeframe profile is edited at a time in the left panel.
7. Advanced parameters remain available, but they are visually and structurally demoted behind a fold.

## Non-Goals

- No change to trading logic itself.
- No change to paper/live backend config semantics beyond what the UI needs to save.
- No multi-column redesign of the left panel; this remains a single-column workspace.
- No removal of advanced parameters from the product.
- No redesign of the center or right dashboard panels in this change.

## Core UX Principle

The left panel should answer one question at a time:

> "What am I trying to configure right now?"

The answer should never be "everything."

## Top-Level Structure

The left panel becomes:

1. mode selector
2. current editing context
3. mode-specific taskflow sections
4. save area

That means the panel stops acting like a generic settings sheet and starts acting like a focused operator workspace.

## Shared Top Area

### Mode Selector

At the top of the left panel, show one dropdown:

- `Paper`
- `Live`

This is the only primary navigation control for the left-side configuration workspace.

Changing this selector updates the visible editing workflow below it.

### Context Summary

Below the mode selector, show a short one-line context summary such as:

- `当前正在编辑：Paper / 15m`
- `当前正在编辑：Live / 15m`

This reduces ambiguity about what the save button will affect.

## Paper Mode Layout

When the mode selector is set to `Paper`, the left panel should show four sections in this order.

### Step 1: Choose Run Scope

This section controls which paper runtimes exist and which one is currently being edited.

Content:

- enabled paper timeframes
- current paper timeframe editor selection

Recommended interaction:

- checkbox or toggle group for enabled timeframes (`5m`, `15m`)
- secondary tab or pill selector for "currently editing timeframe"

Important rule:

- the operator may enable multiple timeframes to run
- but the form below edits only one timeframe profile at a time

This avoids showing two large profile forms simultaneously.

### Step 2: Choose Strategy

This section controls the active strategy context for the currently edited paper timeframe.

Content:

- current primary strategy
- paper strategy list for the active timeframe
- recommended preset action for the active timeframe

This should preserve the existing strategy panel behavior where:

- one strategy is the current focus strategy
- multiple strategies may still run in paper mode

### Step 3: Core Trading Parameters

This section contains the paper parameters most likely to change between runs.

These are the default-visible paper fields:

- `PAPER_TIMEFRAMES`
- active paper timeframe selector
- `PAPER_<TF>_STRATEGY_ID`
- `PAPER_<TF>_STRATEGY_IDS`
- `PAPER_<TF>_TARGET_PROFIT`
- `PAPER_<TF>_BASE_ORDER_COST`
- `PAPER_<TF>_MAX_STAKE`
- `PAPER_<TF>_MAX_CONSECUTIVE_LOSSES`
- `PAPER_<TF>_OPEN_DELAY_SECONDS`

These should be visually grouped as the "normal run setup" fields.

### Advanced Parameters

This remains collapsed by default.

It contains paper parameters that are:

- more technical
- lower frequency
- higher risk to misconfigure

These include:

- `PAPER_<TF>_BET_SIZING_MODE`
- `PAPER_<TF>_SIGNAL_MOMENTUM_THRESHOLD`
- `PAPER_<TF>_OFI_THRESHOLD`
- `PAPER_<TF>_BINANCE_SIGNAL_STALE_SECONDS`
- `PAPER_<TF>_STRATEGY7_OFI_THRESHOLD`
- `PAPER_<TF>_STRATEGY7_MOMENTUM_THRESHOLD`
- `PAPER_<TF>_STRATEGY7_MAX_ENTRY_PRICE`
- other strategy 7 confirmation / signal quality settings
- lower-level WS / execution protection values if still paper-relevant in the panel

## Live Mode Layout

When the mode selector is set to `Live`, the left panel should show a different taskflow.

### Step 1: Market And Activation

This section should show:

- `MARKET_TIMEFRAME`
- `LIVE_TRADING_ENABLED`

The operator should be able to quickly tell:

- which market live mode is targeting
- whether live trading is actually enabled

### Step 2: Order Credentials

This section should show the live credentials needed for actual order placement.

Visible by default:

- `POLYMARKET_PRIVATE_KEY`
- `POLYMARKET_FUNDER`
- `POLYMARKET_API_KEY`
- `POLYMARKET_API_SECRET`
- `POLYMARKET_API_PASSPHRASE`

These remain in the default-visible area because they are operationally required even if they are not changed often.

### Step 3: Redeem And Safety

This section should show live redeem and related operational controls:

- `LIVE_AUTO_REDEEM_ENABLED`
- `LIVE_AUTO_REDEEM_DRY_RUN`
- `LIVE_AUTO_REDEEM_POLL_SECONDS`
- `LIVE_AUTO_REDEEM_MAX_RETRIES`
- `LIVE_AUTO_REDEEM_INITIAL_BACKOFF_SECONDS`
- `LIVE_AUTO_REDEEM_MAX_BACKOFF_SECONDS`

### Advanced Parameters

Collapsed by default.

This should include:

- `POLYMARKET_BUILDER_*`
- `POLYMARKET_RELAYER_*`
- remaining live-specific safety/protection options that do not belong in the default visible flow

## Save Area

The bottom of the left panel should stay minimal.

Keep:

- status chip
- single save button

Add or preserve:

- short context text near the button explaining what is being saved

Example:

- `保存当前模式配置`
- context line: `当前正在编辑：Paper / 15m`

The save area should not compete with the form for attention.

## Existing Elements To Keep

The redesign should reuse and adapt these existing parts where possible:

- `cfgStatus`
- save button flow
- `strategyGuideCard`
- `advancedConfigPanel`
- existing strategy panel selection logic
- current paper profile backend payload structure

These pieces already embody useful behavior and do not need to be discarded just because the information architecture is changing.

## Existing Elements To Remove Or Demote

The redesign should eliminate or weaken these patterns:

- long mixed parameter groups where `paper` and `live` fields coexist
- one giant "运行模式" section that combines mode, credentials, redeem, and unrelated controls
- read-only paper profile summary cards that duplicate information without being the actual editing surface
- internal-feeling field presentation that reads like raw env structure instead of operator intent

## Interaction Rules

### Rule 1: One Primary Context At A Time

The mode dropdown decides whether the panel is in `Paper` or `Live`.

Only one mode's editing workflow should be visible at once.

### Rule 2: One Paper Timeframe Editor At A Time

In `Paper` mode:

- multiple timeframes may be enabled to run
- only one timeframe profile is visible for editing

This is essential for reducing clutter.

### Rule 3: Advanced Parameters Never Compete With Core Setup

Advanced parameters must remain easy to reach, but they should never dominate the initial paper or live workflow.

### Rule 4: Credentials Stay Visible In Live

Even though credentials are not frequently edited, they are operationally critical in `Live` mode and should remain in the default live layout instead of being hidden behind advanced settings.

## Suggested Visual Hierarchy

From top to bottom:

1. mode selector
2. context line
3. section title: step 1
4. section title: step 2
5. section title: step 3
6. advanced fold
7. save area

This hierarchy should make the left panel feel like a lightweight runbook rather than a database form.

## Technical Implications

The left panel should no longer be rendered as one generic group list with all keys treated equally.

Instead, rendering should become mode-aware and task-aware:

- shared shell at top
- dedicated paper taskflow renderer
- dedicated live taskflow renderer
- advanced section filtered per mode

The existing backend payload may still provide raw field metadata, but the frontend should interpret it through the new workflow structure rather than dumping all field groups directly.

## Acceptance Criteria

This redesign is successful when:

- the operator uses one dropdown to switch between `Paper` and `Live`
- `Paper` and `Live` no longer appear as one mixed configuration list
- in `Paper`, multiple timeframes may be enabled, but only one timeframe profile is edited at once
- in `Paper`, the visible default form is limited to run scope, strategy, and core parameters
- in `Live`, the visible default form is limited to market selection, credentials, and redeem/safety
- advanced parameters exist but are folded by default
- the left panel reads like an operator workflow rather than a raw parameter table
