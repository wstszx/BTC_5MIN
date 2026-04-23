# Live Multi-Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add live multi-strategy trading with `LIVE_STRATEGY_IDS`, per-strategy live parameter profiles, isolated per-strategy live state, shared-wallet balance gating, and dashboard visibility without changing live mode into a multi-timeframe runtime.

**Architecture:** Keep one `live-trading-worker`, but turn it into a coordinator over strategy-specific live profiles and `LiveStrategyState` entries. Reuse the existing paper multi-strategy design pattern: shared round/quote discovery, isolated per-strategy ledgers, shared CSV logs, and aggregated runtime-control flags. Add an explicit wallet-budget helper so same-round live submissions can happen safely in sequence.

**Tech Stack:** Python 3 dataclasses, pytest, existing dashboard HTML/JS generation in `dashboard.py`, existing runtime coordination in `main.py` and `runtime_control.py`.

---

### Task 1: Add Live Multi-Strategy Config Parsing

**Files:**
- Modify: `config.py`
- Modify: `tests/test_config_encoding.py`

- [ ] **Step 1: Write the failing config parsing tests**

```python
def test_build_config_uses_live_strategy_ids_when_present():
    cfg = build_config_from_env_values(
        {
            "TRADE_MODE": "live",
            "STRATEGY_ID": "2",
            "LIVE_STRATEGY_IDS": "7,5,7,6",
        }
    )

    assert cfg.live_strategy_ids == [7, 5, 6]


def test_build_config_falls_back_to_strategy_id_for_live_when_list_missing():
    cfg = build_config_from_env_values(
        {
            "TRADE_MODE": "live",
            "STRATEGY_ID": "6",
        }
    )

    assert cfg.live_strategy_ids == [6]


def test_build_config_applies_strategy_specific_live_profile_overrides():
    cfg = build_config_from_env_values(
        {
            "TRADE_MODE": "live",
            "STRATEGY_ID": "2",
            "LIVE_STRATEGY_IDS": "5,7",
            "TARGET_PROFIT": "1.0",
            "BASE_ORDER_COST": "3.0",
            "LIVE_STRATEGY_5_TARGET_PROFIT": "0.8",
            "LIVE_STRATEGY_5_BASE_ORDER_COST": "1.5",
            "LIVE_STRATEGY_7_TARGET_PROFIT": "1.2",
            "LIVE_STRATEGY_7_STRATEGY7_MAX_ENTRY_PRICE": "0.53",
        }
    )

    assert cfg.live_profiles[5].target_profit == 0.8
    assert cfg.live_profiles[5].base_order_cost == 1.5
    assert cfg.live_profiles[7].target_profit == 1.2
    assert cfg.live_profiles[7].strategy7_max_entry_price == 0.53
    assert cfg.live_profiles[7].base_order_cost == 3.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_config_encoding.py -k live_strategy -v`

Expected: FAIL with errors such as `AttributeError: 'AppConfig' object has no attribute 'live_strategy_ids'`.

- [ ] **Step 3: Add `LiveStrategyProfile` and live config parsing**

```python
LIVE_STRATEGY_IDS = "LIVE_STRATEGY_IDS"


def _live_profile_prefix(strategy_id: int) -> str:
    return f"LIVE_STRATEGY_{int(strategy_id)}"


@dataclass(slots=True)
class LiveStrategyProfile:
    strategy_id: int
    target_profit: float
    bet_sizing_mode: str
    base_order_cost: float
    max_consecutive_losses: int
    max_stake: float | None
    open_delay_seconds: int
    signal_momentum_threshold: float
    signal_fallback_strategy_id: int
    signal_weak_signal_mode: str
    signal_history_fidelity_seconds: int
    signal_anchor_max_offset_seconds: int
    signal_dynamic_threshold_k: float
    signal_dynamic_threshold_min_points: int
    signal_lock_before_entry_seconds: int
    max_stake_skip_alert_threshold: int
    ofi_threshold: float
    max_entry_price: float
    binance_signal_stale_seconds: float
    strategy7_ofi_threshold: float
    strategy7_momentum_threshold: float
    strategy7_max_entry_price: float
    strategy7_min_signal_gap: float
    strategy7_confirm_before_entry_seconds: int
    strategy7_late_confirm_strong_signal_gap: float
    strategy7_late_confirm_relax_seconds: float


def _live_profile_for_strategy(cfg: "AppConfig", strategy_id: int) -> LiveStrategyProfile:
    prefix = _live_profile_prefix(strategy_id)
    return LiveStrategyProfile(
        strategy_id=int(strategy_id),
        target_profit=_env_float(f"{prefix}_TARGET_PROFIT", cfg.target_profit),
        bet_sizing_mode=(os.getenv(f"{prefix}_BET_SIZING_MODE") or cfg.bet_sizing_mode).upper(),
        base_order_cost=_env_float(f"{prefix}_BASE_ORDER_COST", cfg.base_order_cost),
        max_consecutive_losses=_env_int(f"{prefix}_MAX_CONSECUTIVE_LOSSES", cfg.max_consecutive_losses),
        max_stake=(
            _env_optional_float(f"{prefix}_MAX_STAKE")
            if os.getenv(f"{prefix}_MAX_STAKE") is not None
            else cfg.max_stake
        ),
        open_delay_seconds=_env_int(f"{prefix}_OPEN_DELAY_SECONDS", cfg.open_delay_seconds),
        signal_momentum_threshold=_env_float(
            f"{prefix}_SIGNAL_MOMENTUM_THRESHOLD",
            cfg.signal_momentum_threshold,
        ),
        signal_fallback_strategy_id=_env_int(
            f"{prefix}_SIGNAL_FALLBACK_STRATEGY_ID",
            cfg.signal_fallback_strategy_id,
        ),
        signal_weak_signal_mode=(os.getenv(f"{prefix}_SIGNAL_WEAK_SIGNAL_MODE") or cfg.signal_weak_signal_mode).upper(),
        signal_history_fidelity_seconds=_env_int(
            f"{prefix}_SIGNAL_HISTORY_FIDELITY_SECONDS",
            cfg.signal_history_fidelity_seconds,
        ),
        signal_anchor_max_offset_seconds=_env_int(
            f"{prefix}_SIGNAL_ANCHOR_MAX_OFFSET_SECONDS",
            cfg.signal_anchor_max_offset_seconds,
        ),
        signal_dynamic_threshold_k=_env_float(
            f"{prefix}_SIGNAL_DYNAMIC_THRESHOLD_K",
            cfg.signal_dynamic_threshold_k,
        ),
        signal_dynamic_threshold_min_points=_env_int(
            f"{prefix}_SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS",
            cfg.signal_dynamic_threshold_min_points,
        ),
        signal_lock_before_entry_seconds=_env_int(
            f"{prefix}_SIGNAL_LOCK_BEFORE_ENTRY_SECONDS",
            cfg.signal_lock_before_entry_seconds,
        ),
        max_stake_skip_alert_threshold=_env_int(
            f"{prefix}_MAX_STAKE_SKIP_ALERT_THRESHOLD",
            cfg.max_stake_skip_alert_threshold,
        ),
        ofi_threshold=_env_float(f"{prefix}_OFI_THRESHOLD", cfg.ofi_threshold),
        max_entry_price=_env_float(f"{prefix}_MAX_ENTRY_PRICE", cfg.max_entry_price),
        binance_signal_stale_seconds=_env_float(
            f"{prefix}_BINANCE_SIGNAL_STALE_SECONDS",
            cfg.binance_signal_stale_seconds,
        ),
        strategy7_ofi_threshold=_env_float(
            f"{prefix}_STRATEGY7_OFI_THRESHOLD",
            cfg.strategy7_ofi_threshold,
        ),
        strategy7_momentum_threshold=_env_float(
            f"{prefix}_STRATEGY7_MOMENTUM_THRESHOLD",
            cfg.strategy7_momentum_threshold,
        ),
        strategy7_max_entry_price=_env_float(
            f"{prefix}_STRATEGY7_MAX_ENTRY_PRICE",
            cfg.strategy7_max_entry_price,
        ),
        strategy7_min_signal_gap=_env_float(
            f"{prefix}_STRATEGY7_MIN_SIGNAL_GAP",
            cfg.strategy7_min_signal_gap,
        ),
        strategy7_confirm_before_entry_seconds=_env_int(
            f"{prefix}_STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS",
            cfg.strategy7_confirm_before_entry_seconds,
        ),
        strategy7_late_confirm_strong_signal_gap=_env_float(
            f"{prefix}_STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP",
            cfg.strategy7_late_confirm_strong_signal_gap,
        ),
        strategy7_late_confirm_relax_seconds=_env_float(
            f"{prefix}_STRATEGY7_LATE_CONFIRM_RELAX_SECONDS",
            cfg.strategy7_late_confirm_relax_seconds,
        ),
    )
```

- [ ] **Step 4: Wire the new fields into `AppConfig`**

```python
live_strategy_ids: list[int] = field(
    default_factory=lambda: _parse_strategy_id_list(
        os.getenv(LIVE_STRATEGY_IDS),
        fallback=_env_int(STRATEGY_ID, 2),
    )
)
live_profiles: dict[int, LiveStrategyProfile] = field(init=False)


def __post_init__(self) -> None:
    if not self.paper_timeframes:
        self.paper_timeframes = _env_paper_timeframes(self.market_timeframe)

    self.live_profiles = {}
    for strategy_id in self.live_strategy_ids:
        self.live_profiles[int(strategy_id)] = _live_profile_for_strategy(self, int(strategy_id))
```

- [ ] **Step 5: Run the config tests to verify they pass**

Run: `pytest tests/test_config_encoding.py -k live_strategy -v`

Expected: PASS for the new `live_strategy` tests.

- [ ] **Step 6: Commit**

```bash
git add config.py tests/test_config_encoding.py
git commit -m "feat: add live multi-strategy config profiles"
```

### Task 2: Migrate Session State to Per-Strategy Live State

**Files:**
- Modify: `models.py`
- Modify: `trader.py`
- Modify: `tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write failing live-state migration tests**

```python
def test_load_session_state_wraps_legacy_live_fields_into_effective_strategy(tmp_path: Path):
    state_path = tmp_path / "live_session_state.json"
    state_path.write_text(
        json.dumps(
            {
                "round_index": 4,
                "cash_pnl": 1.25,
                "pending_live_slug": "btc-5m-2026-04-23-1200",
                "pending_live_side": "UP",
                "pending_live_price": 0.51,
                "pending_live_order_size": 9.0,
                "pending_live_order_cost": 4.59,
                "pending_live_expected_profit": 4.41,
                "pending_live_order_id": "order-1",
                "pending_live_end_time": "2026-04-23T12:05:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    state = load_session_state(state_path, effective_live_strategy_ids=[7])

    assert 7 in state.live_strategies
    assert state.live_strategies[7].pending_live_slug == "btc-5m-2026-04-23-1200"
    assert state.live_strategies[7].round_index == 4


def test_load_session_state_preserves_multiple_live_strategy_entries(tmp_path: Path):
    state_path = tmp_path / "live_session_state.json"
    state_path.write_text(
        json.dumps(
            {
                "live_strategies": {
                    "5": {"round_index": 3, "cash_pnl": 0.8},
                    "7": {"round_index": 2, "cash_pnl": -0.2, "pending_live_slug": "slug-7"},
                }
            }
        ),
        encoding="utf-8",
    )

    state = load_session_state(state_path, effective_live_strategy_ids=[5, 7])

    assert state.live_strategies[5].round_index == 3
    assert state.live_strategies[7].pending_live_slug == "slug-7"
```

- [ ] **Step 2: Run the migration tests to verify they fail**

Run: `pytest tests/test_trader_runtime_and_live.py -k live_strategies -v`

Expected: FAIL with errors such as unexpected keyword arguments or missing `live_strategies`.

- [ ] **Step 3: Add `LiveStrategyState` to `models.py`**

```python
@dataclass(slots=True)
class LiveStrategyState:
    round_index: int = 0
    cash_pnl: float = 0.0
    recovery_loss: float = 0.0
    consecutive_losses: int = 0
    consecutive_max_stake_skips: int = 0
    signal_round_slug: str | None = None
    signal_round_open_up_price: float | None = None
    signal_round_locked_side: str | None = None
    strategy6_last_ofi_score: float | None = None
    stop_loss_count: int = 0
    daily_realized_pnl: float = 0.0
    current_day: str | None = None
    pending_live_slug: str | None = None
    pending_live_side: str | None = None
    pending_live_price: float | None = None
    pending_live_order_size: float | None = None
    pending_live_order_cost: float | None = None
    pending_live_expected_profit: float | None = None
    pending_live_order_id: str | None = None
    pending_live_end_time: str | None = None
```

- [ ] **Step 4: Add `live_strategies` to `SessionState` and teach `load_session_state()` to hydrate it**

```python
@dataclass(slots=True)
class SessionState:
    live_strategies: dict[int, LiveStrategyState] = field(default_factory=dict)


def _hydrate_live_strategy_map(payload: dict[str, Any], effective_live_strategy_ids: list[int]) -> dict[int, LiveStrategyState]:
    hydrated: dict[int, LiveStrategyState] = {}
    raw_map = payload.get("live_strategies")
    if isinstance(raw_map, dict):
        for raw_strategy_id, raw_state in raw_map.items():
            strategy_id = int(raw_strategy_id)
            if isinstance(raw_state, dict):
                hydrated[strategy_id] = LiveStrategyState(**raw_state)

    if not hydrated and effective_live_strategy_ids:
        legacy_fields = {
            "round_index": payload.get("round_index", 0),
            "cash_pnl": payload.get("cash_pnl", 0.0),
            "recovery_loss": payload.get("recovery_loss", 0.0),
            "consecutive_losses": payload.get("consecutive_losses", 0),
            "consecutive_max_stake_skips": payload.get("consecutive_max_stake_skips", 0),
            "signal_round_slug": payload.get("signal_round_slug"),
            "signal_round_open_up_price": payload.get("signal_round_open_up_price"),
            "signal_round_locked_side": payload.get("signal_round_locked_side"),
            "strategy6_last_ofi_score": payload.get("strategy6_last_ofi_score"),
            "stop_loss_count": payload.get("stop_loss_count", 0),
            "daily_realized_pnl": payload.get("daily_realized_pnl", 0.0),
            "current_day": payload.get("current_day"),
            "pending_live_slug": payload.get("pending_live_slug"),
            "pending_live_side": payload.get("pending_live_side"),
            "pending_live_price": payload.get("pending_live_price"),
            "pending_live_order_size": payload.get("pending_live_order_size"),
            "pending_live_order_cost": payload.get("pending_live_order_cost"),
            "pending_live_expected_profit": payload.get("pending_live_expected_profit"),
            "pending_live_order_id": payload.get("pending_live_order_id"),
            "pending_live_end_time": payload.get("pending_live_end_time"),
        }
        hydrated[effective_live_strategy_ids[0]] = LiveStrategyState(**legacy_fields)

    for strategy_id in effective_live_strategy_ids:
        hydrated.setdefault(int(strategy_id), LiveStrategyState())

    return hydrated
```

- [ ] **Step 5: Extend `load_session_state()` signature and persistence path**

```python
def load_session_state(
    path: Path,
    *,
    effective_paper_strategy_ids: list[int] | None = None,
    effective_live_strategy_ids: list[int] | None = None,
) -> SessionState:
    pending_paper_trades = [PendingPaperTrade(**item) for item in payload.get("pending_paper_trades", [])]
    payload["live_strategies"] = _hydrate_live_strategy_map(payload, list(effective_live_strategy_ids or []))
    return SessionState(
        pending_paper_trades=pending_paper_trades,
        **payload,
    )
```

- [ ] **Step 6: Run the migration tests to verify they pass**

Run: `pytest tests/test_trader_runtime_and_live.py -k live_strategies -v`

Expected: PASS for the new migration tests.

- [ ] **Step 7: Commit**

```bash
git add models.py trader.py tests/test_trader_runtime_and_live.py
git commit -m "feat: add per-strategy live session state"
```

### Task 3: Refactor Pending Live Settlement Helpers Around `LiveStrategyState`

**Files:**
- Modify: `trader.py`
- Modify: `tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write failing settlement tests for strategy-local pending orders**

```python
def test_settle_pending_live_trade_operates_on_single_strategy_state(monkeypatch):
    strategy_state = LiveStrategyState(
        cash_pnl=0.0,
        pending_live_slug="slug-1",
        pending_live_side="UP",
        pending_live_price=0.5,
        pending_live_order_size=10.0,
        pending_live_order_cost=5.0,
        pending_live_expected_profit=5.0,
        pending_live_order_id="order-1",
        pending_live_end_time="2026-04-23T12:05:00+00:00",
    )

    class StubMarketClient:
        def get_event_by_slug(self, slug):
            return {"eventMetadata": {"priceToBeat": 100000, "finalPrice": 100100}}

    class StubClobClient:
        def get_order(self, order_id):
            return {"status": "filled", "avg_price": 0.5, "filled_order_size": 10.0, "filled_order_cost": 5.0}

    updated_state, status, changed = _settle_pending_live_trade_if_needed(
        market_client=StubMarketClient(),
        clob_client=StubClobClient(),
        strategy_state=strategy_state,
        now=datetime.fromisoformat("2026-04-23T12:06:00+00:00"),
    )

    assert changed is True
    assert status["status"] == "settled"
    assert updated_state.pending_live_slug is None
    assert updated_state.cash_pnl > 0.0
```

- [ ] **Step 2: Run the settlement tests to verify they fail**

Run: `pytest tests/test_trader_runtime_and_live.py -k settle_pending_live_trade_operates_on_single_strategy_state -v`

Expected: FAIL because `_settle_pending_live_trade_if_needed()` still accepts `SessionState`.

- [ ] **Step 3: Refactor pending live helpers to accept `LiveStrategyState`**

```python
def _clear_pending_live_trade(state: LiveStrategyState) -> None:
    state.pending_live_slug = None
    state.pending_live_side = None
    state.pending_live_price = None
    state.pending_live_order_size = None
    state.pending_live_order_cost = None
    state.pending_live_expected_profit = None
    state.pending_live_order_id = None
    state.pending_live_end_time = None


def _build_verified_pending_live_trade_plan(
    strategy_state: LiveStrategyState,
    *,
    clob_client: Any | None,
) -> TradePlan | None:
    if strategy_state.pending_live_side not in {"UP", "DOWN"}:
        raise RuntimeError("Pending live trade is missing a valid side.")
    if not strategy_state.pending_live_order_id:
        return None
    if clob_client is None:
        return None
    get_order = getattr(clob_client, "get_order", None)
    if not callable(get_order):
        return None
    order_payload = get_order(strategy_state.pending_live_order_id)
    if not isinstance(order_payload, dict):
        return None
    status = str(order_payload.get("status") or "").strip().lower()
    has_fill_markers = any(
        order_payload.get(key) is not None
        for key in (
            "filled_order_size",
            "filledOrderSize",
            "filled_order_cost",
            "filledOrderCost",
            "avg_price",
            "avgPrice",
        )
    )
    if status not in {"filled", "matched"} and not has_fill_markers:
        return None
    order_size = _coerce_positive_float(
        order_payload.get("filled_order_size")
        or order_payload.get("filledOrderSize")
        or order_payload.get("size_matched")
        or order_payload.get("matched_size")
    )
    order_cost = _coerce_positive_float(
        order_payload.get("filled_order_cost")
        or order_payload.get("filledOrderCost")
        or order_payload.get("filled_value")
        or order_payload.get("filledValue")
        or order_payload.get("cost")
    )
    fill_price = _coerce_positive_float(
        order_payload.get("avg_price")
        or order_payload.get("avgPrice")
        or order_payload.get("price")
    )
    if order_size is None and order_cost is not None and fill_price is not None:
        order_size = order_cost / fill_price
    if order_cost is None and order_size is not None and fill_price is not None:
        order_cost = order_size * fill_price
    if fill_price is None and order_size is not None and order_cost is not None:
        fill_price = order_cost / order_size
    if order_size is None or order_cost is None or fill_price is None or not 0 < fill_price < 1:
        return None
    return TradePlan(
        True,
        side=strategy_state.pending_live_side,
        price=fill_price,
        order_size=order_size,
        order_cost=order_cost,
        expected_profit=order_size * (1 - fill_price),
    )
```

- [ ] **Step 4: Update `_settle_pending_live_trade_if_needed()` to return a strategy-local result**

```python
def _settle_pending_live_trade_if_needed(
    *,
    market_client: PolymarketClient | Any,
    clob_client: Any | None,
    strategy_state: LiveStrategyState,
    now: datetime,
) -> tuple[LiveStrategyState, dict[str, Any] | None, bool]:
    if not strategy_state.pending_live_slug:
        return strategy_state, None, False

    end_time = parse_iso_datetime(strategy_state.pending_live_end_time)
    if end_time is None:
        raise RuntimeError("Pending live trade is missing round end time.")
    if now < end_time:
        return (
            strategy_state,
            {
                "status": "pending_settlement",
                "slug": strategy_state.pending_live_slug,
                "side": strategy_state.pending_live_side,
                "skip_reason": "round_in_progress",
                "pending_end_time": strategy_state.pending_live_end_time,
            },
            False,
        )
    plan = _build_verified_pending_live_trade_plan(strategy_state, clob_client=clob_client)
    if plan is None:
        return (
            strategy_state,
            {
                "status": "pending_settlement",
                "slug": strategy_state.pending_live_slug,
                "side": strategy_state.pending_live_side,
                "skip_reason": "awaiting_fill_confirmation",
                "pending_end_time": strategy_state.pending_live_end_time,
                "order_id": strategy_state.pending_live_order_id,
            },
            False,
        )
    event = market_client.get_event_by_slug(strategy_state.pending_live_slug)
    metadata = event.get("eventMetadata") or {}
    if metadata.get("priceToBeat") is None or metadata.get("finalPrice") is None:
        return (
            strategy_state,
            {
                "status": "pending_settlement",
                "slug": strategy_state.pending_live_slug,
                "side": strategy_state.pending_live_side,
                "skip_reason": "round_unresolved",
                "pending_end_time": strategy_state.pending_live_end_time,
            },
            False,
        )
    result = "UP" if float(metadata["finalPrice"]) >= float(metadata["priceToBeat"]) else "DOWN"
    updated_state = apply_round_outcome(strategy_state, plan, won=(result == plan.side))
    _clear_pending_live_trade(updated_state)
    return updated_state, {"status": "settled", "slug": strategy_state.pending_live_slug, "side": plan.side, "result": result}, True
```

- [ ] **Step 5: Run the settlement tests to verify they pass**

Run: `pytest tests/test_trader_runtime_and_live.py -k settle_pending_live_trade_operates_on_single_strategy_state -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add trader.py tests/test_trader_runtime_and_live.py
git commit -m "refactor: make live settlement strategy-local"
```

### Task 4: Turn `run_live_trading()` Into a Multi-Strategy Coordinator With Wallet Budget Gating

**Files:**
- Modify: `trader.py`
- Modify: `config.py`
- Modify: `tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write failing runtime tests for same-round multi-strategy live trading**

```python
def test_run_live_trading_submits_multiple_strategies_when_wallet_budget_allows(monkeypatch, tmp_path: Path):
    cfg = AppConfig(
        trade_mode="live",
        strategy_id=5,
        live_strategy_ids=[5, 7],
        logs_dir=tmp_path / "logs",
    )

    monkeypatch.setitem(cfg.live_profiles, 5, replace(cfg.live_profiles[5], base_order_cost=2.0))
    monkeypatch.setitem(cfg.live_profiles, 7, replace(cfg.live_profiles[7], base_order_cost=3.0))

    submitted: list[tuple[int, float]] = []

    monkeypatch.setattr(trader, "_read_available_live_balance", lambda **_: 10.0)
    monkeypatch.setattr(trader, "_submit_live_strategy_order", lambda strategy_cfg, **_: submitted.append((strategy_cfg.strategy_id, strategy_cfg.base_order_cost)) or {"order_id": f"order-{strategy_cfg.strategy_id}"})

    result = trader.run_live_trading(cfg, stop_event=threading.Event(), dry_run_once=True)

    assert result["status"] == "submitted"
    assert submitted == [(5, 2.0), (7, 3.0)]


def test_run_live_trading_skips_later_strategy_when_budget_is_exhausted(monkeypatch, tmp_path: Path):
    cfg = AppConfig(
        trade_mode="live",
        strategy_id=5,
        live_strategy_ids=[5, 7],
        logs_dir=tmp_path / "logs",
    )

    submitted: list[int] = []

    monkeypatch.setattr(trader, "_read_available_live_balance", lambda **_: 4.0)
    monkeypatch.setattr(trader, "_submit_live_strategy_order", lambda strategy_cfg, **_: submitted.append(strategy_cfg.strategy_id) or {"order_id": f"order-{strategy_cfg.strategy_id}"})

    result = trader.run_live_trading(cfg, stop_event=threading.Event(), dry_run_once=True)

    assert submitted == [5]
    assert any(item["skip_reason"] == "insufficient_live_wallet_balance" for item in result["strategies"])
```

- [ ] **Step 2: Run the live runtime tests to verify they fail**

Run: `pytest tests/test_trader_runtime_and_live.py -k "wallet_budget or submits_multiple_strategies" -v`

Expected: FAIL because `run_live_trading()` still behaves like a single-strategy path.

- [ ] **Step 3: Add helpers for live strategy config views and wallet budget**

```python
def _live_strategy_ids_for_runtime(cfg: AppConfig) -> list[int]:
    strategy_ids = list(getattr(cfg, "live_strategy_ids", []) or [])
    if strategy_ids:
        return strategy_ids
    return [cfg.strategy_id]


def _cfg_for_live_strategy(cfg: AppConfig, strategy_id: int) -> AppConfig:
    profile = dict(getattr(cfg, "live_profiles", {})).get(int(strategy_id))
    if profile is None:
        return replace(cfg, strategy_id=int(strategy_id))
    return replace(
        cfg,
        strategy_id=int(strategy_id),
        target_profit=profile.target_profit,
        bet_sizing_mode=profile.bet_sizing_mode,
        base_order_cost=profile.base_order_cost,
        max_consecutive_losses=profile.max_consecutive_losses,
        max_stake=profile.max_stake,
        open_delay_seconds=profile.open_delay_seconds,
        signal_momentum_threshold=profile.signal_momentum_threshold,
        signal_fallback_strategy_id=profile.signal_fallback_strategy_id,
        signal_weak_signal_mode=profile.signal_weak_signal_mode,
        signal_history_fidelity_seconds=profile.signal_history_fidelity_seconds,
        signal_anchor_max_offset_seconds=profile.signal_anchor_max_offset_seconds,
        signal_dynamic_threshold_k=profile.signal_dynamic_threshold_k,
        signal_dynamic_threshold_min_points=profile.signal_dynamic_threshold_min_points,
        signal_lock_before_entry_seconds=profile.signal_lock_before_entry_seconds,
        max_stake_skip_alert_threshold=profile.max_stake_skip_alert_threshold,
        ofi_threshold=profile.ofi_threshold,
        max_entry_price=profile.max_entry_price,
        binance_signal_stale_seconds=profile.binance_signal_stale_seconds,
        strategy7_ofi_threshold=profile.strategy7_ofi_threshold,
        strategy7_momentum_threshold=profile.strategy7_momentum_threshold,
        strategy7_max_entry_price=profile.strategy7_max_entry_price,
        strategy7_min_signal_gap=profile.strategy7_min_signal_gap,
        strategy7_confirm_before_entry_seconds=profile.strategy7_confirm_before_entry_seconds,
        strategy7_late_confirm_strong_signal_gap=profile.strategy7_late_confirm_strong_signal_gap,
        strategy7_late_confirm_relax_seconds=profile.strategy7_late_confirm_relax_seconds,
    )


def _read_available_live_balance(*, cfg: AppConfig, clob_client: Any | None) -> float:
    if clob_client is None:
        clob_client = _create_live_clob_client(cfg)
    for attr_name in ("get_balance_allowance", "get_balance", "get_available_balance"):
        getter = getattr(clob_client, attr_name, None)
        if callable(getter):
            payload = getter()
            value = _coerce_positive_float(
                getattr(payload, "available", None)
                or (payload.get("available") if isinstance(payload, dict) else None)
                or (payload.get("balance") if isinstance(payload, dict) else None)
            )
            if value is not None:
                return value
    raise RuntimeError("Unable to determine live wallet balance.")


def place_live_order(
    cfg: AppConfig | None = None,
    *,
    market_client: PolymarketClient | None = None,
    binance_signal_service: BinanceDepth5SignalService | None = None,
    clob_client: Any | None = None,
    state_path: Path | None = None,
    log_path: Path | None = None,
    dry_run: bool = False,
    state_override: LiveStrategyState | None = None,
) -> dict[str, Any]:
    cfg = cfg or AppConfig()
    state_path = state_path or cfg.logs_dir / "live_session_state.json"
    state = state_override or load_session_state(
        state_path,
        effective_live_strategy_ids=[cfg.strategy_id],
    )
    persist_state = not dry_run and state_override is None


def _submit_live_strategy_order(
    *,
    strategy_cfg: AppConfig,
    strategy_state: LiveStrategyState,
    market_client: PolymarketClient | Any,
    live_client: Any,
    state_path: Path,
    log_path: Path,
    binance_signal_service: BinanceDepth5SignalService | None,
    dry_run: bool,
) -> dict[str, Any]:
    return place_live_order(
        strategy_cfg,
        market_client=market_client,
        binance_signal_service=binance_signal_service,
        clob_client=live_client,
        state_path=state_path,
        log_path=log_path,
        dry_run=dry_run,
        state_override=strategy_state,
    )
```

- [ ] **Step 4: Refactor `run_live_trading()` into a coordinator over strategy ids**

```python
strategy_ids = _live_strategy_ids_for_runtime(cfg)
state = load_session_state(state_path, effective_live_strategy_ids=strategy_ids)
remaining_live_budget = _read_available_live_balance(cfg=cfg, clob_client=live_client)
strategy_results: list[dict[str, Any]] = []
submitted_any = False

for strategy_id in strategy_ids:
    strategy_cfg = _cfg_for_live_strategy(cfg, strategy_id)
    strategy_state = state.live_strategies.setdefault(strategy_id, LiveStrategyState())
    strategy_state, pending_status, settled_previous_trade = _settle_pending_live_trade_if_needed(
        market_client=market_client,
        clob_client=live_client,
        strategy_state=strategy_state,
        now=now,
    )
    state.live_strategies[strategy_id] = strategy_state
    if pending_status is not None and pending_status["status"] == "pending_settlement":
        strategy_results.append({"strategy": strategy_id, **pending_status})
        continue

    side_decision = _resolve_side_from_strategy(
        cfg=strategy_cfg,
        state=strategy_state,
        slug=target_round.slug,
        quote=strategy_quote,
        market_client=market_client,
        window=target_round,
        now=now,
        entry_time=entry_time,
    )
    if side_decision.side is None:
        strategy_results.append(
            {
                "strategy": strategy_id,
                "status": "skipped",
                "skip_reason": side_decision.reason or "signal_unavailable",
            }
        )
        continue
    side = side_decision.side
    strategy_quote = replace(quote)
    _apply_strategy6_signal_to_quote(
        cfg=strategy_cfg,
        quote=strategy_quote,
        binance_signal_service=binance_signal_service,
    )
    price = strategy_quote.up_price if side == "UP" else strategy_quote.down_price
    plan = build_trade_plan(
        price=price,
        target_profit=strategy_cfg.target_profit,
        cash_pnl=strategy_state.cash_pnl,
        recovery_loss=strategy_state.recovery_loss,
        bet_sizing_mode=strategy_cfg.bet_sizing_mode,
        base_order_cost=strategy_cfg.base_order_cost,
        max_stake=strategy_cfg.max_stake,
    )
    if plan.should_trade and plan.order_cost > remaining_live_budget:
        strategy_results.append(
            {
                "strategy": strategy_id,
                "status": "skipped",
                "skip_reason": "insufficient_live_wallet_balance",
                "order_cost": plan.order_cost,
                "remaining_live_budget": remaining_live_budget,
            }
        )
        continue

    submission = _submit_live_strategy_order(
        strategy_cfg=strategy_cfg,
        strategy_state=strategy_state,
        market_client=market_client,
        live_client=live_client,
        state_path=state_path,
        log_path=log_path,
        binance_signal_service=binance_signal_service,
        dry_run=dry_run_once,
    )
    remaining_live_budget -= plan.order_cost
    submitted_any = True
    strategy_results.append({"strategy": strategy_id, **submission})

return {
    "status": "submitted" if submitted_any else "skipped",
    "strategies": strategy_results,
    "remaining_live_budget": remaining_live_budget,
}
```

- [ ] **Step 5: Run the targeted live runtime tests**

Run: `pytest tests/test_trader_runtime_and_live.py -k "wallet_budget or submits_multiple_strategies" -v`

Expected: PASS for both new multi-strategy runtime tests.

- [ ] **Step 6: Run the broader live runtime file as a regression pass**

Run: `pytest tests/test_trader_runtime_and_live.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add config.py trader.py tests/test_trader_runtime_and_live.py
git commit -m "feat: coordinate live multi-strategy runtime"
```

### Task 5: Aggregate Runtime Flags and Expose Live Multi-Strategy Dashboard State

**Files:**
- Modify: `runtime_control.py`
- Modify: `dashboard.py`
- Modify: `tests/test_runtime_manager.py`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Write failing runtime-control and dashboard tests**

```python
def test_runtime_control_reports_pending_live_order_when_any_live_strategy_is_pending():
    control = RuntimeControl(initial_mode="live")

    snapshot = control.update_worker_state(
        round_in_progress=True,
        safe_to_switch=False,
        pending_live_order=True,
    )

    assert snapshot.pending_live_order is True
    assert snapshot.safe_to_switch is False


def test_dashboard_config_payload_exposes_live_strategy_ids(tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    env_file.write_text(
        "TRADE_MODE=live\n"
        "LIVE_TRADING_ENABLED=true\n"
        "LIVE_STRATEGY_IDS=5,7\n",
        encoding="utf-8",
    )

    state = DashboardState(env_file=env_file)
    payload = state.get_config_payload()

    assert "LIVE_STRATEGY_IDS" in payload["editable_keys"]
    assert payload["env_values"]["LIVE_STRATEGY_IDS"] == "5,7"


def test_dashboard_runtime_status_includes_live_strategy_summaries(tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    env_file.write_text(
        "TRADE_MODE=live\n"
        "LIVE_TRADING_ENABLED=true\n"
        "LIVE_STRATEGY_IDS=5,7\n",
        encoding="utf-8",
    )

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "live_session_state.json").write_text(
        json.dumps(
            {
                "live_strategies": {
                    "5": {"cash_pnl": 1.2, "pending_live_slug": "slug-5"},
                    "7": {"cash_pnl": -0.3, "pending_live_slug": None},
                }
            }
        ),
        encoding="utf-8",
    )

    state = DashboardState(env_file=env_file)
    payload = state.get_runtime_status_payload()

    assert payload["pending_live_order"] is True
    assert payload["live_strategy_ids"] == ["5", "7"]
    assert payload["live_strategy_states"]["5"]["pending_live_slug"] == "slug-5"
```

- [ ] **Step 2: Run the dashboard and runtime tests to verify they fail**

Run: `pytest tests/test_dashboard.py -k live_strategy_ids -v`

Run: `pytest tests/test_runtime_manager.py -k pending_live_order -v`

Expected: FAIL because the payloads do not yet expose live strategy summaries.

- [ ] **Step 3: Add live multi-strategy metadata to dashboard config payloads**

```python
editable = list(DashboardState.EDITABLE_CONFIG_KEYS)
editable.insert(editable.index("STRATEGY_ID") + 1, "LIVE_STRATEGY_IDS")
DashboardState.EDITABLE_CONFIG_KEYS = tuple(editable)
DashboardState.CONFIG_ATTR_MAP["LIVE_STRATEGY_IDS"] = "live_strategy_ids"
DashboardState.SELECT_OPTIONS["LIVE_STRATEGY_IDS"] = ["1", "2", "3", "4", "5", "6", "7"]
DashboardState.FIELD_HELP["LIVE_STRATEGY_IDS"] = "实盘模式可同时运行多个策略，按输入顺序去重，例如 5,7。"
```

- [ ] **Step 4: Expose per-strategy live runtime state in `dashboard.py`**

```python
live_state = load_session_state(
    cfg.logs_dir / "live_session_state.json",
    effective_live_strategy_ids=list(getattr(cfg, "live_strategy_ids", []) or [cfg.strategy_id]),
)

live_strategy_ids = [str(item) for item in (getattr(cfg, "live_strategy_ids", []) or [cfg.strategy_id])]
live_strategy_states = {
    str(strategy_id): asdict(live_state.live_strategies.get(strategy_id) or LiveStrategyState())
    for strategy_id in (getattr(cfg, "live_strategy_ids", []) or [cfg.strategy_id])
}

payload["live_strategy_ids"] = live_strategy_ids
payload["live_strategy_states"] = live_strategy_states
payload["pending_live_order"] = any(
    bool((live_state.live_strategies.get(strategy_id) or LiveStrategyState()).pending_live_slug)
    for strategy_id in (getattr(cfg, "live_strategy_ids", []) or [cfg.strategy_id])
)
```

- [ ] **Step 5: Run the targeted dashboard and runtime tests**

Run: `pytest tests/test_dashboard.py -k live_strategy_ids -v`

Run: `pytest tests/test_runtime_manager.py -k pending_live_order -v`

Expected: PASS.

- [ ] **Step 6: Run the broader dashboard regression file**

Run: `pytest tests/test_dashboard.py tests/test_runtime_manager.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add runtime_control.py dashboard.py tests/test_dashboard.py tests/test_runtime_manager.py
git commit -m "feat: expose live multi-strategy dashboard state"
```

### Task 6: Update Operator Docs and Example Config

**Files:**
- Modify: `README.md`
- Modify: `.env.dashboard.example`
- Modify: `tests/test_dashboard_config_rendering.py`

- [ ] **Step 1: Write a failing doc-rendering coverage test for the new live field**

```python
def test_dashboard_config_rendering_lists_live_strategy_ids_field():
    payload = DashboardState(env_file=Path("dummy/.env.dashboard")).get_config_payload()

    assert "LIVE_STRATEGY_IDS" in payload["editable_keys"]
    assert payload["labels"]["LIVE_STRATEGY_IDS"] == "实盘策略组合"
```

- [ ] **Step 2: Run the rendering test to verify it fails if docs/config metadata is incomplete**

Run: `pytest tests/test_dashboard_config_rendering.py -k live_strategy_ids -v`

Expected: FAIL if the previous dashboard metadata work is incomplete.

- [ ] **Step 3: Update the example env file and README**

```dotenv
# -------- 实盘策略组合
# 留空时实盘回退到单个 STRATEGY_ID
# 多策略实盘使用独立的 LIVE_STRATEGY_IDS
LIVE_STRATEGY_IDS=5,7

# 每个实盘策略可以有自己的参数
LIVE_STRATEGY_5_BASE_ORDER_COST=1.5
LIVE_STRATEGY_5_TARGET_PROFIT=0.8
LIVE_STRATEGY_7_BASE_ORDER_COST=2.0
LIVE_STRATEGY_7_STRATEGY7_MAX_ENTRY_PRICE=0.53
```

```md
### Live multi-strategy profiles

Live mode can now run more than one strategy at once with:

- `LIVE_STRATEGY_IDS=5,7`

Each live strategy can keep its own parameter set through `LIVE_STRATEGY_<id>_<FIELD>` keys. Live mode still uses one `MARKET_TIMEFRAME`, one wallet, one live session state file, and one shared `live_orders.csv`, but strategy ledger state and pending live orders are isolated per strategy.
```

- [ ] **Step 4: Run the doc/config rendering regression test**

Run: `pytest tests/test_dashboard_config_rendering.py -k live_strategy_ids -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add README.md .env.dashboard.example tests/test_dashboard_config_rendering.py
git commit -m "docs: describe live multi-strategy setup"
```

### Task 7: Final Verification Pass

**Files:**
- Modify: `docs/superpowers/plans/2026-04-23-live-multi-strategy-implementation.md`

- [ ] **Step 1: Run the focused regression suite**

Run: `pytest tests/test_config_encoding.py tests/test_trader_runtime_and_live.py tests/test_dashboard.py tests/test_runtime_manager.py tests/test_dashboard_config_rendering.py -v`

Expected: PASS.

- [ ] **Step 2: Run the launcher smoke tests that cover mode wiring**

Run: `pytest tests/test_runtime_launcher.py -v`

Expected: PASS.

- [ ] **Step 3: Inspect git diff for accidental scope creep**

Run: `git diff --stat HEAD~6..HEAD`

Expected: only `config.py`, `models.py`, `trader.py`, `runtime_control.py`, `dashboard.py`, `README.md`, `.env.dashboard.example`, and the targeted test files should appear.

- [ ] **Step 4: Commit any final verification-only adjustments**

```bash
git add config.py models.py trader.py runtime_control.py dashboard.py README.md .env.dashboard.example tests/test_config_encoding.py tests/test_trader_runtime_and_live.py tests/test_dashboard.py tests/test_runtime_manager.py tests/test_dashboard_config_rendering.py
git commit -m "test: verify live multi-strategy rollout"
```
