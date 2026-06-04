from __future__ import annotations

import os
import threading
from contextlib import contextmanager
import stat
import time
from pathlib import Path
from typing import Iterator
from uuid import uuid4


_WINDOWS_REPLACE_RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.4, 0.8, 1.2, 1.8, 2.5)
_INTERPROCESS_LOCK_POLL_SECONDS = 0.05
_INTERPROCESS_LOCK_TIMEOUT_SECONDS = 30.0
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}
_THREAD_LOCK_DEPTHS = threading.local()


def _lock_key(path: Path) -> Path:
    return Path(path).resolve(strict=False)


def _path_lock(path: Path) -> threading.RLock:
    key = _lock_key(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _thread_lock_depths() -> dict[Path, int]:
    depths = getattr(_THREAD_LOCK_DEPTHS, "depths", None)
    if depths is None:
        depths = {}
        _THREAD_LOCK_DEPTHS.depths = depths
    return depths


def _interprocess_lock_path(path: Path) -> Path:
    target = Path(path)
    return target.with_name(f".{target.name}.lock")


def _ensure_lock_byte(handle) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)


def _acquire_windows_lock(handle, lock_path: Path) -> None:
    import msvcrt

    deadline = time.monotonic() + _INTERPROCESS_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for state file lock: {lock_path}") from None
            time.sleep(_INTERPROCESS_LOCK_POLL_SECONDS)


def _release_windows_lock(handle) -> None:
    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _acquire_posix_lock(handle, lock_path: Path) -> None:
    import fcntl

    deadline = time.monotonic() + _INTERPROCESS_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for state file lock: {lock_path}") from None
            time.sleep(_INTERPROCESS_LOCK_POLL_SECONDS)


def _release_posix_lock(handle) -> None:
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _interprocess_path_guard(path: Path) -> Iterator[None]:
    key = _lock_key(path)
    depths = _thread_lock_depths()
    depth = depths.get(key, 0)
    if depth:
        depths[key] = depth + 1
        try:
            yield
        finally:
            if depths[key] == 1:
                depths.pop(key, None)
            else:
                depths[key] -= 1
        return

    lock_path = _interprocess_lock_path(key)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        _ensure_lock_byte(handle)
        if os.name == "nt":
            _acquire_windows_lock(handle, lock_path)
        else:
            _acquire_posix_lock(handle, lock_path)
        depths[key] = 1
        try:
            yield
        finally:
            depths.pop(key, None)
            handle.seek(0)
            if os.name == "nt":
                _release_windows_lock(handle)
            else:
                _release_posix_lock(handle)


@contextmanager
def atomic_path_guard(path: Path) -> Iterator[None]:
    lock = _path_lock(path)
    with lock:
        with _interprocess_path_guard(path):
            yield


def _replace_with_retry(source: Path, target: Path) -> None:
    last_error: PermissionError | None = None
    for delay in (0.0, *_WINDOWS_REPLACE_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            source.replace(target)
            return
        except PermissionError as exc:
            last_error = exc
            try:
                if target.exists():
                    target.chmod(target.stat().st_mode | stat.S_IWRITE)
            except OSError:
                pass
    assert last_error is not None
    raise last_error


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    target = Path(path)
    with atomic_path_guard(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temp_path.write_text(text, encoding=encoding)
            _replace_with_retry(temp_path, target)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
