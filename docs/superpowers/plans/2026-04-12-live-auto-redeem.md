# Live Auto Redeem Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically redeem resolved winning live positions back into `USDC.e` available balance without changing paper trading behavior or blocking the live trading loop.

**Architecture:** Add a live-only redeem worker that runs alongside the existing live trading worker. The worker discovers `redeemable=true` positions through the official positions API, groups work by `conditionId`, executes chain-side `redeemPositions()` calls, and persists redeem state separately so retries, restart recovery, and dashboard visibility stay deterministic.

**Tech Stack:** Python, existing runtime manager/worker model, Polymarket data API, Polygon CTF contract interaction, JSON state persistence, pytest.

---

## File Map

**Modify:**
- `config.py`
  Responsibility: add live-only auto-redeem configuration flags with safe defaults.
- `main.py`
  Responsibility: start and stop the redeem worker only when the active runtime mode is `live`.
- `trader.py`
  Responsibility: add position discovery, redeem state management, and redeem worker loop while keeping live order placement logic isolated.
- `polymarket_api.py`
  Responsibility: add read-only support for fetching current positions from the official data API if no better local abstraction exists yet.
- `dashboard.py`
  Responsibility: optionally expose redeem runtime status and recent redeem attempts after the core worker is working.
- `tests/test_trader_runtime_and_live.py`
  Responsibility: cover redeem worker behavior, state persistence, retry logic, and live-only gating.
- `tests/test_runtime_launcher.py`
  Responsibility: verify the runtime manager starts and stops the redeem worker only in live mode.
- `tests/test_dashboard.py`
  Responsibility: cover any dashboard surface added for redeem visibility.

**Create:**
- `logs/live_redeem_state.json` (runtime artifact, not committed)
  Responsibility: track per-`conditionId` redeem attempts, tx hashes, retry timing, and terminal status.

---

### Task 1: Add Live Auto Redeem Config

**Files:**
- Modify: `config.py`
- Test: `tests/test_runtime_launcher.py`

- [ ] **Step 1: Write the failing test**

Add a config-focused test that builds `AppConfig` from env values and expects these live-only fields to exist with defaults:
- `live_auto_redeem_enabled == False`
- `live_auto_redeem_poll_seconds == 20`
- `live_auto_redeem_max_retries == 6`
- `live_auto_redeem_initial_backoff_seconds == 30`
- `live_auto_redeem_max_backoff_seconds == 300`
- `live_auto_redeem_dry_run == False`

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_runtime_launcher.py -q -k auto_redeem`
Expected: FAIL because the config fields do not exist yet.

- [ ] **Step 3: Write minimal implementation**

Add dataclass fields in `config.py` with live-only naming and safe defaults. Keep them independent from paper config and avoid changing existing live order settings.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_runtime_launcher.py -q -k auto_redeem`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_runtime_launcher.py
git commit -m "feat: add live auto redeem config"
```

### Task 2: Add Positions Discovery Helper

**Files:**
- Modify: `polymarket_api.py`
- Modify: `trader.py`
- Test: `tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing test**

Add a focused test that stubs the positions API response and expects a helper to return only live positions where:
- `redeemable == true`
- `size > 0`
- `conditionId` exists
- the row belongs to the configured `live_funder` or wallet address query target

Also verify rows expose enough fields for downstream execution:
- `conditionId`
- `eventSlug`
- `outcome`
- `size`
- `redeemable`

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_trader_runtime_and_live.py -q -k redeemable_positions`
Expected: FAIL because no helper exists.

- [ ] **Step 3: Write minimal implementation**

Add one read-only API helper in `polymarket_api.py` for current positions by user, and one normalizer in `trader.py` that filters redeemable rows. Keep this logic side-effect free.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_trader_runtime_and_live.py -q -k redeemable_positions`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add polymarket_api.py trader.py tests/test_trader_runtime_and_live.py
git commit -m "feat: add live redeemable position discovery"
```

### Task 3: Define Redeem State Persistence

**Files:**
- Modify: `trader.py`
- Test: `tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing test**

Add tests for a dedicated redeem-state loader/saver that:
- defaults to an empty state when `live_redeem_state.json` is missing
- persists per-`conditionId` entries
- preserves `status`, `attempt_count`, `last_attempt_at`, `next_attempt_at`, `last_tx_hash`, and `event_slug`
- survives restart/reload without losing retry scheduling

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_trader_runtime_and_live.py -q -k redeem_state`
Expected: FAIL because no redeem-state helpers exist.

- [ ] **Step 3: Write minimal implementation**

Add a small JSON-backed redeem state model in `trader.py` and keep it separate from `live_session_state.json` so trade settlement and redeem retries stay decoupled.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_trader_runtime_and_live.py -q -k redeem_state`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader.py tests/test_trader_runtime_and_live.py
git commit -m "feat: persist live redeem state"
```

### Task 4: Build Redeem Execution Adapter

**Files:**
- Modify: `trader.py`
- Test: `tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing test**

Add tests for a redeem executor abstraction that takes:
- `condition_id`
- `event_slug`
- `index_sets=[1, 2]`
- dry-run flag

The tests should expect:
- binary markets always use `indexSets=[1,2]`
- successful calls return a tx hash or dry-run marker
- duplicate or terminally-failed conditions are not re-submitted immediately

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_trader_runtime_and_live.py -q -k redeem_executor`
Expected: FAIL because execution logic does not exist yet.

- [ ] **Step 3: Write minimal implementation**

Add a redeem execution adapter in `trader.py` with a narrow interface. For now, isolate chain interaction behind one helper so implementation can later switch between `py-clob-client`-adjacent support and direct Web3 contract calls without rewriting worker logic.

Implementation notes for the worker:
- only binary markets are in scope
- use `parentCollectionId = bytes32(0)`
- use `indexSets=[1,2]`
- record tx hash on submission
- support `live_auto_redeem_dry_run`

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_trader_runtime_and_live.py -q -k redeem_executor`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader.py tests/test_trader_runtime_and_live.py
git commit -m "feat: add live redeem executor"
```

### Task 5: Add Retry and Terminal Error Policy

**Files:**
- Modify: `trader.py`
- Test: `tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing test**

Add tests that simulate:
- transient RPC/network failure -> retry with backoff
- already redeemed / no redeemable balance -> terminal state
- repeated transient failures -> capped retry count then waiting state

Verify `next_attempt_at` and `attempt_count` update deterministically.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_trader_runtime_and_live.py -q -k redeem_retry`
Expected: FAIL because retry scheduling is not implemented yet.

- [ ] **Step 3: Write minimal implementation**

Implement retry scheduling helpers in `trader.py` using the new config values. Keep retry decisions local to the redeem worker and do not let them affect live trade settlement.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_trader_runtime_and_live.py -q -k redeem_retry`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader.py tests/test_trader_runtime_and_live.py
git commit -m "feat: add live redeem retry policy"
```

### Task 6: Add Live Redeem Worker Loop

**Files:**
- Modify: `trader.py`
- Test: `tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing test**

Add a worker-loop test that verifies:
- worker exits immediately in paper mode
- worker skips when auto redeem disabled
- worker polls positions on the configured interval in live mode
- worker submits redeem only for due `redeemable=true` positions
- worker does not block on the main live settlement state file

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_trader_runtime_and_live.py -q -k live_redeem_worker`
Expected: FAIL because no worker exists.

- [ ] **Step 3: Write minimal implementation**

Add `run_live_redeem_worker(...)` to `trader.py`. It should:
- validate live mode and required private key/funder
- load/save `logs/live_redeem_state.json`
- query redeemable positions for the configured user
- submit due redeems serially
- sleep based on `live_auto_redeem_poll_seconds`
- honor `stop_event`

Keep the worker independent from `run_live_trading()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_trader_runtime_and_live.py -q -k live_redeem_worker`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader.py tests/test_trader_runtime_and_live.py
git commit -m "feat: add live redeem worker"
```

### Task 7: Wire the Worker into Runtime Startup

**Files:**
- Modify: `main.py`
- Test: `tests/test_runtime_launcher.py`

- [ ] **Step 1: Write the failing test**

Add runtime-launcher tests that verify:
- live mode starts the redeem worker alongside live trading
- paper mode does not start it
- shutdown stops it cleanly
- worker failure propagates like other runtime workers without leaving orphan threads

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_runtime_launcher.py -q -k redeem_worker`
Expected: FAIL because runtime startup does not know about the redeem worker.

- [ ] **Step 3: Write minimal implementation**

Update `main.py` so live mode starts a third worker: `live-redeem-worker`. Keep paper mode unchanged. Reuse the existing worker lifecycle pattern and stop-event handling.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_runtime_launcher.py -q -k redeem_worker`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_runtime_launcher.py
git commit -m "feat: wire live redeem worker into runtime"
```

### Task 8: Add Minimal Observability

**Files:**
- Modify: `dashboard.py`
- Modify: `trader.py`
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

Add dashboard asset/state tests expecting a small live-only runtime surface for auto redeem, for example:
- worker enabled/disabled
- last redeem attempt time
- latest tx hash or latest result
- pending redeem count

Do not design a large new panel; keep it to one small runtime card or rows under live status.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dashboard.py -q -k redeem`
Expected: FAIL because dashboard payloads do not include redeem runtime data yet.

- [ ] **Step 3: Write minimal implementation**

Expose a compact redeem runtime payload from the redeem worker state and render it in the dashboard only when live mode is active or redeem is enabled.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_dashboard.py -q -k redeem`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard.py trader.py tests/test_dashboard.py
git commit -m "feat: show live auto redeem runtime status"
```

### Task 9: Full Regression and Manual Dry-Run Checklist

**Files:**
- Modify: `docs/operations_runbook.md`
- Modify: `docs/dashboard_runbook.md`
- Test: existing suites only

- [ ] **Step 1: Write the failing doc/test expectation**

Add or update a doc-oriented test only if the repo already enforces one. Otherwise skip test-first here and keep this task documentation-only.

- [ ] **Step 2: Run targeted regression**

Run:
```bash
pytest tests/test_trader_runtime_and_live.py tests/test_runtime_launcher.py tests/test_dashboard.py -q
```
Expected: PASS.

- [ ] **Step 3: Run broader regression**

Run:
```bash
pytest tests/test_config_encoding.py tests/test_trader_runtime_and_live.py tests/test_dashboard.py tests/test_runtime_launcher.py -q
```
Expected: PASS.

- [ ] **Step 4: Update docs**

Document:
- new live auto redeem config flags
- dry-run mode
- redeem worker logs/state file
- operational caveat that redeem affects available balance recovery but does not decide trade settlement

- [ ] **Step 5: Commit**

```bash
git add docs/operations_runbook.md docs/dashboard_runbook.md
git commit -m "docs: add live auto redeem operations guidance"
```

### Task 10: Final Verification Before Merge

**Files:**
- Modify: none unless fixes are required
- Test: all touched suites

- [ ] **Step 1: Run final verification**

Run:
```bash
pytest tests/test_config_encoding.py tests/test_trader_runtime_and_live.py tests/test_dashboard.py tests/test_runtime_launcher.py -q
```
Expected: PASS.

- [ ] **Step 2: Run a live dry-run smoke check**

Run a non-ordering smoke path in a safe environment with live config loaded and auto redeem dry-run enabled. Confirm logs show redeem discovery without sending transactions.

Suggested command shape:
```bash
$env:LIVE_AUTO_REDEEM_ENABLED='true'
$env:LIVE_AUTO_REDEEM_DRY_RUN='true'
python main.py
```
Expected: runtime starts, live worker stays stable, redeem worker logs discovery/retry decisions only.

- [ ] **Step 3: Review git diff for safety boundaries**

Confirm:
- no paper trading behavior changed
- redeem logic is live-only
- no live order path waits on redeem completion
- no duplicate redeem submissions are possible after restart

- [ ] **Step 4: Commit any last fixes**

```bash
git add <files>
git commit -m "fix: polish live auto redeem integration"
```

- [ ] **Step 5: Prepare for integration**

Use the finishing workflow only after tests and dry-run verification are complete.
