# Dashboard Column Width And Text Overflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Widen the left configuration column to near the monitoring column width, remove the visible `状态 / 最近保存` block, and ensure strategy titles, descriptions, and green badges wrap cleanly instead of overflowing.

**Architecture:** Keep the existing dashboard behavior and payload contracts intact, but adjust the top-level grid widths and the left-column presentation rules in `dashboard.py`. Use asset tests in `tests/test_dashboard.py` to pin down the new width balance and text-wrapping behavior before changing CSS or markup, then run the full dashboard module to confirm the UI changes did not break existing rendering logic.

**Tech Stack:** Python 3, inline HTML/CSS/JS in `dashboard.py`, pytest asset and dashboard behavior tests in `tests/test_dashboard.py`.

---

## File Structure

- `D:/python/BTC_5MIN/dashboard.py`
  - Update the main three-column width balance.
  - Remove the rendered `config-status-inline` block from the left column.
  - Adjust strategy guide and strategy panel wrapping behavior so long text and badges display fully.
- `D:/python/BTC_5MIN/tests/test_dashboard.py`
  - Add asset tests for the new desktop width balance and for clean text wrapping in the strategy panel and strategy guide.

### Task 1: Adjust Main Column Width Balance

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_assets_use_near_symmetric_left_and_right_columns():
    css = dashboard._dashboard_css()

    assert 'grid-template-columns: 380px minmax(520px, 1.15fr) 380px;' in css


def test_dashboard_assets_mid_width_layout_keeps_left_and_right_balanced():
    css = dashboard._dashboard_css()

    assert '@media (max-width: 1450px) {' in css
    assert 'grid-template-columns: 340px minmax(500px, 1.1fr);' in css
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_use_near_symmetric_left_and_right_columns tests/test_dashboard.py::test_dashboard_assets_mid_width_layout_keeps_left_and_right_balanced -v
```

Expected:

- FAIL because the current layout still uses the previous rebalance values and does not widen the left column to near the right-column width

- [ ] **Step 3: Update the top-level grid width values**

```css
.layout {
  padding: 14px;
  display: grid;
  gap: 14px;
  grid-template-columns: 380px minmax(520px, 1.15fr) 380px;
  align-items: start;
}

@media (max-width: 1450px) {
  .layout {
    grid-template-columns: 340px minmax(500px, 1.1fr);
  }
}
```

- [ ] **Step 4: Re-run the focused tests**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_use_near_symmetric_left_and_right_columns tests/test_dashboard.py::test_dashboard_assets_mid_width_layout_keeps_left_and_right_balanced -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "refactor: widen config column and rebalance layout"
```

### Task 2: Remove The Visible Left Status Block

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_assets_remove_visible_config_status_block():
    html = _dashboard_html()

    assert 'class="config-status-inline"' not in html
    assert '<span class="meta-label">状态</span>' not in html
    assert '<span class="meta-label">最近保存</span>' not in html
    assert 'id="cfgError"' not in html
    assert 'id="cfgSavedAt"' not in html
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_remove_visible_config_status_block -v
```

Expected:

- FAIL because the left column still renders the visible `config-status-inline` status row with `cfgError` and `cfgSavedAt`

- [ ] **Step 3: Remove the rendered status block from the left column**

```html
<div class="panel-body">
  <div id="strategyGuideCard" class="strategy-guide-card"></div>

  <div class="strategy-guide-card fold-summary">
    <div class="strategy-guide-head">
      <div>
        <div class="strategy-guide-title">高级参数</div>
        <div class="strategy-guide-subtitle">策略5/7、WS 和实盘相关参数默认折叠。</div>
      </div>
      <button id="advancedConfigToggle" class="btn btn-ghost" type="button" aria-expanded="false" aria-controls="advancedConfigPanel">展开高级参数</button>
    </div>
  </div>

  <form id="configForm" class="form-grid"></form>
  <div id="advancedConfigPanel" hidden></div>

  <div class="actions">
    <button id="btnSaveConfig" class="btn btn-primary" type="button">保存参数</button>
  </div>
</div>
```

- [ ] **Step 4: Re-run the focused test**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_remove_visible_config_status_block -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "refactor: remove visible config status block"
```

### Task 3: Let Strategy Rows Wrap Cleanly Without Overflow

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_assets_allow_strategy_panel_rows_to_wrap_without_overflow():
    css = dashboard._dashboard_css()

    assert '.strategy-panel-row {' in css
    assert 'grid-template-columns: minmax(0, 1fr) auto;' in css
    assert '.strategy-panel-row-main {' in css
    assert 'grid-template-columns: auto minmax(0, 1fr);' in css
    assert '.strategy-panel-primary {' in css
    assert 'justify-self: end;' in css
    assert '@media (max-width: 1024px) {' in css
    assert '.strategy-panel-row { grid-template-columns: 1fr; }' in css
    assert '.strategy-panel-primary { justify-self: start; }' in css
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_allow_strategy_panel_rows_to_wrap_without_overflow -v
```

Expected:

- FAIL because the strategy rows still use a tighter one-line layout that can clip labels or push the primary toggle off-screen

- [ ] **Step 3: Update the strategy row layout for wrapping**

```css
.strategy-panel-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border: 1px solid rgba(35, 64, 97, 0.72);
  border-radius: 10px;
  background: rgba(8, 15, 27, 0.82);
}

.strategy-panel-row-main {
  min-width: 0;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  gap: 10px;
}

.strategy-panel-primary {
  justify-self: end;
  white-space: normal;
}

@media (max-width: 1024px) {
  .strategy-panel-row { grid-template-columns: 1fr; }
  .strategy-panel-primary { justify-self: start; }
}
```

- [ ] **Step 4: Re-run the focused test**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_allow_strategy_panel_rows_to_wrap_without_overflow -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "fix: prevent strategy panel row overflow"
```

### Task 4: Let Strategy Guide Titles, Descriptions, And Badges Wrap Fully

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_assets_allow_strategy_guide_content_to_wrap_fully():
    css = dashboard._dashboard_css()

    assert '.strategy-guide-title {' in css
    assert 'white-space: normal;' in css
    assert 'overflow-wrap: anywhere;' in css
    assert '.strategy-guide-subtitle {' in css
    assert '.strategy-guide-head {' in css
    assert 'align-items: flex-start;' in css
    assert '.strategy-guide-head .chip {' in css
    assert 'white-space: normal;' in css
    assert 'max-width: 100%;' in css
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_allow_strategy_guide_content_to_wrap_fully -v
```

Expected:

- FAIL because the strategy guide title/badge region still favors single-line compression instead of clean multi-line wrapping

- [ ] **Step 3: Update strategy guide wrapping behavior**

```css
.strategy-guide-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
  flex-wrap: wrap;
}

.strategy-guide-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--text);
  white-space: normal;
  overflow-wrap: anywhere;
}

.strategy-guide-subtitle {
  font-size: 12px;
  color: var(--muted);
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.strategy-guide-head .chip {
  white-space: normal;
  max-width: 100%;
}
```

- [ ] **Step 4: Re-run the focused test**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_allow_strategy_guide_content_to_wrap_fully -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "fix: allow strategy guide text to wrap"
```

### Task 5: Run Dashboard Regression Coverage

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Run focused strategy/dashboard regression checks**

Run:

```powershell
pytest tests/test_dashboard.py -k "strategy_panel or strategy_guide or column or overflow" -v
```

Expected:

- PASS for the focused wrapping and width-related checks

- [ ] **Step 2: Run the full dashboard test module**

Run:

```powershell
pytest tests/test_dashboard.py -v
```

Expected:

- PASS

- [ ] **Step 3: Review the final diff**

Run:

```powershell
git diff -- dashboard.py tests/test_dashboard.py
```

Expected:

- The diff only contains the width rebalance, status-block removal, and strategy text wrapping changes

- [ ] **Step 4: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "test: verify dashboard width and overflow fixes"
```

- [ ] **Step 5: Report remaining UI-only follow-ups if any**

```text
If no additional failures remain, report no follow-up blockers.
If any purely visual issue still needs manual browser review, report it explicitly without changing scope.
```

## Self-Review

### Spec coverage

- Left column widened to near the monitoring column width: covered by Task 1.
- Visible `状态 / 最近保存` block removed: covered by Task 2.
- Strategy panel rows wrap instead of overflowing: covered by Task 3.
- Strategy guide title/subtitle/badge wrap fully: covered by Task 4.
- Regression proof for dashboard behavior: covered by Task 5.

No spec gaps found.

### Placeholder scan

- No `TBD`, `TODO`, or deferred “write tests later” language remains.
- Each task includes exact files, concrete assertions, implementation snippets, commands, and expected outcomes.

### Type consistency

- Width values are consistent across the plan: `380px / minmax(520px, 1.15fr) / 380px`.
- Strategy row wrapping consistently uses `strategy-panel-row`, `strategy-panel-row-main`, and `strategy-panel-primary`.
- Strategy guide wrapping consistently uses `strategy-guide-head`, `strategy-guide-title`, and `strategy-guide-subtitle`.
