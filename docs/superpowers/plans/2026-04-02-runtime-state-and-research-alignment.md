# Runtime State And Research Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore correct runtime state progression for live trading, reset daily loss state on day rollover, and align strategy research sizing with real FIXED_BASE_COST behavior.

**Architecture:** Extend `SessionState` with minimal pending-live-trade fields so one-shot `live-trade` runs can settle the previous round before planning the next one; normalize session day using the project’s existing `+08:00` operations convention; and reuse runtime sizing assumptions inside `strategy_research.py` so offline research matches paper/live execution.

**Tech Stack:** Python, pytest, existing `trader.py`/`models.py`/`strategy_research.py` flow

---

### Task 1: Add Regression Tests

**Files:**
- Modify: `D:/python/BTC_5MIN/tests/test_trader_runtime_and_live.py`
- Modify: `D:/python/BTC_5MIN/tests/test_strategy_research.py`

- [ ] Add a failing live-trade test proving a previously submitted pending trade is settled and its loss state is applied before the next run.
- [ ] Add a failing live-trade/day-reset test proving `daily_realized_pnl` is reset when the stored day rolls over.
- [ ] Add a failing research test proving FIXED_BASE_COST candidates use `base_order_cost`, not `target_profit`, as the starting stake.

### Task 2: Implement Runtime State Fixes

**Files:**
- Modify: `D:/python/BTC_5MIN/models.py`
- Modify: `D:/python/BTC_5MIN/trader.py`

- [ ] Add minimal pending-live-trade fields to `SessionState`.
- [ ] Add helpers to compute the current session day in `+08:00`, reset daily state on rollover, and settle a stored pending live trade when its round is resolved.
- [ ] Update `place_live_order()` to settle pending state before planning a new trade and to persist newly submitted live trade details.
- [ ] Update paper-trading runtime to refresh `current_day`/`daily_realized_pnl` before risk checks.

### Task 3: Implement Research Alignment

**Files:**
- Modify: `D:/python/BTC_5MIN/strategy_research.py`

- [ ] Make FIXED_BASE_COST simulation use `cfg.base_order_cost` for the base stake so research matches runtime sizing.

### Task 4: Verify

**Files:**
- Modify: `D:/python/BTC_5MIN/tests/test_trader_runtime_and_live.py`
- Modify: `D:/python/BTC_5MIN/tests/test_strategy_research.py`
- Modify: `D:/python/BTC_5MIN/models.py`
- Modify: `D:/python/BTC_5MIN/trader.py`
- Modify: `D:/python/BTC_5MIN/strategy_research.py`

- [ ] Run focused trader/runtime and strategy-research tests and confirm they fail first, then pass.
- [ ] Run the full test suite and confirm no regressions.
