# BTC 5m/15m Timeframe Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dashboard-selectable BTC market timeframe setting that supports Polymarket's 5-minute and 15-minute series while keeping the existing paper/live runtime flow intact.

**Architecture:** Keep `MARKET_TIMEFRAME` as the single operator-facing setting, derive the Polymarket series metadata from that setting in `config.py`, and make `polymarket_api.py` filter events against the derived series definition. Reuse the current runtime restart/safe-stop flow by adding a config-reload signal so timeframe changes do not require a brand-new runtime architecture.

**Tech Stack:** Python 3, dataclasses, pytest, the existing single-file dashboard HTML/CSS/JS renderer, Polymarket Gamma/CLOB API clients.

---

## File Structure

- `config.py`
  - Add the new `MARKET_TIMEFRAME` env key.
  - Centralize 5m/15m Polymarket series definitions.
  - Derive `series_id`, `series_slug`, and compatible slug prefixes from the selected timeframe.
- `polymarket_api.py`
  - Replace hard-coded 5m event filtering with timeframe-aware matching.
- `dashboard.py`
  - Surface the new selector in the config payload and form.
  - Trigger runtime reload when the saved timeframe changes.
  - Update page copy so it reflects the active timeframe.
- `main.py`
  - Add a runtime-reload request path separate from mode changes.
  - Stop and restart workers at the existing safe-stop boundary when a timeframe change is pending.
- `runtime_control.py`
  - Add a generic pending-state helper so mode switches and reload requests can share the same UI state vocabulary.
- `tests/test_strategy.py`
  - Assert the default 5m mapping still works.
- `tests/test_config_encoding.py`
  - Assert `MARKET_TIMEFRAME=15m` maps to the 15m series.
- `tests/test_polymarket_api.py`
  - Assert 15m events match by `seriesSlug` and historical fallback slug prefixes.
- `tests/test_dashboard.py`
  - Assert the dashboard payload, validation, reload callback, and dynamic copy all support the timeframe selector.
- `tests/test_runtime_manager.py`
  - Assert config reloads wait for the safe boundary and enter the switching state.

### Task 1: Add Timeframe Config Mapping

**Files:**
- Modify: `config.py`
- Modify: `tests/test_strategy.py`
- Modify: `tests/test_config_encoding.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_strategy.py
def test_default_config_targets_btc_5m_series():
    cfg = AppConfig()
    assert cfg.market_timeframe == "5m"
    assert cfg.series_id == 10684
    assert cfg.series_slug == "btc-up-or-down-5m"
    assert cfg.trade_mode == "paper"


# tests/test_config_encoding.py
def test_build_config_supports_btc_15m_market_timeframe():
    cfg = build_config_from_env_values({"MARKET_TIMEFRAME": "15m"})

    assert cfg.market_timeframe == "15m"
    assert cfg.series_id == 10192
    assert cfg.series_slug == "btc-up-or-down-15m"


def test_build_config_defaults_invalid_market_timeframe_to_btc_5m():
    cfg = build_config_from_env_values({"MARKET_TIMEFRAME": "7m"})

    assert cfg.market_timeframe == "5m"
    assert cfg.series_id == 10684
    assert cfg.series_slug == "btc-up-or-down-5m"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_strategy.py tests/test_config_encoding.py -v
```

Expected:

- `AppConfig` has no `market_timeframe` attribute yet, or
- the 15m assertions fail because the config is still hard-coded to the 5m series.

- [ ] **Step 3: Write the minimal implementation**

```python
# config.py
MARKET_TIMEFRAME = "MARKET_TIMEFRAME"


@dataclass(frozen=True, slots=True)
class MarketTimeframeDefinition:
    timeframe: str
    series_id: int
    series_slug: str
    slug_prefixes: tuple[str, ...]


MARKET_TIMEFRAME_DEFINITIONS: dict[str, MarketTimeframeDefinition] = {
    "5m": MarketTimeframeDefinition(
        timeframe="5m",
        series_id=10684,
        series_slug="btc-up-or-down-5m",
        slug_prefixes=("btc-updown-5m-",),
    ),
    "15m": MarketTimeframeDefinition(
        timeframe="15m",
        series_id=10192,
        series_slug="btc-up-or-down-15m",
        slug_prefixes=("btc-up-or-down-15m-", "btc-updown-15m-"),
    ),
}


def _env_market_timeframe(default: str = "5m") -> str:
    raw = (os.getenv(MARKET_TIMEFRAME) or default).strip().lower()
    return raw if raw in MARKET_TIMEFRAME_DEFINITIONS else default


@dataclass(slots=True)
class AppConfig:
    market_timeframe: str = field(default_factory=lambda: _env_market_timeframe("5m"))

    @property
    def market_definition(self) -> MarketTimeframeDefinition:
        return MARKET_TIMEFRAME_DEFINITIONS[self.market_timeframe]

    @property
    def series_id(self) -> int:
        return self.market_definition.series_id

    @property
    def series_slug(self) -> str:
        return self.market_definition.series_slug

    @property
    def series_slug_prefixes(self) -> tuple[str, ...]:
        return self.market_definition.slug_prefixes
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_strategy.py tests/test_config_encoding.py -v
```

Expected:

- all selected tests pass
- the old default-series assertions still pass for 5m
- the new 15m mapping test passes with `series_id == 10192`

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_strategy.py tests/test_config_encoding.py
git commit -m "feat: add btc market timeframe config mapping"
```

### Task 2: Make Polymarket Event Filtering Timeframe-Aware

**Files:**
- Modify: `polymarket_api.py`
- Modify: `tests/test_polymarket_api.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_list_series_events_matches_btc_15m_by_series_slug(monkeypatch):
    client = PolymarketClient(AppConfig(market_timeframe="15m"))
    monkeypatch.setattr(
        client,
        "_get_json",
        lambda *args, **kwargs: [
            {
                "slug": "btc-up-or-down-15m-1757724300",
                "seriesSlug": "btc-up-or-down-15m",
                "markets": [{}],
            },
            {
                "slug": "btc-updown-5m-1766038500",
                "seriesSlug": "btc-up-or-down-5m",
                "markets": [{}],
            },
        ],
    )

    rows = client.list_series_events(limit=10)

    assert [row["slug"] for row in rows] == ["btc-up-or-down-15m-1757724300"]


def test_list_series_events_accepts_historical_btc_15m_fallback_slug(monkeypatch):
    client = PolymarketClient(AppConfig(market_timeframe="15m"))
    monkeypatch.setattr(
        client,
        "_get_json",
        lambda *args, **kwargs: [
            {
                "slug": "btc-updown-15m-1773368100",
                "seriesSlug": "",
                "markets": [{}],
            }
        ],
    )

    rows = client.list_series_events(limit=10)

    assert [row["slug"] for row in rows] == ["btc-updown-15m-1773368100"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_polymarket_api.py -v
```

Expected:

- the first test fails because `list_series_events()` still filters against the hard-coded 5m series
- the fallback-slug test fails because 15m compatibility prefixes are not used yet

- [ ] **Step 3: Write the minimal implementation**

```python
# polymarket_api.py
class PolymarketClient:
    def _matches_configured_series(self, event: dict[str, Any]) -> bool:
        slug = str(event.get("slug") or "")
        series_slug = str(event.get("seriesSlug") or "")

        if series_slug == self.config.series_slug:
            return True

        return any(slug.startswith(prefix) for prefix in self.config.series_slug_prefixes)

    def list_series_events(
        self,
        *,
        limit: int = 200,
        offset: int | None = None,
        active: bool | None = None,
        closed: bool | None = None,
        archived: bool | None = False,
        start_time_min: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"series_id": self.config.series_id, "limit": limit}
        if offset is not None:
            params["offset"] = offset
        if active is not None:
            params["active"] = str(active).lower()
        if closed is not None:
            params["closed"] = str(closed).lower()
        if archived is not None:
            params["archived"] = str(archived).lower()
        if start_time_min is not None:
            if isinstance(start_time_min, datetime):
                if start_time_min.tzinfo is None:
                    start_time_min = start_time_min.replace(tzinfo=timezone.utc)
                params["start_time_min"] = start_time_min.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                params["start_time_min"] = start_time_min

        payload = self._get_json("/events", base_url=self.config.gamma_api_base, params=params)
        events = payload.get("value", payload) if isinstance(payload, dict) else payload
        return [event for event in (events or []) if self._matches_configured_series(event)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_polymarket_api.py -v
```

Expected:

- both new 15m-filtering tests pass
- the existing websocket quote test still passes

- [ ] **Step 5: Commit**

```bash
git add polymarket_api.py tests/test_polymarket_api.py
git commit -m "feat: add timeframe-aware polymarket event filtering"
```

### Task 3: Add Safe Runtime Reload Support For Timeframe Changes

**Files:**
- Modify: `runtime_control.py`
- Modify: `main.py`
- Modify: `dashboard.py`
- Modify: `tests/test_runtime_manager.py`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_runtime_manager.py
def test_runtime_manager_waits_for_safe_boundary_before_reloading_config(tmp_path):
    cfg = AppConfig(trade_mode="paper")
    manager = main.RuntimeManager(
        env_file=tmp_path / ".env.dashboard",
        host="127.0.0.1",
        port=8787,
        startup_cfg=cfg,
        dashboard_runtime_factory=lambda **kwargs: None,
        validate_live_config=lambda cfg: None,
    )
    manager.request_runtime_reload("market_timeframe")
    manager.runtime_control.update_worker_state(round_in_progress=True, safe_to_switch=False)

    manager.poll_once()

    snapshot = manager.snapshot()
    assert snapshot.active_mode == "paper"
    assert snapshot.switch_state == "pending"


# tests/test_dashboard.py
def test_dashboard_update_config_notifies_runtime_reload_for_market_timeframe(tmp_path: Path):
    calls: list[str] = []
    state = DashboardState(
        env_file=tmp_path / ".env.dashboard",
        notify_runtime_reload=lambda reason: calls.append(reason),
    )
    try:
        state.update_config({"MARKET_TIMEFRAME": "15m"})
        assert calls == ["market_timeframe"]
    finally:
        state.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_runtime_manager.py tests/test_dashboard.py -v
```

Expected:

- `RuntimeManager` has no `request_runtime_reload()` path yet
- `DashboardState` has no `notify_runtime_reload` callback yet

- [ ] **Step 3: Write the minimal implementation**

```python
# runtime_control.py
class RuntimeControl:
    def mark_pending(self, reason: str | None = None) -> RuntimeSnapshot:
        with self._lock:
            snapshot = self._snapshot
            updates: dict[str, object | None] = {}
            if snapshot.switch_state != "pending":
                updates["switch_state"] = "pending"
            if snapshot.switch_reason != reason:
                updates["switch_reason"] = reason
            if updates:
                return self._apply_updates(snapshot, updates)
            return replace(snapshot)


# main.py
class RuntimeManager:
    def __init__(self, *, env_file: Path, host: str, port: int, startup_cfg: AppConfig | None = None, dashboard_runtime_factory=create_dashboard_runtime, validate_live_config=validate_live_runtime_config) -> None:
        self.env_file = Path(env_file)
        self.host = host
        self.port = port
        self.dashboard_runtime_factory = dashboard_runtime_factory
        self.validate_live_config = validate_live_config
        self.startup_cfg = startup_cfg or _load_shared_config(self.env_file)
        self.runtime_control = RuntimeControl(initial_mode=getattr(self.startup_cfg, "trade_mode", "paper"))
        self._reload_requested = False
        self._reload_reason: str | None = None

    def request_runtime_reload(self, reason: str = "config_reload") -> None:
        self._reload_requested = True
        self._reload_reason = reason
        self.runtime_control.mark_pending(reason)

    def restart_requested(self) -> bool:
        snapshot = self.runtime_control.snapshot()
        return self._reload_requested or snapshot.desired_mode != snapshot.active_mode

    def complete_runtime_reload(self) -> None:
        self._reload_requested = False
        self._reload_reason = None

    def poll_once(self) -> None:
        snapshot = self.runtime_control.snapshot()
        if snapshot.active_mode == snapshot.desired_mode and not self._reload_requested:
            if snapshot.switch_state != "idle" or snapshot.switch_reason is not None:
                self.runtime_control.mark_active_mode(snapshot.active_mode)
            return
        if snapshot.round_in_progress or not snapshot.safe_to_switch or snapshot.pending_live_order:
            return
        if snapshot.desired_mode == "live":
            cfg = _load_shared_config(self.env_file)
            self.validate_live_config(cfg)
        self.runtime_control.mark_switching(self._reload_reason if self._reload_requested else None)


def run_single_command_runtime(*, env_file: Path = Path(".env.dashboard"), host: str = "127.0.0.1", port: int = 8787) -> int:
    dashboard_runtime = create_dashboard_runtime(
        host=host,
        port=port,
        env_file=env_path,
        running_trade_mode=initial_mode,
        runtime_control=manager.runtime_control,
        notify_mode_change=manager.request_mode_change,
        notify_runtime_reload=manager.request_runtime_reload,
    )
    worker_kwargs = _build_worker_call_kwargs(
        worker,
        stop_event=stop_event,
        config_provider=_config_provider,
        runtime_control=manager.runtime_control,
        stop_when_safe=manager.restart_requested,
    )
    manager.request_mode_change(_config_provider().trade_mode)
    manager.poll_once()
    snapshot = manager.snapshot()
    if snapshot.switch_state == "switching":
        manager.complete_runtime_reload()
        manager.runtime_control.mark_active_mode(snapshot.desired_mode)
        continue


# dashboard.py
class DashboardState:
    def __init__(self, *, env_file: Path, running_trade_mode: str = "paper", runtime_control: RuntimeControl | None = None, notify_mode_change: Any | None = None, notify_runtime_reload: Any | None = None) -> None:
        self.env_file = Path(env_file)
        self.running_trade_mode = str(running_trade_mode or "paper").strip().lower() or "paper"
        self.runtime_control = runtime_control
        self.notify_mode_change = notify_mode_change
        self.notify_runtime_reload = notify_runtime_reload

    def update_config(self, values: dict[str, str]) -> dict[str, Any]:
        previous_mode = str(self._cfg.trade_mode or "paper").strip().lower() or "paper"
        previous_timeframe = getattr(self._cfg, "market_timeframe", "5m")
        self._refresh_runtime()
        next_mode = str(self._env_values.get("TRADE_MODE") or self._cfg.trade_mode or "paper").strip().lower() or "paper"
        next_timeframe = getattr(self._cfg, "market_timeframe", "5m")
        if self.notify_runtime_reload is not None and previous_timeframe != next_timeframe:
            self.notify_runtime_reload("market_timeframe")
        if self.notify_mode_change is not None and previous_mode != next_mode:
            self.notify_mode_change(next_mode)
        return self.get_config_payload()


def create_dashboard_runtime(*, host: str = "127.0.0.1", port: int = 8787, env_file: Path = Path(".env.dashboard"), running_trade_mode: str = "paper", runtime_control: RuntimeControl | None = None, notify_mode_change: Any | None = None, notify_runtime_reload: Any | None = None) -> DashboardRuntime:
    state = DashboardState(
        env_file=env_file,
        running_trade_mode=running_trade_mode,
        runtime_control=runtime_control,
        notify_mode_change=notify_mode_change,
        notify_runtime_reload=notify_runtime_reload,
    )
    class Handler(_DashboardRequestHandler):
        dashboard_state = state
    server = ThreadingHTTPServer((host, port), Handler)
    return DashboardRuntime(server=server, state=state)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_runtime_manager.py tests/test_dashboard.py -v
```

Expected:

- config reload requests now show up as a pending runtime transition
- the dashboard emits exactly one runtime-reload callback when `MARKET_TIMEFRAME` changes

- [ ] **Step 5: Commit**

```bash
git add runtime_control.py main.py dashboard.py tests/test_runtime_manager.py tests/test_dashboard.py
git commit -m "feat: add safe runtime reload for timeframe changes"
```

### Task 4: Add Dashboard Timeframe Selector And Dynamic Copy

**Files:**
- Modify: `dashboard.py`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_dashboard_config_payload_includes_market_timeframe_selector(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    try:
        payload = state.get_config_payload()
        assert "MARKET_TIMEFRAME" in payload["editable_keys"]
        assert payload["select_options"]["MARKET_TIMEFRAME"] == ["5m", "15m"]
        assert payload["labels"]["MARKET_TIMEFRAME"] == "市场频次"
    finally:
        state.close()


def test_dashboard_rejects_invalid_market_timeframe(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    try:
        with pytest.raises(ConfigValidationError) as excinfo:
            state.update_config({"MARKET_TIMEFRAME": "10m"})
        assert "MARKET_TIMEFRAME" in excinfo.value.field_errors
    finally:
        state.close()


def test_dashboard_assets_include_timeframe_copy_hooks():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="brandTitle"' in html
    assert 'id="marketPanelDesc"' in html
    assert "const TIMEFRAME_META = {" in js
    assert "MARKET_TIMEFRAME" in js
    assert "function applyTimeframeCopy(payload)" in js
    assert '"15m": {' in js
    assert '"10m"' not in js
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_dashboard.py -v
```

Expected:

- `MARKET_TIMEFRAME` is missing from the config payload
- invalid timeframe values are not validated yet
- the dashboard assets do not contain timeframe-specific DOM hooks or JavaScript copy helpers

- [ ] **Step 3: Write the minimal implementation**

```python
# dashboard.py
DashboardState.EDITABLE_CONFIG_KEYS = (
    "TRADE_MODE",
    "MARKET_TIMEFRAME",
    *tuple(key for key in DashboardState.EDITABLE_CONFIG_KEYS if key != "TRADE_MODE"),
)
DashboardState.CONFIG_LABELS["MARKET_TIMEFRAME"] = "市场频次"
DashboardState.SELECT_OPTIONS["MARKET_TIMEFRAME"] = ["5m", "15m"]
DashboardState.CONFIG_ATTR_MAP["MARKET_TIMEFRAME"] = "market_timeframe"
DashboardState.FIELD_HELP["MARKET_TIMEFRAME"] = "选择当前要玩的 Polymarket BTC 预测频次，仅支持 5 分钟和 15 分钟。"
DashboardState.FIELD_GROUPS[0]["keys"] = [
    "TRADE_MODE",
    "MARKET_TIMEFRAME",
    *[key for key in DashboardState.FIELD_GROUPS[0]["keys"] if key != "TRADE_MODE"],
]


def _dashboard_html() -> str:
    return """<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>BTC 预测控制台</title>
  <link rel=\"stylesheet\" href=\"/dashboard.css\">
</head>
<body>
  <header class=\"topbar\">
    <div class=\"brand-wrap\">
      <div id=\"brandTitle\" class=\"brand\">QUANT_CMD · BTC</div>
      <div class=\"subtitle\">策略参数、实时盘口、信号决策、纸面收益一屏联动</div>
    </div>
  </header>
  <main class=\"layout\">
    <section class=\"panel center-stack\">
      <div class=\"panel-head\">
        <div>
          <div class=\"head-title\">行情与信号</div>
          <div id=\"marketPanelDesc\" class=\"head-desc\">轮次行情 / 方向信号 / 下注计划</div>
        </div>
      </div>
    </section>
  </main>
"""


def _dashboard_js() -> str:
    return """
const TIMEFRAME_META = {
  "5m": { label: "5分钟", brand: "QUANT_CMD · BTC_5M", marketDesc: "5分钟轮次行情 / 方向信号 / 下注计划" },
  "15m": { label: "15分钟", brand: "QUANT_CMD · BTC_15M", marketDesc: "15分钟轮次行情 / 方向信号 / 下注计划" },
};

function timeframeMeta(payload) {
  const raw = String((((payload || {}).env_values || {}).MARKET_TIMEFRAME || "5m")).toLowerCase();
  return TIMEFRAME_META[raw] || TIMEFRAME_META["5m"];
}

function applyTimeframeCopy(payload) {
  const meta = timeframeMeta(payload);
  document.title = "BTC " + meta.label + "预测控制台";
  const brand = el("brandTitle");
  if (brand) brand.textContent = meta.brand;
  const panel = el("marketPanelDesc");
  if (panel) panel.textContent = meta.marketDesc;
}

applyTimeframeCopy(payload);
"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_dashboard.py -v
```

Expected:

- the payload exposes the new selector with exactly `["5m", "15m"]`
- invalid `MARKET_TIMEFRAME` values raise `ConfigValidationError`
- the dashboard assets expose dynamic copy hooks and do not mention `10m`

- [ ] **Step 5: Commit**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat: add dashboard market timeframe selector"
```

## Verification

Run the focused regression suite after the four tasks are complete:

```powershell
pytest tests/test_strategy.py tests/test_config_encoding.py tests/test_polymarket_api.py tests/test_dashboard.py tests/test_runtime_manager.py -v
```

Then run the broader runtime coverage to catch regressions in paper/live flows:

```powershell
pytest tests/test_trader_runtime_and_live.py tests/test_main.py tests/test_runtime_manager.py -v
```

If both commands are green, inspect the dashboard manually by launching the supported entrypoint:

```powershell
py -m pytest tests/test_dashboard.py -q
py main.py
```

Manual check:

- default load shows 5-minute market copy
- saving `MARKET_TIMEFRAME=15m` updates the config form payload and display copy
- the runtime transitions through the pending/switching state instead of ignoring the timeframe change
- no 10-minute option appears anywhere in the form or UI copy
