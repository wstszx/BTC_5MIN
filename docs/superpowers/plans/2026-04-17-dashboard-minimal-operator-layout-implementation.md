# Dashboard Minimal Operator Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simplify the dashboard so operators see only decision-critical information by default, while diagnostics and low-frequency controls remain available through folded sections.

**Architecture:** Keep the existing dashboard renderer and payload contracts, but restructure the HTML/CSS/JS hierarchy so the default operator path is leaner. Remove clearly unnecessary always-visible elements, convert diagnostic cards into collapsed sections, merge overlapping decision views, and introduce a shared visible paper-report strategy filter without forcing a backend rewrite.

**Tech Stack:** Python 3, inline HTML/CSS/JS generated in `dashboard.py`, pytest dashboard asset tests in `tests/test_dashboard.py`.

---

## File Structure

- `D:/python/BTC_5MIN/dashboard.py`
  - Remove low-value always-visible top/meta controls.
  - Add collapsible sections for runtime, diagnostics, and help.
  - Merge signal and plan cards into a primary decision area with folded signal details.
  - Introduce one visible shared paper-report strategy selector while preserving internal compatibility where needed.
- `D:/python/BTC_5MIN/tests/test_dashboard.py`
  - Add and update asset tests to describe the simplified operator layout and verify obsolete controls are gone.

### Task 1: Remove Unnecessary Always-Visible Elements

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_assets_remove_low_value_operator_clutter():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="clockUtc"' not in html
    assert 'id="cfgEnvFile"' not in html
    assert 'id="btnToggleKeys"' not in html
    assert 'id="btnReloadConfig"' not in html
    assert "el('clockUtc')" not in js
    assert "el('cfgEnvFile')" not in js
    assert "el('btnToggleKeys')" not in js
    assert "el('btnReloadConfig')" not in js
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_remove_low_value_operator_clutter -v
```

Expected:

- FAIL because the UTC clock, config path, and internal-key/reload controls still exist

- [ ] **Step 3: Remove the obsolete HTML and JS bindings**

```python
# dashboard.py
# Delete:
# - clockUtc node
# - cfgEnvFile node
# - btnToggleKeys button
# - btnReloadConfig button
# - related bootstrap/bind logic
```

- [ ] **Step 4: Re-run the focused test**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_remove_low_value_operator_clutter -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "refactor: remove low-value dashboard clutter"
```

### Task 2: Fold Runtime And Diagnostics By Default

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_assets_fold_runtime_and_strategy_diagnostics():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="runtimeSummaryBar"' in html
    assert 'id="runtimeDetailsToggle"' in html
    assert 'id="runtimeDetailsPanel"' in html
    assert 'id="diagnosticsToggle"' in html
    assert 'id="diagnosticsPanel"' in html
    assert 'strategy6Panel' in html
    assert 'strategy7Panel' in html
    assert 'function toggleFoldSection(' in js
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_fold_runtime_and_strategy_diagnostics -v
```

Expected:

- FAIL because runtime and diagnostics are still permanently expanded

- [ ] **Step 3: Implement collapsed runtime and diagnostics sections**

```javascript
function toggleFoldSection(sectionId, expanded) {
  // Toggle aria-expanded and hidden state for folded panels
}
```

- [ ] **Step 4: Re-run the focused test**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_fold_runtime_and_strategy_diagnostics -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "feat: fold runtime and diagnostics sections"
```

### Task 3: Merge Signal And Trade Plan Into A Primary Decision Card

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_assets_use_primary_decision_card_with_folded_signal_details():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="decisionCard"' in html
    assert 'id="signalDetailsToggle"' in html
    assert 'id="signalDetailsPanel"' in html
    assert '盘口价格' in html
    assert '最终决策' in html
    assert '开盘看涨价' in html
    assert '当前看涨价' in html
    assert 'function renderDecisionCard(' in js
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_use_primary_decision_card_with_folded_signal_details -v
```

Expected:

- FAIL because signal and plan are still rendered as separate peer cards

- [ ] **Step 3: Replace the split cards with one primary decision card**

```javascript
function renderDecisionCard(payload) {
  // Show should_trade, side, price, order cost, skip reason
  // Keep signal details behind a folded panel
}
```

- [ ] **Step 4: Re-run the focused test**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_use_primary_decision_card_with_folded_signal_details -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "refactor: merge signal and trade plan views"
```

### Task 4: Introduce One Shared Visible Report Filter

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_assets_use_shared_paper_report_strategy_filter():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="paperReportStrategy"' in html
    assert 'id="paperSummaryStrategy"' not in html
    assert 'id="recentTradesStrategy"' not in html
    assert 'function renderSharedPaperReportStrategySelector(' in js
    assert 'paperReportStrategyFilter' in js
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_use_shared_paper_report_strategy_filter -v
```

Expected:

- FAIL because summary and recent still expose separate selectors

- [ ] **Step 3: Add one visible shared selector while keeping internal compatibility**

```javascript
function renderSharedPaperReportStrategySelector() {
  // One UI selector drives both summary and recent by default
}
```

- [ ] **Step 4: Re-run the focused test**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_use_shared_paper_report_strategy_filter -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "refactor: unify paper report strategy filter"
```

### Task 5: Group Infrequent Config Controls Under Advanced Settings

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_assets_group_strategy7_ws_and_live_controls_under_advanced_settings():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="advancedConfigToggle"' in html
    assert 'id="advancedConfigPanel"' in html
    assert '高级参数' in html
    assert 'function applyAdvancedConfigVisibility(' in js
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_group_strategy7_ws_and_live_controls_under_advanced_settings -v
```

Expected:

- FAIL because advanced controls are still rendered inline with common controls

- [ ] **Step 3: Add the advanced-settings fold**

```javascript
function applyAdvancedConfigVisibility(values) {
  // Keep frequent controls visible
  // Move strategy5/strategy7/ws/live-redeem controls into folded advanced section
}
```

- [ ] **Step 4: Re-run the focused test**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_group_strategy7_ws_and_live_controls_under_advanced_settings -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "feat: add advanced dashboard config fold"
```

### Task 6: Final Verification

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Run minimal-layout focused coverage**

Run:

```powershell
pytest tests/test_dashboard.py -k "dashboard_assets_remove_low_value_operator_clutter or dashboard_assets_fold_runtime_and_strategy_diagnostics or dashboard_assets_use_primary_decision_card_with_folded_signal_details or dashboard_assets_use_shared_paper_report_strategy_filter or dashboard_assets_group_strategy7_ws_and_live_controls_under_advanced_settings" -v
```

Expected:

- PASS for all new minimal-layout tests

- [ ] **Step 2: Run broader dashboard regression coverage**

Run:

```powershell
pytest tests/test_dashboard.py -v
```

Expected:

- PASS without breaking strategy, runtime, or config workflows

- [ ] **Step 3: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "refactor: simplify dashboard operator layout"
```

## Self-Review

- Spec coverage:
  - Direct removals: covered in Task 1
  - Folded runtime/diagnostics: covered in Task 2
  - Signal/plan merge: covered in Task 3
  - Shared report filter: covered in Task 4
  - Advanced settings grouping: covered in Task 5
  - Regression verification: covered in Task 6
- Placeholder scan:
  - No `TODO`, `TBD`, or abstract “handle appropriately” steps remain
- Type consistency:
  - The plan consistently uses `dashboard.py`, `tests/test_dashboard.py`, and named UI hooks matching the described sections
