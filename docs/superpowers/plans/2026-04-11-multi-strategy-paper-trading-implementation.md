# Multi-Strategy Paper Trading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow paper trading to run multiple selected strategies in one runtime and let the dashboard configure and inspect paper results by strategy.

**Architecture:** Keep live trading single-strategy, but make paper trading resolve an effective ordered strategy list from `PAPER_STRATEGY_IDS` with backward-compatible fallback to `STRATEGY_ID`. Replace single paper session state with a per-strategy state map, then make the paper runtime iterate shared market data through isolated per-strategy workers and write all paper trades into the existing shared CSV. Extend the dashboard to edit the multi-select paper strategy config and to filter paper summaries and recent rows by strategy without disturbing existing live-mode behavior.

**Tech Stack:** Python 3.12, dataclasses, existing runtime loop in `trader.py`, built-in dashboard HTTP server, pytest

---

## File Map

- `D:/pythonProject/BTC_5MIN/config.py`
  Adds parsing and normalization for `PAPER_STRATEGY_IDS`, plus helpers that expose the effective paper strategy list while preserving `STRATEGY_ID` compatibility.

- `D:/pythonProject/BTC_5MIN/models.py`
  Introduces per-strategy paper session containers so each paper strategy keeps isolated round counters, pnl, signal state, and pending settlement queue.

- `D:/pythonProject/BTC_5MIN/trader.py`
  Loads old and new paper session formats, runs one paper loop across multiple strategy workers, persists per-strategy state, and logs each settled paper trade with the correct `strategy` value.

- `D:/pythonProject/BTC_5MIN/dashboard.py`
  Exposes `PAPER_STRATEGY_IDS` in config payloads, validates and saves multi-select values, and adds strategy-aware filtering for paper summary and recent trade payloads.

- `D:/pythonProject/BTC_5MIN/tests/test_trader_runtime_and_live.py`
  Covers backward-compatible paper state loading and multi-strategy runtime behavior in the shared paper loop.

- `D:/pythonProject/BTC_5MIN/tests/test_dashboard.py`
  Covers dashboard config payload, dashboard config persistence, paper summary filtering, and recent trade filtering.

- `D:/pythonProject/BTC_5MIN/tests/test_config_encoding.py`
  Keeps a small focused place for config parsing coverage if `PAPER_STRATEGY_IDS` parsing is easiest to verify outside runtime tests.

### Task 1: Add paper multi-strategy config parsing

**Files:**
- Modify: `D:/pythonProject/BTC_5MIN/config.py`
- Test: `D:/pythonProject/BTC_5MIN/tests/test_config_encoding.py`

- [ ] **Step 1: Write the failing test**

Add config parsing tests that verify:

```python
def test_build_config_uses_paper_strategy_ids_when_present():
    cfg = build_config_from_env_values({STRATEGY_ID: 2, PAPER_STRATEGY_IDS: 6,2,6,1})
    assert cfg.strategy_id == 2
    assert cfg.paper_strategy_ids == [6, 2, 1]


def test_build_config_falls_back_to_strategy_id_for_paper():
    cfg = build_config_from_env_values({STRATEGY_ID: 5})
    assert cfg.paper_strategy_ids == [5]
```

Also cover invalid entries such as `6,x,9,2` so the parser ignores unsupported items and preserves stable order for supported strategy ids `1` through `6`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_encoding.py -q`
Expected: FAIL because `AppConfig` does not yet expose `paper_strategy_ids` or normalize `PAPER_STRATEGY_IDS`.

- [ ] **Step 3: Write minimal implementation**

In `config.py`:

```python
def _env_strategy_list(name: str) -> list[int]:
    ...


@dataclass(slots=True)
class AppConfig:
    strategy_id: int = ...
    paper_strategy_ids: list[int] = field(default_factory=list)
```

Implement a helper that:
- splits `PAPER_STRATEGY_IDS` on commas
- trims whitespace
- keeps only strategy ids `1` through `6`
- de-duplicates while preserving first-seen order
- falls back to `[strategy_id]` when the env var is unset or normalizes to an empty list

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_encoding.py -q`
Expected: PASS for all new `PAPER_STRATEGY_IDS` parsing coverage.

- [ ] **Step 5: Commit**
Run: git add config.py tests/test_config_encoding.py
Run: git commit -m feat: parse paper strategy id lists

### Task 2: Add per-strategy paper session state

**Files:**
- Modify: `D:/pythonProject/BTC_5MIN/models.py`
- Modify: `D:/pythonProject/BTC_5MIN/trader.py`
- Test: `D:/pythonProject/BTC_5MIN/tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing test**
Add tests for loading legacy single paper state into the effective selected strategy, loading new `paper_strategies` state with multiple entries, and creating defaults for missing selected strategies.

- [ ] **Step 2: Run test to verify it fails**
Run: `pytest tests/test_trader_runtime_and_live.py -q`
Expected: FAIL because paper state is still single-strategy.

- [ ] **Step 3: Write minimal implementation**
Add a per-strategy paper sub-state dataclass and a `paper_strategies` map on `SessionState`. Update `load_session_state()` to hydrate legacy and new formats.

- [ ] **Step 4: Run test to verify it passes**
Run: `pytest tests/test_trader_runtime_and_live.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
Run: git add models.py trader.py tests/test_trader_runtime_and_live.py
Run: git commit -m feat: add per-strategy paper session state

### Task 3: Run multiple paper strategies in one loop

**Files:**
- Modify: `D:/pythonProject/BTC_5MIN/trader.py`
- Test: `D:/pythonProject/BTC_5MIN/tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing test**
Add a runtime test with `paper_strategy_ids=[1, 6]` and assert both strategies run in one loop, one strategy can skip while the other trades, pending settlement in one strategy does not block the other, and logged rows keep the right `strategy`.

- [ ] **Step 2: Run test to verify it fails**
Run: `pytest tests/test_trader_runtime_and_live.py -q`
Expected: FAIL because runtime still uses one global `cfg.strategy_id`.

- [ ] **Step 3: Write minimal implementation**
Refactor the paper loop to iterate `cfg.paper_strategy_ids`, clone `cfg` per strategy, share market data where safe, and keep each strategy state isolated.

- [ ] **Step 4: Run test to verify it passes**
Run: `pytest tests/test_trader_runtime_and_live.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
Run: git add trader.py tests/test_trader_runtime_and_live.py
Run: git commit -m feat: run multiple paper strategies in one loop

### Task 4: Add dashboard paper strategy multi-select

**Files:**
- Modify: `D:/pythonProject/BTC_5MIN/dashboard.py`
- Test: `D:/pythonProject/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing test**
Add tests that verify config payload includes `PAPER_STRATEGY_IDS`, options are `1` through `6`, `update_config()` persists a normalized comma-separated value, and invalid values return validation feedback.

- [ ] **Step 2: Run test to verify it fails**
Run: `pytest tests/test_dashboard.py -q`
Expected: FAIL because dashboard config does not know `PAPER_STRATEGY_IDS` yet.

- [ ] **Step 3: Write minimal implementation**
Add `PAPER_STRATEGY_IDS` to editable keys, labels, help text, config mapping, and normalization logic. Reuse existing UI patterns where possible.

- [ ] **Step 4: Run test to verify it passes**
Run: `pytest tests/test_dashboard.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
Run: git add dashboard.py tests/test_dashboard.py
Run: git commit -m feat: add dashboard paper strategy multi-select

### Task 5: Filter paper results by strategy

**Files:**
- Modify: `D:/pythonProject/BTC_5MIN/dashboard.py`
- Test: `D:/pythonProject/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing test**
Add tests that verify `get_paper_summary_payload(strategy=6)` recalculates summary from only strategy 6 rows, `get_recent_trades_payload(limit=20, strategy=1)` filters both pending and settled rows, and default `all` preserves aggregate behavior.

- [ ] **Step 2: Run test to verify it fails**
Run: `pytest tests/test_dashboard.py -q`
Expected: FAIL because paper results are still aggregated across all strategies.

- [ ] **Step 3: Write minimal implementation**
Extend summary and recent-trade payload builders to accept `all` or a single strategy id, filtering before sorting and before aggregate calculation.

- [ ] **Step 4: Run test to verify it passes**
Run: `pytest tests/test_dashboard.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
Run: git add dashboard.py tests/test_dashboard.py
Run: git commit -m feat: filter paper dashboard results by strategy

### Task 6: Focused verification pass

**Files:**
- Verify: `D:/pythonProject/BTC_5MIN/config.py`
- Verify: `D:/pythonProject/BTC_5MIN/models.py`
- Verify: `D:/pythonProject/BTC_5MIN/trader.py`
- Verify: `D:/pythonProject/BTC_5MIN/dashboard.py`
- Verify: `D:/pythonProject/BTC_5MIN/tests/test_config_encoding.py`
- Verify: `D:/pythonProject/BTC_5MIN/tests/test_trader_runtime_and_live.py`
- Verify: `D:/pythonProject/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Run config tests**
Run: `pytest tests/test_config_encoding.py -q`
Expected: PASS.

- [ ] **Step 2: Run trader tests**
Run: `pytest tests/test_trader_runtime_and_live.py -q`
Expected: PASS.

- [ ] **Step 3: Run dashboard tests**
Run: `pytest tests/test_dashboard.py -q`
Expected: PASS.

- [ ] **Step 4: Run combined targeted regression**
Run: `pytest tests/test_config_encoding.py tests/test_trader_runtime_and_live.py tests/test_dashboard.py -q`
Expected: PASS with 0 failures.

- [ ] **Step 5: Commit**
Run: git add config.py models.py trader.py dashboard.py tests/test_config_encoding.py tests/test_trader_runtime_and_live.py tests/test_dashboard.py
Run: git commit -m feat: support multi-strategy paper trading

## Notes For Execution

- Keep live trading single-strategy via `STRATEGY_ID`.
- Keep shared `paper_trades.csv`; do not split paper logs by strategy.
- Do not reintroduce fallback semantics between selected paper strategies.
- Preserve backward compatibility for old `session_state.json` files.
- Strategy 6 details should appear only when strategy 6 is relevant to the current view/runtime payload.

## Plan Review

Self-review checks:
- legacy paper state migrates cleanly
- pending paper settlements stay isolated per strategy
- dashboard filtering affects both aggregates and rows
- runtime still shares market data while isolating strategy state
