# Unified Strategy 8 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Strategy 8 while making paper and live strategy selection/configuration use the same behavior source.

**Architecture:** `AppConfig.strategy_ids` becomes the canonical strategy list. Paper and live runtime helpers read that list first and only use `PAPER_STRATEGY_IDS` / `LIVE_STRATEGY_IDS` as legacy fallback. Strategy 8 reuses the Strategy 7 OFI and momentum data path, adding a trend branch and a conflict-reversal branch with shared thresholds.

**Tech Stack:** Python dataclasses, pytest, existing Polymarket/Binance runtime modules.

---

### Task 1: Canonical Strategy List

**Files:**
- Modify: `config.py`
- Modify: `dashboard.py`
- Test: `tests/test_config_encoding.py`
- Test: `tests/test_dashboard.py`

- [ ] Add failing tests proving `STRATEGY_IDS` is parsed, accepts strategy 8, and drives both paper and live ids.
- [ ] Update config parsing so `STRATEGY_IDS` is canonical and old paper/live list keys remain fallback-only.
- [ ] Update dashboard normalization, strategy options, and filters to accept `1-8`.
- [ ] Run focused config/dashboard tests.

### Task 2: Strategy 8 Decision Logic

**Files:**
- Modify: `strategy.py`
- Modify: `trader.py`
- Modify: `backtest.py`
- Modify: `strategy_research.py`
- Test: `tests/test_strategy.py`
- Test: `tests/test_trader_runtime_and_live.py`
- Test: `tests/test_backtest.py`

- [ ] Add failing tests for Strategy 8 trend agreement, conflict reversal, weak skip, stale skip, and high-price skip.
- [ ] Implement Strategy 8 helpers with shared runtime/backtest semantics.
- [ ] Reuse Strategy 7 thresholds as defaults for v1 plus new Strategy 8 reversal gap fields.
- [ ] Run focused strategy/backtest/runtime tests.

### Task 3: Operator Surface

**Files:**
- Modify: `.env.dashboard.example`
- Modify: `dashboard.py`
- Modify: `README.md`

- [ ] Add Strategy 8 labels, help text, skip reason labels, and strategy catalog entry.
- [ ] Add `STRATEGY_IDS` to editable config and keep legacy list fields hidden/compatible.
- [ ] Run dashboard tests.

### Task 4: Verification

**Files:**
- Test: `tests/test_config_encoding.py`
- Test: `tests/test_strategy.py`
- Test: `tests/test_trader_runtime_and_live.py`
- Test: `tests/test_backtest.py`
- Test: `tests/test_dashboard.py`

- [ ] Run the focused test suite.
- [ ] Run full pytest if focused tests pass.
- [ ] Summarize any behavior changes and remaining compatibility notes.
