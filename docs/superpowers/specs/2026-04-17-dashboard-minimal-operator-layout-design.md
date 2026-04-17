# Dashboard Minimal Operator Layout Design

**Date:** 2026-04-17

**Status:** Approved by user for implementation planning

## Goal

Simplify the dashboard so the operator sees only decision-critical information by default, while diagnostics and infrequently used controls remain available through folding or secondary views.

## Problem

The current dashboard has grown into a hybrid of:

- trading cockpit
- parameter editor
- diagnostics console
- onboarding/help center

That makes it informative, but also noisy. Multiple sections compete for attention even though only a subset is required during normal operation.

The user explicitly wants a more minimal layout. The issue is not missing functionality; it is that too many front-end elements are always visible, and the primary decision flow is diluted by secondary information.

## Confirmed User Preference

The user approved a “lightweight simplification” approach rather than a full dashboard redesign.

That means:

1. Keep the existing overall page skeleton.
2. Remove clearly unnecessary elements.
3. Fold advanced sections behind collapsible UI.
4. Merge overlapping information instead of adding new regions.
5. Preserve existing backend semantics and strategy/runtime behavior.

## Decision Summary

1. Keep the dashboard as a control console, not a stripped-down trading terminal.
2. Make the default view emphasize:
   - current round
   - quote state
   - final trade decision
   - paper summary
   - recent trades
   - save status
3. Remove elements that provide little day-to-day operator value.
4. Move diagnostics into collapsible sections.
5. Merge duplicated decision views so the operator reads one primary conclusion card instead of multiple competing cards.

## Information Hierarchy

### Tier 1: Always Visible

These are the only elements the operator should need during normal monitoring:

- current round / countdown / title
- quote prices
- final trade decision
- paper summary
- recent trades
- config save status
- key editable strategy parameters

### Tier 2: Summary Visible, Details Folded

These remain important, but should not consume permanent screen space:

- runtime mode state
- live readiness
- switch pending state
- WebSocket health
- strategy-specific diagnostics for strategy 6 and strategy 7

### Tier 3: On-Demand Reference

These should exist but stay out of the operator’s main path:

- full help center
- internal config keys
- verbose runtime details
- deep execution diagnostics

## Direct Removals

The following items should be removed from the always-visible interface:

- UTC clock
- config file path
- show internal key names button
- reload config button

Why:

- UTC clock is low-value for routine operator decisions because the rest of the dashboard is already localized and timestamped.
- Config path is debugging metadata, not operational decision support.
- Internal key toggling is an expert/debug feature that should not sit in the main control row.
- Reload config is too close in meaning to refresh/save and adds operator ambiguity.

## Folded Sections

The following should become collapsed-by-default sections or drawers:

- runtime mode card
- real-time connection status
- strategy 6 OFI diagnostics
- strategy 7 consensus diagnostics
- help center

### Folding Principle

If a section helps explain “why something happened” rather than “what should I do now,” it belongs behind an expansion control.

## Merges

### Merge `信号判断` and `下注计划与风控`

These two cards currently compete for attention while partially duplicating the decision story.

Replace them with:

- one primary decision card
- one collapsible signal-detail area

The primary card should show:

- whether to trade
- side
- entry price
- order cost
- skip reason

The collapsible detail area should show:

- open price
- current price
- threshold
- delta
- lock status

This keeps the operator focused on the final action, while still preserving explainability.

### Merge paper report strategy filters

`纸面交易汇总` and `最近交易明细` currently maintain separate strategy selectors.

For the minimal layout, use one shared report-level strategy filter by default.

Non-goal for this phase:

- do not delete the underlying independent filter state logic yet if that creates backend or testing churn

Implementation preference:

- one visible shared selector
- internal compatibility layer may keep separate state until later cleanup

## Runtime and Diagnostics Presentation

### Runtime Summary Chip

Keep one compact runtime summary visible with:

- current mode
- target mode
- pending switch state
- live readiness

Detailed runtime fields move into the folded runtime section.

### WebSocket Health

Keep one compact always-visible health indicator:

- normal
- warning
- stale guard triggered

The detailed rows move into the folded real-time connection section.

## Config Panel Simplification

### Keep Always Visible

The frequently changed strategy/risk controls remain visible:

- strategy panel
- target profit
- bet sizing mode
- base order cost
- max consecutive losses
- max stake
- min price threshold
- max price threshold

### Move To Advanced Parameters

The following become folded “advanced” fields:

- strategy 5 specific settings
- strategy 7 specific settings
- WebSocket fine-tuning
- live redeem configuration
- verbose runtime-related controls

This preserves capability while reducing scan cost.

## Layout Shape

Recommended default operator flow:

1. Top bar
   - local time
   - refresh
   - compact system status trigger

2. Main decision area
   - round / countdown
   - quote card
   - final decision card

3. Results area
   - shared report strategy filter
   - paper summary
   - recent trades

4. Config area
   - common strategy controls
   - advanced parameters collapsed
   - save action

5. Diagnostics area
   - runtime details
   - connection details
   - strategy 6/7 diagnostics
   - help

## Non-Goals

- No backend API redesign.
- No strategy-logic change.
- No removal of existing underlying diagnostics payloads.
- No migration to a completely new layout system.
- No reduction in test coverage for current runtime behavior.

## Risks

- Hiding too much could frustrate rare diagnostic workflows.
- Shared visible filter UI may create confusion if internal independent filter logic remains temporarily.
- Folding controls without strong labels may make advanced settings feel “missing.”

## Mitigations

- Use explicit section labels such as `高级参数`, `运行诊断`, `连接诊断`.
- Keep diagnostics accessible within one click.
- Preserve existing payload/state wiring behind the UI where helpful to reduce risk.
- Prefer progressive disclosure over hard removal except for the clearly low-value items.

## Recommendation

Implement the minimal operator layout as an incremental UI refinement:

- delete the obviously unnecessary items
- fold the advanced/diagnostic sections
- merge duplicated decision views
- keep core trading and reporting views always visible

This is the highest-confidence way to reduce clutter without destabilizing the dashboard.
