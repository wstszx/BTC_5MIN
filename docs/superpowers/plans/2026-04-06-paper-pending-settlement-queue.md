# Paper Pending Settlement Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let paper trading continue participating in new rounds without waiting for previous rounds to resolve.

**Architecture:** Extend `SessionState` with a persisted pending-paper queue, enqueue frozen paper trade snapshots at entry, and opportunistically settle pending rounds on each main-loop iteration. Keep the implementation single-threaded and preserve one final CSV row per entered paper round.

**Tech Stack:** Python 3.12, dataclasses, existing Polymarket client/runtime loop, pytest

---

### Task 1: Add persisted pending-paper queue models

**Files:**
- Modify: `D:/pythonProject/BTC_5MIN/models.py`
- Test: `D:/pythonProject/BTC_5MIN/tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing test**

Add a test that creates a `SessionState` with pending paper items, serializes via `asdict`, writes to disk, and reloads through `load_session_state()` from `trader.py`. Assert the pending queue survives round-trip.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_trader_runtime_and_live.py -q`
Expected: FAIL because `SessionState` does not yet support pending paper queue payloads.

- [ ] **Step 3: Write minimal implementation**

In `models.py`:
- add a dataclass for a frozen pending paper trade item
- add `pending_paper_trades` to `SessionState` with a safe default factory list

In `trader.py` if needed:
- update `load_session_state()` to hydrate raw dicts into pending-paper dataclass items
- keep backward compatibility when old state files do not contain the new field

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_trader_runtime_and_live.py -q`
Expected: PASS for the new round-trip coverage.

- [ ] **Step 5: Commit**

```bash
git add models.py trader.py tests/test_trader_runtime_and_live.py
git commit -m "feat: persist pending paper trades in session state"
```

### Task 2: Freeze paper trade snapshots at entry

**Files:**
- Modify: `D:/pythonProject/BTC_5MIN/trader.py`
- Test: `D:/pythonProject/BTC_5MIN/tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing test**

Add a test that simulates a tradable paper round and asserts that after entry:
- the trade is queued into `state.pending_paper_trades`
- `round_index` increments immediately
- the loop does not block until settlement before returning control to subsequent polling logic

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_trader_runtime_and_live.py -q`
Expected: FAIL because current implementation blocks until round-end settlement.

- [ ] **Step 3: Write minimal implementation**

In `trader.py`:
- add helper to build a frozen pending paper item from `target_round`, `plan`, `side_decision`, and current config metadata
- before queueing, guard against duplicate `event_slug`
- replace the current ?entered trade; waiting for settlement? serial block with:
  - queue pending item
  - increment `round_index`
  - persist session state
  - continue main loop without blocking on round end

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_trader_runtime_and_live.py -q`
Expected: PASS for immediate queueing behavior.

- [ ] **Step 5: Commit**

```bash
git add trader.py tests/test_trader_runtime_and_live.py
git commit -m "feat: queue paper trades instead of blocking on settlement"
```

### Task 3: Settle pending paper trades from frozen snapshots

**Files:**
- Modify: `D:/pythonProject/BTC_5MIN/trader.py`
- Test: `D:/pythonProject/BTC_5MIN/tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing test**

Add tests that cover:
- a pending paper trade settles correctly once metadata is available
- settlement uses frozen `price/order_size/order_cost/expected_profit`, not recomputed plan from later state
- unresolved rounds remain in queue without raising fatal runtime errors

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_trader_runtime_and_live.py -q`
Expected: FAIL because settlement currently rebuilds the plan from the current `SessionState` and current loop flow.

- [ ] **Step 3: Write minimal implementation**

In `trader.py`:
- add helper to derive a frozen `TradePlan` from a pending paper item
- add helper to settle one pending paper trade
- add helper to iterate and settle all resolvable pending paper trades at the start of each loop iteration
- update session-state persistence after any settlement batch
- preserve unresolved items in queue

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_trader_runtime_and_live.py -q`
Expected: PASS for frozen settlement behavior and unresolved-queue retention.

- [ ] **Step 5: Commit**

```bash
git add trader.py tests/test_trader_runtime_and_live.py
git commit -m "feat: settle pending paper trades from frozen snapshots"
```

### Task 4: Preserve one final CSV row per entered round

**Files:**
- Modify: `D:/pythonProject/BTC_5MIN/trader.py`
- Test: `D:/pythonProject/BTC_5MIN/tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing test**

Add a test that runs through queued paper entry plus later settlement and asserts:
- only one final trade row is written for the entered round
- the row contains frozen entry values and final result fields
- skip rows still behave as before

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_trader_runtime_and_live.py -q`
Expected: FAIL because logging currently assumes immediate settlement in the same control flow.

- [ ] **Step 3: Write minimal implementation**

In `trader.py`:
- ensure entered paper trades are not logged immediately as final settled rows
- emit the final CSV row only when a pending item is actually settled
- keep skip logging paths unchanged

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_trader_runtime_and_live.py -q`
Expected: PASS for one-row-per-entered-round behavior.

- [ ] **Step 5: Commit**

```bash
git add trader.py tests/test_trader_runtime_and_live.py
git commit -m "fix: log paper trades only after final settlement"
```

### Task 5: Cover restart recovery and loop-through behavior

**Files:**
- Modify: `D:/pythonProject/BTC_5MIN/tests/test_trader_runtime_and_live.py`
- Modify: `D:/pythonProject/BTC_5MIN/trader.py`

- [ ] **Step 1: Write the failing test**

Add integration-style tests that verify:
- pending paper trades survive restart via `session_state.json`
- after restart, resolvable pending trades settle before or during ongoing loop processing
- a later round can still be evaluated while an earlier one remains unresolved

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_trader_runtime_and_live.py -q`
Expected: FAIL because restart-aware pending settlement flow is incomplete.

- [ ] **Step 3: Write minimal implementation**

In `trader.py`:
- ensure startup loop processes loaded `pending_paper_trades`
- settle resolvable items at each iteration before fresh round selection
- keep unresolved items in place until future polls

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_trader_runtime_and_live.py -q`
Expected: PASS for restart recovery and non-blocking participation behavior.

- [ ] **Step 5: Commit**

```bash
git add trader.py tests/test_trader_runtime_and_live.py
git commit -m "test: cover pending paper recovery across restart"
```

### Task 6: Full verification pass

**Files:**
- Modify: `D:/pythonProject/BTC_5MIN/tests/test_trader_runtime_and_live.py` if any assertion cleanup is still needed
- Verify: `D:/pythonProject/BTC_5MIN/trader.py`
- Verify: `D:/pythonProject/BTC_5MIN/models.py`

- [ ] **Step 1: Run targeted trader tests**

Run: `pytest tests/test_trader_runtime_and_live.py -q`
Expected: all targeted trader/runtime tests pass.

- [ ] **Step 2: Run dashboard regression smoke tests**

Run: `pytest tests/test_dashboard.py -q`
Expected: PASS, proving unrelated dashboard behavior remains intact.

- [ ] **Step 3: Run combined regression pass**

Run: `pytest tests/test_trader_runtime_and_live.py tests/test_dashboard.py -q`
Expected: full targeted pass with 0 failures.

- [ ] **Step 4: Manual runtime smoke check**

Run: `py .\main.py`
Expected: runtime starts, paper loop logs current rounds, and when settlement lags it no longer blocks future rounds from being evaluated.

- [ ] **Step 5: Commit**

```bash
git add trader.py models.py tests/test_trader_runtime_and_live.py tests/test_dashboard.py
git commit -m "feat: allow paper trading to continue while prior rounds await settlement"
```
