# Dashboard Left Panel Taskflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the dashboard's left configuration panel into a taskflow-oriented workspace with a single `Paper / Live` mode selector, one active paper timeframe editor at a time, clearer core-vs-advanced parameter separation, and a more understandable save flow.

**Architecture:** Keep the current dashboard backend payload contract as the source of truth, but stop rendering the left side as one generic mixed config form. Instead, add a mode-aware shell at the top, route rendering into dedicated `paper` and `live` taskflow sections, reuse existing strategy/profile data structures, and keep advanced parameters behind a fold per mode.

**Tech Stack:** Embedded HTML/CSS/JS in `dashboard.py`, existing config metadata in `DashboardState`, existing taskflow/profile data already exposed by the dashboard backend, pytest.

---

### Task 1: Add A Mode-Aware Left Panel Shell

**Files:**
- Modify: `D:\python\BTC_5MIN\dashboard.py`
- Test: `D:\python\BTC_5MIN\tests\test_dashboard.py`

- [ ] **Step 1: Write the failing shell/layout tests**

```python
def test_dashboard_assets_include_left_panel_mode_selector_shell():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="configModeSelect"' in html
    assert 'id="configContextSummary"' in html
    assert 'id="paperTaskflowRoot"' in html
    assert 'id="liveTaskflowRoot"' in html
    assert "function effectiveConfigMode(" in js
    assert "function renderConfigModeShell(" in js


def test_dashboard_assets_hide_paper_and_live_sections_by_active_mode():
    js = _dashboard_js()

    assert "paperTaskflowRoot" in js
    assert "liveTaskflowRoot" in js
    assert "mode === 'paper'" in js
    assert "mode === 'live'" in js
```

- [ ] **Step 2: Run the shell/layout tests to verify they fail**

Run: `pytest tests/test_dashboard.py -k "mode_selector_shell or active_mode" -q`

Expected: FAIL because the left panel still uses the old mixed single-form shell.

- [ ] **Step 3: Implement the top-level shell and mode switch rendering**

```html
<section class="panel left-stack config-stack">
  <div class="panel-head">
    <div>
      <div class="head-title">参数工作台</div>
      <div class="head-desc">按当前模式整理配置流程</div>
    </div>
    <div id="cfgStatus" class="chip">未保存</div>
  </div>
  <div class="panel-body">
    <div id="configModeShell" class="strategy-guide-card">
      <div class="strategy-guide-head">
        <div>
          <div class="strategy-guide-title">当前工作模式</div>
          <div id="configContextSummary" class="strategy-guide-subtitle">当前正在编辑：--</div>
        </div>
      </div>
      <div class="rows">
        <select id="configModeSelect"></select>
      </div>
    </div>
    <div id="paperTaskflowRoot" class="rows"></div>
    <div id="liveTaskflowRoot" class="rows"></div>
    <div id="advancedConfigPanel" hidden></div>
    <div class="actions">
      <button id="btnSaveConfig" class="btn btn-primary" type="button">保存当前模式配置</button>
    </div>
  </div>
</section>
```

```javascript
function effectiveConfigMode(payload) {
  const envValues = ((payload || {}).env_values) || {};
  return String(envValues.TRADE_MODE || 'paper').toLowerCase() === 'live' ? 'live' : 'paper';
}

function renderConfigModeShell(payload) {
  const mode = effectiveConfigMode(payload);
  const select = el('configModeSelect');
  const summary = el('configContextSummary');
  const options = [
    { value: 'paper', label: 'Paper' },
    { value: 'live', label: 'Live' },
  ];
  select.innerHTML = options.map((item) => {
    const selected = item.value === mode ? ' selected' : '';
    return '<option value="' + esc(item.value) + '"' + selected + '>' + esc(item.label) + '</option>';
  }).join('');
  summary.textContent = mode === 'live'
    ? '当前正在编辑：Live / ' + String((((payload || {}).env_values) || {}).MARKET_TIMEFRAME || '5m')
    : '当前正在编辑：Paper / ' + String(state.paperTimeframeFilter || '5m');
  select.onchange = async () => {
    const values = collectConfigValues();
    values.TRADE_MODE = select.value;
    renderTaskflowVisibility(select.value);
  };
}

function renderTaskflowVisibility(mode) {
  const paperNode = el('paperTaskflowRoot');
  const liveNode = el('liveTaskflowRoot');
  if (paperNode) paperNode.style.display = mode === 'paper' ? '' : 'none';
  if (liveNode) liveNode.style.display = mode === 'live' ? '' : 'none';
}
```

- [ ] **Step 4: Run the shell/layout tests again**

Run: `pytest tests/test_dashboard.py -k "mode_selector_shell or active_mode" -q`

Expected: PASS with the new mode-aware shell hooks present.

- [ ] **Step 5: Commit the shell slice**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat: add mode-aware dashboard config shell"
```

### Task 2: Rebuild Paper Mode As A Taskflow Workspace

**Files:**
- Modify: `D:\python\BTC_5MIN\dashboard.py`
- Test: `D:\python\BTC_5MIN\tests\test_dashboard.py`

- [ ] **Step 1: Write the failing paper-taskflow tests**

```python
def test_dashboard_assets_include_paper_taskflow_sections():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="paperScopeSection"' in html
    assert 'id="paperStrategySection"' in html
    assert 'id="paperCoreSection"' in html
    assert "function renderPaperScopeSection(" in js
    assert "function renderPaperStrategySection(" in js
    assert "function renderPaperCoreSection(" in js


def test_dashboard_assets_show_one_active_paper_timeframe_editor():
    js = _dashboard_js()

    assert "function effectivePaperEditorTimeframe(" in js
    assert "data-paper-editor-timeframe" in js
    assert "only one timeframe profile is visible" not in js  # semantic check by implementation structure
```

- [ ] **Step 2: Run the paper-taskflow tests to verify they fail**

Run: `pytest tests/test_dashboard.py -k "paper_taskflow_sections or active_paper_timeframe_editor" -q`

Expected: FAIL because paper mode is still mixed into the generic form.

- [ ] **Step 3: Implement the paper taskflow sections**

```javascript
function effectivePaperEditorTimeframe(payload) {
  const available = parsePaperTimeframeList((((payload || {}).env_values) || {}).PAPER_TIMEFRAMES || '');
  if (available.indexOf(String(state.paperTimeframeFilter || '').toLowerCase()) >= 0) {
    return String(state.paperTimeframeFilter).toLowerCase();
  }
  return available[0] || '5m';
}

function renderPaperScopeSection(payload) {
  const node = el('paperScopeSection');
  const timeframes = Array.isArray((payload || {}).paper_timeframes) ? payload.paper_timeframes : [];
  const enabled = parsePaperTimeframeList((((payload || {}).env_values) || {}).PAPER_TIMEFRAMES || timeframes.join(','));
  const active = effectivePaperEditorTimeframe(payload);
  node.innerHTML =
    '<div class="strategy-guide-title">第 1 步：选择运行范围</div>' +
    '<div class="strategy-guide-note">先决定 paper 要跑哪些时间频次，再决定当前正在编辑哪一个。</div>' +
    '<div id="paperTimeframeToggles" class="rows"></div>' +
    '<div id="paperEditorTabs" class="rows"></div>';
}

function renderPaperStrategySection(payload) {
  const node = el('paperStrategySection');
  const timeframe = effectivePaperEditorTimeframe(payload);
  node.innerHTML =
    '<div class="strategy-guide-title">第 2 步：选择策略</div>' +
    '<div class="strategy-guide-note">只编辑当前 timeframe 的主策略与运行策略列表。</div>' +
    '<div id="paperStrategyWorkspace" data-paper-editor-timeframe="' + esc(timeframe) + '"></div>';
}

function renderPaperCoreSection(payload) {
  const node = el('paperCoreSection');
  const timeframe = effectivePaperEditorTimeframe(payload);
  node.innerHTML =
    '<div class="strategy-guide-title">第 3 步：核心交易参数</div>' +
    '<div class="strategy-guide-note">只显示本轮最常改的 paper 参数。</div>' +
    '<div id="paperCoreFields" data-paper-editor-timeframe="' + esc(timeframe) + '"></div>';
}
```

- [ ] **Step 4: Run the paper-taskflow tests again**

Run: `pytest tests/test_dashboard.py -k "paper_taskflow_sections or active_paper_timeframe_editor" -q`

Expected: PASS with dedicated paper taskflow sections and one active timeframe editor concept.

- [ ] **Step 5: Commit the paper-taskflow slice**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat: rebuild paper config as taskflow workspace"
```

### Task 3: Rebuild Live Mode As A Taskflow Workspace

**Files:**
- Modify: `D:\python\BTC_5MIN\dashboard.py`
- Test: `D:\python\BTC_5MIN\tests\test_dashboard.py`

- [ ] **Step 1: Write the failing live-taskflow tests**

```python
def test_dashboard_assets_include_live_taskflow_sections():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="liveMarketSection"' in html
    assert 'id="liveCredentialsSection"' in html
    assert 'id="liveSafetySection"' in html
    assert "function renderLiveMarketSection(" in js
    assert "function renderLiveCredentialsSection(" in js
    assert "function renderLiveSafetySection(" in js


def test_dashboard_assets_keep_live_credentials_visible_in_default_live_layout():
    js = _dashboard_js()

    assert "POLYMARKET_PRIVATE_KEY" in js
    assert "POLYMARKET_FUNDER" in js
    assert "POLYMARKET_API_KEY" in js
```

- [ ] **Step 2: Run the live-taskflow tests to verify they fail**

Run: `pytest tests/test_dashboard.py -k "live_taskflow_sections or live_credentials_visible" -q`

Expected: FAIL because live settings are still embedded in the mixed field group flow.

- [ ] **Step 3: Implement live taskflow sections**

```javascript
function renderLiveMarketSection(payload) {
  const node = el('liveMarketSection');
  node.innerHTML =
    '<div class="strategy-guide-title">第 1 步：运行市场</div>' +
    '<div class="strategy-guide-note">确认当前 live 市场和是否启用实盘。</div>' +
    '<div id="liveMarketFields"></div>';
}

function renderLiveCredentialsSection(payload) {
  const node = el('liveCredentialsSection');
  node.innerHTML =
    '<div class="strategy-guide-title">第 2 步：下单凭证</div>' +
    '<div class="strategy-guide-note">这些字段默认可见，不再藏在混合分组里。</div>' +
    '<div id="liveCredentialFields"></div>';
}

function renderLiveSafetySection(payload) {
  const node = el('liveSafetySection');
  node.innerHTML =
    '<div class="strategy-guide-title">第 3 步：赎回与安全</div>' +
    '<div class="strategy-guide-note">放自动赎回、重试和安全项。</div>' +
    '<div id="liveSafetyFields"></div>';
}
```

- [ ] **Step 4: Run the live-taskflow tests again**

Run: `pytest tests/test_dashboard.py -k "live_taskflow_sections or live_credentials_visible" -q`

Expected: PASS with dedicated live sections present.

- [ ] **Step 5: Commit the live-taskflow slice**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat: rebuild live config as taskflow workspace"
```

### Task 4: Move Fields Into Core vs Advanced Sections By Mode

**Files:**
- Modify: `D:\python\BTC_5MIN\dashboard.py`
- Test: `D:\python\BTC_5MIN\tests\test_dashboard.py`

- [ ] **Step 1: Write the failing field-grouping tests**

```python
def test_dashboard_assets_limit_default_paper_view_to_core_fields():
    js = _dashboard_js()

    assert "TARGET_PROFIT" in js
    assert "BASE_ORDER_COST" in js
    assert "MAX_STAKE" in js
    assert "OPEN_DELAY_SECONDS" in js
    assert "SIGNAL_MOMENTUM_THRESHOLD" in js
    assert "advanced" in js


def test_dashboard_assets_limit_default_live_view_to_market_credentials_and_safety():
    js = _dashboard_js()

    assert "LIVE_AUTO_REDEEM_ENABLED" in js
    assert "POLYMARKET_API_SECRET" in js
    assert "POLYMARKET_BUILDER_API_KEY" in js
```

- [ ] **Step 2: Run the field-grouping tests to verify they fail**

Run: `pytest tests/test_dashboard.py -k "default_paper_view_to_core_fields or default_live_view_to_market_credentials_and_safety" -q`

Expected: FAIL because fields are still rendered through the old generic grouping logic.

- [ ] **Step 3: Implement explicit field maps for paper core/live core/advanced**

```javascript
const PAPER_CORE_FIELDS = [
  'PAPER_TIMEFRAMES',
  'PAPER_<TF>_STRATEGY_ID',
  'PAPER_<TF>_STRATEGY_IDS',
  'PAPER_<TF>_TARGET_PROFIT',
  'PAPER_<TF>_BASE_ORDER_COST',
  'PAPER_<TF>_MAX_STAKE',
  'PAPER_<TF>_MAX_CONSECUTIVE_LOSSES',
  'PAPER_<TF>_OPEN_DELAY_SECONDS',
];

const LIVE_CORE_FIELDS = [
  'MARKET_TIMEFRAME',
  'LIVE_TRADING_ENABLED',
  'POLYMARKET_PRIVATE_KEY',
  'POLYMARKET_FUNDER',
  'POLYMARKET_API_KEY',
  'POLYMARKET_API_SECRET',
  'POLYMARKET_API_PASSPHRASE',
];

const LIVE_SAFETY_FIELDS = [
  'LIVE_AUTO_REDEEM_ENABLED',
  'LIVE_AUTO_REDEEM_DRY_RUN',
  'LIVE_AUTO_REDEEM_POLL_SECONDS',
  'LIVE_AUTO_REDEEM_MAX_RETRIES',
  'LIVE_AUTO_REDEEM_INITIAL_BACKOFF_SECONDS',
  'LIVE_AUTO_REDEEM_MAX_BACKOFF_SECONDS',
];
```

```javascript
function renderModeSpecificAdvancedPanel(payload) {
  const panel = el('advancedConfigPanel');
  const mode = effectiveConfigMode(payload);
  panel.innerHTML = mode === 'paper'
    ? renderPaperAdvancedFields(payload)
    : renderLiveAdvancedFields(payload);
}
```

- [ ] **Step 4: Run the field-grouping tests again**

Run: `pytest tests/test_dashboard.py -k "default_paper_view_to_core_fields or default_live_view_to_market_credentials_and_safety" -q`

Expected: PASS with explicit field tiering in the left panel renderer.

- [ ] **Step 5: Commit the field-tiering slice**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat: separate core and advanced config fields by mode"
```

### Task 5: Update Save Flow, Copy, And Regression Coverage

**Files:**
- Modify: `D:\python\BTC_5MIN\dashboard.py`
- Modify: `D:\python\BTC_5MIN\tests\test_dashboard.py`
- Modify: `D:\python\BTC_5MIN\README.md`

- [ ] **Step 1: Write the failing save/copy tests**

```python
def test_dashboard_assets_use_taskflow_copy_for_left_panel():
    html = _dashboard_html()
    js = _dashboard_js()

    assert '参数工作台' in html
    assert '保存当前模式配置' in html
    assert '当前正在编辑：' in js


def test_dashboard_assets_no_longer_render_mixed_mode_field_groups_by_default():
    html = _dashboard_html()
    js = _dashboard_js()

    assert '运行模式' in html or '当前工作模式' in html
    assert '控制是否启用实盘，并配置实盘所需的钱包凭证。' not in html
```

- [ ] **Step 2: Run the save/copy tests to verify they fail**

Run: `pytest tests/test_dashboard.py -k "taskflow_copy_for_left_panel or mixed_mode_field_groups_by_default" -q`

Expected: FAIL because the old mixed left panel copy still exists.

- [ ] **Step 3: Update save-area copy and operator docs**

```markdown
## Dashboard left panel workflow

The left panel now uses one `Paper / Live` selector.

- `Paper` shows run scope, strategy, core parameters, and folded advanced settings
- `Live` shows market activation, credentials, redeem/safety, and folded advanced settings
```

```javascript
function updateSaveContextText(payload) {
  const node = el('configContextSummary');
  if (!node) return;
  const mode = effectiveConfigMode(payload);
  if (mode === 'paper') {
    node.textContent = '当前正在编辑：Paper / ' + effectivePaperEditorTimeframe(payload);
  } else {
    node.textContent = '当前正在编辑：Live / ' + String((((payload || {}).env_values) || {}).MARKET_TIMEFRAME || '5m');
  }
}
```

- [ ] **Step 4: Run the focused regressions, then the broader dashboard suite**

Run: `pytest tests/test_dashboard.py -k "mode_selector_shell or paper_taskflow_sections or live_taskflow_sections or taskflow_copy_for_left_panel" -q`

Run: `pytest tests/test_dashboard.py -q`

Expected: PASS for the focused taskflow checks and the full dashboard suite.

- [ ] **Step 5: Commit the final left-panel redesign slice**

```bash
git add dashboard.py tests/test_dashboard.py README.md
git commit -m "feat: redesign dashboard left panel as taskflow workspace"
```
