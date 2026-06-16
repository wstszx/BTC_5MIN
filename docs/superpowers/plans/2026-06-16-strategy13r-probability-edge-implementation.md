# Strategy 13R Probability Edge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Strategy 13R as a conservative paper/shadow probability-edge strategy that is fee-aware and not live-enabled by default.

**Architecture:** Extend the existing strategy profile/config path to accept strategy id 13 and Strategy 13R settings, then add probability-edge decision helpers in `strategy_decision.py`. Reuse existing `SessionState.strategy11_round_start_btc_price` as the round BTC anchor for Strategy 13R to avoid state schema churn, and populate existing signal probability/edge/delta fields for diagnostics.

**Tech Stack:** Python 3.12, dataclasses, pytest, existing runtime/config/trade-log modules.

---

## File Structure

- Modify `config.py`
  - Raise the allowed strategy id max from 12 to 13.
  - Add Strategy 13R config fields to `AppConfig`, `PaperTimeframeProfile`, and `LiveStrategyProfile`.
  - Parse `STRATEGY_13_*` and short profile keys.
  - Preserve live defaults by only creating a live Strategy 13 profile when `LIVE_STRATEGY_IDS` explicitly includes 13.
- Modify `strategy_decision.py`
  - Add `Strategy13ProbabilityEdge` dataclass.
  - Add Strategy 13R probability, shrink, edge, micro-confirmation, and side-decision helpers.
  - Route `strategy_id == 13` through `resolve_side_from_strategy`.
- Modify `tests/test_config_encoding.py`
  - Update strategy id validation expectations.
  - Add tests proving Strategy 13 paper profiles parse and live profiles remain opt-in.
- Modify `tests/test_strategy_decision.py`
  - Add focused Strategy 13R decision tests for probability direction, shrink, fee-adjusted edge, low edge skip, micro conflict, stale BTC price, and entry-too-late behavior.
- Dashboard labels/reason translations for Strategy 13R are out of scope for this plan.

---

### Task 1: Config Supports Strategy 13R

**Files:**
- Modify: `config.py`
- Test: `tests/test_config_encoding.py`

- [ ] **Step 1: Write failing config tests**

Add these tests near the existing strategy id and strategy 11/12 config tests in `tests/test_config_encoding.py`:

```python
def test_collect_config_warnings_accepts_strategy13():
    warnings = collect_config_warnings(
        {
            "STRATEGY_ID": "13",
            "PAPER_STRATEGY_IDS": "13",
            "LIVE_STRATEGY_IDS": "13",
        }
    )

    assert "STRATEGY_ID" not in warnings
    assert "PAPER_STRATEGY_IDS" not in warnings
    assert "LIVE_STRATEGY_IDS" not in warnings


def test_build_config_accepts_strategy13_paper_profile_without_live_default():
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "7",
            "PAPER_STRATEGY_IDS": "13",
            "LIVE_STRATEGY_IDS": "7",
            "STRATEGY_13_MIN_EDGE": "0.05",
            "STRATEGY_13_EDGE_BUFFER": "0.007",
            "STRATEGY_13_VOL_LOOKBACK_SECONDS": "240",
            "STRATEGY_13_VOL_MIN_BPS": "9",
            "STRATEGY_13_VOL_MAX_BPS": "42",
            "STRATEGY_13_PROBABILITY_SHRINK": "0.30",
            "STRATEGY_13_MIN_PROBABILITY": "0.60",
            "STRATEGY_13_MAX_ENTRY_PRICE": "0.53",
            "STRATEGY_13_CONFIRM_MICRO": "false",
            "STRATEGY_13_MICRO_DISAGREE_PENALTY": "0.015",
            "STRATEGY_13_CONFIRM_BEFORE_ENTRY_SECONDS": "3",
        }
    )

    assert cfg.paper_strategy_ids == [13]
    assert cfg.live_strategy_ids == [7]
    assert 13 in cfg.paper_strategy_profiles
    assert 13 not in cfg.live_profiles
    profile = cfg.paper_strategy_profiles[13]
    assert profile.strategy13_min_edge == pytest.approx(0.05)
    assert profile.strategy13_edge_buffer == pytest.approx(0.007)
    assert profile.strategy13_vol_lookback_seconds == 240
    assert profile.strategy13_vol_min_bps == pytest.approx(9)
    assert profile.strategy13_vol_max_bps == pytest.approx(42)
    assert profile.strategy13_probability_shrink == pytest.approx(0.30)
    assert profile.strategy13_min_probability == pytest.approx(0.60)
    assert profile.max_entry_price == pytest.approx(0.53)
    assert profile.strategy13_confirm_micro is False
    assert profile.strategy13_micro_disagree_penalty == pytest.approx(0.015)
    assert profile.strategy13_confirm_before_entry_seconds == 3


def test_build_config_accepts_strategy13_short_profile_keys():
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "13",
            "PAPER_STRATEGY_IDS": "13",
            "LIVE_STRATEGY_IDS": "",
            "STRATEGY_13_MIN_EDGE": "0.052",
            "STRATEGY_13_EDGE_BUFFER": "0.008",
            "STRATEGY_13_VOL_LOOKBACK_SECONDS": "210",
            "STRATEGY_13_VOL_MIN_BPS": "10",
            "STRATEGY_13_VOL_MAX_BPS": "40",
            "STRATEGY_13_PROBABILITY_SHRINK": "0.20",
            "STRATEGY_13_MIN_PROBABILITY": "0.59",
            "STRATEGY_13_CONFIRM_MICRO": "true",
            "STRATEGY_13_MICRO_DISAGREE_PENALTY": "0.025",
            "STRATEGY_13_CONFIRM_BEFORE_ENTRY_SECONDS": "4",
        }
    )

    profile = cfg.paper_strategy_profiles[13]
    assert profile.strategy13_min_edge == pytest.approx(0.052)
    assert profile.strategy13_edge_buffer == pytest.approx(0.008)
    assert profile.strategy13_vol_lookback_seconds == 210
    assert profile.strategy13_vol_min_bps == pytest.approx(10)
    assert profile.strategy13_vol_max_bps == pytest.approx(40)
    assert profile.strategy13_probability_shrink == pytest.approx(0.20)
    assert profile.strategy13_min_probability == pytest.approx(0.59)
    assert profile.strategy13_confirm_micro is True
    assert profile.strategy13_micro_disagree_penalty == pytest.approx(0.025)
    assert profile.strategy13_confirm_before_entry_seconds == 4
```

Update the existing invalid scalar expectation so strategy 14 is invalid instead of strategy 13:

```python
def test_collect_config_warnings_reports_invalid_scalar_values():
    warnings = collect_config_warnings(
        {
            "STRATEGY_7_MAX_STAKE": "abc",
            "WS_ENABLED": "maybe",
            "STRATEGY_ID": "14",
            "MARKET_TIMEFRAME": "7m",
            "TARGET_PROFIT": "1.2",
        }
    )

    assert warnings["STRATEGY_7_MAX_STAKE"] == "Invalid value for STRATEGY_7_MAX_STAKE: expected number, got 'abc'"
    assert warnings["WS_ENABLED"] == "Invalid value for WS_ENABLED: expected true/false, got 'maybe'"
    assert warnings["STRATEGY_ID"] == "Invalid value for STRATEGY_ID: expected strategy id 1-13, got '14'"
    assert warnings["MARKET_TIMEFRAME"] == "Invalid value for MARKET_TIMEFRAME: expected one of 5m, 15m, got '7m'"
    assert "TARGET_PROFIT" not in warnings
```

- [ ] **Step 2: Run config tests and verify they fail**

Run:

```powershell
pytest tests/test_config_encoding.py -k "strategy13 or invalid_scalar" -q
```

Expected: FAIL because strategy id 13 is not accepted and Strategy 13R fields do not exist.

- [ ] **Step 3: Implement Strategy 13R config fields**

In `config.py`, change:

```python
_STRATEGY_ID_MAX = 12
```

to:

```python
_STRATEGY_ID_MAX = 13
```

Add a short-key map after `_STRATEGY12_SHORT_PROFILE_KEYS`:

```python
_STRATEGY13_SHORT_PROFILE_KEYS: dict[str, str] = {
    "MIN_EDGE": "STRATEGY13_MIN_EDGE",
    "EDGE_BUFFER": "STRATEGY13_EDGE_BUFFER",
    "VOL_LOOKBACK_SECONDS": "STRATEGY13_VOL_LOOKBACK_SECONDS",
    "VOL_MIN_BPS": "STRATEGY13_VOL_MIN_BPS",
    "VOL_MAX_BPS": "STRATEGY13_VOL_MAX_BPS",
    "PROBABILITY_SHRINK": "STRATEGY13_PROBABILITY_SHRINK",
    "MIN_PROBABILITY": "STRATEGY13_MIN_PROBABILITY",
    "CONFIRM_MICRO": "STRATEGY13_CONFIRM_MICRO",
    "MICRO_DISAGREE_PENALTY": "STRATEGY13_MICRO_DISAGREE_PENALTY",
    "CONFIRM_BEFORE_ENTRY_SECONDS": "STRATEGY13_CONFIRM_BEFORE_ENTRY_SECONDS",
}
```

Add it to `_STRATEGY_SHORT_PROFILE_KEYS`:

```python
13: _STRATEGY13_SHORT_PROFILE_KEYS,
```

Add to `_INT_CONFIG_KEYS`:

```python
"STRATEGY13_VOL_LOOKBACK_SECONDS",
"STRATEGY13_CONFIRM_BEFORE_ENTRY_SECONDS",
```

Add to `_FLOAT_CONFIG_KEYS`:

```python
"STRATEGY13_MIN_EDGE",
"STRATEGY13_EDGE_BUFFER",
"STRATEGY13_VOL_MIN_BPS",
"STRATEGY13_VOL_MAX_BPS",
"STRATEGY13_PROBABILITY_SHRINK",
"STRATEGY13_MIN_PROBABILITY",
"STRATEGY13_MICRO_DISAGREE_PENALTY",
```

Add to `_BOOL_CONFIG_KEYS`:

```python
"STRATEGY13_CONFIRM_MICRO",
```

Add to `_GLOBAL_STRATEGY_CONFIG_KEYS`:

```python
"STRATEGY13_MIN_EDGE",
"STRATEGY13_EDGE_BUFFER",
"STRATEGY13_VOL_LOOKBACK_SECONDS",
"STRATEGY13_VOL_MIN_BPS",
"STRATEGY13_VOL_MAX_BPS",
"STRATEGY13_PROBABILITY_SHRINK",
"STRATEGY13_MIN_PROBABILITY",
"STRATEGY13_CONFIRM_MICRO",
"STRATEGY13_MICRO_DISAGREE_PENALTY",
"STRATEGY13_CONFIRM_BEFORE_ENTRY_SECONDS",
```

Add fields to `PaperTimeframeProfile` and `LiveStrategyProfile`:

```python
strategy13_min_edge: float
strategy13_edge_buffer: float
strategy13_vol_lookback_seconds: int
strategy13_vol_min_bps: float
strategy13_vol_max_bps: float
strategy13_probability_shrink: float
strategy13_min_probability: float
strategy13_confirm_micro: bool
strategy13_micro_disagree_penalty: float
strategy13_confirm_before_entry_seconds: int
```

Add AppConfig defaults after the Strategy 11 fields:

```python
strategy13_min_edge: float = 0.04
strategy13_edge_buffer: float = 0.005
strategy13_vol_lookback_seconds: int = 180
strategy13_vol_min_bps: float = 8.0
strategy13_vol_max_bps: float = 45.0
strategy13_probability_shrink: float = 0.25
strategy13_min_probability: float = 0.58
strategy13_confirm_micro: bool = True
strategy13_micro_disagree_penalty: float = 0.02
strategy13_confirm_before_entry_seconds: int = 2
```

Add these fields everywhere profile constructors currently pass Strategy 11 fields:

```python
strategy13_min_edge=cfg.strategy13_min_edge,
strategy13_edge_buffer=cfg.strategy13_edge_buffer,
strategy13_vol_lookback_seconds=cfg.strategy13_vol_lookback_seconds,
strategy13_vol_min_bps=cfg.strategy13_vol_min_bps,
strategy13_vol_max_bps=cfg.strategy13_vol_max_bps,
strategy13_probability_shrink=cfg.strategy13_probability_shrink,
strategy13_min_probability=cfg.strategy13_min_probability,
strategy13_confirm_micro=cfg.strategy13_confirm_micro,
strategy13_micro_disagree_penalty=cfg.strategy13_micro_disagree_penalty,
strategy13_confirm_before_entry_seconds=cfg.strategy13_confirm_before_entry_seconds,
```

In `_profile_for_strategy`, include strategy 13 in the `strategy7_max_entry_fallback` set:

```python
if strategy_id in {7, 8, 9, 10, 11, 12, 13}
```

Add Strategy 13R per-strategy parsing in `_profile_for_strategy`:

```python
strategy13_min_edge=_strategy_env_float(strategy_id, "STRATEGY13_MIN_EDGE", cfg.strategy13_min_edge),
strategy13_edge_buffer=_strategy_env_float(strategy_id, "STRATEGY13_EDGE_BUFFER", cfg.strategy13_edge_buffer),
strategy13_vol_lookback_seconds=_strategy_env_int(
    strategy_id,
    "STRATEGY13_VOL_LOOKBACK_SECONDS",
    cfg.strategy13_vol_lookback_seconds,
),
strategy13_vol_min_bps=_strategy_env_float(strategy_id, "STRATEGY13_VOL_MIN_BPS", cfg.strategy13_vol_min_bps),
strategy13_vol_max_bps=_strategy_env_float(strategy_id, "STRATEGY13_VOL_MAX_BPS", cfg.strategy13_vol_max_bps),
strategy13_probability_shrink=_strategy_env_float(
    strategy_id,
    "STRATEGY13_PROBABILITY_SHRINK",
    cfg.strategy13_probability_shrink,
),
strategy13_min_probability=_strategy_env_float(
    strategy_id,
    "STRATEGY13_MIN_PROBABILITY",
    cfg.strategy13_min_probability,
),
strategy13_confirm_micro=_strategy_env_bool(strategy_id, "STRATEGY13_CONFIRM_MICRO", cfg.strategy13_confirm_micro),
strategy13_micro_disagree_penalty=_strategy_env_float(
    strategy_id,
    "STRATEGY13_MICRO_DISAGREE_PENALTY",
    cfg.strategy13_micro_disagree_penalty,
),
strategy13_confirm_before_entry_seconds=_strategy_env_int(
    strategy_id,
    "STRATEGY13_CONFIRM_BEFORE_ENTRY_SECONDS",
    cfg.strategy13_confirm_before_entry_seconds,
),
```

In the `PaperTimeframeProfile` constructor inside `AppConfig.__post_init__`, pass the AppConfig Strategy 13R defaults:

```python
strategy13_min_edge=self.strategy13_min_edge,
strategy13_edge_buffer=self.strategy13_edge_buffer,
strategy13_vol_lookback_seconds=self.strategy13_vol_lookback_seconds,
strategy13_vol_min_bps=self.strategy13_vol_min_bps,
strategy13_vol_max_bps=self.strategy13_vol_max_bps,
strategy13_probability_shrink=self.strategy13_probability_shrink,
strategy13_min_probability=self.strategy13_min_probability,
strategy13_confirm_micro=self.strategy13_confirm_micro,
strategy13_micro_disagree_penalty=self.strategy13_micro_disagree_penalty,
strategy13_confirm_before_entry_seconds=self.strategy13_confirm_before_entry_seconds,
```

- [ ] **Step 4: Run config tests and verify they pass**

Run:

```powershell
pytest tests/test_config_encoding.py -k "strategy13 or invalid_scalar" -q
```

Expected: PASS.

- [ ] **Step 5: Commit config support**

Run:

```powershell
git add config.py tests/test_config_encoding.py
git commit -m "feat: add strategy 13r config"
```

Expected: commit succeeds.

---

### Task 2: Add Strategy 13R Probability-Edge Helpers

**Files:**
- Modify: `strategy_decision.py`
- Test: `tests/test_strategy_decision.py`

- [ ] **Step 1: Write failing helper tests**

Add `effective_price_after_fee` below the existing pytest/config imports in `tests/test_strategy_decision.py`:

```python
from clob_adapter import effective_price_after_fee
```

Replace the existing single-line `strategy_decision` import with this parenthesized import:

```python
from strategy_decision import (
    SideDecision,
    effective_decision_order_cost_multiplier,
    estimate_strategy13_probability_edge,
    resolve_side_from_strategy,
    strategy7_order_cost_multiplier,
)
```

Add these tests near the Strategy 11/12 probability tests:

```python
def test_strategy13_probability_moves_with_btc_distance_and_remaining_time():
    now = datetime(2026, 4, 30, 1, 4, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
    )
    cfg = AppConfig(
        strategy_id=13,
        strategy13_vol_min_bps=8.0,
        strategy13_vol_max_bps=45.0,
        strategy13_probability_shrink=0.25,
        strategy13_edge_buffer=0.0,
        strategy13_min_probability=0.50,
        strategy13_min_edge=0.0,
    )
    up_quote = MarketQuote(
        slug="s1",
        up_best_ask=0.50,
        down_best_ask=0.50,
        binance_mid_price=100080.0,
        binance_signal_at=now,
    )
    down_quote = MarketQuote(
        slug="s1",
        up_best_ask=0.50,
        down_best_ask=0.50,
        binance_mid_price=99920.0,
        binance_signal_at=now,
    )

    up_edge = estimate_strategy13_probability_edge(
        cfg=cfg,
        quote=up_quote,
        window=window,
        now=now,
        round_start_btc_price=100000.0,
    )
    down_edge = estimate_strategy13_probability_edge(
        cfg=cfg,
        quote=down_quote,
        window=window,
        now=now,
        round_start_btc_price=100000.0,
    )

    assert up_edge is not None
    assert down_edge is not None
    assert up_edge.up_probability > 0.5
    assert up_edge.down_probability < 0.5
    assert down_edge.down_probability > 0.5
    assert down_edge.up_probability < 0.5
    assert up_edge.best_side == "UP"
    assert down_edge.best_side == "DOWN"


def test_strategy13_probability_shrink_reduces_confidence_toward_half():
    now = datetime(2026, 4, 30, 1, 4, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
    )
    base_cfg = AppConfig(
        strategy_id=13,
        strategy13_vol_min_bps=8.0,
        strategy13_vol_max_bps=45.0,
        strategy13_edge_buffer=0.0,
        strategy13_min_probability=0.50,
        strategy13_min_edge=0.0,
    )
    quote = MarketQuote(
        slug="s1",
        up_best_ask=0.50,
        down_best_ask=0.50,
        binance_mid_price=100120.0,
        binance_signal_at=now,
    )

    unshrunk = estimate_strategy13_probability_edge(
        cfg=replace(base_cfg, strategy13_probability_shrink=0.0),
        quote=quote,
        window=window,
        now=now,
        round_start_btc_price=100000.0,
    )
    shrunk = estimate_strategy13_probability_edge(
        cfg=replace(base_cfg, strategy13_probability_shrink=0.5),
        quote=quote,
        window=window,
        now=now,
        round_start_btc_price=100000.0,
    )

    assert unshrunk is not None
    assert shrunk is not None
    assert shrunk.up_probability < unshrunk.up_probability
    assert shrunk.up_probability > 0.5
```

Add `replace` to the dataclass imports:

```python
from dataclasses import replace
```

Add the fee-adjusted edge test:

```python
def test_strategy13_edge_uses_fee_adjusted_effective_price():
    now = datetime(2026, 4, 30, 1, 4, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
    )
    cfg = AppConfig(
        strategy_id=13,
        strategy13_min_edge=0.0,
        strategy13_edge_buffer=0.0,
        strategy13_vol_min_bps=30.0,
        strategy13_vol_max_bps=30.0,
        strategy13_probability_shrink=0.0,
        strategy13_min_probability=0.50,
    )
    quote = MarketQuote(
        slug="s1",
        up_best_ask=0.55,
        down_best_ask=0.45,
        binance_mid_price=100140.0,
        binance_signal_at=now,
    )

    edge = estimate_strategy13_probability_edge(
        cfg=cfg,
        quote=quote,
        window=window,
        now=now,
        round_start_btc_price=100000.0,
    )

    assert edge is not None
    assert edge.best_side == "UP"
    assert edge.best_price == pytest.approx(0.55)
    assert edge.best_effective_price == pytest.approx(effective_price_after_fee(0.55))
    assert edge.best_edge == pytest.approx(edge.up_probability - effective_price_after_fee(0.55), abs=0.001)
```

- [ ] **Step 2: Run helper tests and verify they fail**

Run:

```powershell
pytest tests/test_strategy_decision.py -k "strategy13_probability or strategy13_edge_uses_fee" -q
```

Expected: FAIL because `estimate_strategy13_probability_edge` is missing.

- [ ] **Step 3: Implement Strategy 13R helper dataclass and estimator**

In `strategy_decision.py`, add this dataclass after `Strategy11Probability`:

```python
@dataclass(slots=True)
class Strategy13ProbabilityEdge:
    up_probability: float
    down_probability: float
    raw_up_probability: float
    raw_down_probability: float
    volatility_bps: float
    up_effective_price: float | None
    down_effective_price: float | None
    up_edge: float | None
    down_edge: float | None
    best_side: str | None
    best_price: float | None
    best_effective_price: float | None
    best_edge: float | None
```

Add these helper functions near `estimate_strategy11_probability`:

```python
def _strategy13_clamped_volatility_bps(cfg: AppConfig) -> float:
    min_bps = max(0.1, float(getattr(cfg, "strategy13_vol_min_bps", 8.0)))
    max_bps = max(min_bps, float(getattr(cfg, "strategy13_vol_max_bps", 45.0)))
    configured = float(getattr(cfg, "strategy11_volatility_bps_per_sqrt_minute", min_bps))
    return max(min_bps, min(max_bps, configured))


def _strategy13_shrink_probability(probability: float, shrink: float) -> float:
    bounded = _clamp_probability(probability, low=0.0, high=1.0)
    shrink_value = max(0.0, min(1.0, float(shrink)))
    return _clamp_probability(0.5 + (bounded - 0.5) * (1.0 - shrink_value), low=0.0, high=1.0)


def estimate_strategy13_probability_edge(
    *,
    cfg: AppConfig,
    quote: MarketQuote,
    window: MarketWindow,
    now: datetime,
    round_start_btc_price: float,
) -> Strategy13ProbabilityEdge | None:
    current_btc_price = getattr(quote, "binance_mid_price", None)
    if current_btc_price is None or current_btc_price <= 0 or round_start_btc_price <= 0:
        return None

    remaining_seconds = max(1.0, (window.end_time - now).total_seconds())
    remaining_minutes = max(remaining_seconds / 60.0, 1.0 / 60.0)
    volatility_bps = _strategy13_clamped_volatility_bps(cfg)
    sigma_price = round_start_btc_price * (volatility_bps / 10_000.0) * sqrt(remaining_minutes)
    if sigma_price <= 0:
        return None

    distance = float(current_btc_price) - float(round_start_btc_price)
    raw_up_probability = _normal_cdf(distance / sigma_price)
    raw_down_probability = 1.0 - raw_up_probability
    shrink = float(getattr(cfg, "strategy13_probability_shrink", 0.25))
    up_probability = _strategy13_shrink_probability(raw_up_probability, shrink)
    down_probability = 1.0 - up_probability

    edge_buffer = max(0.0, float(getattr(cfg, "strategy13_edge_buffer", 0.0)))
    up_price = resolve_quote_price("UP", quote)
    down_price = resolve_quote_price("DOWN", quote)
    up_effective_price = effective_price_after_fee(up_price) if is_valid_signal_price(up_price) else None
    down_effective_price = effective_price_after_fee(down_price) if is_valid_signal_price(down_price) else None
    up_edge = up_probability - up_effective_price - edge_buffer if up_effective_price is not None else None
    down_edge = down_probability - down_effective_price - edge_buffer if down_effective_price is not None else None

    best_side: str | None = None
    best_price: float | None = None
    best_effective_price: float | None = None
    best_edge: float | None = None
    for side, price, effective_price, edge in (
        ("UP", up_price, up_effective_price, up_edge),
        ("DOWN", down_price, down_effective_price, down_edge),
    ):
        if edge is None:
            continue
        if best_edge is None or edge > best_edge:
            best_side = side
            best_price = price
            best_effective_price = effective_price
            best_edge = edge

    return Strategy13ProbabilityEdge(
        up_probability=up_probability,
        down_probability=down_probability,
        raw_up_probability=raw_up_probability,
        raw_down_probability=raw_down_probability,
        volatility_bps=volatility_bps,
        up_effective_price=up_effective_price,
        down_effective_price=down_effective_price,
        up_edge=up_edge,
        down_edge=down_edge,
        best_side=best_side,
        best_price=best_price,
        best_effective_price=best_effective_price,
        best_edge=best_edge,
    )
```

This first implementation intentionally uses configured/fallback volatility clamped by Strategy 13R bounds. Runtime-derived volatility is out of scope for this implementation.

- [ ] **Step 4: Run helper tests and verify they pass**

Run:

```powershell
pytest tests/test_strategy_decision.py -k "strategy13_probability or strategy13_edge_uses_fee" -q
```

Expected: PASS.

- [ ] **Step 5: Commit helper implementation**

Run:

```powershell
git add strategy_decision.py tests/test_strategy_decision.py
git commit -m "feat: add strategy 13r probability edge estimator"
```

Expected: commit succeeds.

---

### Task 3: Route Strategy 13R Decisions

**Files:**
- Modify: `strategy_decision.py`
- Test: `tests/test_strategy_decision.py`

- [ ] **Step 1: Write failing Strategy 13R decision tests**

Add these tests near the Strategy 12 tests:

```python
def test_strategy13_buys_underpriced_probability_edge_with_micro_confirmation():
    now = datetime(2026, 4, 30, 1, 2, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
        price_to_beat=100000.0,
    )
    cfg = AppConfig(
        strategy_id=13,
        max_entry_price=0.54,
        strategy13_min_edge=0.02,
        strategy13_edge_buffer=0.0,
        strategy13_vol_min_bps=8.0,
        strategy13_vol_max_bps=12.0,
        strategy13_probability_shrink=0.25,
        strategy13_min_probability=0.58,
        strategy13_confirm_micro=True,
        strategy13_micro_disagree_penalty=0.02,
        strategy13_confirm_before_entry_seconds=0,
        strategy7_ofi_threshold=0.50,
        strategy7_momentum_threshold=0.005,
        strategy7_min_signal_gap=0.0,
        strategy7_max_momentum_delta=0.12,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState()
    quote = MarketQuote(
        slug="s1",
        up_price=0.52,
        up_best_ask=0.52,
        down_price=0.48,
        down_best_ask=0.48,
        strategy6_ofi_score=0.72,
        strategy6_signal_at=now,
        binance_mid_price=100140.0,
        binance_signal_at=now,
    )

    decision = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        window=window,
        entry_time=now,
        now=now,
    )

    assert state.strategy11_round_start_btc_price == pytest.approx(100000.0)
    assert decision.side == "UP"
    assert decision.reason is None
    assert decision.candidate_side == "UP"
    assert decision.candidate_price == pytest.approx(0.52)
    assert decision.signal_open_up_price == pytest.approx(100000.0)
    assert decision.signal_current_up_price == pytest.approx(100140.0)
    assert decision.signal_probability is not None
    assert decision.signal_probability >= cfg.strategy13_min_probability
    assert decision.signal_edge is not None
    assert decision.signal_edge >= cfg.strategy13_min_edge
    assert decision.signal_threshold == pytest.approx(cfg.strategy13_min_edge)
    assert decision.ofi_score == pytest.approx(0.72)


def test_strategy13_skips_when_edge_is_too_low():
    now = datetime(2026, 4, 30, 1, 2, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
        price_to_beat=100000.0,
    )
    cfg = AppConfig(
        strategy_id=13,
        max_entry_price=0.56,
        strategy13_min_edge=0.08,
        strategy13_edge_buffer=0.0,
        strategy13_vol_min_bps=30.0,
        strategy13_vol_max_bps=30.0,
        strategy13_probability_shrink=0.25,
        strategy13_min_probability=0.58,
        strategy13_confirm_micro=False,
        strategy13_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState(signal_round_slug="s1", strategy11_round_start_btc_price=100000.0)
    quote = MarketQuote(
        slug="s1",
        up_price=0.54,
        up_best_ask=0.54,
        down_price=0.46,
        down_best_ask=0.46,
        binance_mid_price=100080.0,
        binance_signal_at=now,
    )

    decision = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        window=window,
        entry_time=now,
        now=now,
    )

    assert decision.side is None
    assert decision.reason == "strategy13_edge_too_low"
    assert decision.candidate_side is not None
    assert decision.signal_probability is not None
    assert decision.signal_edge is not None
    assert decision.signal_threshold == pytest.approx(0.08)


def test_strategy13_skips_when_microstructure_conflicts():
    now = datetime(2026, 4, 30, 1, 2, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
        price_to_beat=100000.0,
    )
    cfg = AppConfig(
        strategy_id=13,
        max_entry_price=0.54,
        strategy13_min_edge=0.02,
        strategy13_edge_buffer=0.0,
        strategy13_vol_min_bps=8.0,
        strategy13_vol_max_bps=12.0,
        strategy13_probability_shrink=0.25,
        strategy13_min_probability=0.58,
        strategy13_confirm_micro=True,
        strategy13_micro_disagree_penalty=0.02,
        strategy13_confirm_before_entry_seconds=0,
        strategy7_ofi_threshold=0.50,
        strategy7_momentum_threshold=0.005,
        strategy7_min_signal_gap=0.0,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.52,
        up_best_ask=0.52,
        down_price=0.48,
        down_best_ask=0.48,
        strategy6_ofi_score=-0.72,
        strategy6_signal_at=now,
        binance_mid_price=100140.0,
        binance_signal_at=now,
    )

    decision = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        window=window,
        entry_time=now,
        now=now,
    )

    assert decision.side is None
    assert decision.reason == "strategy13_micro_conflict"
    assert decision.candidate_side == "UP"
    assert decision.signal_probability is not None
    assert decision.signal_edge is not None
    assert decision.ofi_score == pytest.approx(-0.72)


def test_strategy13_skips_when_btc_price_is_stale():
    now = datetime(2026, 4, 30, 1, 2, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
        price_to_beat=100000.0,
    )
    cfg = AppConfig(strategy_id=13, binance_signal_stale_seconds=1.0)
    state = SessionState()
    quote = MarketQuote(
        slug="s1",
        up_best_ask=0.52,
        down_best_ask=0.48,
        binance_mid_price=100140.0,
        binance_signal_at=now - timedelta(seconds=5),
    )

    decision = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        window=window,
        entry_time=now,
        now=now,
    )

    assert decision.side is None
    assert decision.reason == "strategy13_btc_price_stale"
    assert decision.signal_threshold == pytest.approx(1.0)


def test_strategy13_skips_when_confirmation_is_too_late():
    now = datetime(2026, 4, 30, 1, 4, 59, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
        price_to_beat=100000.0,
    )
    cfg = AppConfig(
        strategy_id=13,
        max_entry_price=0.54,
        strategy13_min_edge=0.02,
        strategy13_edge_buffer=0.0,
        strategy13_vol_min_bps=8.0,
        strategy13_vol_max_bps=12.0,
        strategy13_probability_shrink=0.25,
        strategy13_min_probability=0.58,
        strategy13_confirm_micro=False,
        strategy13_confirm_before_entry_seconds=2,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState()
    quote = MarketQuote(
        slug="s1",
        up_price=0.52,
        up_best_ask=0.52,
        down_price=0.48,
        down_best_ask=0.48,
        binance_mid_price=100140.0,
        binance_signal_at=now,
    )

    decision = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        window=window,
        entry_time=window.end_time,
        now=now,
    )

    assert decision.side is None
    assert decision.reason == "strategy13_entry_too_late"
    assert decision.candidate_side == "UP"
    assert decision.signal_probability is not None
```

- [ ] **Step 2: Run Strategy 13R decision tests and verify they fail**

Run:

```powershell
pytest tests/test_strategy_decision.py -k "strategy13_" -q
```

Expected: FAIL because `resolve_side_from_strategy` does not route strategy 13.

- [ ] **Step 3: Implement `evaluate_strategy13_probability_edge` side decision**

In `strategy_decision.py`, add this function after `evaluate_strategy12_hybrid_edge`:

```python
def _strategy13_best_probability(edge: Strategy13ProbabilityEdge) -> float | None:
    if edge.best_side == "UP":
        return edge.up_probability
    if edge.best_side == "DOWN":
        return edge.down_probability
    return None


def _strategy13_decision_from_edge(
    *,
    cfg: AppConfig,
    state: SessionState,
    quote: MarketQuote,
    now: datetime,
    window: MarketWindow,
    edge: Strategy13ProbabilityEdge,
    reason: str | None,
    ofi_score: float | None = None,
) -> SideDecision:
    current_btc_price = getattr(quote, "binance_mid_price", None)
    best_probability = _strategy13_best_probability(edge)
    distance = (
        float(current_btc_price) - float(state.strategy11_round_start_btc_price)
        if current_btc_price is not None and state.strategy11_round_start_btc_price is not None
        else None
    )
    return SideDecision(
        side=None if reason is not None else edge.best_side,
        reason=reason,
        candidate_side=edge.best_side,
        candidate_price=edge.best_price,
        signal_open_up_price=state.strategy11_round_start_btc_price,
        signal_current_up_price=float(current_btc_price) if current_btc_price is not None else None,
        signal_threshold=max(0.0, float(getattr(cfg, "strategy13_min_edge", 0.0))),
        signal_delta=distance,
        signal_probability=best_probability,
        signal_edge=edge.best_edge,
        max_entry_price=getattr(cfg, "max_entry_price", None),
        ofi_score=ofi_score,
    )


def evaluate_strategy13_probability_edge(
    *,
    cfg: AppConfig,
    quote: MarketQuote,
    now: datetime,
    window: MarketWindow | None,
    state: SessionState,
    signal_open_up_price: float | None,
    signal_current_up_price: float | None,
) -> SideDecision:
    if window is None:
        return SideDecision(side=None, reason="strategy13_btc_anchor_unavailable")
    if is_binance_price_signal_stale(quote=quote, now=now, stale_seconds=cfg.binance_signal_stale_seconds):
        return SideDecision(
            side=None,
            reason="strategy13_btc_price_stale",
            signal_threshold=cfg.binance_signal_stale_seconds,
        )
    current_btc_price = getattr(quote, "binance_mid_price", None)
    if current_btc_price is None or current_btc_price <= 0:
        return SideDecision(side=None, reason="strategy13_btc_price_unavailable")
    if state.signal_round_slug != window.slug or state.strategy11_round_start_btc_price is None:
        price_to_beat = getattr(window, "price_to_beat", None)
        if price_to_beat is not None and price_to_beat > 0:
            state.strategy11_round_start_btc_price = float(price_to_beat)
        else:
            state.strategy11_round_start_btc_price = float(current_btc_price)

    edge = estimate_strategy13_probability_edge(
        cfg=cfg,
        quote=quote,
        window=window,
        now=now,
        round_start_btc_price=state.strategy11_round_start_btc_price,
    )
    if edge is None:
        return SideDecision(side=None, reason="strategy13_volatility_unavailable")

    best_probability = _strategy13_best_probability(edge)
    min_probability = max(0.5, min(0.99, float(getattr(cfg, "strategy13_min_probability", 0.58))))
    min_edge = max(0.0, float(getattr(cfg, "strategy13_min_edge", 0.0)))
    if edge.best_side is None or best_probability is None or best_probability < min_probability:
        return _strategy13_decision_from_edge(
            cfg=cfg,
            state=state,
            quote=quote,
            now=now,
            window=window,
            edge=edge,
            reason="strategy13_probability_too_low",
        )
    if edge.best_edge is None or edge.best_edge < min_edge:
        return _strategy13_decision_from_edge(
            cfg=cfg,
            state=state,
            quote=quote,
            now=now,
            window=window,
            edge=edge,
            reason="strategy13_edge_too_low",
        )

    ofi_score = resolve_strategy6_ofi_score(quote)
    if getattr(cfg, "strategy13_confirm_micro", True):
        micro_check = evaluate_strategy7_consensus_signal(
            cfg=cfg,
            quote=quote,
            now=now,
            signal_open_up_price=signal_open_up_price,
            signal_current_up_price=signal_current_up_price,
        )
        ofi_score = micro_check.ofi_score
        if micro_check.decision.side is None:
            return _strategy13_decision_from_edge(
                cfg=cfg,
                state=state,
                quote=quote,
                now=now,
                window=window,
                edge=edge,
                reason="strategy13_micro_unavailable"
                if micro_check.decision.reason in {None, "strategy7_ofi_unavailable", "strategy7_momentum_unavailable"}
                else "strategy13_micro_conflict",
                ofi_score=ofi_score,
            )
        if micro_check.decision.side != edge.best_side:
            return _strategy13_decision_from_edge(
                cfg=cfg,
                state=state,
                quote=quote,
                now=now,
                window=window,
                edge=edge,
                reason="strategy13_micro_conflict",
                ofi_score=ofi_score,
            )

    return _strategy13_decision_from_edge(
        cfg=cfg,
        state=state,
        quote=quote,
        now=now,
        window=window,
        edge=edge,
        reason=None,
        ofi_score=ofi_score,
    )
```

- [ ] **Step 4: Route strategy 13 in `resolve_side_from_strategy`**

In `resolve_side_from_strategy`, change:

```python
if cfg.strategy_id not in {5, 7, 8, 9, 10, 11, 12}:
```

to:

```python
if cfg.strategy_id not in {5, 7, 8, 9, 10, 11, 12, 13}:
```

In the locked-side branch, add a Strategy 13 branch similar to Strategy 11/12 before the shared `{7, 9, 10, 11}` branch:

```python
if cfg.strategy_id == 13:
    now = now or datetime.now(timezone.utc)
    edge_decision = evaluate_strategy13_probability_edge(
        cfg=cfg,
        quote=quote,
        now=now,
        window=window,
        state=state,
        signal_open_up_price=state.signal_round_open_up_price,
        signal_current_up_price=signal_current_up_price,
    )
    edge_decision.signal_locked = True
    if edge_decision.side is None:
        return edge_decision
    if edge_decision.side != state.signal_round_locked_side:
        edge_decision.side = None
        edge_decision.reason = "strategy13_signal_conflict"
        return edge_decision
    candidate_price = resolve_quote_price(state.signal_round_locked_side, quote)
    price_skip_reason = entry_price_skip_reason(
        strategy_prefix="strategy13",
        price=candidate_price,
        min_entry_price=getattr(cfg, "min_entry_price", None),
        max_entry_price=getattr(cfg, "max_entry_price", None),
    )
    if price_skip_reason is not None:
        edge_decision.side = None
        edge_decision.reason = price_skip_reason
        edge_decision.candidate_side = state.signal_round_locked_side
        edge_decision.candidate_price = candidate_price
        return edge_decision
    edge_decision.side = state.signal_round_locked_side
    edge_decision.candidate_side = state.signal_round_locked_side
    edge_decision.candidate_price = candidate_price
    return edge_decision
```

In the non-locked branch, add Strategy 13 before Strategy 7/8/9 handling:

```python
if cfg.strategy_id == 13:
    edge_decision = evaluate_strategy13_probability_edge(
        cfg=cfg,
        quote=quote,
        now=now,
        window=window,
        state=state,
        signal_open_up_price=signal_open_up_price,
        signal_current_up_price=signal_current_up_price,
    )
    state.strategy6_last_ofi_score = edge_decision.ofi_score
    if edge_decision.side is None:
        return edge_decision

    effective_confirm_before_entry_seconds = max(
        0,
        int(getattr(cfg, "strategy13_confirm_before_entry_seconds", 0)),
    )
    if (
        entry_time is not None
        and effective_confirm_before_entry_seconds > 0
        and (entry_time - now).total_seconds() < effective_confirm_before_entry_seconds
    ):
        edge_decision.side = None
        edge_decision.reason = "strategy13_entry_too_late"
        return edge_decision

    candidate_price = resolve_quote_price(edge_decision.side, quote)
    price_skip_reason = entry_price_skip_reason(
        strategy_prefix="strategy13",
        price=candidate_price,
        min_entry_price=getattr(cfg, "min_entry_price", None),
        max_entry_price=getattr(cfg, "max_entry_price", None),
    )
    if price_skip_reason is not None:
        edge_decision.side = None
        edge_decision.reason = price_skip_reason
        edge_decision.candidate_side = edge_decision.candidate_side or edge_decision.side
        edge_decision.candidate_price = candidate_price
        return edge_decision

    if entry_time is not None:
        lock_at = entry_time - timedelta(seconds=max(0, cfg.signal_lock_before_entry_seconds))
        if now >= lock_at:
            state.signal_round_locked_side = edge_decision.side
    edge_decision.signal_locked = state.signal_round_locked_side in {"UP", "DOWN"}
    edge_decision.candidate_side = edge_decision.side
    edge_decision.candidate_price = candidate_price
    return edge_decision
```

- [ ] **Step 5: Run Strategy 13R decision tests and verify they pass**

Run:

```powershell
pytest tests/test_strategy_decision.py -k "strategy13_" -q
```

Expected: PASS.

- [ ] **Step 6: Commit routing**

Run:

```powershell
git add strategy_decision.py tests/test_strategy_decision.py
git commit -m "feat: route strategy 13r decisions"
```

Expected: commit succeeds.

---

### Task 4: Paper Runtime Smoke Coverage

**Files:**
- Modify: `tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write failing paper runtime smoke test**

Add this test near existing multi-strategy paper runtime tests:

```python
def test_run_paper_trading_can_queue_strategy13_pending_trade(tmp_path, monkeypatch):
    now = datetime(2026, 4, 30, 1, 0, 4, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="btc-updown-5m-strategy13",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
        price_to_beat=100000.0,
        up_token_id="up-token",
        down_token_id="down-token",
    )
    cfg = AppConfig(
        strategy_id=13,
        paper_strategy_ids=[13],
        live_strategy_ids=[],
        trade_mode="paper",
        logs_dir=tmp_path / "logs",
        history_dir=tmp_path / "data",
        max_entry_price=0.54,
        open_delay_seconds=5,
        entry_grace_seconds=300,
        poll_interval_seconds=1,
        strategy13_min_edge=0.02,
        strategy13_edge_buffer=0.0,
        strategy13_vol_min_bps=8.0,
        strategy13_vol_max_bps=12.0,
        strategy13_probability_shrink=0.25,
        strategy13_min_probability=0.58,
        strategy13_confirm_micro=False,
        strategy13_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
    )
    quote = MarketQuote(
        slug=window.slug,
        up_price=0.52,
        up_best_ask=0.52,
        down_price=0.48,
        down_best_ask=0.48,
        binance_mid_price=100140.0,
        binance_signal_at=now,
        accepting_orders=True,
        fetched_at=now,
    )

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    class Strategy13PaperClient:
        def find_current_and_next_rounds(self, *, now):
            return window, None

        def get_market_by_slug(self, slug):
            assert slug == window.slug
            return {
                "slug": slug,
                "clobTokenIds": '["up-token", "down-token"]',
                "outcomes": '["Up", "Down"]',
                "acceptingOrders": True,
            }

        def quote_from_market(self, market):
            return quote

    state_path = tmp_path / "paper_state.json"
    log_path = tmp_path / "paper_trades.csv"
    monkeypatch.setattr(trader, "datetime", FixedDateTime)
    monkeypatch.setattr("trader._sleep_until_round_end", lambda *_args, **_kwargs: False)

    result = trader.run_paper_trading(
        cfg,
        client=Strategy13PaperClient(),
        state_path=state_path,
        log_path=log_path,
    )

    state = load_session_state(
        state_path,
        effective_paper_strategy_ids=[13],
    )
    pending = state.paper_strategies[13].pending_paper_trades
    assert result["status"] == "stopped"
    assert len(pending) == 1
    assert pending[0].strategy == 13
    assert pending[0].side == "UP"
    assert pending[0].signal_probability is not None
    assert pending[0].signal_edge is not None
```

- [ ] **Step 2: Run smoke test and identify any paper-runtime gap**

Run:

```powershell
pytest tests/test_trader_runtime_and_live.py -k "strategy13_pending_trade" -q
```

Expected: PASS after Tasks 1-3. A failure here identifies a paper-runtime integration gap to fix in Step 3.

- [ ] **Step 3: Fix only missing paper-runtime integration**

Make only the runtime correction exposed by Step 2:

- Strategy id filter rejection: include 13 in the same paper runtime strategy allowlist used for strategies 10-12.
- Missing signal diagnostics on queued pending trades: keep the existing `_signal_record_kwargs(side_decision)` path and ensure Strategy 13R populates `signal_probability` and `signal_edge` before `_queue_pending_paper_trade()` is called.

Do not add live-specific behavior in this task.

- [ ] **Step 4: Run smoke test again**

Run:

```powershell
pytest tests/test_trader_runtime_and_live.py -k "strategy13_pending_trade" -q
```

Expected: PASS.

- [ ] **Step 5: Commit runtime smoke coverage**

Run:

```powershell
git add tests/test_trader_runtime_and_live.py trader.py
git commit -m "test: cover strategy 13r paper runtime"
```

Expected: commit succeeds.

---

### Task 5: Full Verification

**Files:**
- No code changes unless verification exposes an issue.

- [ ] **Step 1: Run focused strategy/config tests**

Run:

```powershell
pytest tests/test_config_encoding.py -k "strategy13 or invalid_scalar" -q
pytest tests/test_strategy_decision.py -k "strategy13_ or strategy11 or strategy12" -q
pytest tests/test_trader_runtime_and_live.py -k "strategy13_pending_trade" -q
```

Expected: all selected tests PASS.

- [ ] **Step 2: Run broader regression tests for touched areas**

Run:

```powershell
pytest tests/test_config_encoding.py tests/test_strategy_decision.py tests/test_runtime_helpers.py -q
```

Expected: all tests PASS.

- [ ] **Step 3: Inspect git status**

Run:

```powershell
git status --short --branch
```

Expected: clean working tree except expected ahead commits.

- [ ] **Step 4: Report completion with evidence**

Summarize:

- config support for Strategy 13R
- probability-edge helper behavior
- strategy decision routing
- paper runtime smoke result
- exact test commands and pass/fail status
