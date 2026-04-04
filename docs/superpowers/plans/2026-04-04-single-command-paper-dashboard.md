# Single-Command Paper Trading Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the public multi-command CLI with one supported entry, `python main.py`, that starts the local dashboard and continuous paper trading together, uses `.env.dashboard` as the shared runtime config source, and shuts both services down cleanly with `Ctrl+C`.

**Architecture:** Keep trading logic in `trader.py` and HTTP behavior in `dashboard.py`, but add explicit lifecycle hooks so a thin launcher can coordinate startup, runtime config loading, and shutdown. Move shared runtime-config loading out of dashboard-only code, add stop-aware worker APIs, then wire `main.py` to the new coordinator while shrinking docs to one operator workflow.

**Tech Stack:** Python 3.10, stdlib threading/http server, pytest, existing Polymarket client/runtime modules

---

## File Structure

**Files:**
- Create: `tests/test_runtime_launcher.py`
  - launcher-focused tests for default entry behavior, shared config loading, and coordinated shutdown
- Modify: `config.py`
  - add reusable helpers for loading `.env.dashboard` values and building `AppConfig` from those values
- Modify: `dashboard.py`
  - replace the current only-blocking server entry with a reusable closeable runtime/server wrapper
  - reuse shared config helpers instead of dashboard-private env parsing
- Modify: `trader.py`
  - add stop-aware paper-trading loop support
  - add safe config refresh support between polling cycles/rounds
- Modify: `main.py`
  - remove public subcommand parsing from the normal entry path
  - delegate to a single runtime launcher path
- Modify: `tests/test_dashboard.py`
  - cover dashboard server lifecycle and shared config-source behavior
- Modify: `tests/test_trader_runtime_and_live.py`
  - cover stop events and runtime config refresh boundaries
- Modify: `README.md`
  - reduce usage docs to install/config/run/open dashboard/stop
- Modify: `docs/operations_runbook.md`
  - rewrite operator flow around `python main.py`
- Modify: `docs/dashboard_runbook.md`
  - align dashboard instructions with the combined runtime
- Reference only: `docs/superpowers/specs/2026-04-04-single-command-paper-dashboard-design.md`

### Task 1: Extract Shared Runtime Config Loading

**Files:**
- Modify: `config.py`
- Modify: `dashboard.py`
- Create: `tests/test_runtime_launcher.py`

- [ ] **Step 1: Write the failing config-loading tests**

Add tests that lock in `.env.dashboard` as the shared runtime config source:

```python
from pathlib import Path

from config import AppConfig, build_config_from_env_values, load_env_file_values


def test_load_env_file_values_reads_simple_key_value_pairs(tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    env_file.write_text("STRATEGY_ID=5\nMAX_STAKE=9.5\n", encoding="utf-8")

    values = load_env_file_values(env_file)

    assert values == {"STRATEGY_ID": "5", "MAX_STAKE": "9.5"}


def test_build_config_from_env_values_applies_dashboard_values():
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "5",
            "MAX_STAKE": "9.5",
            "TARGET_PROFIT": "0.8",
        }
    )

    assert isinstance(cfg, AppConfig)
    assert cfg.strategy_id == 5
    assert cfg.max_stake == 9.5
    assert cfg.target_profit == 0.8
```

- [ ] **Step 2: Run the focused config tests to verify they fail**

Run:

```bash
pytest tests/test_runtime_launcher.py -k "load_env_file_values or build_config_from_env_values" -q
```

Expected: FAIL because the shared config helpers do not exist yet.

- [ ] **Step 3: Implement reusable env-file helpers in `config.py`**

Add small public helpers instead of leaving this logic dashboard-private:

```python
from contextlib import contextmanager


def load_env_file_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip()
    return values


@contextmanager
def patched_env(overrides: dict[str, str]):
    ...


def build_config_from_env_values(values: dict[str, str]) -> AppConfig:
    with patched_env(values):
        return AppConfig()
```

Update `dashboard.py` to import and use these helpers instead of maintaining a separate private env-file implementation for runtime config.

- [ ] **Step 4: Run the focused config tests to verify they pass**

Run:

```bash
pytest tests/test_runtime_launcher.py -k "load_env_file_values or build_config_from_env_values" -q
```

Expected: PASS with both helper tests green.

- [ ] **Step 5: Commit the shared config-source extraction**

```bash
git add config.py dashboard.py tests/test_runtime_launcher.py
git commit -m "refactor: share runtime config loading helpers"
```

### Task 2: Make Paper Trading Stop-Aware And Config-Refreshable

**Files:**
- Modify: `trader.py`
- Modify: `tests/test_trader_runtime_and_live.py`

- [ ] **Step 1: Write the failing paper-runtime control tests**

Add tests that prove the paper loop can stop cleanly and refresh config safely:

```python
import threading

from config import AppConfig
from trader import run_paper_trading


class _NoMarketClient:
    def find_current_and_next_rounds(self, *, now):
        return None, None


def test_run_paper_trading_stops_when_stop_event_is_set(tmp_path, monkeypatch):
    stop_event = threading.Event()
    sleep_calls = {"count": 0}

    def fake_sleep(_seconds):
        sleep_calls["count"] += 1
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)

    result = run_paper_trading(
        AppConfig(poll_interval_seconds=1),
        client=_NoMarketClient(),
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "paper.csv",
        stop_event=stop_event,
    )

    assert result["status"] == "stopped"
    assert sleep_calls["count"] == 1


def test_run_paper_trading_uses_config_provider_between_cycles(tmp_path, monkeypatch):
    seen = []

    class _CyclingClient:
        def find_current_and_next_rounds(self, *, now):
            seen.append("tick")
            raise KeyboardInterrupt

    def config_provider():
        return AppConfig(strategy_id=5, poll_interval_seconds=1)

    with pytest.raises(KeyboardInterrupt):
        run_paper_trading(
            AppConfig(strategy_id=2, poll_interval_seconds=1),
            client=_CyclingClient(),
            state_path=tmp_path / "state.json",
            log_path=tmp_path / "paper.csv",
            config_provider=config_provider,
        )

    # The implementation detail to assert after wiring:
    # run_paper_trading should replace the active cfg from config_provider
```

Refine the second assertion to the exact observable chosen during implementation, for example by checking a returned field, a captured runtime log string, or a helper extracted for easier unit testing.

- [ ] **Step 2: Run the new paper-runtime tests to verify they fail**

Run:

```bash
pytest tests/test_trader_runtime_and_live.py -k "stop_event or config_provider" -q
```

Expected: FAIL because `run_paper_trading()` does not yet accept `stop_event` or `config_provider`.

- [ ] **Step 3: Add stop and config-refresh hooks to `run_paper_trading()`**

Extend the runtime signature carefully:

```python
from collections.abc import Callable
import threading


def run_paper_trading(
    cfg: AppConfig | None = None,
    *,
    client: PolymarketClient | None = None,
    state_path: Path | None = None,
    log_path: Path | None = None,
    dry_run_once: bool = False,
    stop_event: threading.Event | None = None,
    config_provider: Callable[[], AppConfig] | None = None,
) -> dict[str, Any]:
    ...
```

Implementation rules:

- check `stop_event.is_set()` at the top of each loop and before long sleeps
- return `{"status": "stopped"}` on coordinated shutdown instead of raising
- if `config_provider` is present, refresh `cfg` at a safe loop boundary before evaluating the next round
- never mutate session state just because config refresh happened

- [ ] **Step 4: Run the focused paper-runtime tests to verify they pass**

Run:

```bash
pytest tests/test_trader_runtime_and_live.py -k "stop_event or config_provider" -q
```

Expected: PASS with the new runtime control hooks covered.

- [ ] **Step 5: Run the full trader runtime regression**

Run:

```bash
pytest tests/test_trader_runtime_and_live.py -q
```

Expected: PASS with no regression to the existing live and paper-runtime tests.

- [ ] **Step 6: Commit the stoppable paper-trading runtime**

```bash
git add trader.py tests/test_trader_runtime_and_live.py
git commit -m "feat: add stoppable paper trading runtime"
```

### Task 3: Add A Closeable Dashboard Runtime Wrapper

**Files:**
- Modify: `dashboard.py`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing dashboard lifecycle tests**

Add tests for a reusable dashboard server wrapper instead of only `serve_forever()`:

```python
import threading
from pathlib import Path

from dashboard import create_dashboard_runtime


def test_create_dashboard_runtime_uses_requested_env_file(tmp_path: Path):
    runtime = create_dashboard_runtime(host="127.0.0.1", port=0, env_file=tmp_path / ".env.dashboard")
    try:
        assert runtime.state.env_file == tmp_path / ".env.dashboard"
        assert runtime.server.server_address[0] == "127.0.0.1"
    finally:
        runtime.close()


def test_dashboard_runtime_can_shutdown_cleanly(tmp_path: Path):
    runtime = create_dashboard_runtime(host="127.0.0.1", port=0, env_file=tmp_path / ".env.dashboard")
    thread = threading.Thread(target=runtime.serve_forever)
    thread.start()
    runtime.shutdown()
    thread.join(timeout=2)
    assert not thread.is_alive()
    runtime.close()
```

- [ ] **Step 2: Run the dashboard lifecycle tests to verify they fail**

Run:

```bash
pytest tests/test_dashboard.py -k "create_dashboard_runtime or shutdown_cleanly" -q
```

Expected: FAIL because no reusable closeable runtime wrapper exists yet.

- [ ] **Step 3: Implement `create_dashboard_runtime()` and reuse it in `run_dashboard()`**

Introduce a thin wrapper object:

```python
from dataclasses import dataclass


@dataclass
class DashboardRuntime:
    server: ThreadingHTTPServer
    state: DashboardState

    def serve_forever(self) -> None:
        self.server.serve_forever()

    def shutdown(self) -> None:
        self.server.shutdown()

    def close(self) -> None:
        self.server.server_close()
        self.state.close()


def create_dashboard_runtime(*, host: str = "127.0.0.1", port: int = 8787, env_file: Path = Path(".env.dashboard")) -> DashboardRuntime:
    ...
```

Then rewrite `run_dashboard()` as a thin blocking convenience wrapper around this object so existing internals stay simple.

- [ ] **Step 4: Run the dashboard lifecycle tests to verify they pass**

Run:

```bash
pytest tests/test_dashboard.py -k "create_dashboard_runtime or shutdown_cleanly" -q
```

Expected: PASS with clean startup/shutdown behavior on an ephemeral test port.

- [ ] **Step 5: Run the full dashboard test suite**

Run:

```bash
pytest tests/test_dashboard.py -q
```

Expected: PASS with existing config/help-center tests still green.

- [ ] **Step 6: Commit the dashboard runtime wrapper**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat: add closeable dashboard runtime"
```

### Task 4: Build The Single-Command Launcher And Replace The Public CLI

**Files:**
- Modify: `main.py`
- Create: `tests/test_runtime_launcher.py`

- [ ] **Step 1: Write the failing launcher tests**

Add tests that lock in the new public behavior:

```python
from pathlib import Path

import main


def test_main_without_args_starts_single_command_runtime(monkeypatch, tmp_path: Path):
    calls = {}

    def fake_run(*, env_file, host, port):
        calls["env_file"] = env_file
        calls["host"] = host
        calls["port"] = port
        return 0

    monkeypatch.setattr(main, "run_single_command_runtime", fake_run)

    exit_code = main.main([])

    assert exit_code == 0
    assert calls["env_file"] == Path(".env.dashboard")
    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 8787


def test_main_rejects_legacy_subcommands(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        main.main(["paper-trade"])

    assert exc.value.code == 2
```

Also add launcher-unit tests for coordinated shutdown with injected workers:

```python
def test_launcher_shuts_down_dashboard_when_trader_start_fails(...):
    ...
```

Use dependency injection instead of real threads where possible so the coordinator can be tested deterministically.

- [ ] **Step 2: Run the launcher tests to verify they fail**

Run:

```bash
pytest tests/test_runtime_launcher.py -q
```

Expected: FAIL because `main.main([])` still prints CLI help and there is no `run_single_command_runtime()` path.

- [ ] **Step 3: Implement a thin single-command runtime coordinator**

Keep orchestration out of the trading/dashboard modules. The minimal launcher surface should look like:

```python
def run_single_command_runtime(
    *,
    env_file: Path = Path(".env.dashboard"),
    host: str = "127.0.0.1",
    port: int = 8787,
) -> int:
    ...


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv or [])
    if args:
        raise SystemExit(2)
    return run_single_command_runtime()
```

Coordinator rules:

- load `.env.dashboard` via the shared config helpers
- start dashboard and paper-trading workers with the same effective config source
- print a compact startup banner with the fixed URL
- on `KeyboardInterrupt`, signal trader stop, shut down dashboard, join workers, and return `0`
- if startup partially fails, stop anything already started and return nonzero

- [ ] **Step 4: Run the launcher tests to verify they pass**

Run:

```bash
pytest tests/test_runtime_launcher.py -q
```

Expected: PASS with the single-command public entry locked in.

- [ ] **Step 5: Do a focused end-to-end smoke test from the terminal**

Run:

```bash
python main.py
```

Expected:

- terminal prints that paper trading and dashboard have started
- dashboard is reachable at `http://127.0.0.1:8787/`
- `Ctrl+C` stops the runtime cleanly

Then verify the port is no longer actively served:

```powershell
Get-NetTCPConnection -LocalPort 8787 -ErrorAction SilentlyContinue
```

Expected: no remaining listening entry for the stopped process.

- [ ] **Step 6: Commit the single-command launcher**

```bash
git add main.py tests/test_runtime_launcher.py
git commit -m "feat: launch dashboard and paper trading together"
```

### Task 5: Rewrite Operator Docs Around One Workflow

**Files:**
- Modify: `README.md`
- Modify: `docs/operations_runbook.md`
- Modify: `docs/dashboard_runbook.md`

- [ ] **Step 1: Write the docs edits**

Refocus all operator-facing docs around the one supported flow:

- install dependencies
- adjust `.env.dashboard` or dashboard parameters
- run `python main.py`
- open `http://127.0.0.1:8787/`
- stop with `Ctrl+C`

Concrete edits:

- remove the main command catalog from `README.md`
- replace separate `paper-trade` and `dashboard` startup sections with one combined runtime section
- update stale workspace paths from `D:\python\BTC_5MIN` to `D:\pythonProject\BTC_5MIN`
- de-emphasize old commands if they must remain documented internally

- [ ] **Step 2: Review the docs diff manually**

Run:

```bash
git diff -- README.md docs/operations_runbook.md docs/dashboard_runbook.md
```

Expected: the docs now teach one primary operator path instead of many unrelated modes.

- [ ] **Step 3: Run the full regression suite**

Run:

```bash
pytest -q
```

Expected: PASS with runtime, dashboard, and documentation-adjacent code changes all green.

- [ ] **Step 4: Commit the documentation rewrite**

```bash
git add README.md docs/operations_runbook.md docs/dashboard_runbook.md
git commit -m "docs: simplify runtime instructions to one command"
```

### Task 6: Final Verification And Cleanup Pass

**Files:**
- Modify only if follow-up fixes are required after verification

- [ ] **Step 1: Run a final status check**

Run:

```bash
git status --short
```

Expected: clean working tree after the planned commits above, or only intentional follow-up edits.

- [ ] **Step 2: Re-run the highest-signal checks**

Run:

```bash
pytest tests/test_runtime_launcher.py tests/test_dashboard.py tests/test_trader_runtime_and_live.py -q
python main.py
```

Expected: tests pass, runtime starts, dashboard opens, and `Ctrl+C` shuts everything down cleanly.

- [ ] **Step 3: Summarize any deliberate non-goals left in the codebase**

Capture in the handoff summary:

- legacy internal modules still exist but are no longer the public operator interface
- live trading and analysis helpers remain untouched unless required by shared refactors
- deeper deletion/cleanup can happen in a later focused task after the single-command flow proves stable
