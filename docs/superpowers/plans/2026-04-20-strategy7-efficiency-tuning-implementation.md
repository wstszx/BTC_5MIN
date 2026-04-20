# Strategy 7 Efficiency Tuning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve strategy 7 order frequency modestly by allowing clearly strong consensus signals to pass the late-confirmation gate with a smaller timing requirement.

**Architecture:** Keep the change isolated to strategy-7 runtime timing logic in `trader.py`, with small config additions in `config.py` and focused regression tests in `tests/test_trader_runtime_and_live.py`. Do not alter OFI direction rules, momentum rules, or price-quality gates.

**Tech Stack:** Python 3, dataclasses, pytest.

---

## File Structure

- `D:/pythonProject/BTC_5MIN/config.py`
  - Add two strategy-7 timing-tuning config fields with neutral defaults.
- `D:/pythonProject/BTC_5MIN/trader.py`
  - Add a helper that detects stronger-than-threshold strategy-7 consensus signals.
  - Adjust the effective confirmation requirement only when the strong-signal fast path is enabled.
- `D:/pythonProject/BTC_5MIN/tests/test_trader_runtime_and_live.py`
  - Add focused red-green coverage for the new timing behavior.

### Task 1: Add Regression Tests For Strategy 7 Late Confirmation Relaxation

**Files:**
- Modify: `D:/pythonProject/BTC_5MIN/tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing tests**

Add tests that assert:

- ordinary strategy-7 signals still return `strategy7_entry_too_late`
- stronger strategy-7 signals pass when `STRATEGY7_LATE_CONFIRM_RELAX_SECONDS` is configured

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest tests/test_trader_runtime_and_live.py -k "strategy7 and late" -v`

Expected: FAIL because the new config and relaxed timing behavior do not exist yet.

### Task 2: Add Strategy 7 Timing Config Surface

**Files:**
- Modify: `D:/pythonProject/BTC_5MIN/config.py`

- [ ] **Step 1: Add neutral config fields**

Add:

- `strategy7_late_confirm_strong_signal_gap`
- `strategy7_late_confirm_relax_seconds`

- [ ] **Step 2: Keep defaults backward-compatible**

Set a conservative positive default for the strong-signal gap and `0` for relax seconds.

### Task 3: Implement The Minimal Runtime Fast Path

**Files:**
- Modify: `D:/pythonProject/BTC_5MIN/trader.py`

- [ ] **Step 1: Add a helper for stronger-than-threshold signals**

The helper should return true only when both OFI and momentum exceed their respective thresholds by the configured extra gap.

- [ ] **Step 2: Adjust the effective confirmation requirement**

When the helper returns true, subtract `strategy7_late_confirm_relax_seconds` from the effective confirmation window before applying the `entry_too_late` check.

- [ ] **Step 3: Keep all other strategy-7 gates unchanged**

Do not modify:

- OFI staleness rules
- momentum conflict rules
- price ceiling rules
- confidence gap rules

### Task 4: Verify Focused Behavior

**Files:**
- Modify: none

- [ ] **Step 1: Run focused runtime tests**

Run: `pytest tests/test_trader_runtime_and_live.py -k "strategy7 and late" -v`

Expected: PASS

- [ ] **Step 2: Run the broader strategy-7 suite**

Run: `pytest tests/test_trader_runtime_and_live.py -k strategy7 -v`

Expected: PASS
