# Strategy 7 Consensus Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `strategy_id=7` as a conservative consensus strategy that only trades when Binance OFI and Polymarket momentum agree and all quality filters pass.

**Architecture:** Keep strategy 7 as a rule-based runtime strategy centered in `trader.py`, with small config and dashboard extensions around it. Reuse the existing strategy-5 momentum context and strategy-6 OFI signal source, but isolate strategy 7 from weak-signal fallback semantics and expose new skip reasons and diagnostics end-to-end.

**Tech Stack:** Python 3, dataclasses, inline dashboard HTML/CSS/JS generation in `dashboard.py`, pytest, existing optimizer candidate generation in `optimizer.py`.

---

## File Structure

- `D:/python/BTC_5MIN/config.py`
  - Add the five new `STRATEGY7_*` config values to `AppConfig`.
- `D:/python/BTC_5MIN/trader.py`
  - Implement strategy-7-specific consensus and quality-filter logic inside `_resolve_side_from_strategy()`.
  - Add strategy-7 skip reasons and strategy-view payload support.
- `D:/python/BTC_5MIN/dashboard.py`
  - Add strategy 7 to catalog and config surfaces.
  - Add strategy-7-only field labels, help text, field scope, and market diagnostics.
- `D:/python/BTC_5MIN/optimizer.py`
  - Add first-pass strategy-7 candidate generation for OFI threshold, momentum threshold, and max entry price.
- `D:/python/BTC_5MIN/tests/test_trader_runtime_and_live.py`
  - Add runtime-level strategy 7 decision tests.
- `D:/python/BTC_5MIN/tests/test_dashboard.py`
  - Add config payload and UI coverage for strategy 7.
- `D:/python/BTC_5MIN/tests/test_optimizer.py`
  - Add strategy-7 optimizer candidate coverage.

### Task 1: Add Strategy 7 Config Surface

**Files:**
- Modify: `D:/python/BTC_5MIN/config.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dashboard.py
def test_dashboard_config_payload_includes_strategy7_fields(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        payload = state.get_config_payload()
        assert payload['select_options']['STRATEGY_ID'] == ['1', '2', '3', '4', '5', '6', '7']
        assert payload['labels']['STRATEGY7_OFI_THRESHOLD'] == '策略7 OFI阈值'
        assert payload['labels']['STRATEGY7_MOMENTUM_THRESHOLD'] == '策略7 动量阈值'
        assert payload['field_scope']['STRATEGY7_OFI_THRESHOLD'] == 'strategy_7_only'
        assert 'STRATEGY7_MAX_ENTRY_PRICE' in payload['editable_keys']
    finally:
        state.close()


def test_dashboard_update_config_accepts_strategy7_values(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    state = DashboardState(env_file=env_file)
    try:
        payload = state.update_config({
            'STRATEGY_ID': '7',
            'PAPER_STRATEGY_IDS': '7',
            'STRATEGY7_OFI_THRESHOLD': '0.7',
            'STRATEGY7_MOMENTUM_THRESHOLD': '0.025',
            'STRATEGY7_MAX_ENTRY_PRICE': '0.54',
            'STRATEGY7_MIN_SIGNAL_GAP': '0.03',
            'STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS': '12',
        })
        assert payload['env_values']['STRATEGY_ID'] == '7'
        assert payload['env_values']['STRATEGY7_OFI_THRESHOLD'] == '0.7'
        assert payload['env_values']['STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS'] == '12'
    finally:
        state.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_config_payload_includes_strategy7_fields tests/test_dashboard.py::test_dashboard_update_config_accepts_strategy7_values -v
```

Expected:

- FAIL because `STRATEGY_ID` does not include `7`
- FAIL because `STRATEGY7_*` labels and editable keys do not exist yet

- [ ] **Step 3: Add minimal config support**

```python
# config.py
strategy7_ofi_threshold: float = field(default_factory=lambda: _env_float("STRATEGY7_OFI_THRESHOLD", 0.7))
strategy7_momentum_threshold: float = field(default_factory=lambda: _env_float("STRATEGY7_MOMENTUM_THRESHOLD", 0.025))
strategy7_max_entry_price: float = field(default_factory=lambda: _env_float("STRATEGY7_MAX_ENTRY_PRICE", 0.54))
strategy7_min_signal_gap: float = field(default_factory=lambda: _env_float("STRATEGY7_MIN_SIGNAL_GAP", 0.03))
strategy7_confirm_before_entry_seconds: int = field(default_factory=lambda: _env_int("STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS", 12))
```

- [ ] **Step 4: Wire config metadata into the dashboard payload**

```python
# dashboard.py
"STRATEGY_ID": ["1", "2", "3", "4", "5", "6", "7"],
"STRATEGY7_OFI_THRESHOLD": "策略7 OFI阈值",
"STRATEGY7_MOMENTUM_THRESHOLD": "策略7 动量阈值",
"STRATEGY7_MAX_ENTRY_PRICE": "策略7 最高入场价",
"STRATEGY7_MIN_SIGNAL_GAP": "策略7 最小信号优势",
"STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "策略7 最晚确认秒数",
```

- [ ] **Step 5: Re-run the focused tests**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_config_payload_includes_strategy7_fields tests/test_dashboard.py::test_dashboard_update_config_accepts_strategy7_values -v
```

Expected:

- PASS

- [ ] **Step 6: Commit**

```powershell
git add config.py dashboard.py tests/test_dashboard.py
git commit -m "feat: add strategy7 config surface"
```

### Task 2: Implement Strategy 7 Runtime Decision Logic

**Files:**
- Modify: `D:/python/BTC_5MIN/trader.py`
- Modify: `D:/python/BTC_5MIN/tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trader_runtime_and_live.py
def test_strategy7_returns_up_when_ofi_and_momentum_agree():
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_max_entry_price=0.56,
        strategy7_min_signal_gap=0.01,
        strategy7_confirm_before_entry_seconds=12,
        binance_signal_stale_seconds=2.0,
    )
    state = SessionState(round_index=0, signal_round_slug='s1', signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug='s1',
        up_price=0.54,
        up_best_ask=0.54,
        strategy6_ofi_score=0.8,
        fetched_at=datetime.now(timezone.utc),
        strategy6_signal_at=datetime.now(timezone.utc),
    )
    now = datetime.now(timezone.utc)

    decision = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug='s1',
        quote=quote,
        now=now,
        entry_time=now + timedelta(seconds=20),
    )

    assert decision.side == 'UP'
    assert decision.reason is None
    assert decision.signal_delta == pytest.approx(0.04)


def test_strategy7_skips_when_signals_conflict():
    cfg = AppConfig(strategy_id=7, strategy7_ofi_threshold=0.65, strategy7_momentum_threshold=0.02)
    state = SessionState(round_index=0, signal_round_slug='s1', signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug='s1',
        up_price=0.47,
        up_best_ask=0.47,
        strategy6_ofi_score=0.8,
        fetched_at=datetime.now(timezone.utc),
        strategy6_signal_at=datetime.now(timezone.utc),
    )

    decision = _resolve_side_from_strategy(cfg=cfg, state=state, slug='s1', quote=quote)

    assert decision.side is None
    assert decision.reason == 'strategy7_signal_conflict'


def test_strategy7_skips_when_confirmation_is_too_late():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_confirm_before_entry_seconds=15,
    )
    state = SessionState(round_index=0, signal_round_slug='s1', signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug='s1',
        up_price=0.54,
        up_best_ask=0.54,
        strategy6_ofi_score=0.8,
        fetched_at=now,
        strategy6_signal_at=now,
    )

    decision = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug='s1',
        quote=quote,
        now=now,
        entry_time=now + timedelta(seconds=10),
    )

    assert decision.side is None
    assert decision.reason == 'strategy7_entry_too_late'
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_trader_runtime_and_live.py -k strategy7 -v
```

Expected:

- FAIL because strategy 7 branch does not exist yet

- [ ] **Step 3: Add strategy-7-specific helper checks**

```python
# trader.py
def _strategy7_signal_gap_ok(*, ofi_score: float, momentum_delta: float, cfg: AppConfig) -> bool:
    return (
        abs(ofi_score) >= cfg.strategy7_ofi_threshold + cfg.strategy7_min_signal_gap
        and abs(momentum_delta) >= cfg.strategy7_momentum_threshold + cfg.strategy7_min_signal_gap
    )
```

- [ ] **Step 4: Add the strategy 7 branch**

```python
# trader.py inside _resolve_side_from_strategy()
if cfg.strategy_id == 7:
    now = now or datetime.now(timezone.utc)
    ofi_score = _resolve_strategy6_ofi_score(quote)
    if ofi_score is None:
        return SideDecision(side=None, reason='strategy7_ofi_unavailable')
    if _is_strategy6_signal_stale(quote=quote, now=now, stale_seconds=cfg.binance_signal_stale_seconds):
        return SideDecision(side=None, reason='strategy7_ofi_stale', signal_delta=ofi_score)
    if abs(ofi_score) < cfg.strategy7_ofi_threshold:
        return SideDecision(side=None, reason='strategy7_ofi_too_weak', signal_delta=ofi_score)

    signal_current_up_price = _resolve_signal_up_price(quote)
    signal_open_up_price = state.signal_round_open_up_price
    if not (_is_valid_signal_price(signal_open_up_price) and _is_valid_signal_price(signal_current_up_price)):
        return SideDecision(side=None, reason='strategy7_momentum_unavailable')

    momentum_delta = signal_current_up_price - signal_open_up_price
    if abs(momentum_delta) < cfg.strategy7_momentum_threshold:
        return SideDecision(side=None, reason='strategy7_momentum_too_weak', signal_delta=momentum_delta)
    if ofi_score * momentum_delta <= 0:
        return SideDecision(side=None, reason='strategy7_signal_conflict', signal_delta=momentum_delta)
    if entry_time is not None and (entry_time - now).total_seconds() < cfg.strategy7_confirm_before_entry_seconds:
        return SideDecision(side=None, reason='strategy7_entry_too_late', signal_delta=momentum_delta)
    if quote.up_price is not None and float(quote.up_price) > cfg.strategy7_max_entry_price:
        return SideDecision(side=None, reason='strategy7_price_too_high', signal_delta=momentum_delta)
    if not _strategy7_signal_gap_ok(ofi_score=ofi_score, momentum_delta=momentum_delta, cfg=cfg):
        return SideDecision(side=None, reason='strategy7_confidence_too_low', signal_delta=momentum_delta)
    return SideDecision(side='UP' if momentum_delta > 0 else 'DOWN', signal_delta=momentum_delta)
```

- [ ] **Step 5: Re-run the focused runtime tests**

Run:

```powershell
pytest tests/test_trader_runtime_and_live.py -k strategy7 -v
```

Expected:

- PASS

- [ ] **Step 6: Commit**

```powershell
git add trader.py tests/test_trader_runtime_and_live.py
git commit -m "feat: add strategy7 consensus runtime"
```

### Task 3: Add Strategy 7 Dashboard Diagnostics

**Files:**
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dashboard.py
def test_dashboard_assets_include_strategy7_copy_and_reasons():
    html = _dashboard_html()
    js = _dashboard_js()

    assert '7 | OFI+动量共识' in js
    assert 'strategy7_signal_conflict' in js
    assert 'strategy7_confidence_too_low' in js
    assert 'OFI+动量需同向确认' in js
    assert '策略7 OFI阈值' in js


def test_dashboard_market_payload_can_show_strategy7_view(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text('STRATEGY_ID=7\nPAPER_STRATEGY_IDS=7\n', encoding='utf-8')
    state = DashboardState(env_file=env_file)
    try:
        payload = state.get_market_payload(strategy='7')
        assert payload['strategy_view']['selected'] == '7'
    finally:
        state.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_dashboard.py -k strategy7 -v
```

Expected:

- FAIL because strategy 7 catalog and reasons are not exposed yet

- [ ] **Step 3: Add strategy 7 labels, reason strings, and catalog metadata**

```python
# dashboard.py
"7": {
    "label": "OFI+动量共识",
    "summary": "只有 Binance OFI 和 Polymarket 动量同向时才允许交易。",
    "preview": ["OFI", "MOMENTUM", "THRESHOLD", "SKIP"],
    "detail": "更偏向少做、做高质量信号。",
}
```

- [ ] **Step 4: Add strategy-7-specific diagnostics to the market payload renderer**

```python
# dashboard.py market payload
"strategy7": {
    "enabled": strategy_view["selected"] == "7",
    "ofi_score": ...,
    "momentum_delta": ...,
    "agreement": ...,
    "quality_gate": ...,
}
```

- [ ] **Step 5: Re-run focused dashboard tests**

Run:

```powershell
pytest tests/test_dashboard.py -k strategy7 -v
```

Expected:

- PASS

- [ ] **Step 6: Commit**

```powershell
git add dashboard.py tests/test_dashboard.py
git commit -m "feat: expose strategy7 diagnostics"
```

### Task 4: Add Strategy 7 Optimizer Support

**Files:**
- Modify: `D:/python/BTC_5MIN/optimizer.py`
- Modify: `D:/python/BTC_5MIN/tests/test_optimizer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_optimizer.py
def test_build_candidate_configs_creates_strategy7_parameter_bundles():
    cfg = AppConfig()

    candidates = build_candidate_configs(
        cfg,
        strategy_ids=[7],
        target_profits=[1.0],
        max_price_thresholds=[0.55],
        strategy5_thresholds=[0.012],
    )

    assert len(candidates) > 0
    assert all(candidate.base_strategy_id == 7 for candidate in candidates)
    assert all('STRATEGY7_OFI_THRESHOLD' in candidate.params for candidate in candidates)
    assert all('STRATEGY7_MOMENTUM_THRESHOLD' in candidate.params for candidate in candidates)
    assert all('STRATEGY7_MAX_ENTRY_PRICE' in candidate.params for candidate in candidates)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_optimizer.py::test_build_candidate_configs_creates_strategy7_parameter_bundles -v
```

Expected:

- FAIL because strategy 7 is treated like a generic strategy without its own param bundle

- [ ] **Step 3: Add first-pass candidate generation**

```python
# optimizer.py
if strategy_id == 7:
    for ofi_threshold, momentum_threshold, max_entry_price in product(
        [0.65, 0.7, 0.75],
        [0.02, 0.025, 0.03],
        [0.53, 0.54, 0.55],
    ):
        params = {
            "TARGET_PROFIT": float(target_profit),
            "STRATEGY7_OFI_THRESHOLD": float(ofi_threshold),
            "STRATEGY7_MOMENTUM_THRESHOLD": float(momentum_threshold),
            "STRATEGY7_MAX_ENTRY_PRICE": float(max_entry_price),
        }
```

- [ ] **Step 4: Re-run the focused optimizer test**

Run:

```powershell
pytest tests/test_optimizer.py::test_build_candidate_configs_creates_strategy7_parameter_bundles -v
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```powershell
git add optimizer.py tests/test_optimizer.py
git commit -m "feat: add strategy7 optimizer candidates"
```

### Task 5: Final Verification

**Files:**
- Modify: `D:/python/BTC_5MIN/config.py`
- Modify: `D:/python/BTC_5MIN/trader.py`
- Modify: `D:/python/BTC_5MIN/dashboard.py`
- Modify: `D:/python/BTC_5MIN/optimizer.py`
- Modify: `D:/python/BTC_5MIN/tests/test_dashboard.py`
- Modify: `D:/python/BTC_5MIN/tests/test_trader_runtime_and_live.py`
- Modify: `D:/python/BTC_5MIN/tests/test_optimizer.py`

- [ ] **Step 1: Run focused strategy 7 coverage**

Run:

```powershell
pytest tests/test_dashboard.py -k strategy7 -v
pytest tests/test_trader_runtime_and_live.py -k strategy7 -v
pytest tests/test_optimizer.py -k strategy7 -v
```

Expected:

- PASS across all new strategy-7-specific cases

- [ ] **Step 2: Run broader strategy regression coverage**

Run:

```powershell
pytest tests/test_dashboard.py -k strategy -v
pytest tests/test_optimizer.py -v
pytest tests/test_strategy.py -v
```

Expected:

- PASS with no regressions to strategies 1-6

- [ ] **Step 3: Commit**

```powershell
git add config.py trader.py dashboard.py optimizer.py tests/test_dashboard.py tests/test_trader_runtime_and_live.py tests/test_optimizer.py
git commit -m "feat: add strategy7 consensus filter"
```

## Self-Review

- Spec coverage:
  - New config keys: covered in Task 1
  - Runtime consensus logic and skip reasons: covered in Task 2
  - Dashboard diagnostics and operator copy: covered in Task 3
  - Optimizer integration: covered in Task 4
  - Verification criteria and regression safety: covered in Task 5
- Placeholder scan:
  - No `TODO`, `TBD`, or abstract “handle edge cases” instructions remain
- Type consistency:
  - Reused existing `AppConfig`, `SideDecision`, `SessionState`, `MarketQuote`, and `DashboardState` names consistently
