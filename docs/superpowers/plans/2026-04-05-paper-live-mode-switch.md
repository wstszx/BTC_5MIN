# Paper/Live Mode Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a restart-based `paper`/`live` trading mode switch controlled from the dashboard, with live-mode startup validation, isolated live runtime state, masked secrets, and mode-aware runtime status.

**Architecture:** Keep `main.py` as the thin runtime launcher, but make it choose the trading worker from saved config instead of always starting paper mode. Add shared live-runtime validation and a continuous `run_live_trading(...)` loop in `trader.py`, then extend `dashboard.py` so the config editor can safely manage mode selection, live credentials, and saved-vs-running mode status without exposing private keys.

**Tech Stack:** Python 3.x, stdlib threading/http server, pytest, existing Polymarket client/runtime modules, plain HTML/CSS/JS emitted by `dashboard.py`

---

## File Structure

**Reference Spec:** `docs/superpowers/specs/2026-04-05-paper-live-mode-switch-design.md`

**Files:**
- Modify: `config.py`
  - read `TRADE_MODE` from env values and normalize live-related config fields through one shared config surface
- Modify: `main.py`
  - choose paper or live worker at startup, print mode-aware startup messages, and fail fast on invalid live startup config
- Modify: `trader.py`
  - add shared live startup validation and a continuous `run_live_trading(...)` loop that reuses `place_live_order(...)`
- Modify: `dashboard.py`
  - expose new mode/live config fields, mask the private key, preserve secrets across saves, add runtime-mode status, and add live-history/status payloads
- Modify: `README.md`
  - document restart-based mode switching and live-mode prerequisites
- Modify: `docs/operations_runbook.md`
  - add operator steps for switching modes safely
- Modify: `docs/dashboard_runbook.md`
  - explain dashboard behavior for saved mode, running mode, and live config requirements
- Modify: `tests/test_runtime_launcher.py`
  - cover config-driven worker selection and startup failure behavior
- Modify: `tests/test_trader_runtime_and_live.py`
  - cover live validation and the continuous live runtime loop
- Modify: `tests/test_dashboard.py`
  - cover mode config editing, secret masking/preservation, runtime status, and live-report payloads

### Task 1: Add Config-Driven Mode Parsing And Launcher Tests

**Files:**
- Modify: `config.py`
- Modify: `tests/test_runtime_launcher.py`

- [ ] **Step 1: Write the failing config and launcher-selection tests**
