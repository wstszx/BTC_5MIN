# Dashboard Column Responsibility Rebalance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebalance the dashboard so the left column becomes a lighter configuration sidebar, the center column becomes the decision cockpit, and the right column becomes a stronger runtime and connection monitoring rail.

**Architecture:** Keep all existing backend payloads and most existing DOM ids, but move runtime and diagnostics ownership into more appropriate columns. The implementation should primarily reshape `dashboard.py` HTML/CSS/JS so the screen prioritizes decision-making and monitoring without rewriting trading behavior. Use TDD through `tests/test_dashboard.py` asset assertions so each structural move is verified before the next one lands.

**Tech Stack:** Python 3, inline HTML/CSS/JS in `dashboard.py`, pytest asset and dashboard behavior tests in `tests/test_dashboard.py`.

---

## File Structure

- `D:/python/BTC_5MIN/dashboard.py`
  - Rebalance top-level three-column widths and add semantic column classes.
  - Move runtime summary/details into the monitoring column.
  - Move diagnostics toggle/host into the decision column.
  - Compress configuration status presentation and remove one redundant timing row from the center column.
- `D:/python/BTC_5MIN/tests/test_dashboard.py`
  - Add and update dashboard asset tests describing the new ownership of configuration, decision, and monitoring sections.

### Task 1: Rebalance The Three Main Columns

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_assets_rebalance_main_columns_for_config_decision_monitoring():
    html = _dashboard_html()
    css = dashboard._dashboard_css()

    assert 'class="panel left-stack config-stack"' in html
    assert 'class="panel center-stack decision-stack"' in html
    assert 'class="stack right-stack monitor-stack"' in html
    assert 'grid-template-columns: 312px minmax(620px, 1.35fr) 392px;' in css
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_rebalance_main_columns_for_config_decision_monitoring -v
```

Expected:

- FAIL because the current layout still uses the old equal-weight three-column structure and lacks the new semantic column classes

- [ ] **Step 3: Add the new top-level column classes and width weighting**

```html
<main class="layout">
  <section class="panel left-stack config-stack"></section>
  <section class="panel center-stack decision-stack"></section>
  <section class="stack right-stack monitor-stack"></section>
</main>
```

```css
.layout {
  padding: 14px;
  display: grid;
  gap: 14px;
  grid-template-columns: 312px minmax(620px, 1.35fr) 392px;
  align-items: start;
}

.config-stack {
  min-width: 0;
}

.decision-stack {
  min-width: 0;
}

.monitor-stack {
  min-width: 0;
}
```

- [ ] **Step 4: Re-run the focused test**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_rebalance_main_columns_for_config_decision_monitoring -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "refactor: rebalance dashboard column widths"
```

### Task 2: Move Runtime Monitoring Into The Right Column And Diagnostics Into The Center

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_assets_move_runtime_monitoring_into_monitor_column():
    html = _dashboard_html()

    assert 'id="monitorRuntimePanel"' in html
    assert '运行与连接监控' in html
    assert 'id="runtimeSummaryBar"' in html
    assert 'id="runtimeDetailsToggle"' in html
    assert 'id="runtimeDetailsPanel"' in html
    assert 'id="wsRuntimeList"' in html


def test_dashboard_assets_move_diagnostics_entry_into_decision_column():
    html = _dashboard_html()

    assert 'id="decisionDiagnosticsHost"' in html
    assert 'id="diagnosticsToggle"' in html
    assert 'id="diagnosticsPanel"' in html
    assert '诊断区' in html
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_move_runtime_monitoring_into_monitor_column tests/test_dashboard.py::test_dashboard_assets_move_diagnostics_entry_into_decision_column -v
```

Expected:

- FAIL because runtime summary/details still live in the configuration column and diagnostics still start from the left side

- [ ] **Step 3: Re-home runtime and diagnostics containers**

```html
<div id="decisionDiagnosticsHost" class="strategy-guide-card fold-summary">
  <div class="strategy-guide-head">
    <div>
      <div class="strategy-guide-title">诊断区</div>
      <div class="strategy-guide-subtitle">策略 6/7 解释型信号与其他辅助诊断默认折叠。</div>
    </div>
    <button id="diagnosticsToggle" class="btn btn-ghost" type="button" aria-expanded="false" aria-controls="diagnosticsPanel">展开诊断区</button>
  </div>
</div>

<div id="diagnosticsPanel" hidden>
  <div id="strategy6Panel" class="box">
    <div class="box-title">策略 6 OFI</div>
    <div class="row">
      <span class="label">OFI 分数</span>
      <span id="strategy6OfiScore" class="value">--</span>
    </div>
    <div class="row">
      <span class="label">信号时间</span>
      <span id="strategy6SignalAt" class="value">--</span>
    </div>
    <div class="row">
      <span class="label">是否陈旧</span>
      <span id="strategy6Stale" class="value">--</span>
    </div>
    <div class="kv-grid">
      <div class="kv"><div class="k">买一价</div><div id="strategy6BidPrice" class="v">--</div></div>
      <div class="kv"><div class="k">买一量</div><div id="strategy6BidQty" class="v">--</div></div>
      <div class="kv"><div class="k">卖一价</div><div id="strategy6AskPrice" class="v">--</div></div>
      <div class="kv"><div class="k">卖一量</div><div id="strategy6AskQty" class="v">--</div></div>
    </div>
  </div>

  <div id="strategy7Panel" class="box">
    <div class="box-title">策略 7 共识诊断</div>
    <div class="row">
      <span class="label">OFI 分数</span>
      <span id="strategy7OfiScore" class="value">--</span>
    </div>
    <div class="row">
      <span class="label">动量偏移</span>
      <span id="strategy7MomentumDelta" class="value">--</span>
    </div>
    <div class="row">
      <span class="label">是否同向</span>
      <span id="strategy7Agreement" class="value">--</span>
    </div>
    <div class="row">
      <span class="label">质量过滤</span>
      <span id="strategy7QualityGate" class="value">--</span>
    </div>
    <div class="row">
      <span class="label">最终原因</span>
      <span id="strategy7FinalReason" class="value">--</span>
    </div>
  </div>
</div>

<div id="monitorRuntimePanel" class="panel">
  <div class="panel-head">
    <div>
      <div class="head-title">运行与连接监控</div>
      <div class="head-desc">运行模式、连接质量与后台状态</div>
    </div>
    <div id="wsHealth" class="chip">待刷新</div>
  </div>
  <div class="panel-body monitor-runtime-grid">
    <div id="runtimeSummaryBar" class="strategy-guide-card fold-summary">
      <div class="strategy-guide-head">
        <div>
          <div class="strategy-guide-title">系统状态</div>
          <div id="runtimeSummaryText" class="strategy-guide-subtitle">当前模式 -- / 目标模式 -- / 是否待切换 -- / 实盘就绪 --</div>
        </div>
        <button id="runtimeDetailsToggle" class="btn btn-ghost" type="button" aria-expanded="false" aria-controls="runtimeDetailsPanel">展开运行详情</button>
      </div>
    </div>

    <div id="runtimeDetailsPanel" hidden>
      <div id="runtimeModeCard" class="strategy-guide-card">
        <div class="strategy-guide-head">
          <div>
            <div class="strategy-guide-title">运行模式</div>
            <div class="strategy-guide-subtitle">显示配置目标、当前实际状态、切换进度和实盘条件。</div>
          </div>
          <span class="chip warn">热切换受控</span>
        </div>
        <div class="rows">
          <div class="row"><span class="label">目标模式</span><span id="runtimeSavedMode" class="value">--</span></div>
          <div class="row"><span class="label">当前模式</span><span id="runtimeRunningMode" class="value">--</span></div>
          <div class="row"><span class="label">是否待切换</span><span id="runtimeRestartRequired" class="value">--</span></div>
          <div class="row"><span class="label">实盘就绪</span><span id="runtimeLiveReady" class="value">--</span></div>
          <div class="row"><span class="label">校验结果</span><span id="runtimeLiveError" class="value">--</span></div>
        </div>
      </div>
    </div>

    <div id="wsRuntimeList" class="runtime-list"></div>
  </div>
</div>
```

- [ ] **Step 4: Re-run the focused tests**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_move_runtime_monitoring_into_monitor_column tests/test_dashboard.py::test_dashboard_assets_move_diagnostics_entry_into_decision_column -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "refactor: move runtime and diagnostics to new columns"
```

### Task 3: Compress The Left Configuration Sidebar And Remove Redundant Time Noise

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_assets_compress_config_status_into_inline_summary():
    html = _dashboard_html()
    css = dashboard._dashboard_css()

    assert 'class="config-status-inline"' in html
    assert 'id="cfgError"' in html
    assert 'id="cfgSavedAt"' in html
    assert '<span class="meta-label">读取状态</span>' not in html
    assert '<div class="meta">' not in html
    assert '.config-status-inline {' in css


def test_dashboard_assets_remove_redundant_market_updated_time_row():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="marketUpdatedAt"' not in html
    assert "el('marketUpdatedAt')" not in js
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_compress_config_status_into_inline_summary tests/test_dashboard.py::test_dashboard_assets_remove_redundant_market_updated_time_row -v
```

Expected:

- FAIL because configuration status still renders as separate stacked blocks and the session card still includes the `marketUpdatedAt` row

- [ ] **Step 3: Replace stacked config meta blocks with a compact status row and remove the extra refresh row**

```html
<div class="config-status-inline">
  <span class="config-status-item">
    <span class="meta-label">状态</span>
    <span id="cfgError" class="meta-value">--</span>
  </span>
  <span class="config-status-item">
    <span class="meta-label">最近保存</span>
    <span id="cfgSavedAt" class="meta-value">--</span>
  </span>
</div>
```

```html
<div class="row">
  <span class="label">WS 交易陈旧保护</span>
  <span id="wsGuard" class="value">--</span>
</div>
```

```css
.config-status-inline {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  margin-bottom: 12px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: rgba(7, 14, 25, 0.8);
}

.config-status-item {
  display: inline-flex;
  gap: 8px;
  align-items: baseline;
}
```

- [ ] **Step 4: Re-run the focused tests**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_compress_config_status_into_inline_summary tests/test_dashboard.py::test_dashboard_assets_remove_redundant_market_updated_time_row -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "refactor: compress config sidebar status"
```

### Task 4: Add Column-Specific Styling And Run Dashboard Regression Coverage

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_assets_style_monitor_and_decision_columns_for_rebalanced_roles():
    css = dashboard._dashboard_css()

    assert '.monitor-stack .panel-body {' in css
    assert '.decision-stack {' in css
    assert '.config-stack {' in css
    assert '.monitor-runtime-grid {' in css


def test_dashboard_assets_responsive_layout_preserves_priority_order():
    css = dashboard._dashboard_css()

    assert '@media (max-width: 1450px) {' in css
    assert 'grid-template-columns: 300px minmax(540px, 1.2fr);' in css
    assert '@media (max-width: 1024px) {' in css
    assert '.monitor-stack { grid-column: auto; grid-template-columns: 1fr; }' in css
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_assets_style_monitor_and_decision_columns_for_rebalanced_roles tests/test_dashboard.py::test_dashboard_assets_responsive_layout_preserves_priority_order -v
```

Expected:

- FAIL because the rebalanced column-specific styles and responsive rules do not exist yet

- [ ] **Step 3: Add the rebalanced column styling and responsive fallbacks**

```css
.decision-stack {
  min-width: 0;
}

.config-stack {
  min-width: 0;
}

.monitor-stack {
  min-width: 0;
}

.monitor-stack .panel-body {
  display: grid;
  gap: 12px;
}

.monitor-runtime-grid {
  display: grid;
  gap: 12px;
}

@media (max-width: 1450px) {
  .layout {
    grid-template-columns: 300px minmax(540px, 1.2fr);
  }

  .monitor-stack {
    grid-column: span 2;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
  }
}

@media (max-width: 1024px) {
  .layout { grid-template-columns: 1fr; }
  .monitor-stack { grid-column: auto; grid-template-columns: 1fr; }
}
```

- [ ] **Step 4: Run focused and full regression verification**

Run:

```powershell
pytest tests/test_dashboard.py -k "dashboard_assets or runtime_mode_status or recent_panel_header" -v
pytest tests/test_dashboard.py -v
```

Expected:

- PASS for the focused asset slice
- PASS for the full `tests/test_dashboard.py` module

- [ ] **Step 5: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "test: verify dashboard column rebalance"
```

## Self-Review

### Spec coverage

- Left column becomes lighter and loses runtime/diagnostics ownership: covered by Tasks 2 and 3.
- Center column becomes the decision cockpit and gains the diagnostics entry point: covered by Task 2.
- Right column becomes the stronger monitoring rail: covered by Task 2 and Task 4.
- Width priority becomes center > right > left: covered by Tasks 1 and 4.
- Repeated timing noise reduced in the center column: covered by Task 3.
- Existing ids and hooks remain usable: covered by Tasks 2, 3, and 4 through asset regression.

No spec gaps found.

### Placeholder scan

- No `TBD`, `TODO`, or deferred “write tests later” language remains.
- Each task includes exact files, concrete test code, implementation snippets, commands, and expected outcomes.

### Type consistency

- The plan consistently uses `config-stack`, `decision-stack`, and `monitor-stack` for column roles.
- Runtime ownership consistently centers on `monitorRuntimePanel`, `runtimeSummaryBar`, `runtimeDetailsPanel`, and `wsRuntimeList`.
- Diagnostics ownership consistently centers on `decisionDiagnosticsHost`, `diagnosticsToggle`, and `diagnosticsPanel`.
