from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

import main
from config import AppConfig, build_config_from_env_values, load_env_file_values
from runtime_config import cfg_for_live_strategy, cfg_for_paper_strategy


def _path_tail(value: str | Path, count: int) -> tuple[str, ...]:
    return Path(value).parts[-count:]


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
            "STRATEGY_5_MAX_STAKE": "9.5",
            "STRATEGY_5_BASE_ORDER_COST": "1.2",
        }
    )

    assert isinstance(cfg, AppConfig)
    assert cfg.strategy_id == 5
    assert cfg.max_stake is None
    assert cfg.paper_strategy_profiles[5].max_stake == pytest.approx(9.5)
    assert cfg.paper_strategy_profiles[5].base_order_cost == pytest.approx(1.2)


def test_build_config_from_env_values_reads_trade_mode_and_live_switches():
    cfg = build_config_from_env_values(
        {
            'TRADE_MODE': 'live',
            'LIVE_TRADING_ENABLED': 'true',
            'POLYMARKET_CHAIN_ID': '137',
        }
    )

    assert cfg.trade_mode == 'live'
    assert cfg.live_trading_enabled is True
    assert cfg.live_chain_id == 137


def test_build_config_from_env_values_accepts_both_trade_mode():
    cfg = build_config_from_env_values({'TRADE_MODE': 'both'})

    assert cfg.trade_mode == 'both'


def test_build_config_from_env_values_defaults_trade_mode_to_paper():
    cfg = build_config_from_env_values({})

    assert cfg.trade_mode == 'paper'


def test_build_config_from_env_values_ignores_removed_live_auto_redeem_settings():
    cfg = build_config_from_env_values(
        {
            'LIVE_AUTO_REDEEM_ENABLED': 'true',
            'LIVE_AUTO_REDEEM_POLL_SECONDS': '5',
            'POLYMARKET_RELAYER_API_KEY': 'relayer-key',
        }
    )

    assert not hasattr(cfg, 'live_auto_redeem_enabled')
    assert not hasattr(cfg, 'live_auto_redeem_poll_seconds')
    assert not hasattr(cfg, 'live_redeem_relayer_api_key')

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
    assert load_calls[:2] == [env_file, env_file]
    assert len(load_calls) >= 2
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

    def fake_wait_for_runtime_exit(*, stop_event, dashboard_thread, worker_threads):
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

def test_run_single_command_runtime_uses_live_worker_when_trade_mode_live(monkeypatch, tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    startup_cfg = AppConfig(
        trade_mode='live',
        live_trading_enabled=True,
        live_private_key='pk',
        live_funder='0xfunder',
    )
    refreshed_cfg = AppConfig(
        trade_mode='live',
        live_trading_enabled=True,
        live_private_key='pk-refresh',
        live_funder='0xfunder',
    )
    load_calls: list[Path] = []

    def fake_load_shared_config(path: Path):
        load_calls.append(path)
        if len(load_calls) == 1:
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
    live_calls = {}
    paper_calls = {'count': 0}

    def fake_run_live_trading(cfg, *, stop_event, config_provider):
        live_calls['cfg'] = cfg
        live_calls['provider_cfg'] = config_provider()
        live_calls['stop_event'] = stop_event
        return {'status': 'stopped'}

    def fake_run_paper_trading(cfg, **kwargs):
        paper_calls['count'] += 1
        return {'status': 'stopped'}

    monkeypatch.setattr(main, '_load_shared_config', fake_load_shared_config)
    monkeypatch.setattr(main, 'create_dashboard_runtime', lambda **_: dashboard_runtime)
    monkeypatch.setattr(main, 'run_live_trading', fake_run_live_trading, raising=False)
    monkeypatch.setattr(main, 'run_paper_trading', fake_run_paper_trading)

    exit_code = main.run_single_command_runtime(env_file=env_file)

    assert exit_code == 0
    assert load_calls[:2] == [env_file, env_file]
    assert paper_calls['count'] == 0
    assert live_calls['cfg'] is startup_cfg
    assert live_calls['provider_cfg'] is refreshed_cfg
    assert dashboard_runtime.shutdown_calls >= 1
    assert dashboard_runtime.close_calls == 1


def test_run_single_command_runtime_live_mode_without_live_switch_fails_fast(monkeypatch, tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    startup_cfg = AppConfig(
        trade_mode='live',
        live_trading_enabled=False,
    )
    dashboard_kwargs = {}
    live_calls = {"count": 0}
    paper_calls = {"count": 0}

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

    def fake_dashboard_runtime(**kwargs):
        dashboard_kwargs.update(kwargs)
        return dashboard_runtime

    def fake_run_live_trading(*args, **kwargs):
        live_calls["count"] += 1
        return {"status": "stopped"}

    def fake_run_paper_trading(cfg, *, stop_event, config_provider, **kwargs):
        paper_calls["count"] += 1
        stop_event.set()
        return {"status": "stopped"}

    monkeypatch.setattr(main, '_load_shared_config', lambda _path: startup_cfg)
    monkeypatch.setattr(main, 'create_dashboard_runtime', fake_dashboard_runtime)
    monkeypatch.setattr(main, 'run_live_trading', fake_run_live_trading, raising=False)
    monkeypatch.setattr(main, 'run_paper_trading', fake_run_paper_trading)

    exit_code = main.run_single_command_runtime(env_file=env_file)

    assert exit_code == 1
    assert dashboard_kwargs == {}
    assert paper_calls["count"] == 0
    assert live_calls["count"] == 0


def test_run_single_command_runtime_does_not_start_live_redeem_worker_in_live_mode(monkeypatch, tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    startup_cfg = AppConfig(
        trade_mode="live",
        live_trading_enabled=True,
        live_private_key="pk",
        live_funder="0xfunder",
    )
    load_calls: list[Path] = []

    def fake_load_shared_config(path: Path):
        load_calls.append(path)
        return startup_cfg

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
    live_calls = {"trading": 0, "redeem": 0}
    paper_calls = {"count": 0}

    def fake_run_live_trading(cfg, *, stop_event, config_provider):
        live_calls["trading"] += 1
        return {"status": "stopped"}

    def fake_run_live_redeem_worker(cfg, *, stop_event, config_provider):
        live_calls["redeem"] += 1
        stop_event.set()
        return {"status": "stopped"}

    def fake_run_paper_trading(cfg, **kwargs):
        paper_calls["count"] += 1
        return {"status": "stopped"}

    monkeypatch.setattr(main, "_load_shared_config", fake_load_shared_config)
    monkeypatch.setattr(main, "create_dashboard_runtime", lambda **_: dashboard_runtime)
    monkeypatch.setattr(main, "run_live_trading", fake_run_live_trading, raising=False)
    monkeypatch.setattr(main, "run_live_redeem_worker", fake_run_live_redeem_worker, raising=False)
    monkeypatch.setattr(main, "run_paper_trading", fake_run_paper_trading)

    exit_code = main.run_single_command_runtime(env_file=env_file)

    assert exit_code == 0
    assert load_calls[0] == env_file
    assert paper_calls["count"] == 0
    assert live_calls == {"trading": 1, "redeem": 0}


def test_run_single_command_runtime_does_not_start_live_redeem_worker_in_paper_mode(monkeypatch):
    class FakeDashboardRuntime:
        def serve_forever(self) -> None:
            return
        def shutdown(self) -> None:
            return
        def close(self) -> None:
            return

    redeem_calls = {"count": 0}

    def fake_run_paper_trading(cfg, *, stop_event, config_provider):
        stop_event.set()
        return {"status": "stopped"}

    def fake_run_live_redeem_worker(*args, **kwargs):
        redeem_calls["count"] += 1
        return {"status": "stopped"}

    monkeypatch.setattr(main, "_load_shared_config", lambda _: AppConfig(trade_mode="paper"))
    monkeypatch.setattr(main, "create_dashboard_runtime", lambda **_: FakeDashboardRuntime())
    monkeypatch.setattr(main, "run_paper_trading", fake_run_paper_trading)
    monkeypatch.setattr(main, "run_live_redeem_worker", fake_run_live_redeem_worker, raising=False)

    exit_code = main.run_single_command_runtime()

    assert exit_code == 0
    assert redeem_calls["count"] == 0


def test_run_single_command_runtime_fails_fast_when_live_startup_validation_fails(monkeypatch):
    startup_cfg = AppConfig(trade_mode='live', live_trading_enabled=True)
    dashboard_calls = {'count': 0}
    paper_calls = {'count': 0}
    live_calls = {'count': 0}
    validate_calls = {'count': 0}

    def fake_validate(cfg):
        validate_calls['count'] += 1
        raise RuntimeError('Missing private key for live trading.')

    def unexpected_dashboard_runtime(**kwargs):
        dashboard_calls['count'] += 1
        raise AssertionError('dashboard should not start when live validation fails')

    def unexpected_paper_worker(*args, **kwargs):
        paper_calls['count'] += 1
        raise AssertionError('paper worker should not start when live validation fails')

    def unexpected_live_worker(*args, **kwargs):
        live_calls['count'] += 1
        raise AssertionError('live worker should not start when live validation fails')

    monkeypatch.setattr(main, '_load_shared_config', lambda _: startup_cfg)
    monkeypatch.setattr(main, 'validate_live_runtime_config', fake_validate, raising=False)
    monkeypatch.setattr(main, 'create_dashboard_runtime', unexpected_dashboard_runtime)
    monkeypatch.setattr(main, 'run_paper_trading', unexpected_paper_worker)
    monkeypatch.setattr(main, 'run_live_trading', unexpected_live_worker, raising=False)

    exit_code = main.run_single_command_runtime()

    assert exit_code == 1
    assert validate_calls['count'] == 1
    assert dashboard_calls['count'] == 0
    assert paper_calls['count'] == 0
    assert live_calls['count'] == 0



def test_run_single_command_runtime_switches_to_live_after_safe_point(monkeypatch, tmp_path: Path):
    startup_cfg = AppConfig(trade_mode='paper')
    live_cfg = AppConfig(
        trade_mode='live',
        live_trading_enabled=True,
        live_private_key='pk',
        live_funder='0xfunder',
    )
    states = {'count': 0}
    calls = []

    def fake_load_shared_config(_path: Path):
        states['count'] += 1
        return startup_cfg if states['count'] == 1 else live_cfg

    def fake_create_dashboard_runtime(**kwargs):
        class FakeDashboardRuntime:
            def serve_forever(self):
                return
            def shutdown(self):
                return
            def close(self):
                return
        return FakeDashboardRuntime()

    def fake_run_paper_trading(cfg, *, stop_event, config_provider, runtime_control=None, stop_when_safe=None):
        calls.append(('paper', cfg.trade_mode))
        runtime_control.update_worker_state(round_in_progress=False, safe_to_switch=True, pending_live_order=False, current_round_slug=None)
        return {'status': 'stopped'}

    def fake_run_live_trading(cfg, *, stop_event, config_provider, runtime_control=None, stop_when_safe=None):
        calls.append(('live', cfg.trade_mode))
        stop_event.set()
        return {'status': 'stopped'}

    monkeypatch.setattr(main, '_load_shared_config', fake_load_shared_config)
    monkeypatch.setattr(main, 'create_dashboard_runtime', fake_create_dashboard_runtime)
    monkeypatch.setattr(main, 'run_paper_trading', fake_run_paper_trading)
    monkeypatch.setattr(main, 'run_live_trading', fake_run_live_trading)

    exit_code = main.run_single_command_runtime(env_file=tmp_path / '.env.dashboard')

    assert exit_code == 0
    assert calls[0] == ('paper', 'paper')
    assert ('live', 'live') in calls


def test_run_single_command_runtime_switches_from_live_to_paper_when_live_disabled(monkeypatch, tmp_path: Path):
    startup_cfg = AppConfig(
        trade_mode='live',
        live_trading_enabled=True,
        live_private_key='pk',
        live_funder='0xfunder',
    )
    paper_cfg = AppConfig(trade_mode='paper', live_trading_enabled=False)
    states = {'count': 0}
    calls: list[tuple[str, str, bool]] = []

    def fake_load_shared_config(_path: Path):
        states['count'] += 1
        return startup_cfg if states['count'] == 1 else paper_cfg

    def fake_create_dashboard_runtime(**kwargs):
        class FakeDashboardRuntime:
            def serve_forever(self):
                return
            def shutdown(self):
                return
            def close(self):
                return
        return FakeDashboardRuntime()

    def fake_run_paper_trading(cfg, *, stop_event, config_provider, runtime_control=None, stop_when_safe=None):
        calls.append(('paper', cfg.trade_mode, cfg.live_trading_enabled))
        if not cfg.live_trading_enabled:
            stop_event.set()
        return {'status': 'stopped'}

    def fake_run_live_trading(cfg, *, stop_event, config_provider, runtime_control=None, stop_when_safe=None):
        calls.append(('live', cfg.trade_mode, cfg.live_trading_enabled))
        runtime_control.update_worker_state(round_in_progress=False, safe_to_switch=True, pending_live_order=False, current_round_slug=None)
        return {'status': 'stopped'}

    monkeypatch.setattr(main, '_load_shared_config', fake_load_shared_config)
    monkeypatch.setattr(main, 'create_dashboard_runtime', fake_create_dashboard_runtime)
    monkeypatch.setattr(main, 'run_paper_trading', fake_run_paper_trading)
    monkeypatch.setattr(main, 'run_live_trading', fake_run_live_trading)

    exit_code = main.run_single_command_runtime(env_file=tmp_path / '.env.dashboard')

    assert exit_code == 0
    assert calls == [
        ('live', 'live', True),
        ('paper', 'paper', False),
    ]


def test_run_single_command_runtime_reloads_to_paper_only_when_live_switch_disabled(monkeypatch, tmp_path: Path):
    startup_cfg = AppConfig(
        trade_mode='both',
        live_trading_enabled=True,
        live_private_key='pk',
        live_funder='0xfunder',
    )
    paper_cfg = AppConfig(trade_mode='paper', live_trading_enabled=False)
    states = {'count': 0}
    calls: list[tuple[str, str, bool]] = []
    runtime_reload = {'callback': None}

    def fake_load_shared_config(_path: Path):
        states['count'] += 1
        return startup_cfg if states['count'] == 1 else paper_cfg

    def fake_create_dashboard_runtime(**kwargs):
        runtime_reload['callback'] = kwargs.get('notify_runtime_reload')

        class FakeDashboardRuntime:
            def serve_forever(self):
                return
            def shutdown(self):
                return
            def close(self):
                return

        return FakeDashboardRuntime()

    def fake_run_paper_trading(cfg, *, stop_event, config_provider, runtime_control=None, stop_when_safe=None):
        calls.append(('paper', cfg.trade_mode, cfg.live_trading_enabled))
        if len(calls) == 1:
            runtime_reload['callback']('live_trading_enabled')
            return {'status': 'stopped'}
        stop_event.set()
        return {'status': 'stopped'}

    def fake_run_live_trading(cfg, *, stop_event, config_provider, runtime_control=None, stop_when_safe=None):
        calls.append(('live', cfg.trade_mode, cfg.live_trading_enabled))
        runtime_control.update_worker_state(round_in_progress=False, safe_to_switch=True, pending_live_order=False, current_round_slug=None)
        return {'status': 'stopped', 'skip_reason': 'live_trading_disabled'}

    monkeypatch.setattr(main, '_load_shared_config', fake_load_shared_config)
    monkeypatch.setattr(main, 'create_dashboard_runtime', fake_create_dashboard_runtime)
    monkeypatch.setattr(main, 'run_paper_trading', fake_run_paper_trading)
    monkeypatch.setattr(main, 'run_live_trading', fake_run_live_trading)

    exit_code = main.run_single_command_runtime(env_file=tmp_path / '.env.dashboard')

    assert exit_code == 0
    assert calls == [
        ('live', 'live', True),
        ('paper', 'paper', False),
    ]


def test_run_single_command_runtime_restarts_worker_after_timeframe_reload_request(monkeypatch, tmp_path: Path):
    startup_cfg = AppConfig(trade_mode='paper', market_timeframe='5m')
    reloaded_cfg = AppConfig(trade_mode='paper', market_timeframe='15m')
    states = {'count': 0}
    calls: list[tuple[str, str]] = []
    runtime_reload = {'callback': None}

    def fake_load_shared_config(_path: Path):
        states['count'] += 1
        return startup_cfg if states['count'] == 1 else reloaded_cfg

    def fake_create_dashboard_runtime(**kwargs):
        runtime_reload['callback'] = kwargs.get('notify_runtime_reload')

        class FakeDashboardRuntime:
            def serve_forever(self):
                return
            def shutdown(self):
                return
            def close(self):
                return

        return FakeDashboardRuntime()

    def fake_run_paper_trading(cfg, *, stop_event, config_provider, runtime_control=None, stop_when_safe=None):
        calls.append(('paper', cfg.market_timeframe))
        runtime_control.update_worker_state(round_in_progress=False, safe_to_switch=True, pending_live_order=False, current_round_slug=None)
        if len(calls) == 1:
            runtime_reload['callback']('market_timeframe')
            return {'status': 'stopped'}
        stop_event.set()
        return {'status': 'stopped'}

    monkeypatch.setattr(main, '_load_shared_config', fake_load_shared_config)
    monkeypatch.setattr(main, 'create_dashboard_runtime', fake_create_dashboard_runtime)
    monkeypatch.setattr(main, 'run_paper_trading', fake_run_paper_trading)

    exit_code = main.run_single_command_runtime(env_file=tmp_path / '.env.dashboard')

    assert exit_code == 0
    assert calls == [('paper', '5m'), ('paper', '15m')]


def test_run_single_command_runtime_starts_one_paper_worker_per_enabled_timeframe(monkeypatch, tmp_path: Path):
    startup_cfg = build_config_from_env_values(
        {
            'TRADE_MODE': 'paper',
            'PAPER_TIMEFRAMES': '5m,15m',
            'PAPER_5M_STRATEGY_ID': '5',
            'PAPER_5M_STRATEGY_IDS': '5,6',
            'PAPER_15M_STRATEGY_ID': '2',
            'PAPER_15M_STRATEGY_IDS': '1,2',
        }
    )
    calls: list[tuple[str, list[int], str, str]] = []

    class FakeDashboardRuntime:
        def serve_forever(self) -> None:
            return

        def shutdown(self) -> None:
            return

        def close(self) -> None:
            return

    def fake_run_paper_trading(cfg, *, stop_event, config_provider, state_path=None, log_path=None, runtime_control=None, stop_when_safe=None):
        calls.append((cfg.market_timeframe, list(cfg.paper_strategy_ids), str(state_path), str(log_path)))
        if len(calls) == 2:
            stop_event.set()
        return {'status': 'stopped'}

    monkeypatch.setattr(main, '_load_shared_config', lambda _: startup_cfg)
    monkeypatch.setattr(main, 'run_paper_trading', fake_run_paper_trading)
    monkeypatch.setattr(main, 'create_dashboard_runtime', lambda **_: FakeDashboardRuntime())

    exit_code = main.run_single_command_runtime(env_file=tmp_path / '.env.dashboard')

    assert exit_code == 0
    assert [call[0] for call in calls] == ['5m', '15m']
    assert calls[0][1] == [5, 6]
    assert calls[1][1] == [1, 2]
    assert _path_tail(calls[0][2], 4) == ('logs', 'paper', '5m', 'session_state.json')
    assert _path_tail(calls[1][3], 4) == ('logs', 'paper', '15m', 'paper_trades.csv')


def test_run_single_command_runtime_keeps_live_single_worker_when_paper_profiles_exist(monkeypatch):
    cfg = build_config_from_env_values(
        {
            'TRADE_MODE': 'live',
            'MARKET_TIMEFRAME': '15m',
            'LIVE_TRADING_ENABLED': 'true',
            'POLYMARKET_PRIVATE_KEY': 'pk',
            'POLYMARKET_FUNDER': '0xfunder',
            'PAPER_TIMEFRAMES': '5m,15m',
        }
    )
    live_calls: list[str] = []

    class FakeDashboardRuntime:
        def serve_forever(self) -> None:
            return

        def shutdown(self) -> None:
            return

        def close(self) -> None:
            return

    monkeypatch.setattr(main, '_load_shared_config', lambda _: cfg)
    monkeypatch.setattr(main, 'create_dashboard_runtime', lambda **_: FakeDashboardRuntime())
    monkeypatch.setattr(
        main,
        'run_live_trading',
        lambda cfg, **kwargs: live_calls.append(cfg.market_timeframe) or {'status': 'stopped'},
        raising=False,
    )

    exit_code = main.run_single_command_runtime()

    assert exit_code == 0
    assert live_calls == ['15m']


def test_run_single_command_runtime_starts_only_paper_when_trade_mode_is_paper_even_if_live_enabled(monkeypatch):
    cfg = build_config_from_env_values(
        {
            'TRADE_MODE': 'paper',
            'LIVE_TRADING_ENABLED': 'true',
            'POLYMARKET_PRIVATE_KEY': 'pk',
            'POLYMARKET_FUNDER': '0xfunder',
            'PAPER_TIMEFRAMES': '5m',
            'PAPER_5M_STRATEGY_ID': '5',
            'PAPER_5M_STRATEGY_IDS': '5,6',
            'LIVE_STRATEGY_IDS': '7',
            'MARKET_TIMEFRAME': '15m',
        }
    )
    calls: list[tuple[str, str, str]] = []

    class FakeDashboardRuntime:
        def serve_forever(self) -> None:
            return

        def shutdown(self) -> None:
            return

        def close(self) -> None:
            return

    def fake_run_paper_trading(cfg, *, stop_event, config_provider, state_path=None, log_path=None, runtime_control=None, stop_when_safe=None):
        calls.append(('paper', cfg.market_timeframe, str(log_path)))
        return {'status': 'stopped'}

    def fake_run_live_trading(cfg, *, stop_event, config_provider, runtime_control=None, stop_when_safe=None):
        calls.append(('live', cfg.market_timeframe, 'live_orders.csv'))
        stop_event.set()
        return {'status': 'stopped'}

    monkeypatch.setattr(main, '_load_shared_config', lambda _: cfg)
    monkeypatch.setattr(main, 'create_dashboard_runtime', lambda **_: FakeDashboardRuntime())
    monkeypatch.setattr(main, 'run_paper_trading', fake_run_paper_trading)
    monkeypatch.setattr(main, 'run_live_trading', fake_run_live_trading, raising=False)

    exit_code = main.run_single_command_runtime()

    assert exit_code == 0
    assert calls == [('paper', '5m', str(Path('logs') / 'paper' / '5m' / 'paper_trades.csv'))]


def test_paper_timeframe_worker_config_preserves_strategy7_runtime_gates():
    cfg = build_config_from_env_values(
        {
            'TRADE_MODE': 'paper',
            'MAX_ENTRY_PRICE': '0.52',
            'STRATEGY7_MAX_ENTRY_PRICE': '0.52',
            'PAPER_TIMEFRAMES': '5m',
            'PAPER_5M_STRATEGY_ID': '7',
            'PAPER_5M_STRATEGY_IDS': '7',
            'PAPER_5M_MIN_ENTRY_PRICE': '0.48',
            'PAPER_5M_MAX_ENTRY_PRICE': '0.54',
            'PAPER_5M_STRATEGY7_MAX_MOMENTUM_DELTA': '0.12',
            'PAPER_5M_STRATEGY7_MIN_SIGNAL_GAP': '0.006',
            'PAPER_5M_STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS': '2',
            'PAPER_5M_STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP': '0.035',
            'PAPER_5M_STRATEGY7_LATE_CONFIRM_RELAX_SECONDS': '0',
            'STRATEGY_7_MIN_ENTRY_PRICE': '0.48',
            'STRATEGY_7_MAX_ENTRY_PRICE': '0.54',
            'STRATEGY_7_MAX_MOMENTUM_DELTA': '0.12',
            'STRATEGY_7_MIN_SIGNAL_GAP': '0.006',
            'STRATEGY_7_CONFIRM_BEFORE_ENTRY_SECONDS': '2',
            'STRATEGY_7_LATE_CONFIRM_STRONG_SIGNAL_GAP': '0.035',
            'STRATEGY_7_LATE_CONFIRM_RELAX_SECONDS': '0',
        }
    )

    timeframe_cfg = main._paper_cfg_for_timeframe(cfg, '5m')
    strategy_cfg = cfg_for_paper_strategy(timeframe_cfg, 7)

    assert strategy_cfg.min_entry_price == pytest.approx(0.48)
    assert strategy_cfg.max_entry_price == pytest.approx(0.54)
    assert strategy_cfg.strategy7_max_momentum_delta == pytest.approx(0.12)
    assert strategy_cfg.strategy7_min_signal_gap == pytest.approx(0.006)
    assert strategy_cfg.strategy7_confirm_before_entry_seconds == 2
    assert strategy_cfg.strategy7_late_confirm_strong_signal_gap == pytest.approx(0.035)
    assert strategy_cfg.strategy7_late_confirm_relax_seconds == pytest.approx(0.0)


def test_paper_timeframe_worker_preserves_shared_strategy_profile_overrides():
    cfg = build_config_from_env_values(
        {
            'TRADE_MODE': 'paper',
            'PAPER_TIMEFRAMES': '5m',
            'PAPER_STRATEGY_IDS': '7',
            'PAPER_5M_STRATEGY_ID': '7',
            'PAPER_5M_STRATEGY_IDS': '7',
            'PAPER_5M_MAX_ENTRY_PRICE': '0.53',
            'STRATEGY_7_BASE_ORDER_COST': '1.2',
            'STRATEGY_7_MAX_STAKE': '60',
            'STRATEGY_7_MIN_ENTRY_PRICE': '0.50',
            'STRATEGY_7_MAX_ENTRY_PRICE': '0.54',
            'STRATEGY_7_LIVE_MAX_PRICE_IMPROVEMENT': '0.04',
            'STRATEGY_7_MOMENTUM_THRESHOLD': '0.008',
        }
    )

    timeframe_cfg = main._paper_cfg_for_timeframe(cfg, '5m')
    strategy_cfg = cfg_for_paper_strategy(timeframe_cfg, 7)

    assert strategy_cfg.base_order_cost == pytest.approx(1.2)
    assert strategy_cfg.max_stake == pytest.approx(60.0)
    assert strategy_cfg.min_entry_price == pytest.approx(0.50)
    assert strategy_cfg.max_entry_price == pytest.approx(0.54)
    assert strategy_cfg.live_max_price_improvement == pytest.approx(0.04)
    assert strategy_cfg.strategy7_momentum_threshold == pytest.approx(0.008)


def test_active_mode_worker_config_preserves_strategy_profile_overrides():
    cfg = build_config_from_env_values(
        {
            'TRADE_MODE': 'both',
            'PAPER_STRATEGY_IDS': '7,10',
            'LIVE_STRATEGY_IDS': '7,10',
            'STRATEGY_10_BASE_ORDER_COST': '1.2',
            'STRATEGY_10_MIN_ENTRY_PRICE': '0.50',
            'STRATEGY_10_MAX_ENTRY_PRICE': '0.54',
            'STRATEGY_10_LIVE_MAX_PRICE_IMPROVEMENT': '0.04',
            'STRATEGY_10_MIN_EDGE': '0.05',
        }
    )

    live_cfg = main._cfg_for_active_mode(cfg, 'live')
    paper_cfg = main._cfg_for_active_mode(cfg, 'paper')
    live_strategy_cfg = cfg_for_live_strategy(live_cfg, 10)
    paper_strategy_cfg = cfg_for_paper_strategy(paper_cfg, 10)

    assert live_strategy_cfg.base_order_cost == pytest.approx(1.2)
    assert live_strategy_cfg.min_entry_price == pytest.approx(0.50)
    assert live_strategy_cfg.max_entry_price == pytest.approx(0.54)
    assert live_strategy_cfg.live_max_price_improvement == pytest.approx(0.04)
    assert live_strategy_cfg.strategy10_min_edge == pytest.approx(0.05)
    assert paper_strategy_cfg.base_order_cost == pytest.approx(1.2)
    assert paper_strategy_cfg.min_entry_price == pytest.approx(0.50)
    assert paper_strategy_cfg.max_entry_price == pytest.approx(0.54)
    assert paper_strategy_cfg.strategy10_min_edge == pytest.approx(0.05)


def test_build_config_from_env_values_prefers_mode_specific_strategy_overrides():
    cfg = build_config_from_env_values(
        {
            'TRADE_MODE': 'both',
            'PAPER_STRATEGY_IDS': '7,9,10,11',
            'LIVE_STRATEGY_IDS': '7,10',
            'STRATEGY_10_BASE_ORDER_COST': '1.5',
            'STRATEGY_10_MIN_ENTRY_PRICE': '0.49',
            'STRATEGY_10_MAX_ENTRY_PRICE': '0.54',
            'STRATEGY_10_MIN_EDGE': '0.045',
            'PAPER_STRATEGY_10_BASE_ORDER_COST': '1.0',
            'PAPER_STRATEGY_10_MIN_ENTRY_PRICE': '0.45',
            'PAPER_STRATEGY_10_MIN_EDGE': '0.035',
            'LIVE_STRATEGY_10_BASE_ORDER_COST': '2.0',
            'LIVE_STRATEGY_10_MIN_ENTRY_PRICE': '0.50',
            'LIVE_STRATEGY_10_MIN_EDGE': '0.05',
        }
    )

    paper_strategy_cfg = cfg_for_paper_strategy(cfg, 10)
    live_strategy_cfg = cfg_for_live_strategy(cfg, 10)

    assert cfg.paper_strategy_ids == [7, 9, 10, 11]
    assert cfg.live_strategy_ids == [7, 10]
    assert paper_strategy_cfg.base_order_cost == pytest.approx(1.0)
    assert paper_strategy_cfg.min_entry_price == pytest.approx(0.45)
    assert paper_strategy_cfg.max_entry_price == pytest.approx(0.54)
    assert paper_strategy_cfg.strategy10_min_edge == pytest.approx(0.035)
    assert live_strategy_cfg.base_order_cost == pytest.approx(2.0)
    assert live_strategy_cfg.min_entry_price == pytest.approx(0.50)
    assert live_strategy_cfg.max_entry_price == pytest.approx(0.54)
    assert live_strategy_cfg.strategy10_min_edge == pytest.approx(0.05)


def test_paper_timeframe_worker_config_preserves_strategy9_dynamic_sizing():
    cfg = build_config_from_env_values(
        {
            'TRADE_MODE': 'paper',
            'PAPER_TIMEFRAMES': '5m',
            'PAPER_5M_STRATEGY_ID': '9',
            'PAPER_5M_STRATEGY_IDS': '9',
            'PAPER_5M_STRATEGY9_DYNAMIC_SIZING_ENABLED': 'true',
            'PAPER_5M_STRATEGY9_SIZING_REFERENCE_PRICE': '0.51',
            'PAPER_5M_STRATEGY9_SIZING_PRICE_STEP': '0.02',
            'PAPER_5M_STRATEGY9_SIZING_PRICE_STEP_REDUCTION': '0.15',
            'PAPER_5M_STRATEGY9_SIZING_MIN_MULTIPLIER': '0.45',
            'PAPER_5M_STRATEGY9_SIZING_MAX_MULTIPLIER': '1.10',
            'PAPER_5M_STRATEGY9_SIZING_STRONG_SIGNAL_GAP': '0.03',
            'PAPER_5M_STRATEGY9_SIZING_STRONG_SIGNAL_BOOST': '0.25',
            'STRATEGY_9_DYNAMIC_SIZING_ENABLED': 'true',
            'STRATEGY_9_SIZING_REFERENCE_PRICE': '0.51',
            'STRATEGY_9_SIZING_PRICE_STEP': '0.02',
            'STRATEGY_9_SIZING_PRICE_STEP_REDUCTION': '0.15',
            'STRATEGY_9_SIZING_MIN_MULTIPLIER': '0.45',
            'STRATEGY_9_SIZING_MAX_MULTIPLIER': '1.10',
            'STRATEGY_9_SIZING_STRONG_SIGNAL_GAP': '0.03',
            'STRATEGY_9_SIZING_STRONG_SIGNAL_BOOST': '0.25',
        }
    )

    timeframe_cfg = main._paper_cfg_for_timeframe(cfg, '5m')
    strategy_cfg = cfg_for_paper_strategy(timeframe_cfg, 9)

    assert strategy_cfg.strategy9_dynamic_sizing_enabled is True
    assert strategy_cfg.strategy9_sizing_reference_price == pytest.approx(0.51)
    assert strategy_cfg.strategy9_sizing_price_step == pytest.approx(0.02)
    assert strategy_cfg.strategy9_sizing_price_step_reduction == pytest.approx(0.15)
    assert strategy_cfg.strategy9_sizing_min_multiplier == pytest.approx(0.45)
    assert strategy_cfg.strategy9_sizing_max_multiplier == pytest.approx(1.10)
    assert strategy_cfg.strategy9_sizing_strong_signal_gap == pytest.approx(0.03)
    assert strategy_cfg.strategy9_sizing_strong_signal_boost == pytest.approx(0.25)


def test_run_single_command_runtime_starts_only_live_when_trade_mode_is_live(monkeypatch):
    cfg = build_config_from_env_values(
        {
            'TRADE_MODE': 'live',
            'LIVE_TRADING_ENABLED': 'true',
            'POLYMARKET_PRIVATE_KEY': 'pk',
            'POLYMARKET_FUNDER': '0xfunder',
            'PAPER_TIMEFRAMES': '5m',
            'PAPER_5M_STRATEGY_IDS': '5',
            'LIVE_STRATEGY_IDS': '7',
            'MARKET_TIMEFRAME': '15m',
        }
    )
    calls: list[tuple[str, str, str]] = []

    class FakeDashboardRuntime:
        def serve_forever(self) -> None:
            return

        def shutdown(self) -> None:
            return

        def close(self) -> None:
            return

    def fake_run_paper_trading(cfg, *, stop_event, config_provider, state_path=None, log_path=None, runtime_control=None, stop_when_safe=None):
        calls.append(('paper', cfg.trade_mode, cfg.market_timeframe))
        return {'status': 'stopped'}

    def fake_run_live_trading(cfg, *, stop_event, config_provider, runtime_control=None, stop_when_safe=None):
        calls.append(('live', cfg.trade_mode, cfg.market_timeframe))
        stop_event.set()
        return {'status': 'stopped'}

    monkeypatch.setattr(main, '_load_shared_config', lambda _: cfg)
    monkeypatch.setattr(main, 'create_dashboard_runtime', lambda **_: FakeDashboardRuntime())
    monkeypatch.setattr(main, 'run_paper_trading', fake_run_paper_trading)
    monkeypatch.setattr(main, 'run_live_trading', fake_run_live_trading, raising=False)

    exit_code = main.run_single_command_runtime()

    assert exit_code == 0
    assert calls == [('live', 'live', '15m')]


def test_run_single_command_runtime_uses_live_worker_as_both_mode_decision_source(monkeypatch):
    cfg = build_config_from_env_values(
        {
            'TRADE_MODE': 'both',
            'LIVE_TRADING_ENABLED': 'true',
            'POLYMARKET_PRIVATE_KEY': 'pk',
            'POLYMARKET_FUNDER': '0xfunder',
            'PAPER_TIMEFRAMES': '5m',
            'PAPER_5M_STRATEGY_ID': '5',
            'PAPER_5M_STRATEGY_IDS': '5,6',
            'LIVE_STRATEGY_IDS': '7',
            'MARKET_TIMEFRAME': '15m',
        }
    )
    calls: list[tuple[str, str, str, object, object]] = []

    class FakeDashboardRuntime:
        def serve_forever(self) -> None:
            return

        def shutdown(self) -> None:
            return

        def close(self) -> None:
            return

    def fake_run_paper_trading(cfg, *, stop_event, config_provider, state_path=None, log_path=None, runtime_control=None, stop_when_safe=None):
        calls.append(('paper', cfg.trade_mode, cfg.market_timeframe, state_path, log_path))
        return {'status': 'stopped'}

    def fake_run_live_trading(
        cfg,
        *,
        stop_event,
        config_provider,
        runtime_control=None,
        stop_when_safe=None,
        mirror_paper_state_path=None,
        mirror_paper_log_path=None,
    ):
        calls.append(('live', cfg.trade_mode, cfg.market_timeframe, mirror_paper_state_path, mirror_paper_log_path))
        stop_event.set()
        return {'status': 'stopped'}

    monkeypatch.setattr(main, '_load_shared_config', lambda _: cfg)
    monkeypatch.setattr(main, 'create_dashboard_runtime', lambda **_: FakeDashboardRuntime())
    monkeypatch.setattr(main, 'run_paper_trading', fake_run_paper_trading)
    monkeypatch.setattr(main, 'run_live_trading', fake_run_live_trading, raising=False)

    exit_code = main.run_single_command_runtime()

    assert exit_code == 0
    assert not any(call[0] == 'paper' for call in calls)
    assert calls == [
        (
            'live',
            'live',
            '15m',
            cfg.logs_dir / 'paper' / '15m' / 'session_state.json',
            cfg.logs_dir / 'paper' / '15m' / 'paper_trades.csv',
        )
    ]



def test_runtime_manager_keeps_dashboard_online_while_switch_pending(tmp_path):
    cfg = AppConfig(trade_mode='paper')
    manager = main.RuntimeManager(
        env_file=tmp_path / '.env.dashboard',
        host='127.0.0.1',
        port=8787,
        startup_cfg=cfg,
        dashboard_runtime_factory=lambda **kwargs: object(),
        validate_live_config=lambda cfg: None,
    )
    manager.request_mode_change('live')
    manager.runtime_control.update_worker_state(round_in_progress=True, safe_to_switch=False, pending_live_order=False)

    manager.poll_once()

    snapshot = manager.snapshot()
    assert snapshot.switch_state == 'pending'


def test_readme_mentions_split_strategy_selection():
    text = Path('README.md').read_text(encoding='utf-8')

    assert 'Split strategy selection' in text
    assert 'PAPER_STRATEGY_IDS' in text
    assert 'LIVE_STRATEGY_IDS' in text
    assert 'STRATEGY_IDS` is retained only as a legacy fallback' in text
    assert 'Legacy paper timeframe/profile keys are still parsed for compatibility' in text
