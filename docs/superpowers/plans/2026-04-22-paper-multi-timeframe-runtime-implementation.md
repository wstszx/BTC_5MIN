# Paper Multi-Timeframe Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add paper-only multi-timeframe runtime support so `5m` and `15m` paper trading can run in parallel with independent strategy lists and independent parameter profiles, while live mode remains single-timeframe.

**Architecture:** Extend config parsing to expose paper timeframe profiles, keep live on the existing top-level single-timeframe path, add a paper supervisor in `main.py` that launches one `run_paper_trading` worker per enabled timeframe with isolated state/log paths, and evolve the dashboard into per-timeframe paper profile editors plus separate `Paper 5m` / `Paper 15m` runtime cards.

**Tech Stack:** Python dataclasses, existing `AppConfig` / `main.py` / `trader.py` runtime architecture, embedded dashboard HTML/CSS/JS in `dashboard.py`, pytest.

---

### Task 1: Add Paper Timeframe Profile Parsing With Backward Compatibility

**Files:**
- Modify: `D:\python\BTC_5MIN\config.py`
- Test: `D:\python\BTC_5MIN\tests\test_config_encoding.py`

- [ ] **Step 1: Write the failing config tests**

```python
def test_build_config_parses_enabled_paper_timeframes_and_profile_values():
    cfg = build_config_from_env_values(
        {
            "TRADE_MODE": "paper",
            "PAPER_TIMEFRAMES": "5m,15m",
            "PAPER_5M_STRATEGY_ID": "5",
            "PAPER_5M_STRATEGY_IDS": "5,6",
            "PAPER_5M_TARGET_PROFIT": "0.8",
            "PAPER_15M_STRATEGY_ID": "2",
            "PAPER_15M_STRATEGY_IDS": "1,2,7",
            "PAPER_15M_TARGET_PROFIT": "1.0",
        }
    )

    assert cfg.paper_timeframes == ["5m", "15m"]
    assert cfg.paper_profiles["5m"].strategy_id == 5
    assert cfg.paper_profiles["5m"].paper_strategy_ids == [5, 6]
    assert cfg.paper_profiles["5m"].target_profit == 0.8
    assert cfg.paper_profiles["15m"].strategy_id == 2
    assert cfg.paper_profiles["15m"].paper_strategy_ids == [1, 2, 7]
    assert cfg.paper_profiles["15m"].target_profit == 1.0


def test_build_config_keeps_live_single_timeframe_when_paper_profiles_exist():
    cfg = build_config_from_env_values(
        {
            "TRADE_MODE": "live",
            "MARKET_TIMEFRAME": "15m",
            "PAPER_TIMEFRAMES": "5m,15m",
            "PAPER_5M_STRATEGY_IDS": "5,6",
            "PAPER_15M_STRATEGY_IDS": "1,2",
        }
    )

    assert cfg.market_timeframe == "15m"
    assert cfg.series_slug == "btc-up-or-down-15m"
    assert cfg.paper_timeframes == ["5m", "15m"]


def test_build_config_uses_legacy_single_timeframe_paper_fields_when_paper_timeframes_missing():
    cfg = build_config_from_env_values(
        {
            "TRADE_MODE": "paper",
            "MARKET_TIMEFRAME": "15m",
            "STRATEGY_ID": "7",
            "PAPER_STRATEGY_IDS": "7,6",
            "TARGET_PROFIT": "1.1",
        }
    )

    assert cfg.paper_timeframes == ["15m"]
    assert cfg.paper_profiles["15m"].strategy_id == 7
    assert cfg.paper_profiles["15m"].paper_strategy_ids == [7, 6]
    assert cfg.paper_profiles["15m"].target_profit == 1.1
```

- [ ] **Step 2: Run the config tests to verify they fail first**

Run: `pytest tests/test_config_encoding.py -k "paper_timeframes or live_single_timeframe or legacy_single_timeframe_paper_fields" -q`

Expected: FAIL with missing `paper_timeframes` / `paper_profiles` attributes or equivalent assertion failures.

- [ ] **Step 3: Implement paper profile parsing in `config.py`**

```python
@dataclass(frozen=True, slots=True)
class PaperTimeframeProfile:
    timeframe: str
    strategy_id: int
    paper_strategy_ids: list[int]
    target_profit: float
    bet_sizing_mode: str
    base_order_cost: float
    max_consecutive_losses: int
    max_stake: float | None
    open_delay_seconds: int
    signal_momentum_threshold: float
    ofi_threshold: float
    binance_signal_stale_seconds: float
    strategy7_ofi_threshold: float
    strategy7_momentum_threshold: float
    strategy7_max_entry_price: float


def _env_paper_timeframes(default_timeframe: str) -> list[str]:
    raw = (os.getenv("PAPER_TIMEFRAMES") or "").strip().lower()
    if not raw:
        return [default_timeframe]
    result: list[str] = []
    for item in raw.split(","):
        candidate = item.strip().lower()
        if candidate in MARKET_TIMEFRAME_DEFINITIONS and candidate not in result:
            result.append(candidate)
    return result or [default_timeframe]


def _paper_profile_value(prefix: str, key: str, fallback: str | None) -> str | None:
    scoped = os.getenv(f"{prefix}_{key}")
    if scoped is not None:
        return scoped
    return fallback
```

```python
@dataclass(slots=True)
class AppConfig:
    paper_timeframes: list[str] = field(default_factory=lambda: _env_paper_timeframes(_env_market_timeframe("5m")))
    paper_profiles: dict[str, PaperTimeframeProfile] = field(init=False)

    def __post_init__(self) -> None:
        self.paper_profiles = {}
        for timeframe in self.paper_timeframes:
            prefix = f"PAPER_{timeframe.upper()}"
            strategy_id = _env_int(f"{prefix}_STRATEGY_ID", self.strategy_id)
            strategy_ids = _parse_strategy_id_list(
                os.getenv(f"{prefix}_STRATEGY_IDS"),
                fallback=strategy_id,
            )
            self.paper_profiles[timeframe] = PaperTimeframeProfile(
                timeframe=timeframe,
                strategy_id=strategy_id,
                paper_strategy_ids=strategy_ids,
                target_profit=_env_float(f"{prefix}_TARGET_PROFIT", self.target_profit),
                bet_sizing_mode=(os.getenv(f"{prefix}_BET_SIZING_MODE") or self.bet_sizing_mode).upper(),
                base_order_cost=_env_float(f"{prefix}_BASE_ORDER_COST", self.base_order_cost),
                max_consecutive_losses=_env_int(f"{prefix}_MAX_CONSECUTIVE_LOSSES", self.max_consecutive_losses),
                max_stake=_env_optional_float(f"{prefix}_MAX_STAKE") if os.getenv(f"{prefix}_MAX_STAKE") is not None else self.max_stake,
                open_delay_seconds=_env_int(f"{prefix}_OPEN_DELAY_SECONDS", self.open_delay_seconds),
                signal_momentum_threshold=_env_float(f"{prefix}_SIGNAL_MOMENTUM_THRESHOLD", self.signal_momentum_threshold),
                ofi_threshold=_env_float(f"{prefix}_OFI_THRESHOLD", self.ofi_threshold),
                binance_signal_stale_seconds=_env_float(f"{prefix}_BINANCE_SIGNAL_STALE_SECONDS", self.binance_signal_stale_seconds),
                strategy7_ofi_threshold=_env_float(f"{prefix}_STRATEGY7_OFI_THRESHOLD", self.strategy7_ofi_threshold),
                strategy7_momentum_threshold=_env_float(f"{prefix}_STRATEGY7_MOMENTUM_THRESHOLD", self.strategy7_momentum_threshold),
                strategy7_max_entry_price=_env_float(f"{prefix}_STRATEGY7_MAX_ENTRY_PRICE", self.strategy7_max_entry_price),
            )
```

- [ ] **Step 4: Run the config tests again to verify they pass**

Run: `pytest tests/test_config_encoding.py -k "paper_timeframes or live_single_timeframe or legacy_single_timeframe_paper_fields" -q`

Expected: PASS with all new config tests green.

- [ ] **Step 5: Commit the config parsing slice**

```bash
git add config.py tests/test_config_encoding.py
git commit -m "feat: parse paper timeframe profiles"
```

### Task 2: Add Paper Supervisor Startup In `main.py`

**Files:**
- Modify: `D:\python\BTC_5MIN\main.py`
- Test: `D:\python\BTC_5MIN\tests\test_runtime_launcher.py`

- [ ] **Step 1: Write the failing runtime launcher tests**

```python
def test_run_single_command_runtime_starts_one_paper_worker_per_enabled_timeframe(monkeypatch, tmp_path: Path):
    startup_cfg = build_config_from_env_values(
        {
            "TRADE_MODE": "paper",
            "PAPER_TIMEFRAMES": "5m,15m",
            "PAPER_5M_STRATEGY_ID": "5",
            "PAPER_5M_STRATEGY_IDS": "5,6",
            "PAPER_15M_STRATEGY_ID": "2",
            "PAPER_15M_STRATEGY_IDS": "1,2",
        }
    )
    calls: list[tuple[str, list[int], str, str]] = []

    def fake_run_paper_trading(cfg, *, stop_event, config_provider, state_path=None, log_path=None, runtime_control=None, stop_when_safe=None):
        calls.append((cfg.market_timeframe, list(cfg.paper_strategy_ids), str(state_path), str(log_path)))
        if len(calls) == 2:
            stop_event.set()
        return {"status": "stopped"}

    monkeypatch.setattr(main, "_load_shared_config", lambda _: startup_cfg)
    monkeypatch.setattr(main, "run_paper_trading", fake_run_paper_trading)
    monkeypatch.setattr(main, "create_dashboard_runtime", lambda **_: type("FakeDashboardRuntime", (), {"serve_forever": lambda self: None, "shutdown": lambda self: None, "close": lambda self: None})())

    exit_code = main.run_single_command_runtime(env_file=tmp_path / ".env.dashboard")

    assert exit_code == 0
    assert [call[0] for call in calls] == ["5m", "15m"]
    assert calls[0][1] == [5, 6]
    assert calls[1][1] == [1, 2]
    assert calls[0][2].endswith("logs\\paper\\5m\\session_state.json")
    assert calls[1][3].endswith("logs\\paper\\15m\\paper_trades.csv")
```

```python
def test_run_single_command_runtime_keeps_live_single_worker_when_paper_profiles_exist(monkeypatch):
    cfg = build_config_from_env_values(
        {
            "TRADE_MODE": "live",
            "MARKET_TIMEFRAME": "15m",
            "LIVE_TRADING_ENABLED": "true",
            "POLYMARKET_PRIVATE_KEY": "pk",
            "POLYMARKET_FUNDER": "0xfunder",
            "PAPER_TIMEFRAMES": "5m,15m",
        }
    )
    live_calls: list[str] = []

    monkeypatch.setattr(main, "_load_shared_config", lambda _: cfg)
    monkeypatch.setattr(main, "create_dashboard_runtime", lambda **_: type("FakeDashboardRuntime", (), {"serve_forever": lambda self: None, "shutdown": lambda self: None, "close": lambda self: None})())
    monkeypatch.setattr(main, "run_live_trading", lambda cfg, **kwargs: live_calls.append(cfg.market_timeframe) or {"status": "stopped"}, raising=False)

    exit_code = main.run_single_command_runtime()

    assert exit_code == 0
    assert live_calls == ["15m"]
```

- [ ] **Step 2: Run the runtime launcher tests to verify they fail first**

Run: `pytest tests/test_runtime_launcher.py -k "one_paper_worker_per_enabled_timeframe or keeps_live_single_worker_when_paper_profiles_exist" -q`

Expected: FAIL because `run_single_command_runtime` still starts a single paper worker.

- [ ] **Step 3: Implement paper supervisor helpers in `main.py`**

```python
def _paper_runtime_paths(cfg: AppConfig, timeframe: str) -> tuple[Path, Path]:
    base = cfg.logs_dir / "paper" / timeframe
    return base / "session_state.json", base / "paper_trades.csv"


def _paper_cfg_for_timeframe(cfg: AppConfig, timeframe: str) -> AppConfig:
    profile = cfg.paper_profiles[timeframe]
    return replace(
        cfg,
        market_timeframe=timeframe,
        strategy_id=profile.strategy_id,
        paper_strategy_ids=list(profile.paper_strategy_ids),
        target_profit=profile.target_profit,
        bet_sizing_mode=profile.bet_sizing_mode,
        base_order_cost=profile.base_order_cost,
        max_consecutive_losses=profile.max_consecutive_losses,
        max_stake=profile.max_stake,
        open_delay_seconds=profile.open_delay_seconds,
        signal_momentum_threshold=profile.signal_momentum_threshold,
        ofi_threshold=profile.ofi_threshold,
        binance_signal_stale_seconds=profile.binance_signal_stale_seconds,
        strategy7_ofi_threshold=profile.strategy7_ofi_threshold,
        strategy7_momentum_threshold=profile.strategy7_momentum_threshold,
        strategy7_max_entry_price=profile.strategy7_max_entry_price,
    )
```

```python
if active_mode == "paper":
    paper_timeframes = list(getattr(current_cfg, "paper_timeframes", []) or [current_cfg.market_timeframe])
    worker_targets = []
    for timeframe in paper_timeframes:
        paper_cfg = _paper_cfg_for_timeframe(current_cfg, timeframe)
        state_path, log_path = _paper_runtime_paths(current_cfg, timeframe)
        target = lambda paper_cfg=paper_cfg, timeframe=timeframe, state_path=state_path, log_path=log_path: run_paper_trading(
            paper_cfg,
            **_build_worker_call_kwargs(
                run_paper_trading,
                stop_event=stop_event,
                config_provider=lambda timeframe=timeframe: _paper_cfg_for_timeframe(_config_provider(), timeframe),
                runtime_control=manager.runtime_control,
                stop_when_safe=manager.restart_requested,
            ),
            state_path=state_path,
            log_path=log_path,
        )
        worker_targets.append((f"paper-trading-worker-{timeframe}", target))
```

- [ ] **Step 4: Run the runtime launcher tests again to verify they pass**

Run: `pytest tests/test_runtime_launcher.py -k "one_paper_worker_per_enabled_timeframe or keeps_live_single_worker_when_paper_profiles_exist" -q`

Expected: PASS with one paper worker per enabled timeframe and live still single-worker.

- [ ] **Step 5: Commit the runtime launcher slice**

```bash
git add main.py tests/test_runtime_launcher.py
git commit -m "feat: launch one paper worker per timeframe"
```

### Task 3: Expose Multi-Timeframe Paper Profiles In Dashboard Backend

**Files:**
- Modify: `D:\python\BTC_5MIN\dashboard.py`
- Test: `D:\python\BTC_5MIN\tests\test_dashboard.py`

- [ ] **Step 1: Write the failing dashboard backend tests**

```python
def test_dashboard_config_payload_includes_paper_timeframes_and_profiles(tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    env_file.write_text(
        "\n".join(
            [
                "TRADE_MODE=paper",
                "PAPER_TIMEFRAMES=5m,15m",
                "PAPER_5M_STRATEGY_ID=5",
                "PAPER_5M_STRATEGY_IDS=5,6",
                "PAPER_15M_STRATEGY_ID=2",
                "PAPER_15M_STRATEGY_IDS=1,2",
            ]
        ),
        encoding="utf-8",
    )
    state = DashboardState(env_file=env_file)
    try:
        payload = state.get_config_payload()
        assert payload["paper_timeframes"] == ["5m", "15m"]
        assert payload["paper_profiles"]["5m"]["strategy_id"] == "5"
        assert payload["paper_profiles"]["5m"]["paper_strategy_ids"] == ["5", "6"]
        assert payload["paper_profiles"]["15m"]["strategy_id"] == "2"
        assert payload["paper_profiles"]["15m"]["paper_strategy_ids"] == ["1", "2"]
    finally:
        state.close()
```

```python
def test_dashboard_recent_trades_payload_reads_timeframe_specific_paths(tmp_path: Path):
    logs_dir = tmp_path / "logs" / "paper"
    (logs_dir / "5m").mkdir(parents=True, exist_ok=True)
    (logs_dir / "15m").mkdir(parents=True, exist_ok=True)
    (logs_dir / "5m" / "paper_trades.csv").write_text(
        "timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason,experiment_id\n"
        "2026-04-22T10:00:00+00:00,paper,1,5,OPEN,btc-updown-5m-a,2026-04-22T09:55:00+00:00,2026-04-22T10:00:00+00:00,UP,0.5,2,1,1,UP,1,1,0,0,False,,,,,,False,,\n",
        encoding="utf-8",
    )
    (logs_dir / "15m" / "paper_trades.csv").write_text(
        "timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason,experiment_id\n"
        "2026-04-22T10:15:00+00:00,paper,1,2,OPEN,btc-updown-15m-a,2026-04-22T10:00:00+00:00,2026-04-22T10:15:00+00:00,DOWN,0.4,2.5,1,1,DOWN,1,1,0,0,False,,,,,,False,,\n",
        encoding="utf-8",
    )
    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    try:
        state._cfg.logs_dir = tmp_path / "logs"
        payload_5m = state.get_recent_trades_payload(limit=20, timeframe="5m")
        payload_15m = state.get_recent_trades_payload(limit=20, timeframe="15m")
        assert payload_5m["rows"][0]["event_slug"] == "btc-updown-5m-a"
        assert payload_15m["rows"][0]["event_slug"] == "btc-updown-15m-a"
    finally:
        state.close()
```

- [ ] **Step 2: Run the dashboard backend tests to verify they fail first**

Run: `pytest tests/test_dashboard.py -k "paper_timeframes_and_profiles or timeframe_specific_paths" -q`

Expected: FAIL because config payload does not include paper profile data and recent trades does not accept a timeframe selector.

- [ ] **Step 3: Implement backend payload and query helpers in `dashboard.py`**

```python
def _paper_runtime_dir(cfg: AppConfig, timeframe: str) -> Path:
    return cfg.logs_dir / "paper" / timeframe


def _paper_session_state_path(cfg: AppConfig, timeframe: str) -> Path:
    return _paper_runtime_dir(cfg, timeframe) / "session_state.json"


def _paper_trades_path(cfg: AppConfig, timeframe: str) -> Path:
    return _paper_runtime_dir(cfg, timeframe) / "paper_trades.csv"
```

```python
def get_config_payload(self) -> dict[str, Any]:
    with self._lock:
        env_values = dict(self._env_values)
        runtime_status = self._runtime_status()
        validation_errors = dict(self._validation_errors)
        strategy_catalog = _strategy_catalog()
        field_groups = json.loads(json.dumps(_field_groups()))
        select_options = json.loads(json.dumps(self.SELECT_OPTIONS))
        select_options["STRATEGY_ID"] = ["1", "2", "3", "4", "5", "6", "7"]
    paper_profiles = {
        timeframe: {
            "strategy_id": str(profile.strategy_id),
            "paper_strategy_ids": [str(item) for item in profile.paper_strategy_ids],
            "target_profit": _fmt_env(profile.target_profit),
            "open_delay_seconds": _fmt_env(profile.open_delay_seconds),
            "signal_momentum_threshold": _fmt_env(profile.signal_momentum_threshold),
            "ofi_threshold": _fmt_env(profile.ofi_threshold),
        }
        for timeframe, profile in self._cfg.paper_profiles.items()
    }
    return {
        "env_file": str(self.env_file),
        "env_values": self._masked_env_values(env_values),
        "timeframe_presets": json.loads(json.dumps(TIMEFRAME_PRESETS)),
        "editable_keys": list(self.EDITABLE_CONFIG_KEYS),
        "labels": self.CONFIG_LABELS,
        "select_options": select_options,
        "strategy_catalog": strategy_catalog,
        "field_groups": field_groups,
        "field_scope": self.FIELD_SCOPE,
        "field_help": self.FIELD_HELP,
        "validation_errors": validation_errors,
        "runtime_status": runtime_status,
        "saved_at": _iso(self._last_saved_at),
        "paper_timeframes": list(self._cfg.paper_timeframes),
        "paper_profiles": paper_profiles,
    }
```

```python
def get_recent_trades_payload(self, *, limit: int, strategy: int | str | None = None, timeframe: str | None = None) -> dict[str, Any]:
    with self._lock:
        cfg = self._cfg
    target_timeframe = str(timeframe or cfg.market_timeframe).strip().lower()
    paper_csv = _paper_trades_path(cfg, target_timeframe)
    state_path = _paper_session_state_path(cfg, target_timeframe)
    capped_limit = max(1, min(300, int(limit)))
    strategy_filter = _normalize_strategy_filter(strategy)
    rows = _filter_trade_rows_by_strategy(_tail_csv_rows(paper_csv, limit=capped_limit * 4), strategy_filter)
    session_state = load_session_state(state_path, effective_paper_strategy_ids=cfg.paper_profiles.get(target_timeframe, cfg).paper_strategy_ids if target_timeframe in cfg.paper_profiles else cfg.paper_strategy_ids)
    pending_items = list(getattr(session_state, "pending_paper_trades", []) or [])
    if getattr(session_state, "paper_strategies", None):
        pending_items = []
        for strategy_state in session_state.paper_strategies.values():
            pending_items.extend(getattr(strategy_state, "pending_paper_trades", []) or [])
    pending_rows = [
        _pending_paper_trade_to_recent_row(item)
        for item in _filter_pending_paper_trades_by_strategy(pending_items, strategy_filter)
    ]
    merged_rows = (pending_rows + rows)[:capped_limit]
    return {
        "csv_path": str(paper_csv),
        "strategy": strategy_filter or "all",
        "timeframe": target_timeframe,
        "count": len(merged_rows),
        "rows": merged_rows,
    }
```

- [ ] **Step 4: Run the dashboard backend tests again to verify they pass**

Run: `pytest tests/test_dashboard.py -k "paper_timeframes_and_profiles or timeframe_specific_paths" -q`

Expected: PASS with backend payloads returning paper profile data and timeframe-isolated recent trades.

- [ ] **Step 5: Commit the dashboard backend slice**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat: expose paper timeframe profiles in dashboard backend"
```

### Task 4: Render Per-Timeframe Paper Config Editors And Runtime Cards

**Files:**
- Modify: `D:\python\BTC_5MIN\dashboard.py`
- Test: `D:\python\BTC_5MIN\tests\test_dashboard.py`

- [ ] **Step 1: Write the failing frontend asset tests**

```python
def test_dashboard_assets_include_paper_profile_editor_hooks():
    js = _dashboard_js()
    html = _dashboard_html()

    assert 'id="paperProfilesRoot"' in html
    assert "function renderPaperProfiles(" in js
    assert "paper_timeframes" in js
    assert "paper_profiles" in js
    assert "PAPER_5M_" in js or "PAPER_15M_" in js


def test_dashboard_assets_include_multi_timeframe_paper_runtime_cards():
    js = _dashboard_js()
    html = _dashboard_html()

    assert 'id="paperRuntimeCards"' in html
    assert "function renderPaperRuntimeCards(" in js
    assert "Paper 5m" in js or "Paper " in js
    assert "refreshPaperRuntimeCard" in js
```

- [ ] **Step 2: Run the frontend asset tests to verify they fail first**

Run: `pytest tests/test_dashboard.py -k "paper_profile_editor_hooks or multi_timeframe_paper_runtime_cards" -q`

Expected: FAIL because the current frontend still renders a single paper runtime/config flow.

- [ ] **Step 3: Implement multi-profile editor and multi-card rendering in embedded JS/HTML**

```javascript
function renderPaperProfiles(payload) {
  const root = el('paperProfilesRoot');
  if (!root) return;
  const timeframes = (payload.paper_timeframes || []);
  const profiles = payload.paper_profiles || {};
  root.innerHTML = timeframes.map((timeframe) => {
    const profile = profiles[timeframe] || {};
    return `
      <section class="paper-profile-card" data-timeframe="${esc(timeframe)}">
        <h3>Paper ${esc(timeframe)}</h3>
        <div id="paperProfileFields_${esc(timeframe)}"></div>
      </section>
    `;
  }).join('');
}

function renderPaperRuntimeCards(cards) {
  const root = el('paperRuntimeCards');
  if (!root) return;
  root.innerHTML = (cards || []).map((card) => `
    <article class="runtime-card">
      <h3>Paper ${esc(card.timeframe)}</h3>
      <div>${esc(card.round_slug || '--')}</div>
      <div>${esc(card.status || '--')}</div>
    </article>
  `).join('');
}
```

```html
<section class="config-panel">
  <div id="paperProfilesRoot"></div>
</section>

<section class="runtime-panel">
  <div id="paperRuntimeCards" class="runtime-card-grid"></div>
</section>
```

- [ ] **Step 4: Run the frontend asset tests again to verify they pass**

Run: `pytest tests/test_dashboard.py -k "paper_profile_editor_hooks or multi_timeframe_paper_runtime_cards" -q`

Expected: PASS with the new editor/card hooks present in the generated dashboard assets.

- [ ] **Step 5: Commit the dashboard frontend slice**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat: render paper profiles and runtime cards by timeframe"
```

### Task 5: Add Regression Coverage For Timeframe Isolation And Update Operator Docs

**Files:**
- Modify: `D:\python\BTC_5MIN\tests\test_trader_runtime_and_live.py`
- Modify: `D:\python\BTC_5MIN\README.md`

- [ ] **Step 1: Write the failing regression tests for isolated paper state/log behavior**

```python
def test_run_paper_trading_uses_isolated_paths_for_each_timeframe(tmp_path):
    logs_root = tmp_path / "logs" / "paper"
    cfg_5m = build_config_from_env_values(
        {
            "TRADE_MODE": "paper",
            "PAPER_TIMEFRAMES": "5m,15m",
            "PAPER_5M_STRATEGY_ID": "5",
            "PAPER_5M_STRATEGY_IDS": "5",
        }
    )
    cfg_15m = build_config_from_env_values(
        {
            "TRADE_MODE": "paper",
            "PAPER_TIMEFRAMES": "5m,15m",
            "PAPER_15M_STRATEGY_ID": "2",
            "PAPER_15M_STRATEGY_IDS": "2",
        }
    )

    state_5m = logs_root / "5m" / "session_state.json"
    log_5m = logs_root / "5m" / "paper_trades.csv"
    state_15m = logs_root / "15m" / "session_state.json"
    log_15m = logs_root / "15m" / "paper_trades.csv"

    assert str(state_5m) != str(state_15m)
    assert str(log_5m) != str(log_15m)
```

```python
def test_readme_mentions_paper_timeframe_profiles():
    text = Path("README.md").read_text(encoding="utf-8")
    assert "PAPER_TIMEFRAMES" in text
    assert "PAPER_5M_STRATEGY_IDS" in text
    assert "PAPER_15M_STRATEGY_IDS" in text
    assert "live mode still uses MARKET_TIMEFRAME" in text
```

- [ ] **Step 2: Run the regression/doc tests to verify they fail first**

Run: `pytest tests/test_trader_runtime_and_live.py -k "isolated_paths_for_each_timeframe" -q`

Run: `pytest tests/test_trader_runtime_and_live.py -k "readme_mentions_paper_timeframe_profiles" -q`

Expected: FAIL because docs and regression assertions for the new paper model are not in place yet.

- [ ] **Step 3: Implement final regression fixes and update `README.md`**

```markdown
## Paper multi-timeframe profiles

Paper mode can now enable more than one timeframe at once with:

- `PAPER_TIMEFRAMES=5m,15m`

Each timeframe can keep its own paper strategy list and thresholds, for example:

- `PAPER_5M_STRATEGY_IDS=5,6`
- `PAPER_15M_STRATEGY_IDS=1,2,7`

Live mode is still single-timeframe and continues to use `MARKET_TIMEFRAME`.
```

```python
def _paper_runtime_paths(cfg: AppConfig, timeframe: str) -> tuple[Path, Path]:
    base = cfg.logs_dir / "paper" / timeframe
    base.mkdir(parents=True, exist_ok=True)
    return base / "session_state.json", base / "paper_trades.csv"
```

- [ ] **Step 4: Run the focused regression suite, then the broader end-to-end checks**

Run: `pytest tests/test_config_encoding.py -k "paper_timeframes or legacy_single_timeframe_paper_fields" -q`

Run: `pytest tests/test_runtime_launcher.py -k "one_paper_worker_per_enabled_timeframe or keeps_live_single_worker_when_paper_profiles_exist" -q`

Run: `pytest tests/test_dashboard.py -k "paper_timeframes_and_profiles or timeframe_specific_paths or paper_profile_editor_hooks or multi_timeframe_paper_runtime_cards" -q`

Run: `pytest tests/test_trader_runtime_and_live.py -k "isolated_paths_for_each_timeframe" -q`

Expected: PASS for all focused checks.

- [ ] **Step 5: Commit the regression/doc slice**

```bash
git add tests/test_trader_runtime_and_live.py README.md
git commit -m "docs: describe paper multi-timeframe runtime"
```
