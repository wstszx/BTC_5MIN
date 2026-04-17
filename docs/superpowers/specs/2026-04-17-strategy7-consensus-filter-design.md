# Strategy 7 Consensus Filter Design

**Date:** 2026-04-17

**Status:** Approved by user for planning

## Goal

Add a new `strategy_id=7` that trades much less often than strategies 5 and 6, but aims for higher win quality and lower drawdown by requiring cross-signal agreement before opening a position.

## Problem

The current strategy set has three practical categories:

- Strategies `1-4`: deterministic round-pattern baselines with no market-information input
- Strategy `5`: same-market momentum signal based on Polymarket intraround `UP` price change
- Strategy `6`: Binance depth-based OFI signal with threshold and staleness filtering

Strategies 5 and 6 are both more scientific than the fixed pattern baselines, but each still acts on a single signal source. That leaves the runtime vulnerable to noisy one-source moves, false positives near thresholds, and avoidable low-quality trades.

The user wants a better strategy that optimizes for all three of these outcomes together:

1. Higher long-run profitability
2. Lower drawdown and better stability
3. Fewer bad trades, even if total trade count drops materially

When those objectives conflict, the user explicitly prefers: fewer trades, better win quality, and lower drawdown.

## Decision Summary

Add `strategy_id=7` as a strict consensus strategy:

1. Use Binance OFI as the primary market-structure signal.
2. Use Polymarket intraround momentum as the confirmation signal.
3. Require both signals to be present, timely, strong enough, and directionally aligned.
4. Apply an extra quality filter before allowing entry.
5. Default to `SKIP` whenever signal quality is ambiguous.

This makes strategy 7 a selective, high-confidence strategy instead of a frequent-entry strategy.

## Strategy Definition

### High-Level Behavior

Strategy 7 only trades when all of the following are true:

- OFI signal exists
- OFI signal is not stale
- OFI absolute value exceeds a minimum threshold
- Polymarket momentum signal exists
- Momentum absolute value exceeds a minimum threshold
- OFI direction and momentum direction agree
- Entry timing is still early enough
- Entry price is still acceptable under a strategy-7-specific quality gate
- Signal strength is not merely threshold-touching but exceeds a minimum confidence gap

If any check fails, strategy 7 returns `SKIP`.

### Direction Rules

- `UP` only when `ofi_score >= strategy7_ofi_threshold` and `momentum_delta >= strategy7_momentum_threshold`
- `DOWN` only when `ofi_score <= -strategy7_ofi_threshold` and `momentum_delta <= -strategy7_momentum_threshold`
- `SKIP` for all mixed, weak, stale, missing, or late cases

### Philosophy

Strategy 7 is intentionally conservative:

- It does not try to recover weak signals through fallback patterns.
- It does not try to infer direction when only one signal source is strong.
- It prefers skipping over forcing low-confidence entries.

That matches the user’s stated preference for improved trade quality over trade count.

## Detailed Signal Flow

### Step 1: OFI Gate

Strategy 7 starts with the same OFI source used by strategy 6:

- read `quote.strategy6_ofi_score`
- reject missing OFI
- reject stale OFI
- reject weak OFI below threshold

If OFI fails, stop immediately with `SKIP`.

### Step 2: Momentum Gate

Then evaluate the same-market momentum input used by strategy 5:

- resolve round open `UP` price
- resolve current `UP` price
- compute `momentum_delta = current_up - open_up`
- reject missing or invalid momentum context
- reject momentum whose absolute value is below threshold

If momentum fails, stop with `SKIP`.

### Step 3: Direction Agreement Gate

Only continue when both signals point the same way:

- OFI positive + momentum positive -> candidate `UP`
- OFI negative + momentum negative -> candidate `DOWN`
- any disagreement -> `SKIP`

This is the core consensus behavior.

### Step 4: Quality Filter Gate

Even after agreement, strategy 7 still rejects low-quality entries:

- reject entries too close to planned entry time
- reject entries above a stricter strategy-7 price ceiling
- reject entries where both signals only barely exceed the threshold

This final gate is how strategy 7 trades less than strategies 5 and 6 while trying to improve stability.

## New Config Surface

Add the following operator-facing config values:

- `STRATEGY7_OFI_THRESHOLD`
- `STRATEGY7_MOMENTUM_THRESHOLD`
- `STRATEGY7_MAX_ENTRY_PRICE`
- `STRATEGY7_MIN_SIGNAL_GAP`
- `STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS`

### Semantics

- `STRATEGY7_OFI_THRESHOLD`
  Minimum absolute OFI strength required before strategy 7 will consider a trade.

- `STRATEGY7_MOMENTUM_THRESHOLD`
  Minimum absolute intraround Polymarket `UP` price movement required for confirmation.

- `STRATEGY7_MAX_ENTRY_PRICE`
  Strategy-7-specific entry ceiling. This should be equal to or stricter than global price gating.

- `STRATEGY7_MIN_SIGNAL_GAP`
  Minimum amount by which each signal must exceed its threshold. This prevents barely-qualified trades.

- `STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS`
  Minimum remaining time before planned entry required for strategy 7 to act. If confirmation arrives too late, strategy 7 skips.

### Defaults

Initial defaults should be conservative rather than aggressive. The first release should bias toward under-trading and let the optimizer tune upward later if justified.

## Runtime Changes

### `strategy.py`

Keep `strategy.py` as the simple pure-signal library for deterministic cases and add a minimal strategy 7 branch only if the pure helper remains readable. If strategy 7 requires too much context, keep most of its logic in `trader.py` and leave `strategy.py` focused on low-context helpers.

### `trader.py`

`_resolve_side_from_strategy()` becomes the main execution path for strategy 7.

Add a new branch after strategy 6 handling:

- resolve OFI score
- resolve momentum open/current values
- compute direction agreement
- evaluate quality filters
- return a `SideDecision` with either:
  - `UP`
  - `DOWN`
  - `None` plus a strategy-7-specific reason

### State Usage

Strategy 7 can reuse the existing round-level momentum state used by strategy 5 rather than inventing a parallel state model, but it should not reuse weak-signal fallback semantics.

## Dashboard Experience

### Strategy Catalog

Add strategy 7 to the strategy catalog with copy that makes its behavior clear:

- label: consensus / confirmation strategy
- summary: Binance OFI plus Polymarket momentum must agree
- preview: OFI + MOMENTUM + FILTER + SKIP

### Config Panel

Expose the five new config keys in the dashboard under either:

- the same signal/risk groups as strategies 5 and 6, or
- one dedicated strategy-7-only subgroup if that reads more clearly

Recommended choice: keep them inside existing signal/risk sections and mark them as `strategy_7_only` to avoid another large config block.

### Market Panel

When strategy 7 is selected, surface these operator-facing diagnostics:

- OFI score
- momentum delta
- agreement status
- quality gate status
- final skip or entry reason

This keeps the strategy explainable and debuggable.

## Skip Reasons

Add explicit reasons so logs, dashboard, and future analysis can distinguish failure modes:

- `strategy7_ofi_unavailable`
- `strategy7_ofi_stale`
- `strategy7_ofi_too_weak`
- `strategy7_momentum_unavailable`
- `strategy7_momentum_too_weak`
- `strategy7_signal_conflict`
- `strategy7_entry_too_late`
- `strategy7_price_too_high`
- `strategy7_confidence_too_low`

## Optimizer Integration

Do not over-expand the first optimizer search space.

First-pass optimizer support should tune only:

- `STRATEGY7_OFI_THRESHOLD`
- `STRATEGY7_MOMENTUM_THRESHOLD`
- `STRATEGY7_MAX_ENTRY_PRICE`

Leave `MIN_SIGNAL_GAP` and `CONFIRM_BEFORE_ENTRY_SECONDS` fixed at conservative defaults in the first release. That reduces overfitting risk and keeps candidate generation manageable.

## Validation Standard

Strategy 7 should only be considered better than strategies 5 or 6 if it improves the quality profile, not just raw PnL.

Primary evaluation dimensions:

1. Out-of-sample total PnL
2. Max drawdown
3. Average PnL per executed trade
4. Win rate
5. Trade count

Interpretation rule:

- If strategy 7 earns slightly less total PnL but materially lowers drawdown and raises per-trade quality, that still counts as a valid success for the user’s preference.

## Risks

- Too many filters could reduce trade count so much that results lose statistical meaning.
- Shared use of strategy-5 momentum infrastructure could accidentally inherit fallback behavior if not isolated carefully.
- Dashboard complexity could grow if strategy-7-only diagnostics are scattered across unrelated sections.
- Optimizer candidate explosion is likely if too many strategy-7 parameters are tuned in the first release.

## Non-Goals

- No machine-learning classifier in v1.
- No probability model in v1.
- No regime-switching controller in v1.
- No change to strategies 1-6 beyond any small refactoring needed to keep the code readable.

## Recommendation

Implement strategy 7 first as a strict rule-based consensus strategy.

That gives the project:

- a more scientific signal structure than fixed-pattern strategies
- better explainability than a model-first approach
- lower implementation risk
- a cleaner path to future optimizer and model upgrades
