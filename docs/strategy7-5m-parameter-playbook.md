# Strategy 7 5m Parameter Playbook

## Purpose

This note captures two Strategy 7 parameter profiles for the BTC `5m` market.

- Profile A keeps Strategy 7 selective while preserving enough paper-trading samples.
- Profile B is more defensive and is meant for noisy `5m` conditions.
- Neither profile changes the current `15m` runtime by itself.

## Profile Comparison

| Parameter | Profile A: 5m High Quality | Profile B: 5m Ultra Conservative |
| --- | ---: | ---: |
| `MARKET_TIMEFRAME` | `5m` | `5m` |
| `OPEN_DELAY_SECONDS` | `12` | `15` |
| `SIGNAL_LOCK_BEFORE_ENTRY_SECONDS` | `10` | `12` |
| `STRATEGY7_OFI_THRESHOLD` | `0.58` | `0.62` |
| `STRATEGY7_MOMENTUM_THRESHOLD` | `0.008` | `0.010` |
| `STRATEGY7_MAX_ENTRY_PRICE` | `0.54` | `0.53` |
| `STRATEGY7_MIN_SIGNAL_GAP` | `0.015` | `0.020` |
| `STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS` | `2` | `2` |
| `STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP` | `0.035` | `0.040` |
| `STRATEGY7_LATE_CONFIRM_RELAX_SECONDS` | `2` | `1` |

## Recommended Starting Point

Start with Profile A.

Why:

- `5m` needs shorter timing gates than the current `15m` setup.
- Profile A still keeps Strategy 7 selective instead of turning it into a frequent-entry system.
- It is more likely to generate enough paper trades to judge quality.

## When To Switch From A To B

Move from Profile A to Profile B if one or more of these patterns persist across a meaningful paper sample:

- `strategy7_signal_conflict` increases materially and starts dominating skip reasons.
- Win rate drops clearly versus the current `15m` Strategy 7 baseline.
- Average PnL per executed trade declines while trade count rises only modestly.
- Entries feel too expensive for `5m` and `strategy7_price_too_high` becomes a recurring quality concern.

## Operator Guidance

- Do not compare `5m` trade count directly against `15m` trade count without also comparing average PnL per trade and drawdown.
- For `5m`, quality metrics matter more than raw frequency because short-round noise is higher.
- Avoid relaxing OFI and momentum thresholds first. For `5m`, adjust timing only after reviewing skip-reason mix and per-trade quality.

## Copy-Paste Blocks

### Profile A

```env
MARKET_TIMEFRAME=5m
OPEN_DELAY_SECONDS=12
SIGNAL_LOCK_BEFORE_ENTRY_SECONDS=10
STRATEGY7_OFI_THRESHOLD=0.58
STRATEGY7_MOMENTUM_THRESHOLD=0.008
STRATEGY7_MAX_ENTRY_PRICE=0.54
STRATEGY7_MIN_SIGNAL_GAP=0.015
STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS=2
STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP=0.035
STRATEGY7_LATE_CONFIRM_RELAX_SECONDS=2
```

### Profile B

```env
MARKET_TIMEFRAME=5m
OPEN_DELAY_SECONDS=15
SIGNAL_LOCK_BEFORE_ENTRY_SECONDS=12
STRATEGY7_OFI_THRESHOLD=0.62
STRATEGY7_MOMENTUM_THRESHOLD=0.010
STRATEGY7_MAX_ENTRY_PRICE=0.53
STRATEGY7_MIN_SIGNAL_GAP=0.020
STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS=2
STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP=0.040
STRATEGY7_LATE_CONFIRM_RELAX_SECONDS=1
```
