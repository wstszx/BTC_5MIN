# Strategy 7 Frequency Optimization Evaluation

- Generated: 2026-05-13 10:16:53 +0800
- Source log: `logs\paper\5m\paper_trades.csv`
- Result cache: `logs\analysis\strategy7_slug_results_20260506_20260513.json`
- Strategy 7 rows: 1511, executed: 119, skipped: 1392

## Main Findings

- Existing executed orders do not yet prove a stable positive edge on normalized unit ROI.
- Frequency should not be raised by weakening OFI/conflict/hot-momentum gates first.
- The safest frequency lever is earlier/tighter runtime timing, because `entry_too_late` rows already passed signal quality before the timing gate.
- Raising max entry price above 0.54 looks risky; 0.56 materially increases candidates but historical normalized ROI remains weak.

## Scenario Summary

| Scenario | Trades | Win Rate | Avg Price | Unit ROI Sum | Avg Unit ROI | Main Reasons |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Existing executed orders | 119 | 42.0% | 0.508 | -21.349 | -0.179 | executed:103, entry_window_missed:16 |
| Added by price cap 0.54 from `price_too_high` | 0 | 0.0% | 0.000 | 0.000 | 0.000 |  |
| Added by price cap 0.56 from `price_too_high` | 15 | 33.3% | 0.554 | -5.942 | -0.396 | strategy7_price_too_high:15 |
| Added by timing fix with candidate price <=0.54 | 46 | 54.3% | 0.521 | 1.871 | 0.041 | strategy7_entry_too_late:46 |
| Added by timing fix with candidate price <=0.56 | 82 | 51.2% | 0.534 | -3.267 | -0.040 | strategy7_entry_too_late:82 |
| Today only: timing fix with candidate price <=0.54 | 3 | 66.7% | 0.530 | 0.792 | 0.264 | strategy7_entry_too_late:3 |
| Today only: timing fix with candidate price <=0.56 | 11 | 63.6% | 0.544 | 1.900 | 0.173 | strategy7_entry_too_late:11 |

## Existing Executed Orders By Day

| Local Day | Trades | Win Rate | Avg Price | Unit ROI Sum | Avg Unit ROI |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-05-06 | 10 | 30.0% | 0.502 | -4.010 | -0.401 |
| 2026-05-07 | 6 | 16.7% | 0.522 | -4.077 | -0.679 |
| 2026-05-08 | 52 | 44.2% | 0.506 | -7.480 | -0.144 |
| 2026-05-09 | 33 | 48.5% | 0.512 | -2.371 | -0.072 |
| 2026-05-10 | 10 | 40.0% | 0.492 | -1.111 | -0.111 |
| 2026-05-11 | 2 | 50.0% | 0.535 | -0.148 | -0.074 |
| 2026-05-12 | 6 | 33.3% | 0.525 | -2.152 | -0.359 |
| 2026-05-13 | 0 | 0.0% | 0.000 | 0.000 | 0.000 |

## Skip Reason Mix

- `strategy7_ofi_too_weak`: 633
- `strategy7_price_too_high`: 274
- `strategy7_entry_too_late`: 170
- `strategy7_signal_conflict`: 134
- `strategy7_momentum_too_hot`: 82
- `strategy7_momentum_too_weak`: 55
- `strategy7_ofi_stale`: 22
- `strategy7_confidence_too_low`: 21
- `strategy7_price_too_low`: 1

## Recommended Next Experiment

Run a paper-only challenger that changes frequency by timing first, not by signal weakening:

```env
STRATEGY7_OFI_THRESHOLD=0.62
STRATEGY7_MOMENTUM_THRESHOLD=0.008
STRATEGY7_MAX_MOMENTUM_DELTA=0.10
STRATEGY7_MIN_SIGNAL_GAP=0.015
MAX_ENTRY_PRICE=0.54
STRATEGY7_MAX_ENTRY_PRICE=0.54
STRATEGY7_LATE_CONFIRM_RELAX_SECONDS=2
FAST_POLL_INTERVAL_SECONDS=1
NEAR_ENTRY_POLL_WINDOW_SECONDS=20
```

Promotion gate: do not consider live unless the next paper sample reaches at least 40 executed trades with normalized avg unit ROI > 0 and no single day below -0.15 avg unit ROI.

## Notes

- For skipped non-price rows, DOWN candidate price is approximated as `1 - current_up_price` because the log does not store the contemporaneous DOWN ask. Treat those rows as directional diagnostics, not exact fill backtests.
- `price_too_high` rows include the actual candidate price from the runtime log, so their price analysis is stronger.
