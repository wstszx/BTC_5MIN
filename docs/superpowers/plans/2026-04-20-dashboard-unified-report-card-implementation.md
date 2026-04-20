# Dashboard Unified Report Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge the dashboard's report selector, paper summary, and recent trades into one full-width `交易报告` card with a single shared strategy selector and a two-column wide-screen layout.

**Architecture:** Keep the existing dashboard payload contracts and shared report-filter logic, but move the current three report surfaces into one outer report card in `dashboard.py`. Preserve existing DOM ids where possible so the current summary/recent renderers and refresh code need only small, intentional wiring changes. Add asset tests first, then reshape HTML, then add CSS and status/header wiring until the unified card matches the approved spec.

**Tech Stack:** Python 3, inline HTML/CSS/JS generated in `dashboard.py`, pytest asset tests in `tests/test_dashboard.py`.

---

## File Structure

- `D:/python/BTC_5MIN/dashboard.py`
  - Replace the standalone `报告视图`, `纸面交易汇总`, and `最近交易明细` shells with one full-width report card.
  - Add the new in-card left/right section structure and shared header status area.
  - Add report-card CSS and responsive stacking rules.
  - Keep `paperReportStrategy`, `paperStatus`, `recentStatus`, `daysTbody`, `recentTbody`, and `recentPanelDesc` wired into the new structure.
- `D:/python/BTC_5MIN/tests/test_dashboard.py`
  - Add and update asset tests that describe the new unified report card, the wide/narrow responsive layout, and the shared header status wiring.

### Task 1: Add Asset Tests For The Unified Report Card Markup

**Files:**
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`
- Modify: `D:/python/BTC_5MIN/dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_assets_render_unified_report_card_shell():
    html = _dashboard_html()

    assert 'class="panel unified-report-card"' in html
    assert 'class="report-card-head"' in html
    assert '交易报告' in html
    assert '策略筛选同时作用于纸面交易汇总与最近交易明细' in html
    assert 'id="paperReportStrategy"' in html
    assert 'id="paperStatus"' in html
    assert 'id="recentStatus"' in html
    assert 'id="reportSummarySection"' in html
    assert 'id="reportRecentSection"' in html
    assert '纸面交易汇总' in html
    assert '最近交易明细' in html


def test_dashboard_assets_remove_old_report_panel_shells():
    html = _dashboard_html()

    assert '<div class=\\"head-title\\">报告视图</div>' not in html
    assert '<section class="panel trades-panel">' not in html
    assert '<div class=\\"head-title\\">纸面交易汇总</div>' not in html
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_render_unified_report_card_shell tests/test_dashboard.py::test_dashboard_assets_remove_old_report_panel_shells -v
```

Expected:

- FAIL because the HTML still renders the old standalone report selector, summary panel, and recent-trades panel

- [ ] **Step 3: Replace the old report shells with one unified card in `dashboard.py`**

```html
<section class="panel unified-report-card">
  <div class="panel-head report-card-head">
    <div>
      <div class="head-title">交易报告</div>
      <div class="head-desc">策略筛选同时作用于纸面交易汇总与最近交易明细</div>
    </div>
    <div class="top-actions report-card-actions">
      <select id="paperReportStrategy" class="btn btn-ghost"></select>
      <div class="report-status-group">
        <div id="paperStatus" class="chip">待刷新</div>
        <div id="recentStatus" class="chip">待刷新</div>
      </div>
    </div>
  </div>
  <div class="report-card-body">
    <section id="reportSummarySection" class="report-section">
      <div class="section-title">纸面交易汇总</div>
      <div class="kv-grid" style="margin-bottom: 10px;">
        <div class="kv"><div class="k">日期</div><div id="sumDate" class="v">--</div></div>
        <div class="kv"><div class="k">交易笔数</div><div id="sumTrades" class="v">--</div></div>
        <div class="kv"><div class="k">命中率</div><div id="sumHitRate" class="v">--</div></div>
        <div class="kv"><div class="k">总盈亏</div><div id="sumTotalPnl" class="v">--</div></div>
        <div class="kv"><div class="k">最大回撤</div><div id="sumDrawdown" class="v">--</div></div>
        <div class="kv"><div class="k">强信号占比</div><div id="sumStrongRate" class="v">--</div></div>
      </div>
      <div class="days-table-wrap">
        <table>
          <thead>
            <tr>
              <th>日期</th>
              <th>交易</th>
              <th>命中率</th>
              <th>总盈亏</th>
              <th>回撤</th>
            </tr>
          </thead>
          <tbody id="daysTbody"></tbody>
        </table>
      </div>
    </section>
    <section id="reportRecentSection" class="report-section">
      <div class="section-title">最近交易明细</div>
      <div id="recentPanelDesc" class="section-desc">按时间倒序显示最近 80 条记录 · 当前策略：全部</div>
      <div class="report-recent-table table-wrap">
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>轮次</th>
              <th>方向</th>
              <th>价格</th>
              <th>下注金额</th>
              <th>结果</th>
              <th>校验</th>
              <th>开盘价</th>
              <th>收盘价</th>
              <th>单笔盈亏</th>
              <th>累计盈亏</th>
              <th>跳过原因</th>
              <th>信号偏移</th>
            </tr>
          </thead>
          <tbody id="recentTbody"></tbody>
        </table>
      </div>
    </section>
  </div>
</section>
```

- [ ] **Step 4: Re-run the focused tests**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_render_unified_report_card_shell tests/test_dashboard.py::test_dashboard_assets_remove_old_report_panel_shells -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "refactor: unify dashboard report card structure"
```

### Task 2: Add Wide-Screen Two-Column Layout And Responsive Stack Rules

**Files:**
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`
- Modify: `D:/python/BTC_5MIN/dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_assets_style_unified_report_card_layout():
    css = dashboard._dashboard_css()

    assert '.unified-report-card {' in css
    assert '.report-card-body {' in css
    assert 'grid-template-columns: minmax(320px, 0.95fr) minmax(0, 1.45fr);' in css
    assert '.report-status-group {' in css
    assert '.report-section {' in css
    assert '.report-recent-table {' in css


def test_dashboard_assets_stack_unified_report_card_on_narrow_layouts():
    css = dashboard._dashboard_css()

    assert '@media (max-width: 1450px) {' in css
    assert '.report-card-body {' in css
    assert '@media (max-width: 1024px) {' in css
    assert 'grid-template-columns: 1fr;' in css
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_style_unified_report_card_layout tests/test_dashboard.py::test_dashboard_assets_stack_unified_report_card_on_narrow_layouts -v
```

Expected:

- FAIL because the current CSS only has the old `.trades-panel` full-width block and no unified report-card layout classes

- [ ] **Step 3: Add the report-card CSS and responsive stacking rules**

```css
.unified-report-card {
  grid-column: 1 / -1;
}

.report-card-head {
  align-items: flex-start;
}

.report-status-group {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.report-card-body {
  display: grid;
  grid-template-columns: minmax(320px, 0.95fr) minmax(0, 1.45fr);
  gap: 14px;
  padding: 14px;
}

.report-section {
  min-width: 0;
  display: grid;
  gap: 10px;
}

.report-recent-table {
  max-height: 420px;
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: rgba(6, 12, 22, 0.66);
}

@media (max-width: 1450px) {
  .report-card-body {
    grid-template-columns: minmax(280px, 0.95fr) minmax(0, 1.25fr);
  }
}

@media (max-width: 1024px) {
  .report-card-body {
    grid-template-columns: 1fr;
  }
}
```

- [ ] **Step 4: Re-run the focused tests**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_style_unified_report_card_layout tests/test_dashboard.py::test_dashboard_assets_stack_unified_report_card_on_narrow_layouts -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "feat: add unified report card layout styles"
```

### Task 3: Wire Shared Header Status And Preserve Report Refresh Behavior

**Files:**
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`
- Modify: `D:/python/BTC_5MIN/dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_assets_render_unified_report_header_status_and_recent_copy():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="paperStatus"' in html
    assert 'id="recentStatus"' in html
    assert 'class="report-status-group"' in html
    assert 'id="recentPanelDesc"' in html
    assert 'function recentStrategyHeaderText()' in js
    assert 'function setReportStatus(' in js
    assert "setReportStatus('paperStatus', '汇总', '已更新', 'ok');" in js
    assert "setReportStatus('recentStatus', '明细', rows.length + ' 行' + (runningMode === 'live' ? ' · 实盘' : ''), pendingCount > 0 ? 'warn' : 'ok');" in js
    assert "el('recentPanelDesc').textContent = recentStrategyHeaderText();" in js


def test_dashboard_assets_refresh_shared_selector_still_updates_summary_and_recent():
    js = _dashboard_js()

    assert "state.paperReportStrategyFilter = node.value || 'all';" in js
    assert "state.paperSummaryStrategyFilter = '';" in js
    assert "state.paperRecentStrategyFilter = '';" in js
    assert "await Promise.allSettled([refreshSummary(), refreshRecent()]);" in js
    assert "const strategy = encodeURIComponent(effectivePaperSummaryStrategyFilter());" in js
    assert "const strategy = encodeURIComponent(effectivePaperRecentStrategyFilter());" in js
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_render_unified_report_header_status_and_recent_copy tests/test_dashboard.py::test_dashboard_assets_refresh_shared_selector_still_updates_summary_and_recent -v
```

Expected:

- FAIL if the report rewrite accidentally drops `paperStatus`, `recentStatus`, `recentPanelDesc`, or the shared selector refresh flow

- [ ] **Step 3: Keep the current refresh/filter logic intact while moving it into the new report card**

```javascript
function setReportStatus(id, prefix, text, tone) {
  setChip(id, prefix + ': ' + text, tone);
}

function renderSharedPaperReportStrategySelector() {
  const options = paperReportStrategyOptions();
  const current = effectivePaperReportStrategyFilter();
  const node = el('paperReportStrategy');
  el('recentPanelDesc').textContent = recentStrategyHeaderText();
  if (!node) return;

  node.innerHTML = '';
  options.forEach((value) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value === 'all' ? '查看全部策略' : ('查看策略 ' + value);
    option.selected = value === current;
    node.appendChild(option);
  });

  node.onchange = async () => {
    state.paperReportStrategyFilter = node.value || 'all';
    state.paperSummaryStrategyFilter = '';
    state.paperRecentStrategyFilter = '';
    renderSharedPaperReportStrategySelector();
    await Promise.allSettled([refreshSummary(), refreshRecent()]);
  };
}
```

- [ ] **Step 4: Re-run the focused tests**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_render_unified_report_header_status_and_recent_copy tests/test_dashboard.py::test_dashboard_assets_refresh_shared_selector_still_updates_summary_and_recent -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "refactor: preserve unified report refresh wiring"
```

### Task 4: Run Regression Coverage For Dashboard Assets

**Files:**
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`
- Modify: `D:/python/BTC_5MIN/dashboard.py`

- [ ] **Step 1: Run the focused dashboard asset suite**

Run:

```powershell
pytest tests/test_dashboard.py -k "dashboard_assets or recent_panel_header or shared_paper_report_strategy_filter" -v
```

Expected:

- PASS with the updated report-card tests and no regressions in the existing dashboard asset coverage

- [ ] **Step 2: If any assertions still reference the old three-panel shell, update them minimally**

```python
def test_dashboard_assets_show_current_strategy_in_recent_panel_header():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="recentPanelDesc"' in html
    assert 'function recentStrategyHeaderText()' in js
    assert "const strategy = effectivePaperRecentStrategyFilter();" in js
    assert "el('recentPanelDesc').textContent = recentStrategyHeaderText();" in js
```

- [ ] **Step 3: Run the full dashboard test module**

Run:

```powershell
pytest tests/test_dashboard.py -v
```

Expected:

- PASS

- [ ] **Step 4: Run a final git diff review**

Run:

```powershell
git diff -- dashboard.py tests/test_dashboard.py
```

Expected:

- The diff shows one unified report card, matching CSS, and only the intended test updates

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "test: verify unified dashboard report card"
```

## Self-Review

### Spec coverage

- Unified full-width `交易报告` card: covered by Task 1.
- Single visible shared selector in the header: covered by Task 1 and Task 3.
- Left summary / right recent wide-screen layout: covered by Task 2.
- Narrow-screen vertical stack: covered by Task 2.
- Shared header status area with separate summary/recent states: covered by Task 1 and Task 3.
- Preserve recent strategy subtitle and shared refresh behavior: covered by Task 3.
- Update dashboard asset tests for the new structure: covered across Tasks 1 to 4.

No spec gaps found.

### Placeholder scan

- No `TBD`, `TODO`, or deferred “write tests later” steps remain.
- Each task includes exact files, concrete test code, explicit commands, and expected outcomes.

### Type consistency

- `paperReportStrategy`, `paperStatus`, `recentStatus`, `daysTbody`, `recentTbody`, and `recentPanelDesc` are used consistently across tasks.
- The new structural class names are consistently `unified-report-card`, `report-card-head`, `report-card-body`, `report-section`, and `report-status-group`.
