# Strategy 7 Late Confirm Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tighten strategy 7 late-confirmation relaxation so it only applies to final-quality signals, and align runtime, backtest, and research behavior.

**Architecture:** Reuse the existing strategy-7 confidence gate as the prerequisite for late-confirmation relaxation, then share the same strong-signal timing decision across `trader.py`, `backtest.py`, and `strategy_research.py`. Add regression coverage first so runtime and offline research stay behaviorally consistent.

**Tech Stack:** Python, pytest

---

### Task 1: Add failing regression tests for late-confirm gating

**Files:**
- Modify: `D:\python\BTC_5MIN\tests\test_trader_runtime_and_live.py`
- Modify: `D:\python\BTC_5MIN\tests\test_backtest.py`
- Modify: `D:\python\BTC_5MIN\tests\test_strategy_research.py`

- [ ] **Step 1: Write the failing tests**

Add tests that assert strategy 7 still skips late entries when the signal only clears the relaxed late-confirm threshold but fails the final `strategy7_min_signal_gap` quality gate, and that backtest/research accept the same late entry once both the quality gate and relaxation are satisfied.

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `pytest tests/test_trader_runtime_and_live.py -k "strategy7 and late" -v`
Expected: FAIL because runtime late-confirm relaxation still activates before the final quality gate.

Run: `pytest tests/test_backtest.py tests/test_strategy_research.py -k "strategy7 and late_confirm" -v`
Expected: FAIL because backtest and research do not yet model strategy-7 late-confirm relaxation.

### Task 2: Implement aligned strategy-7 late-confirm behavior

**Files:**
- Modify: `D:\python\BTC_5MIN\trader.py`
- Modify: `D:\python\BTC_5MIN\backtest.py`
- Modify: `D:\python\BTC_5MIN\strategy_research.py`

- [ ] **Step 1: Tighten runtime gating**

Require the runtime late-confirm fast path to pass `strategy7_signal_gap_ok()` before applying any relaxation seconds, then keep the existing strong-signal margin as an additional check.

- [ ] **Step 2: Reuse the same decision in offline paths**

Add a small shared helper path in `backtest.py` and `strategy_research.py` so historical strategy-7 timing checks use the same quality-plus-strong-signal rule before relaxing the effective confirmation window.

- [ ] **Step 3: Run targeted tests to verify they pass**

Run: `pytest tests/test_trader_runtime_and_live.py -k "strategy7 and late" -v`
Expected: PASS

Run: `pytest tests/test_backtest.py tests/test_strategy_research.py -k "strategy7 and late_confirm" -v`
Expected: PASS

### Task 3: Run broader safety checks

**Files:**
- Modify: `D:\python\BTC_5MIN\tests\test_backtest.py`
- Modify: `D:\python\BTC_5MIN\tests\test_strategy_research.py`
- Modify: `D:\python\BTC_5MIN\tests\test_trader_runtime_and_live.py`

- [ ] **Step 1: Re-run strategy-7 suites**

Run: `pytest tests/test_trader_runtime_and_live.py -k strategy7 -v`
Expected: PASS

Run: `pytest tests/test_backtest.py -k strategy7 -v`
Expected: PASS

Run: `pytest tests/test_strategy_research.py -k strategy_7 -v`
Expected: PASS
