# Dashboard Config Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent the dashboard from saving or displaying invalid editable config values as if they were active.

**Architecture:** Add server-side validation in `DashboardState` for every editable config key, reject invalid updates before writing `.env`, and make the returned config payload reflect the effective parsed runtime values instead of misleading raw invalid strings. Cover both update rejection and payload behavior with focused tests.

**Tech Stack:** Python, pytest, existing `DashboardState`/`AppConfig` config flow

---

### Task 1: Add Regression Tests

**Files:**
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] Add a failing test that `DashboardState.update_config()` rejects invalid numeric/bool values such as `MAX_STAKE=abc` and `WS_ENABLED=maybe`.
- [ ] Add a failing test that when the env file already contains an invalid value, `get_config_payload()` returns the effective parsed value plus validation metadata instead of echoing the invalid raw value as active.
- [ ] Run the focused dashboard tests and confirm they fail for the expected reason.

### Task 2: Implement Validation

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`

- [ ] Add per-key validation/parsing helpers for editable config values.
- [ ] Reject invalid `update_config()` inputs before writing `.env`.
- [ ] Track validation errors for existing env-file values and merge payload values from effective runtime config when a raw env value is invalid.
- [ ] Keep the API backward-compatible by preserving `env_values` while adding validation metadata.

### Task 3: Verify

**Files:**
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`
- Modify: `D:/python/BTC_5MIN/dashboard.py`

- [ ] Run the focused dashboard test file and confirm all tests pass.
- [ ] Run the full test suite and confirm no regressions.
