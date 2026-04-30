from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models import TradeRecord


def row_has_result(row: dict[str, Any]) -> bool:
    result = str(row.get("result") or "").strip()
    return bool(result and result != "--")


def live_trade_log_upsert_key(row: dict[str, Any]) -> tuple[str, str] | None:
    if str(row.get("mode") or "").strip().lower() != "live":
        return None
    strategy = str(row.get("strategy") or "").strip()
    event_slug = str(row.get("event_slug") or "").strip()
    if not strategy or not event_slug:
        return None
    return strategy, event_slug


def merge_live_trade_log_rows(existing: dict[str, str], incoming: dict[str, Any], fieldnames: list[str]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(existing)
    for field_name in fieldnames:
        incoming_value = incoming.get(field_name)
        if incoming_value in (None, ""):
            continue
        if field_name in {"timestamp", "round_index"} and str(existing.get(field_name) or "").strip():
            continue
        merged[field_name] = incoming_value
    return merged


def append_trade_log(path: Path, record: TradeRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = asdict(record)
    row["timestamp"] = record.timestamp.isoformat()
    row["start_time"] = record.start_time.isoformat()
    row["end_time"] = record.end_time.isoformat()
    fieldnames = list(row.keys())

    write_header = not path.exists()
    if path.exists():
        with path.open("r", newline="", encoding="utf-8") as handle:
            existing_header = next(csv.reader(handle), [])
        if existing_header and existing_header != fieldnames:
            legacy_path = path.with_name(f"{path.stem}_legacy_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}{path.suffix}")
            path.replace(legacy_path)
            write_header = True

    upsert_key = live_trade_log_upsert_key(row) if row_has_result(row) else None
    if upsert_key is not None and path.exists() and not write_header:
        with path.open("r", newline="", encoding="utf-8") as handle:
            existing_rows = list(csv.DictReader(handle))
        for index in range(len(existing_rows) - 1, -1, -1):
            existing_row = existing_rows[index]
            if live_trade_log_upsert_key(existing_row) != upsert_key:
                continue
            if row_has_result(existing_row):
                continue
            existing_rows[index] = merge_live_trade_log_rows(existing_row, row, fieldnames)
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(existing_rows)
            return

    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
