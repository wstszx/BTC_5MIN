from __future__ import annotations

from pathlib import Path
from uuid import uuid4


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        temp_path.write_text(text, encoding=encoding)
        temp_path.replace(target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
