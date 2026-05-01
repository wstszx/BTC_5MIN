from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

import main
from config import AppConfig, build_config_from_env_values, load_env_file_values


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
            "TARGET_PROFIT": "0.8",
        }
    )

    assert isinstance(cfg, AppConfig)
    assert cfg.strategy_id == 5
    assert cfg.max_stake == 9.5
    assert cfg.target_profit == 0.8


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


def test_build_config_from_env_values_defaults_trade_mode_to_paper():
    cfg = build_config_from_env_values({})

    assert cfg.trade_mode == 'paper'


def test_build_config_from_env_values_defaults_live_auto_redeem_settings():
    cfg = build_config_from_env_values({})

    assert cfg.live_auto_redeem_enabled is False
    assert cfg.live_auto_redeem_poll_seconds == 20
    assert cfg.live_auto_redeem_max_retries == 6
    assert cfg.live_auto_redeem_initial_backoff_seconds == 30
    assert cfg.live_auto_redeem_max_backoff_seconds == 300
    assert cfg.live_auto_redeem_dry_run is False


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
    assert load_calls[:3] == [env_file, env_file, env_file]
    assert paper_calls['count'] == 1
    assert live_calls['cfg'] is startup_cfg
    assert live_calls['provider_cfg'] is refreshed_cfg
    assert dashboard_runtime.shutdown_calls >= 1
    assert dashboard_runtime.close_calls == 1


def test_run_single_command_runtime_starts_live_redeem_worker_in_live_mode(monkeypatch, tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    startup_cfg = AppConfig(
        trade_mode="live",
        live_trading_enabled=True,
        live_private_key="pk",
        live_funder="0xfunder",
        live_auto_redeem_enabled=True,
        live_redeem_relayer_api_key="relayer-key",
        live_redeem_relayer_api_key_address="0xrelayer",
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
    assert paper_calls["count"] == 1
    assert live_calls == {"trading": 1, "redeem": 1}


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


def test_run_single_command_runtime_surfaces_live_redeem_worker_failures(monkeypatch):
    class FakeDashboardRuntime:
        def serve_forever(self) -> None:
            return
        def shutdown(self) -> None:
            return
        def close(self) -> None:
            return

    startup_cfg = AppConfig(
        trade_mode="live",
        live_trading_enabled=True,
        live_private_key="pk",
        live_funder="0xfunder",
        live_auto_redeem_enabled=True,
    )

    monkeypatch.setattr(main, "_load_shared_config", lambda _: startup_cfg)
    monkeypatch.setattr(main, "create_dashboard_runtime", lambda **_: FakeDashboardRuntime())
    monkeypatch.setattr(main, "run_live_trading", lambda cfg, *, stop_event, config_provider: {"status": "stopped"}, raising=False)
    monkeypatch.setattr(main, "run_live_redeem_worker", lambda cfg, *, stop_event, config_provider: (_ for _ in ()).throw(RuntimeError("redeem boom")), raising=False)

    exit_code = main.run_single_command_runtime()

    assert exit_code == 1


def test_run_single_command_runtime_fails_fast_when_live_startup_validation_fails(monkeypatch):
    startup_cfg = AppConfig(trade_mode='live')
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


def test_run_single_command_runtime_starts_paper_and_live_when_live_enabled(monkeypatch):
    cfg = build_config_from_env_values(
        {
            'TRADE_MODE': 'live',
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
    assert any(
        worker == 'paper'
        and timeframe == '5m'
        and _path_tail(log_path, 4) == ('logs', 'paper', '5m', 'paper_trades.csv')
        for worker, timeframe, log_path in calls
    )
    assert ('live', '15m', 'live_orders.csv') in calls


def test_run_single_command_runtime_can_run_live_alongside_paper_mode(monkeypatch):
    cfg = build_config_from_env_values(
        {
            'TRADE_MODE': 'paper',
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
    assert ('paper', 'paper', '5m') in calls
    assert ('live', 'live', '15m') in calls



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


def test_readme_mentions_unified_strategy_selection():
    text = Path('README.md').read_text(encoding='utf-8')

    assert 'Unified strategy selection' in text
    assert 'PAPER_STRATEGY_IDS' in text
    assert 'LIVE_STRATEGY_IDS' in text
    assert 'Legacy paper timeframe/profile keys are still parsed for compatibility' in text
