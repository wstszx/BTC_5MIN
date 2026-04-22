# Strategy 5/6 Timeframe Parameter Playbook

## Purpose

This note captures practical starting presets for Strategy 5 and Strategy 6 under BTC `5m` and `15m` markets.

The goal is not to claim a final optimum. These profiles are intended as paper-trading baselines that can later be refined from skip-reason mix, win rate, average PnL per trade, and drawdown.

## Strategy 5 Presets

### Summary

Strategy 5 is the most timeframe-sensitive momentum strategy in the current system.

- `5m` needs a stricter momentum threshold because short rounds are noisier.
- `15m` can tolerate a slightly lower threshold because intraround moves have more time to develop.

### Comparison

| Parameter | 5m Draft | 15m Draft |
| --- | ---: | ---: |
| `SIGNAL_MOMENTUM_THRESHOLD` | `0.020` | `0.015` |
| `SIGNAL_LOCK_BEFORE_ENTRY_SECONDS` | `10` | `20` |
| `SIGNAL_FALLBACK_STRATEGY_ID` | `2` | `2` |
| `OPEN_DELAY_SECONDS` | `12` | `25` |
| `MAX_PRICE_THRESHOLD` | `0.60` | `0.65` |
| `TARGET_PROFIT` | `0.8` | `1.0` |

### Notes

- `5m` uses a higher momentum threshold to reduce false positives.
- `5m` enters earlier because the usable signal window is shorter.
- `5m` keeps a tighter max-entry-price ceiling to reduce chasing.
- `15m` remains more tolerant because signals have more time to stabilize.

### First Adjustment Rule

If the `5m` paper run shows too many momentum weak-signal skips without meaningful trade volume, reduce `SIGNAL_MOMENTUM_THRESHOLD` gradually from `0.020` to `0.018` before changing other fields.

## Strategy 6 Presets

### Summary

Strategy 6 depends on Binance OFI strength and freshness.

- `5m` requires a stronger and fresher OFI signal.
- `15m` can use the more relaxed baseline because the strategy has more room to wait for confirmation quality.

### Comparison

| Parameter | 5m Draft | 15m Draft |
| --- | ---: | ---: |
| `OFI_THRESHOLD` | `0.72` | `0.65` |
| `BINANCE_SIGNAL_STALE_SECONDS` | `1.0` | `2.0` |
| `OPEN_DELAY_SECONDS` | `10` | `25` |
| `MAX_ENTRY_PRICE` | `0.54` | `0.56` |
| `TARGET_PROFIT` | `0.8` | `1.0` |

### Notes

- `5m` raises the OFI threshold because shallow short-term imbalance is more likely to be noise.
- `5m` shortens signal staleness tolerance because the book can change quickly.
- `5m` also tightens price control and enters sooner.
- `15m` can keep the looser baseline because signal life is longer.

### First Adjustment Rule

If the `5m` paper run shows too many `ofi_stale` skips, first verify data freshness and runtime timing. Only if the feed quality is healthy should `BINANCE_SIGNAL_STALE_SECONDS` be widened slightly, for example from `1.0` to `1.2`.

Do not lower `OFI_THRESHOLD` first unless trade quality remains strong and stale behavior is already under control.

## Recommended Rollout Order

Use this order when adding timeframe-specific presets to the dashboard or manual testing workflow:

1. Strategy 5 `5m/15m`
2. Strategy 6 `5m/15m`
3. Strategy 7 `5m/15m`
4. Shared execution presets for Strategies 1-4

## Operator Guidance

- Compare `5m` and `15m` by quality metrics, not only by raw trade count.
- For Strategy 5, watch `signal_too_weak_skip`, average PnL per trade, and win rate.
- For Strategy 6, watch `ofi_too_weak`, `ofi_stale`, average PnL per trade, and drawdown.
- Keep changes small and isolated. Avoid changing threshold, timing, and price controls all at once unless a paper sample clearly supports it.
