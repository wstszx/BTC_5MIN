from __future__ import annotations

import json
import subprocess
import sys
import threading
import textwrap
import time
from pathlib import Path

import atomic_io
import pytest
from models import PendingPaperTrade, SessionState, Strategy9SignalSample
from state_manager import load_session_state, save_session_state
from trader import load_session_state as trader_load_session_state
from trader import save_session_state as trader_save_session_state


def test_state_manager_loads_strategy_maps_without_trader_runtime_dependency(tmp_path: Path):
    state_path = tmp_path / "session_state.json"
    state_path.write_text(
        json.dumps(
            {
                "paper_strategies": {
                    "6": {
                        "round_index": 4,
                        "cash_pnl": 1.25,
                        "pending_paper_trades": [
                            {
                                "round_index": 4,
                                "event_slug": "btc-updown-5m-queued",
                                "start_time": "2026-04-30T01:00:00+00:00",
                                "end_time": "2026-04-30T01:05:00+00:00",
                                "side": "UP",
                                "price": 0.52,
                                "order_size": 2.0,
                                "order_cost": 1.04,
                                "expected_profit": 0.96,
                                "strategy": 6,
                                "entry_timing": "open",
                            }
                        ],
                    }
                },
                "live_strategies": {
                    "7": {
                        "round_index": 2,
                        "cash_pnl": -0.5,
                        "pending_live_slug": "btc-updown-5m-live",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    state = load_session_state(
        state_path,
        effective_paper_strategy_ids=[6, 8],
        effective_live_strategy_ids=[7, 3],
    )

    assert state.paper_strategies[6].pending_paper_trades == [
        PendingPaperTrade(
            round_index=4,
            event_slug="btc-updown-5m-queued",
            start_time="2026-04-30T01:00:00+00:00",
            end_time="2026-04-30T01:05:00+00:00",
            side="UP",
            price=0.52,
            order_size=2.0,
            order_cost=1.04,
            expected_profit=0.96,
            strategy=6,
            entry_timing="open",
        )
    ]
    assert state.paper_strategies[8].round_index == 0
    assert state.live_strategies[7].pending_live_slug == "btc-updown-5m-live"
    assert state.live_strategies[3].pending_live_slug is None


def test_trader_reexports_session_state_persistence_helpers():
    assert trader_load_session_state is load_session_state
    assert trader_save_session_state is save_session_state


def test_state_manager_save_session_state_roundtrips(tmp_path: Path):
    state_path = tmp_path / "session_state.json"

    save_session_state(
        state_path,
        SessionState(
            cash_pnl=1.25,
            strategy9_signal_samples=[
                Strategy9SignalSample(
                    observed_at="2026-04-30T01:00:00+00:00",
                    ofi_score=0.7,
                    momentum_delta=0.03,
                    current_up_price=0.53,
                )
            ],
        ),
    )

    loaded = load_session_state(state_path)

    assert loaded.cash_pnl == 1.25
    assert loaded.strategy9_signal_samples[0].ofi_score == 0.7


def test_state_manager_save_session_state_outwaits_long_windows_replace_denial(
    monkeypatch,
    tmp_path: Path,
):
    state_path = tmp_path / "session_state.json"
    attempts = 0
    original_replace = Path.replace

    def flaky_replace(self: Path, target: Path):
        nonlocal attempts
        attempts += 1
        if attempts <= 6:
            raise PermissionError(13, "Permission denied", str(target))
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    monkeypatch.setattr(atomic_io.time, "sleep", lambda _seconds: None)

    save_session_state(state_path, SessionState(cash_pnl=2.5))

    assert attempts == 7
    assert load_session_state(state_path).cash_pnl == 2.5


def test_load_session_state_waits_for_in_process_atomic_replace(monkeypatch, tmp_path: Path):
    state_path = tmp_path / "session_state.json"
    state_path.write_text(json.dumps({"cash_pnl": 0.0}), encoding="utf-8")
    original_replace = Path.replace
    replace_started = threading.Event()
    allow_replace = threading.Event()
    reader_done = threading.Event()
    writer_errors: list[BaseException] = []
    reader_values: list[float] = []

    def blocked_replace(self: Path, target: Path):
        if Path(target) == state_path:
            replace_started.set()
            assert allow_replace.wait(timeout=2.0)
        return original_replace(self, target)

    def write_state():
        try:
            save_session_state(state_path, SessionState(cash_pnl=9.0))
        except BaseException as exc:
            writer_errors.append(exc)

    def read_state():
        reader_values.append(load_session_state(state_path).cash_pnl)
        reader_done.set()

    monkeypatch.setattr(Path, "replace", blocked_replace)
    writer = threading.Thread(target=write_state)
    writer.start()
    assert replace_started.wait(timeout=2.0)
    reader = threading.Thread(target=read_state)
    reader.start()
    completed_while_replace_blocked = reader_done.wait(timeout=0.1)
    allow_replace.set()
    writer.join(timeout=2.0)
    reader.join(timeout=2.0)

    assert writer_errors == []
    assert completed_while_replace_blocked is False
    assert reader_values == [9.0]


def test_state_manager_save_waits_for_external_process_path_lock(tmp_path: Path):
    if sys.platform != "win32":
        pytest.skip("Windows file-lock behavior")

    state_path = tmp_path / "session_state.json"
    lock_path = tmp_path / ".session_state.json.lock"
    lock_acquired_path = tmp_path / "lock_acquired.txt"
    release_lock_path = tmp_path / "release_lock.txt"
    holder_code = textwrap.dedent(
        f"""
        import msvcrt
        from pathlib import Path
        import time

        lock_path = Path(r"{lock_path}")
        lock_acquired_path = Path(r"{lock_acquired_path}")
        release_lock_path = Path(r"{release_lock_path}")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            lock_acquired_path.write_text("1", encoding="utf-8")
            try:
                while not release_lock_path.exists():
                    time.sleep(0.01)
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        """
    )

    holder = subprocess.Popen([sys.executable, "-c", holder_code])
    try:
        deadline = time.monotonic() + 5.0
        while not lock_acquired_path.exists():
            assert holder.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)

        writer_done = threading.Event()
        writer_errors: list[BaseException] = []

        def write_state():
            try:
                save_session_state(state_path, SessionState(cash_pnl=4.0))
            except BaseException as exc:
                writer_errors.append(exc)
            finally:
                writer_done.set()

        writer = threading.Thread(target=write_state)
        writer.start()

        assert writer_done.wait(timeout=0.2) is False
        release_lock_path.write_text("1", encoding="utf-8")
        writer.join(timeout=5.0)
        assert writer_done.is_set()
    finally:
        release_lock_path.write_text("1", encoding="utf-8")
        holder.wait(timeout=5.0)

    assert writer_errors == []
    assert load_session_state(state_path).cash_pnl == 4.0
