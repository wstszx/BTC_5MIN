# Dashboard Strategy Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current dashboard strategy selector cluster with a single strategy panel that supports paper multi-select, explicit main-strategy choice, and inline bulk actions without changing the config model.

**Architecture:** Keep the existing `STRATEGY_ID` and `PAPER_STRATEGY_IDS` payload semantics and update only the dashboard renderer plus its JS helpers. Reuse the current unified selection resolver, but change the DOM structure from two `<select>` elements plus a standalone button to a row-based strategy panel with bulk actions and main-strategy auto-inclusion.

**Tech Stack:** Python 3, inline dashboard HTML/CSS/JS generation in `dashboard.py`, pytest string-assertion tests in `tests/test_dashboard.py`.

---

## File Structure

- `dashboard.py`
  - Replace the current unified strategy controls with a strategy panel layout.
  - Add helper functions for panel rendering, bulk actions, and row-state collection.
  - Preserve `collectUnifiedStrategyValues()` as the source of truth for final payload normalization.
- `tests/test_dashboard.py`
  - Replace old asset assertions with tests that describe the new strategy panel behavior.

### Task 1: Lock The New Strategy Panel Contract With Tests

**Files:**
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

```python
def test_dashboard_assets_use_strategy_panel_for_unified_strategy_selection():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'strategy-panel' in js
    assert 'cfgStrategyPanel' in js
    assert 'cfgPaperStrategiesSelectAll' not in js
    assert '全选全部策略' not in js
    assert 'function renderStrategyPanel(' in js
    assert 'function togglePaperStrategySelection(' in js
    assert 'function setPrimaryStrategy(' in js
    assert 'function clearPaperStrategies()' in js
    assert "payload.PAPER_STRATEGY_IDS = unifiedValues.PAPER_STRATEGY_IDS;" in js
    assert 'id="strategyGuideCard"' in html
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_use_strategy_panel_for_unified_strategy_selection -v
```

Expected:

- FAIL because the dashboard still renders `cfgPaperStrategiesSelectAll`
- FAIL because the new strategy panel helpers do not exist yet

### Task 2: Implement The Strategy Panel

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`

- [ ] **Step 1: Write the minimal implementation**

```javascript
function renderStrategyPanel(payload, values) {
  // render strategy rows with paper checkbox + main strategy radio state
}

function togglePaperStrategySelection(strategyId, selected) {
  // update row state and re-render without changing the config schema
}

function setPrimaryStrategy(strategyId) {
  // update STRATEGY_ID and preserve auto-inclusion in PAPER_STRATEGY_IDS
}
```

- [ ] **Step 2: Wire the panel into config collection**

```javascript
const unifiedValues = collectUnifiedStrategyValues(state.config || {}, rawValues);
payload.STRATEGY_ID = unifiedValues.STRATEGY_ID;
payload.PAPER_STRATEGY_IDS = unifiedValues.PAPER_STRATEGY_IDS;
```

- [ ] **Step 3: Remove the old dedicated button path**

```javascript
// delete selectAllPaperStrategies() and the cfgPaperStrategiesSelectAll button markup
```

### Task 3: Verify The Dashboard Asset Contract

**Files:**
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`
- Modify: `D:/python/BTC_5MIN/dashboard.py`

- [ ] **Step 1: Run the focused dashboard tests**

Run:

```powershell
pytest tests/test_dashboard.py -k strategy -v
```

Expected:

- PASS for the updated unified-selector coverage
- PASS for the existing summary/recent filter independence tests

- [ ] **Step 2: Run the broader dashboard test file**

Run:

```powershell
pytest tests/test_dashboard.py -v
```

Expected:

- PASS without regressions in unrelated dashboard assets
