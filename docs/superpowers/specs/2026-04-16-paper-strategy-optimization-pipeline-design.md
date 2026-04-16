# Paper Strategy Optimization Pipeline Design

## Summary

Design a conservative automatic strategy-optimization pipeline for this BTC prediction-game repository that improves long-run paper-trading profitability while controlling drawdown.

The key constraint is practical usefulness for the current product, not novelty. This design therefore avoids real-time self-modifying strategy behavior and instead builds a low-frequency, auditable loop:

1. generate offline parameter candidates from historical data
2. validate them with rolling windows
3. run a small challenger set in real paper trading against the current champion
4. recommend promotion only after the challenger proves itself under explicit rules

## Goals

- Increase long-run paper-trading net profitability without accepting uncontrolled drawdown.
- Reuse the repository's existing strengths: `backtest.py`, `strategy_research.py`, and multi-strategy paper trading in `trader.py`.
- Make optimization outputs traceable and reviewable instead of opaque.
- Separate offline candidate discovery from online paper validation.
- Support low-frequency parameter refreshes such as daily optimization, not constant retuning.

## Non-Goals

- No reinforcement learning.
- No automatic invention of brand-new strategy logic.
- No online parameter updates every few rounds.
- No direct auto-promotion into live trading.
- No fully autonomous strategy switching without operator review in the first version.
- No broad ML platform buildout that exceeds the current repository's needs.

## Why This Is Useful For The Current Game

This repository already has three assets that make a practical optimization loop worthwhile:

- historical backtesting via [backtest.py](D:/python/BTC_5MIN/backtest.py)
- parameter-grid style research via [strategy_research.py](D:/python/BTC_5MIN/strategy_research.py)
- simultaneous multi-strategy paper trading via [trader.py](D:/python/BTC_5MIN/trader.py)

What is missing is the orchestration layer that turns those pieces into a disciplined decision process. The first version should therefore optimize existing strategies rather than create new ones.

## Product Principle

The system should optimize only when it can produce evidence that a candidate is better than the current paper-trading baseline.

This means:

- offline research can nominate candidates
- only real paper-trading comparison can make a candidate promotable
- promotion remains conservative

The pipeline should behave like an assistant that proposes upgrades, not like a black-box trader that mutates itself continuously.

## Optimization Scope

### Strategies In Scope

The first version should focus on existing configurable strategies that have meaningful thresholds:

- Strategy 5 momentum
- Strategy 6 Binance OFI
- optionally common sizing/risk parameters shared by both

Pattern strategies 1-4 may still be included in ranking as benchmarks, but the primary optimization value is in tunable threshold-based strategies.

### Parameters Worth Optimizing First

Recommended first-wave search space:

- Common:
  - `TARGET_PROFIT`
  - `MAX_PRICE_THRESHOLD`
  - `MAX_CONSECUTIVE_LOSSES`
- Strategy 5:
  - `SIGNAL_MOMENTUM_THRESHOLD`
  - `SIGNAL_DYNAMIC_THRESHOLD_K`
  - `SIGNAL_LOCK_BEFORE_ENTRY_SECONDS`
- Strategy 6:
  - `OFI_THRESHOLD`
  - `MAX_ENTRY_PRICE`

These parameters matter to profitability and drawdown, and they already exist in the current runtime/config model.

## Architecture

The pipeline should be split into four layers.

### 1. Candidate Generation

An offline optimizer explores parameter combinations using historical data. It should build on [strategy_research.py](D:/python/BTC_5MIN/strategy_research.py) rather than replace it wholesale.

Responsibilities:

- enumerate or sample candidate parameter sets
- run each candidate through research/backtest evaluation
- produce ranked candidates

### 2. Walk-Forward Validation

Candidates should not be accepted based on one full-history result. Instead, they should be evaluated across rolling train/validation windows.

Responsibilities:

- split historical rows into rolling windows
- rank candidates by validation stability, not just one aggregate result
- reject candidates that only win in a narrow slice of history

### 3. Paper Challenger Evaluation

Top offline candidates become paper challengers. These challengers run alongside the current champion in real paper trading.

Responsibilities:

- assign candidate configurations to paper challenger slots
- compare challenger results with the active champion over a meaningful sample
- keep the challenger pool small

### 4. Promotion Decision

Promotion policy converts performance evidence into action recommendations.

Responsibilities:

- classify candidates as `candidate`, `challenger`, `promotable`, or `rejected`
- enforce minimum sample sizes
- require profitability and drawdown checks
- produce a recommendation payload for review

## Proposed Modules

The first version can stay file-oriented and consistent with the current repository style.

- `optimizer.py`
  - candidate generation and offline search orchestration
- `walk_forward.py`
  - rolling window splitting and validation scoring
- `paper_evaluator.py`
  - compare champion vs challengers from paper logs/state
- `promotion_policy.py`
  - promotion decisions and status transitions
- `optimizer_state.json`
  - persisted optimization state under `logs/` or another runtime-safe path

This keeps each unit focused:

- search
- validate
- evaluate live paper evidence
- decide

## Data Flow

1. Historical round data is loaded from exported CSV.
2. Offline optimization generates candidate parameter sets.
3. Walk-forward validation filters unstable candidates.
4. Top candidates are written into a challenger pool.
5. Paper runtime runs champion and challengers concurrently.
6. Paper logs and session state are evaluated periodically.
7. Promotion policy emits recommendation states.

The crucial separation is:

- historical data chooses what is worth testing
- current paper trading decides what is worth trusting

## Scoring Philosophy

The repository should not optimize for `total_pnl` alone. The score needs to reward profitable and stable candidates while penalizing drawdown, fragility, and excessive capital demand.

### Candidate Scoring Inputs

Recommended metrics:

- total paper/backtest PnL
- max drawdown
- profitable validation windows
- trade count
- maximum single-order cost
- recommended bankroll
- hit rate

### Candidate Score Shape

The exact formula can evolve, but the first version should follow this principle:

- reward profitability
- reward stability across windows
- penalize drawdown
- penalize excessive bankroll requirements
- penalize too-small sample sizes

The system should reject "lucky but fragile" candidates.

## Promotion Policy

### States

Suggested candidate lifecycle:

- `candidate`
  - passed offline search and entered validation
- `challenger`
  - currently running in real paper-trading comparison
- `promotable`
  - passed challenger thresholds and is ready for operator review
- `rejected`
  - failed offline stability or paper comparison

### Promotion Requirements

A challenger should only become `promotable` when it meets minimum evidence requirements such as:

- minimum paper trade count
- net PnL better than champion by a configurable margin
- max drawdown not materially worse than champion
- no severe degradation over recent days
- no reliance on very sparse trades

The first version should stop at recommendation. Operators can then choose whether to adopt the challenger config.

## Cadence

This pipeline should be intentionally low-frequency.

Recommended cadence:

- run offline optimization daily
- refresh challenger evaluation every few hours or daily
- only allow promotion after a multi-day observation window

Not recommended:

- per-round retuning
- intraday continuous retuning

For this prediction game, low-frequency adaptation is far more useful than hyper-reactive optimization.

## Integration With Existing Runtime

The first version should integrate around the current paper-trading architecture rather than rework it.

### Runtime Reuse

Use the existing multi-strategy paper-trading runtime in [trader.py](D:/python/BTC_5MIN/trader.py) for simultaneous evaluation.

This may require extending how paper challengers are represented, because current multi-strategy support is strategy-ID oriented rather than parameter-bundle oriented. The design should therefore treat "challenger" as a higher-level paper experiment identity that may share a base strategy ID but differ in config values.

### Configuration Reuse

The pipeline should reuse `AppConfig`-compatible fields so candidate configs can be materialized as normal runtime settings.

### Dashboard Reuse

The dashboard should eventually surface:

- current champion
- active challengers
- challenger status
- paper comparison summary
- promotion recommendation

This is useful because the operator already uses the dashboard as the runtime control center.

## Version 1 Scope

### In Scope

- offline candidate generation
- walk-forward validation
- challenger pool persistence
- paper challenger evaluation
- promotion recommendations
- operator-facing visibility of candidate/challenger status

### Out of Scope

- live trading auto-promotion
- automatic live capital allocation
- strategy-code mutation
- model training infrastructure
- large-scale distributed optimization

## Suggested Delivery Order

The first version should be built in this order:

1. walk-forward validation around existing research code
2. candidate/challenger state model and persistence
3. paper challenger evaluation from current logs/state
4. promotion policy and recommendation output
5. dashboard/reporting view

This ordering ensures each step is independently useful and reduces the chance of building a large but fragile system.

## Risks

### Risk: Overfitting historical data

Mitigation:

- require walk-forward validation
- require real paper challenger evidence
- avoid direct auto-promotion from offline results

### Risk: Too many challengers create noise

Mitigation:

- keep challenger pool intentionally small
- only advance top candidates from validation

### Risk: Parameter churn harms comparability

Mitigation:

- low-frequency updates
- explicit promotion states
- recommendation-first, not auto-switch

### Risk: Strategy 5 historical momentum snapshots may be degraded

Mitigation:

- preserve the current overlap-ratio warning behavior
- penalize degraded signal quality in ranking
- prefer paper challenger verification before trust

## Testing Strategy

The implementation should be covered in layers:

- unit tests for walk-forward splitting and promotion policy
- regression tests for offline candidate scoring
- integration tests for challenger evaluation on paper logs
- dashboard payload tests for challenger/promotable visibility

## Files Likely To Change

New files:

- `optimizer.py`
- `walk_forward.py`
- `paper_evaluator.py`
- `promotion_policy.py`

Likely modified:

- `strategy_research.py`
- `trader.py`
- `dashboard.py`
- `config.py`
- tests covering the new modules and dashboard/runtime integration

## Recommendation

The most useful first version for this repository is not a self-modifying AI trader. It is a disciplined, conservative optimization assistant that:

- searches parameter candidates offline
- validates them across rolling windows
- tests only the strongest few in paper trading
- recommends promotion only after evidence

That gives the project a real optimization loop without sacrificing interpretability or risk control.
