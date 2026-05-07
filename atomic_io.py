from __future__ import annotations

import stat
import time
from pathlib import Path
from uuid import uuid4


_WINDOWS_REPLACE_RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.4)


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
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        temp_path.write_text(text, encoding=encoding)
        _replace_with_retry(temp_path, target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
