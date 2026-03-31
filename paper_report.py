from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    normalized = str(value).strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def parse_utc_offset(offset: str) -> timezone:
    raw = (offset or "").strip()
    if len(raw) != 6 or raw[0] not in {"+", "-"} or raw[3] != ":":
        raise ValueError(f"Invalid UTC offset format: {offset}. Expected format like +08:00.")
    sign = 1 if raw[0] == "+" else -1
    try:
        hours = int(raw[1:3])
        minutes = int(raw[4:6])
    except ValueError as exc:
        raise ValueError(f"Invalid UTC offset: {offset}") from exc
    if hours > 23 or minutes > 59:
        raise ValueError(f"Invalid UTC offset: {offset}")
    delta = timedelta(hours=hours, minutes=minutes) * sign
    return timezone(delta)


@dataclass(slots=True)
class DailyPaperSummary:
    date: str
    rows: int
    trade_rows: int
    skip_rows: int
    wins: int
    losses: int
    hit_rate: float
    total_pnl: float
    avg_trade_pnl: float
    max_drawdown: float
    signal_rows: int
    avg_abs_signal_delta: float
    strong_signal_rate: float
    signal_locked_rate: float
    skip_reason_counts: dict[str, int]


def summarize_paper_trades(
    csv_path: Path,
    *,
    tz_offset: str = "+08:00",
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[DailyPaperSummary]:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    tzinfo = parse_utc_offset(tz_offset)
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return []

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        ts_raw = row.get("timestamp")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            continue
        local_day = ts.astimezone(tzinfo).date().isoformat()
        if start_date and local_day < start_date:
            continue
        if end_date and local_day > end_date:
            continue
        grouped[local_day].append(row)

    summaries: list[DailyPaperSummary] = []
    for day in sorted(grouped.keys()):
        day_rows = sorted(grouped[day], key=lambda item: item.get("timestamp") or "")
        rows_count = 0
        trade_rows = 0
        skip_rows = 0
        wins = 0
        losses = 0
        trade_pnls: list[float] = []
        signal_abs_deltas: list[float] = []
        strong_signal_count = 0
        signal_locked_count = 0
        skip_reason_counts: Counter[str] = Counter()

        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0

        for row in day_rows:
            rows_count += 1
            trade_pnl = _optional_float(row.get("trade_pnl")) or 0.0
            cumulative += trade_pnl
            peak = max(peak, cumulative)
            max_drawdown = max(max_drawdown, peak - cumulative)

            side = (row.get("side") or "").strip().upper()
            result = (row.get("result") or "").strip().upper()
            skip_reason = (row.get("skip_reason") or "").strip()
            if skip_reason:
                skip_reason_counts[skip_reason] += 1

            if result in {"UP", "DOWN"}:
                trade_rows += 1
                trade_pnls.append(trade_pnl)
                if side == result:
                    wins += 1
                else:
                    losses += 1
            elif skip_reason or side == "SKIP":
                skip_rows += 1

            signal_delta = _optional_float(row.get("signal_delta"))
            signal_threshold = _optional_float(row.get("signal_threshold"))
            if signal_delta is not None:
                signal_abs_deltas.append(abs(signal_delta))
                if signal_threshold is not None and abs(signal_delta) >= signal_threshold:
                    strong_signal_count += 1
            if _parse_bool(row.get("signal_locked")):
                signal_locked_count += 1

        total_pnl = sum(_optional_float(row.get("trade_pnl")) or 0.0 for row in day_rows)
        hit_rate = (wins / trade_rows) if trade_rows else 0.0
        avg_trade_pnl = (sum(trade_pnls) / trade_rows) if trade_rows else 0.0
        signal_rows = len(signal_abs_deltas)
        avg_abs_signal_delta = (sum(signal_abs_deltas) / signal_rows) if signal_rows else 0.0
        strong_signal_rate = (strong_signal_count / signal_rows) if signal_rows else 0.0
        signal_locked_rate = (signal_locked_count / rows_count) if rows_count else 0.0

        summaries.append(
            DailyPaperSummary(
                date=day,
                rows=rows_count,
                trade_rows=trade_rows,
                skip_rows=skip_rows,
                wins=wins,
                losses=losses,
                hit_rate=hit_rate,
                total_pnl=total_pnl,
                avg_trade_pnl=avg_trade_pnl,
                max_drawdown=max_drawdown,
                signal_rows=signal_rows,
                avg_abs_signal_delta=avg_abs_signal_delta,
                strong_signal_rate=strong_signal_rate,
                signal_locked_rate=signal_locked_rate,
                skip_reason_counts=dict(skip_reason_counts),
            )
        )

    return summaries
