from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models import TradeRecord


OBSERVED_WAITING_SKIP_REASON = "observed_waiting_for_entry"
_OBSERVED_PLACEHOLDER_FIELDS = {
    "side",
    "price",
    "order_size",
    "order_cost",
    "expected_profit",
    "trade_pnl",
    "skip_reason",
    "stop_loss_triggered",
    "signal_open_up_price",
    "signal_current_up_price",
    "signal_threshold",
    "signal_delta",
    "signal_probability",
    "signal_edge",
    "signal_locked",
    "signal_reason",
    "order_id",
    "fill_source",
    "raw_price",
    "raw_order_cost",
    "fee",
    "effective_price_with_fee",
    "effective_order_cost_with_fee",
}

_LIVE_FILL_SOURCE_MARKERS = {
    "official_fill_price_below_min_entry",
    "official_fill_price_below_decision_floor",
    "official_fill_price_above_max_entry_price",
}

_MIGRATABLE_HEADER_REQUIRED_FIELDS = {
    "timestamp",
    "mode",
    "round_index",
    "strategy",
    "event_slug",
    "start_time",
    "end_time",
}


def row_has_result(row: dict[str, Any]) -> bool:
    result = str(row.get("result") or "").strip()
    return bool(result and result != "--")


def _positive_number(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def live_row_is_observed_placeholder(row: dict[str, Any]) -> bool:
    return str(row.get("skip_reason") or "").strip() == OBSERVED_WAITING_SKIP_REASON


def live_row_is_actionable_update(row: dict[str, Any]) -> bool:
    skip_reason = str(row.get("skip_reason") or "").strip()
    if row_has_result(row):
        return True
    if skip_reason and skip_reason != OBSERVED_WAITING_SKIP_REASON:
        return True
    return _positive_number(row.get("order_cost")) or _positive_number(row.get("order_size"))


def _mode_trade_log_upsert_key(row: dict[str, Any], mode: str) -> tuple[str, str, str] | None:
    if str(row.get("mode") or "").strip().lower() != mode:
        return None
    strategy = str(row.get("strategy") or "").strip()
    event_slug = str(row.get("event_slug") or "").strip()
    if not strategy or not event_slug:
        return None
    experiment_id = str(row.get("experiment_id") or "").strip()
    return strategy, event_slug, experiment_id


def live_trade_log_upsert_key(row: dict[str, Any]) -> tuple[str, str, str] | None:
    return _mode_trade_log_upsert_key(row, "live")


def paper_trade_log_upsert_key(row: dict[str, Any]) -> tuple[str, str, str] | None:
    return _mode_trade_log_upsert_key(row, "paper")


def merge_live_trade_log_rows(existing: dict[str, str], incoming: dict[str, Any], fieldnames: list[str]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(existing)
    keep_observed_placeholder = live_row_is_observed_placeholder(existing) and not live_row_is_actionable_update(incoming)
    for field_name in fieldnames:
        incoming_value = incoming.get(field_name)
        if keep_observed_placeholder and field_name in _OBSERVED_PLACEHOLDER_FIELDS:
            continue
        if field_name == "skip_reason" and incoming_value in (None, ""):
            merged[field_name] = ""
            continue
        if (
            field_name == "fill_source"
            and str(existing.get(field_name) or "").strip() in _LIVE_FILL_SOURCE_MARKERS
            and str(incoming_value or "").strip() == "official_confirmed_trade"
        ):
            continue
        if incoming_value in (None, ""):
            continue
        if field_name in {"timestamp", "round_index"} and str(existing.get(field_name) or "").strip():
            continue
        merged[field_name] = incoming_value
    return merged


def _expire_prior_live_observed_placeholders(rows: list[dict[str, Any]], incoming: dict[str, Any]) -> bool:
    incoming_key = live_trade_log_upsert_key(incoming)
    if incoming_key is None:
        return False
    incoming_strategy, incoming_event_slug, incoming_experiment_id = incoming_key
    if str(incoming.get("skip_reason") or "").strip() == OBSERVED_WAITING_SKIP_REASON:
        return False
    changed = False
    for row in rows:
        if not live_row_is_observed_placeholder(row):
            continue
        row_key = live_trade_log_upsert_key(row)
        if row_key is None:
            continue
        strategy, event_slug, experiment_id = row_key
        if strategy != incoming_strategy or experiment_id != incoming_experiment_id:
            continue
        if event_slug == incoming_event_slug:
            continue
        row["skip_reason"] = "entry_window_missed"
        changed = True
    return changed


def merge_paper_trade_log_rows(existing: dict[str, str], incoming: dict[str, Any], fieldnames: list[str]) -> dict[str, Any] | None:
    existing_actionable = row_has_result(existing) or _positive_number(existing.get("order_cost")) or _positive_number(existing.get("order_size"))
    incoming_actionable = row_has_result(incoming) or _positive_number(incoming.get("order_cost")) or _positive_number(incoming.get("order_size"))
    if existing_actionable and not incoming_actionable:
        return None
    return {field_name: incoming.get(field_name, "") for field_name in fieldnames}


def _can_migrate_header(existing_header: list[str], fieldnames: list[str]) -> bool:
    if not existing_header:
        return False
    if len(existing_header) > len(fieldnames):
        return False
    if not _MIGRATABLE_HEADER_REQUIRED_FIELDS.issubset(set(existing_header)):
        return False
    field_index = 0
    for existing_field in existing_header:
        try:
            field_index = fieldnames.index(existing_field, field_index) + 1
        except ValueError:
            return False
    return True


def _rewrite_with_fieldnames(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _rows_with_fieldnames(rows: list[dict[str, Any]], fieldnames: list[str]) -> list[dict[str, Any]]:
    return [{field_name: row.get(field_name, "") for field_name in fieldnames} for row in rows]


def append_trade_log(path: Path, record: TradeRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = asdict(record)
    if row.get("effective_price_with_fee") in (None, ""):
        row["effective_price_with_fee"] = row.get("price")
    if row.get("effective_order_cost_with_fee") in (None, ""):
        row["effective_order_cost_with_fee"] = row.get("order_cost")
    row["timestamp"] = record.timestamp.isoformat()
    row["start_time"] = record.start_time.isoformat()
    row["end_time"] = record.end_time.isoformat()
    if record.entry_time is not None:
        row["entry_time"] = record.entry_time.isoformat()
    fieldnames = list(row.keys())

    write_header = not path.exists()
    if path.exists():
        with path.open("r", newline="", encoding="utf-8") as handle:
            existing_header = next(csv.reader(handle), [])
        if existing_header and existing_header != fieldnames:
            if _can_migrate_header(existing_header, fieldnames):
                with path.open("r", newline="", encoding="utf-8") as handle:
                    existing_rows = list(csv.DictReader(handle))
                _rewrite_with_fieldnames(path, existing_rows, fieldnames)
            else:
                with path.open("r", newline="", encoding="utf-8") as handle:
                    existing_rows = list(csv.DictReader(handle))
                legacy_path = path.with_name(f"{path.stem}_legacy_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}{path.suffix}")
                try:
                    path.replace(legacy_path)
                    write_header = True
                except PermissionError:
                    _rewrite_with_fieldnames(path, _rows_with_fieldnames(existing_rows, fieldnames), fieldnames)
                    write_header = False

    upsert_key = live_trade_log_upsert_key(row)
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
            _rewrite_with_fieldnames(path, existing_rows, fieldnames)
            return
        if _expire_prior_live_observed_placeholders(existing_rows, row):
            existing_rows.append(row)
            _rewrite_with_fieldnames(path, existing_rows, fieldnames)
            return

    upsert_key = paper_trade_log_upsert_key(row)
    if upsert_key is not None and path.exists() and not write_header:
        with path.open("r", newline="", encoding="utf-8") as handle:
            existing_rows = list(csv.DictReader(handle))
        for index in range(len(existing_rows) - 1, -1, -1):
            existing_row = existing_rows[index]
            if paper_trade_log_upsert_key(existing_row) != upsert_key:
                continue
            merged_row = merge_paper_trade_log_rows(existing_row, row, fieldnames)
            if merged_row is None:
                return
            existing_rows[index] = merged_row
            _rewrite_with_fieldnames(path, existing_rows, fieldnames)
            return

    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
