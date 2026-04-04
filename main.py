from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Sequence

from config import AppConfig, build_config_from_env_values, load_env_file_values
from dashboard import create_dashboard_runtime
from trader import run_paper_trading


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
    trader_thread: threading.Thread,
) -> None:
    while True:
        if stop_event.is_set():
            return
        if not dashboard_thread.is_alive() or not trader_thread.is_alive():
            return
        time.sleep(0.1)


def run_single_command_runtime(
    *,
    env_file: Path = Path(".env.dashboard"),
    host: str = "127.0.0.1",
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
        print(f"Runtime startup failed: could not load config from {env_path}: {exc}")
        return 1

    def _config_provider() -> AppConfig:
        return _load_shared_config(env_path)

    dashboard_runtime = None
    dashboard_thread = None
    trader_thread = None
    try:
        dashboard_runtime = create_dashboard_runtime(host=host, port=port, env_file=env_path)
        trader_thread = _spawn_runtime_worker(
            name="paper-trading-worker",
            target=lambda: run_paper_trading(
                startup_cfg,
                stop_event=stop_event,
                config_provider=_config_provider,
            ),
            stop_event=stop_event,
            worker_errors=worker_errors,
        )
        dashboard_thread = _spawn_runtime_worker(
            name="dashboard-worker",
            target=dashboard_runtime.serve_forever,
            stop_event=stop_event,
            worker_errors=worker_errors,
        )

        print("Runtime started: paper trading + dashboard")
        print(f"Dashboard URL: http://{host}:{port}/")

        _wait_for_runtime_exit(
            stop_event=stop_event,
            dashboard_thread=dashboard_thread,
            trader_thread=trader_thread,
        )
    except KeyboardInterrupt:
        interrupted = True
    except BaseException as exc:
        startup_error = exc
    finally:
        stop_event.set()
        if dashboard_runtime is not None:
            dashboard_runtime.shutdown()
        if trader_thread is not None:
            trader_thread.join(timeout=10)
        if dashboard_thread is not None:
            dashboard_thread.join(timeout=10)
        if dashboard_runtime is not None:
            dashboard_runtime.close()

    if startup_error is not None:
        print(f"Runtime startup failed: {startup_error}")
        return 1

    if worker_errors:
        worker_name, exc = worker_errors[0]
        print(f"Runtime stopped due to {worker_name} failure: {exc}")
        return 1

    if interrupted:
        print("Runtime stopped.")
        return 0

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv or [])
    if args:
        raise SystemExit(2)
    return run_single_command_runtime(
        env_file=Path(".env.dashboard"),
        host="127.0.0.1",
        port=8787,
    )


if __name__ == "__main__":
    raise SystemExit(main())
