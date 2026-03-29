# Polymarket BTC 5m Trading Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a directly runnable pure-Python Polymarket BTC 5 minute trading bot that supports historical export, backtesting, paper trading, and disabled-by-default live trading hooks.

**Architecture:** Keep the codebase flat and file-oriented. Put all Polymarket I/O in one API wrapper, share strategy and sizing logic across backtest and runtime, and persist lightweight CSV and JSON files under `data/` and `logs/`. The runtime loop should be a small explicit state machine with safe skip-first behavior.

**Tech Stack:** Python 3.11+, `requests`, `pandas`, `python-dateutil`, `pytest`, optional `websocket-client` and `tenacity`

---

## File Map

### Files to create

- `D:/pythonProject/BTC_5MIN/config.py`
- `D:/pythonProject/BTC_5MIN/models.py`
- `D:/pythonProject/BTC_5MIN/polymarket_api.py`
- `D:/pythonProject/BTC_5MIN/strategy.py`
- `D:/pythonProject/BTC_5MIN/risk_and_sizing.py`
- `D:/pythonProject/BTC_5MIN/backtest.py`
- `D:/pythonProject/BTC_5MIN/trader.py`
- `D:/pythonProject/BTC_5MIN/main.py`
- `D:/pythonProject/BTC_5MIN/requirements.txt`
- `D:/pythonProject/BTC_5MIN/README.md`
- `D:/pythonProject/BTC_5MIN/tests/test_strategy.py`
- `D:/pythonProject/BTC_5MIN/tests/test_risk_and_sizing.py`
- `D:/pythonProject/BTC_5MIN/tests/test_backtest.py`
- `D:/pythonProject/BTC_5MIN/tests/fixtures/sample_history.csv`
- `D:/pythonProject/BTC_5MIN/data/.gitkeep`
- `D:/pythonProject/BTC_5MIN/logs/.gitkeep`

### Responsibilities

- `config.py`: dataclasses and defaults for runtime settings
- `models.py`: canonical in-memory structures
- `polymarket_api.py`: official API integration, parsing, history export helpers
- `strategy.py`: rhythm-based side selection
- `risk_and_sizing.py`: sizing math, risk checks, PnL updates
- `backtest.py`: CSV simulation engine and summary metrics
- `trader.py`: polling runtime state machine, paper fills, state persistence
- `main.py`: CLI entry points and orchestration
- `README.md`: setup and usage
- `tests/*`: regression coverage for logic-heavy units

### Constraints to preserve

- The workspace is not currently a git repository, so commit steps are conditional.
- Live trading paths must remain disabled by default until credentials are intentionally added.
- Position sizing uses `order_size` first and derives `order_cost = order_size * price`.

## Task 1: Scaffold the project structure and configuration model

**Files:**
- Create: `D:/pythonProject/BTC_5MIN/config.py`
- Create: `D:/pythonProject/BTC_5MIN/models.py`
- Create: `D:/pythonProject/BTC_5MIN/data/.gitkeep`
- Create: `D:/pythonProject/BTC_5MIN/logs/.gitkeep`

- [ ] **Step 1: Write the failing configuration/model test**

```python
from config import AppConfig


def test_default_config_targets_btc_5m_series():
    cfg = AppConfig()
    assert cfg.series_id == 10684
    assert cfg.series_slug == "btc-up-or-down-5m"
    assert cfg.trade_mode == "paper"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_strategy.py -v`
Expected: FAIL with import error because `config.py` and `models.py` do not exist yet.

- [ ] **Step 3: Create the configuration and model files**

Implement:

- `AppConfig` dataclass with:
  - API base URLs
  - `series_id`
  - `series_slug`
  - strategy id
  - target profit
  - max consecutive losses
  - max stake
  - max price threshold
  - daily loss cap
  - polling interval
  - entry timing mode
  - timing offsets
  - history/log paths
  - live trading enabled flag defaulting to `False`
- model dataclasses:
  - `MarketWindow`
  - `MarketQuote`
  - `ResolvedRound`
  - `TradePlan`
  - `TradeRecord`
  - `SessionState`
  - `BacktestResult`

- [ ] **Step 4: Run the targeted test**

Run: `pytest tests/test_strategy.py -v`
Expected: PASS for the config bootstrap test.

- [ ] **Step 5: Checkpoint the scaffold**

If git is initialized later:

```bash
git add config.py models.py data/.gitkeep logs/.gitkeep tests/test_strategy.py
git commit -m "feat: add config and shared models"
```

If git is still unavailable, record the checkpoint in the session notes and continue.

## Task 2: Implement and test the rhythm strategy module

**Files:**
- Create: `D:/pythonProject/BTC_5MIN/strategy.py`
- Create: `D:/pythonProject/BTC_5MIN/tests/test_strategy.py`

- [ ] **Step 1: Write the failing strategy tests**

```python
import pytest

from strategy import get_side_for_round


@pytest.mark.parametrize(
    ("strategy_id", "expected"),
    [
        (1, ["UP", "DOWN", "UP", "DOWN", "UP", "DOWN"]),
        (2, ["UP", "UP", "DOWN", "DOWN", "UP", "UP"]),
        (3, ["UP", "UP", "UP", "DOWN", "DOWN", "DOWN"]),
        (4, ["UP", "UP", "UP", "UP", "DOWN", "DOWN"]),
    ],
)
def test_strategy_sequences(strategy_id, expected):
    actual = [get_side_for_round(strategy_id, idx) for idx in range(len(expected))]
    assert actual == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_strategy.py -v`
Expected: FAIL with `ModuleNotFoundError` for `strategy`.

- [ ] **Step 3: Implement the minimal strategy code**

Implement:

```python
def get_group_size(strategy_id: int) -> int:
    ...


def get_side_for_round(strategy_id: int, round_index: int) -> str:
    ...
```

Rules:

- strategy ids 1-4 map directly to group sizes 1-4
- `block_index = round_index // group_size`
- even block -> `UP`
- odd block -> `DOWN`
- reject invalid strategy ids with `ValueError`

- [ ] **Step 4: Run the strategy tests**

Run: `pytest tests/test_strategy.py -v`
Expected: PASS.

- [ ] **Step 5: Checkpoint the strategy logic**

If git is available:

```bash
git add strategy.py tests/test_strategy.py
git commit -m "feat: add rhythm strategy logic"
```

## Task 3: Implement and test sizing, risk checks, and settlement math

**Files:**
- Create: `D:/pythonProject/BTC_5MIN/risk_and_sizing.py`
- Create: `D:/pythonProject/BTC_5MIN/tests/test_risk_and_sizing.py`

- [ ] **Step 1: Write the failing risk and sizing tests**

```python
from risk_and_sizing import (
    apply_round_outcome,
    build_trade_plan,
)
from models import SessionState


def test_build_trade_plan_without_loss_uses_target_profit_formula():
    state = SessionState()
    plan = build_trade_plan(
        state=state,
        side="UP",
        price=0.5,
        target_profit=0.5,
        max_price_threshold=0.65,
        max_stake=10,
        daily_loss_cap=20,
        max_consecutive_losses=8,
    )
    assert round(plan.order_size, 4) == 1.0
    assert round(plan.order_cost, 4) == 0.5


def test_apply_round_outcome_loss_updates_recovery_pool():
    state = SessionState()
    plan = build_trade_plan(
        state=state,
        side="DOWN",
        price=0.5,
        target_profit=0.5,
        max_price_threshold=0.65,
        max_stake=10,
        daily_loss_cap=20,
        max_consecutive_losses=8,
    )
    updated = apply_round_outcome(state, plan, won=False)
    assert round(updated.recovery_loss, 4) == 0.5
    assert updated.consecutive_losses == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_risk_and_sizing.py -v`
Expected: FAIL because `risk_and_sizing.py` is missing.

- [ ] **Step 3: Implement the sizing and risk module**

Implement:

- `build_trade_plan(...)`
- `apply_round_outcome(...)`
- helper functions:
  - `compute_order_size(...)`
  - `compute_order_cost(...)`
  - `should_stop_for_daily_loss(...)`
  - `should_reset_after_max_losses(...)`
  - `validate_price(...)`

Behavior to encode:

- `order_size = (recovery_loss + target_profit) / (1 - price)` when `recovery_loss > 0`
- `order_size = target_profit / (1 - price)` otherwise
- skip reasons:
  - invalid price
  - price above threshold
  - daily loss cap reached
  - max consecutive losses reached
  - order cost above max stake
- win settlement uses `order_size * (1 - price)`
- loss settlement uses `order_cost`

- [ ] **Step 4: Expand tests for edge branches**

Add tests for:

- price above threshold -> skip
- `order_cost > max_stake` -> skip
- max consecutive losses -> stop-loss reset
- daily loss cap -> no trade
- recovery-loss case with `price=0.62`

- [ ] **Step 5: Run the risk test suite**

Run: `pytest tests/test_risk_and_sizing.py -v`
Expected: PASS.

- [ ] **Step 6: Checkpoint the sizing/risk logic**

If git is available:

```bash
git add risk_and_sizing.py tests/test_risk_and_sizing.py
git commit -m "feat: add risk management and sizing"
```

## Task 4: Build the official Polymarket API wrapper and parsers

**Files:**
- Create: `D:/pythonProject/BTC_5MIN/polymarket_api.py`
- Modify: `D:/pythonProject/BTC_5MIN/models.py`

- [ ] **Step 1: Write a small parser-focused test**

```python
from polymarket_api import parse_outcome_prices


def test_parse_outcome_prices_maps_up_and_down():
    parsed = parse_outcome_prices('["0.555", "0.445"]', '["Up", "Down"]')
    assert parsed["UP"] == 0.555
    assert parsed["DOWN"] == 0.445
```

- [ ] **Step 2: Run the parser test to verify it fails**

Run: `pytest tests/test_backtest.py::test_parse_outcome_prices_maps_up_and_down -v`
Expected: FAIL because `polymarket_api.py` is missing.

- [ ] **Step 3: Implement the API client and parsing helpers**

Implement:

- `PolymarketClient` with methods:
  - `list_series_events(...)`
  - `get_event_by_slug(...)`
  - `get_market_by_slug(...)`
  - `get_price_history(...)`
  - `find_current_and_next_rounds(...)`
  - `export_history(...)`
- parsing helpers:
  - `parse_json_list_field(...)`
  - `parse_outcome_prices(...)`
  - `extract_token_ids(...)`
  - `build_resolved_round(...)`
  - `nearest_price_from_history(...)`

Implementation notes:

- use official gamma and clob endpoints only
- locally filter event lists if server filtering is noisy
- normalize output labels to `UP` and `DOWN`
- use `eventMetadata.priceToBeat` and `eventMetadata.finalPrice` for resolved rounds

- [ ] **Step 4: Add retry-safe HTTP error handling**

Implement minimal retry or bounded retry loops for transient HTTP failures and JSON parsing errors, returning warnings and skip-safe results instead of hard crashes.

- [ ] **Step 5: Run parser and smoke tests**

Run: `pytest tests/test_backtest.py::test_parse_outcome_prices_maps_up_and_down -v`
Expected: PASS.

- [ ] **Step 6: Checkpoint the API layer**

If git is available:

```bash
git add polymarket_api.py models.py tests/test_backtest.py
git commit -m "feat: add polymarket api client"
```

## Task 5: Implement historical export and backtest engine with fixture coverage

**Files:**
- Create: `D:/pythonProject/BTC_5MIN/backtest.py`
- Create: `D:/pythonProject/BTC_5MIN/tests/test_backtest.py`
- Create: `D:/pythonProject/BTC_5MIN/tests/fixtures/sample_history.csv`

- [ ] **Step 1: Write the failing backtest fixture and test**

Create `sample_history.csv` with a few rounds that exercise:

- win
- loss
- skipped round due to high price
- stop-loss reset path

Write test:

```python
from pathlib import Path

from backtest import run_backtest
from config import AppConfig


def test_backtest_returns_summary_metrics():
    cfg = AppConfig()
    result = run_backtest(Path("tests/fixtures/sample_history.csv"), cfg)
    assert result.trade_count > 0
    assert result.max_drawdown >= 0
    assert result.max_consecutive_losses >= 0
```

- [ ] **Step 2: Run the backtest test to verify it fails**

Run: `pytest tests/test_backtest.py -v`
Expected: FAIL because `backtest.py` is missing.

- [ ] **Step 3: Implement the backtest engine**

Implement:

- CSV loader
- entry-price selector for `OPEN` and `PRE_CLOSE`
- per-round simulation using:
  - `get_side_for_round`
  - `build_trade_plan`
  - `apply_round_outcome`
- summary metric computation:
  - total PnL
  - average PnL per round
  - max consecutive losses
  - stop-loss count
  - max drawdown
  - skipped rounds

- [ ] **Step 4: Add parser test coverage to this file**

Include the `parse_outcome_prices` unit test here so the API parsing and backtest fixture share one small, focused test module.

- [ ] **Step 5: Run the backtest suite**

Run: `pytest tests/test_backtest.py -v`
Expected: PASS.

- [ ] **Step 6: Checkpoint the backtest feature**

If git is available:

```bash
git add backtest.py tests/test_backtest.py tests/fixtures/sample_history.csv
git commit -m "feat: add history backtest engine"
```

## Task 6: Build the paper-trading runtime state machine and persistence

**Files:**
- Create: `D:/pythonProject/BTC_5MIN/trader.py`
- Modify: `D:/pythonProject/BTC_5MIN/models.py`
- Modify: `D:/pythonProject/BTC_5MIN/polymarket_api.py`

- [ ] **Step 1: Write a small state persistence test**

```python
from pathlib import Path

from trader import load_session_state, save_session_state
from models import SessionState


def test_session_state_round_trip(tmp_path: Path):
    state = SessionState(round_index=3, recovery_loss=1.25)
    path = tmp_path / "session_state.json"
    save_session_state(path, state)
    restored = load_session_state(path)
    assert restored.round_index == 3
    assert restored.recovery_loss == 1.25
```

- [ ] **Step 2: Run the persistence test to verify it fails**

Run: `pytest tests/test_risk_and_sizing.py::test_session_state_round_trip -v`
Expected: FAIL because `trader.py` does not exist yet.

- [ ] **Step 3: Implement the runtime loop**

Implement:

- session state load/save helpers
- trade log CSV append helper
- polling loop states:
  - `DISCOVER`
  - `WAIT_ENTRY`
  - `EVALUATE`
  - `PLACE_ORDER`
  - `WAIT_RESOLUTION`
  - `SETTLE`
  - `ADVANCE`
- paper fill behavior only:
  - capture side
  - capture price
  - capture `order_size` and `order_cost`
  - wait for official resolution
  - settle into `SessionState`

- [ ] **Step 4: Keep live trading disabled by default**

Add a placeholder function such as:

```python
def place_live_order(*args, **kwargs):
    raise RuntimeError("Live trading is disabled until credentials are configured.")
```

- [ ] **Step 5: Run focused tests**

Run: `pytest tests/test_risk_and_sizing.py -v`
Expected: PASS with the new persistence test added.

- [ ] **Step 6: Checkpoint the runtime loop**

If git is available:

```bash
git add trader.py models.py tests/test_risk_and_sizing.py
git commit -m "feat: add paper trading runtime"
```

## Task 7: Wire the CLI and runtime modes together

**Files:**
- Create: `D:/pythonProject/BTC_5MIN/main.py`
- Modify: `D:/pythonProject/BTC_5MIN/backtest.py`
- Modify: `D:/pythonProject/BTC_5MIN/polymarket_api.py`
- Modify: `D:/pythonProject/BTC_5MIN/trader.py`

- [ ] **Step 1: Write a simple CLI dispatch test**

```python
from main import build_parser


def test_cli_exposes_expected_commands():
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert {"fetch-history", "backtest", "paper-trade", "live-trade"} <= set(choices)
```

- [ ] **Step 2: Run the CLI test to verify it fails**

Run: `pytest tests/test_strategy.py::test_cli_exposes_expected_commands -v`
Expected: FAIL because `main.py` does not exist yet.

- [ ] **Step 3: Implement the CLI parser and command dispatch**

Implement:

- `build_parser()`
- `main()`
- command handlers:
  - fetch history to CSV
  - run backtest and print summary
  - start paper trading loop
  - refuse live trading unless explicitly enabled

- [ ] **Step 4: Add basic console output formatting**

Print concise summaries for:

- export file path
- backtest metrics
- runtime mode startup
- live trading disabled warning

- [ ] **Step 5: Run the CLI test**

Run: `pytest tests/test_strategy.py::test_cli_exposes_expected_commands -v`
Expected: PASS.

- [ ] **Step 6: Checkpoint the CLI**

If git is available:

```bash
git add main.py backtest.py trader.py polymarket_api.py tests/test_strategy.py
git commit -m "feat: add cli entrypoints"
```

## Task 8: Add packaging metadata, README, and end-to-end verification

**Files:**
- Create: `D:/pythonProject/BTC_5MIN/requirements.txt`
- Create: `D:/pythonProject/BTC_5MIN/README.md`

- [ ] **Step 1: Write `requirements.txt`**

Include:

```text
requests
pandas
python-dateutil
pytest
websocket-client
tenacity
```

Mark `websocket-client` and `tenacity` in README as optional-at-runtime helpers if they are not used immediately in code.

- [ ] **Step 2: Write `README.md`**

Cover:

- what the bot does
- current safety scope
- install instructions
- configuration file overview
- CLI commands
- history export usage
- backtest usage
- paper trading usage
- live trading disabled-by-default note

- [ ] **Step 3: Run the full test suite**

Run: `pytest -v`
Expected: PASS for all unit tests.

- [ ] **Step 4: Run a CLI smoke test**

Run: `python main.py --help`
Expected: usage text showing all four commands.

- [ ] **Step 5: Run one backtest smoke test**

Run: `python main.py backtest --csv tests/fixtures/sample_history.csv`
Expected: a printed summary containing total PnL and max drawdown.

- [ ] **Step 6: Run one history-export smoke test**

Run: `python main.py fetch-history --limit 3`
Expected: a CSV written under `data/` with BTC 5m rows.

- [ ] **Step 7: Checkpoint docs and verification**

If git is available:

```bash
git add requirements.txt README.md
git commit -m "docs: add usage guide and dependencies"
```

## Verification Checklist

- [ ] `pytest -v`
- [ ] `python main.py --help`
- [ ] `python main.py backtest --csv tests/fixtures/sample_history.csv`
- [ ] `python main.py fetch-history --limit 3`
- [ ] `python main.py paper-trade --dry-run-once`

## Notes for the Implementer

- Keep Polymarket endpoint usage isolated to `polymarket_api.py`.
- Do not let live-trading credential requirements leak into paper mode.
- Prefer explicit small helpers over large monolithic functions.
- Reuse shared logic; do not duplicate sizing logic between runtime and backtest.
- If a required external response shape differs from the spec, update parsing centrally and adjust the fixture tests first.
