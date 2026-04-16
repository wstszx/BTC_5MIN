# Paper Strategy Optimization Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a conservative paper-strategy optimization loop that finds candidate parameter sets offline, validates them with walk-forward analysis, evaluates them against the current champion in paper trading, and emits promotion recommendations without auto-switching live behavior.

**Architecture:** The implementation is split into four focused components: walk-forward windowing, offline candidate generation, paper challenger evaluation plus promotion policy, and runtime/dashboard integration. Each stage should produce useful artifacts independently, so the repository gains value before the full pipeline is complete.

**Tech Stack:** Python 3, dataclasses, CSV/JSON persistence, pytest, the existing `backtest.py` / `strategy_research.py` / `trader.py` / `dashboard.py` runtime stack.

---

## File Structure

- `walk_forward.py`
  - Create reusable rolling train/validation window logic.
- `optimizer.py`
  - Generate parameter candidates from the existing research/backtest stack.
- `paper_evaluator.py`
  - Read paper-trading results and compare champion vs challenger experiments.
- `promotion_policy.py`
  - Convert paper comparison metrics into recommendation states.
- `models.py`
  - Add new dataclasses for optimization artifacts if keeping shared typed objects in one place is clearer than ad hoc dicts.
- `config.py`
  - Add low-frequency optimization settings and defaults.
- `trader.py`
  - Support paper challengers identified by experiment id and parameter bundle, not only base strategy id.
- `dashboard.py`
  - Surface optimization state, challengers, and promotion recommendations.
- `tests/test_walk_forward.py`
  - New tests for window splitting and validation guards.
- `tests/test_optimizer.py`
  - New tests for candidate generation and ranking.
- `tests/test_paper_evaluator.py`
  - New tests for challenger-vs-champion comparison.
- `tests/test_promotion_policy.py`
  - New tests for state transitions and promotion rules.
- `tests/test_trader_runtime_and_live.py`
  - Extend paper runtime coverage for challenger state.
- `tests/test_dashboard.py`
  - Extend dashboard payload/UI coverage for optimization views.

## Shared Types To Introduce

Keep shared optimization types small and explicit:

- `WalkForwardWindow`
  - `train_start`
  - `train_end`
  - `validation_start`
  - `validation_end`
- `OptimizerCandidate`
  - `candidate_id`
  - `base_strategy_id`
  - `params`
  - `offline_score`
  - `validation_score`
  - `validation_passed`
- `PaperComparisonMetrics`
  - `champion_trade_count`
  - `challenger_trade_count`
  - `champion_total_pnl`
  - `challenger_total_pnl`
  - `champion_max_drawdown`
  - `challenger_max_drawdown`
  - `challenger_advantage`
- `PromotionDecision`
  - `state`
  - `reason`
  - `promotable`
  - `recommended_config`

These can live in `models.py` if you want one shared type module. If that file starts getting too broad, create `optimization_models.py` instead and update imports consistently.

### Task 1: Build Walk-Forward Validation

**Files:**
- Create: `walk_forward.py`
- Create: `tests/test_walk_forward.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_walk_forward.py
from walk_forward import WalkForwardWindow, build_walk_forward_windows


def test_build_walk_forward_windows_splits_rows_into_ordered_train_and_validation_ranges():
    rows = [{"start_time": f"2026-04-01T00:{minute:02d}:00Z"} for minute in range(12)]

    windows = build_walk_forward_windows(
        rows,
        train_size=6,
        validation_size=3,
        step_size=3,
    )

    assert windows == [
        WalkForwardWindow(train_start=0, train_end=6, validation_start=6, validation_end=9),
        WalkForwardWindow(train_start=3, train_end=9, validation_start=9, validation_end=12),
    ]


def test_build_walk_forward_windows_returns_empty_when_not_enough_rows():
    rows = [{"start_time": f"2026-04-01T00:{minute:02d}:00Z"} for minute in range(5)]

    windows = build_walk_forward_windows(
        rows,
        train_size=4,
        validation_size=3,
        step_size=2,
    )

    assert windows == []


def test_build_walk_forward_windows_rejects_non_positive_sizes():
    rows = [{"start_time": "2026-04-01T00:00:00Z"}]

    try:
        build_walk_forward_windows(rows, train_size=0, validation_size=1, step_size=1)
    except ValueError as exc:
        assert "train_size" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_walk_forward.py -v
```

Expected:

- `ModuleNotFoundError` for `walk_forward`, or missing symbol failures because the module does not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
# walk_forward.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Any


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    train_start: int
    train_end: int
    validation_start: int
    validation_end: int


def build_walk_forward_windows(
    rows: Sequence[dict[str, Any]],
    *,
    train_size: int,
    validation_size: int,
    step_size: int,
) -> list[WalkForwardWindow]:
    if train_size <= 0:
        raise ValueError("train_size must be > 0")
    if validation_size <= 0:
        raise ValueError("validation_size must be > 0")
    if step_size <= 0:
        raise ValueError("step_size must be > 0")

    total = len(rows)
    out: list[WalkForwardWindow] = []
    train_start = 0
    while True:
        train_end = train_start + train_size
        validation_start = train_end
        validation_end = validation_start + validation_size
        if validation_end > total:
            break
        out.append(
            WalkForwardWindow(
                train_start=train_start,
                train_end=train_end,
                validation_start=validation_start,
                validation_end=validation_end,
            )
        )
        train_start += step_size
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_walk_forward.py -v
```

Expected:

- `3 passed`

- [ ] **Step 5: Commit**

```bash
git add walk_forward.py tests/test_walk_forward.py
git commit -m "feat: add walk-forward window builder"
```

### Task 2: Build Offline Candidate Generation

**Files:**
- Create: `optimizer.py`
- Modify: `strategy_research.py`
- Create: `tests/test_optimizer.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_optimizer.py
from pathlib import Path

from config import AppConfig
from optimizer import build_candidate_configs, rank_optimizer_candidates


def test_build_candidate_configs_creates_strategy_specific_parameter_bundles():
    cfg = AppConfig()

    candidates = build_candidate_configs(
        cfg,
        strategy_ids=[5],
        target_profits=[0.8, 1.2],
        max_price_thresholds=[0.55],
        strategy5_thresholds=[0.012, 0.018],
    )

    assert len(candidates) == 4
    assert all(candidate.base_strategy_id == 5 for candidate in candidates)
    assert {candidate.params["TARGET_PROFIT"] for candidate in candidates} == {0.8, 1.2}
    assert {candidate.params["SIGNAL_MOMENTUM_THRESHOLD"] for candidate in candidates} == {0.012, 0.018}


def test_rank_optimizer_candidates_prefers_higher_validation_score_then_lower_drawdown():
    ranked = rank_optimizer_candidates(
        [
            {"candidate_id": "a", "validation_score": 0.6, "max_drawdown": 5.0, "total_pnl": 10.0},
            {"candidate_id": "b", "validation_score": 0.8, "max_drawdown": 7.0, "total_pnl": 9.0},
            {"candidate_id": "c", "validation_score": 0.8, "max_drawdown": 4.0, "total_pnl": 8.0},
        ]
    )

    assert [item["candidate_id"] for item in ranked] == ["c", "b", "a"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_optimizer.py -v
```

Expected:

- missing `optimizer.py`, or missing function failures.

- [ ] **Step 3: Write minimal implementation**

```python
# optimizer.py
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Iterable, Any

from config import AppConfig


@dataclass(frozen=True, slots=True)
class OptimizerCandidate:
    candidate_id: str
    base_strategy_id: int
    params: dict[str, float | int | str]


def build_candidate_configs(
    cfg: AppConfig,
    *,
    strategy_ids: Iterable[int],
    target_profits: Iterable[float],
    max_price_thresholds: Iterable[float],
    strategy5_thresholds: Iterable[float],
) -> list[OptimizerCandidate]:
    candidates: list[OptimizerCandidate] = []
    for strategy_id, target_profit, max_price_threshold in product(strategy_ids, target_profits, max_price_thresholds):
        if strategy_id == 5:
            for momentum_threshold in strategy5_thresholds:
                params = {
                    "TARGET_PROFIT": float(target_profit),
                    "MAX_PRICE_THRESHOLD": float(max_price_threshold),
                    "SIGNAL_MOMENTUM_THRESHOLD": float(momentum_threshold),
                }
                candidates.append(
                    OptimizerCandidate(
                        candidate_id=f"s{strategy_id}-tp{target_profit}-mp{max_price_threshold}-sm{momentum_threshold}",
                        base_strategy_id=strategy_id,
                        params=params,
                    )
                )
        else:
            params = {
                "TARGET_PROFIT": float(target_profit),
                "MAX_PRICE_THRESHOLD": float(max_price_threshold),
            }
            candidates.append(
                OptimizerCandidate(
                    candidate_id=f"s{strategy_id}-tp{target_profit}-mp{max_price_threshold}",
                    base_strategy_id=strategy_id,
                    params=params,
                )
            )
    return candidates


def rank_optimizer_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda item: (
            float(item.get("validation_score", 0.0)),
            -float(item.get("max_drawdown", 0.0)),
            float(item.get("total_pnl", 0.0)),
        ),
        reverse=True,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_optimizer.py -v
```

Expected:

- `2 passed`

- [ ] **Step 5: Commit**

```bash
git add optimizer.py tests/test_optimizer.py strategy_research.py
git commit -m "feat: add offline optimizer candidate generation"
```

### Task 3: Build Paper Evaluator And Promotion Policy

**Files:**
- Create: `paper_evaluator.py`
- Create: `promotion_policy.py`
- Create: `tests/test_paper_evaluator.py`
- Create: `tests/test_promotion_policy.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_paper_evaluator.py
from paper_evaluator import compare_paper_candidates


def test_compare_paper_candidates_summarizes_champion_and_challenger_rows():
    rows = [
        {"experiment_id": "champion", "trade_pnl": "1.0", "cash_pnl": "1.0"},
        {"experiment_id": "champion", "trade_pnl": "-0.5", "cash_pnl": "0.5"},
        {"experiment_id": "challenger-a", "trade_pnl": "1.5", "cash_pnl": "1.5"},
        {"experiment_id": "challenger-a", "trade_pnl": "0.5", "cash_pnl": "2.0"},
    ]

    metrics = compare_paper_candidates(rows, champion_id="champion", challenger_id="challenger-a")

    assert metrics.champion_trade_count == 2
    assert metrics.challenger_trade_count == 2
    assert metrics.champion_total_pnl == 0.5
    assert metrics.challenger_total_pnl == 2.0
    assert metrics.challenger_advantage == 1.5


# tests/test_promotion_policy.py
from promotion_policy import evaluate_promotion


def test_evaluate_promotion_marks_candidate_promotable_only_when_sample_and_drawdown_rules_pass():
    decision = evaluate_promotion(
        champion_trade_count=40,
        challenger_trade_count=42,
        champion_total_pnl=8.0,
        challenger_total_pnl=12.0,
        champion_max_drawdown=4.0,
        challenger_max_drawdown=4.5,
        min_trade_count=30,
        required_pnl_edge=2.0,
        max_drawdown_multiplier=1.25,
    )

    assert decision.state == "promotable"
    assert decision.promotable is True


def test_evaluate_promotion_rejects_candidate_with_too_few_trades():
    decision = evaluate_promotion(
        champion_trade_count=40,
        challenger_trade_count=8,
        champion_total_pnl=8.0,
        challenger_total_pnl=12.0,
        champion_max_drawdown=4.0,
        challenger_max_drawdown=3.0,
        min_trade_count=30,
        required_pnl_edge=2.0,
        max_drawdown_multiplier=1.25,
    )

    assert decision.state == "challenger"
    assert decision.promotable is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_paper_evaluator.py tests/test_promotion_policy.py -v
```

Expected:

- missing module failures.

- [ ] **Step 3: Write minimal implementation**

```python
# paper_evaluator.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PaperComparisonMetrics:
    champion_trade_count: int
    challenger_trade_count: int
    champion_total_pnl: float
    challenger_total_pnl: float
    champion_max_drawdown: float
    challenger_max_drawdown: float
    challenger_advantage: float


def _max_drawdown(cash_values: list[float]) -> float:
    peak = 0.0
    max_dd = 0.0
    for value in cash_values:
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)
    return max_dd


def compare_paper_candidates(rows: list[dict[str, str]], *, champion_id: str, challenger_id: str) -> PaperComparisonMetrics:
    champion = [row for row in rows if str(row.get("experiment_id")) == champion_id]
    challenger = [row for row in rows if str(row.get("experiment_id")) == challenger_id]
    champion_cash = [float(row.get("cash_pnl") or 0.0) for row in champion]
    challenger_cash = [float(row.get("cash_pnl") or 0.0) for row in challenger]
    champion_total = champion_cash[-1] if champion_cash else 0.0
    challenger_total = challenger_cash[-1] if challenger_cash else 0.0
    return PaperComparisonMetrics(
        champion_trade_count=len(champion),
        challenger_trade_count=len(challenger),
        champion_total_pnl=champion_total,
        challenger_total_pnl=challenger_total,
        champion_max_drawdown=_max_drawdown(champion_cash),
        challenger_max_drawdown=_max_drawdown(challenger_cash),
        challenger_advantage=challenger_total - champion_total,
    )


# promotion_policy.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    state: str
    reason: str
    promotable: bool


def evaluate_promotion(
    *,
    champion_trade_count: int,
    challenger_trade_count: int,
    champion_total_pnl: float,
    challenger_total_pnl: float,
    champion_max_drawdown: float,
    challenger_max_drawdown: float,
    min_trade_count: int,
    required_pnl_edge: float,
    max_drawdown_multiplier: float,
) -> PromotionDecision:
    if challenger_trade_count < min_trade_count:
        return PromotionDecision(state="challenger", reason="insufficient_trade_count", promotable=False)
    if challenger_total_pnl - champion_total_pnl < required_pnl_edge:
        return PromotionDecision(state="challenger", reason="insufficient_pnl_edge", promotable=False)
    if champion_max_drawdown > 0 and challenger_max_drawdown > champion_max_drawdown * max_drawdown_multiplier:
        return PromotionDecision(state="rejected", reason="drawdown_too_high", promotable=False)
    return PromotionDecision(state="promotable", reason="thresholds_passed", promotable=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_paper_evaluator.py tests/test_promotion_policy.py -v
```

Expected:

- all tests pass

- [ ] **Step 5: Commit**

```bash
git add paper_evaluator.py promotion_policy.py tests/test_paper_evaluator.py tests/test_promotion_policy.py
git commit -m "feat: add paper challenger evaluation and promotion policy"
```

### Task 4: Persist Challenger State And Integrate With Runtime

**Files:**
- Modify: `config.py`
- Modify: `trader.py`
- Modify: `models.py`
- Modify: `tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trader_runtime_and_live.py
def test_load_session_state_preserves_paper_experiment_ids(tmp_path):
    state_path = tmp_path / "session_state.json"
    state_path.write_text(
        json.dumps(
            {
                "paper_strategies": {
                    "5": {
                        "round_index": 10,
                        "cash_pnl": 2.0,
                        "recovery_loss": 0.0,
                        "consecutive_losses": 0,
                        "consecutive_max_stake_skips": 0,
                        "signal_round_slug": None,
                        "signal_round_open_up_price": None,
                        "signal_round_locked_side": None,
                        "strategy6_last_ofi_score": None,
                        "stop_loss_count": 0,
                        "daily_realized_pnl": 2.0,
                        "current_day": "2026-04-16",
                        "pending_paper_trades": [],
                        "experiment_id": "challenger-s5-a"
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    state = load_session_state(state_path, effective_paper_strategy_ids=[5])

    assert state.paper_strategies[5].experiment_id == "challenger-s5-a"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_trader_runtime_and_live.py::test_load_session_state_preserves_paper_experiment_ids -v
```

Expected:

- failure because `PaperStrategyState` has no `experiment_id`, or hydration drops the field.

- [ ] **Step 3: Write minimal implementation**

```python
# models.py
@dataclass(slots=True)
class PaperStrategyState:
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
    pending_paper_trades: list[PendingPaperTrade] = field(default_factory=list)
    experiment_id: str | None = None


# trader.py
def _session_state_to_paper_strategy_state(state: SessionState) -> PaperStrategyState:
    return PaperStrategyState(
        round_index=state.round_index,
        cash_pnl=state.cash_pnl,
        recovery_loss=state.recovery_loss,
        consecutive_losses=state.consecutive_losses,
        consecutive_max_stake_skips=state.consecutive_max_stake_skips,
        signal_round_slug=state.signal_round_slug,
        signal_round_open_up_price=state.signal_round_open_up_price,
        signal_round_locked_side=state.signal_round_locked_side,
        strategy6_last_ofi_score=state.strategy6_last_ofi_score,
        stop_loss_count=state.stop_loss_count,
        daily_realized_pnl=state.daily_realized_pnl,
        current_day=state.current_day,
        pending_paper_trades=list(state.pending_paper_trades),
        experiment_id=getattr(state, "experiment_id", None),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_trader_runtime_and_live.py::test_load_session_state_preserves_paper_experiment_ids -v
```

Expected:

- test passes and session hydration preserves experiment identity.

- [ ] **Step 5: Commit**

```bash
git add models.py trader.py tests/test_trader_runtime_and_live.py config.py
git commit -m "feat: add paper challenger runtime state"
```

### Task 5: Surface Optimization State In The Dashboard

**Files:**
- Modify: `dashboard.py`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dashboard.py
def test_dashboard_runtime_payload_includes_optimizer_status(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    try:
        payload = state.get_config_payload()
        runtime = payload["runtime_status"]
        assert "optimizer_enabled" in runtime
        assert "optimizer_last_run_at" in runtime
        assert "optimizer_promotable_count" in runtime
    finally:
        state.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_runtime_payload_includes_optimizer_status -v
```

Expected:

- failure because the dashboard runtime payload does not expose optimization status yet.

- [ ] **Step 3: Write minimal implementation**

```python
# dashboard.py
def _default_optimizer_runtime() -> dict[str, Any]:
    return {
        "enabled": False,
        "last_run_at": None,
        "champion_id": None,
        "active_challengers": [],
        "promotable_count": 0,
    }


class DashboardState:
    def _build_runtime_status(self, env_values: dict[str, str]) -> dict[str, Any]:
        optimizer_runtime = _default_optimizer_runtime()
        optimizer_state_path = self._cfg.logs_dir / "optimizer_state.json"
        if optimizer_state_path.exists():
            payload = json.loads(optimizer_state_path.read_text(encoding="utf-8"))
            optimizer_runtime["enabled"] = bool(payload.get("enabled", False))
            optimizer_runtime["last_run_at"] = payload.get("last_run_at")
            optimizer_runtime["champion_id"] = payload.get("champion_id")
            optimizer_runtime["active_challengers"] = payload.get("active_challengers") or []
            optimizer_runtime["promotable_count"] = len(payload.get("promotable_candidates") or [])

        return {
            "saved_mode": saved_mode,
            "running_mode": active_mode,
            "restart_required": saved_mode != active_mode,
            "live_ready": live_ready,
            "live_validation_error": live_validation_error,
            "active_mode": active_mode,
            "desired_mode": desired_mode,
            "switch_state": switch_state,
            "switch_reason": switch_reason,
            "current_round_slug": runtime_snapshot.current_round_slug if runtime_snapshot is not None else None,
            "round_in_progress": runtime_snapshot.round_in_progress if runtime_snapshot is not None else False,
            "safe_to_switch": runtime_snapshot.safe_to_switch if runtime_snapshot is not None else (saved_mode == active_mode),
            "pending_live_order": runtime_snapshot.pending_live_order if runtime_snapshot is not None else False,
            "optimizer_enabled": optimizer_runtime["enabled"],
            "optimizer_last_run_at": optimizer_runtime["last_run_at"],
            "optimizer_champion_id": optimizer_runtime["champion_id"],
            "optimizer_active_challengers": optimizer_runtime["active_challengers"],
            "optimizer_promotable_count": optimizer_runtime["promotable_count"],
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_dashboard.py::test_dashboard_runtime_payload_includes_optimizer_status -v
```

Expected:

- the runtime payload exposes optimizer metadata with safe defaults.

- [ ] **Step 5: Commit**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat: surface optimizer status in dashboard"
```

## Verification

Run the new focused optimization test suite:

```powershell
pytest tests/test_walk_forward.py tests/test_optimizer.py tests/test_paper_evaluator.py tests/test_promotion_policy.py -v
```

Run the runtime/dashboard regressions:

```powershell
pytest tests/test_dashboard.py tests/test_trader_runtime_and_live.py tests/test_runtime_manager.py tests/test_runtime_launcher.py -v
```

Run the existing research/backtest regressions:

```powershell
pytest tests/test_strategy_research.py tests/test_backtest.py tests/test_config_encoding.py tests/test_strategy.py -v
```

If all three commands pass, manually inspect:

- `logs/optimizer_state.json` shape is stable
- dashboard shows optimizer summary without blocking runtime
- paper challenger ids do not break existing strategy views

## Self-Review

- Spec coverage:
  - walk-forward validation is covered by Task 1
  - candidate generation is covered by Task 2
  - challenger evaluation and promotion policy are covered by Task 3
  - runtime identity/persistence is covered by Task 4
  - dashboard visibility is covered by Task 5
- Placeholder scan:
  - no `TBD`, `TODO`, or deferred implementation markers remain
  - each task includes concrete file paths, test names, commands, and code sketches
- Type consistency:
  - `WalkForwardWindow`, `OptimizerCandidate`, `PaperComparisonMetrics`, and `PromotionDecision` are introduced before downstream tasks rely on them
  - `experiment_id` is used consistently as the paper challenger identity name
