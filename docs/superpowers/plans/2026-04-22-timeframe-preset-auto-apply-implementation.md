# Timeframe Preset Auto-Apply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically apply the matching recommended parameter preset in the dashboard form whenever the operator switches `MARKET_TIMEFRAME` between `5m` and `15m`.

**Architecture:** Add one authoritative timeframe preset mapping on the server side, expose it in the dashboard config payload, and wire a small frontend handler that overwrites only the timeframe-sensitive fields when the timeframe selector changes. Keep save and runtime reload behavior unchanged.

**Tech Stack:** Python, embedded dashboard JavaScript, pytest

---

### Task 1: Add failing payload tests for timeframe presets

**Files:**
- Modify: `D:\python\BTC_5MIN\tests\test_dashboard.py`
- Modify: `D:\python\BTC_5MIN\dashboard.py`

- [ ] **Step 1: Write the failing backend payload tests**

Add tests that assert the dashboard config payload includes a `timeframe_presets` object with both `5m` and `15m`, and that the preset values contain the expected recommended settings.

Use tests in this style:

```python
def test_dashboard_config_payload_includes_timeframe_presets(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    payload = state.config_payload()
    presets = payload["timeframe_presets"]
    assert set(presets) == {"5m", "15m"}
    assert presets["5m"]["OPEN_DELAY_SECONDS"] == "12"
    assert presets["15m"]["OPEN_DELAY_SECONDS"] == "25"
    assert presets["5m"]["STRATEGY7_OFI_THRESHOLD"] == "0.58"
    assert presets["15m"]["STRATEGY7_OFI_THRESHOLD"] == "0.50"
```

- [ ] **Step 2: Run the targeted test to verify it fails**

Run: `pytest tests/test_dashboard.py -k "timeframe_presets" -v`
Expected: FAIL because `timeframe_presets` is not yet included in the payload.

### Task 2: Add the server-side preset mapping and payload field

**Files:**
- Modify: `D:\python\BTC_5MIN\dashboard.py`
- Test: `D:\python\BTC_5MIN\tests\test_dashboard.py`

- [ ] **Step 1: Add the authoritative preset mapping**

Create a small mapping in `dashboard.py` for the two supported timeframes:

```python
TIMEFRAME_PRESETS: dict[str, dict[str, str]] = {
    "5m": {
        "OPEN_DELAY_SECONDS": "12",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "10",
        "STRATEGY7_OFI_THRESHOLD": "0.58",
        "STRATEGY7_MOMENTUM_THRESHOLD": "0.008",
        "STRATEGY7_MAX_ENTRY_PRICE": "0.54",
        "STRATEGY7_MIN_SIGNAL_GAP": "0.015",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "2",
        "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP": "0.035",
        "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS": "2",
    },
    "15m": {
        "OPEN_DELAY_SECONDS": "25",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "20",
        "STRATEGY7_OFI_THRESHOLD": "0.50",
        "STRATEGY7_MOMENTUM_THRESHOLD": "0.005",
        "STRATEGY7_MAX_ENTRY_PRICE": "0.55",
        "STRATEGY7_MIN_SIGNAL_GAP": "0.01",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "3",
        "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP": "0.03",
        "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS": "3",
    },
}
```

- [ ] **Step 2: Expose the presets in the config payload**

Add `timeframe_presets` to the dashboard config payload near other config metadata so the JS can consume it directly.

- [ ] **Step 3: Re-run the targeted payload test**

Run: `pytest tests/test_dashboard.py -k "timeframe_presets" -v`
Expected: PASS

### Task 3: Add failing frontend behavior tests for auto-apply

**Files:**
- Modify: `D:\python\BTC_5MIN\tests\test_dashboard.py`
- Modify: `D:\python\BTC_5MIN\dashboard.py`

- [ ] **Step 1: Write frontend-oriented asset tests**

Add tests that verify the embedded dashboard JS includes the timeframe-preset auto-apply logic and references `timeframe_presets`, `MARKET_TIMEFRAME`, and at least one preset-managed field.

Example shape:

```python
def test_dashboard_assets_include_timeframe_preset_auto_apply_logic(tmp_path: Path):
    html, js = render_dashboard_assets(tmp_path)
    assert "timeframe_presets" in js
    assert "MARKET_TIMEFRAME" in js
    assert "OPEN_DELAY_SECONDS" in js
    assert "STRATEGY7_OFI_THRESHOLD" in js
```

- [ ] **Step 2: Run the targeted asset test to verify it fails**

Run: `pytest tests/test_dashboard.py -k "timeframe_preset_auto_apply" -v`
Expected: FAIL because the frontend handler does not exist yet.

### Task 4: Implement frontend auto-apply on timeframe change

**Files:**
- Modify: `D:\python\BTC_5MIN\dashboard.py`
- Test: `D:\python\BTC_5MIN\tests\test_dashboard.py`

- [ ] **Step 1: Add a small JS helper**

Inside the embedded dashboard JS, add a helper that:

```javascript
function applyTimeframePreset(timeframe) {
  const presets = (((state.config || {}).timeframe_presets) || {});
  const preset = presets[timeframe];
  if (!preset) return;
  Object.entries(preset).forEach(([key, value]) => {
    const field = document.querySelector(`[data-config-key="${key}"]`);
    if (!field) return;
    field.value = String(value);
  });
}
```

Adapt the field lookup to the existing form-rendering structure if the actual selectors differ.

- [ ] **Step 2: Call the helper when `MARKET_TIMEFRAME` changes**

Wire the timeframe selector change event so it automatically applies the matching preset immediately in the form.

- [ ] **Step 3: Keep save behavior unchanged**

Do not add a second hidden backend rewrite. The current save path should simply persist the form’s updated values.

- [ ] **Step 4: Run the targeted asset test**

Run: `pytest tests/test_dashboard.py -k "timeframe_preset_auto_apply" -v`
Expected: PASS

### Task 5: Add regression coverage for scope and reload behavior

**Files:**
- Modify: `D:\python\BTC_5MIN\tests\test_dashboard.py`
- Modify: `D:\python\BTC_5MIN\dashboard.py`

- [ ] **Step 1: Add a scope regression test**

Add a test that verifies only the intended timeframe-sensitive fields are included in `timeframe_presets`, and that unrelated fields like `TRADE_MODE` or credentials are not present.

- [ ] **Step 2: Add a save-path regression test**

Add a test that simulates saving a config payload after timeframe-driven updates and verifies the existing `market_timeframe` runtime reload notification still fires.

- [ ] **Step 3: Run targeted dashboard regressions**

Run: `pytest tests/test_dashboard.py -k "timeframe_preset or market_timeframe" -v`
Expected: PASS

### Task 6: Run broader dashboard verification

**Files:**
- Modify: `D:\python\BTC_5MIN\dashboard.py`
- Modify: `D:\python\BTC_5MIN\tests\test_dashboard.py`

- [ ] **Step 1: Run the relevant dashboard suite**

Run: `pytest tests/test_dashboard.py -v`
Expected: PASS

- [ ] **Step 2: Run runtime-launcher timeframe checks**

Run: `pytest tests/test_runtime_launcher.py -k "timeframe" -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add dashboard.py tests/test_dashboard.py docs/superpowers/specs/2026-04-22-timeframe-preset-auto-apply-design.md docs/superpowers/plans/2026-04-22-timeframe-preset-auto-apply-implementation.md
git commit -m "Add timeframe preset auto-apply"
```
