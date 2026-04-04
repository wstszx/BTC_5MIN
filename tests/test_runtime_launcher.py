from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

import main
from config import AppConfig, build_config_from_env_values, load_env_file_values


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


def test_main_without_explicit_argv_uses_sys_argv(monkeypatch):
    calls = {"count": 0}

    def fake_run(*, env_file, host, port):
        calls["count"] += 1
        return 0

    monkeypatch.setattr(main, "run_single_command_runtime", fake_run)
    monkeypatch.setattr(sys, "argv", ["main.py", "paper-trade"])

    with pytest.raises(SystemExit) as exc:
        main.main()

    assert exc.value.code == 2
    assert calls["count"] == 0


def test_main_rejects_legacy_subcommands():
    with pytest.raises(SystemExit) as exc:
        main.main(["paper-trade"])

    assert exc.value.code == 2


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


def test_run_single_command_runtime_loads_shared_config_for_startup_and_refresh(monkeypatch, tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    startup_cfg = object()
    refreshed_cfg = object()
    load_calls: list[Path] = []
    build_calls: list[dict[str, str]] = []
    payloads = [
        {"STRATEGY_ID": "2", "MAX_STAKE": "15.0"},
        {"STRATEGY_ID": "5", "MAX_STAKE": "9.5"},
    ]

    def fake_load_env(path: Path) -> dict[str, str]:
        load_calls.append(path)
        index = min(len(load_calls) - 1, len(payloads) - 1)
        return payloads[index]

    def fake_build_config(values: dict[str, str]):
        build_calls.append(dict(values))
        if values["STRATEGY_ID"] == "2":
            return startup_cfg
        return refreshed_cfg

    class FakeDashboardRuntime:
        def __init__(self) -> None:
            self.shutdown_calls = 0
            self.close_calls = 0

        def serve_forever(self) -> None:
            return

        def shutdown(self) -> None:
            self.shutdown_calls += 1

        def close(self) -> None:
            self.close_calls += 1

    dashboard_runtime = FakeDashboardRuntime()
    trader_calls = {}

    def fake_run_paper_trading(cfg, *, stop_event, config_provider):
        trader_calls["cfg"] = cfg
        trader_calls["provider_cfg"] = config_provider()
        trader_calls["stop_event"] = stop_event
        return {"status": "stopped"}

    monkeypatch.setattr(main, "load_env_file_values", fake_load_env)
    monkeypatch.setattr(main, "build_config_from_env_values", fake_build_config)
    monkeypatch.setattr(main, "create_dashboard_runtime", lambda **_: dashboard_runtime)
    monkeypatch.setattr(main, "run_paper_trading", fake_run_paper_trading)

    exit_code = main.run_single_command_runtime(env_file=env_file)

    assert exit_code == 0
    assert load_calls == [env_file, env_file]
    assert build_calls == payloads
    assert trader_calls["cfg"] is startup_cfg
    assert trader_calls["provider_cfg"] is refreshed_cfg
    assert dashboard_runtime.shutdown_calls >= 1
    assert dashboard_runtime.close_calls == 1


def test_run_single_command_runtime_keyboard_interrupt_triggers_coordinated_shutdown(monkeypatch):
    trader_stopped = threading.Event()
    stop_event_holder = {}

    class FakeDashboardRuntime:
        def __init__(self) -> None:
            self.closed = 0
            self.shutdown_calls = 0
            self._shutdown = threading.Event()

        def serve_forever(self) -> None:
            self._shutdown.wait(timeout=2)

        def shutdown(self) -> None:
            self.shutdown_calls += 1
            self._shutdown.set()

        def close(self) -> None:
            self.closed += 1
            self._shutdown.set()

    runtime = FakeDashboardRuntime()

    def fake_run_paper_trading(cfg, *, stop_event, config_provider):
        stop_event_holder["event"] = stop_event
        while not stop_event.is_set():
            time.sleep(0.01)
        trader_stopped.set()
        return {"status": "stopped"}

    def fake_wait_for_runtime_exit(*, stop_event, dashboard_thread, trader_thread):
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "load_env_file_values", lambda _: {})
    monkeypatch.setattr(main, "build_config_from_env_values", lambda _: object())
    monkeypatch.setattr(main, "create_dashboard_runtime", lambda **_: runtime)
    monkeypatch.setattr(main, "run_paper_trading", fake_run_paper_trading)
    monkeypatch.setattr(main, "_wait_for_runtime_exit", fake_wait_for_runtime_exit)

    exit_code = main.run_single_command_runtime()

    assert exit_code == 0
    assert runtime.shutdown_calls >= 1
    assert runtime.closed == 1
    assert stop_event_holder["event"].is_set()
    assert trader_stopped.wait(timeout=1)


def test_run_single_command_runtime_cleans_up_if_worker_crashes(monkeypatch):
    class FakeDashboardRuntime:
        def __init__(self) -> None:
            self.closed = 0
            self.shutdown_calls = 0

        def serve_forever(self) -> None:
            return

        def shutdown(self) -> None:
            self.shutdown_calls += 1

        def close(self) -> None:
            self.closed += 1

    runtime = FakeDashboardRuntime()

    def fake_run_paper_trading(cfg, *, stop_event, config_provider):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "load_env_file_values", lambda _: {})
    monkeypatch.setattr(main, "build_config_from_env_values", lambda _: object())
    monkeypatch.setattr(main, "create_dashboard_runtime", lambda **_: runtime)
    monkeypatch.setattr(main, "run_paper_trading", fake_run_paper_trading)

    exit_code = main.run_single_command_runtime()

    assert exit_code == 1
    assert runtime.shutdown_calls >= 1
    assert runtime.closed == 1
