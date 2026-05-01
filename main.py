from __future__ import annotations

import inspect
from dataclasses import replace
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

from config import AppConfig, build_config_from_env_values, load_env_file_values
from dashboard import create_dashboard_runtime
from runtime_control import RuntimeControl
from trader import run_live_redeem_worker, run_live_trading, run_paper_trading, validate_live_runtime_config




def _build_worker_call_kwargs(
    worker,
    *,
    stop_event: threading.Event,
    config_provider,
    runtime_control: RuntimeControl,
    stop_when_safe,
) -> dict[str, object]:
    kwargs: dict[str, object] = {
        'stop_event': stop_event,
        'config_provider': config_provider,
    }
    signature = inspect.signature(worker)
    if 'runtime_control' in signature.parameters:
        kwargs['runtime_control'] = runtime_control
    if 'stop_when_safe' in signature.parameters:
        kwargs['stop_when_safe'] = stop_when_safe
    return kwargs


def _cfg_for_active_mode(cfg: AppConfig, mode: str) -> AppConfig:
    if not hasattr(cfg, '__dataclass_fields__'):
        return cfg
    if getattr(cfg, 'trade_mode', None) == mode:
        return cfg
    worker_cfg = replace(cfg)
    worker_cfg.trade_mode = mode
    return worker_cfg


def _paper_runtime_paths(cfg: AppConfig, timeframe: str) -> tuple[Path, Path]:
    base = cfg.logs_dir / 'paper' / timeframe
    return base / 'session_state.json', base / 'paper_trades.csv'


def _paper_cfg_for_timeframe(cfg: AppConfig, timeframe: str) -> AppConfig:
    if not hasattr(cfg, '__dataclass_fields__') or not hasattr(cfg, 'paper_profiles'):
        return cfg
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
        min_stake=profile.min_stake,
        max_stake=profile.max_stake,
        min_entry_price=profile.min_entry_price,
        max_entry_price=profile.max_entry_price,
        open_delay_seconds=profile.open_delay_seconds,
        signal_momentum_threshold=profile.signal_momentum_threshold,
        ofi_threshold=profile.ofi_threshold,
        binance_signal_stale_seconds=profile.binance_signal_stale_seconds,
        strategy7_ofi_threshold=profile.strategy7_ofi_threshold,
        strategy7_momentum_threshold=profile.strategy7_momentum_threshold,
        strategy7_max_entry_price=profile.strategy7_max_entry_price,
    )


class _PaperRuntimeControlProxy:
    def __init__(self, runtime_control: RuntimeControl, worker_key: str) -> None:
        self._runtime_control = runtime_control
        self._worker_key = worker_key

    def update_worker_state(self, **changes):
        return self._runtime_control.update_paper_worker_state(self._worker_key, **changes)


class _NullRuntimeControl:
    def update_worker_state(self, **changes):
        return None

    def snapshot(self):
        return RuntimeControl(initial_mode='paper').snapshot()


def _cfg_trade_mode(cfg: AppConfig, fallback: str = 'paper') -> str:
    return str(getattr(cfg, 'trade_mode', fallback) or fallback).strip().lower() or fallback


def _runtime_control_for_paper(
    *,
    runtime_control: RuntimeControl,
    worker_key: str | None = None,
    live_enabled: bool,
):
    if live_enabled:
        return _NullRuntimeControl()
    if worker_key:
        return _PaperRuntimeControlProxy(runtime_control, worker_key)
    return runtime_control


def _load_shared_config(env_file: Path) -> AppConfig:
    env_values = load_env_file_values(env_file)
    return build_config_from_env_values(env_values)


def _spawn_runtime_worker(
    *,
    name: str,
    target,
    stop_event: threading.Event,
    worker_errors: list[tuple[str, BaseException]],
) -> threading.Thread:
    def _runner() -> None:
        try:
            target()
        except BaseException as exc:  # pragma: no cover - exercised in integration tests
            worker_errors.append((name, exc))
            stop_event.set()

    thread = threading.Thread(target=_runner, name=name, daemon=True)
    thread.start()
    return thread


def _wait_for_runtime_exit(
    *,
    stop_event: threading.Event,
    dashboard_thread: threading.Thread,
    worker_threads: list[threading.Thread],
) -> None:
    while True:
        if stop_event.is_set():
            return
        if not dashboard_thread.is_alive():
            return
        if any(not worker_thread.is_alive() for worker_thread in worker_threads):
            return
        time.sleep(0.1)




class RuntimeManager:
    def __init__(
        self,
        *,
        env_file: Path,
        host: str,
        port: int,
        startup_cfg: AppConfig | None = None,
        dashboard_runtime_factory=create_dashboard_runtime,
        validate_live_config=validate_live_runtime_config,
    ) -> None:
        self.env_file = Path(env_file)
        self.host = host
        self.port = port
        self.dashboard_runtime_factory = dashboard_runtime_factory
        self.validate_live_config = validate_live_config
        self.startup_cfg = startup_cfg or _load_shared_config(self.env_file)
        self.runtime_control = RuntimeControl(initial_mode=getattr(self.startup_cfg, 'trade_mode', 'paper'))
        self._reload_requested = False
        self._reload_reason: str | None = None

    def snapshot(self):
        return self.runtime_control.snapshot()

    def request_mode_change(self, mode: str) -> None:
        self.runtime_control.set_desired_mode(mode)

    def request_runtime_reload(self, reason: str = 'config_reload') -> None:
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
            if snapshot.switch_state != 'idle' or snapshot.switch_reason is not None:
                self.runtime_control.mark_active_mode(snapshot.active_mode)
            return
        if snapshot.round_in_progress or not snapshot.safe_to_switch or snapshot.pending_live_order:
            return
        if snapshot.desired_mode == 'live':
            try:
                cfg = _load_shared_config(self.env_file)
                self.validate_live_config(cfg)
            except BaseException as exc:
                self.runtime_control.mark_blocked(str(exc))
                return
        self.runtime_control.mark_switching(self._reload_reason if self._reload_requested else None)

    def shutdown(self) -> None:
        return

def run_single_command_runtime(
    *,
    env_file: Path = Path('.env.dashboard'),
    host: str = '127.0.0.1',
    port: int = 8787,
) -> int:
    env_path = Path(env_file)
    stop_event = threading.Event()
    worker_errors: list[tuple[str, BaseException]] = []
    startup_error: BaseException | None = None
    interrupted = False

    try:
        startup_cfg = _load_shared_config(env_path)
    except BaseException as exc:
        print(f'Runtime startup failed: could not load config from {env_path}: {exc}')
        return 1

    manager = RuntimeManager(
        env_file=env_path,
        host=host,
        port=port,
        startup_cfg=startup_cfg,
        dashboard_runtime_factory=create_dashboard_runtime,
        validate_live_config=validate_live_runtime_config,
    )

    def _config_provider() -> AppConfig:
        return _load_shared_config(env_path)

    def _append_paper_worker_targets(
        base_cfg: AppConfig,
        worker_targets: list[tuple[str, object]],
        *,
        live_enabled: bool,
    ) -> None:
        paper_cfg = _cfg_for_active_mode(base_cfg, 'paper')
        if not hasattr(paper_cfg, '__dataclass_fields__') or not hasattr(paper_cfg, 'paper_profiles'):
            paper_kwargs = _build_worker_call_kwargs(
                run_paper_trading,
                stop_event=stop_event,
                config_provider=lambda: _cfg_for_active_mode(_config_provider(), 'paper'),
                runtime_control=_runtime_control_for_paper(
                    runtime_control=manager.runtime_control,
                    live_enabled=live_enabled,
                ),
                stop_when_safe=manager.restart_requested,
            )
            trader_target = lambda cfg=paper_cfg, paper_kwargs=paper_kwargs: run_paper_trading(cfg, **paper_kwargs)
            worker_targets.append(('paper-trading-worker', trader_target))
            return

        paper_timeframes = list(getattr(paper_cfg, 'paper_timeframes', []) or [paper_cfg.market_timeframe])
        manager.runtime_control.clear_paper_worker_states()
        paper_signature = inspect.signature(run_paper_trading)
        supports_state_path = 'state_path' in paper_signature.parameters
        supports_log_path = 'log_path' in paper_signature.parameters
        for timeframe in paper_timeframes:
            timeframe_cfg = _paper_cfg_for_timeframe(paper_cfg, timeframe)
            state_path, log_path = _paper_runtime_paths(paper_cfg, timeframe)
            paper_kwargs = _build_worker_call_kwargs(
                run_paper_trading,
                stop_event=stop_event,
                config_provider=lambda timeframe=timeframe: _paper_cfg_for_timeframe(
                    _cfg_for_active_mode(_config_provider(), 'paper'),
                    timeframe,
                ),
                runtime_control=_runtime_control_for_paper(
                    runtime_control=manager.runtime_control,
                    worker_key=timeframe,
                    live_enabled=live_enabled,
                ),
                stop_when_safe=manager.restart_requested,
            )
            if supports_state_path:
                paper_kwargs['state_path'] = state_path
            if supports_log_path:
                paper_kwargs['log_path'] = log_path
            trader_target = lambda timeframe_cfg=timeframe_cfg, paper_kwargs=paper_kwargs: run_paper_trading(timeframe_cfg, **paper_kwargs)
            worker_targets.append((f'paper-trading-worker-{timeframe}', trader_target))

    def _append_live_worker_targets(base_cfg: AppConfig, worker_targets: list[tuple[str, object]]) -> None:
        live_cfg = _cfg_for_active_mode(base_cfg, 'live')
        validate_live_runtime_config(live_cfg)
        live_kwargs = _build_worker_call_kwargs(
            run_live_trading,
            stop_event=stop_event,
            config_provider=lambda: _cfg_for_active_mode(_config_provider(), 'live'),
            runtime_control=manager.runtime_control,
            stop_when_safe=manager.restart_requested,
        )
        live_target = lambda cfg=live_cfg, live_kwargs=live_kwargs: run_live_trading(cfg, **live_kwargs)
        worker_targets.append(('live-trading-worker', live_target))

        redeem_kwargs = _build_worker_call_kwargs(
            run_live_redeem_worker,
            stop_event=stop_event,
            config_provider=lambda: _cfg_for_active_mode(_config_provider(), 'live'),
            runtime_control=_NullRuntimeControl(),
            stop_when_safe=manager.restart_requested,
        )
        redeem_target = lambda cfg=live_cfg, redeem_kwargs=redeem_kwargs: run_live_redeem_worker(cfg, **redeem_kwargs)
        worker_targets.append(('live-redeem-worker', redeem_target))

    dashboard_runtime = None
    dashboard_thread = None
    trader_threads: list[threading.Thread] = []
    printed_startup = False
    first_worker = True
    try:
        initial_mode = manager.snapshot().active_mode
        if initial_mode == 'live' or getattr(startup_cfg, 'live_trading_enabled', False):
            validate_live_runtime_config(_cfg_for_active_mode(startup_cfg, 'live'))
        dashboard_runtime = create_dashboard_runtime(
            host=host,
            port=port,
            env_file=env_path,
            running_trade_mode=initial_mode,
            runtime_control=manager.runtime_control,
            notify_mode_change=manager.request_mode_change,
            notify_runtime_reload=manager.request_runtime_reload,
        )
        dashboard_thread = _spawn_runtime_worker(
            name='dashboard-worker',
            target=dashboard_runtime.serve_forever,
            stop_event=stop_event,
            worker_errors=worker_errors,
        )

        while not stop_event.is_set():
            snapshot = manager.snapshot()
            active_mode = snapshot.active_mode
            base_cfg = startup_cfg if first_worker else _config_provider()
            first_worker = False
            worker_targets: list[tuple[str, object]] = []
            live_enabled = bool(getattr(base_cfg, 'live_trading_enabled', False) or active_mode == 'live')
            worker_supports_runtime_control = (
                'runtime_control' in inspect.signature(run_paper_trading).parameters
                or (live_enabled and 'runtime_control' in inspect.signature(run_live_trading).parameters)
            )
            _append_paper_worker_targets(base_cfg, worker_targets, live_enabled=live_enabled)
            if live_enabled:
                _append_live_worker_targets(base_cfg, worker_targets)

            startup_label = 'paper + live trading' if live_enabled else 'paper trading'
            trader_threads = [
                _spawn_runtime_worker(
                    name=name,
                    target=target,
                    stop_event=stop_event,
                    worker_errors=worker_errors,
                )
                for name, target in worker_targets
            ]

            if not printed_startup:
                print(f'Runtime started: {startup_label} + dashboard')
                print(f'Trade mode: {active_mode}')
                print(f'Dashboard URL: http://{host}:{port}/')
                printed_startup = True

            _wait_for_runtime_exit(
                stop_event=stop_event,
                dashboard_thread=dashboard_thread,
                worker_threads=trader_threads,
            )
            for trader_thread in trader_threads:
                trader_thread.join(timeout=10)

            if worker_errors or stop_event.is_set():
                break

            if worker_supports_runtime_control:
                try:
                    next_cfg = _config_provider()
                    manager.request_mode_change(_cfg_trade_mode(next_cfg, active_mode))
                    manager.poll_once()
                except BaseException as exc:
                    startup_error = exc
                    break

                snapshot = manager.snapshot()
                if snapshot.switch_state == 'switching':
                    manager.complete_runtime_reload()
                    manager.runtime_control.mark_active_mode(snapshot.desired_mode)
                    continue
            if not dashboard_thread.is_alive():
                break
            break
    except KeyboardInterrupt:
        interrupted = True
    except BaseException as exc:
        startup_error = exc
    finally:
        stop_event.set()
        if dashboard_runtime is not None:
            dashboard_runtime.shutdown()
        for trader_thread in trader_threads:
            trader_thread.join(timeout=10)
        if dashboard_thread is not None:
            dashboard_thread.join(timeout=10)
        if dashboard_runtime is not None:
            dashboard_runtime.close()

    if startup_error is not None:
        print(f'Runtime startup failed: {startup_error}')
        return 1

    if worker_errors:
        worker_name, exc = worker_errors[0]
        print(f'Runtime stopped due to {worker_name} failure: {exc}')
        return 1

    if interrupted:
        print('Runtime stopped.')
        return 0

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        raise SystemExit(2)
    return run_single_command_runtime(
        env_file=Path('.env.dashboard'),
        host='127.0.0.1',
        port=8787,
    )


if __name__ == '__main__':
    raise SystemExit(main())
