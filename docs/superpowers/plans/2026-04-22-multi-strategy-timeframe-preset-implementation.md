# Multi-Strategy Timeframe Preset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically apply the full `5m` or `15m` recommended preset for shared execution fields plus Strategy 5, Strategy 6, and Strategy 7 whenever the dashboard timeframe selector changes.

**Architecture:** Replace the current flat timeframe preset mapping with a structured server-side preset map containing `shared`, `strategy5`, `strategy6`, and `strategy7`, then update the frontend helper to merge those sections into one flat field update when `MARKET_TIMEFRAME` changes. Keep save and runtime reload behavior unchanged.

**Tech Stack:** Python, embedded dashboard JavaScript, pytest

---

### Task 1: Add failing payload tests for structured presets

**Files:**
- Modify: `D:\python\BTC_5MIN\tests\test_dashboard.py`
- Modify: `D:\python\BTC_5MIN\dashboard.py`

- [ ] **Step 1: Write the failing backend payload tests**

Add tests that assert `timeframe_presets` is now structured per timeframe with the sections `shared`, `strategy5`, `strategy6`, and `strategy7`.

Use tests in this style:

```python
def test_dashboard_config_payload_includes_structured_timeframe_presets(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    payload = state.get_config_payload()
    presets = payload["timeframe_presets"]
    assert set(presets["5m"]) == {"shared", "strategy5", "strategy6", "strategy7"}
    assert presets["5m"]["shared"]["OPEN_DELAY_SECONDS"] == "12"
    assert presets["5m"]["strategy5"]["SIGNAL_MOMENTUM_THRESHOLD"] == "0.020"
    assert presets["5m"]["strategy6"]["OFI_THRESHOLD"] == "0.72"
    assert presets["5m"]["strategy7"]["STRATEGY7_OFI_THRESHOLD"] == "0.58"
```

- [ ] **Step 2: Run the targeted test to verify it fails**

Run: `pytest tests/test_dashboard.py -k "structured_timeframe_presets" -v`
Expected: FAIL because the payload currently exposes a flat preset map.

### Task 2: Replace the server-side preset mapping with structured sections

**Files:**
- Modify: `D:\python\BTC_5MIN\dashboard.py`
- Test: `D:\python\BTC_5MIN\tests\test_dashboard.py`

- [ ] **Step 1: Replace the flat preset table**

Convert `TIMEFRAME_PRESETS` into this structure:

```python
TIMEFRAME_PRESETS: dict[str, dict[str, dict[str, str]]] = {
    "5m": {
        "shared": {
            "OPEN_DELAY_SECONDS": "12",
            "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "10",
        },
        "strategy5": {
            "SIGNAL_MOMENTUM_THRESHOLD": "0.020",
            "SIGNAL_FALLBACK_STRATEGY_ID": "2",
            "MAX_PRICE_THRESHOLD": "0.60",
            "TARGET_PROFIT": "0.8",
        },
        "strategy6": {
            "OFI_THRESHOLD": "0.72",
            "BINANCE_SIGNAL_STALE_SECONDS": "1.0",
            "TARGET_PROFIT": "0.8",
        },
        "strategy7": {
            "STRATEGY7_OFI_THRESHOLD": "0.58",
            "STRATEGY7_MOMENTUM_THRESHOLD": "0.008",
            "STRATEGY7_MAX_ENTRY_PRICE": "0.54",
            "STRATEGY7_MIN_SIGNAL_GAP": "0.015",
            "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "2",
            "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP": "0.035",
            "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS": "2",
        },
    },
    "15m": {
        "shared": {
            "OPEN_DELAY_SECONDS": "25",
            "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "20",
        },
        "strategy5": {
            "SIGNAL_MOMENTUM_THRESHOLD": "0.015",
            "SIGNAL_FALLBACK_STRATEGY_ID": "2",
            "MAX_PRICE_THRESHOLD": "0.65",
            "TARGET_PROFIT": "1.0",
        },
        "strategy6": {
            "OFI_THRESHOLD": "0.65",
            "BINANCE_SIGNAL_STALE_SECONDS": "2.0",
            "TARGET_PROFIT": "1.0",
        },
        "strategy7": {
            "STRATEGY7_OFI_THRESHOLD": "0.50",
            "STRATEGY7_MOMENTUM_THRESHOLD": "0.005",
            "STRATEGY7_MAX_ENTRY_PRICE": "0.55",
            "STRATEGY7_MIN_SIGNAL_GAP": "0.01",
            "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "3",
            "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP": "0.03",
            "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS": "3",
        },
    },
}
```

- [ ] **Step 2: Preserve payload export**

Continue returning `timeframe_presets` in `get_config_payload()`, but now expose the structured object directly.

- [ ] **Step 3: Re-run the targeted payload test**

Run: `pytest tests/test_dashboard.py -k "structured_timeframe_presets" -v`
Expected: PASS

### Task 3: Add failing frontend asset tests for merged preset application

**Files:**
- Modify: `D:\python\BTC_5MIN\tests\test_dashboard.py`
- Modify: `D:\python\BTC_5MIN\dashboard.py`

- [ ] **Step 1: Write asset tests for section merging**

Add tests that assert the dashboard JS includes logic to:

- read the structured timeframe preset sections
- merge `shared`, `strategy5`, `strategy6`, and `strategy7`
- apply the merged values to the config form

Example shape:

```python
def test_dashboard_assets_merge_structured_timeframe_presets():
    js = _dashboard_js()
    assert "function flattenTimeframePreset(" in js
    assert "preset.shared" in js
    assert "preset.strategy5" in js
    assert "preset.strategy6" in js
    assert "preset.strategy7" in js
```

- [ ] **Step 2: Run the targeted asset test to verify it fails**

Run: `pytest tests/test_dashboard.py -k "merge_structured_timeframe_presets" -v`
Expected: FAIL because the current JS helper expects a flat preset map.

### Task 4: Implement structured preset flattening in dashboard JS

**Files:**
- Modify: `D:\python\BTC_5MIN\dashboard.py`
- Test: `D:\python\BTC_5MIN\tests\test_dashboard.py`

- [ ] **Step 1: Add a frontend flatten helper**

Implement a small helper like:

```javascript
function flattenTimeframePreset(preset) {
  if (!preset) return {};
  return {
    ...(preset.shared || {}),
    ...(preset.strategy5 || {}),
    ...(preset.strategy6 || {}),
    ...(preset.strategy7 || {}),
  };
}
```

- [ ] **Step 2: Update auto-apply logic**

Change `applyTimeframePreset()` so it:

```javascript
function applyTimeframePreset(timeframe) {
  const presets = (((state.config || {}).timeframe_presets) || {});
  const preset = presets[String(timeframe || '').toLowerCase()];
  const flatPreset = flattenTimeframePreset(preset);
  Object.entries(flatPreset).forEach(([key, value]) => {
    const field = el('cfg_' + key);
    if (!field) return;
    field.value = String(value);
  });
}
```

- [ ] **Step 3: Keep save behavior unchanged**

Do not add any second backend rewrite layer. Save should continue to persist the current form values.

- [ ] **Step 4: Run the targeted asset test**

Run: `pytest tests/test_dashboard.py -k "merge_structured_timeframe_presets" -v`
Expected: PASS

### Task 5: Add scope and value regressions for strategies 5, 6, and 7

**Files:**
- Modify: `D:\python\BTC_5MIN\tests\test_dashboard.py`
- Modify: `D:\python\BTC_5MIN\dashboard.py`

- [ ] **Step 1: Add preset-scope regression tests**

Add tests that verify:

- `shared` includes only shared fields
- `strategy5` includes strategy-5-specific fields
- `strategy6` includes strategy-6-specific fields
- `strategy7` includes strategy-7-specific fields
- unrelated fields such as credentials and `TRADE_MODE` remain absent

- [ ] **Step 2: Add save-path regression test**

Add a test that simulates a save payload carrying timeframe plus representative Strategy 5, 6, and 7 values, and verifies:

- values are accepted by `update_config()`
- `market_timeframe` reload notification still fires

- [ ] **Step 3: Run the targeted regression set**

Run: `pytest tests/test_dashboard.py -k "structured_timeframe_presets or market_timeframe" -v`
Expected: PASS

### Task 6: Run broader verification

**Files:**
- Modify: `D:\python\BTC_5MIN\dashboard.py`
- Modify: `D:\python\BTC_5MIN\tests\test_dashboard.py`

- [ ] **Step 1: Run the dashboard suite**

Run: `pytest tests/test_dashboard.py -v`
Expected: PASS

- [ ] **Step 2: Run timeframe runtime-launcher checks**

Run: `pytest tests/test_runtime_launcher.py -k "timeframe" -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add dashboard.py tests/test_dashboard.py docs/superpowers/specs/2026-04-22-multi-strategy-timeframe-preset-design.md docs/superpowers/plans/2026-04-22-multi-strategy-timeframe-preset-implementation.md
git commit -m "Add multi-strategy timeframe presets"
```
