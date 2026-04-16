from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    train_start: int
    train_end: int
    validation_start: int
    validation_end: int


def build_walk_forward_windows(
    rows: Sequence[dict[str, Any]],
    *,
    train_size: int,
    validation_size: int,
    step_size: int,
) -> list[WalkForwardWindow]:
    if train_size <= 0:
        raise ValueError("train_size must be > 0")
    if validation_size <= 0:
        raise ValueError("validation_size must be > 0")
    if step_size <= 0:
        raise ValueError("step_size must be > 0")

    total_rows = len(rows)
    windows: list[WalkForwardWindow] = []
    train_start = 0

    while True:
        train_end = train_start + train_size
        validation_start = train_end
        validation_end = validation_start + validation_size
        if validation_end > total_rows:
            break
        windows.append(
            WalkForwardWindow(
                train_start=train_start,
                train_end=train_end,
                validation_start=validation_start,
                validation_end=validation_end,
            )
        )
        train_start += step_size

    return windows
