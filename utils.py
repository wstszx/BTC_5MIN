from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable


def _is_stop_requested(stop_event: threading.Event | None) -> bool:
    return bool(stop_event and stop_event.is_set())


def _sleep_if_not_stopped(stop_event: threading.Event | None, seconds: float) -> bool:
    if _is_stop_requested(stop_event):
        return False
    if stop_event is not None:
        # Yield to other threads before waiting
        time.sleep(0)
        if _is_stop_requested(stop_event):
            return False
        return not stop_event.wait(max(0.0, seconds))
    time.sleep(seconds)
    return not _is_stop_requested(stop_event)


def _safe_stop_requested(stop_when_safe: Callable[[], bool] | None) -> bool:
    return bool(stop_when_safe and stop_when_safe())


def _runtime_log(message: str) -> None:
    print('[' + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S') + ' UTC] ' + message, flush=True)
