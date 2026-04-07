# Max Stake Skip Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent repeated `MAX_STAKE` sizing skips from trapping the runtime in a non-recovering state, and keep paper/live skip-state handling aligned.

**Architecture:** Keep sizing decisions in `build_trade_plan()`, but centralize the runtime-side state transition that happens after a skipped round. Both paper and live should use the same helper to update streak counters, optionally apply a stop-loss reset, and advance round state after a real post-entry skip.

**Tech Stack:** Python, pytest, dataclasses, existing runtime/session state.

---

### Task 1: Add regression tests for repeated max-stake skips

**Files:**
- Modify: `D:\python\BTC_5MIN\tests\test_trader_runtime_and_live.py`
- Test: `D:\python\BTC_5MIN\tests\test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_place_live_order_resets_state_after_repeated_max_stake_skips(tmp_path):
    ...

def test_run_paper_trading_resets_state_after_repeated_max_stake_skips(tmp_path):
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_trader_runtime_and_live.py -q`
Expected: FAIL because live/paper skip handling does not reset `recovery_loss`, does not bump `stop_loss_count`, and live does not advance `round_index`.

- [ ] **Step 3: Write minimal implementation**

```python
def _apply_skipped_trade_state(...):
    ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_trader_runtime_and_live.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_trader_runtime_and_live.py trader.py docs/superpowers/specs/2026-04-07-max-stake-skip-recovery-design.md docs/superpowers/plans/2026-04-07-max-stake-skip-recovery.md
git commit -m "fix(trader): recover from repeated max stake skips"
```

### Task 2: Unify skip-state transitions between paper and live

**Files:**
- Modify: `D:\python\BTC_5MIN\trader.py`
- Test: `D:\python\BTC_5MIN\tests\test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing test**

```python
def test_live_and_paper_skip_paths_apply_same_state_transition(...):
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_trader_runtime_and_live.py -q`
Expected: FAIL because live and paper mutate `round_index` / `stop_loss_count` differently on the same skip reason.

- [ ] **Step 3: Write minimal implementation**

```python
def _handle_skipped_trade_after_entry(...):
    should_alert = _update_max_stake_skip_streak(...)
    if plan.stop_loss_triggered or state.consecutive_max_stake_skips >= cfg.max_consecutive_losses:
        state = reset_after_stop_loss(state)
    state.round_index += 1
    return state, should_alert
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_trader_runtime_and_live.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader.py tests/test_trader_runtime_and_live.py
git commit -m "refactor(trader): unify risk-gate skip state handling"
```

### Task 3: Full verification

**Files:**
- Modify: none
- Test: `D:\python\BTC_5MIN\tests\test_trader_runtime_and_live.py`

- [ ] **Step 1: Run targeted runtime tests**

Run: `pytest -q tests/test_trader_runtime_and_live.py -q`
Expected: PASS

- [ ] **Step 2: Run full suite**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 3: Commit if needed**

```bash
git add trader.py tests/test_trader_runtime_and_live.py
git commit -m "test(trader): cover repeated max stake skip recovery"
```
