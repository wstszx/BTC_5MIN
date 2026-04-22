# Near-Entry Fast Polling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce avoidable `entry_window_missed` skips by switching the paper runtime to a shorter poll interval only when a target round is close to entry.

**Architecture:** Add two small runtime config values, introduce a focused helper in `trader.py` that decides whether to use the base or fast poll interval, and wire that helper into the existing paper-runtime sleep points without changing strategy logic or entry-window semantics.

**Tech Stack:** Python, pytest

---

### Task 1: Add focused failing tests for poll-interval selection

**Files:**
- Modify: `D:\python\BTC_5MIN\tests\test_trader_runtime_and_live.py`
- Modify: `D:\python\BTC_5MIN\trader.py`

- [ ] **Step 1: Write the failing tests**

Add direct helper-level tests that cover:

```python
def test_poll_interval_uses_base_when_no_target_round():
    cfg = AppConfig()
    now = datetime.now(timezone.utc)
    assert _poll_interval_for_target_round(cfg=cfg, now=now, target_round=None) == pytest.approx(cfg.poll_interval_seconds)


def test_poll_interval_switches_to_fast_window_near_entry():
    now = datetime(2026, 4, 22, 1, 29, 18, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="evt",
        market_id="mkt",
        slug="btc-updown-15m-test",
        title="Near Entry",
        start_time=datetime(2026, 4, 22, 1, 29, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 22, 1, 44, 0, tzinfo=timezone.utc),
        up_token_id="up",
        down_token_id="down",
    )
    cfg = AppConfig(open_delay_seconds=25, near_entry_poll_window_seconds=10, fast_poll_interval_seconds=1)
    assert _poll_interval_for_target_round(cfg=cfg, now=now, target_round=window) == pytest.approx(1.0)


def test_poll_interval_stays_base_after_entry_window_is_missed():
    now = datetime(2026, 4, 22, 1, 29, 31, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="evt",
        market_id="mkt",
        slug="btc-updown-15m-test",
        title="Missed Entry",
        start_time=datetime(2026, 4, 22, 1, 29, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 22, 1, 44, 0, tzinfo=timezone.utc),
        up_token_id="up",
        down_token_id="down",
    )
    cfg = AppConfig(open_delay_seconds=25, entry_grace_seconds=5, near_entry_poll_window_seconds=10, fast_poll_interval_seconds=1)
    assert _poll_interval_for_target_round(cfg=cfg, now=now, target_round=window) == pytest.approx(cfg.poll_interval_seconds)
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `pytest tests/test_trader_runtime_and_live.py -k "poll_interval and target_round" -v`
Expected: FAIL because the helper and config keys do not exist yet.

### Task 2: Add runtime config surface for fast polling

**Files:**
- Modify: `D:\python\BTC_5MIN\config.py`
- Test: `D:\python\BTC_5MIN\tests\test_trader_runtime_and_live.py`

- [ ] **Step 1: Add config fields**

Add two new `AppConfig` fields with conservative defaults:

```python
near_entry_poll_window_seconds: float = field(
    default_factory=lambda: _env_float("NEAR_ENTRY_POLL_WINDOW_SECONDS", 10.0)
)
fast_poll_interval_seconds: float = field(
    default_factory=lambda: _env_float("FAST_POLL_INTERVAL_SECONDS", 1.0)
)
```

- [ ] **Step 2: Keep values safe**

Clamp these values where they are used rather than rejecting startup, so malformed negative values fall back to non-negative behavior instead of crashing the runtime.

- [ ] **Step 3: Run the targeted tests again**

Run: `pytest tests/test_trader_runtime_and_live.py -k "poll_interval and target_round" -v`
Expected: still FAIL because the helper logic is not implemented yet.

### Task 3: Implement near-entry poll-interval selection

**Files:**
- Modify: `D:\python\BTC_5MIN\trader.py`
- Test: `D:\python\BTC_5MIN\tests\test_trader_runtime_and_live.py`

- [ ] **Step 1: Add the helper**

Implement a helper near `_entry_time_for_round()`:

```python
def _poll_interval_for_target_round(
    *,
    cfg: AppConfig,
    now: datetime,
    target_round: MarketWindow | None,
) -> float:
    base_interval = max(0.0, float(cfg.poll_interval_seconds))
    if target_round is None:
        return base_interval
    entry_time = _entry_time_for_round(cfg, target_round)
    if _entry_window_missed(now, entry_time, grace_seconds=cfg.entry_grace_seconds):
        return base_interval
    remaining = (entry_time - now).total_seconds()
    near_entry_window = max(0.0, float(cfg.near_entry_poll_window_seconds))
    if remaining <= near_entry_window:
        return min(base_interval, max(0.0, float(cfg.fast_poll_interval_seconds)))
    return base_interval
```

- [ ] **Step 2: Use the helper at paper-runtime sleep sites**

Replace the paper-runtime sleep calls that currently always use `cfg.poll_interval_seconds` with:

```python
sleep_seconds = _poll_interval_for_target_round(cfg=cfg, now=now, target_round=target_round)
if not _sleep_if_not_stopped(stop_event, sleep_seconds):
    return {"status": "stopped"}
```

Use the helper only in the paper loop around the active target-round polling path. Keep `_sleep_until_round_end()` and unrelated runtime paths unchanged in this task.

- [ ] **Step 3: Run targeted tests to verify they pass**

Run: `pytest tests/test_trader_runtime_and_live.py -k "poll_interval and target_round" -v`
Expected: PASS

### Task 4: Add a paper-runtime regression around missed-entry timing

**Files:**
- Modify: `D:\python\BTC_5MIN\tests\test_trader_runtime_and_live.py`
- Modify: `D:\python\BTC_5MIN\trader.py`

- [ ] **Step 1: Add a higher-level regression test**

Add a focused runtime test that simulates repeated loop timing near entry and verifies the runtime asks for the shorter interval when the target round is within the configured near-entry window.

Use a stubbed sleep collector pattern similar to:

```python
sleep_calls = []

def fake_sleep(seconds: float) -> None:
    sleep_calls.append(seconds)
```

Then assert the collected calls include `1.0` near entry instead of only `5.0`.

- [ ] **Step 2: Run the focused runtime regression**

Run: `pytest tests/test_trader_runtime_and_live.py -k "near_entry and fast_poll" -v`
Expected: PASS

### Task 5: Run broader verification

**Files:**
- Modify: `D:\python\BTC_5MIN\config.py`
- Modify: `D:\python\BTC_5MIN\trader.py`
- Modify: `D:\python\BTC_5MIN\tests\test_trader_runtime_and_live.py`

- [ ] **Step 1: Run the relevant runtime suite**

Run: `pytest tests/test_trader_runtime_and_live.py -v`
Expected: PASS

- [ ] **Step 2: Run the strategy-7 regression subset**

Run: `pytest tests/test_trader_runtime_and_live.py -k strategy7 -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add config.py trader.py tests/test_trader_runtime_and_live.py docs/superpowers/specs/2026-04-22-near-entry-fast-polling-design.md docs/superpowers/plans/2026-04-22-near-entry-fast-polling-implementation.md
git commit -m "Add near-entry fast polling"
```
