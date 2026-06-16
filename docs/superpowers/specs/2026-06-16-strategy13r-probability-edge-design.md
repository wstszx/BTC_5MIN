# Strategy 13R Probability Edge Design

## Purpose

Add Strategy 13R as a paper/shadow research strategy for BTC up/down markets.

Strategy 13R should test whether a probability-first, fee-aware model produces a better decision surface than signal-first strategies. It must not be treated as live-ready by default. Its first purpose is calibration: when it estimates a side has a high enough probability, later settlement results should let us measure whether that probability was realistic after fees, delay, and execution filters.

## Scope

The first implementation adds Strategy 13R to the existing strategy/profile/runtime path so it can be included in paper strategy lists and paper reports.

The first implementation does not automatically add Strategy 13R to live strategy lists, does not change existing live defaults, and does not promote the strategy automatically.

## Core Decision Model

Strategy 13R decides in this order:

1. Anchor the round with the Binance BTC price at or near round start.
2. Read the current Binance BTC price and remaining seconds in the market window.
3. Estimate the probability that BTC finishes above or below the round anchor.
4. Read the current Polymarket buy price for UP and DOWN.
5. Convert each candidate price into an effective cost using the existing crypto taker fee model.
6. Compute edge:

   ```text
   edge = model_probability - effective_buy_price - edge_buffer
   ```

7. Choose the side with the highest positive edge only if it also clears the minimum probability and minimum edge gates.
8. Apply microstructure confirmation as a safety filter or penalty, not as the primary direction source.
9. Skip when required data is stale, unavailable, too noisy, too late, or too expensive.

## Probability Estimation

Strategy 13R reuses the Strategy 11 concept of estimating finish probability from:

- round anchor BTC price
- current BTC price
- remaining time
- volatility per square-root minute

Unlike Strategy 11's fixed volatility setting, Strategy 13R should support dynamic volatility derived from recent Binance mid-price observations. If the first implementation cannot reliably obtain enough recent observations from the runtime, it may fall back to a configured volatility value, but the skip or diagnostic reason must make the fallback visible.

Probability must be shrunk toward 0.5 before edge checks to reduce overconfidence:

```text
shrunk_probability = 0.5 + (raw_probability - 0.5) * (1 - probability_shrink)
```

The model must clamp volatility between configured minimum and maximum bounds.

## Microstructure Confirmation

Microstructure inputs include Binance depth/OFI and Polymarket signal momentum where available.

They do not directly select the trading side. They may:

- allow the probability edge unchanged when they agree with the model side
- subtract a configured edge penalty when mixed or weak
- skip when explicitly conflicting and confirmation is required
- skip when stale or unavailable and confirmation is required

The default should be conservative. If confirmation is enabled and the micro signal strongly disagrees with the model side, Strategy 13R should skip.

## Entry And Execution Constraints

Strategy 13R should respect the existing trade plan and runtime safeguards:

- configured minimum entry price
- configured maximum entry price
- minimum stake
- maximum stake
- maximum consecutive losses
- entry timing and grace windows
- stale Binance signal protection
- paper/live profile isolation

The first recommended max entry price is `0.54` raw price. Edge checks must use effective price after fee.

## Configuration

Add Strategy 13R configuration with conservative defaults:

```env
STRATEGY_13_MIN_EDGE=0.04
STRATEGY_13_EDGE_BUFFER=0.005
STRATEGY_13_VOL_LOOKBACK_SECONDS=180
STRATEGY_13_VOL_MIN_BPS=8
STRATEGY_13_VOL_MAX_BPS=45
STRATEGY_13_PROBABILITY_SHRINK=0.25
STRATEGY_13_MIN_PROBABILITY=0.58
STRATEGY_13_MAX_ENTRY_PRICE=0.54
STRATEGY_13_CONFIRM_MICRO=true
STRATEGY_13_MICRO_DISAGREE_PENALTY=0.02
STRATEGY_13_CONFIRM_BEFORE_ENTRY_SECONDS=2
```

These values are research defaults, not live recommendations.

## Logging And Diagnostics

Strategy 13R should populate existing trade log fields first:

- `signal_probability`: selected side model probability after shrink
- `signal_edge`: final edge after fee, buffer, and micro penalty
- `signal_threshold`: configured minimum edge
- `signal_delta`: BTC price distance from anchor, or a normalized distance if that is more consistent with existing strategy fields
- `signal_reason`: diagnostic reason such as volatility fallback, micro confirmation result, or selected model summary

If existing fields are insufficient for calibration, add narrowly scoped CSV fields in a backward-compatible way. Candidate fields are:

- `model_volatility_bps`
- `model_effective_cost`
- `model_raw_probability`

The first implementation should avoid broad dashboard redesign. Dashboard display can continue showing the existing probability and edge fields.

## Skip Reasons

Use clear Strategy 13R-specific reasons:

- `strategy13_btc_anchor_unavailable`
- `strategy13_btc_price_unavailable`
- `strategy13_btc_price_stale`
- `strategy13_volatility_unavailable`
- `strategy13_probability_too_low`
- `strategy13_edge_too_low`
- `strategy13_micro_unavailable`
- `strategy13_micro_conflict`
- `strategy13_price_too_low`
- `strategy13_price_too_high`
- `strategy13_entry_too_late`

Skip reasons should identify the first decisive gate that failed.

## Testing

Add focused tests for:

- probability increases for UP when current BTC rises above anchor
- probability increases for DOWN when current BTC falls below anchor
- probability moves toward 0.5 as volatility increases
- probability becomes more extreme as remaining time decreases with nonzero distance
- volatility clamp and probability shrink are applied
- fee-adjusted effective cost is used for edge
- insufficient edge skips
- minimum probability skips
- micro disagreement penalty or conflict skip behavior
- Strategy 13R config/profile parsing
- Strategy 13R can resolve a paper side decision without changing live defaults

## Promotion Gate

Strategy 13R is not live-ready after implementation.

A later promotion review should require calibration and profitability evidence from paper/shadow logs. At minimum:

- enough executed paper trades to avoid one-session noise
- bucketed calibration showing predicted probabilities broadly match outcomes
- positive average edge after fees
- no severe drawdown cluster from a single market condition
- manual review of skip reasons and model diagnostics

Until those conditions are met, Strategy 13R should remain paper-only.

