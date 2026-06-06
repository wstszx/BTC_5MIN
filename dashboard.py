from __future__ import annotations

import csv
import errno
import json
import os
import re
import shutil
import tempfile
import threading
from collections import deque
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from atomic_io import atomic_path_guard, atomic_write_text
from clob_adapter import (
    create_live_clob_client as _create_live_clob_client,
    read_available_live_balance as _read_available_live_balance,
)
from config import (
    AppConfig,
    MARKET_TIMEFRAME_DEFINITIONS,
    build_config_from_env_values,
    collect_config_warnings,
    canonical_strategy_profile_base_key,
    display_strategy_profile_base_key,
    load_env_file_values,
    LIVE_STRATEGY_IDS,
    PAPER_STRATEGY_IDS,
    STRATEGY_IDS,
)
from binance_signal import BinanceDepth5SignalService
from models import LiveStrategyState, MarketWindow, PaperStrategyState, PendingPaperTrade
from paper_report import summarize_paper_trades, summarize_paper_trades_by_strategy
from polymarket_api import (
    PolymarketClient,
    extract_token_ids,
    normalize_outcome_label,
    parse_outcome_prices,
)
from settlement import (
    live_market_waits_for_final_price,
    PROVISIONAL_LOSS_RESULT,
    resolved_result_from_redeemable_positions,
    resolved_live_result_from_official_sources,
)
from risk_and_sizing import build_trade_plan
from strategy import get_side_for_round
from strategy_decision import effective_decision_order_cost_multiplier as _effective_decision_order_cost_multiplier
from runtime_control import RuntimeControl
from runtime_helpers import session_day_key
from state_manager import load_session_state, save_session_state
from trader import (
    _entry_window_missed,
    _entry_time_for_round,
    _resolve_side_from_strategy,
    _apply_strategy6_signal_to_quote,
    _cfg_for_paper_strategy,
    _is_strategy6_signal_stale,
    _ws_is_stale_for_trade,
    resolve_quote_price,
)
from runtime_config import (
    cfg_for_live_strategy as _cfg_for_live_strategy,
    cfg_for_paper_strategy as _runtime_cfg_for_paper_strategy,
    live_strategy_ids_for_runtime as _live_strategy_ids_for_runtime,
    paper_strategy_ids_for_runtime as _paper_strategy_ids_for_runtime,
    validate_live_runtime_config,
)


_POLYMARKET_CLIENT_CLASS = PolymarketClient


def _select_display_round(
    *,
    current_round: MarketWindow | None,
    next_round: MarketWindow | None,
) -> MarketWindow | None:
    if current_round is not None:
        return current_round
    return next_round


def _fmt_env(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = [f"{key}={values[key]}" for key in sorted(values.keys())]
    text = "\n".join(lines)
    if text:
        text += "\n"
    atomic_write_text(path, text, encoding="utf-8")


SUPPORTED_STRATEGY_ID_TEXTS: set[str] = {str(strategy_id) for strategy_id in range(1, 13)}
SUPPORTED_STRATEGY_SELECT_OPTIONS: list[str] = [str(strategy_id) for strategy_id in range(1, 13)]


def _normalize_strategy_id_list_for_key(value: str, key: str, attr_name: str) -> str:
    cfg = build_config_from_env_values({key: value})
    raw = [item.strip() for item in str(value).split(',') if item.strip()]
    normalized_ids = list(getattr(cfg, attr_name))
    if raw and len(normalized_ids) == 1 and normalized_ids[0] == cfg.strategy_id:
        has_valid = any(item in SUPPORTED_STRATEGY_ID_TEXTS for item in raw)
        if not has_valid:
            raise ValueError(f"Invalid value for {key}: expected comma-separated strategy ids 1-12, got {value!r}")
    normalized = [str(item) for item in normalized_ids]
    if not normalized:
        raise ValueError(f"Invalid value for {key}: expected comma-separated strategy ids 1-12, got {value!r}")
    return ",".join(normalized)


def _normalize_unified_strategy_id_list_value(value: str) -> str:
    return _normalize_strategy_id_list_for_key(value, STRATEGY_IDS, "strategy_ids")


def _normalize_strategy_id_list_value(value: str) -> str:
    return _normalize_strategy_id_list_for_key(value, PAPER_STRATEGY_IDS, "paper_strategy_ids")


def _normalize_live_strategy_id_list_value(value: str) -> str:
    return _normalize_strategy_id_list_for_key(value, LIVE_STRATEGY_IDS, "live_strategy_ids")

def _tail_csv_rows(path: Path, *, limit: int) -> list[dict[str, str]]:
    if limit <= 0 or not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        buffer: deque[dict[str, str]] = deque(maxlen=limit)
        for row in reader:
            buffer.append(row)
    rows = list(buffer)
    rows.reverse()
    return rows


def _all_csv_rows_newest_first(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    rows.reverse()
    return rows


def _normalize_strategy_filter(strategy: int | str | None) -> str | None:
    if strategy is None:
        return None
    raw = str(strategy).strip().lower()
    if raw in {"", "all"}:
        return None
    if raw in SUPPORTED_STRATEGY_ID_TEXTS:
        return raw
    raise ValueError(f"Invalid strategy filter: {strategy!r}")


def _is_explicit_all_strategy_filter(strategy: int | str | None) -> bool:
    return strategy is not None and str(strategy).strip().lower() == "all"


def _filter_trade_rows_by_strategy(rows: list[dict[str, str]], strategy: int | str | None) -> list[dict[str, str]]:
    strategy_filter = _normalize_strategy_filter(strategy)
    if strategy_filter is None:
        return list(rows)
    return [row for row in rows if str(row.get("strategy") or "").strip() == strategy_filter]


def _filter_trade_rows_by_strategy_ids(rows: list[dict[str, str]], strategy_ids: list[int] | set[int] | tuple[int, ...]) -> list[dict[str, str]]:
    strategy_texts = {str(item) for item in strategy_ids}
    if not strategy_texts:
        return list(rows)
    return [row for row in rows if str(row.get("strategy") or "").strip() in strategy_texts]


def _filter_pending_paper_trades_by_strategy(items: list[PendingPaperTrade], strategy: int | str | None) -> list[PendingPaperTrade]:
    strategy_filter = _normalize_strategy_filter(strategy)
    if strategy_filter is None:
        return list(items)
    return [item for item in items if str(item.strategy) == strategy_filter]


def _filter_pending_paper_trades_by_strategy_ids(
    items: list[PendingPaperTrade],
    strategy_ids: list[int] | set[int] | tuple[int, ...],
) -> list[PendingPaperTrade]:
    strategy_texts = {str(item) for item in strategy_ids}
    if not strategy_texts:
        return list(items)
    return [item for item in items if str(item.strategy) in strategy_texts]


def _has_explicit_live_strategy_scope(env_values: dict[str, str]) -> bool:
    return any(str(env_values.get(key) or "").strip() for key in (STRATEGY_IDS, LIVE_STRATEGY_IDS))


def _has_explicit_paper_strategy_scope(env_values: dict[str, str], timeframe: str) -> bool:
    normalized_timeframe = _normalize_timeframe_filter(timeframe)
    timeframe_prefix = normalized_timeframe.upper().replace("M", "M")
    return any(
        str(env_values.get(key) or "").strip()
        for key in (
            STRATEGY_IDS,
            PAPER_STRATEGY_IDS,
            f"PAPER_{timeframe_prefix}_STRATEGY_IDS",
        )
    )


def _recent_row_has_result(row: dict[str, str]) -> bool:
    result = str(row.get("result") or "").strip()
    return bool(result and result != "--")


def _live_recent_merge_key(row: dict[str, str]) -> tuple[str, str] | None:
    if str(row.get("mode") or "").strip().lower() != "live":
        return None
    strategy = str(row.get("strategy") or "").strip()
    event_slug = str(row.get("event_slug") or "").strip()
    if not strategy or not event_slug:
        return None
    return strategy, event_slug


def _merge_recent_live_rows(entry_row: dict[str, str], settlement_row: dict[str, str]) -> dict[str, str]:
    merged = dict(entry_row)
    for key, value in settlement_row.items():
        if value in (None, ""):
            continue
        if key in {"timestamp", "round_index"} and str(entry_row.get(key) or "").strip():
            continue
        merged[key] = value
    return merged


def _collapse_live_recent_rows(rows: list[dict[str, str]], timeframe: str | None = None) -> list[dict[str, str]]:
    collapsed: list[dict[str, str]] = []
    unresolved_index_by_key: dict[tuple[str, str], int] = {}
    for row in reversed(rows):
        row_copy = dict(row)
        key = _live_recent_merge_key(row_copy)
        if key is not None and _recent_row_has_result(row_copy) and key in unresolved_index_by_key:
            unresolved_index = unresolved_index_by_key.pop(key)
            collapsed[unresolved_index] = _merge_recent_live_rows(collapsed[unresolved_index], row_copy)
            continue
        collapsed.append(row_copy)
        if key is not None and not _recent_row_has_result(row_copy):
            unresolved_index_by_key[key] = len(collapsed) - 1
    _sort_recent_rows_by_round(collapsed, timeframe)
    return collapsed


def _backfill_strategy_price_skip_price(row: dict[str, str]) -> dict[str, str]:
    if str(row.get("price") or "").strip():
        return row
    skip_reason = str(row.get("skip_reason") or "").strip()
    if skip_reason not in {
        "strategy7_price_too_low",
        "strategy7_price_too_high",
        "strategy8_price_too_low",
        "strategy8_price_too_high",
    }:
        return row
    current_up_price = _optional_float(row.get("signal_current_up_price"))
    signal_delta = _optional_float(row.get("signal_delta"))
    if current_up_price is None or signal_delta is None:
        return row
    candidate_price = current_up_price if signal_delta > 0 else 1 - current_up_price
    updated = dict(row)
    updated["price"] = str(candidate_price)
    return updated


def _backfill_recent_strategy_price_skip_prices(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [_backfill_strategy_price_skip_price(row) for row in rows]


def _fmt_price_check_value(value: float | None) -> str:
    return "--" if value is None else f"{value:.4f}"


def _live_recent_price_check(row: dict[str, str], cfg: AppConfig) -> dict[str, str]:
    checked = dict(row)
    checked.setdefault("price_check_status", "")
    checked.setdefault("price_check_label", "")
    checked.setdefault("price_check_detail", "")
    side = str(checked.get("side") or "").strip().upper()
    if side not in {"UP", "DOWN"} or str(checked.get("skip_reason") or "").strip():
        return checked
    order_cost = _optional_float(checked.get("order_cost")) or 0.0
    order_size = _optional_float(checked.get("order_size")) or 0.0
    if order_cost <= 0.0 or order_size <= 0.0:
        return checked

    raw_price = _optional_float(checked.get("raw_price"))
    if raw_price is None:
        raw_price = _optional_float(checked.get("price"))
    if raw_price is None:
        return checked
    strategy_text = str(checked.get("strategy") or "").strip()
    try:
        strategy_cfg = _cfg_for_live_strategy(cfg, int(strategy_text))
    except (TypeError, ValueError):
        strategy_cfg = cfg
    min_entry_price = getattr(strategy_cfg, "min_entry_price", None)
    max_entry_price = getattr(strategy_cfg, "max_entry_price", None)
    if max_entry_price is not None and raw_price > float(max_entry_price) + 1e-9:
        checked["price_check_status"] = "above_max"
        checked["price_check_label"] = "高于价格上限"
        checked["price_check_detail"] = (
            f"成交原始价 {_fmt_price_check_value(raw_price)} "
            f"高于最高入场价 {_fmt_price_check_value(float(max_entry_price))}"
        )
        return checked
    if min_entry_price is not None and raw_price < float(min_entry_price) - 1e-9:
        checked["price_check_status"] = "improved"
        checked["price_check_label"] = "价格改善"
        checked["price_check_detail"] = (
            f"成交原始价 {_fmt_price_check_value(raw_price)} "
            f"低于最低入场价 {_fmt_price_check_value(float(min_entry_price))}，按价格改善处理"
        )
        return checked
    checked["price_check_status"] = "ok"
    checked["price_check_detail"] = "成交原始价在策略入场区间内"
    return checked


def _with_live_recent_price_checks(rows: list[dict[str, str]], cfg: AppConfig) -> list[dict[str, str]]:
    return [_live_recent_price_check(row, cfg) for row in rows]


def _normalize_timeframe_filter(timeframe: str | None, *, fallback: str = "5m") -> str:
    raw = str(timeframe or fallback).strip().lower()
    return raw if raw in {"5m", "15m"} else fallback


def _paper_runtime_dir(cfg: AppConfig, timeframe: str) -> Path:
    return cfg.logs_dir / "paper" / timeframe


def _paper_session_state_path(cfg: AppConfig, timeframe: str) -> Path:
    return _paper_runtime_dir(cfg, timeframe) / "session_state.json"


def _paper_trades_path(cfg: AppConfig, timeframe: str) -> Path:
    return _paper_runtime_dir(cfg, timeframe) / "paper_trades.csv"


SUPPORTED_PAPER_TIMEFRAMES: tuple[str, ...] = ("5m", "15m")
PAPER_PROFILE_EDITABLE_FIELDS: tuple[str, ...] = (
    "STRATEGY_ID",
    "STRATEGY_IDS",
)

STRATEGY_PROFILE_EDITABLE_FIELDS: tuple[str, ...] = (
    "BASE_ORDER_COST",
    "MIN_STAKE",
    "MAX_STAKE",
    "MIN_ENTRY_PRICE",
    "MAX_ENTRY_PRICE",
    "LIVE_MAX_PRICE_IMPROVEMENT",
    "MAX_CONSECUTIVE_LOSSES",
    "MAX_STAKE_SKIP_ALERT_THRESHOLD",
    "OPEN_DELAY_SECONDS",
    "SIGNAL_MOMENTUM_THRESHOLD",
    "SIGNAL_WEAK_SIGNAL_MODE",
    "SIGNAL_FALLBACK_STRATEGY_ID",
    "SIGNAL_HISTORY_FIDELITY_SECONDS",
    "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS",
    "SIGNAL_DYNAMIC_THRESHOLD_K",
    "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS",
    "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS",
    "OFI_THRESHOLD",
    "BINANCE_SIGNAL_STALE_SECONDS",
    "STRATEGY7_OFI_THRESHOLD",
    "STRATEGY7_MOMENTUM_THRESHOLD",
    "STRATEGY7_MAX_MOMENTUM_DELTA",
    "STRATEGY7_MIN_SIGNAL_GAP",
    "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS",
    "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP",
    "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS",
    "STRATEGY7_DYNAMIC_SIZING_ENABLED",
    "STRATEGY7_SIZING_REFERENCE_PRICE",
    "STRATEGY7_SIZING_PRICE_STEP",
    "STRATEGY7_SIZING_PRICE_STEP_REDUCTION",
    "STRATEGY7_SIZING_MIN_MULTIPLIER",
    "STRATEGY7_SIZING_MAX_MULTIPLIER",
    "STRATEGY7_SIZING_STRONG_SIGNAL_GAP",
    "STRATEGY7_SIZING_STRONG_SIGNAL_BOOST",
    "STRATEGY9_DYNAMIC_SIZING_ENABLED",
    "STRATEGY9_SIZING_REFERENCE_PRICE",
    "STRATEGY9_SIZING_PRICE_STEP",
    "STRATEGY9_SIZING_PRICE_STEP_REDUCTION",
    "STRATEGY9_SIZING_MIN_MULTIPLIER",
    "STRATEGY9_SIZING_MAX_MULTIPLIER",
    "STRATEGY9_SIZING_STRONG_SIGNAL_GAP",
    "STRATEGY9_SIZING_STRONG_SIGNAL_BOOST",
    "STRATEGY9_STABILITY_SAMPLE_COUNT",
    "STRATEGY9_STABILITY_REQUIRED_COUNT",
    "STRATEGY9_STABILITY_WINDOW_SECONDS",
    "STRATEGY9_REVERSAL_LOOKBACK_SECONDS",
    "STRATEGY9_MAX_SIGNAL_DECAY",
    "STRATEGY9_BASE_MAX_ENTRY_PRICE",
    "STRATEGY9_STRONG_MAX_ENTRY_PRICE",
    "STRATEGY9_ULTRA_MAX_ENTRY_PRICE",
    "STRATEGY9_STRONG_SIGNAL_GAP",
    "STRATEGY9_ULTRA_SIGNAL_GAP",
    "STRATEGY10_MIN_EDGE",
    "STRATEGY10_EDGE_BUFFER",
    "STRATEGY10_OFI_WEIGHT",
    "STRATEGY10_MOMENTUM_WEIGHT",
    "STRATEGY10_MAX_FAIR_VALUE",
    "STRATEGY10_MIN_MOMENTUM_DELTA",
    "STRATEGY10_MAX_MOMENTUM_DELTA",
    "STRATEGY10_DOWN_MIN_EDGE",
    "STRATEGY10_CONFIRM_BEFORE_ENTRY_SECONDS",
    "STRATEGY11_MIN_EDGE",
    "STRATEGY11_EDGE_BUFFER",
    "STRATEGY11_VOLATILITY_BPS_PER_SQRT_MINUTE",
    "STRATEGY11_MIN_PROBABILITY",
    "STRATEGY11_MAX_PROBABILITY",
    "STRATEGY11_CONFIRM_BEFORE_ENTRY_SECONDS",
)

STRATEGY_PROFILE_COMMON_FIELDS: tuple[str, ...] = (
    "BASE_ORDER_COST",
    "MIN_STAKE",
    "MAX_STAKE",
    "MIN_ENTRY_PRICE",
    "MAX_ENTRY_PRICE",
    "MAX_CONSECUTIVE_LOSSES",
    "MAX_STAKE_SKIP_ALERT_THRESHOLD",
    "OPEN_DELAY_SECONDS",
)


def _paper_profile_env_prefix(timeframe: str) -> str:
    return f"PAPER_{str(timeframe).upper()}"


def _paper_profile_config_key(timeframe: str, field_name: str) -> str:
    return f"{_paper_profile_env_prefix(timeframe)}_{field_name}"


def _paper_profile_display_prefix(timeframe: str) -> str:
    return f"{str(timeframe).lower()} 纸面配置"


def _split_paper_profile_key(key: str) -> tuple[str, str] | None:
    for timeframe in SUPPORTED_PAPER_TIMEFRAMES:
        prefix = _paper_profile_env_prefix(timeframe) + "_"
        if key.startswith(prefix):
            return timeframe, key[len(prefix):]
    return None


def _shared_strategy_profile_prefix(strategy_id: int | str) -> str:
    return f"STRATEGY_{strategy_id}"


def _shared_strategy_profile_key(strategy_id: int | str, base_key: str) -> str:
    display_base_key = display_strategy_profile_base_key(strategy_id, base_key)
    return f"{_shared_strategy_profile_prefix(strategy_id)}_{display_base_key}"


def _split_strategy_profile_key(key: str) -> tuple[str, str, str] | None:
    match = re.match(r"^(?:(PAPER|LIVE)_)?STRATEGY_(12|11|10|[1-9])_(.+)$", str(key or ""))
    if not match:
        return None
    mode = (match.group(1) or "shared").lower()
    strategy_id = match.group(2)
    base_key = match.group(3)
    base_key = canonical_strategy_profile_base_key(strategy_id, base_key)
    if base_key not in STRATEGY_PROFILE_EDITABLE_FIELDS:
        return None
    return mode, strategy_id, base_key


def _strategy_profile_key_for_mode(mode: str, strategy_id: int | str, base_key: str) -> str:
    return _shared_strategy_profile_key(strategy_id, base_key)


def _parse_strategy_id_texts(value: str | None) -> list[str]:
    strategy_ids: list[str] = []
    seen: set[str] = set()
    for item in str(value or "").split(","):
        candidate = item.strip()
        if candidate not in SUPPORTED_STRATEGY_ID_TEXTS or candidate in seen:
            continue
        seen.add(candidate)
        strategy_ids.append(candidate)
    return strategy_ids


def _deduplicate_paper_live_strategy_ids(env_values: dict[str, str]) -> None:
    if "LIVE_STRATEGY_IDS" not in env_values:
        return
    live_ids = _parse_strategy_id_texts(env_values.get("LIVE_STRATEGY_IDS", ""))
    if not live_ids:
        return
    paper_ids = _parse_strategy_id_texts(env_values.get("PAPER_STRATEGY_IDS", ""))
    filtered_paper_ids = [strategy_id for strategy_id in paper_ids if strategy_id not in set(live_ids)]
    env_values["PAPER_STRATEGY_IDS"] = ",".join(filtered_paper_ids)


def _shared_strategy_profile_update_key(key: str) -> str:
    strategy_profile_key = _split_strategy_profile_key(key)
    if strategy_profile_key is None:
        return key
    _, strategy_id, base_key = strategy_profile_key
    return _shared_strategy_profile_key(strategy_id, base_key)


def _strategy_profile_field_names(strategy_id: int | str) -> list[str]:
    fields = list(STRATEGY_PROFILE_COMMON_FIELDS)
    strategy_text = str(strategy_id)
    if strategy_text == "5":
        fields.extend(
            [
                "SIGNAL_MOMENTUM_THRESHOLD",
                "SIGNAL_WEAK_SIGNAL_MODE",
                "SIGNAL_FALLBACK_STRATEGY_ID",
                "SIGNAL_HISTORY_FIDELITY_SECONDS",
                "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS",
                "SIGNAL_DYNAMIC_THRESHOLD_K",
                "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS",
                "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS",
            ]
        )
    if strategy_text == "6":
        fields.extend(["OFI_THRESHOLD", "BINANCE_SIGNAL_STALE_SECONDS"])
    if strategy_text in {"7", "8", "9", "10", "11", "12"}:
        fields.extend(
            [
                "OFI_THRESHOLD",
                "BINANCE_SIGNAL_STALE_SECONDS",
                "STRATEGY7_OFI_THRESHOLD",
                "STRATEGY7_MOMENTUM_THRESHOLD",
                "STRATEGY7_MAX_MOMENTUM_DELTA",
                "STRATEGY7_MIN_SIGNAL_GAP",
                "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS",
                "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP",
                "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS",
                "STRATEGY7_DYNAMIC_SIZING_ENABLED",
                "STRATEGY7_SIZING_REFERENCE_PRICE",
                "STRATEGY7_SIZING_PRICE_STEP",
                "STRATEGY7_SIZING_PRICE_STEP_REDUCTION",
                "STRATEGY7_SIZING_MIN_MULTIPLIER",
                "STRATEGY7_SIZING_MAX_MULTIPLIER",
                "STRATEGY7_SIZING_STRONG_SIGNAL_GAP",
                "STRATEGY7_SIZING_STRONG_SIGNAL_BOOST",
            ]
        )
    if strategy_text == "9":
        fields.extend(
            [
                "STRATEGY9_DYNAMIC_SIZING_ENABLED",
                "STRATEGY9_SIZING_REFERENCE_PRICE",
                "STRATEGY9_SIZING_PRICE_STEP",
                "STRATEGY9_SIZING_PRICE_STEP_REDUCTION",
                "STRATEGY9_SIZING_MIN_MULTIPLIER",
                "STRATEGY9_SIZING_MAX_MULTIPLIER",
                "STRATEGY9_SIZING_STRONG_SIGNAL_GAP",
                "STRATEGY9_SIZING_STRONG_SIGNAL_BOOST",
                "STRATEGY9_STABILITY_SAMPLE_COUNT",
                "STRATEGY9_STABILITY_REQUIRED_COUNT",
                "STRATEGY9_STABILITY_WINDOW_SECONDS",
                "STRATEGY9_REVERSAL_LOOKBACK_SECONDS",
                "STRATEGY9_MAX_SIGNAL_DECAY",
                "STRATEGY9_BASE_MAX_ENTRY_PRICE",
                "STRATEGY9_STRONG_MAX_ENTRY_PRICE",
                "STRATEGY9_ULTRA_MAX_ENTRY_PRICE",
                "STRATEGY9_STRONG_SIGNAL_GAP",
                "STRATEGY9_ULTRA_SIGNAL_GAP",
            ]
        )
    if strategy_text == "10":
        fields.extend(
            [
                "STRATEGY10_MIN_EDGE",
                "STRATEGY10_EDGE_BUFFER",
                "STRATEGY10_OFI_WEIGHT",
                "STRATEGY10_MOMENTUM_WEIGHT",
                "STRATEGY10_MAX_FAIR_VALUE",
                "STRATEGY10_MIN_MOMENTUM_DELTA",
                "STRATEGY10_MAX_MOMENTUM_DELTA",
                "STRATEGY10_DOWN_MIN_EDGE",
                "STRATEGY10_CONFIRM_BEFORE_ENTRY_SECONDS",
            ]
        )
    if strategy_text in {"11", "12"}:
        fields.extend(
            [
                "STRATEGY11_MIN_EDGE",
                "STRATEGY11_EDGE_BUFFER",
                "STRATEGY11_VOLATILITY_BPS_PER_SQRT_MINUTE",
                "STRATEGY11_MIN_PROBABILITY",
                "STRATEGY11_MAX_PROBABILITY",
                "STRATEGY11_CONFIRM_BEFORE_ENTRY_SECONDS",
            ]
        )
    return fields


def _normalize_paper_timeframes_value(value: str) -> str:
    selected: list[str] = []
    for item in str(value).split(","):
        timeframe = item.strip().lower()
        if timeframe in SUPPORTED_PAPER_TIMEFRAMES and timeframe not in selected:
            selected.append(timeframe)
    if not selected:
        raise ValueError(f"Invalid value for PAPER_TIMEFRAMES: expected comma-separated 5m/15m, got {value!r}")
    return ",".join(selected)


def _cfg_for_paper_timeframe(cfg: AppConfig, timeframe: str) -> AppConfig:
    target_timeframe = _normalize_timeframe_filter(timeframe, fallback=cfg.market_timeframe)
    profile = getattr(cfg, "paper_profiles", {}).get(target_timeframe)
    if profile is None:
        return replace(cfg, market_timeframe=target_timeframe)
    return replace(
        cfg,
        market_timeframe=target_timeframe,
        strategy_id=profile.strategy_id,
        paper_strategy_ids=list(profile.paper_strategy_ids),
        base_order_cost=profile.base_order_cost,
        max_consecutive_losses=profile.max_consecutive_losses,
        min_stake=profile.min_stake,
        max_stake=profile.max_stake,
        min_entry_price=profile.min_entry_price,
        max_entry_price=profile.max_entry_price,
        open_delay_seconds=profile.open_delay_seconds,
        signal_momentum_threshold=profile.signal_momentum_threshold,
        ofi_threshold=profile.ofi_threshold,
        binance_signal_stale_seconds=profile.binance_signal_stale_seconds,
        strategy7_ofi_threshold=profile.strategy7_ofi_threshold,
        strategy7_momentum_threshold=profile.strategy7_momentum_threshold,
        strategy7_max_momentum_delta=profile.strategy7_max_momentum_delta,
        strategy7_min_signal_gap=profile.strategy7_min_signal_gap,
        strategy7_confirm_before_entry_seconds=profile.strategy7_confirm_before_entry_seconds,
        strategy7_late_confirm_strong_signal_gap=profile.strategy7_late_confirm_strong_signal_gap,
        strategy7_late_confirm_relax_seconds=profile.strategy7_late_confirm_relax_seconds,
        strategy7_dynamic_sizing_enabled=profile.strategy7_dynamic_sizing_enabled,
        strategy7_sizing_reference_price=profile.strategy7_sizing_reference_price,
        strategy7_sizing_price_step=profile.strategy7_sizing_price_step,
        strategy7_sizing_price_step_reduction=profile.strategy7_sizing_price_step_reduction,
        strategy7_sizing_min_multiplier=profile.strategy7_sizing_min_multiplier,
        strategy7_sizing_max_multiplier=profile.strategy7_sizing_max_multiplier,
        strategy7_sizing_strong_signal_gap=profile.strategy7_sizing_strong_signal_gap,
        strategy7_sizing_strong_signal_boost=profile.strategy7_sizing_strong_signal_boost,
        strategy7_max_entry_price=profile.max_entry_price,
        strategy9_dynamic_sizing_enabled=profile.strategy9_dynamic_sizing_enabled,
        strategy9_sizing_reference_price=profile.strategy9_sizing_reference_price,
        strategy9_sizing_price_step=profile.strategy9_sizing_price_step,
        strategy9_sizing_price_step_reduction=profile.strategy9_sizing_price_step_reduction,
        strategy9_sizing_min_multiplier=profile.strategy9_sizing_min_multiplier,
        strategy9_sizing_max_multiplier=profile.strategy9_sizing_max_multiplier,
        strategy9_sizing_strong_signal_gap=profile.strategy9_sizing_strong_signal_gap,
        strategy9_sizing_strong_signal_boost=profile.strategy9_sizing_strong_signal_boost,
        strategy9_stability_sample_count=profile.strategy9_stability_sample_count,
        strategy9_stability_required_count=profile.strategy9_stability_required_count,
        strategy9_stability_window_seconds=profile.strategy9_stability_window_seconds,
        strategy9_reversal_lookback_seconds=profile.strategy9_reversal_lookback_seconds,
        strategy9_max_signal_decay=profile.strategy9_max_signal_decay,
        strategy9_base_max_entry_price=profile.strategy9_base_max_entry_price,
        strategy9_strong_max_entry_price=profile.strategy9_strong_max_entry_price,
        strategy9_ultra_max_entry_price=profile.strategy9_ultra_max_entry_price,
        strategy9_strong_signal_gap=profile.strategy9_strong_signal_gap,
        strategy9_ultra_signal_gap=profile.strategy9_ultra_signal_gap,
        strategy10_min_edge=profile.strategy10_min_edge,
        strategy10_edge_buffer=profile.strategy10_edge_buffer,
        strategy10_ofi_weight=profile.strategy10_ofi_weight,
        strategy10_momentum_weight=profile.strategy10_momentum_weight,
        strategy10_max_fair_value=profile.strategy10_max_fair_value,
        strategy10_min_momentum_delta=profile.strategy10_min_momentum_delta,
        strategy10_max_momentum_delta=profile.strategy10_max_momentum_delta,
        strategy10_down_min_edge=profile.strategy10_down_min_edge,
        strategy10_confirm_before_entry_seconds=profile.strategy10_confirm_before_entry_seconds,
        strategy11_min_edge=profile.strategy11_min_edge,
        strategy11_edge_buffer=profile.strategy11_edge_buffer,
        strategy11_volatility_bps_per_sqrt_minute=profile.strategy11_volatility_bps_per_sqrt_minute,
        strategy11_min_probability=profile.strategy11_min_probability,
        strategy11_max_probability=profile.strategy11_max_probability,
        strategy11_confirm_before_entry_seconds=profile.strategy11_confirm_before_entry_seconds,
    )


def _pending_paper_trade_to_recent_row(item: PendingPaperTrade) -> dict[str, str]:
    return {
        'timestamp': item.queued_at or item.end_time,
        'mode': 'paper',
        'round_index': str(item.round_index),
        'experiment_id': item.experiment_id or '',
        'strategy': str(item.strategy),
        'entry_timing': item.entry_timing,
        'event_slug': item.event_slug,
        'start_time': item.start_time,
        'end_time': item.end_time,
        'side': item.side,
        'price': str(item.price),
        'order_size': str(item.order_size),
        'order_cost': str(item.order_cost),
        'expected_profit': str(item.expected_profit),
        'result': '--',
        'trade_pnl': '0.0',
        'cash_pnl': '--',
        'recovery_loss': '--',
        'consecutive_losses': '--',
        'stop_loss_triggered': 'False',
        'skip_reason': '',
        'signal_open_up_price': '' if item.signal_open_up_price is None else str(item.signal_open_up_price),
        'signal_current_up_price': '' if item.signal_current_up_price is None else str(item.signal_current_up_price),
        'signal_threshold': '' if item.signal_threshold is None else str(item.signal_threshold),
        'signal_delta': '' if item.signal_delta is None else str(item.signal_delta),
        'signal_locked': str(item.signal_locked),
        'signal_reason': item.signal_reason or '',
        'pending_status': 'pending_settlement',
    }


def _parse_recent_row_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _round_start_from_slug(slug: Any) -> datetime | None:
    match = re.search(r"-(\d{10})(?:$|\D)", str(slug or ""))
    if not match:
        return None
    try:
        return datetime.fromtimestamp(int(match.group(1)), timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _timeframe_seconds(timeframe: str | None) -> int:
    normalized = _normalize_timeframe_filter(timeframe, fallback="5m")
    return 900 if normalized == "15m" else 300


def _floor_to_timeframe(dt: datetime, timeframe: str | None) -> datetime:
    seconds = _timeframe_seconds(timeframe)
    timestamp = int(dt.timestamp())
    return datetime.fromtimestamp(timestamp - (timestamp % seconds), timezone.utc)


def _recent_row_round_display_time(row: dict[str, str], timeframe: str | None) -> str:
    slug_round = _round_start_from_slug(row.get("event_slug"))
    if slug_round is not None:
        return _iso(slug_round) or ""

    anchor = (
        _parse_recent_row_datetime(row.get("start_time"))
        or _parse_recent_row_datetime(row.get("timestamp"))
        or _parse_recent_row_datetime(row.get("end_time"))
    )
    if anchor is None:
        return ""
    return _iso(_floor_to_timeframe(anchor, timeframe)) or ""


def _recent_row_round_sort_time(row: dict[str, str], timeframe: str | None) -> datetime:
    slug_round = _round_start_from_slug(row.get("event_slug"))
    if slug_round is not None:
        return slug_round
    anchor = (
        _parse_recent_row_datetime(row.get("start_time"))
        or _parse_recent_row_datetime(row.get("round_display_time"))
        or _parse_recent_row_datetime(row.get("timestamp"))
        or _parse_recent_row_datetime(row.get("end_time"))
    )
    if anchor is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return _floor_to_timeframe(anchor, timeframe)


def _recent_row_round_sort_key(row: dict[str, str], timeframe: str | None = None) -> tuple[datetime, str]:
    return (
        _recent_row_round_sort_time(row, timeframe),
        str(row.get("timestamp") or ""),
    )


def _sort_recent_rows_by_round(rows: list[dict[str, str]], timeframe: str | None = None) -> None:
    rows.sort(key=lambda row: _recent_row_round_sort_key(row, timeframe), reverse=True)


def _with_recent_round_display_time(row: dict[str, str], timeframe: str | None) -> dict[str, str]:
    enriched = dict(row)
    enriched["round_display_time"] = _recent_row_round_display_time(enriched, timeframe)
    return enriched


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _market_first_value(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    lowered = {str(key).lower(): value for key, value in payload.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return None


def _health_check_payload(
    check_id: str,
    label: str,
    *,
    ok: bool,
    detail: str,
    value: Any | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": check_id,
        "label": label,
        "ok": bool(ok),
        "detail": detail,
    }
    if value is not None:
        payload["value"] = value
    return payload


def _terminal_outcome_price(value: float | None, target: float) -> bool:
    return value is not None and abs(value - target) <= 1e-9


def _normalize_validation_outcome(value: Any) -> str:
    normalized = normalize_outcome_label(str(value or ""))
    return normalized if normalized in {"UP", "DOWN"} else ""


def _first_value(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _truthy_official_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "winner"}
    return False


def _result_validation_error_text(exc: Exception) -> str:
    text = f"{exc.__class__.__name__}: {exc}".strip()
    if len(text) > 240:
        return text[:237] + "..."
    return text


def _token_outcome(token: dict[str, Any]) -> str:
    return _normalize_validation_outcome(
        _first_value(token, ("outcome", "name", "label", "side"))
    )


def _official_winning_outcome(event_payload: dict[str, Any], *, require_final_price: bool = False) -> str:
    market = (event_payload.get("markets") or [{}])[0]
    metadata = event_payload.get("eventMetadata") or {}
    if require_final_price and metadata.get("priceToBeat") is not None and metadata.get("finalPrice") is None:
        return ""
    outcome_keys = (
        "winning_outcome",
        "winningOutcome",
        "winningOutcomeName",
        "winner",
        "resolvedOutcome",
    )
    for payload in (market, event_payload):
        direct = _normalize_validation_outcome(_first_value(payload, outcome_keys))
        if direct:
            return direct

    token_keys = ("winning_asset_id", "winningAssetId", "winningTokenId", "winning_token_id")
    winning_asset_id = str(_first_value(market, token_keys) or _first_value(event_payload, token_keys) or "").strip()
    if winning_asset_id:
        token_ids = extract_token_ids(market.get("clobTokenIds"), market.get("outcomes"))
        if winning_asset_id == str(token_ids.get("UP") or "").strip():
            return "UP"
        if winning_asset_id == str(token_ids.get("DOWN") or "").strip():
            return "DOWN"

    tokens = market.get("tokens")
    if isinstance(tokens, list):
        for token in tokens:
            if isinstance(token, dict) and _truthy_official_value(token.get("winner")):
                outcome = _token_outcome(token)
                if outcome:
                    return outcome
        if winning_asset_id:
            for token in tokens:
                if not isinstance(token, dict):
                    continue
                token_id = str(_first_value(token, ("token_id", "tokenId", "asset_id", "assetId", "id")) or "").strip()
                if token_id == winning_asset_id:
                    outcome = _token_outcome(token)
                    if outcome:
                        return outcome

    prices = parse_outcome_prices(market.get("outcomePrices"), market.get("outcomes"))
    up_price = prices.get("UP")
    down_price = prices.get("DOWN")
    if _terminal_outcome_price(up_price, 1.0) and _terminal_outcome_price(down_price, 0.0):
        return "UP"
    if _terminal_outcome_price(down_price, 1.0) and _terminal_outcome_price(up_price, 0.0):
        return "DOWN"
    return ""


def _slug_matches_client_series(slug: str, client: PolymarketClient | Any) -> bool:
    supported_prefixes = tuple(
        prefix
        for definition in MARKET_TIMEFRAME_DEFINITIONS.values()
        for prefix in definition.slug_prefixes
    )
    if not supported_prefixes:
        return True
    for prefix in supported_prefixes:
        if not slug.startswith(prefix):
            continue
        suffix = slug[len(prefix):]
        if type(client) is _POLYMARKET_CLIENT_CLASS and not suffix.isdigit():
            return False
        return True
    return False


def _is_live_btc_numeric_slug(slug: str) -> bool:
    for definition in MARKET_TIMEFRAME_DEFINITIONS.values():
        for prefix in definition.slug_prefixes:
            if slug.startswith(prefix) and slug[len(prefix):].isdigit():
                return True
    return False


def _event_metadata_from_market_endpoint(market_payload: dict[str, Any]) -> dict[str, Any]:
    metadata = market_payload.get("eventMetadata") or {}
    if isinstance(metadata, dict) and metadata:
        return metadata
    events = market_payload.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            event_metadata = event.get("eventMetadata") or {}
            if isinstance(event_metadata, dict) and event_metadata:
                return event_metadata
    return {}


def _should_validate_trade_result(row: dict[str, str], *, fill_missing_result: bool, missing_result: bool) -> bool:
    side = str(row.get('side') or '').strip().upper()
    if side not in {'UP', 'DOWN'}:
        return False
    if str(row.get('skip_reason') or '').strip():
        return False
    order_cost = _optional_float(row.get('order_cost')) or 0.0
    order_size = _optional_float(row.get('order_size')) or 0.0
    if order_cost <= 0.0 or order_size <= 0.0:
        return False
    return bool(fill_missing_result or not missing_result)


def _cached_validation_result_is_stale(
    row: dict[str, str],
    resolved: dict[str, str],
    *,
    fill_missing_result: bool,
) -> bool:
    if not resolved:
        return False
    if not (fill_missing_result or str(row.get("mode") or "").strip().lower() == "live"):
        return False
    if not str(resolved.get("resolved_price_to_beat") or "").strip():
        return False
    return not str(resolved.get("resolved_final_price") or "").strip()


def _validate_recent_trade_row(
    row: dict[str, str],
    *,
    client: PolymarketClient | Any,
    validation_cache: dict[str, dict[str, str]] | None = None,
    fill_missing_result: bool = False,
    validate_existing_result: bool = True,
) -> dict[str, str]:
    validated = dict(row)
    validated.setdefault('resolved_price_to_beat', '')
    validated.setdefault('resolved_final_price', '')
    validated.setdefault('resolved_expected_result', '')
    validated.setdefault('result_check_status', '')
    validated.setdefault('result_check_error', '')

    if validated.get('pending_status') == 'pending_settlement':
        validated['result_check_status'] = 'pending'
        return validated

    slug = str(validated.get('event_slug') or '').strip()
    result = str(validated.get('result') or '').strip().upper()
    missing_result = not result or result == '--'
    should_validate_result = _should_validate_trade_result(
        validated,
        fill_missing_result=fill_missing_result,
        missing_result=missing_result,
    )
    if str(validated.get("mode") or "").strip().lower() == "live" and not should_validate_result:
        return validated
    if result in {"UP", "DOWN"} and not validate_existing_result:
        return validated
    if not slug or (missing_result and not fill_missing_result):
        return validated
    if not _slug_matches_client_series(slug, client):
        return validated

    cache_key = slug
    resolved = dict((validation_cache or {}).get(cache_key) or {})
    if _cached_validation_result_is_stale(
        validated,
        resolved,
        fill_missing_result=fill_missing_result,
    ):
        resolved = {}
    if not resolved:
        resolved = {
            'resolved_price_to_beat': '',
            'resolved_final_price': '',
            'resolved_expected_result': '',
            'result_check_status': '',
        }
        try:
            event_payload = client.get_event_by_slug(slug)
        except Exception as exc:
            resolved['result_check_status'] = 'error'
            resolved['result_check_error'] = _result_validation_error_text(exc)
            if should_validate_result:
                validated['result_check_status'] = 'error'
                validated['result_check_error'] = resolved['result_check_error']
            return validated

        metadata = event_payload.get("eventMetadata") or {}
        endpoint_market_payload = None
        price_to_beat = _optional_float(metadata.get("priceToBeat"))
        final_price = _optional_float(metadata.get("finalPrice"))
        live_result_validation = str(validated.get("mode") or "").strip().lower() == "live"
        if live_result_validation and _is_live_btc_numeric_slug(slug) and (price_to_beat is None or final_price is None):
            get_market = getattr(client, "get_market_by_slug", None)
            if callable(get_market):
                try:
                    endpoint_market_payload = get_market(slug)
                except Exception:
                    endpoint_market_payload = None
                if isinstance(endpoint_market_payload, dict):
                    endpoint_metadata = _event_metadata_from_market_endpoint(endpoint_market_payload)
                    if isinstance(endpoint_metadata, dict):
                        price_to_beat = price_to_beat if price_to_beat is not None else _optional_float(endpoint_metadata.get("priceToBeat"))
                        final_price = final_price if final_price is not None else _optional_float(endpoint_metadata.get("finalPrice"))
        if final_price is None and not live_result_validation:
            read_fallback_metadata = getattr(client, "get_market_ahead_event_metadata", None)
            if callable(read_fallback_metadata):
                try:
                    fallback_metadata = read_fallback_metadata(slug)
                except Exception:
                    fallback_metadata = {}
                if isinstance(fallback_metadata, dict):
                    price_to_beat = price_to_beat if price_to_beat is not None else _optional_float(fallback_metadata.get("priceToBeat"))
                    final_price = _optional_float(fallback_metadata.get("finalPrice"))
        if price_to_beat is not None:
            resolved['resolved_price_to_beat'] = str(price_to_beat)
        if final_price is not None:
            resolved['resolved_final_price'] = str(final_price)

        if live_result_validation:
            market = (event_payload.get("markets") or [{}])[0]
            if endpoint_market_payload is not None and not isinstance(endpoint_market_payload, dict):
                endpoint_market_payload = None
            if _is_live_btc_numeric_slug(slug) and (price_to_beat is None or final_price is None):
                official_result = ""
                event_waits_for_final_price = True
            elif _is_live_btc_numeric_slug(slug) and price_to_beat is not None and final_price is not None:
                official_result = "UP" if final_price >= price_to_beat else "DOWN"
                event_waits_for_final_price = False
            else:
                official_result = resolved_live_result_from_official_sources(
                    client,
                    event_payload,
                    market,
                ) or ""
                event_waits_for_final_price = live_market_waits_for_final_price(client, event_payload, market)
            if not official_result and not event_waits_for_final_price:
                funder = getattr(getattr(client, "config", None), "live_funder", None)
                official_result = resolved_result_from_redeemable_positions(client, funder=funder, slug=slug) or ""
        else:
            official_result = _official_winning_outcome(
                event_payload,
                require_final_price=fill_missing_result,
            )
        if not official_result and price_to_beat is not None and final_price is not None:
            official_result = "UP" if final_price >= price_to_beat else "DOWN"

        if not official_result:
            resolved['result_check_status'] = 'official_pending'
        else:
            resolved['resolved_expected_result'] = official_result
            if validation_cache is not None:
                validation_cache[cache_key] = dict(resolved)

    validated['resolved_price_to_beat'] = resolved.get('resolved_price_to_beat', '')
    validated['resolved_final_price'] = resolved.get('resolved_final_price', '')
    if resolved.get('result_check_status') == 'error':
        if should_validate_result:
            validated['result_check_status'] = 'error'
            validated['result_check_error'] = resolved.get('result_check_error', '')
        return validated
    official_result = resolved.get('resolved_expected_result', '')
    if not should_validate_result:
        return validated
    if not official_result:
        validated['result_check_status'] = resolved.get('result_check_status') or 'official_pending'
        return validated

    validated['resolved_expected_result'] = official_result
    if missing_result:
        validated['result'] = official_result
        validated['result_check_status'] = 'official'
        return validated
    validated['result_check_status'] = 'match' if result == official_result else 'mismatch'
    return validated


def _live_summary_row_key(row: dict[str, str]) -> tuple[str, str, str]:
    return (
        str(row.get("strategy") or "").strip(),
        str(row.get("event_slug") or "").strip(),
        str(row.get("side") or "").strip().upper(),
    )


def _is_live_summary_backfill_candidate(row: dict[str, str], settled_keys: set[tuple[str, str, str]]) -> bool:
    result = str(row.get("result") or "").strip().upper()
    side = str(row.get("side") or "").strip().upper()
    if result in {"UP", "DOWN"} or side not in {"UP", "DOWN"}:
        return False
    if _live_summary_row_key(row) in settled_keys:
        return False
    if str(row.get("skip_reason") or "").strip():
        return False
    return (_optional_float(row.get("order_cost")) or 0.0) > 0.0


def _backfill_live_summary_rows(
    rows: list[dict[str, str]],
    *,
    client: PolymarketClient | Any,
    validation_cache: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    settled_keys = {
        _live_summary_row_key(row)
        for row in rows
        if str(row.get("result") or "").strip().upper() in {"UP", "DOWN"}
    }
    backfilled: list[dict[str, str]] = []
    for row in rows:
        if not _is_live_summary_backfill_candidate(row, settled_keys):
            backfilled.append(dict(row))
            continue
        validated = _validate_recent_trade_row(
            row,
            client=client,
            validation_cache=validation_cache,
            fill_missing_result=True,
        )
        result = str(validated.get("result") or "").strip().upper()
        side = str(validated.get("side") or "").strip().upper()
        if result in {"UP", "DOWN"}:
            order_cost = _optional_float(validated.get("order_cost")) or 0.0
            expected_profit = _optional_float(validated.get("expected_profit")) or 0.0
            validated["trade_pnl"] = str(expected_profit if result == side else -order_cost)
        backfilled.append(validated)
    return backfilled


def _csv_fieldnames_for_rows(rows: list[dict[str, str]]) -> list[str]:
    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    return fieldnames


def _write_summary_work_csv(base_csv: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> Path:
    base_csv.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        newline="",
        encoding="utf-8",
        dir=base_csv.parent,
        prefix=f"{base_csv.stem}_summary_work_",
        suffix=base_csv.suffix,
        delete=False,
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return Path(handle.name)


def _cleanup_summary_work_csv(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except (FileNotFoundError, PermissionError, OSError):
        return


def _live_result_value(row: dict[str, str]) -> str:
    result = str(row.get("result") or "").strip().upper()
    return result if result in {"UP", "DOWN"} else ""


def _live_ledger_result_value(row: dict[str, str]) -> str:
    result = str(row.get("result") or "").strip().upper()
    return result if result in {"UP", "DOWN", PROVISIONAL_LOSS_RESULT} else ""


def _live_float_value(row: dict[str, str], key: str) -> float:
    return _optional_float(row.get(key)) or 0.0


def _refresh_live_row_trade_pnl_from_result(row: dict[str, str]) -> dict[str, str]:
    refreshed = dict(row)
    result = _live_result_value(refreshed)
    side = str(refreshed.get("side") or "").strip().upper()
    if result and side in {"UP", "DOWN"}:
        order_cost = _live_float_value(refreshed, "order_cost")
        expected_profit = _live_float_value(refreshed, "expected_profit")
        existing_pnl = _live_float_value(refreshed, "trade_pnl")
        refreshed["trade_pnl"] = str(expected_profit if result == side else (existing_pnl if existing_pnl < 0 else -order_cost))
    return refreshed


def _live_row_session_day(row: dict[str, str]) -> str:
    for key in ("end_time", "start_time", "timestamp"):
        raw_value = str(row.get(key) or "").strip()
        if not raw_value:
            continue
        try:
            return session_day_key(datetime.fromisoformat(raw_value))
        except ValueError:
            continue
    return ""


def _live_row_counts_for_ledger(row: dict[str, str], result: str) -> bool:
    side = str(row.get("side") or "").strip().upper()
    if result not in {"UP", "DOWN", PROVISIONAL_LOSS_RESULT} or side not in {"UP", "DOWN"}:
        return False
    if str(row.get("skip_reason") or "").strip():
        return False
    return _live_float_value(row, "order_cost") > 0.0


def _live_strategy_uses_recovery_loss(cfg: AppConfig | None, strategy_id: str) -> bool:
    return False


def _live_row_explicit_tracks_recovery_loss(row: dict[str, str]) -> bool | None:
    return False


def _live_row_tracks_recovery_loss(row: dict[str, str], cfg: AppConfig | None, strategy_id: str) -> bool:
    return False


def _live_row_tracks_loss_streak(row: dict[str, str], cfg: AppConfig | None, strategy_id: str) -> bool:
    return True


def _recompute_live_ledger_rows(
    rows: list[dict[str, str]],
    *,
    cfg: AppConfig | None = None,
) -> dict[str, dict[str, float | int]]:
    states: dict[str, dict[str, float | int]] = {}
    latest_days: dict[str, str] = {}
    trade_deltas: list[tuple[str, str, float]] = []
    for row in rows:
        strategy_id = str(row.get("strategy") or "").strip()
        if not strategy_id:
            continue
        state = states.setdefault(
            strategy_id,
            {
                "cash_pnl": 0.0,
                "daily_realized_pnl": 0.0,
                "recovery_loss": 0.0,
                "consecutive_losses": 0,
            },
        )
        result = _live_ledger_result_value(row)
        trade_pnl = 0.0
        if _live_row_counts_for_ledger(row, result):
            side = str(row.get("side") or "").strip().upper()
            order_cost = _live_float_value(row, "order_cost")
            expected_profit = _live_float_value(row, "expected_profit")
            tracks_recovery_loss = _live_row_tracks_recovery_loss(row, cfg, strategy_id)
            tracks_loss_streak = _live_row_tracks_loss_streak(row, cfg, strategy_id)
            if result == side:
                trade_pnl = expected_profit
                state["cash_pnl"] = float(state["cash_pnl"]) + trade_pnl
                state["daily_realized_pnl"] = float(state["daily_realized_pnl"]) + trade_pnl
                if tracks_recovery_loss:
                    state["recovery_loss"] = 0.0
                if tracks_loss_streak:
                    state["consecutive_losses"] = 0
            else:
                existing_pnl = _live_float_value(row, "trade_pnl")
                trade_pnl = existing_pnl if existing_pnl < 0 else -order_cost
                state["cash_pnl"] = float(state["cash_pnl"]) + trade_pnl
                if tracks_recovery_loss:
                    state["recovery_loss"] = float(state["recovery_loss"]) + order_cost
                if tracks_loss_streak:
                    state["consecutive_losses"] = int(state["consecutive_losses"]) + 1
            session_day = _live_row_session_day(row)
            if session_day:
                latest_days[strategy_id] = max(latest_days.get(strategy_id, ""), session_day)
                trade_deltas.append((strategy_id, session_day, trade_pnl))
        row["trade_pnl"] = str(trade_pnl)
        row["cash_pnl"] = str(state["cash_pnl"])
        row["recovery_loss"] = str(state["recovery_loss"])
        row["consecutive_losses"] = str(state["consecutive_losses"])
    for strategy_id, latest_day in latest_days.items():
        states[strategy_id]["daily_realized_pnl"] = sum(
            trade_pnl
            for delta_strategy_id, session_day, trade_pnl in trade_deltas
            if delta_strategy_id == strategy_id and session_day == latest_day
        )
    return states


def _update_live_session_state_from_ledger(
    *,
    state_path: Path,
    ledger_states: dict[str, dict[str, float | int]],
    active_strategy_id: int,
    cfg: AppConfig | None = None,
) -> None:
    with atomic_path_guard(state_path):
        if not state_path.exists():
            return
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return
        raw_live_strategies = payload.get("live_strategies")
        live_strategies = raw_live_strategies if isinstance(raw_live_strategies, dict) else {}
        for strategy_id, ledger_state in ledger_states.items():
            strategy_payload = live_strategies.get(strategy_id)
            if not isinstance(strategy_payload, dict):
                continue
            for key in ("cash_pnl", "daily_realized_pnl", "recovery_loss", "consecutive_losses"):
                strategy_payload[key] = ledger_state[key]
            if not _live_strategy_uses_recovery_loss(cfg, strategy_id):
                strategy_payload["recovery_loss"] = 0.0
        active_state = ledger_states.get(str(active_strategy_id))
        if active_state is not None:
            for key in ("cash_pnl", "daily_realized_pnl", "recovery_loss", "consecutive_losses"):
                payload[key] = active_state[key]
            if not _live_strategy_uses_recovery_loss(cfg, str(active_strategy_id)):
                payload["recovery_loss"] = 0.0
        atomic_write_text(state_path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _backup_live_ledger_files(*, live_csv: Path, state_path: Path) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if live_csv.exists():
        shutil.copy2(
            live_csv,
            live_csv.with_name(f"{live_csv.stem}_backup_before_reconcile_{stamp}{live_csv.suffix}"),
        )
    if state_path.exists():
        shutil.copy2(
            state_path,
            state_path.with_name(f"{state_path.stem}_backup_before_reconcile_{stamp}{state_path.suffix}"),
        )


def _live_csv_has_provisional_loss(live_csv: Path) -> bool:
    if not live_csv.exists():
        return False
    with live_csv.open("r", newline="", encoding="utf-8") as handle:
        return any(
            _live_ledger_result_value(row) == PROVISIONAL_LOSS_RESULT
            for row in csv.DictReader(handle)
        )


def _auto_reconcile_live_ledger(
    *,
    live_csv: Path,
    state_path: Path,
    client: PolymarketClient | Any,
    validation_cache: dict[str, dict[str, str]] | None,
    active_strategy_id: int,
    cfg: AppConfig | None = None,
    provisional_only: bool = False,
) -> int:
    if not live_csv.exists():
        return 0
    with live_csv.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if not fieldnames or not rows:
        return 0

    changed = 0
    for row in rows:
        if _live_ledger_result_value(row) == PROVISIONAL_LOSS_RESULT:
            validated = _validate_recent_trade_row(
                row,
                client=client,
                validation_cache=validation_cache,
                fill_missing_result=True,
            )
            official_result = str(validated.get("resolved_expected_result") or "").strip().upper()
            if official_result in {"UP", "DOWN"}:
                row["result"] = official_result
                changed += 1
            continue
        row_slug = str(row.get("event_slug") or "").strip()
        if provisional_only:
            continue
        if _live_result_value(row) not in {"UP", "DOWN"}:
            continue
        validated = _validate_recent_trade_row(
            row,
            client=client,
            validation_cache=validation_cache,
        )
        if (
            _is_live_btc_numeric_slug(row_slug)
            and validated.get("result_check_status") == "official_pending"
        ):
            row["result"] = PROVISIONAL_LOSS_RESULT
            changed += 1
            continue
        if validated.get("result_check_status") != "mismatch":
            continue
        official_result = str(validated.get("resolved_expected_result") or "").strip().upper()
        if official_result not in {"UP", "DOWN"}:
            continue
        row["result"] = official_result
        changed += 1

    needs_state_sync = any(_live_ledger_result_value(row) == PROVISIONAL_LOSS_RESULT for row in rows)
    if changed <= 0 and not needs_state_sync:
        return 0

    ledger_states = _recompute_live_ledger_rows(rows, cfg=cfg)
    if changed > 0:
        _backup_live_ledger_files(live_csv=live_csv, state_path=state_path)
        with live_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    _update_live_session_state_from_ledger(
        state_path=state_path,
        ledger_states=ledger_states,
        active_strategy_id=active_strategy_id,
        cfg=cfg,
    )
    return changed


_LIVE_HEALTH_PROFILE_COMPARE_ATTRS: tuple[str, ...] = tuple(
    field.name
    for field in fields(AppConfig)
    if field.init
    and field.name
    not in {
        "trade_mode",
        "strategy_id",
        "strategy_ids",
        "paper_strategy_ids",
        "live_strategy_ids",
        "paper_timeframes",
        "live_trading_enabled",
        "live_private_key",
        "live_api_key",
        "live_api_secret",
        "live_api_passphrase",
        "live_chain_id",
        "live_signature_type",
        "live_funder",
        "live_order_type",
        "paper_simulated_wallet_balance",
    }
)



def _localize_runtime_message(message: str | None) -> str | None:
    if not message:
        return message
    lowered = message.lower()
    if "trading restricted" in lowered or "geoblock" in lowered:
        return "Polymarket 限制当前地区实盘交易。程序已保持运行，但当前地区无法提交实盘订单；请更换允许地区或切回模拟盘。"
    mapping = {
        "Live trading is disabled.": "实盘交易未开启。",
        "Live trading is disabled. Set LIVE_TRADING_ENABLED=true (or config flag) to submit orders.": "并行实盘未开启。请先打开并行实盘开关。",
        "Missing private key for live trading.": "缺少实盘私钥。",
        "Missing POLYMARKET_FUNDER for live trading.": "\u7f3a\u5c11\u5b9e\u76d8\u94b1\u5305\u5730\u5740\u3002",
    }
    return mapping.get(message, message)


def _default_optimizer_runtime() -> dict[str, Any]:
    return {
        "enabled": False,
        "last_run_at": None,
        "champion_id": None,
        "active_challengers": [],
        "promotable_count": 0,
    }


def _load_optimizer_runtime(path: Path) -> dict[str, Any]:
    runtime = _default_optimizer_runtime()
    if not path.exists():
        return runtime
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return runtime
    runtime["enabled"] = bool(payload.get("enabled", False))
    runtime["last_run_at"] = payload.get("last_run_at")
    runtime["champion_id"] = payload.get("champion_id")
    active_challengers = payload.get("active_challengers")
    runtime["active_challengers"] = active_challengers if isinstance(active_challengers, list) else []
    promotable_candidates = payload.get("promotable_candidates")
    runtime["promotable_count"] = len(promotable_candidates) if isinstance(promotable_candidates, list) else 0
    return runtime

def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _pattern_strategy_preview(strategy_id: int, *, length: int | None = None) -> list[str]:
    rounds = length or max(4, strategy_id * 2)
    return [get_side_for_round(strategy_id, index) for index in range(rounds)]


def _strategy_catalog() -> dict[str, dict[str, Any]]:
    return {
        "1": {
            "label": "单轮交替",
            "summary": "每 1 轮切换一次方向，节奏最直接。",
            "preview": _pattern_strategy_preview(1),
            "detail": "适合快速观察最基础的涨跌交替节奏。",
        },
        "2": {
            "label": "双轮分组交替",
            "summary": "每 2 轮切换一次方向，默认稳健配置。",
            "preview": _pattern_strategy_preview(2),
            "detail": "当前仓位恢复和研究流程默认围绕这组节奏展开。",
        },
        "3": {
            "label": "三轮分组交替",
            "summary": "每 3 轮切换一次方向，单边持续更久。",
            "preview": _pattern_strategy_preview(3),
            "detail": "适合想观察更长分组惯性的场景。",
        },
        "4": {
            "label": "四轮分组交替",
            "summary": "每 4 轮切换一次方向，分组最长。",
            "preview": _pattern_strategy_preview(4),
            "detail": "更强调单边延续，切换频率最低。",
        },
        "5": {
            "label": "动量信号 V2",
            "summary": "比较本轮 UP 价格相对开盘的变化，强信号才给方向。",
            "preview": ["MOMENTUM", "THRESHOLD", "FALLBACK"],
            "detail": "弱信号时按 SIGNAL_WEAK_SIGNAL_MODE 决定跳过还是回退到基础策略。",
        },
        "6": {
            "label": "币安盘口失衡",
            "summary": "根据币安深度盘口的失衡强弱决定方向。",
            "preview": ["OFI", "THRESHOLD", "SKIP"],
            "detail": "仅在盘口失衡信号足够强且未过期时给出方向，否则按规则跳过。",
        },
        "7": {
            "label": "盘口+动量共识",
            "summary": "币安盘口失衡与预测市场动量必须同向确认，冲突时跳过。",
            "preview": ["OFI", "MOMENTUM", "THRESHOLD", "SKIP"],
            "detail": "仅在两类信号同向、强度足够且满足入场风控时给出方向。",
        },
        "8": {
            "label": "状态切换",
            "summary": "趋势时跟随共识，强冲突时按盘口方向做反转，否则跳过。",
            "preview": ["REGIME", "OFI", "MOMENTUM", "REVERSAL"],
            "detail": "用同一套信号在趋势和过热冲突之间切换，纸面和实盘行为保持一致。",
        },
        "9": {
            "label": "稳定共振",
            "summary": "在策略7共识基础上加入连续稳定、衰减风险与动态价帽过滤。",
            "preview": ["OFI", "MOMENTUM", "STABILITY", "PRICE CAP"],
            "detail": "只在信号持续同向、没有明显衰减且价格低于动态上限时给出方向。",
        },
        "10": {
            "label": "估值优势",
            "summary": "用 Polymarket 价格、币安盘口失衡和本轮动量估算公平概率，只买明显低估的一边。",
            "preview": ["FAIR VALUE", "EDGE", "BUFFER", "SKIP"],
            "detail": "只有估算胜率减去当前买入价和成本缓冲后仍超过阈值时才给出方向。",
        },
        "11": {
            "label": "BTC 概率定价",
            "summary": "用 Binance BTC 本轮起点、当前中间价和剩余时间估算到期概率，只买概率明显高于买入价的一边。",
            "preview": ["BTC MID", "PROBABILITY", "EDGE", "SKIP"],
            "detail": "它不追亏、不靠固定节奏，而是把真实 BTC 距离和时间波动换成概率优势再决定是否入场。",
        },
        "12": {
            "label": "BTC 概率+盘口确认",
            "summary": "先用 BTC 概率定价找低估方向，再要求 Binance OFI 和本轮动量同向确认。",
            "preview": ["BTC EDGE", "OFI", "MOMENTUM", "SKIP"],
            "detail": "它比策略11更保守：概率优势不足会跳过，概率方向和微观结构方向冲突也会跳过。",
        },
    }


def _field_groups() -> list[dict[str, Any]]:
    return [
        {
            "title": "运行模式",
            "description": "控制是否并行启动实盘，并配置实盘所需的钱包凭证。",
            "keys": [
                "TRADE_MODE",
                "MARKET_TIMEFRAME",
                "LIVE_TRADING_ENABLED",
                "POLYMARKET_PRIVATE_KEY",
                "POLYMARKET_FUNDER",
                "POLYMARKET_API_KEY",
                "POLYMARKET_API_SECRET",
                "POLYMARKET_API_PASSPHRASE",
            ],
        },
        {
            "title": "基础策略",
            "description": "只保留策略选择和纸面资金；具体下注与信号参数在每个策略自己的配置区调整。",
            "keys": [
                "STRATEGY_ID",
                "STRATEGY_IDS",
                "PAPER_SIMULATED_WALLET_BALANCE",
            ],
        },
        {
            "title": "实时连接保护",
            "description": "控制 WS 行情刷新与交易陈旧保护阈值。",
            "keys": [
                "WS_ENABLED",
                "WS_QUOTE_STALE_SECONDS",
                "WS_TRADE_GUARD_STALE_SECONDS",
                "WS_CONNECT_TIMEOUT_SECONDS",
                "FINAL_PRICE_WAIT_SECONDS",
                "FINAL_PRICE_POLL_INTERVAL_SECONDS",
            ],
        },
    ]


TIMEFRAME_PRESETS: dict[str, dict[str, dict[str, str]]] = {"5m": {}, "15m": {}}


class ConfigValidationError(ValueError):
    def __init__(self, field_errors: dict[str, str]):
        self.field_errors = dict(field_errors)
        super().__init__("; ".join(self.field_errors.values()))


class DashboardState:
    EDITABLE_CONFIG_KEYS: tuple[str, ...] = (
        "TRADE_MODE",
        "MARKET_TIMEFRAME",
        "PAPER_TIMEFRAMES",
        "LIVE_TRADING_ENABLED",
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_FUNDER",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
        "POLYMARKET_FOK_FALLBACK_TO_FAK",
        "POLYMARKET_PRECHECK_ORDER_BOOK_DEPTH",
        "STRATEGY_ID",
        "STRATEGY_IDS",
        "LIVE_STRATEGY_IDS",
        "PAPER_STRATEGY_IDS",
        "PAPER_SIMULATED_WALLET_BALANCE",
        "WS_ENABLED",
        "WS_QUOTE_STALE_SECONDS",
        "WS_TRADE_GUARD_STALE_SECONDS",
        "WS_CONNECT_TIMEOUT_SECONDS",
        "FINAL_PRICE_WAIT_SECONDS",
        "FINAL_PRICE_POLL_INTERVAL_SECONDS",
        *tuple(
            _paper_profile_config_key(timeframe, field_name)
            for timeframe in SUPPORTED_PAPER_TIMEFRAMES
            for field_name in PAPER_PROFILE_EDITABLE_FIELDS
        ),
    )

    CONFIG_LABELS: dict[str, str] = {
        "ENABLE_LIVE_TRADING": "并行实盘",
        "TRADE_MODE": "运行视角",
        "MARKET_TIMEFRAME": "市场频次",
        "LIVE_TRADING_ENABLED": "并行实盘开关",
        "POLYMARKET_PRIVATE_KEY": "实盘私钥",
        "POLYMARKET_FUNDER": "\u5b9e\u76d8\u94b1\u5305\u5730\u5740",
        "POLYMARKET_API_KEY": "官方 API 访问密钥",
        "POLYMARKET_API_SECRET": "官方 API 签名密钥",
        "POLYMARKET_API_PASSPHRASE": "官方 API 通行口令",
        "POLYMARKET_FOK_FALLBACK_TO_FAK": "FOK 未成交改用 FAK",
        "POLYMARKET_PRECHECK_ORDER_BOOK_DEPTH": "下单前检查盘口深度",
        "STRATEGY_ID": "基础策略",
        "STRATEGY_IDS": "统一策略组合",
        "LIVE_STRATEGY_IDS": "实盘策略组合",
        "PAPER_STRATEGY_IDS": "纸面策略组合",
        "OPEN_DELAY_SECONDS": "开盘后入场秒数",
        "BASE_ORDER_COST": "固定下注金额",
        "MIN_STAKE": "单笔最小下注金额",
        "PAPER_SIMULATED_WALLET_BALANCE": "纸面模拟钱包余额",
        "MAX_CONSECUTIVE_LOSSES": "连亏重置轮数",
        "MAX_STAKE": "单笔最大下注金额",
        "MIN_ENTRY_PRICE": "最低买入价格",
        "MAX_ENTRY_PRICE": "最高有效买入价(含费)",
        "LIVE_MAX_PRICE_IMPROVEMENT": "最大允许价格改善",
        "MAX_PRICE_THRESHOLD": "最高买入价格阈值",
        "OFI_THRESHOLD": "盘口失衡阈值",
        "STRATEGY7_OFI_THRESHOLD": "策略7 盘口失衡阈值",
        "STRATEGY7_MOMENTUM_THRESHOLD": "策略7 动量阈值",
        "STRATEGY7_MAX_MOMENTUM_DELTA": "策略7 动量过热上限",
        "STRATEGY7_MIN_SIGNAL_GAP": "策略7 最小信号优势",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "策略7 最晚确认秒数",
        "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP": "策略7 强信号额外优势",
        "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS": "策略7 强信号放宽秒数",
        "STRATEGY7_DYNAMIC_SIZING_ENABLED": "策略7 动态下注",
        "STRATEGY7_SIZING_REFERENCE_PRICE": "策略7 金额参考价",
        "STRATEGY7_SIZING_PRICE_STEP": "策略7 金额价格步长",
        "STRATEGY7_SIZING_PRICE_STEP_REDUCTION": "策略7 每步缩仓比例",
        "STRATEGY7_SIZING_MIN_MULTIPLIER": "策略7 最小金额倍数",
        "STRATEGY7_SIZING_MAX_MULTIPLIER": "策略7 最大金额倍数",
        "STRATEGY7_SIZING_STRONG_SIGNAL_GAP": "策略7 加仓信号优势",
        "STRATEGY7_SIZING_STRONG_SIGNAL_BOOST": "策略7 强信号金额补偿",
        "STRATEGY9_DYNAMIC_SIZING_ENABLED": "策略9 动态下注",
        "STRATEGY9_SIZING_REFERENCE_PRICE": "策略9 金额参考价",
        "STRATEGY9_SIZING_PRICE_STEP": "策略9 金额价格步长",
        "STRATEGY9_SIZING_PRICE_STEP_REDUCTION": "策略9 每步缩仓比例",
        "STRATEGY9_SIZING_MIN_MULTIPLIER": "策略9 最小金额倍数",
        "STRATEGY9_SIZING_MAX_MULTIPLIER": "策略9 最大金额倍数",
        "STRATEGY9_SIZING_STRONG_SIGNAL_GAP": "策略9 加仓信号优势",
        "STRATEGY9_SIZING_STRONG_SIGNAL_BOOST": "策略9 强信号金额补偿",
        "STRATEGY9_STABILITY_SAMPLE_COUNT": "策略9 稳定采样数",
        "STRATEGY9_STABILITY_REQUIRED_COUNT": "策略9 同向采样数",
        "STRATEGY9_STABILITY_WINDOW_SECONDS": "策略9 稳定窗口秒",
        "STRATEGY9_REVERSAL_LOOKBACK_SECONDS": "策略9 衰减回看秒",
        "STRATEGY9_MAX_SIGNAL_DECAY": "策略9 最大信号衰减",
        "STRATEGY9_BASE_MAX_ENTRY_PRICE": "策略9 普通价格上限",
        "STRATEGY9_STRONG_MAX_ENTRY_PRICE": "策略9 强信号价格上限",
        "STRATEGY9_ULTRA_MAX_ENTRY_PRICE": "策略9 超强信号价格上限",
        "STRATEGY9_STRONG_SIGNAL_GAP": "策略9 强信号优势",
        "STRATEGY9_ULTRA_SIGNAL_GAP": "策略9 超强信号优势",
        "STRATEGY10_MIN_EDGE": "策略10 最小期望优势",
        "STRATEGY10_EDGE_BUFFER": "策略10 成本缓冲",
        "STRATEGY10_OFI_WEIGHT": "策略10 OFI 权重",
        "STRATEGY10_MOMENTUM_WEIGHT": "策略10 动量权重",
        "STRATEGY10_MAX_FAIR_VALUE": "策略10 估值上限",
        "STRATEGY10_MIN_MOMENTUM_DELTA": "策略10 最小动量",
        "STRATEGY10_MAX_MOMENTUM_DELTA": "策略10 最大动量",
        "STRATEGY10_DOWN_MIN_EDGE": "策略10 DOWN 最小优势",
        "STRATEGY10_CONFIRM_BEFORE_ENTRY_SECONDS": "策略10 最晚确认秒数",
        "STRATEGY11_MIN_EDGE": "策略11 最小概率优势",
        "STRATEGY11_EDGE_BUFFER": "策略11 成本缓冲",
        "STRATEGY11_VOLATILITY_BPS_PER_SQRT_MINUTE": "策略11 波动率估计",
        "STRATEGY11_MIN_PROBABILITY": "策略11 最低方向概率",
        "STRATEGY11_MAX_PROBABILITY": "策略11 概率上限",
        "STRATEGY11_CONFIRM_BEFORE_ENTRY_SECONDS": "策略11 最晚确认秒数",
        "SIGNAL_MOMENTUM_THRESHOLD": "动量阈值",
        "SIGNAL_WEAK_SIGNAL_MODE": "弱信号处理",
        "SIGNAL_FALLBACK_STRATEGY_ID": "弱信号回退基础策略",
        "SIGNAL_HISTORY_FIDELITY_SECONDS": "信号采样秒数",
        "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS": "开盘锚点最大偏移秒",
        "SIGNAL_DYNAMIC_THRESHOLD_K": "动态阈值系数K",
        "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS": "动态阈值最少样本点",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "入场前锁边秒数",
        "BINANCE_SIGNAL_STALE_SECONDS": "盘口信号过期秒",
        "MAX_STAKE_SKIP_ALERT_THRESHOLD": "超额跳过告警阈值",
        "WS_ENABLED": "实时连接开关",
        "WS_QUOTE_STALE_SECONDS": "行情过期秒",
        "WS_TRADE_GUARD_STALE_SECONDS": "交易防陈旧阈值秒",
        "WS_CONNECT_TIMEOUT_SECONDS": "实时连接超时秒",
        "FINAL_PRICE_WAIT_SECONDS": "官方结算等待秒",
        "FINAL_PRICE_POLL_INTERVAL_SECONDS": "官方结算轮询秒",
    }

    SELECT_OPTIONS: dict[str, list[str]] = {
        "ENABLE_LIVE_TRADING": ["false", "true"],
        "TRADE_MODE": ["paper", "live", "both"],
        "MARKET_TIMEFRAME": ["5m", "15m"],
        "LIVE_TRADING_ENABLED": ["true", "false"],
        "POLYMARKET_FOK_FALLBACK_TO_FAK": ["true", "false"],
        "POLYMARKET_PRECHECK_ORDER_BOOK_DEPTH": ["true", "false"],
        "STRATEGY_ID": SUPPORTED_STRATEGY_SELECT_OPTIONS,
        "STRATEGY_IDS": SUPPORTED_STRATEGY_SELECT_OPTIONS,
        "LIVE_STRATEGY_IDS": SUPPORTED_STRATEGY_SELECT_OPTIONS,
        "PAPER_STRATEGY_IDS": SUPPORTED_STRATEGY_SELECT_OPTIONS,
        "SIGNAL_WEAK_SIGNAL_MODE": ["SKIP", "FALLBACK"],
        "SIGNAL_FALLBACK_STRATEGY_ID": ["1", "2", "3", "4"],
        "WS_ENABLED": ["true", "false"],
        "STRATEGY7_DYNAMIC_SIZING_ENABLED": ["false", "true"],
        "STRATEGY9_DYNAMIC_SIZING_ENABLED": ["false", "true"],
    }

    CONFIG_ATTR_MAP: dict[str, str] = {
        "TRADE_MODE": "trade_mode",
        "MARKET_TIMEFRAME": "market_timeframe",
        "LIVE_TRADING_ENABLED": "live_trading_enabled",
        "POLYMARKET_PRIVATE_KEY": "live_private_key",
        "POLYMARKET_FUNDER": "live_funder",
        "POLYMARKET_API_KEY": "live_api_key",
        "POLYMARKET_API_SECRET": "live_api_secret",
        "POLYMARKET_API_PASSPHRASE": "live_api_passphrase",
        "POLYMARKET_FOK_FALLBACK_TO_FAK": "live_fok_fallback_to_fak",
        "POLYMARKET_PRECHECK_ORDER_BOOK_DEPTH": "live_precheck_order_book_depth",
        "STRATEGY_ID": "strategy_id",
        "STRATEGY_IDS": "strategy_ids",
        "LIVE_STRATEGY_IDS": "live_strategy_ids",
        "PAPER_STRATEGY_IDS": "paper_strategy_ids",
        "OPEN_DELAY_SECONDS": "open_delay_seconds",
        "BASE_ORDER_COST": "base_order_cost",
        "MIN_STAKE": "min_stake",
        "PAPER_SIMULATED_WALLET_BALANCE": "paper_simulated_wallet_balance",
        "MAX_CONSECUTIVE_LOSSES": "max_consecutive_losses",
        "MAX_STAKE": "max_stake",
        "MIN_ENTRY_PRICE": "min_entry_price",
        "MAX_ENTRY_PRICE": "max_entry_price",
        "LIVE_MAX_PRICE_IMPROVEMENT": "live_max_price_improvement",
        "MAX_PRICE_THRESHOLD": "max_price_threshold",
        "OFI_THRESHOLD": "ofi_threshold",
        "STRATEGY7_OFI_THRESHOLD": "strategy7_ofi_threshold",
        "STRATEGY7_MOMENTUM_THRESHOLD": "strategy7_momentum_threshold",
        "STRATEGY7_MAX_MOMENTUM_DELTA": "strategy7_max_momentum_delta",
        "STRATEGY7_MIN_SIGNAL_GAP": "strategy7_min_signal_gap",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "strategy7_confirm_before_entry_seconds",
        "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP": "strategy7_late_confirm_strong_signal_gap",
        "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS": "strategy7_late_confirm_relax_seconds",
        "STRATEGY7_DYNAMIC_SIZING_ENABLED": "strategy7_dynamic_sizing_enabled",
        "STRATEGY7_SIZING_REFERENCE_PRICE": "strategy7_sizing_reference_price",
        "STRATEGY7_SIZING_PRICE_STEP": "strategy7_sizing_price_step",
        "STRATEGY7_SIZING_PRICE_STEP_REDUCTION": "strategy7_sizing_price_step_reduction",
        "STRATEGY7_SIZING_MIN_MULTIPLIER": "strategy7_sizing_min_multiplier",
        "STRATEGY7_SIZING_MAX_MULTIPLIER": "strategy7_sizing_max_multiplier",
        "STRATEGY7_SIZING_STRONG_SIGNAL_GAP": "strategy7_sizing_strong_signal_gap",
        "STRATEGY7_SIZING_STRONG_SIGNAL_BOOST": "strategy7_sizing_strong_signal_boost",
        "STRATEGY9_DYNAMIC_SIZING_ENABLED": "strategy9_dynamic_sizing_enabled",
        "STRATEGY9_SIZING_REFERENCE_PRICE": "strategy9_sizing_reference_price",
        "STRATEGY9_SIZING_PRICE_STEP": "strategy9_sizing_price_step",
        "STRATEGY9_SIZING_PRICE_STEP_REDUCTION": "strategy9_sizing_price_step_reduction",
        "STRATEGY9_SIZING_MIN_MULTIPLIER": "strategy9_sizing_min_multiplier",
        "STRATEGY9_SIZING_MAX_MULTIPLIER": "strategy9_sizing_max_multiplier",
        "STRATEGY9_SIZING_STRONG_SIGNAL_GAP": "strategy9_sizing_strong_signal_gap",
        "STRATEGY9_SIZING_STRONG_SIGNAL_BOOST": "strategy9_sizing_strong_signal_boost",
        "STRATEGY9_STABILITY_SAMPLE_COUNT": "strategy9_stability_sample_count",
        "STRATEGY9_STABILITY_REQUIRED_COUNT": "strategy9_stability_required_count",
        "STRATEGY9_STABILITY_WINDOW_SECONDS": "strategy9_stability_window_seconds",
        "STRATEGY9_REVERSAL_LOOKBACK_SECONDS": "strategy9_reversal_lookback_seconds",
        "STRATEGY9_MAX_SIGNAL_DECAY": "strategy9_max_signal_decay",
        "STRATEGY9_BASE_MAX_ENTRY_PRICE": "strategy9_base_max_entry_price",
        "STRATEGY9_STRONG_MAX_ENTRY_PRICE": "strategy9_strong_max_entry_price",
        "STRATEGY9_ULTRA_MAX_ENTRY_PRICE": "strategy9_ultra_max_entry_price",
        "STRATEGY9_STRONG_SIGNAL_GAP": "strategy9_strong_signal_gap",
        "STRATEGY9_ULTRA_SIGNAL_GAP": "strategy9_ultra_signal_gap",
        "STRATEGY10_MIN_EDGE": "strategy10_min_edge",
        "STRATEGY10_EDGE_BUFFER": "strategy10_edge_buffer",
        "STRATEGY10_OFI_WEIGHT": "strategy10_ofi_weight",
        "STRATEGY10_MOMENTUM_WEIGHT": "strategy10_momentum_weight",
        "STRATEGY10_MAX_FAIR_VALUE": "strategy10_max_fair_value",
        "STRATEGY10_MIN_MOMENTUM_DELTA": "strategy10_min_momentum_delta",
        "STRATEGY10_MAX_MOMENTUM_DELTA": "strategy10_max_momentum_delta",
        "STRATEGY10_DOWN_MIN_EDGE": "strategy10_down_min_edge",
        "STRATEGY10_CONFIRM_BEFORE_ENTRY_SECONDS": "strategy10_confirm_before_entry_seconds",
        "STRATEGY11_MIN_EDGE": "strategy11_min_edge",
        "STRATEGY11_EDGE_BUFFER": "strategy11_edge_buffer",
        "STRATEGY11_VOLATILITY_BPS_PER_SQRT_MINUTE": "strategy11_volatility_bps_per_sqrt_minute",
        "STRATEGY11_MIN_PROBABILITY": "strategy11_min_probability",
        "STRATEGY11_MAX_PROBABILITY": "strategy11_max_probability",
        "STRATEGY11_CONFIRM_BEFORE_ENTRY_SECONDS": "strategy11_confirm_before_entry_seconds",
        "SIGNAL_MOMENTUM_THRESHOLD": "signal_momentum_threshold",
        "SIGNAL_WEAK_SIGNAL_MODE": "signal_weak_signal_mode",
        "SIGNAL_FALLBACK_STRATEGY_ID": "signal_fallback_strategy_id",
        "SIGNAL_HISTORY_FIDELITY_SECONDS": "signal_history_fidelity_seconds",
        "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS": "signal_anchor_max_offset_seconds",
        "SIGNAL_DYNAMIC_THRESHOLD_K": "signal_dynamic_threshold_k",
        "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS": "signal_dynamic_threshold_min_points",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "signal_lock_before_entry_seconds",
        "BINANCE_SIGNAL_STALE_SECONDS": "binance_signal_stale_seconds",
        "MAX_STAKE_SKIP_ALERT_THRESHOLD": "max_stake_skip_alert_threshold",
        "WS_ENABLED": "ws_enabled",
        "WS_QUOTE_STALE_SECONDS": "ws_quote_stale_seconds",
        "WS_TRADE_GUARD_STALE_SECONDS": "ws_trade_guard_stale_seconds",
        "WS_CONNECT_TIMEOUT_SECONDS": "ws_connect_timeout_seconds",
        "FINAL_PRICE_WAIT_SECONDS": "final_price_wait_seconds",
        "FINAL_PRICE_POLL_INTERVAL_SECONDS": "final_price_poll_interval_seconds",
    }

    INT_CONFIG_KEYS: tuple[str, ...] = (
        "STRATEGY_ID",
        "MAX_CONSECUTIVE_LOSSES",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS",
        "STRATEGY10_CONFIRM_BEFORE_ENTRY_SECONDS",
        "OPEN_DELAY_SECONDS",
        "SIGNAL_FALLBACK_STRATEGY_ID",
        "SIGNAL_HISTORY_FIDELITY_SECONDS",
        "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS",
        "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS",
        "MAX_STAKE_SKIP_ALERT_THRESHOLD",
        "STRATEGY9_STABILITY_SAMPLE_COUNT",
        "STRATEGY9_STABILITY_REQUIRED_COUNT",
        "STRATEGY11_CONFIRM_BEFORE_ENTRY_SECONDS",
        "WS_QUOTE_STALE_SECONDS",
        "WS_CONNECT_TIMEOUT_SECONDS",
    )

    FLOAT_CONFIG_KEYS: tuple[str, ...] = (
        "BASE_ORDER_COST",
        "MIN_STAKE",
        "PAPER_SIMULATED_WALLET_BALANCE",
        "MAX_STAKE",
        "MIN_ENTRY_PRICE",
        "MAX_ENTRY_PRICE",
        "LIVE_MAX_PRICE_IMPROVEMENT",
        "MAX_PRICE_THRESHOLD",
        "BINANCE_SIGNAL_STALE_SECONDS",
        "OFI_THRESHOLD",
        "STRATEGY7_OFI_THRESHOLD",
        "STRATEGY7_MOMENTUM_THRESHOLD",
        "STRATEGY7_MAX_MOMENTUM_DELTA",
        "STRATEGY7_MIN_SIGNAL_GAP",
        "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP",
        "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS",
        "STRATEGY7_SIZING_REFERENCE_PRICE",
        "STRATEGY7_SIZING_PRICE_STEP",
        "STRATEGY7_SIZING_PRICE_STEP_REDUCTION",
        "STRATEGY7_SIZING_MIN_MULTIPLIER",
        "STRATEGY7_SIZING_MAX_MULTIPLIER",
        "STRATEGY7_SIZING_STRONG_SIGNAL_GAP",
        "STRATEGY7_SIZING_STRONG_SIGNAL_BOOST",
        "STRATEGY9_SIZING_REFERENCE_PRICE",
        "STRATEGY9_SIZING_PRICE_STEP",
        "STRATEGY9_SIZING_PRICE_STEP_REDUCTION",
        "STRATEGY9_SIZING_MIN_MULTIPLIER",
        "STRATEGY9_SIZING_MAX_MULTIPLIER",
        "STRATEGY9_SIZING_STRONG_SIGNAL_GAP",
        "STRATEGY9_SIZING_STRONG_SIGNAL_BOOST",
        "STRATEGY9_STABILITY_WINDOW_SECONDS",
        "STRATEGY9_REVERSAL_LOOKBACK_SECONDS",
        "STRATEGY9_MAX_SIGNAL_DECAY",
        "STRATEGY9_BASE_MAX_ENTRY_PRICE",
        "STRATEGY9_STRONG_MAX_ENTRY_PRICE",
        "STRATEGY9_ULTRA_MAX_ENTRY_PRICE",
        "STRATEGY9_STRONG_SIGNAL_GAP",
        "STRATEGY9_ULTRA_SIGNAL_GAP",
        "STRATEGY10_MIN_EDGE",
        "STRATEGY10_EDGE_BUFFER",
        "STRATEGY10_OFI_WEIGHT",
        "STRATEGY10_MOMENTUM_WEIGHT",
        "STRATEGY10_MAX_FAIR_VALUE",
        "STRATEGY10_MIN_MOMENTUM_DELTA",
        "STRATEGY10_MAX_MOMENTUM_DELTA",
        "STRATEGY10_DOWN_MIN_EDGE",
        "STRATEGY11_MIN_EDGE",
        "STRATEGY11_EDGE_BUFFER",
        "STRATEGY11_VOLATILITY_BPS_PER_SQRT_MINUTE",
        "STRATEGY11_MIN_PROBABILITY",
        "STRATEGY11_MAX_PROBABILITY",
        "SIGNAL_MOMENTUM_THRESHOLD",
        "SIGNAL_DYNAMIC_THRESHOLD_K",
        "WS_TRADE_GUARD_STALE_SECONDS",
        "FINAL_PRICE_WAIT_SECONDS",
        "FINAL_PRICE_POLL_INTERVAL_SECONDS",
    )

    BOOL_CONFIG_KEYS: tuple[str, ...] = (
        "LIVE_TRADING_ENABLED",
        "POLYMARKET_FOK_FALLBACK_TO_FAK",
        "POLYMARKET_PRECHECK_ORDER_BOOK_DEPTH",
        "WS_ENABLED",
        "STRATEGY7_DYNAMIC_SIZING_ENABLED",
        "STRATEGY9_DYNAMIC_SIZING_ENABLED",
    )
    STRING_CONFIG_KEYS: tuple[str, ...] = (
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_FUNDER",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
    )
    SECRET_CONFIG_KEYS: tuple[str, ...] = (
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_FUNDER",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
    )
    MASKED_SECRET_VALUE = "********"

    STRATEGY_CATALOG: dict[str, dict[str, Any]] = _strategy_catalog()
    FIELD_GROUPS: list[dict[str, Any]] = _field_groups()
    FIELD_SCOPE: dict[str, str] = {
        "SIGNAL_MOMENTUM_THRESHOLD": "strategy_5_only",
        "SIGNAL_WEAK_SIGNAL_MODE": "strategy_5_only",
        "SIGNAL_FALLBACK_STRATEGY_ID": "strategy_5_only",
        "SIGNAL_HISTORY_FIDELITY_SECONDS": "strategy_5_only",
        "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS": "strategy_5_only",
        "SIGNAL_DYNAMIC_THRESHOLD_K": "strategy_5_only",
        "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS": "strategy_5_only",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "strategy_5_only",
        "STRATEGY7_OFI_THRESHOLD": "strategy_7_only",
        "STRATEGY7_MOMENTUM_THRESHOLD": "strategy_7_only",
        "STRATEGY7_MAX_MOMENTUM_DELTA": "strategy_7_only",
        "STRATEGY7_MIN_SIGNAL_GAP": "strategy_7_only",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "strategy_7_only",
        "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP": "strategy_7_only",
        "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS": "strategy_7_only",
        "STRATEGY7_DYNAMIC_SIZING_ENABLED": "strategy_7_only",
        "STRATEGY7_SIZING_REFERENCE_PRICE": "strategy_7_only",
        "STRATEGY7_SIZING_PRICE_STEP": "strategy_7_only",
        "STRATEGY7_SIZING_PRICE_STEP_REDUCTION": "strategy_7_only",
        "STRATEGY7_SIZING_MIN_MULTIPLIER": "strategy_7_only",
        "STRATEGY7_SIZING_MAX_MULTIPLIER": "strategy_7_only",
        "STRATEGY7_SIZING_STRONG_SIGNAL_GAP": "strategy_7_only",
        "STRATEGY7_SIZING_STRONG_SIGNAL_BOOST": "strategy_7_only",
        "STRATEGY9_DYNAMIC_SIZING_ENABLED": "strategy_9_only",
        "STRATEGY9_SIZING_REFERENCE_PRICE": "strategy_9_only",
        "STRATEGY9_SIZING_PRICE_STEP": "strategy_9_only",
        "STRATEGY9_SIZING_PRICE_STEP_REDUCTION": "strategy_9_only",
        "STRATEGY9_SIZING_MIN_MULTIPLIER": "strategy_9_only",
        "STRATEGY9_SIZING_MAX_MULTIPLIER": "strategy_9_only",
        "STRATEGY9_SIZING_STRONG_SIGNAL_GAP": "strategy_9_only",
        "STRATEGY9_SIZING_STRONG_SIGNAL_BOOST": "strategy_9_only",
        "STRATEGY9_STABILITY_SAMPLE_COUNT": "strategy_9_only",
        "STRATEGY9_STABILITY_REQUIRED_COUNT": "strategy_9_only",
        "STRATEGY9_STABILITY_WINDOW_SECONDS": "strategy_9_only",
        "STRATEGY9_REVERSAL_LOOKBACK_SECONDS": "strategy_9_only",
        "STRATEGY9_MAX_SIGNAL_DECAY": "strategy_9_only",
        "STRATEGY9_BASE_MAX_ENTRY_PRICE": "strategy_9_only",
        "STRATEGY9_STRONG_MAX_ENTRY_PRICE": "strategy_9_only",
        "STRATEGY9_ULTRA_MAX_ENTRY_PRICE": "strategy_9_only",
        "STRATEGY9_STRONG_SIGNAL_GAP": "strategy_9_only",
        "STRATEGY9_ULTRA_SIGNAL_GAP": "strategy_9_only",
        "STRATEGY10_MIN_EDGE": "strategy_10_only",
        "STRATEGY10_EDGE_BUFFER": "strategy_10_only",
        "STRATEGY10_OFI_WEIGHT": "strategy_10_only",
        "STRATEGY10_MOMENTUM_WEIGHT": "strategy_10_only",
        "STRATEGY10_MAX_FAIR_VALUE": "strategy_10_only",
        "STRATEGY10_MIN_MOMENTUM_DELTA": "strategy_10_only",
        "STRATEGY10_MAX_MOMENTUM_DELTA": "strategy_10_only",
        "STRATEGY10_DOWN_MIN_EDGE": "strategy_10_only",
        "STRATEGY10_CONFIRM_BEFORE_ENTRY_SECONDS": "strategy_10_only",
        "STRATEGY11_MIN_EDGE": "strategy_11_only",
        "STRATEGY11_EDGE_BUFFER": "strategy_11_only",
        "STRATEGY11_VOLATILITY_BPS_PER_SQRT_MINUTE": "strategy_11_only",
        "STRATEGY11_MIN_PROBABILITY": "strategy_11_only",
        "STRATEGY11_MAX_PROBABILITY": "strategy_11_only",
        "STRATEGY11_CONFIRM_BEFORE_ENTRY_SECONDS": "strategy_11_only",
    }
    FIELD_HELP: dict[str, str] = {
        "STRATEGY_ID": "策略 1-4 是固定节奏策略，策略 5 是动量策略，策略 6 是 Binance OFI，策略 7/8/9 是组合信号策略，策略 10 是估值优势策略，策略 11 是 BTC 概率定价策略。",
        "STRATEGY_IDS": "统一策略组合。设置后纸面和实盘都使用同一组策略和同一套参数，切换模式只改变执行环境。",
        "LIVE_STRATEGY_IDS": "实盘运行时可轮询的策略列表，按输入顺序去重，例如 2,6。未填写时会回退到 STRATEGY_ID。",
        "PAPER_STRATEGY_IDS": "纸面测试可同时运行多个策略，按输入顺序去重，例如 1,2,6。",
        "STRATEGY10_MIN_EDGE": "策略10 估算胜率减去当前买入价和成本缓冲后的最小优势，低于该值就跳过。",
        "STRATEGY10_EDGE_BUFFER": "策略10 对手续费、价差和延迟预留的额外安全边际。",
        "STRATEGY10_OFI_WEIGHT": "策略10 将 Binance 盘口失衡映射到估值概率的权重。",
        "STRATEGY10_MOMENTUM_WEIGHT": "策略10 将本轮 Polymarket 动量映射到估值概率的权重。",
        "STRATEGY10_MAX_FAIR_VALUE": "策略10 对估算概率做截断，避免单一信号把估值推到极端。",
        "STRATEGY10_MIN_MOMENTUM_DELTA": "策略10 只在轮内 UP 价格动量不低于该值时入场；留空则不限制下限。",
        "STRATEGY10_MAX_MOMENTUM_DELTA": "策略10 只在轮内 UP 价格动量不高于该值时入场；留空则不限制上限。",
        "STRATEGY10_DOWN_MIN_EDGE": "策略10 买 DOWN 时使用的最小优势；留空则沿用策略10最小期望优势。",
        "STRATEGY10_CONFIRM_BEFORE_ENTRY_SECONDS": "策略10 需要在计划入场前至少提前这么多秒完成估值优势确认。",
        "STRATEGY11_MIN_EDGE": "策略11 的估算方向概率减去当前买入价和成本缓冲后的最小优势。",
        "STRATEGY11_EDGE_BUFFER": "策略11 对手续费、价差和延迟预留的额外安全边际。",
        "STRATEGY11_VOLATILITY_BPS_PER_SQRT_MINUTE": "策略11 估算本轮剩余时间概率时使用的 BTC 每平方根分钟波动率，单位为基点。",
        "STRATEGY11_MIN_PROBABILITY": "策略11 只有方向概率达到该下限时才考虑入场。",
        "STRATEGY11_MAX_PROBABILITY": "策略11 对估算概率做截断，避免短时价格跳动把概率推到极端。",
        "STRATEGY11_CONFIRM_BEFORE_ENTRY_SECONDS": "策略11 需要在计划入场前至少提前这么多秒完成概率确认。",
        "ENABLE_LIVE_TRADING": "运行模式选择实盘或纸面+实盘时会自动开启；关闭后只能安全运行纸面测试。",
        "MARKET_TIMEFRAME": "选择当前要玩的 Polymarket BTC 预测频次，仅支持 5 分钟和 15 分钟。",
        "OPEN_DELAY_SECONDS": "OPEN 模式下，从每轮开始后延迟多少秒再尝试入场。",
        "POLYMARKET_PRIVATE_KEY": "实盘钱包私钥，仅在并行实盘开启时需要。",
        "POLYMARKET_FUNDER": "与私钥对应的实盘钱包地址（0x...），并且需要实际承担实盘订单资金。",
        "POLYMARKET_API_KEY": "CLOB 实盘下单凭证，仅用于实盘下单私有接口。",
        "POLYMARKET_API_SECRET": "CLOB 实盘下单签名密钥，仅用于实盘下单私有接口。",
        "POLYMARKET_API_PASSPHRASE": "CLOB 实盘下单通行口令，仅用于实盘下单私有接口。",
        "FINAL_PRICE_WAIT_SECONDS": "实盘轮次结束后，最多等待官方 priceToBeat + finalPrice 的秒数；过期仍未取得时，先按暂记亏损进入下一轮 sizing。",
        "FINAL_PRICE_POLL_INTERVAL_SECONDS": "等待官方 finalPrice 期间的快速重试间隔；建议小于 OPEN_DELAY_SECONDS，避免错过下一轮 sizing。",
        "BASE_ORDER_COST": "每轮固定下注金额；不会根据上一轮亏损自动放大。",
        "MIN_STAKE": "单笔订单允许投入的最小 USDC；低于它时会跳过本轮。",
        "PAPER_SIMULATED_WALLET_BALANCE": "仅纸面模式使用，作为 dry-run 的模拟钱包余额；纸面不会读取真实钱包，但会经过与实盘相同的预算检查节点。",
        "MAX_CONSECUTIVE_LOSSES": "连续亏损达到这个次数后，策略会执行一次止损重置。",
        "MAX_STAKE": "单笔订单允许投入的最大 USDC；超过后会直接跳过本轮。",
        "MIN_ENTRY_PRICE": "目标方向价格低于该值时不入场；留空则不设置下限。",
        "MAX_ENTRY_PRICE": "目标方向含手续费后的有效买入价高于该值时不入场；实盘会反推官方 raw price 作为订单价格保护。",
        "LIVE_MAX_PRICE_IMPROVEMENT": "实盘成交价允许比决策价最多低多少；超过该幅度视为行情已变质。",
        "MAX_PRICE_THRESHOLD": "目标方向价格高于该阈值时不入场。",
        "OFI_THRESHOLD": "策略 6 的 Binance OFI 最小强度要求，低于该阈值直接跳过。",
        "STRATEGY7_OFI_THRESHOLD": "策略 7 对 Binance OFI 的最小强度要求，低于该阈值直接跳过。",
        "STRATEGY7_MOMENTUM_THRESHOLD": "策略 7 对 Polymarket 轮内动量确认的最小要求。",
        "STRATEGY7_MAX_MOMENTUM_DELTA": "策略 7 对 Polymarket 轮内动量的过热上限，超过该值会跳过，留空则不启用。",
        "STRATEGY7_MIN_SIGNAL_GAP": "策略 7 要求 OFI 和动量超过阈值的最小额外优势，避免擦线交易。",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "策略 7 需要在计划入场前至少提前这么多秒完成双信号确认。",
        "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP": "策略 7 只有在 OFI 和动量都额外强于阈值时，才允许走晚确认放宽通道。",
        "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS": "满足强信号条件后，可从策略 7 的最晚确认要求里减去的秒数。",
        "STRATEGY7_DYNAMIC_SIZING_ENABLED": "开启后策略 7 会按买入价和共识强度调整下注金额；默认关闭，建议先用于纸面测试。",
        "STRATEGY7_SIZING_REFERENCE_PRICE": "动态下注的参考价格。高于该价格会开始按价格步长缩仓。",
        "STRATEGY7_SIZING_PRICE_STEP": "买入价每高出参考价多少，触发一次金额缩减。",
        "STRATEGY7_SIZING_PRICE_STEP_REDUCTION": "每个价格步长对应的金额缩减比例。例如 0.10 表示减少 10%。",
        "STRATEGY7_SIZING_MIN_MULTIPLIER": "动态下注允许的最低金额倍数。",
        "STRATEGY7_SIZING_MAX_MULTIPLIER": "动态下注允许的最高金额倍数；默认 1 表示不放大原始下注。",
        "STRATEGY7_SIZING_STRONG_SIGNAL_GAP": "策略 7 判断强信号时，OFI 和动量需要额外超过阈值的幅度。",
        "STRATEGY7_SIZING_STRONG_SIGNAL_BOOST": "强信号时给金额倍数的补偿，受最高金额倍数限制。",
        "STRATEGY9_DYNAMIC_SIZING_ENABLED": "开启后策略 9 会按买入价和共振强度调整下注金额；默认关闭，建议先用于纸面测试。",
        "STRATEGY9_SIZING_REFERENCE_PRICE": "策略 9 动态下注的参考价格。高于该价格会开始按价格步长缩仓。",
        "STRATEGY9_SIZING_PRICE_STEP": "策略 9 买入价每高出参考价多少，触发一次金额缩减。",
        "STRATEGY9_SIZING_PRICE_STEP_REDUCTION": "策略 9 每个价格步长对应的金额缩减比例。例如 0.10 表示减少 10%。",
        "STRATEGY9_SIZING_MIN_MULTIPLIER": "策略 9 动态下注允许的最低金额倍数。",
        "STRATEGY9_SIZING_MAX_MULTIPLIER": "策略 9 动态下注允许的最高金额倍数；默认 1 表示不放大原始下注。",
        "STRATEGY9_SIZING_STRONG_SIGNAL_GAP": "策略 9 判断强信号加仓补偿时，OFI 和动量需要额外超过阈值的幅度。",
        "STRATEGY9_SIZING_STRONG_SIGNAL_BOOST": "策略 9 强信号时给金额倍数的补偿，受最高金额倍数限制。",
        "STRATEGY9_STABILITY_SAMPLE_COUNT": "策略 9 在稳定窗口内要求的有效采样数量。",
        "STRATEGY9_STABILITY_REQUIRED_COUNT": "策略 9 在稳定窗口内要求同向共振的最少采样数量。",
        "STRATEGY9_STABILITY_WINDOW_SECONDS": "策略 9 判断稳定共振的回看秒数。",
        "STRATEGY9_REVERSAL_LOOKBACK_SECONDS": "策略 9 判断信号衰减风险的回看秒数。",
        "STRATEGY9_MAX_SIGNAL_DECAY": "策略 9 当前信号相对近期峰值允许衰减的最大比例。",
        "STRATEGY9_BASE_MAX_ENTRY_PRICE": "策略 9 普通信号允许的最高有效买入价，含手续费。",
        "STRATEGY9_STRONG_MAX_ENTRY_PRICE": "策略 9 强信号允许的最高有效买入价，含手续费。",
        "STRATEGY9_ULTRA_MAX_ENTRY_PRICE": "策略 9 超强信号允许的最高有效买入价，含手续费。",
        "STRATEGY9_STRONG_SIGNAL_GAP": "策略 9 判断强信号时，OFI 和动量需要额外超过阈值的幅度。",
        "STRATEGY9_ULTRA_SIGNAL_GAP": "策略 9 判断超强信号时，OFI 和动量需要额外超过阈值的幅度。",
        "SIGNAL_MOMENTUM_THRESHOLD": "策略 5 的基础动量阈值，比较 abs(current_up - open_up)。",
        "SIGNAL_WEAK_SIGNAL_MODE": "弱动量信号的处理方式：直接跳过，或回退到固定节奏策略。",
        "SIGNAL_FALLBACK_STRATEGY_ID": "仅当策略 5 在弱信号下回退时使用。",
        "SIGNAL_HISTORY_FIDELITY_SECONDS": "历史价格拉取的采样粒度；数值越小越精确，但请求也更重。",
        "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS": "动量逻辑对齐开盘锚点时允许的最大时间偏移。",
        "SIGNAL_DYNAMIC_THRESHOLD_K": "动态阈值系数，运行时会使用 max(基础阈值, k * sigma)。",
        "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS": "启用动态阈值前要求的最少样本点数量。",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "在计划入场前提前锁定方向，避免最后几秒来回翻边。",
        "BINANCE_SIGNAL_STALE_SECONDS": "盘口失衡信号最多允许滞后多少秒，超过后视为过期。",
        "MAX_STAKE_SKIP_ALERT_THRESHOLD": "连续因超过 MAX_STAKE 而跳过多少次后触发提醒。",
        "WS_ENABLED": "优先使用实时连接行情缓存；必要时再回退到接口拉取。",
        "WS_QUOTE_STALE_SECONDS": "实时连接行情在多久未更新后视为过期。",
        "WS_TRADE_GUARD_STALE_SECONDS": "入场前若实时连接行情年龄超过该阈值，则禁止本轮交易。",
        "WS_CONNECT_TIMEOUT_SECONDS": "建立实时连接会话时的连接超时秒数。",
    }

    @classmethod
    def _normalize_bool_config_value(cls, key: str, value: str) -> str:
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return "true"
        if normalized in {"0", "false", "no", "off"}:
            return "false"
        raise ValueError(f"Invalid value for {key}: expected true/false, got {value!r}")

    @classmethod
    def _normalize_config_value(cls, key: str, value: str) -> str:
        normalized = value.strip()
        if normalized == "":
            return ""

        if key == "PAPER_TIMEFRAMES":
            return _normalize_paper_timeframes_value(normalized)

        strategy_profile_key = _split_strategy_profile_key(key)
        if strategy_profile_key is not None:
            _, _, base_key = strategy_profile_key
            if base_key in cls.BOOL_CONFIG_KEYS:
                return cls._normalize_bool_config_value(key, normalized)
            if base_key in cls.SELECT_OPTIONS:
                allowed = cls.SELECT_OPTIONS[base_key]
                upper_value = normalized.upper()
                lower_value = normalized.lower()
                if normalized in allowed:
                    return normalized
                if upper_value in allowed:
                    return upper_value
                if lower_value in allowed:
                    return lower_value
                raise ValueError(f"Invalid value for {key}: expected one of {allowed}, got {value!r}")
            if base_key in cls.INT_CONFIG_KEYS:
                try:
                    return str(int(normalized))
                except ValueError as exc:
                    raise ValueError(f"Invalid value for {key}: expected integer, got {value!r}") from exc
            if base_key in cls.FLOAT_CONFIG_KEYS:
                try:
                    return str(float(normalized))
                except ValueError as exc:
                    raise ValueError(f"Invalid value for {key}: expected number, got {value!r}") from exc

        paper_profile_key = _split_paper_profile_key(key)
        if paper_profile_key is not None:
            _, base_key = paper_profile_key
            if base_key == "STRATEGY_IDS":
                return _normalize_strategy_id_list_value(normalized)
            if base_key in cls.BOOL_CONFIG_KEYS:
                return cls._normalize_bool_config_value(key, normalized)
            if base_key in cls.SELECT_OPTIONS:
                allowed = cls.SELECT_OPTIONS[base_key]
                upper_value = normalized.upper()
                lower_value = normalized.lower()
                if normalized in allowed:
                    return normalized
                if upper_value in allowed:
                    return upper_value
                if lower_value in allowed:
                    return lower_value
                raise ValueError(f"Invalid value for {key}: expected one of {allowed}, got {value!r}")
            if base_key in cls.INT_CONFIG_KEYS:
                try:
                    return str(int(normalized))
                except ValueError as exc:
                    raise ValueError(f"Invalid value for {key}: expected integer, got {value!r}") from exc
            if base_key in cls.FLOAT_CONFIG_KEYS:
                try:
                    return str(float(normalized))
                except ValueError as exc:
                    raise ValueError(f"Invalid value for {key}: expected number, got {value!r}") from exc

        if key == "LIVE_STRATEGY_IDS":
            return _normalize_live_strategy_id_list_value(normalized)

        if key == "STRATEGY_IDS":
            return _normalize_unified_strategy_id_list_value(normalized)

        if key == "PAPER_STRATEGY_IDS":
            return _normalize_strategy_id_list_value(normalized)

        if key in cls.BOOL_CONFIG_KEYS:
            return cls._normalize_bool_config_value(key, normalized)

        if key in cls.SELECT_OPTIONS:
            allowed = cls.SELECT_OPTIONS[key]
            upper_value = normalized.upper()
            lower_value = normalized.lower()
            if normalized in allowed:
                return normalized
            if upper_value in allowed:
                return upper_value
            if lower_value in allowed:
                return lower_value
            raise ValueError(f"Invalid value for {key}: expected one of {allowed}, got {value!r}")

        if key in cls.INT_CONFIG_KEYS:
            try:
                return str(int(normalized))
            except ValueError as exc:
                raise ValueError(f"Invalid value for {key}: expected integer, got {value!r}") from exc

        if key in cls.FLOAT_CONFIG_KEYS:
            try:
                return str(float(normalized))
            except ValueError as exc:
                raise ValueError(f"Invalid value for {key}: expected number, got {value!r}") from exc

        if key in cls.STRING_CONFIG_KEYS:
            return normalized

        raise ValueError(f"Unsupported config key: {key}")

    def __init__(
        self,
        *,
        env_file: Path,
        running_trade_mode: str = "paper",
        runtime_control: RuntimeControl | None = None,
        notify_mode_change: Any | None = None,
        notify_runtime_reload: Any | None = None,
    ) -> None:
        self.env_file = Path(env_file)
        self.running_trade_mode = str(running_trade_mode or "paper").strip().lower() or "paper"
        self.runtime_control = runtime_control
        self.notify_mode_change = notify_mode_change
        self.notify_runtime_reload = notify_runtime_reload
        self._lock = threading.RLock()
        self._env_values = load_env_file_values(self.env_file)
        self._cfg = self._build_config(self._env_values)
        self._client = PolymarketClient(self._cfg)
        self._binance_signal_service = self._build_binance_signal_service(self._cfg)
        self._result_validation_cache: dict[str, dict[str, str]] = {}
        self._last_saved_at: datetime | None = None

    def close(self) -> None:
        with self._lock:
            client = self._client
            binance_signal_service = self._binance_signal_service
            self._client = None  # type: ignore[assignment]
            self._binance_signal_service = None
        if client is not None:
            client.close()
        if binance_signal_service is not None:
            binance_signal_service.close()

    def _build_config(self, env_values: dict[str, str]) -> AppConfig:
        return build_config_from_env_values(env_values)

    def _build_binance_signal_service(self, cfg: AppConfig) -> BinanceDepth5SignalService | None:
        strategy_ids = list(getattr(cfg, "strategy_ids", []) or [])
        if not strategy_ids:
            strategy_ids = []
            strategy_ids.extend(getattr(cfg, "paper_strategy_ids", []) or [])
            strategy_ids.extend(getattr(cfg, "live_strategy_ids", []) or [])
        ofi_strategy_ids = {6, 7, 8, 9, 10, 11, 12}
        if cfg.strategy_id not in ofi_strategy_ids and not any(strategy_id in ofi_strategy_ids for strategy_id in strategy_ids):
            return None
        service = BinanceDepth5SignalService(ws_url=cfg.binance_ws_url, stream=cfg.binance_depth_stream)
        service.start()
        return service

    @classmethod
    def _mask_secret(cls, value: str | None) -> str:
        if not value:
            return ""
        return cls.MASKED_SECRET_VALUE

    def _effective_config_value(self, key: str) -> str:
        if key == "PAPER_TIMEFRAMES":
            return ",".join(getattr(self._cfg, "paper_timeframes", []) or [])
        strategy_profile_key = _split_strategy_profile_key(key)
        if strategy_profile_key is not None:
            mode, strategy_id_text, base_key = strategy_profile_key
            if mode == "live":
                profile = getattr(self._cfg, "live_profiles", {}).get(int(strategy_id_text))
            elif mode == "paper":
                profile = getattr(self._cfg, "paper_strategy_profiles", {}).get(int(strategy_id_text))
            else:
                profile = (
                    getattr(self._cfg, "paper_strategy_profiles", {}).get(int(strategy_id_text))
                    or getattr(self._cfg, "live_profiles", {}).get(int(strategy_id_text))
                )
            if profile is None:
                return self._effective_config_value(base_key)
            value = getattr(profile, self.CONFIG_ATTR_MAP[base_key])
            if value is None:
                return ""
            return _fmt_env(value)
        paper_profile_key = _split_paper_profile_key(key)
        if paper_profile_key is not None:
            timeframe, base_key = paper_profile_key
            profile = getattr(self._cfg, "paper_profiles", {}).get(timeframe)
            if profile is None:
                return ""
            attr_name = "paper_strategy_ids" if base_key == "STRATEGY_IDS" else self.CONFIG_ATTR_MAP[base_key]
            value = getattr(profile, attr_name)
            if value is None:
                return ""
            if base_key == "STRATEGY_IDS":
                return ",".join(str(item) for item in value)
            return _fmt_env(value)
        value = getattr(self._cfg, self.CONFIG_ATTR_MAP[key])
        if value is None:
            return ""
        if key in {"STRATEGY_IDS", "LIVE_STRATEGY_IDS", "PAPER_STRATEGY_IDS"}:
            return ",".join(str(item) for item in value)
        return _fmt_env(value)

    def _active_strategy_profile_payload(self, env_values: dict[str, str]) -> dict[str, Any]:
        saved_mode = str(env_values.get("TRADE_MODE") or self._cfg.trade_mode or "paper").strip().lower()
        live_enabled = str(env_values.get("LIVE_TRADING_ENABLED") or getattr(self._cfg, "live_trading_enabled", False)).strip().lower()
        mode = "live" if saved_mode == "live" and live_enabled in {"1", "true", "yes", "on"} else "paper"
        if mode == "live":
            strategy_ids = list(getattr(self._cfg, "strategy_ids", []) or getattr(self._cfg, "live_strategy_ids", []) or [self._cfg.strategy_id])
        else:
            timeframe = _normalize_timeframe_filter(getattr(self._cfg, "market_timeframe", "5m"))
            profile = getattr(self._cfg, "paper_profiles", {}).get(timeframe)
            strategy_ids = list(
                getattr(self._cfg, "strategy_ids", [])
                or (getattr(profile, "paper_strategy_ids", None) if profile is not None else None)
                or getattr(self._cfg, "paper_strategy_ids", [])
                or [self._cfg.strategy_id]
            )

        strategies: dict[str, Any] = {}
        for strategy_id in strategy_ids:
            strategy_text = str(strategy_id)
            fields: dict[str, Any] = {}
            for base_key in _strategy_profile_field_names(strategy_id):
                prefixed_key = _strategy_profile_key_for_mode(mode, strategy_text, base_key)
                inherited_value = self._effective_config_value(base_key)
                explicit_value = self._env_values.get(prefixed_key)
                value = explicit_value if explicit_value is not None else self._effective_config_value(prefixed_key)
                fields[base_key] = {
                    "key": prefixed_key,
                    "label": self.CONFIG_LABELS.get(base_key, base_key),
                    "value": value,
                    "inherited_value": inherited_value,
                    "inherited": explicit_value is None,
                    "options": list(self.SELECT_OPTIONS.get(base_key, [])),
                }
            strategies[strategy_text] = {
                "strategy": strategy_text,
                "label": self.STRATEGY_CATALOG.get(strategy_text, {}).get("label", f"策略 {strategy_text}"),
                "fields": fields,
            }
        return {
            "mode": mode,
            "strategies": strategies,
        }

    def _masked_env_values(self, env_values: dict[str, str]) -> dict[str, str]:
        masked = dict(env_values)
        for key in self.SECRET_CONFIG_KEYS:
            masked[key] = self._mask_secret(masked.get(key))
        return masked

    def _build_runtime_status(
        self,
        env_values: dict[str, str],
        config_warnings: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        saved_mode = str(env_values.get("TRADE_MODE") or self._cfg.trade_mode or "paper").strip().lower() or "paper"
        runtime_snapshot = self.runtime_control.snapshot() if self.runtime_control is not None else None
        active_mode = str((runtime_snapshot.active_mode if runtime_snapshot is not None else self.running_trade_mode) or "paper").strip().lower() or "paper"
        desired_mode = str((runtime_snapshot.desired_mode if runtime_snapshot is not None else saved_mode) or "paper").strip().lower() or "paper"
        switch_state = runtime_snapshot.switch_state if runtime_snapshot is not None else ("idle" if desired_mode == active_mode else "pending")
        switch_reason = runtime_snapshot.switch_reason if runtime_snapshot is not None else None
        validation_values = dict(self._env_values)
        validation_values.update(env_values)
        validation_values["TRADE_MODE"] = "live"
        live_ready = False
        live_validation_error = None
        validated_live_cfg = None
        try:
            validated_live_cfg = self._build_config(validation_values)
            validate_live_runtime_config(validated_live_cfg)
            live_ready = True
        except Exception as exc:
            live_validation_error = _localize_runtime_message(str(exc))

        live_cfg = validated_live_cfg if validated_live_cfg is not None else self._build_config(validation_values)
        live_strategy_ids = [str(item) for item in (getattr(live_cfg, "live_strategy_ids", None) or [live_cfg.strategy_id])]
        live_session_state = load_session_state(
            live_cfg.logs_dir / "live_session_state.json",
            effective_live_strategy_ids=[int(item) for item in live_strategy_ids],
        )
        live_strategy_states = {
            str(strategy_id): asdict(strategy_state)
            for strategy_id, strategy_state in getattr(live_session_state, "live_strategies", {}).items()
        }
        live_pending_live_order = any(
            bool(state.get("pending_live_slug"))
            for strategy_id, state in live_strategy_states.items()
        )
        optimizer_runtime = _load_optimizer_runtime(live_cfg.logs_dir / "optimizer_state.json")

        return {
            "saved_mode": saved_mode,
            "running_mode": active_mode,
            "restart_required": saved_mode != active_mode,
            "live_ready": live_ready,
            "live_validation_error": live_validation_error,
            "active_mode": active_mode,
            "desired_mode": desired_mode,
                "switch_state": switch_state,
                "switch_reason": switch_reason,
                "current_round_slug": runtime_snapshot.current_round_slug if runtime_snapshot is not None else None,
                "round_in_progress": runtime_snapshot.round_in_progress if runtime_snapshot is not None else False,
                "safe_to_switch": runtime_snapshot.safe_to_switch if runtime_snapshot is not None else (saved_mode == active_mode),
                "pending_live_order": bool((runtime_snapshot.pending_live_order if runtime_snapshot is not None else False) or live_pending_live_order),
                "runtime_alert_code": runtime_snapshot.runtime_alert_code if runtime_snapshot is not None else None,
                "runtime_alert_message": (
                    _localize_runtime_message(runtime_snapshot.runtime_alert_message)
                    if runtime_snapshot is not None
                    else None
                ),
                "runtime_alert_level": runtime_snapshot.runtime_alert_level if runtime_snapshot is not None else None,
                "runtime_alert_at": runtime_snapshot.runtime_alert_at if runtime_snapshot is not None else None,
                "live_strategy_ids": live_strategy_ids,
                "live_strategy_states": live_strategy_states,
            "optimizer_enabled": optimizer_runtime["enabled"],
            "optimizer_last_run_at": optimizer_runtime["last_run_at"],
            "optimizer_champion_id": optimizer_runtime["champion_id"],
            "optimizer_active_challengers": optimizer_runtime["active_challengers"],
            "optimizer_promotable_count": optimizer_runtime["promotable_count"],
            "config_warning_count": len(config_warnings or {}),
        }

    def _refresh_runtime(self) -> None:
        with self._lock:
            old_client = self._client
            old_binance_signal_service = self._binance_signal_service
            self._cfg = self._build_config(self._env_values)
            self._client = PolymarketClient(self._cfg)
            self._binance_signal_service = self._build_binance_signal_service(self._cfg)
        old_client.close()
        if old_binance_signal_service is not None:
            old_binance_signal_service.close()

    def _merged_env_values(self) -> tuple[dict[str, str], dict[str, str]]:
        merged: dict[str, str] = {}
        validation_errors: dict[str, str] = {}
        for key in self.EDITABLE_CONFIG_KEYS:
            effective_value = self._effective_config_value(key)
            if key in self._env_values:
                raw_value = self._env_values[key]
                try:
                    merged[key] = self._normalize_config_value(key, raw_value)
                except ValueError as exc:
                    merged[key] = effective_value
                    validation_errors[key] = str(exc)
            else:
                merged[key] = effective_value
        if LIVE_STRATEGY_IDS in self._env_values:
            _deduplicate_paper_live_strategy_ids(merged)
        return merged, validation_errors

    def get_config_payload(self) -> dict[str, Any]:
        with self._lock:
            env_values, validation_errors = self._merged_env_values()
            for key, value in self._env_values.items():
                strategy_profile_key = _split_strategy_profile_key(key)
                if strategy_profile_key is not None and strategy_profile_key[0] == "shared":
                    env_values[key] = value
            config_warnings = collect_config_warnings(self._env_values)
            runtime_status = self._build_runtime_status(env_values, config_warnings=config_warnings)
            strategy_catalog = json.loads(json.dumps(self.STRATEGY_CATALOG))
            field_groups = json.loads(json.dumps(self.FIELD_GROUPS))
            if field_groups:
                field_groups[0]["title"] = "运行模式"
            select_options = json.loads(json.dumps(self.SELECT_OPTIONS))
            select_options["STRATEGY_ID"] = list(SUPPORTED_STRATEGY_SELECT_OPTIONS)
            select_options["STRATEGY_IDS"] = list(SUPPORTED_STRATEGY_SELECT_OPTIONS)
            select_options["PAPER_STRATEGY_IDS"] = list(SUPPORTED_STRATEGY_SELECT_OPTIONS)
            select_options["LIVE_STRATEGY_IDS"] = list(SUPPORTED_STRATEGY_SELECT_OPTIONS)
            paper_profiles = {
                timeframe: {
                    "strategy_id": str(profile.strategy_id),
                    "paper_strategy_ids": [str(item) for item in profile.paper_strategy_ids],
                    "base_order_cost": _fmt_env(profile.base_order_cost),
                    "max_consecutive_losses": _fmt_env(profile.max_consecutive_losses),
                    "min_stake": _fmt_env(profile.min_stake) if profile.min_stake is not None else "",
                    "max_stake": _fmt_env(profile.max_stake) if profile.max_stake is not None else "",
                    "min_entry_price": _fmt_env(profile.min_entry_price) if profile.min_entry_price is not None else "",
                    "max_entry_price": _fmt_env(profile.max_entry_price),
                    "open_delay_seconds": _fmt_env(profile.open_delay_seconds),
                    "signal_momentum_threshold": _fmt_env(profile.signal_momentum_threshold),
                    "ofi_threshold": _fmt_env(profile.ofi_threshold),
                    "binance_signal_stale_seconds": _fmt_env(profile.binance_signal_stale_seconds),
                    "strategy7_ofi_threshold": _fmt_env(profile.strategy7_ofi_threshold),
                    "strategy7_momentum_threshold": _fmt_env(profile.strategy7_momentum_threshold),
                    "strategy7_max_momentum_delta": (
                        _fmt_env(profile.strategy7_max_momentum_delta)
                        if profile.strategy7_max_momentum_delta is not None
                        else ""
                    ),
                    "strategy7_max_entry_price": _fmt_env(profile.strategy7_max_entry_price),
                    "strategy7_dynamic_sizing_enabled": "true" if profile.strategy7_dynamic_sizing_enabled else "false",
                    "strategy7_sizing_reference_price": _fmt_env(profile.strategy7_sizing_reference_price),
                    "strategy7_sizing_price_step": _fmt_env(profile.strategy7_sizing_price_step),
                    "strategy7_sizing_price_step_reduction": _fmt_env(profile.strategy7_sizing_price_step_reduction),
                    "strategy7_sizing_min_multiplier": _fmt_env(profile.strategy7_sizing_min_multiplier),
                    "strategy7_sizing_max_multiplier": _fmt_env(profile.strategy7_sizing_max_multiplier),
                    "strategy7_sizing_strong_signal_gap": _fmt_env(profile.strategy7_sizing_strong_signal_gap),
                    "strategy7_sizing_strong_signal_boost": _fmt_env(profile.strategy7_sizing_strong_signal_boost),
                    "strategy9_dynamic_sizing_enabled": "true" if profile.strategy9_dynamic_sizing_enabled else "false",
                    "strategy9_sizing_reference_price": _fmt_env(profile.strategy9_sizing_reference_price),
                    "strategy9_sizing_price_step": _fmt_env(profile.strategy9_sizing_price_step),
                    "strategy9_sizing_price_step_reduction": _fmt_env(profile.strategy9_sizing_price_step_reduction),
                    "strategy9_sizing_min_multiplier": _fmt_env(profile.strategy9_sizing_min_multiplier),
                    "strategy9_sizing_max_multiplier": _fmt_env(profile.strategy9_sizing_max_multiplier),
                    "strategy9_sizing_strong_signal_gap": _fmt_env(profile.strategy9_sizing_strong_signal_gap),
                    "strategy9_sizing_strong_signal_boost": _fmt_env(profile.strategy9_sizing_strong_signal_boost),
                    "strategy9_stability_sample_count": _fmt_env(profile.strategy9_stability_sample_count),
                    "strategy9_stability_required_count": _fmt_env(profile.strategy9_stability_required_count),
                    "strategy9_stability_window_seconds": _fmt_env(profile.strategy9_stability_window_seconds),
                    "strategy9_reversal_lookback_seconds": _fmt_env(profile.strategy9_reversal_lookback_seconds),
                    "strategy9_max_signal_decay": _fmt_env(profile.strategy9_max_signal_decay),
                    "strategy9_base_max_entry_price": _fmt_env(profile.strategy9_base_max_entry_price),
                    "strategy9_strong_max_entry_price": _fmt_env(profile.strategy9_strong_max_entry_price),
                    "strategy9_ultra_max_entry_price": _fmt_env(profile.strategy9_ultra_max_entry_price),
                    "strategy9_strong_signal_gap": _fmt_env(profile.strategy9_strong_signal_gap),
                    "strategy9_ultra_signal_gap": _fmt_env(profile.strategy9_ultra_signal_gap),
                    "strategy10_min_edge": _fmt_env(profile.strategy10_min_edge),
                    "strategy10_edge_buffer": _fmt_env(profile.strategy10_edge_buffer),
                    "strategy10_ofi_weight": _fmt_env(profile.strategy10_ofi_weight),
                    "strategy10_momentum_weight": _fmt_env(profile.strategy10_momentum_weight),
                    "strategy10_max_fair_value": _fmt_env(profile.strategy10_max_fair_value),
                    "strategy10_min_momentum_delta": (
                        _fmt_env(profile.strategy10_min_momentum_delta)
                        if profile.strategy10_min_momentum_delta is not None
                        else ""
                    ),
                    "strategy10_max_momentum_delta": (
                        _fmt_env(profile.strategy10_max_momentum_delta)
                        if profile.strategy10_max_momentum_delta is not None
                        else ""
                    ),
                    "strategy10_down_min_edge": (
                        _fmt_env(profile.strategy10_down_min_edge)
                        if profile.strategy10_down_min_edge is not None
                        else ""
                    ),
                    "strategy10_confirm_before_entry_seconds": _fmt_env(profile.strategy10_confirm_before_entry_seconds),
                    "strategy11_min_edge": _fmt_env(profile.strategy11_min_edge),
                    "strategy11_edge_buffer": _fmt_env(profile.strategy11_edge_buffer),
                    "strategy11_volatility_bps_per_sqrt_minute": _fmt_env(profile.strategy11_volatility_bps_per_sqrt_minute),
                    "strategy11_min_probability": _fmt_env(profile.strategy11_min_probability),
                    "strategy11_max_probability": _fmt_env(profile.strategy11_max_probability),
                    "strategy11_confirm_before_entry_seconds": _fmt_env(profile.strategy11_confirm_before_entry_seconds),
                }
                for timeframe, profile in getattr(self._cfg, "paper_profiles", {}).items()
            }
            strategy_profiles = self._active_strategy_profile_payload(env_values)
            return {
                "env_file": str(self.env_file),
                "env_values": self._masked_env_values(env_values),
                "timeframe_presets": json.loads(json.dumps(TIMEFRAME_PRESETS)),
                "editable_keys": list(self.EDITABLE_CONFIG_KEYS),
                "labels": self.CONFIG_LABELS,
                "select_options": select_options,
                "strategy_catalog": strategy_catalog,
                "field_groups": field_groups,
                "field_scope": self.FIELD_SCOPE,
                "field_help": self.FIELD_HELP,
                "validation_errors": validation_errors,
                "config_warnings": config_warnings,
                "runtime_status": runtime_status,
                "saved_at": _iso(self._last_saved_at),
                "paper_timeframes": list(getattr(self._cfg, "paper_timeframes", [])),
                "paper_profiles": paper_profiles,
                "strategy_profiles": strategy_profiles,
            }

    def update_config(self, values: dict[str, str]) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise ValueError("Config payload must be an object.")
        unsupported = sorted(
            key
            for key in values.keys()
            if key not in self.EDITABLE_CONFIG_KEYS and _split_strategy_profile_key(key) is None
        )
        if unsupported:
            raise ValueError(f"Unsupported keys: {', '.join(unsupported)}")

        normalized_updates: dict[str, str] = {}
        field_errors: dict[str, str] = {}
        with self._lock:
            preserved_masked_values = {
                key: self._env_values.get(key) or self._effective_config_value(key)
                for key in self.SECRET_CONFIG_KEYS
            }
            preserved_masks = {
                key: self._mask_secret(value)
                for key, value in preserved_masked_values.items()
            }

        for raw_key, value in values.items():
            key = _shared_strategy_profile_update_key(raw_key)
            normalized = "" if value is None else str(value).strip()
            if key in self.SECRET_CONFIG_KEYS and normalized == preserved_masks.get(key) and preserved_masked_values.get(key):
                normalized_updates[key] = preserved_masked_values[key]
                continue
            if normalized == "":
                normalized_updates[key] = ""
                continue
            try:
                normalized_updates[key] = self._normalize_config_value(key, normalized)
            except ValueError as exc:
                field_errors[key] = str(exc)
        if field_errors:
            raise ConfigValidationError(field_errors)

        with self._lock:
            for key, normalized in normalized_updates.items():
                if normalized == "":
                    self._env_values.pop(key, None)
                else:
                    self._env_values[key] = normalized
            _deduplicate_paper_live_strategy_ids(self._env_values)
            _write_env_file(self.env_file, self._env_values)
            self._last_saved_at = datetime.now(timezone.utc)

        previous_mode = None
        next_mode = None
        previous_timeframe = None
        previous_live_enabled = None
        next_live_enabled = None
        with self._lock:
            previous_mode = str(self._cfg.trade_mode or 'paper').strip().lower() or 'paper'
            previous_timeframe = getattr(self._cfg, 'market_timeframe', '5m')
            previous_live_enabled = bool(getattr(self._cfg, 'live_trading_enabled', False))
            next_mode = str(self._env_values.get('TRADE_MODE') or self._cfg.trade_mode or 'paper').strip().lower() or 'paper'
        self._refresh_runtime()
        next_timeframe = getattr(self._cfg, 'market_timeframe', '5m')
        next_live_enabled = bool(getattr(self._cfg, 'live_trading_enabled', False))
        if self.notify_runtime_reload is not None:
            if previous_timeframe != next_timeframe:
                self.notify_runtime_reload('market_timeframe')
            elif previous_live_enabled != next_live_enabled:
                self.notify_runtime_reload('live_trading_enabled')
        if self.notify_mode_change is not None and previous_mode != next_mode:
            self.notify_mode_change(next_mode)
        return self.get_config_payload()

    @staticmethod
    def _reset_strategy_sizing_state(strategy_state: Any) -> None:
        strategy_state.round_index = 0
        strategy_state.recovery_loss = 0.0
        strategy_state.consecutive_losses = 0
        strategy_state.consecutive_max_stake_skips = 0
        strategy_state.signal_round_slug = None
        strategy_state.signal_round_open_up_price = None
        strategy_state.signal_round_locked_side = None
        strategy_state.strategy6_last_ofi_score = None
        strategy_state.stop_loss_count = 0
        if hasattr(strategy_state, "last_processed_live_event_slug"):
            strategy_state.last_processed_live_event_slug = None

    def reset_strategy_state(
        self,
        *,
        mode: str | None = None,
        strategy: int | str,
        timeframe: str | None = None,
    ) -> dict[str, Any]:
        strategy_filter = _normalize_strategy_filter(strategy)
        if strategy_filter is None:
            raise ValueError("请选择要重置的策略。")
        strategy_id = int(strategy_filter)
        with self._lock:
            cfg = self._cfg
            target_mode = str(mode or cfg.trade_mode or "paper").strip().lower()
            if target_mode not in {"paper", "live"}:
                raise ValueError(f"Invalid reset mode: {mode!r}")

            if target_mode == "live":
                strategy_ids = list(getattr(cfg, "strategy_ids", []) or getattr(cfg, "live_strategy_ids", []) or [cfg.strategy_id])
                state_path = cfg.logs_dir / "live_session_state.json"
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state = load_session_state(state_path, effective_live_strategy_ids=strategy_ids)
                strategy_state = state.live_strategies.setdefault(strategy_id, LiveStrategyState())
                if getattr(strategy_state, "pending_live_slug", None):
                    raise ValueError("该策略还有待结算实盘订单，不能重置。")
                self._reset_strategy_sizing_state(strategy_state)
                state.live_strategies[strategy_id] = strategy_state
                save_session_state(state_path, state)
            else:
                target_timeframe = _normalize_timeframe_filter(timeframe, fallback=cfg.market_timeframe)
                profile = getattr(cfg, "paper_profiles", {}).get(target_timeframe)
                strategy_ids = list(
                    getattr(cfg, "strategy_ids", [])
                    or (getattr(profile, "paper_strategy_ids", None) if profile is not None else None)
                    or getattr(cfg, "paper_strategy_ids", [])
                    or [cfg.strategy_id]
                )
                state_path = _paper_session_state_path(cfg, target_timeframe)
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state = load_session_state(state_path, effective_paper_strategy_ids=strategy_ids)
                strategy_state = state.paper_strategies.get(strategy_id)
                if strategy_state is None:
                    strategy_state = PaperStrategyState()
                    state.paper_strategies[strategy_id] = strategy_state
                if getattr(strategy_state, "pending_paper_trades", None):
                    raise ValueError("该策略还有待结算纸面订单，不能重置。")
                self._reset_strategy_sizing_state(strategy_state)
                state.paper_strategies[strategy_id] = strategy_state
                save_session_state(state_path, state)

        payload = self.get_config_payload()
        payload["reset"] = {"mode": target_mode, "strategy": str(strategy_id)}
        return payload

    def _live_health_strategy_alignment(self, cfg: AppConfig) -> tuple[list[str], list[str], list[str]]:
        paper_ids = [str(item) for item in _paper_strategy_ids_for_runtime(cfg)]
        live_ids = [str(item) for item in _live_strategy_ids_for_runtime(cfg)]
        diffs: list[str] = []
        overlapping_ids = sorted(set(paper_ids) & set(live_ids), key=lambda item: int(item))
        if overlapping_ids:
            diffs.append(f"strategies in both paper and live: {','.join(overlapping_ids)}")
        return paper_ids, live_ids, diffs

    def _live_health_market_constraints(
        self,
        *,
        client: PolymarketClient | Any,
        live_client: Any | None,
        now: datetime,
    ) -> tuple[dict[str, Any], str | None]:
        current_round, next_round = client.find_current_and_next_rounds(now=now)
        display_round = _select_display_round(current_round=current_round, next_round=next_round)
        if display_round is None:
            return {}, "当前没有可检查的 Polymarket 轮次。"

        market = client.get_market_by_slug(display_round.slug)
        if not isinstance(market, dict):
            return {"round_slug": display_round.slug}, "当前轮次市场返回值不可识别。"

        clob_info: dict[str, Any] = {}
        condition_id = str(
            _market_first_value(
                market,
                ("conditionId", "condition_id", "conditionID"),
            )
            or ""
        ).strip()
        if condition_id:
            get_clob_market = getattr(client, "get_clob_market_by_condition_id", None)
            if callable(get_clob_market):
                try:
                    maybe_clob_info = get_clob_market(condition_id)
                    if isinstance(maybe_clob_info, dict):
                        clob_info = maybe_clob_info
                except Exception:
                    clob_info = {}

        token_ids = extract_token_ids(market.get("clobTokenIds"), market.get("outcomes"))
        sample_token_id = str(token_ids.get("UP") or token_ids.get("DOWN") or "").strip()
        tick_size = (
            _market_first_value(clob_info, ("minimum_tick_size", "minimumTickSize", "tick_size", "tickSize", "mts"))
            or _market_first_value(market, ("minimum_tick_size", "minimumTickSize", "tick_size", "tickSize", "mts"))
        )
        neg_risk = _optional_bool(
            _market_first_value(clob_info, ("negRisk", "neg_risk", "nr"))
            or _market_first_value(market, ("negRisk", "neg_risk", "nr"))
        )
        if live_client is not None and sample_token_id:
            if tick_size in (None, ""):
                get_tick_size = getattr(live_client, "get_tick_size", None)
                if callable(get_tick_size):
                    try:
                        tick_size = get_tick_size(sample_token_id)
                    except Exception:
                        tick_size = None
            if neg_risk is None:
                get_neg_risk = getattr(live_client, "get_neg_risk", None)
                if callable(get_neg_risk):
                    try:
                        neg_risk = _optional_bool(get_neg_risk(sample_token_id))
                    except Exception:
                        neg_risk = None

        constraints = {
            "round_slug": display_round.slug,
            "round_title": display_round.title,
            "round_start_time": _iso(display_round.start_time),
            "round_end_time": _iso(display_round.end_time),
            "condition_id": condition_id or None,
            "sample_token_id": sample_token_id or None,
            "order_min_size": _optional_float(
                _market_first_value(
                    market,
                    ("orderMinSize", "minimum_order_size", "min_order_size", "minimumOrderSize", "minOrderSize"),
                )
            ),
            "minimum_tick_size": str(tick_size) if tick_size not in (None, "") else None,
            "neg_risk": neg_risk,
            "fees_enabled": _optional_bool(_market_first_value(market, ("feesEnabled", "fees_enabled"))),
            "maker_base_fee": _optional_int(
                _market_first_value(clob_info, ("makerBaseFee", "maker_base_fee", "mbf"))
                or _market_first_value(market, ("makerBaseFee", "maker_base_fee", "mbf"))
            ),
            "taker_base_fee": _optional_int(
                _market_first_value(clob_info, ("takerBaseFee", "taker_base_fee", "tbf"))
                or _market_first_value(market, ("takerBaseFee", "taker_base_fee", "tbf"))
            ),
        }
        if constraints["order_min_size"] is None:
            return constraints, "未读到当前市场最小下单金额。"
        if constraints["minimum_tick_size"] in (None, ""):
            return constraints, "未读到当前市场 tick size。"
        return constraints, None

    def get_live_health_payload(self) -> dict[str, Any]:
        with self._lock:
            validation_values = dict(self._env_values)
            validation_values["TRADE_MODE"] = "both" if str(validation_values.get("TRADE_MODE") or "").lower() == "both" else "live"
            cfg = self._build_config(validation_values)
            client = self._client
        now = datetime.now(timezone.utc)
        checks: list[dict[str, Any]] = []
        constraints: dict[str, Any] = {
            "order_type": str(getattr(cfg, "live_order_type", "") or "FOK").upper(),
            "signature_type": getattr(cfg, "live_signature_type", None),
            "has_funder": bool(getattr(cfg, "live_funder", None)),
        }

        try:
            validate_live_runtime_config(cfg)
            checks.append(
                _health_check_payload(
                    "live_config",
                    "实盘配置",
                    ok=True,
                    detail="实盘配置已通过运行前校验。",
                )
            )
        except Exception as exc:
            checks.append(
                _health_check_payload(
                    "live_config",
                    "实盘配置",
                    ok=False,
                    detail=_localize_runtime_message(str(exc)) or str(exc),
                )
            )

        paper_ids, live_ids, strategy_diffs = self._live_health_strategy_alignment(cfg)
        constraints["paper_strategy_ids"] = paper_ids
        constraints["live_strategy_ids"] = live_ids
        constraints["strategy_profile_diff_count"] = len(strategy_diffs)
        checks.append(
            _health_check_payload(
                "strategy_alignment",
                "纸面/实盘策略一致",
                ok=not strategy_diffs,
                detail=(
                    f"策略组合一致：{','.join(live_ids) or '--'}。"
                    if not strategy_diffs
                    else "存在差异：" + "; ".join(strategy_diffs[:5])
                ),
                value={"paper": paper_ids, "live": live_ids, "profile_diffs": len(strategy_diffs)},
            )
        )

        live_client = None
        try:
            live_client = _create_live_clob_client(cfg)
            checks.append(
                _health_check_payload(
                    "clob_client",
                    "CLOB 客户端",
                    ok=True,
                    detail="CLOB 客户端和 API 凭证可用。",
                )
            )
        except Exception as exc:
            checks.append(
                _health_check_payload(
                    "clob_client",
                    "CLOB 客户端",
                    ok=False,
                    detail=_localize_runtime_message(str(exc)) or str(exc),
                )
            )

        if live_client is not None:
            try:
                available_balance = _read_available_live_balance(cfg=cfg, clob_client=live_client)
                constraints["available_balance"] = available_balance
                checks.append(
                    _health_check_payload(
                        "balance_allowance",
                        "余额/授权",
                        ok=available_balance > 0,
                        detail=f"可用余额/授权约 {available_balance:.6f} USDC。",
                        value=available_balance,
                    )
                )
            except Exception as exc:
                constraints["available_balance"] = None
                checks.append(
                    _health_check_payload(
                        "balance_allowance",
                        "余额/授权",
                        ok=False,
                        detail=str(exc),
                    )
                )
        else:
            constraints["available_balance"] = None
            checks.append(
                _health_check_payload(
                    "balance_allowance",
                    "余额/授权",
                    ok=False,
                    detail="CLOB 客户端不可用，未检查余额/授权。",
                )
            )

        try:
            market_constraints, market_error = self._live_health_market_constraints(
                client=client,
                live_client=live_client,
                now=now,
            )
            constraints.update(market_constraints)
            market_ok = market_error is None
            checks.append(
                _health_check_payload(
                    "market_constraints",
                    "当前市场约束",
                    ok=market_ok,
                    detail=(
                        "已读取当前市场最小下单、tick size 和费用参数。"
                        if market_ok
                        else market_error or "当前市场约束不可用。"
                    ),
                    value={
                        "order_min_size": constraints.get("order_min_size"),
                        "minimum_tick_size": constraints.get("minimum_tick_size"),
                        "fees_enabled": constraints.get("fees_enabled"),
                    },
                )
            )
        except Exception as exc:
            checks.append(
                _health_check_payload(
                    "market_constraints",
                    "当前市场约束",
                    ok=False,
                    detail=str(exc),
                )
            )

        order_type_ok = constraints["order_type"] in {"FOK", "FAK"}
        checks.append(
            _health_check_payload(
                "order_type",
                "订单类型",
                ok=order_type_ok,
                detail=(
                    f"实盘市价单将使用 {constraints['order_type']}。"
                    if order_type_ok
                    else f"当前订单类型 {constraints['order_type']} 不适合实盘市价单。"
                ),
                value=constraints["order_type"],
            )
        )

        ok = all(item["ok"] for item in checks)
        return {
            "ok": ok,
            "checked_at": _iso(now),
            "summary": "实盘只读健康检查通过。" if ok else "实盘健康检查存在需要处理的项目。",
            "checks": checks,
            "constraints": constraints,
        }

    def get_market_payload(self, *, strategy: int | str | None = None, timeframe: str | None = None) -> dict[str, Any]:
        with self._lock:
            cfg = self._cfg
            binance_signal_service = self._binance_signal_service

        now = datetime.now(timezone.utc)
        target_timeframe = _normalize_timeframe_filter(timeframe, fallback=cfg.market_timeframe)
        timeframe_cfg = _cfg_for_paper_timeframe(cfg, target_timeframe)
        strategy_filter = _normalize_strategy_filter(strategy)
        selected_strategy = int(strategy_filter or str(timeframe_cfg.strategy_id))
        effective_cfg = _cfg_for_paper_strategy(timeframe_cfg, selected_strategy)
        effective_paper_strategy_ids = list(getattr(timeframe_cfg, "paper_strategy_ids", []) or [timeframe_cfg.strategy_id])
        if selected_strategy not in effective_paper_strategy_ids:
            effective_paper_strategy_ids.append(selected_strategy)
        state_path = _paper_session_state_path(cfg, target_timeframe)
        if timeframe is None and not state_path.exists():
            state_path = cfg.logs_dir / "session_state.json"
        session_state = load_session_state(
            state_path,
            effective_paper_strategy_ids=effective_paper_strategy_ids,
        )
        strategy_session = session_state
        if getattr(session_state, "paper_strategies", None):
            strategy_session = session_state.paper_strategies.get(selected_strategy) or session_state
        client = self._client if target_timeframe == cfg.market_timeframe else PolymarketClient(effective_cfg)
        current_round, next_round = client.find_current_and_next_rounds(now=now)
        display_round = _select_display_round(current_round=current_round, next_round=next_round)
        target_round = display_round
        ws_runtime = client.get_ws_runtime_stats()
        strategy_view = {
            "selected": str(selected_strategy),
            "paper_strategy_ids": [str(item) for item in effective_paper_strategy_ids],
            "available": [str(item) for item in effective_paper_strategy_ids],
            "timeframe": target_timeframe,
        }

        try:
            if target_round is None:
                return {
                    "ok": True,
                    "timestamp": _iso(now),
                    "round": None,
                    "quote": None,
                    "signal": None,
                    "plan": None,
                    "session_state": asdict(strategy_session),
                    "ws_runtime": ws_runtime,
                    "ws_stale_guard_triggered": False,
                    "message": "\u5f53\u524d\u6ca1\u6709\u53ef\u7528\u7684" + target_timeframe + "\u8f6e\u6b21\u3002",
                    "strategy_view": strategy_view,
                    "strategy6": {
                        "enabled": selected_strategy == 6,
                        "ofi_score": None,
                        "signal_at": None,
                        "stale": True,
                        "threshold": effective_cfg.ofi_threshold,
                        "max_entry_price": effective_cfg.max_entry_price,
                        "bid_price": None,
                        "bid_qty": None,
                        "ask_price": None,
                        "ask_qty": None,
                    },
                    "strategy7": {
                        "enabled": selected_strategy == 7,
                        "ofi_score": None,
                        "momentum_delta": None,
                        "agreement": None,
                        "quality_gate": None,
                        "final_reason": None,
                    },
                }

            market = client.get_market_by_slug(target_round.slug)
            quote = client.quote_from_market(market)
            entry_time = _entry_time_for_round(effective_cfg, target_round)
            _apply_strategy6_signal_to_quote(cfg=effective_cfg, quote=quote, binance_signal_service=binance_signal_service)
            latest_binance_signal = binance_signal_service.latest() if binance_signal_service is not None else None
            strategy6_payload = {
                "enabled": selected_strategy == 6,
                "ofi_score": quote.strategy6_ofi_score,
                "signal_at": _iso(quote.strategy6_signal_at),
                "stale": (
                    quote.strategy6_signal_at is None
                    or (now - quote.strategy6_signal_at).total_seconds() > effective_cfg.binance_signal_stale_seconds
                ),
                "threshold": effective_cfg.ofi_threshold,
                "max_entry_price": effective_cfg.max_entry_price,
                "bid_price": latest_binance_signal.bid_price if latest_binance_signal is not None else None,
                "bid_qty": latest_binance_signal.bid_qty if latest_binance_signal is not None else None,
                "ask_price": latest_binance_signal.ask_price if latest_binance_signal is not None else None,
                "ask_qty": latest_binance_signal.ask_qty if latest_binance_signal is not None else None,
            }

            side_decision = _resolve_side_from_strategy(
                cfg=effective_cfg,
                state=strategy_session,
                slug=target_round.slug,
                quote=quote,
                market_client=client,
                window=target_round,
                now=now,
                entry_time=entry_time,
            )

            side = side_decision.side
            price = resolve_quote_price(side, quote) if side in {"UP", "DOWN"} else None
            ws_stale = _ws_is_stale_for_trade(client, effective_cfg)

            if side in {"UP", "DOWN"} and not ws_stale and not _entry_window_missed(
                now,
                entry_time,
                grace_seconds=effective_cfg.entry_grace_seconds,
            ):
                plan_obj = build_trade_plan(
                    state=strategy_session,
                    side=side,
                    price=price,
                    min_price_threshold=getattr(effective_cfg, 'min_price_threshold', None),
                    max_price_threshold=effective_cfg.max_price_threshold,
                    max_stake=effective_cfg.max_stake,
                    max_consecutive_losses=effective_cfg.max_consecutive_losses,
                    min_stake=getattr(effective_cfg, "min_stake", None),
                    min_entry_price=getattr(effective_cfg, "min_entry_price", getattr(effective_cfg, "min_price_threshold", None)),
                    max_entry_price=getattr(effective_cfg, "max_entry_price", effective_cfg.max_price_threshold),
                    base_order_cost=effective_cfg.base_order_cost,
                    order_cost_multiplier=_effective_decision_order_cost_multiplier(
                        cfg=effective_cfg,
                        decision=side_decision,
                        price=price,
                    ),
                )
                side_decision.order_cost_multiplier = plan_obj.order_cost_multiplier
                plan = {
                    "should_trade": plan_obj.should_trade,
                    "side": plan_obj.side,
                    "price": plan_obj.price,
                    "order_size": plan_obj.order_size,
                    "order_cost": plan_obj.order_cost,
                    "expected_profit": plan_obj.expected_profit,
                    "order_cost_multiplier": plan_obj.order_cost_multiplier,
                    "skip_reason": plan_obj.skip_reason,
                    "stop_loss_triggered": plan_obj.stop_loss_triggered,
                }
            else:
                if ws_stale:
                    reason = "ws_stale"
                elif side in {"UP", "DOWN"} and _entry_window_missed(
                    now,
                    entry_time,
                    grace_seconds=effective_cfg.entry_grace_seconds,
                ):
                    reason = "entry_window_missed"
                else:
                    reason = side_decision.reason or "signal_unavailable"
                plan = {
                    "should_trade": False,
                    "side": side,
                    "price": price,
                    "order_size": 0.0,
                    "order_cost": 0.0,
                    "expected_profit": 0.0,
                    "skip_reason": reason,
                    "stop_loss_triggered": False,
                }

            strategy7_payload = {
                "enabled": selected_strategy in {7, 8, 9},
                "ofi_score": quote.strategy6_ofi_score,
                "momentum_delta": side_decision.signal_delta,
                "agreement": (
                    "agree"
                    if selected_strategy in {7, 8, 9} and side_decision.side in {"UP", "DOWN"} and side_decision.reason != "strategy8_conflict_reversal"
                    else ("conflict" if side_decision.reason in {"strategy7_signal_conflict", "strategy8_conflict_reversal", "strategy9_signal_conflict"} else None)
                ),
                "quality_gate": (
                    "passed"
                    if selected_strategy in {7, 8, 9} and side_decision.side in {"UP", "DOWN"}
                    else (side_decision.reason if selected_strategy in {7, 8, 9} else None)
                ),
                "final_reason": side_decision.reason,
                "dynamic_max_entry_price": side_decision.max_entry_price,
            }

            return {
                "ok": True,
                "timestamp": _iso(now),
                "round": {
                    "slug": target_round.slug,
                    "title": target_round.title,
                    "start_time": _iso(target_round.start_time),
                    "end_time": _iso(target_round.end_time),
                    "entry_time": _iso(entry_time),
                    "is_current": current_round is not None and target_round.slug == current_round.slug,
                    "seconds_to_entry": (entry_time - now).total_seconds(),
                    "seconds_to_end": (target_round.end_time - now).total_seconds(),
                },
                "quote": {
                    "source": quote.source,
                    "accepting_orders": quote.accepting_orders,
                    "up_price": quote.up_price,
                    "up_best_bid": quote.up_best_bid,
                    "up_best_ask": quote.up_best_ask,
                    "down_price": quote.down_price,
                    "down_best_bid": quote.down_best_bid,
                    "down_best_ask": quote.down_best_ask,
                    "fetched_at": _iso(quote.fetched_at),
                },
                "signal": {
                    "side": side_decision.side,
                    "reason": side_decision.reason,
                    "open_up": side_decision.signal_open_up_price,
                    "current_up": side_decision.signal_current_up_price,
                    "threshold": side_decision.signal_threshold,
                    "delta": side_decision.signal_delta,
                    "locked": side_decision.signal_locked,
                },
                "plan": plan,
                "session_state": asdict(strategy_session),
                "ws_runtime": ws_runtime,
                "ws_stale_guard_triggered": ws_stale,
                "strategy6": strategy6_payload,
                "strategy7": strategy7_payload,
                "strategy_view": strategy_view,
            }
        finally:
            if client is not self._client:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
    def get_paper_summary_payload(self, *, strategy: int | str | None = None, timeframe: str | None = None) -> dict[str, Any]:
        with self._lock:
            cfg = self._cfg
            target_timeframe = _normalize_timeframe_filter(timeframe, fallback=cfg.market_timeframe)
            paper_csv = _paper_trades_path(cfg, target_timeframe)
            if timeframe is None and not paper_csv.exists():
                paper_csv = cfg.logs_dir / "paper_trades.csv"
        strategy_filter = _normalize_strategy_filter(strategy)
        try:
            if strategy_filter is None:
                daily = summarize_paper_trades(paper_csv, tz_offset="+08:00")
                strategy_daily = summarize_paper_trades_by_strategy(paper_csv, tz_offset="+08:00")
            else:
                filtered_rows = list(reversed(_filter_trade_rows_by_strategy(_tail_csv_rows(paper_csv, limit=1000000), strategy_filter)))
                if not filtered_rows:
                    daily = []
                    strategy_daily = []
                else:
                    filtered_csv = _write_summary_work_csv(
                        paper_csv,
                        filtered_rows,
                        list(filtered_rows[0].keys()),
                    )
                    try:
                        daily = summarize_paper_trades(filtered_csv, tz_offset="+08:00")
                        strategy_daily = summarize_paper_trades_by_strategy(filtered_csv, tz_offset="+08:00")
                    finally:
                        _cleanup_summary_work_csv(filtered_csv)
        except (FileNotFoundError, ValueError):
            daily = []
            strategy_daily = []
        days = [asdict(item) for item in daily[-14:]]
        summary_dates = {item["date"] for item in days}
        strategy_days = [asdict(item) for item in strategy_daily if item.date in summary_dates]
        return {
            "csv_path": str(paper_csv),
            "mode": "paper",
            "tz_offset": "+08:00",
            "strategy": strategy_filter or "all",
            "timeframe": target_timeframe,
            "days": days,
            "strategy_days": strategy_days,
            "latest": days[-1] if days else None,
        }

    def get_live_summary_payload(self, *, strategy: int | str | None = None) -> dict[str, Any]:
        with self._lock:
            cfg = self._cfg
            live_csv = cfg.logs_dir / "live_orders.csv"
            validation_cache = self._result_validation_cache
        strategy_filter = _normalize_strategy_filter(strategy)
        try:
            live_rows = list(reversed(_tail_csv_rows(live_csv, limit=1000000)))
            filtered_rows = _filter_trade_rows_by_strategy(live_rows, strategy_filter)
            if not filtered_rows:
                daily = []
                strategy_daily = []
            else:
                client = PolymarketClient(cfg)
                try:
                    summary_rows = _backfill_live_summary_rows(
                        filtered_rows,
                        client=client,
                        validation_cache=validation_cache,
                    )
                finally:
                    close = getattr(client, "close", None)
                    if callable(close):
                        close()
                filtered_csv = _write_summary_work_csv(
                    live_csv,
                    summary_rows,
                    _csv_fieldnames_for_rows(summary_rows),
                )
                try:
                    daily = summarize_paper_trades(filtered_csv, tz_offset="+08:00")
                    strategy_daily = summarize_paper_trades_by_strategy(filtered_csv, tz_offset="+08:00")
                finally:
                    _cleanup_summary_work_csv(filtered_csv)
        except (FileNotFoundError, ValueError):
            daily = []
            strategy_daily = []
        days = [asdict(item) for item in daily[-14:]]
        summary_dates = {item["date"] for item in days}
        strategy_days = [asdict(item) for item in strategy_daily if item.date in summary_dates]
        return {
            "csv_path": str(live_csv),
            "mode": "live",
            "tz_offset": "+08:00",
            "strategy": strategy_filter or "all",
            "days": days,
            "strategy_days": strategy_days,
            "latest": days[-1] if days else None,
        }

    def get_recent_trades_payload(self, *, limit: int, strategy: int | str | None = None, timeframe: str | None = None) -> dict[str, Any]:
        with self._lock:
            cfg = self._cfg
            validation_cache = self._result_validation_cache
            target_timeframe = _normalize_timeframe_filter(timeframe, fallback=cfg.market_timeframe)
            paper_csv = _paper_trades_path(cfg, target_timeframe)
            state_path = _paper_session_state_path(cfg, target_timeframe)
            effective_paper_strategy_ids = list(
                getattr(getattr(cfg, "paper_profiles", {}).get(target_timeframe), "paper_strategy_ids", cfg.paper_strategy_ids)
            )
            if timeframe is None and not paper_csv.exists():
                paper_csv = cfg.logs_dir / "paper_trades.csv"
                state_path = cfg.logs_dir / "session_state.json"
            explicit_paper_strategy_scope = _has_explicit_paper_strategy_scope(self._env_values, target_timeframe)
        capped_limit = max(1, min(300, int(limit)))
        strategy_filter = _normalize_strategy_filter(strategy)
        explicit_all_strategy_filter = _is_explicit_all_strategy_filter(strategy)
        rows = _all_csv_rows_newest_first(paper_csv)
        if strategy_filter is None and explicit_paper_strategy_scope and not explicit_all_strategy_filter:
            rows = _filter_trade_rows_by_strategy_ids(rows, effective_paper_strategy_ids)
        elif strategy_filter is not None:
            rows = _filter_trade_rows_by_strategy(rows, strategy_filter)
        pending_rows: list[dict[str, str]] = []
        session_state = load_session_state(state_path, effective_paper_strategy_ids=effective_paper_strategy_ids)
        pending_items = list(getattr(session_state, "pending_paper_trades", []) or [])
        if getattr(session_state, "paper_strategies", None):
            pending_items = []
            for strategy_state in session_state.paper_strategies.values():
                pending_items.extend(getattr(strategy_state, "pending_paper_trades", []) or [])
        if strategy_filter is None and explicit_paper_strategy_scope and not explicit_all_strategy_filter:
            filtered_pending_items = _filter_pending_paper_trades_by_strategy_ids(pending_items, effective_paper_strategy_ids)
        elif strategy_filter is not None:
            filtered_pending_items = _filter_pending_paper_trades_by_strategy(pending_items, strategy_filter)
        else:
            filtered_pending_items = list(pending_items)
        for item in filtered_pending_items:
            pending_rows.append(_pending_paper_trade_to_recent_row(item))
        merged_rows = pending_rows + rows
        _sort_recent_rows_by_round(merged_rows, target_timeframe)
        merged_rows = merged_rows[:capped_limit]

        client = PolymarketClient(cfg)
        try:
            validated_rows = [
                _validate_recent_trade_row(row, client=client, validation_cache=validation_cache)
                for row in merged_rows
            ]
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        validated_rows = [
            _with_recent_round_display_time(row, target_timeframe)
            for row in validated_rows
        ]

        return {
            "csv_path": str(paper_csv),
            "strategy": strategy_filter or "all",
            "timeframe": target_timeframe,
            "count": len(validated_rows),
            "rows": validated_rows,
        }

    def get_live_recent_orders_payload(self, *, limit: int, strategy: int | str | None = None) -> dict[str, Any]:
        with self._lock:
            cfg = self._cfg
            live_csv = self._cfg.logs_dir / "live_orders.csv"
            target_timeframe = self._cfg.market_timeframe
            validation_cache = self._result_validation_cache
            effective_live_strategy_ids = list(getattr(cfg, "strategy_ids", []) or getattr(cfg, "live_strategy_ids", []) or [cfg.strategy_id])
            explicit_live_strategy_scope = _has_explicit_live_strategy_scope(self._env_values)
        capped_limit = max(1, min(300, int(limit)))
        strategy_filter = _normalize_strategy_filter(strategy)
        rows = _all_csv_rows_newest_first(live_csv)
        if strategy_filter is None and explicit_live_strategy_scope:
            rows = _filter_trade_rows_by_strategy_ids(rows, effective_live_strategy_ids)
        elif strategy_filter is not None:
            rows = _filter_trade_rows_by_strategy(rows, strategy_filter)
        rows = _collapse_live_recent_rows(rows, target_timeframe)
        rows = rows[:capped_limit]
        client = PolymarketClient(cfg)
        try:
            rows = [
                _refresh_live_row_trade_pnl_from_result(
                    _validate_recent_trade_row(
                        row,
                        client=client,
                        validation_cache=validation_cache,
                        fill_missing_result=True,
                    )
                )
                for row in rows
            ]
            has_recent_mismatch = any(row.get("result_check_status") == "mismatch" for row in rows)
            has_unofficial_live_result = any(
                _is_live_btc_numeric_slug(str(row.get("event_slug") or "").strip())
                and _live_result_value(row) in {"UP", "DOWN"}
                and row.get("result_check_status") == "official_pending"
                for row in rows
            )
            has_provisional_loss = _live_csv_has_provisional_loss(live_csv)
            if has_recent_mismatch or has_unofficial_live_result or has_provisional_loss:
                corrected_count = _auto_reconcile_live_ledger(
                    live_csv=live_csv,
                    state_path=cfg.logs_dir / "live_session_state.json",
                    client=client,
                    validation_cache=validation_cache,
                    active_strategy_id=cfg.strategy_id,
                    cfg=cfg,
                    provisional_only=not (has_recent_mismatch or has_unofficial_live_result),
                )
                if corrected_count > 0:
                    rows = _all_csv_rows_newest_first(live_csv)
                    if strategy_filter is None and explicit_live_strategy_scope:
                        rows = _filter_trade_rows_by_strategy_ids(rows, effective_live_strategy_ids)
                    elif strategy_filter is not None:
                        rows = _filter_trade_rows_by_strategy(rows, strategy_filter)
                    rows = _collapse_live_recent_rows(rows, target_timeframe)
                    rows = rows[:capped_limit]
                    rows = [
                        _refresh_live_row_trade_pnl_from_result(
                            _validate_recent_trade_row(
                                row,
                                client=client,
                                validation_cache=validation_cache,
                                fill_missing_result=True,
                            )
                        )
                        for row in rows
                    ]
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        rows = _backfill_recent_strategy_price_skip_prices(rows)
        rows = _with_live_recent_price_checks(rows, cfg)
        rows = [_with_recent_round_display_time(row, target_timeframe) for row in rows]
        return {
            "csv_path": str(live_csv),
            "strategy": strategy_filter or "all",
            "timeframe": target_timeframe,
            "count": len(rows),
            "rows": rows,
        }


class _DashboardRequestHandler(BaseHTTPRequestHandler):
    dashboard_state: DashboardState

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    @staticmethod
    def _is_client_disconnect(exc: Exception) -> bool:
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            return True
        winerr = getattr(exc, "winerror", None)
        if winerr in {10053, 10054}:
            return True
        err_no = getattr(exc, "errno", None)
        if err_no in {errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED}:
            return True
        return False

    def _safe_send_bytes(self, raw: bytes, *, content_type: str, status: HTTPStatus) -> bool:
        try:
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return True
        except OSError as exc:
            if self._is_client_disconnect(exc):
                return False
            raise

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self._safe_send_bytes(raw, content_type="application/json; charset=utf-8", status=status)

    def _send_html(self, html: str) -> None:
        raw = html.encode("utf-8")
        self._safe_send_bytes(raw, content_type="text/html; charset=utf-8", status=HTTPStatus.OK)

    def _send_text(self, text: str, *, content_type: str) -> None:
        raw = text.encode("utf-8")
        self._safe_send_bytes(raw, content_type=content_type, status=HTTPStatus.OK)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be object")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/index.html"}:
                self._send_html(_dashboard_html())
                return
            if parsed.path == "/dashboard.css":
                self._send_text(_dashboard_css(), content_type="text/css; charset=utf-8")
                return
            if parsed.path == "/dashboard.js":
                self._send_text(_dashboard_js(), content_type="application/javascript; charset=utf-8")
                return
            if parsed.path == "/api/config":
                self._send_json(self.dashboard_state.get_config_payload())
                return
            if parsed.path == "/api/market":
                query = parse_qs(parsed.query)
                strategy = (query.get("strategy") or [None])[0]
                timeframe = (query.get("timeframe") or [None])[0]
                self._send_json(self.dashboard_state.get_market_payload(strategy=strategy, timeframe=timeframe))
                return
            if parsed.path == "/api/paper/summary":
                query = parse_qs(parsed.query)
                strategy = (query.get("strategy") or [None])[0]
                timeframe = (query.get("timeframe") or [None])[0]
                self._send_json(self.dashboard_state.get_paper_summary_payload(strategy=strategy, timeframe=timeframe))
                return
            if parsed.path == "/api/live/summary":
                query = parse_qs(parsed.query)
                strategy = (query.get("strategy") or [None])[0]
                self._send_json(self.dashboard_state.get_live_summary_payload(strategy=strategy))
                return
            if parsed.path == "/api/live/health":
                self._send_json(self.dashboard_state.get_live_health_payload())
                return
            if parsed.path == "/api/paper/recent":
                query = parse_qs(parsed.query)
                limit = int((query.get("limit") or ["20"])[0])
                strategy = (query.get("strategy") or [None])[0]
                timeframe = (query.get("timeframe") or [None])[0]
                self._send_json(self.dashboard_state.get_recent_trades_payload(limit=limit, strategy=strategy, timeframe=timeframe))
                return
            if parsed.path == "/api/live/recent":
                query = parse_qs(parsed.query)
                limit = int((query.get("limit") or ["20"])[0])
                strategy = (query.get("strategy") or [None])[0]
                self._send_json(self.dashboard_state.get_live_recent_orders_payload(limit=limit, strategy=strategy))
                return
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
        except OSError as exc:
            if self._is_client_disconnect(exc):
                return
            raise
        except Exception as exc:  # pragma: no cover
            try:
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            except OSError as send_exc:
                if self._is_client_disconnect(send_exc):
                    return
                raise

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/strategy/reset":
            try:
                payload = self._read_json_body()
                strategy = payload.get("strategy_id", payload.get("strategy"))
                updated = self.dashboard_state.reset_strategy_state(
                    mode=payload.get("mode"),
                    strategy=strategy,
                    timeframe=payload.get("timeframe"),
                )
                self._send_json(updated)
            except OSError as exc:
                if self._is_client_disconnect(exc):
                    return
                raise
            except ValueError as exc:
                try:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                except OSError as send_exc:
                    if self._is_client_disconnect(send_exc):
                        return
                    raise
            except Exception as exc:  # pragma: no cover
                try:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                except OSError as send_exc:
                    if self._is_client_disconnect(send_exc):
                        return
                    raise
            return
        if parsed.path != "/api/config":
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json_body()
            env_values = payload.get("env_values", payload.get("values", payload))
            if not isinstance(env_values, dict):
                raise ValueError("env_values must be object")
            updated = self.dashboard_state.update_config({str(k): str(v) for k, v in env_values.items()})
            self._send_json(updated)
        except OSError as exc:
            if self._is_client_disconnect(exc):
                return
            raise
        except ConfigValidationError as exc:
            try:
                self._send_json(
                    {"error": str(exc), "field_errors": exc.field_errors},
                    status=HTTPStatus.BAD_REQUEST,
                )
            except OSError as send_exc:
                if self._is_client_disconnect(send_exc):
                    return
                raise
        except ValueError as exc:
            try:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except OSError as send_exc:
                if self._is_client_disconnect(send_exc):
                    return
                raise
        except Exception as exc:  # pragma: no cover
            try:
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            except OSError as send_exc:
                if self._is_client_disconnect(send_exc):
                    return
                raise


@dataclass
class DashboardRuntime:
    server: ThreadingHTTPServer
    state: DashboardState
    _serve_started: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _shutdown_requested: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _shutdown_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def serve_forever(self) -> None:
        self._serve_started.set()
        try:
            if self._shutdown_requested.is_set():
                return
            self.server.serve_forever()
        finally:
            self._serve_started.clear()

    def shutdown(self) -> None:
        if self._shutdown_requested.is_set():
            return
        with self._shutdown_lock:
            if self._shutdown_requested.is_set():
                return
            self._shutdown_requested.set()
        if self._serve_started.is_set():
            self._shutdown_server()

    def _shutdown_server(self) -> None:
        try:
            self.server.shutdown()
        except OSError:
            pass

    def close(self) -> None:
        try:
            self.shutdown()
        finally:
            try:
                self.server.server_close()
            finally:
                self.state.close()


def _install_dashboard_min_price_threshold() -> None:
    DashboardState.CONFIG_LABELS['MIN_PRICE_THRESHOLD'] = '最低买入价格阈值'
    DashboardState.CONFIG_ATTR_MAP['MIN_PRICE_THRESHOLD'] = 'min_price_threshold'
    DashboardState.FIELD_HELP['MIN_PRICE_THRESHOLD'] = '目标方向价格低于此阈值就不入场，和最高价格阈值一起构成允许入场的价格区间。'

    if 'MIN_PRICE_THRESHOLD' not in DashboardState.FLOAT_CONFIG_KEYS:
        float_keys = list(DashboardState.FLOAT_CONFIG_KEYS)
        insert_at = float_keys.index('MAX_PRICE_THRESHOLD') if 'MAX_PRICE_THRESHOLD' in float_keys else len(float_keys)
        float_keys.insert(insert_at, 'MIN_PRICE_THRESHOLD')
        DashboardState.FLOAT_CONFIG_KEYS = tuple(float_keys)


_install_dashboard_min_price_threshold()


def _install_dashboard_paper_profile_fields() -> None:
    if 'PAPER_TIMEFRAMES' not in DashboardState.CONFIG_LABELS:
        DashboardState.CONFIG_LABELS['PAPER_TIMEFRAMES'] = '纸面时间频次'
        DashboardState.SELECT_OPTIONS['PAPER_TIMEFRAMES'] = list(SUPPORTED_PAPER_TIMEFRAMES)
        DashboardState.FIELD_HELP['PAPER_TIMEFRAMES'] = '纸面模式下要同时运行的时间频次列表，例如 5m,15m。'
    for timeframe in SUPPORTED_PAPER_TIMEFRAMES:
        for field_name in PAPER_PROFILE_EDITABLE_FIELDS:
            scoped_key = _paper_profile_config_key(timeframe, field_name)
            if scoped_key not in DashboardState.CONFIG_LABELS:
                label_key = 'PAPER_STRATEGY_IDS' if field_name == 'STRATEGY_IDS' else field_name
                base_label = DashboardState.CONFIG_LABELS[label_key]
                DashboardState.CONFIG_LABELS[scoped_key] = f'{_paper_profile_display_prefix(timeframe)} · {base_label}'
            select_key = 'STRATEGY_ID' if field_name == 'STRATEGY_IDS' else field_name
            if select_key in DashboardState.SELECT_OPTIONS and scoped_key not in DashboardState.SELECT_OPTIONS:
                DashboardState.SELECT_OPTIONS[scoped_key] = list(DashboardState.SELECT_OPTIONS[select_key])
            if scoped_key not in DashboardState.FIELD_HELP:
                DashboardState.FIELD_HELP[scoped_key] = f'仅作用于 {str(timeframe).lower()} 纸面配置。'


_install_dashboard_paper_profile_fields()


def create_dashboard_runtime(
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    env_file: Path = Path(".env.dashboard"),
    running_trade_mode: str = "paper",
    runtime_control: RuntimeControl | None = None,
    notify_mode_change: Any | None = None,
    notify_runtime_reload: Any | None = None,
) -> DashboardRuntime:
    env_path = Path(env_file)
    state = DashboardState(
        env_file=env_path,
        running_trade_mode=running_trade_mode,
        runtime_control=runtime_control,
        notify_mode_change=notify_mode_change,
        notify_runtime_reload=notify_runtime_reload,
    )

    class Handler(_DashboardRequestHandler):
        dashboard_state = state

    server = ThreadingHTTPServer((host, port), Handler)
    return DashboardRuntime(server=server, state=state)


def run_dashboard(
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    env_file: Path = Path(".env.dashboard"),
) -> None:
    env_path = Path(env_file)
    runtime = create_dashboard_runtime(host=host, port=port, env_file=env_path)
    print(f"Dashboard running at http://{host}:{port}")
    print(f"Config file: {env_path}")
    try:
        runtime.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        runtime.close()


def _dashboard_html() -> str:
    return """<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>BTC 预测控制台</title>
  <link rel=\"icon\" type=\"image/svg+xml\" href='data:image/svg+xml;utf8,<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 64 64\"><rect width=\"64\" height=\"64\" rx=\"14\" fill=\"%230b1220\"/><text x=\"50%\" y=\"54%\" text-anchor=\"middle\" dominant-baseline=\"middle\" font-family=\"Arial,sans-serif\" font-size=\"24\" font-weight=\"700\" fill=\"%23f59e0b\">BTC</text></svg>'>
  <link rel=\"stylesheet\" href=\"/dashboard.css\">
</head>
<body>
  <!-- 计划入场 -->
  <header class=\"topbar\">
    <div class=\"brand-wrap\">
      <div id=\"brandTitle\" class=\"brand\">QUANT_CMD · BTC_5M</div>
      <div class=\"subtitle\">策略参数、实时盘口、信号决策、纸面收益一屏联动</div>
    </div>
    <div class=\"top-actions\">
      <div id=\"topConnectionStatus\" class=\"top-connection-status\" title=\"实时连接状态\">
        <span class=\"top-connection-label\">连接</span>
        <div id=\"wsHealth\" class=\"chip\">待刷新</div>
        <span id=\"topConnectionDetail\" class=\"top-connection-detail\">--</span>
      </div>
      <div id=\"clockLocal\" class=\"clock\">本地时间 --</div>
      <button id=\"btnHelp\" class=\"btn btn-ghost\" type=\"button\">帮助</button>
      <button id=\"btnRefreshNow\" class=\"btn btn-ghost\" type=\"button\">立即刷新</button>
    </div>
  </header>

  <main class=\"layout\">
    <section class=\"panel left-stack config-stack\">
      <div class=\"panel-head\">
        <div>
          <div class=\"head-title\">参数引擎</div>
          <div class=\"head-desc\">参数可编辑并写回 .env</div>
        </div>
        <div id=\"cfgStatus\" class=\"chip\">未保存</div>
      </div>
      <div class=\"panel-body\">
        <section id=\"configWarningBanner\" class=\"config-warning-banner\" hidden>
          <div class=\"config-warning-head\">
            <div>
              <div id=\"configWarningTitle\" class=\"config-warning-title\">配置警告</div>
              <div id=\"configWarningSummary\" class=\"config-warning-summary\">--</div>
            </div>
          </div>
          <div id=\"configWarningList\" class=\"config-warning-list\"></div>
        </section>

        <section class="strategy-guide-card">
          <div class="strategy-guide-head">
            <div>
              <div class="strategy-guide-title">模式任务流</div>
              <div id="configContextSummary" class="strategy-guide-subtitle">当前按模拟盘配置展示。</div>
            </div>
            <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
              <select id="configModeSelect" class="input-compact" aria-label="运行模式选择" title="运行模式">
                <option value="paper">纸面</option>
                <option value="live">实盘</option>
                <option value="both">纸面+实盘</option>
              </select>
              <select id="cfg_MARKET_TIMEFRAME" class="input-compact" aria-label="市场频次" title="市场频次">
                <option value="5m">频次 5m</option>
                <option value="15m">频次 15m</option>
              </select>
              <select id="cfg_ENABLE_LIVE_TRADING" class="input-compact" aria-label="并行实盘" title="并行实盘开关">
                <option value="false">仅纸面</option>
                <option value="true">纸面+实盘</option>
              </select>
            </div>
          </div>
          <div id="paperTaskflowRoot" class="rows">
            <div class="field-help">模拟盘任务流区域预留中，当前先显示模式摘要与可见性壳层。</div>
          </div>
          <div id="liveTaskflowRoot" class="rows" hidden>
            <div class="field-help">实盘任务流区域预留中，当前先显示模式摘要与可见性壳层。</div>
          </div>
        </section>

        <div id=\"strategyGuideCard\" class=\"strategy-guide-card\"></div>
        <div id="paperProfilesRoot" class="rows"></div>

        <div class=\"strategy-guide-card fold-summary\">
          <div class=\"strategy-guide-head\">
            <div>
              <div class=\"strategy-guide-title\">高级参数</div>
              <div class=\"strategy-guide-subtitle\">策略5/7、WS 和实盘相关参数默认折叠。</div>
            </div>
            <button id=\"advancedConfigToggle\" class=\"btn btn-ghost\" type=\"button\" aria-expanded=\"false\" aria-controls=\"advancedConfigPanel\">展开高级参数</button>
          </div>
        </div>

        <form id=\"configForm\" class=\"form-grid\"></form>
        <div id=\"advancedConfigPanel\" hidden></div>

        <div class=\"actions\">
          <button id=\"btnSaveConfig\" class=\"btn btn-primary\" type=\"button\">保存参数</button>
        </div>
      </div>
    </section>

    <section class=\"panel center-stack decision-stack\">
      <div class=\"panel-head\">
        <div>
          <div class=\"head-title\">行情与信号</div>
          <div id=\"marketPanelDesc\" class=\"head-desc\">5分钟轮次行情 / 方向信号 / 下注计划</div>
        </div>
        <div id=\"marketHealth\" class=\"chip\">待刷新</div>
      </div>
      <div class=\"panel-body market-grid\">
        <div class=\"market-header\">
          <div>
            <div id=\"marketDeadline\" class=\"deadline\">--</div>
            <div id=\"marketTitle\" class=\"title\">--</div>
            <div id=\"marketSlug\" class=\"slug\">--</div>
          </div>
          <div class=\"timer-wrap\">
            <div id=\"entryCountdownLabel\" class=\"timer-label\">距离计划入场</div>
            <div id=\"entryCountdown\" class=\"timer-val\">--:--</div>
            <div id=\"entrySyncAt\" class=\"timer-label\">同步于 --</div>
          </div>
        </div>

        <div class=\"split\">
          <div class=\"box\">
            <div class=\"box-title\">盘口价格</div>
            <div class=\"kv-grid\">
              <div class=\"kv\"><div class=\"k\">看涨买价</div><div id=\"upPrice\" class=\"v cyan\">--</div></div>
              <div class=\"kv\"><div class=\"k\">看跌买价</div><div id=\"downPrice\" class=\"v cyan\">--</div></div>
              <div class=\"kv\"><div class=\"k\">看涨最优卖价</div><div id=\"upAsk\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">看跌最优卖价</div><div id=\"downAsk\" class=\"v\">--</div></div>
            </div>
            <div class=\"row\">
              <span class=\"label\">行情来源</span>
              <span id=\"quoteSource\" class=\"value\">--</span>
            </div>
            <div class=\"row\">
              <span class=\"label\">允许下单</span>
              <span id=\"quoteAccepting\" class=\"value\">--</span>
            </div>
            <div class=\"row\">
              <span class=\"label\">行情时间</span>
              <span id=\"quoteFetchedAt\" class=\"value\">--</span>
            </div>
          </div>

          <div id=\"decisionCard\" class=\"box\">
            <div class=\"box-title\">最终决策</div>
            <div class=\"rows\">
              <div class=\"row\"><span class=\"label\">是否下单</span><span id=\"planShouldTrade\" class=\"value\">--</span></div>
              <div class=\"row\"><span class=\"label\">方向</span><span id=\"planSide\" class=\"value\">--</span></div>
              <div class=\"row\"><span class=\"label\">买入价格</span><span id=\"planPrice\" class=\"value\">--</span></div>
              <div class=\"row\"><span class=\"label\">下单金额</span><span id=\"planOrderCost\" class=\"value\">--</span></div>
              <div class=\"row\"><span class=\"label\">下单份额</span><span id=\"planOrderSize\" class=\"value\">--</span></div>
              <div class=\"row\"><span class=\"label\">预期收益</span><span id=\"planExpectedProfit\" class=\"value\">--</span></div>
              <div class=\"row\"><span class=\"label\">跳过原因</span><span id=\"planSkipReason\" class=\"value\">--</span></div>
              <div class=\"row\"><span class=\"label\">触发止损重置</span><span id=\"planStopLoss\" class=\"value\">--</span></div>
            </div>
            <div class=\"actions\">
              <button id=\"signalDetailsToggle\" class=\"btn btn-ghost\" type=\"button\" aria-expanded=\"false\" aria-controls=\"signalDetailsPanel\">展开信号详情</button>
            </div>
            <div id=\"signalDetailsPanel\" hidden>
              <div class=\"kv-grid\">
                <div class=\"kv\"><div class=\"k\">开盘看涨价</div><div id=\"signalOpenUp\" class=\"v\">--</div></div>
                <div class=\"kv\"><div class=\"k\">当前看涨价</div><div id=\"signalCurrentUp\" class=\"v\">--</div></div>
                <div class=\"kv\"><div class=\"k\">信号阈值</div><div id=\"signalThreshold\" class=\"v\">--</div></div>
                <div class=\"kv\"><div class=\"k\">信号偏移</div><div id=\"signalDelta\" class=\"v\">--</div></div>
              </div>
              <div class=\"row\">
                <span class=\"label\">原始方向</span>
                <span id=\"signalSide\" class=\"value\">--</span>
              </div>
              <div class=\"row\">
                <span class=\"label\">原始原因</span>
                <span id=\"signalReason\" class=\"value\">--</span>
              </div>
              <div class=\"row\">
                <span class=\"label\">已锁边</span>
                <span id=\"signalLocked\" class=\"value\">--</span>
              </div>
            </div>
          </div>
        </div>

        <div class=\"split\">
          <div class=\"box\">
            <div class=\"box-title\">会话状态</div>
            <div id=\"paperSerialHint\" class=\"serial-hint\">当前没有待结算轮次</div>
            <div class=\"kv-grid\">
              <div class=\"kv\"><div class=\"k\">轮次计数</div><div id=\"ssRoundIndex\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">累计盈亏</div><div id=\"ssCashPnl\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">待回补亏损</div><div id=\"ssRecoveryLoss\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">连续亏损轮数</div><div id=\"ssConsecutiveLosses\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">止损重置次数</div><div id=\"ssStopLossCount\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">当日已实现盈亏</div><div id=\"ssDailyPnl\" class=\"v\">--</div></div>
            </div>
            <div class=\"row\">
              <span class=\"label\">WS 交易陈旧保护</span>
              <span id=\"wsGuard\" class=\"value\">--</span>
            </div>
          </div>

          </div>
        </div>

        <div id="decisionDiagnosticsHost" class="strategy-guide-card fold-summary">
          <div class="strategy-guide-head">
            <div>
              <div class="strategy-guide-title">诊断区</div>
              <div class="strategy-guide-subtitle">策略 6/7 解释型信号与其他辅助诊断默认折叠。</div>
            </div>
            <button id="diagnosticsToggle" class="btn btn-ghost" type="button" aria-expanded="false" aria-controls="diagnosticsPanel">展开诊断区</button>
          </div>
        </div>

        <div id="diagnosticsPanel" hidden>
          <div id="liveHealthPanel" class="box">
            <div class="box-title">实盘健康检查</div>
            <div class="actions">
              <button id="btnLiveHealthCheck" class="btn btn-ghost" type="button">只读检查</button>
              <div id="liveHealthStatus" class="chip">未检查</div>
            </div>
            <div class="kv-grid">
              <div class="kv"><div class="k">可用余额</div><div id="liveHealthBalance" class="v">--</div></div>
              <div class="kv"><div class="k">订单类型</div><div id="liveHealthOrderType" class="v">--</div></div>
              <div class="kv"><div class="k">最小下单</div><div id="liveHealthMinOrder" class="v">--</div></div>
              <div class="kv"><div class="k">Tick size</div><div id="liveHealthTickSize" class="v">--</div></div>
              <div class="kv"><div class="k">手续费</div><div id="liveHealthFees" class="v">--</div></div>
              <div class="kv"><div class="k">策略组合</div><div id="liveHealthStrategies" class="v">--</div></div>
            </div>
            <div id="liveHealthList" class="runtime-list"></div>
          </div>

          <div id=strategy6Panel class=box>
            <div class=box-title>策略 6 OFI</div>
            <div class=row>
              <span class=label>OFI 分数</span>
              <span id=strategy6OfiScore class=value>--</span>
            </div>
            <div class=row>
              <span class=label>信号时间</span>
              <span id=strategy6SignalAt class=value>--</span>
            </div>
            <div class=row>
              <span class=label>是否陈旧</span>
              <span id=strategy6Stale class=value>--</span>
            </div>
            <div class=kv-grid>
              <div class=kv><div class=k>买一价</div><div id=strategy6BidPrice class=v>--</div></div>
              <div class=kv><div class=k>买一量</div><div id=strategy6BidQty class=v>--</div></div>
              <div class=kv><div class=k>卖一价</div><div id=strategy6AskPrice class=v>--</div></div>
              <div class=kv><div class=k>卖一量</div><div id=strategy6AskQty class=v>--</div></div>
            </div>
          </div>

          <div id=strategy7Panel class=box>
            <div class=box-title>策略 7 共识诊断</div>
            <div class=row>
              <span class=label>OFI 分数</span>
              <span id=strategy7OfiScore class=value>--</span>
            </div>
            <div class=row>
              <span class=label>动量偏移</span>
              <span id=strategy7MomentumDelta class=value>--</span>
            </div>
            <div class=row>
              <span class=label>是否同向</span>
              <span id=strategy7Agreement class=value>--</span>
            </div>
            <div class=row>
              <span class=label>质量过滤</span>
              <span id=strategy7QualityGate class=value>--</span>
            </div>
            <div class=row>
              <span class=label>最终原因</span>
              <span id=strategy7FinalReason class=value>--</span>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section class="panel unified-report-card">
      <div class="report-card-head">
        <div>
          <div class="head-title">交易报告</div>
          <div id="reportCardDesc" class="head-desc">策略筛选同时作用于纸面交易汇总与最近交易明细</div>
        </div>
        <div class="top-actions report-card-actions">
          <select id="reportModeSelect" class="btn btn-ghost">
            <option value="paper">纸面</option>
            <option value="live">实盘</option>
          </select>
          <select id="paperReportStrategy" class="btn btn-ghost"></select>
          <div class="report-status-group">
            <div id="paperStatus" class="chip">待刷新</div>
            <div id="recentStatus" class="chip">待刷新</div>
          </div>
        </div>
      </div>
      <div class="report-card-body">
        <section id="reportSummarySection" class="report-section">
          <div id="reportSummaryTitle" class="section-title">纸面交易汇总</div>
          <div class=\"kv-grid\" style=\"margin-bottom: 10px;\">
            <div class=\"kv\"><div class=\"k\">日期</div><div id=\"sumDate\" class=\"v\">--</div></div>
            <div class=\"kv\"><div class=\"k\">交易笔数</div><div id=\"sumTrades\" class=\"v\">--</div></div>
            <div class=\"kv\"><div class=\"k\">命中率</div><div id=\"sumHitRate\" class=\"v\">--</div></div>
            <div class=\"kv\"><div class=\"k\">总盈亏</div><div id=\"sumTotalPnl\" class=\"v\">--</div></div>
            <div class=\"kv\"><div class=\"k\">最大回撤</div><div id=\"sumDrawdown\" class=\"v\">--</div></div>
            <div class=\"kv\"><div class=\"k\">强信号占比</div><div id=\"sumStrongRate\" class=\"v\">--</div></div>
          </div>

          <div class=\"days-table-wrap\">
            <table>
              <thead>
                <tr>
                  <th>日期</th>
                  <th>交易</th>
                  <th>命中率</th>
                  <th>总盈亏</th>
                  <th>回撤</th>
                </tr>
              </thead>
              <tbody id=\"daysTbody\"></tbody>
            </table>
          </div>

          <div class="section-title">每日策略表现</div>
          <div class=\"strategy-days-table table-wrap\">
            <table>
              <thead>
                <tr>
                  <th>日期</th>
                  <th>策略</th>
                  <th>下单</th>
                  <th>跳过</th>
                  <th>胜率</th>
                  <th>单日盈亏</th>
                  <th>累计盈亏</th>
                </tr>
              </thead>
              <tbody id=\"strategyDaysTbody\"></tbody>
            </table>
          </div>
        </section>

        <section id="reportRecentSection" class="report-section">
          <div class="section-title">最近交易明细</div>
          <div id="recentPanelDesc" class="section-desc">按轮次倒序显示最近 80 条记录 · 当前策略：全部</div>
          <div class=\"report-recent-table table-wrap\">
            <table>
              <thead>
                <tr>
                  <th>记录时间</th>
                  <th>轮次</th>
                  <th>策略</th>
                  <th>方向</th>
                  <th>价格</th>
                  <th>下注金额</th>
                  <th>结果</th>
                  <th>校验</th>
                  <th>开盘价</th>
                  <th>收盘价</th>
                  <th>单笔盈亏</th>
                  <th>累计盈亏</th>
                  <th>跳过原因</th>
                  <th>信号偏移</th>
                </tr>
              </thead>
              <tbody id=\"recentTbody\"></tbody>
            </table>
          </div>
        </section>
      </div>
    </section>
  </main>
  <div id=\"helpBackdrop\" class=\"help-backdrop\"></div>
  <aside id=\"helpDrawer\" class=\"help-drawer\" aria-hidden=\"true\" tabindex=\"-1\">
    <div class=\"help-head\">
      <div>
        <div class=\"help-title\">帮助中心</div>
        <div class=\"help-subtitle\">快速上手与元素说明</div>
      </div>
      <button id=\"btnHelpClose\" class=\"btn btn-ghost\" type=\"button\">关闭</button>
    </div>
    <div id=\"helpTabs\" class=\"help-tabs\"></div>
    <div id=\"helpBody\" class=\"help-body\"></div>
    <div id=\"helpFooter\" class=\"help-footer\"></div>
  </aside>
  <script src=\"/dashboard.js\"></script>
</body>
</html>
"""


def _dashboard_css() -> str:
    return """
:root {
  --bg0: #050a16;
  --bg1: #0d1628;
  --bg2: #111f35;
  --line: #234061;
  --text: #dce9ff;
  --muted: #90a8ce;
  --cyan: #3cd7ff;
  --green: #5aeaa5;
  --red: #ff8498;
  --amber: #ffd67a;
  --panel-shadow: 0 16px 36px rgba(0, 0, 0, 0.34);
  --mono: Consolas, "Courier New", monospace;
  --sans: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  color: var(--text);
  font-family: var(--sans);
  background:
    radial-gradient(circle at 10% 0%, rgba(60, 215, 255, 0.12), transparent 28%),
    radial-gradient(circle at 95% 0%, rgba(90, 234, 165, 0.08), transparent 28%),
    linear-gradient(180deg, #060d1b 0%, var(--bg0) 65%);
  min-height: 100vh;
  overflow-x: hidden;
}

body::before {
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  opacity: 0.12;
  background-image: radial-gradient(#2c4f75 0.5px, transparent 0.5px);
  background-size: 18px 18px;
  z-index: -1;
}

.topbar {
  min-height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--line);
  background: rgba(5, 11, 20, 0.92);
  position: sticky;
  top: 0;
  z-index: 20;
  backdrop-filter: blur(8px);
}

.brand-wrap {
  display: flex;
  align-items: baseline;
  gap: 12px;
  min-width: 0;
}

.brand {
  font-family: var(--mono);
  font-size: 19px;
  letter-spacing: 0.06em;
  font-weight: 800;
  color: var(--cyan);
  white-space: nowrap;
}

.subtitle {
  color: var(--muted);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: min(40vw, 460px);
}

.top-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.top-connection-status {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  color: var(--muted);
  font-size: 12px;
}

.top-connection-label {
  color: #87a2c9;
  white-space: nowrap;
}

.top-connection-detail {
  max-width: 220px;
  color: #a8bad8;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.clock {
  font-family: var(--mono);
  color: var(--muted);
  font-size: 12px;
  white-space: nowrap;
}

.btn {
  border: none;
  cursor: pointer;
  border-radius: 10px;
  font-weight: 700;
  font-size: 12px;
  padding: 8px 11px;
  transition: 120ms ease;
  white-space: nowrap;
}

.btn:hover { transform: translateY(-1px); }
.btn:active { transform: translateY(0); }

.btn-primary {
  color: #032130;
  background: linear-gradient(120deg, #23d4ff, #51e7ff);
  box-shadow: 0 0 0 1px rgba(35, 212, 255, 0.45) inset;
}

.btn-ghost {
  color: var(--text);
  background: #0a1528;
  border: 1px solid #395679;
}

.help-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(2, 8, 18, 0.58);
  opacity: 0;
  pointer-events: none;
  transition: opacity 140ms ease;
  z-index: 40;
}

.help-drawer {
  position: fixed;
  top: 0;
  right: 0;
  width: min(460px, calc(100vw - 24px));
  height: 100vh;
  background: linear-gradient(180deg, rgba(15, 24, 40, 0.98), rgba(8, 14, 25, 0.98));
  border-left: 1px solid rgba(61, 93, 141, 0.55);
  box-shadow: -18px 0 32px rgba(0, 0, 0, 0.35);
  transform: translateX(100%);
  transition: transform 160ms ease;
  z-index: 50;
  display: grid;
  grid-template-rows: auto auto 1fr auto;
}

.help-backdrop.open {
  opacity: 1;
  pointer-events: auto;
}

.help-drawer.open {
  transform: translateX(0);
}

.help-head {
  padding: 14px;
  border-bottom: 1px solid rgba(61, 93, 141, 0.35);
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
}

.help-title {
  font-size: 15px;
  font-weight: 700;
  color: var(--text);
}

.help-subtitle {
  font-size: 12px;
  color: var(--muted);
  margin-top: 4px;
}

.help-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 12px 14px;
  border-bottom: 1px solid rgba(61, 93, 141, 0.3);
}

.help-tab {
  border: 1px solid rgba(57, 86, 121, 0.8);
  background: rgba(10, 21, 40, 0.9);
  color: var(--muted);
  border-radius: 999px;
  padding: 6px 10px;
  font-size: 12px;
  cursor: pointer;
}

.help-tab.help-tab-active {
  color: var(--text);
  border-color: rgba(60, 215, 255, 0.55);
  background: rgba(60, 215, 255, 0.14);
}

.help-body {
  overflow: auto;
  padding: 14px;
}

.help-intro {
  border: 1px solid rgba(84, 129, 194, 0.32);
  border-radius: 10px;
  background: rgba(12, 22, 38, 0.78);
  padding: 10px 12px;
  color: #d8e6ff;
  line-height: 1.6;
  font-size: 12px;
  margin-bottom: 12px;
}

.help-section {
  display: grid;
  gap: 8px;
  padding-bottom: 12px;
  margin-bottom: 12px;
  border-bottom: 1px dashed rgba(62, 98, 145, 0.35);
}

.help-section:last-child {
  border-bottom: none;
  margin-bottom: 0;
  padding-bottom: 0;
}

.help-section h3 {
  margin: 0;
  font-size: 13px;
  color: #dce8ff;
}

.help-section ul {
  margin: 0;
  padding-left: 18px;
  display: grid;
  gap: 6px;
}

.help-section li {
  color: #d4deef;
  line-height: 1.6;
  font-size: 12px;
}

.help-section p {
  margin: 0;
  color: #d4deef;
  line-height: 1.6;
  font-size: 12px;
}

.help-detail-list {
  margin: 0;
  padding-left: 18px;
  display: grid;
  gap: 10px;
}

.help-item-subkey {
  font-family: var(--mono);
  font-size: 11px;
  color: #8db0dc;
}

.help-item-scope {
  font-size: 11px;
  color: #d0a464;
}

.help-strategy-card {
  display: grid;
  gap: 8px;
  border: 1px solid rgba(61, 93, 141, 0.35);
  border-radius: 12px;
  background: rgba(9, 18, 31, 0.72);
  padding: 12px;
  margin-bottom: 12px;
}

.help-strategy-card:last-child {
  margin-bottom: 0;
}

.help-strategy-card-active {
  border-color: rgba(60, 215, 255, 0.55);
  box-shadow: 0 0 0 1px rgba(60, 215, 255, 0.2) inset;
}

.help-strategy-summary,
.help-strategy-detail,
.help-strategy-extra {
  color: #d4deef;
  line-height: 1.6;
  font-size: 12px;
}

.help-strategy-preview {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.help-footer {
  min-height: 20px;
  padding: 12px 14px;
  border-top: 1px solid rgba(61, 93, 141, 0.35);
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.help-footer a {
  color: var(--cyan);
  font-size: 12px;
  text-decoration: none;
}

.help-footer a:hover {
  text-decoration: underline;
}

.layout {
  padding: 14px;
  display: grid;
  gap: 14px;
  grid-template-columns: minmax(560px, 1.42fr) minmax(360px, 0.82fr);
  align-items: start;
}

.config-stack {
  min-width: 0;
}

.decision-stack {
  min-width: 0;
}

.panel {
  border: 1px solid var(--line);
  border-radius: 14px;
  background: linear-gradient(180deg, rgba(17, 29, 48, 0.95), rgba(8, 14, 25, 0.95));
  box-shadow: var(--panel-shadow);
  overflow: hidden;
  min-width: 0;
}

.panel-head {
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
  background: rgba(5, 12, 22, 0.72);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.head-title {
  font-family: var(--mono);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--cyan);
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}

.head-desc {
  font-size: 11px;
  color: var(--muted);
}

.panel-body {
  padding: 14px;
}

.stack { display: grid; gap: 14px; align-content: start; }

.chip {
  border: 1px solid rgba(60, 215, 255, 0.55);
  border-radius: 999px;
  color: var(--cyan);
  background: rgba(60, 215, 255, 0.12);
  padding: 3px 8px;
  font-size: 11px;
  white-space: nowrap;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chip.ok {
  color: var(--green);
  border-color: rgba(90, 234, 165, 0.55);
  background: rgba(90, 234, 165, 0.14);
}

.chip.warn {
  color: var(--amber);
  border-color: rgba(255, 214, 122, 0.5);
  background: rgba(255, 214, 122, 0.15);
}

.chip.err {
  color: var(--red);
  border-color: rgba(255, 132, 152, 0.52);
  background: rgba(255, 132, 152, 0.15);
}

.meta {
  display: grid;
  gap: 8px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: rgba(7, 14, 25, 0.8);
  padding: 10px;
  margin-bottom: 12px;
  font-size: 12px;
}

.meta-item {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 10px;
}

.meta-label { color: var(--muted); }
.meta-value { font-family: var(--mono); color: var(--text); }

.meta-value.flash-saved {
  color: var(--green);
  text-shadow: 0 0 12px rgba(90, 234, 165, 0.35);
}

.config-status-inline {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  margin-bottom: 12px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: rgba(7, 14, 25, 0.8);
}

.config-status-item {
  display: inline-flex;
  gap: 8px;
  align-items: baseline;
}

.form-grid {
  display: grid;
  gap: 12px;
  max-height: 560px;
  overflow: auto;
  padding-right: 4px;
}

.strategy-guide-card {
  border: 1px solid rgba(90, 144, 255, 0.28);
  border-radius: 12px;
  background: linear-gradient(135deg, rgba(12, 25, 45, 0.95), rgba(8, 18, 33, 0.92));
  padding: 12px;
  margin-bottom: 12px;
  display: grid;
  gap: 10px;
}

.strategy-guide-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
  flex-wrap: wrap;
}

.strategy-guide-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--text);
  white-space: normal;
  overflow-wrap: anywhere;
}

.strategy-guide-subtitle {
  font-size: 12px;
  color: var(--muted);
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.strategy-guide-head .chip {
  white-space: normal;
  max-width: 100%;
}

.strategy-guide-note {
  font-size: 12px;
  color: #d9e6ff;
  line-height: 1.6;
}

.strategy-guide-preview,
.strategy-guide-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.strategy-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 9px;
  border-radius: 999px;
  background: rgba(43, 83, 145, 0.35);
  border: 1px solid rgba(112, 166, 255, 0.22);
  color: var(--text);
  font-size: 11px;
  white-space: nowrap;
}

.strategy-pill.trade-up {
  background: rgba(24, 129, 91, 0.22);
  border-color: rgba(53, 202, 143, 0.28);
}

.strategy-pill.trade-down {
  background: rgba(165, 54, 54, 0.2);
  border-color: rgba(255, 120, 120, 0.24);
}

.strategy-pill.strategy-info {
  background: rgba(157, 116, 35, 0.22);
  border-color: rgba(229, 183, 92, 0.22);
}

.config-group {
  display: grid;
  gap: 8px;
}

.config-group-head {
  display: grid;
  gap: 3px;
  padding: 0 2px;
}

.config-group-title {
  font-size: 12px;
  font-weight: 700;
  color: #dce8ff;
  letter-spacing: 0.04em;
}

.config-group-desc {
  font-size: 12px;
  line-height: 1.5;
  color: var(--muted);
}

.group-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 150px), 1fr));
  gap: 10px;
}

.field {
  border: 1px solid var(--line);
  border-radius: 10px;
  background: rgba(5, 12, 22, 0.75);
  padding: 9px;
  display: grid;
  gap: 6px;
  min-width: 0;
}

.field.field-wide {
  grid-column: 1 / -1;
}

.field label {
  font-size: 11px;
  color: var(--muted);
  line-height: 1.45;
  min-width: 0;
  overflow-wrap: anywhere;
}

.field input,
.field select,
select.input-compact,
input.input-compact {
  width: 100%;
  min-height: 32px;
  box-sizing: border-box;
  border: 1px solid #2f4b70;
  border-radius: 8px;
  background: #0a1528;
  color: var(--text);
  padding: 6px 8px;
  font-size: 12px;
  font-family: var(--mono);
  min-width: 0;
}

.field input.input-compact,
.field select.input-compact,
select.input-compact,
input.input-compact {
  width: min(100%, 156px);
}

.field-help,
.field-scope-note,
.field-error {
  font-size: 11px;
  line-height: 1.45;
}

.field-help {
  color: var(--muted);
}

.field-scope-note {
  color: #d3a35f;
  min-height: 16px;
}

.field-error {
  color: #ff8d8d;
}

.field.field-muted {
  opacity: 0.58;
  border-style: dashed;
}

.config-group.config-group-muted .config-group-desc {
  color: #c69c58;
}

.actions {
  margin-top: 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.market-grid {
  display: grid;
  gap: 12px;
}

.market-header {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 14px;
  align-items: end;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: rgba(6, 13, 23, 0.76);
  padding: 12px;
}

.deadline {
  font-family: var(--mono);
  font-size: 17px;
  color: #c2f2ff;
  font-weight: 700;
}

.title {
  margin-top: 4px;
  color: #e7eefc;
  font-size: 13px;
}

.slug {
  margin-top: 4px;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--muted);
  word-break: break-all;
}

.timer-wrap { text-align: right; }
.timer-label { font-size: 11px; color: var(--muted); }
.timer-val {
  font-family: var(--mono);
  font-size: 26px;
  font-weight: 700;
  color: var(--cyan);
  line-height: 1.1;
  margin-top: 2px;
}

.split {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.box {
  border: 1px solid var(--line);
  border-radius: 12px;
  background: rgba(6, 12, 22, 0.78);
  padding: 12px;
  display: grid;
  gap: 10px;
  min-width: 0;
}

.box-title {
  font-size: 11px;
  text-transform: uppercase;
  color: var(--muted);
  letter-spacing: 0.08em;
  font-weight: 700;
  font-family: var(--mono);
}

.serial-hint {
  margin-bottom: 10px;
  padding: 8px 10px;
  border: 1px solid rgba(122, 146, 178, 0.28);
  border-radius: 10px;
  color: var(--muted);
  background: rgba(10, 18, 31, 0.58);
  font-size: 12px;
}

.serial-hint.warn {
  color: var(--amber);
  border-color: rgba(245, 166, 35, 0.35);
  background: rgba(245, 166, 35, 0.08);
}

.config-warning-banner {
  display: grid;
  gap: 8px;
  padding: 10px;
  border: 1px solid rgba(255, 214, 122, 0.42);
  border-radius: 10px;
  background: rgba(245, 166, 35, 0.1);
}

.config-warning-banner[hidden] {
  display: none;
}

.config-warning-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.config-warning-title {
  color: var(--amber);
  font-size: 12px;
  font-weight: 800;
}

.config-warning-summary {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.45;
}

.config-warning-list {
  display: grid;
  gap: 6px;
}

.config-warning-item {
  display: grid;
  grid-template-columns: minmax(90px, 0.45fr) minmax(0, 1fr);
  gap: 8px;
  padding: 7px 8px;
  border: 1px solid rgba(255, 214, 122, 0.24);
  border-radius: 8px;
  background: rgba(5, 12, 22, 0.42);
  font-size: 11px;
}

.config-warning-key {
  color: var(--amber);
  font-family: var(--mono);
  overflow-wrap: anywhere;
}

.config-warning-message {
  color: var(--text);
  overflow-wrap: anywhere;
}

.kv-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.kv {
  border: 1px solid rgba(35, 64, 97, 0.75);
  border-radius: 9px;
  padding: 8px;
  background: rgba(5, 12, 22, 0.65);
  min-width: 0;
}

.k {
  font-size: 11px;
  color: var(--muted);
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.v {
  font-size: 14px;
  font-weight: 700;
  font-family: var(--mono);
  overflow-wrap: anywhere;
}

.v.pos { color: var(--green); }
.v.neg { color: var(--red); }
.v.warn { color: var(--amber); }
.v.cyan { color: var(--cyan); }

.rows { display: grid; gap: 8px; }
.strategy-panel-host {
  display: grid;
  gap: 10px;
}

.strategy-panel-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.strategy-mode-switch {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.strategy-mode-active {
  border-color: rgba(63, 205, 255, 0.58);
  color: #eff6ff;
  background: rgba(63, 205, 255, 0.12);
}

.strategy-panel {
  display: grid;
  gap: 6px;
}

.strategy-panel-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border: 1px solid rgba(35, 64, 97, 0.72);
  border-radius: 10px;
  background: rgba(8, 15, 27, 0.82);
}

.strategy-panel-row-primary {
  border-color: rgba(63, 205, 255, 0.4);
  box-shadow: inset 0 0 0 1px rgba(63, 205, 255, 0.08);
}

.strategy-panel-row-main {
  min-width: 0;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  gap: 10px;
}

.strategy-panel-toggle,
.strategy-panel-primary {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--muted);
  font-size: 11px;
  white-space: normal;
}

.strategy-panel-toggle input,
.strategy-panel-primary input {
  margin: 0;
  width: 14px;
  min-width: 14px;
  height: 14px;
  min-height: 14px;
  padding: 0;
}

.strategy-panel-meta {
  min-width: 0;
  display: flex;
  align-items: center;
}

.strategy-panel-title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-weight: 700;
  color: #eff6ff;
  font-size: 12px;
  min-width: 0;
  overflow-wrap: anywhere;
}

.strategy-panel-summary {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.45;
}

.strategy-profile-editor,
.strategy-profile-card {
  display: grid;
  gap: 10px;
}

.strategy-profile-editor {
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 340px), 1fr));
}

.strategy-profile-editor > .strategy-profile-title,
.strategy-profile-editor > .strategy-profile-subtitle,
.strategy-profile-editor > .empty {
  grid-column: 1 / -1;
}

.strategy-profile-card {
  border: 1px solid rgba(90, 144, 255, 0.24);
  border-radius: 10px;
  padding: 10px;
  background: rgba(6, 12, 22, 0.72);
  min-width: 0;
}

.strategy-profile-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 10px;
  flex-wrap: wrap;
  min-width: 0;
}

.strategy-profile-title {
  font-size: 12px;
  font-weight: 700;
  color: var(--text);
}

.strategy-profile-subtitle {
  font-size: 11px;
  color: var(--muted);
  line-height: 1.45;
}

.strategy-profile-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 150px), 1fr));
  gap: 8px;
}

.strategy-profile-field {
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 6px;
  align-items: stretch;
  font-size: 11px;
}

.strategy-profile-field label {
  color: var(--muted);
  overflow-wrap: anywhere;
}

.strategy-profile-field .chip {
  justify-self: start;
  max-width: 100%;
  white-space: normal;
  overflow-wrap: anywhere;
}

.strategy-panel-primary {
  justify-self: end;
}

.strategy-panel-hidden-input {
  display: none;
}

.row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
  border-bottom: 1px dashed rgba(35, 64, 97, 0.6);
  padding-bottom: 6px;
  font-size: 12px;
}

.row:last-child {
  border-bottom: none;
  padding-bottom: 0;
}

.label { color: var(--muted); }
.value { font-family: var(--mono); }

.runtime-list {
  display: grid;
  gap: 8px;
  max-height: 260px;
  overflow: auto;
  padding-right: 4px;
}

.runtime-item {
  border: 1px solid var(--line);
  border-radius: 9px;
  background: rgba(6, 12, 22, 0.72);
  padding: 8px;
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
}

.runtime-item .rk { color: var(--muted); }
.runtime-item .rv { font-family: var(--mono); color: var(--text); word-break: break-all; text-align: right; }

.days-table-wrap {
  border: 1px solid var(--line);
  border-radius: 10px;
  overflow: auto;
  max-height: 260px;
  background: rgba(6, 12, 22, 0.74);
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

th,
td {
  padding: 8px 7px;
  border-bottom: 1px solid rgba(37, 64, 98, 0.55);
  white-space: nowrap;
  text-align: left;
}

th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: #0a1424;
  color: #96afd4;
  font-size: 11px;
  letter-spacing: 0.05em;
  font-family: var(--mono);
  text-transform: uppercase;
}

tr:hover td { background: rgba(50, 88, 131, 0.1); }

.unified-report-card {
  grid-column: 1 / -1;
}

.report-card-head {
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
  background: rgba(5, 12, 22, 0.72);
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}

.report-status-group {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.report-card-body {
  display: grid;
  grid-template-columns: minmax(220px, 0.55fr) minmax(0, 1.85fr);
  gap: 14px;
  padding: 14px;
}

.report-section {
  min-width: 0;
  display: grid;
  grid-auto-rows: max-content;
  gap: 10px;
}

.strategy-days-table {
  max-height: 300px;
  border: 1px solid var(--line);
  border-radius: 10px;
}

.strategy-days-table table {
  min-width: 560px;
}

.report-recent-table {
  max-height: 420px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: rgba(6, 12, 22, 0.66);
}

.report-recent-table table {
  table-layout: fixed;
}

.report-recent-table th,
.report-recent-table td {
  padding: 7px 5px;
  white-space: normal;
  overflow-wrap: anywhere;
  line-height: 1.35;
  vertical-align: top;
}

.report-recent-table th:nth-child(1) { width: 9%; }
.report-recent-table th:nth-child(2) { width: 8%; }
.report-recent-table th:nth-child(3) { width: 6%; }
.report-recent-table th:nth-child(4) { width: 5%; }
.report-recent-table th:nth-child(5) { width: 6%; }
.report-recent-table th:nth-child(6) { width: 7%; }
.report-recent-table th:nth-child(7) { width: 5%; }
.report-recent-table th:nth-child(8) { width: 6%; }
.report-recent-table th:nth-child(9) { width: 6%; }
.report-recent-table th:nth-child(10) { width: 6%; }
.report-recent-table th:nth-child(11) { width: 7%; }
.report-recent-table th:nth-child(12) { width: 7%; }
.report-recent-table th:nth-child(13) { width: 15%; }
.report-recent-table th:nth-child(14) { width: 7%; }

.section-title {
  font-size: 11px;
  text-transform: uppercase;
  color: var(--muted);
  letter-spacing: 0.08em;
  font-weight: 700;
  font-family: var(--mono);
}

.section-desc {
  font-size: 11px;
  color: var(--muted);
}

.table-wrap {
  max-height: 380px;
  overflow: auto;
  border-top: 1px solid var(--line);
  background: rgba(6, 12, 22, 0.66);
}

.report-recent-table.table-wrap {
  overflow-x: hidden;
  overflow-y: auto;
}

.trade-up { color: var(--green); font-weight: 700; }
.trade-down { color: var(--red); font-weight: 700; }
.trade-skip { color: var(--amber); font-weight: 700; }
.recent-price-check {
  display: inline-block;
  margin-top: 2px;
  font-size: 11px;
  line-height: 1.2;
  white-space: nowrap;
}
.pnl-plus { color: var(--green); font-family: var(--mono); }
.pnl-minus { color: var(--red); font-family: var(--mono); }
.skip-reason-badge {
  display: inline-flex;
  align-items: center;
  padding: 3px 8px;
  border-radius: 999px;
  border: 1px solid transparent;
}
.skip-reason-badge.missed-entry {
  color: #ffd7ad;
  background: rgba(255, 138, 61, 0.14);
  border-color: rgba(255, 138, 61, 0.32);
  font-weight: 700;
}
.recent-pending td {
  background: rgba(245, 166, 35, 0.08);
}
.recent-missed-entry td {
  background: rgba(255, 138, 61, 0.08);
}

.empty {
  text-align: center;
  color: var(--muted);
  padding: 22px;
  font-size: 12px;
}

.footnote {
  margin-top: 8px;
  color: #87a2c9;
  font-size: 11px;
  line-height: 1.45;
}

::-webkit-scrollbar { width: 6px; height: 8px; }
::-webkit-scrollbar-track { background: #08101f; }
::-webkit-scrollbar-thumb { background: #34557e; border-radius: 999px; }

@media (max-width: 1450px) {
  .layout {
    grid-template-columns: minmax(500px, 1.28fr) minmax(340px, 0.9fr);
  }

  .report-card-body {
    grid-template-columns: minmax(200px, 0.48fr) minmax(0, 1.72fr);
  }
}

@media (max-width: 1024px) {
  .layout { grid-template-columns: 1fr; }
  .report-card-body { grid-template-columns: 1fr; }
  .strategy-panel-row { grid-template-columns: 1fr; }
  .strategy-panel-primary { justify-self: start; }
  .split,
  .kv-grid,
  .group-grid { grid-template-columns: 1fr; }
  .market-header { grid-template-columns: 1fr; }
  .timer-wrap { text-align: left; }
  .subtitle { max-width: 56vw; }
  .top-connection-detail { display: none; }
  .help-drawer { width: 100vw; }
}
"""


def _dashboard_js() -> str:
    return """
const state = {
  config: null,
  market: null,
  summary: null,
  recent: null,
  summaryRequestSeq: 0,
  recentRequestSeq: 0,
  recentRefreshInFlight: false,
  paperRuntimeCards: {},
  marketStrategyFilter: 'all',
  paperReportStrategyFilter: 'all',
  paperSummaryStrategyFilter: null,
  paperRecentStrategyFilter: null,
  reportMode: 'paper',
  reportModeUserSelected: false,
  paperTimeframeFilter: '',
  countdownSnapshotAtMs: null,
  countdownBaseSeconds: null,
  showInternalKeys: false,
  runtimeDetailsOpen: false,
  diagnosticsOpen: false,
  signalDetailsOpen: false,
  advancedConfigOpen: false,
  strategyEditMode: 'paper',
  lastRuntimeAlertKey: null,
  helpOpen: false,
  helpTab: 'quickstart',
  helpReturnFocusId: 'btnHelp',
};

const POLL_MS = {
  market: 3000,
  summary: 20000,
  recent: 12000,
  clock: 1000,
};

const TIMEFRAME_META = {
  "5m": {
    label: "5分钟",
    brand: "QUANT_CMD · BTC_5M",
    marketDesc: "5分钟轮次行情 / 方向信号 / 下注计划",
  },
  "15m": {
    label: "15分钟",
    brand: "QUANT_CMD · BTC_15M",
    marketDesc: "15分钟轮次行情 / 方向信号 / 下注计划",
  },
};

const HELP_TABS = [
  { id: 'quickstart', label: '快速上手' },
  { id: 'pageguide', label: '页面说明' },
  { id: 'configdict', label: '配置字典' },
  { id: 'riskguide', label: '风控限制' },
  { id: 'strategyguide', label: '策略说明' },
  { id: 'faq', label: '常见问题' },
];

const HELP_SECTIONS = {
  quickstart: {
    title: '快速上手',
    intro: '先确认基础策略、固定下注金额和单笔最大下注金额，再连续观察 3-5 轮后再决定是否同时改多个参数。',
    sections: [
      {
        title: '先看哪里',
        bullets: [
          '先看行情与信号，确认当前轮次、方向判断和倒计时是否正常。',
          '再看下注计划与风控，确认当前是否准备下注、为什么跳过、以及预期收益是多少。',
          '然后看会话状态，关注累计盈亏、连续亏损轮数和当日已实现盈亏。',
          '最后看实时连接状态，判断实时连接数据是否可靠。',
        ],
      },
      {
        title: '怎么安全改参数',
        bullets: [
          '调整前先确认当前查看的是哪一个策略，因为固定节奏策略和策略 5 使用的参数并不完全一样。',
          '一次只改一类参数，不要把策略、阈值和下注规模同时一起改。',
          '保存后优先看页面展示的生效值和字段提示，不要只看自己输入了什么。',
          '如果字段出现校验错误，说明这次输入其实还没有真正生效。',
        ],
      },
      {
        title: '怎么判断当前能不能跑',
        bullets: [
          '允许下单=是，说明当前轮次、价格、风控检查和实时连接防护都允许执行。',
          '允许下单=否时先结合跳过原因一起看，不要先默认程序坏了。',
          '价格超过阈值、弱信号、超过下注上限，这些都属于正常的跳过原因。',
          '如果是实时连接陈旧、当日亏损限制或止损重置，则应立即复核。',
        ],
      },
    ],
  },
  pageguide: {
    title: '页面元素说明',
    sections: [
      {
        title: '参数引擎',
        bullets: [
          '用于查看并编辑运行参数。',
          '重点关注基础策略、固定下注金额、风控边界，以及哪些字段只对策略 5 生效。',
        ],
      },
      {
        title: '行情与信号',
        bullets: [
          '用于观察当前轮次市场状态和方向判断。',
          '重点关注方向、原因、阈值、偏移和是否已锁边。',
        ],
      },
      {
        title: '下注计划与风控',
        bullets: [
          '用于判断当前轮次是否允许执行。',
          '重点关注是否下单、买入价格、下单金额和跳过原因。',
        ],
      },
      {
        title: '会话状态',
        bullets: [
          '用于看累计收益和当前风控状态。',
          '重点关注累计盈亏、连续亏损轮数和当日已实现盈亏。',
        ],
      },
      {
        title: '实时连接状态',
        bullets: [
          '用于判断实时连接行情是否可信。',
          '重点关注最近消息延迟、重连次数、最近错误和是否触发陈旧保护。',
        ],
      },
      {
        title: '纸面交易汇总',
        bullets: [
          '用于从日维度查看策略近期表现。',
          '适合看趋势，不适合解释某一笔具体异常。',
        ],
      },
      {
        title: '最近交易明细',
        bullets: [
          '用于排查最近交易到底发生了什么。',
          '重点关注时间、方向、结果、跳过原因和信号偏移。',
        ],
      },
    ],
  },
  riskguide: {
    title: '风控与下单限制',
    sections: [
      {
        title: '所有模式通用',
        bullets: [
          '纸面和实盘都会先经过同一套下注计划检查：价格、下注金额、连续亏损和止损重置都会先判断。',
          'MAX_CONSECUTIVE_LOSSES 控制连续亏损达到多少轮后触发止损重置，重置会清空连续亏损计数。',
          'MAX_STAKE 是单笔最大下注金额；计算出的 order_cost 超过它时，本轮会跳过并显示 order_cost_above_max_stake。',
          'MAX_ENTRY_PRICE / MAX_PRICE_THRESHOLD 按含手续费后的有效买入价限制；实盘会反推官方 raw price 作为订单价格保护。',
        ],
      },
      {
        title: '纸面与实盘差异',
        bullets: [
          '纸面模式不会读取真实钱包，会使用 PAPER_SIMULATED_WALLET_BALANCE 作为 dry-run 钱包预算。',
          '并行实盘会读取真实钱包余额；余额不可用会显示“实盘钱包余额不可用”，余额不足会显示“实盘钱包余额不足”。',
          '纸面和实盘的信号、风控和预算检查路径应保持一致；纸面记录有效买入价，实盘同时记录官方 raw_price 和 live_price_cap。',
        ],
      },
      {
        title: '常见跳过原因',
        bullets: [
          'price_above_threshold 表示目标方向原始入场价高于上限；price_below_threshold 表示入场候选价格低于下限。',
          '实盘成交后的有效价低于最低入场价不是违规，会在最近明细中显示为价格改善。',
          'order_cost_above_max_stake 表示本轮所需下注金额超过 MAX_STAKE。',
          'max_consecutive_losses_reached 表示连续亏损已达到 MAX_CONSECUTIVE_LOSSES，本轮触发止损重置。',
          'ws_stale 表示实时行情过旧，程序会阻止本轮交易，避免拿陈旧报价下单。',
          'entry_window_missed 表示已经错过当前轮次允许入场的时间窗口。',
        ],
      },
      {
        title: '策略专属门槛',
        bullets: [
          '策略 1-4 是固定节奏策略，没有额外信号门槛，但仍受价格、下注金额、连续亏损和预算限制。',
          '策略 5 使用动量信号，SIGNAL_MOMENTUM_THRESHOLD 不满足时会跳过；价格不可用也会跳过。',
          '策略 6 使用 Binance OFI，OFI_THRESHOLD 不满足、信号过期或不可用时都会跳过。',
          '策略 7 要求 Binance OFI 与 Polymarket 动量同向确认，冲突、弱信号、过期、价格过高或确认过晚都会跳过。',
        ],
      },
      {
        title: '重置与告警',
        bullets: [
          '亏损只会增加 consecutive_losses，不会增加 recovery_loss 或放大下一单。',
          '连续亏损达到 MAX_CONSECUTIVE_LOSSES 会触发止损重置，并增加 stop_loss_count。',
          '连续因 MAX_STAKE 超额而跳过也会累积计数，达到 MAX_CONSECUTIVE_LOSSES 时会按风险门处理并重置。',
          'MAX_STAKE_SKIP_ALERT_THRESHOLD 控制连续超额跳过多少次后打印提醒。',
        ],
      },
    ],
  },
};

const HELP_FAQ = [
  ['为什么保存了参数却像是没生效？', '先看字段提示和页面上的生效值。无效输入会回退到上一次有效配置，不会带着错误参数直接运行。'],
  ['为什么现在没有下注？', '先看下注计划与风控里的跳过原因，再判断是价格、风控、信号还是实时连接保护在阻止执行。'],
  ['为什么策略 5 经常没有信号？', '策略 5 不是固定节奏，它需要价格波动幅度超过阈值；弱信号会按照“跳过”或“回退到基础策略”这两种规则处理。'],
  ['为什么方向和我预期的不一样？', '固定节奏策略取决于当前轮次索引；动量策略则取决于开盘价、当前价、阈值和偏移。'],
  ['为什么会触发实时连接保护？', '这表示实时连接行情数据已经过旧，系统会选择阻止执行，而不是拿陈旧数据去下注。'],
  ['为什么当日已实现盈亏会重置？', '这是按天统计的自然切日行为；累计盈亏仍然保留在会话状态里。'],
  ['实盘自动赎回开关是什么意思？', '这是实盘自动赎回开关。开启后，并行实盘会启动单独的自动赎回线程，自动扫描并尝试赎回获胜仓位。'],
  ['自动赎回演练模式需要关闭吗？', '不需要一直关闭。它是一个安全阀：开启时只做演练，不会发送真实的 Polygon 赎回交易。正常实盘应保持关闭，仅在首次验证或调试时开启。'],
  ['为什么触发最大下注金额后会连续跳过？', '当前固定下注金额、信号金额倍数或价格条件可能会让本轮 order_cost 超过最大下注金额。'],
  ['新用户最容易配错什么？', '最常见的是一次改太多参数、把固定节奏和动量逻辑混在一起看，以及把实时连接保护误当成策略故障。'],
];

const STORAGE_KEYS = {
  showInternalKeys: 'dashboard_show_internal_keys',
  reportMode: 'dashboard_report_mode',
};

const STRATEGY_LABELS = {
  1: '单轮交替',
  2: '双轮分组交替',
  3: '三轮分组交替',
  4: '四轮分组交替',
  5: '动量信号 V2',
  6: '币安盘口失衡',
  7: '盘口+动量共识',
  8: '状态切换',
};

const OPTION_LABELS = {
  ENABLE_LIVE_TRADING: {
    true: '开启',
    false: '关闭',
  },
  TRADE_MODE: {
    paper: '模拟盘',
    live: '实盘',
    both: '纸面+实盘',
  },
  LIVE_TRADING_ENABLED: {
    true: '开启',
    false: '关闭',
  },
  SIGNAL_WEAK_SIGNAL_MODE: {
    SKIP: '弱信号跳过',
    FALLBACK: '按跳过处理',
  },
  WS_ENABLED: {
    true: '开启',
    false: '关闭',
  },
};

const REASON_LABELS = {
  strategy7_momentum_too_hot: '策略7 动量过热，跳过追单',
  observed_waiting_for_entry: '等待入场观察中',
  entry_window_missed: '已错过入场时间',
  ws_stale: '连接数据陈旧',
  signal_unavailable: '信号不可用',
  signal_too_weak_skip: '信号太弱，按规则跳过',
  signal_too_weak: '信号太弱',
  signal_too_weak_fallback: '信号太弱，回退到基础策略',
  signal_price_unavailable: '信号价格不可用',
  signal_price_unavailable_fallback: '信号价格不可用，回退到基础策略',
  price_above_threshold: '价格超过上限阈值',
  price_below_threshold: '价格低于下限阈值',
  invalid_price: '价格无效',
  invalid_base_order_cost: '固定下注金额无效',
  order_cost_below_min_stake: '下单金额低于单笔下限',
  order_cost_above_max_stake: '下单金额超过单笔上限',
  order_size_not_positive: '下单份额无效',
  max_consecutive_losses_reached: '达到连续亏损重置阈值',
  stop_loss_triggered: '触发止损重置',
  manual_skip: '人工跳过',
  ofi_unavailable: '盘口失衡信号不可用',
  ofi_stale: '盘口失衡信号已过期',
  ofi_too_weak: '盘口失衡信号过弱',
  strategy7_ofi_unavailable: '策略7 盘口失衡信号不可用',
  strategy7_ofi_stale: '策略7 盘口失衡信号已过期',
  strategy7_ofi_too_weak: '策略7 盘口失衡信号过弱',
  strategy7_momentum_unavailable: '策略7 动量信号不可用',
  strategy7_momentum_too_weak: '策略7 动量信号过弱',
  strategy7_signal_conflict: '盘口失衡与动量需同向确认',
  strategy7_entry_too_late: '策略7 确认出现过晚',
  strategy7_price_too_low: '策略7 入场价格过低',
  strategy7_price_too_high: '策略7 入场价格过高',
  strategy7_confidence_too_low: '策略7 信号优势不足',
  strategy8_signal_unavailable: '策略8 信号不可用',
  strategy8_ofi_unavailable: '策略8 盘口失衡信号不可用',
  strategy8_ofi_stale: '策略8 盘口失衡信号已过期',
  strategy8_momentum_unavailable: '策略8 动量信号不可用',
  strategy8_market_state_weak: '策略8 市场状态不明确',
  strategy8_conflict_reversal: '策略8 强冲突反转入场',
  strategy8_entry_too_late: '策略8 确认出现过晚',
  strategy8_price_too_low: '策略8 入场价格过低',
  strategy8_price_too_high: '策略8 入场价格过高',
  strategy9_ofi_unavailable: '策略9 盘口失衡信号不可用',
  strategy9_ofi_stale: '策略9 盘口失衡信号已过期',
  strategy9_ofi_too_weak: '策略9 盘口失衡信号过弱',
  strategy9_momentum_unavailable: '策略9 动量信号不可用',
  strategy9_momentum_too_weak: '策略9 动量信号过弱',
  strategy9_momentum_too_hot: '策略9 动量过热，跳过追单',
  strategy9_signal_conflict: '策略9 盘口失衡与动量需同向确认',
  strategy9_confidence_too_low: '策略9 信号优势不足',
  strategy9_entry_too_late: '策略9 确认出现过晚',
  strategy9_signal_unstable: '策略9 信号稳定性不足',
  strategy9_signal_decaying: '策略9 信号明显衰减',
  strategy9_dynamic_price_too_high: '策略9 动态价帽过高',
  strategy9_price_too_low: '策略9 入场价格过低',
  strategy9_price_too_high: '策略9 入场价格过高',
  strategy10_ofi_unavailable: '策略10 盘口失衡信号不可用',
  strategy10_ofi_stale: '策略10 盘口失衡信号已过期',
  strategy10_momentum_unavailable: '策略10 动量信号不可用',
  strategy10_momentum_too_cold: '策略10 动量低于测试区间',
  strategy10_momentum_too_hot: '策略10 动量高于测试区间',
  strategy10_edge_too_low: '策略10 估值优势不足',
  strategy10_signal_conflict: '策略10 锁边后信号反向',
  strategy10_entry_too_late: '策略10 确认出现过晚',
  strategy10_price_too_low: '策略10 入场价格过低',
  strategy10_price_too_high: '策略10 入场价格过高',
  live_order_book_depth_insufficient: '盘口深度不足',
  live_order_book_price_below_min_entry: '盘口价格低于入场下限',
  live_order_book_price_improved_too_much: '盘口价格偏离过大',
  live_order_book_price_above_max_entry: '盘口价格高于买入上限',
  live_order_book_unavailable: '盘口暂不可用',
  official_fill_price_below_min_entry: '成交价低于入场下限',
  official_fill_price_below_decision_floor: '成交价偏离过大',
  strategy11_window_unavailable: '策略11 当前轮次不可用',
  strategy11_btc_price_stale: '策略11 BTC 价格信号已过期',
  strategy11_btc_price_unavailable: '策略11 BTC 价格不可用',
  strategy11_probability_unavailable: '策略11 概率估算不可用',
  strategy11_edge_too_low: '策略11 概率优势不足',
  strategy11_signal_conflict: '策略11 锁边后信号反向',
  strategy11_entry_too_late: '策略11 确认出现过晚',
  strategy11_price_too_low: '策略11 入场价格过低',
  strategy11_price_too_high: '策略11 入场价格过高',
  strategy12_window_unavailable: '策略12 当前轮次不可用',
  strategy12_btc_price_stale: '策略12 BTC 价格信号已过期',
  strategy12_btc_price_unavailable: '策略12 BTC 价格不可用',
  strategy12_probability_unavailable: '策略12 概率估算不可用',
  strategy12_edge_too_low: '策略12 概率优势不足',
  strategy12_signal_conflict: '策略12 概率方向与盘口确认冲突',
  strategy12_entry_too_late: '策略12 确认出现过晚',
  strategy12_price_too_low: '策略12 入场价格过低',
  strategy12_price_too_high: '策略12 入场价格过高',
  strategy12_micro_ofi_unavailable: '策略12 盘口失衡信号不可用',
  strategy12_micro_ofi_stale: '策略12 盘口失衡信号已过期',
  strategy12_micro_ofi_too_weak: '策略12 盘口失衡信号过弱',
  strategy12_micro_momentum_unavailable: '策略12 动量信号不可用',
  strategy12_micro_momentum_too_weak: '策略12 动量信号过弱',
  strategy12_micro_momentum_too_hot: '策略12 动量过热，跳过追单',
  strategy12_micro_confidence_too_low: '策略12 盘口确认优势不足',
  live_fok_not_filled: '实时 FOK 订单未成交',
  live_fak_not_matched: '实时 FAK 订单无可成交挂单',
  awaiting_fill_confirmation: '等待成交确认',
  round_in_progress: '轮次仍在进行中',
  round_unresolved: '轮次尚未结算',
  live_wallet_balance_unavailable: '实盘钱包余额不可用',
  insufficient_live_wallet_balance: '实盘钱包余额不足',
  strategy_evaluation_error: '策略评估异常',
  strategy_settlement_error: '策略结算异常',
  market_timeframe: '市场频次切换待生效',
  'INVALID OPERATION': '实时连接订阅请求无效',
};

const CONFIG_KEY_NAMES = {
  STRATEGY7_MAX_MOMENTUM_DELTA: '策略7 动量过热上限',
  ENABLE_LIVE_TRADING: '并行实盘',
  TRADE_MODE: '运行视角',
  LIVE_TRADING_ENABLED: '并行实盘开关',
  POLYMARKET_PRIVATE_KEY: '实盘私钥',
  POLYMARKET_FUNDER: '\u5b9e\u76d8\u94b1\u5305\u5730\u5740',
  POLYMARKET_FOK_FALLBACK_TO_FAK: 'FOK 未成交改用 FAK',
  POLYMARKET_PRECHECK_ORDER_BOOK_DEPTH: '下单前检查盘口深度',
  STRATEGY_ID: '基础策略',
  STRATEGY_IDS: '统一策略组合',
  BASE_ORDER_COST: '固定下注金额',
  MIN_STAKE: '单笔最小下注金额',
  PAPER_SIMULATED_WALLET_BALANCE: '纸面模拟钱包余额',
  MAX_CONSECUTIVE_LOSSES: '连亏重置轮数',
  MAX_STAKE: '单笔最大下注金额',
  MIN_ENTRY_PRICE: '最低买入价格',
  MAX_ENTRY_PRICE: '最高有效买入价(含费)',
  LIVE_MAX_PRICE_IMPROVEMENT: '最大允许价格改善',
  MAX_PRICE_THRESHOLD: '最高买入价格阈值',
  STRATEGY7_OFI_THRESHOLD: '策略7 盘口失衡阈值',
  STRATEGY7_MOMENTUM_THRESHOLD: '策略7 动量阈值',
  STRATEGY7_MIN_SIGNAL_GAP: '策略7 最小信号优势',
  STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS: '策略7 最晚确认秒数',
  STRATEGY7_DYNAMIC_SIZING_ENABLED: '策略7 动态下注',
  STRATEGY7_SIZING_REFERENCE_PRICE: '策略7 金额参考价',
  STRATEGY7_SIZING_PRICE_STEP: '策略7 金额价格步长',
  STRATEGY7_SIZING_PRICE_STEP_REDUCTION: '策略7 每步缩仓比例',
  STRATEGY7_SIZING_MIN_MULTIPLIER: '策略7 最小金额倍数',
  STRATEGY7_SIZING_MAX_MULTIPLIER: '策略7 最大金额倍数',
  STRATEGY7_SIZING_STRONG_SIGNAL_GAP: '策略7 加仓信号优势',
  STRATEGY7_SIZING_STRONG_SIGNAL_BOOST: '策略7 强信号金额补偿',
  STRATEGY9_DYNAMIC_SIZING_ENABLED: '策略9 动态下注',
  STRATEGY9_SIZING_REFERENCE_PRICE: '策略9 金额参考价',
  STRATEGY9_SIZING_PRICE_STEP: '策略9 金额价格步长',
  STRATEGY9_SIZING_PRICE_STEP_REDUCTION: '策略9 每步缩仓比例',
  STRATEGY9_SIZING_MIN_MULTIPLIER: '策略9 最小金额倍数',
  STRATEGY9_SIZING_MAX_MULTIPLIER: '策略9 最大金额倍数',
  STRATEGY9_SIZING_STRONG_SIGNAL_GAP: '策略9 加仓信号优势',
  STRATEGY9_SIZING_STRONG_SIGNAL_BOOST: '策略9 强信号金额补偿',
  STRATEGY9_STABILITY_SAMPLE_COUNT: '策略9 稳定采样数',
  STRATEGY9_STABILITY_REQUIRED_COUNT: '策略9 同向采样数',
  STRATEGY9_STABILITY_WINDOW_SECONDS: '策略9 稳定窗口秒',
  STRATEGY9_REVERSAL_LOOKBACK_SECONDS: '策略9 衰减回看秒',
  STRATEGY9_MAX_SIGNAL_DECAY: '策略9 最大信号衰减',
  STRATEGY9_BASE_MAX_ENTRY_PRICE: '策略9 普通价格上限',
  STRATEGY9_STRONG_MAX_ENTRY_PRICE: '策略9 强信号价格上限',
  STRATEGY9_ULTRA_MAX_ENTRY_PRICE: '策略9 超强信号价格上限',
  STRATEGY9_STRONG_SIGNAL_GAP: '策略9 强信号优势',
  STRATEGY9_ULTRA_SIGNAL_GAP: '策略9 超强信号优势',
  STRATEGY10_MIN_EDGE: '策略10 最小期望优势',
  STRATEGY10_EDGE_BUFFER: '策略10 成本缓冲',
  STRATEGY10_OFI_WEIGHT: '策略10 OFI 权重',
  STRATEGY10_MOMENTUM_WEIGHT: '策略10 动量权重',
  STRATEGY10_MAX_FAIR_VALUE: '策略10 估值上限',
  STRATEGY10_MIN_MOMENTUM_DELTA: '策略10 最小动量',
  STRATEGY10_MAX_MOMENTUM_DELTA: '策略10 最大动量',
  STRATEGY10_DOWN_MIN_EDGE: '策略10 DOWN 最小优势',
  STRATEGY10_CONFIRM_BEFORE_ENTRY_SECONDS: '策略10 最晚确认秒数',
  STRATEGY11_MIN_EDGE: '策略11 最小概率优势',
  STRATEGY11_EDGE_BUFFER: '策略11 成本缓冲',
  STRATEGY11_VOLATILITY_BPS_PER_SQRT_MINUTE: '策略11 波动率估计',
  STRATEGY11_MIN_PROBABILITY: '策略11 最低方向概率',
  STRATEGY11_MAX_PROBABILITY: '策略11 概率上限',
  STRATEGY11_CONFIRM_BEFORE_ENTRY_SECONDS: '策略11 最晚确认秒数',
  SIGNAL_MOMENTUM_THRESHOLD: '动量阈值',
  SIGNAL_WEAK_SIGNAL_MODE: '弱信号处理',
  SIGNAL_FALLBACK_STRATEGY_ID: '弱信号回退基础策略',
  SIGNAL_HISTORY_FIDELITY_SECONDS: '信号采样秒数',
  SIGNAL_ANCHOR_MAX_OFFSET_SECONDS: '开盘锚点最大偏移秒',
  SIGNAL_DYNAMIC_THRESHOLD_K: '动态阈值系数K',
  SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS: '动态阈值最少样本点',
  SIGNAL_LOCK_BEFORE_ENTRY_SECONDS: '入场前锁边秒数',
  MAX_STAKE_SKIP_ALERT_THRESHOLD: '超额跳过告警阈值',
  WS_ENABLED: '实时连接开关',
  WS_QUOTE_STALE_SECONDS: '行情过期秒',
  WS_TRADE_GUARD_STALE_SECONDS: '交易防陈旧阈值秒',
  WS_CONNECT_TIMEOUT_SECONDS: '实时连接超时秒',
};

function reasonText(reason) {
  if (!reason) {
    return '--';
  }
  if (REASON_LABELS[reason]) {
    return REASON_LABELS[reason];
  }
  const rawReason = String(reason);
  const reasonCode = rawReason.split(':', 1)[0];
  if (REASON_LABELS[reasonCode]) {
    return REASON_LABELS[reasonCode];
  }
  return rawReason;
}

function formatConfigLabel(key, labels) {
  const base = (labels && labels[key]) || CONFIG_KEY_NAMES[key] || key;
  if (state.showInternalKeys) {
    return base + '（' + key + '）';
  }
  return base;
}

function loadUiPrefs() {
  try {
    const raw = localStorage.getItem(STORAGE_KEYS.showInternalKeys);
    if (raw === null) {
      state.showInternalKeys = false;
    } else {
      state.showInternalKeys = raw === '1';
    }
    const storedReportMode = localStorage.getItem(STORAGE_KEYS.reportMode);
    state.reportMode = storedReportMode === 'live' ? 'live' : 'paper';
    state.reportModeUserSelected = false;
  } catch (_err) {
    state.showInternalKeys = false;
    state.reportMode = 'paper';
    state.reportModeUserSelected = false;
  }
}

function saveUiPrefs() {
  try {
    localStorage.setItem(STORAGE_KEYS.showInternalKeys, state.showInternalKeys ? '1' : '0');
    localStorage.setItem(STORAGE_KEYS.reportMode, effectiveReportMode());
  } catch (_err) {
    // Ignore storage failures (private mode / storage disabled)
  }
}

function syncToggleButtonText() {
  return;
}

function toggleFoldSection(sectionId, expanded) {
  const panel = el(sectionId + 'Panel');
  const toggle = el(sectionId + 'Toggle');
  if (!panel || !toggle) {
    return;
  }
  const nextExpanded = typeof expanded === 'boolean' ? expanded : panel.hidden;
  panel.hidden = !nextExpanded;
  toggle.setAttribute('aria-expanded', nextExpanded ? 'true' : 'false');
  toggle.textContent = nextExpanded
    ? (sectionId === 'runtimeDetails'
        ? '收起运行详情'
        : (sectionId === 'diagnostics'
            ? '收起诊断区'
            : (sectionId === 'advancedConfig' ? '收起高级参数' : '收起信号详情')))
    : (sectionId === 'runtimeDetails'
        ? '展开运行详情'
        : (sectionId === 'diagnostics'
            ? '展开诊断区'
            : (sectionId === 'advancedConfig' ? '展开高级参数' : '展开信号详情')));
}

function openHelpDrawer(tab = 'quickstart') {
  state.helpOpen = true;
  state.helpTab = tab;
  renderHelpDrawer();
  const drawer = el('helpDrawer');
  if (drawer) {
    drawer.focus();
  }
}

function closeHelpDrawer() {
  state.helpOpen = false;
  renderHelpDrawer();
  const trigger = el(state.helpReturnFocusId || 'btnHelp');
  if (trigger) {
    trigger.focus();
  }
}

function el(id) {
  return document.getElementById(id);
}

function esc(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function toNum(value) {
  if (value === null || value === undefined || value === '') {
    return null;
  }
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function fmtNum(value, digits = 4) {
  const n = toNum(value);
  if (n === null) {
    return '--';
  }
  return n.toFixed(digits);
}

function fmtPnl(value, digits = 4) {
  const n = toNum(value);
  if (n === null) {
    return '--';
  }
  const sign = n > 0 ? '+' : '';
  return sign + n.toFixed(digits);
}

function fmtPct(value, digits = 2) {
  const n = toNum(value);
  if (n === null) {
    return '--';
  }
  return (n * 100).toFixed(digits) + '%';
}

function fmtIso(value) {
  if (!value) {
    return '--';
  }
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) {
    return String(value);
  }
  return dt.toLocaleString('zh-CN', { hour12: false });
}

function formatRoundSlug(value) {
  if (!value) {
    return '--';
  }
  const raw = String(value).trim();
  const match = raw.match(/-(\\d{10})(?:$|\\D)/);
  if (!match) {
    return raw;
  }
  const ts = Number(match[1]);
  if (!Number.isFinite(ts)) {
    return raw;
  }
  const dt = new Date(ts * 1000);
  if (Number.isNaN(dt.getTime())) {
    return raw;
  }
  return dt.toLocaleString('zh-CN', { hour12: false });
}

function fmtSeconds(value) {
  const n = toNum(value);
  if (n === null) {
    return '--';
  }
  const sign = n < 0 ? '-' : '';
  const abs = Math.abs(Math.floor(n));
  const mm = String(Math.floor(abs / 60)).padStart(2, '0');
  const ss = String(abs % 60).padStart(2, '0');
  return sign + mm + ':' + ss;
}

function fmtDuration(value) {
  const n = toNum(value);
  if (n === null) {
    return '--:--';
  }
  const abs = Math.abs(Math.floor(n));
  const mm = String(Math.floor(abs / 60)).padStart(2, '0');
  const ss = String(abs % 60).padStart(2, '0');
  return mm + ':' + ss;
}

function renderEntryCountdown(secondsToEntry) {
  const sec = toNum(secondsToEntry);
  if (sec === null) {
    el('entryCountdownLabel').textContent = '距离计划入场';
    el('entryCountdown').textContent = '--:--';
    el('entrySyncAt').textContent = '同步于 --';
    state.countdownSnapshotAtMs = null;
    state.countdownBaseSeconds = null;
    return;
  }
  if (sec >= 0) {
    el('entryCountdownLabel').textContent = '距离计划入场';
    el('entryCountdown').textContent = fmtDuration(sec);
  } else {
    el('entryCountdownLabel').textContent = '已过计划入场';
    el('entryCountdown').textContent = fmtDuration(sec);
  }
  state.countdownSnapshotAtMs = Date.now();
  state.countdownBaseSeconds = sec;
  el('entrySyncAt').textContent = '同步于 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
}

function tickEntryCountdown() {
  if (state.countdownSnapshotAtMs === null || state.countdownBaseSeconds === null) {
    return;
  }
  const elapsed = (Date.now() - state.countdownSnapshotAtMs) / 1000;
  const liveSeconds = state.countdownBaseSeconds - elapsed;
  if (liveSeconds >= 0) {
    el('entryCountdownLabel').textContent = '距离计划入场';
    el('entryCountdown').textContent = fmtDuration(liveSeconds);
  } else {
    el('entryCountdownLabel').textContent = '已过计划入场';
    el('entryCountdown').textContent = fmtDuration(liveSeconds);
  }
}

function sideText(side) {
  if (side === 'UP') return '看涨';
  if (side === 'DOWN') return '看跌';
  if (side === 'SKIP') return '跳过';
  return '待定';
}

function tradeResultText(result) {
  const resultValue = String(result || '').trim().toUpperCase();
  if (!resultValue) return '--';
  if (resultValue === 'UP') return '看涨';
  if (resultValue === 'DOWN') return '看跌';
  if (resultValue === 'PROVISIONAL_LOSS') return '暂记亏损';
  return String(result || '');
}

function resultCheckText(status) {
  if (status === 'match') return '已对官方';
  if (status === 'mismatch') return '与官方不符';
  if (status === 'pending') return '待结算';
  if (status === 'official') return '页面补官方';
  if (status === 'official_pending') return '待官方结算';
  if (status === 'error') return '校验异常';
  return '--';
}

function priceCheckClass(status) {
  if (status === 'above_max') return 'trade-down';
  if (status === 'improved') return 'trade-up';
  return 'trade-skip';
}

function strategyCatalog(payload) {
  return (payload && payload.strategy_catalog) || {};
}

function strategyMeta(payload, strategyId) {
  return strategyCatalog(payload)[String(strategyId || '')] || null;
}

function strategyShortLabel(payload, strategyId) {
  const meta = strategyMeta(payload, strategyId);
  if (meta && meta.label) {
    return meta.label;
  }
  if (STRATEGY_LABELS[String(strategyId || '')]) {
    return STRATEGY_LABELS[String(strategyId || '')];
  }
  return '策略 ' + String(strategyId || '--');
}

function strategyOptionLabel(key, opt, payload) {
  if (key === 'STRATEGY_ID' || key === 'STRATEGY_IDS' || key === 'PAPER_STRATEGY_IDS' || key === 'LIVE_STRATEGY_IDS' || key === 'SIGNAL_FALLBACK_STRATEGY_ID') {
    return String(opt) + ' | ' + strategyShortLabel(payload, opt);
  }
  const optMap = OPTION_LABELS[key] || {};
  return optMap[opt] || opt;
}

function strategyPreviewText(token) {
  if (token === 'UP') return '\u770b\u6da8';
  if (token === 'DOWN') return '\u770b\u8dcc';
  if (token === 'MOMENTUM') return '\u52a8\u91cf\u5224\u65ad';
  if (token === 'THRESHOLD') return '\u9608\u503c\u8fc7\u6ee4';
  if (token === 'FALLBACK') return '\u5f31\u4fe1\u53f7\u8df3\u8fc7';
  if (token === 'SKIP') return '\u5f31\u4fe1\u53f7\u8df3\u8fc7';
  if (token === 'OFI') return '\u76d8\u53e3\u5931\u8861\u5224\u65ad';
  return String(token || '--');
}

function strategyPreviewClass(token) {
  if (token === 'UP') return 'trade-up';
  if (token === 'DOWN') return 'trade-down';
  return 'strategy-info';
}

function renderStrategyPills(tokens) {
  if (!Array.isArray(tokens) || tokens.length === 0) {
    return '<span class="strategy-pill strategy-info">\u6682\u65e0\u8282\u594f\u9884\u89c8</span>';
  }
  return tokens.map((token) => {
    return '<span class="strategy-pill ' + esc(strategyPreviewClass(token)) + '">' + esc(strategyPreviewText(token)) + '</span>';
  }).join('');
}

function parseStrategyIdList(rawValue) {
  return String(rawValue || '')
    .split(',')
    .map((item) => String(item || '').trim())
    .filter((item, index, arr) => item && arr.indexOf(item) === index);
}

function configuredStrategyIdsForMode(mode) {
  const normalizedMode = String(mode || '').toLowerCase() === 'live' ? 'live' : 'paper';
  const payload = state.config || {};
  const envValues = payload.env_values || {};
  const constraints = ((payload.live_health || {}).constraints) || {};
  const draft = currentUnifiedStrategyDraftForReport();
  if (draft) {
    const unified = resolveUnifiedStrategySelection(payload, draft);
    if (unified.multiKey === strategyListKeyForMode(normalizedMode) && unified.selected.length > 0) {
      return unified.selected.slice();
    }
  }
  if (normalizedMode === 'live') {
    const liveIds = parseStrategyIdList(envValues.LIVE_STRATEGY_IDS);
    if (liveIds.length > 0) {
      return liveIds;
    }
    if (Array.isArray(constraints.live_strategy_ids) && constraints.live_strategy_ids.length > 0) {
      return constraints.live_strategy_ids.map((item) => String(item));
    }
    return parseStrategyIdList(envValues.STRATEGY_IDS || envValues.STRATEGY_ID);
  }
  const paperIds = parseStrategyIdList(envValues.PAPER_STRATEGY_IDS);
  if (paperIds.length > 0) {
    return paperIds;
  }
  if (Array.isArray(constraints.paper_strategy_ids) && constraints.paper_strategy_ids.length > 0) {
    return constraints.paper_strategy_ids.map((item) => String(item));
  }
  return parseStrategyIdList(envValues.STRATEGY_IDS || envValues.STRATEGY_ID);
}

function reportStrategyOptionsForMode(mode) {
  return configuredStrategyIdsForMode(mode);
}

function parsePaperTimeframeList(rawValue) {
  return String(rawValue || '')
    .split(',')
    .map((item) => String(item || '').trim().toLowerCase())
    .filter((item, index, arr) => (item === '5m' || item === '15m') && arr.indexOf(item) === index);
}

function effectivePaperTimeframeFilter() {
  const payload = state.config || {};
  const configured = parsePaperTimeframeList((((payload || {}).env_values || {}).PAPER_TIMEFRAMES) || ((payload.paper_timeframes || []).join(',')));
  if (configured.indexOf(String(state.paperTimeframeFilter || '').toLowerCase()) >= 0) {
    return String(state.paperTimeframeFilter).toLowerCase();
  }
  if (configured.length > 0) {
    state.paperTimeframeFilter = configured[0];
    return configured[0];
  }
  const fallback = String((((payload || {}).env_values || {}).MARKET_TIMEFRAME) || '5m').toLowerCase();
  state.paperTimeframeFilter = fallback;
  return fallback;
}

function paperTimeframeLabel(timeframe) {
  const raw = String(timeframe || '').toLowerCase();
  if (raw === '15m') return '15m';
  return '5m';
}

function isPaperProfileConfigKey(key) {
  const raw = String(key || '');
  return raw === 'PAPER_TIMEFRAMES' || /^PAPER_(5M|15M)_/.test(raw);
}

function normalizeTradeMode(value) {
  const normalized = String(value || 'paper').toLowerCase();
  return ['paper', 'live', 'both'].indexOf(normalized) >= 0 ? normalized : 'paper';
}

function configStrategyModeForTradeMode(value) {
  return normalizeTradeMode(value) === 'live' ? 'live' : 'paper';
}

function strategyEditModeForValues(values) {
  const mode = normalizeTradeMode((values || {}).TRADE_MODE);
  if (mode === 'live') {
    state.strategyEditMode = 'live';
    return 'live';
  }
  if (mode === 'both') {
    state.strategyEditMode = state.strategyEditMode === 'live' ? 'live' : 'paper';
    return state.strategyEditMode;
  }
  state.strategyEditMode = 'paper';
  return 'paper';
}

function modeRunsLive(value) {
  const normalized = normalizeTradeMode(value);
  return normalized === 'live' || normalized === 'both';
}

function activeConfigModeFromValues(payload, values) {
  const envValues = (payload && payload.env_values) || {};
  const mergedValues = {
    ...envValues,
    ...(values || {}),
  };
  return strategyEditModeForValues(mergedValues);
}

function strategyListKeyForMode(mode) {
  const normalized = String(mode || '').toLowerCase();
  return normalized === 'live' ? 'LIVE_STRATEGY_IDS' : 'PAPER_STRATEGY_IDS';
}

function strategyListKeysForMode(mode) {
  const normalized = normalizeTradeMode(mode);
  if (normalized === 'live') {
    return ['LIVE_STRATEGY_IDS'];
  }
  if (normalized === 'both') {
    return ['PAPER_STRATEGY_IDS', 'LIVE_STRATEGY_IDS'];
  }
  return ['PAPER_STRATEGY_IDS'];
}

function activeStrategyListKey(payload, values) {
  const mode = activeConfigModeFromValues(payload, values);
  return strategyListKeyForMode(mode);
}

function resolveUnifiedStrategySelection(payload, values) {
  const envValues = (payload && payload.env_values) || {};
  const multiKey = activeStrategyListKey(payload, values);
  const selectOptions = ((payload || {}).select_options || {});
  const options = (selectOptions[multiKey] || selectOptions.STRATEGY_ID || selectOptions.PAPER_STRATEGY_IDS || []).map((item) => String(item));
  const focusRaw = String((values && values.STRATEGY_ID) ?? envValues.STRATEGY_ID ?? options[0] ?? '');
  const splitSelectedRaw = parseStrategyIdList((values && values[multiKey]) ?? envValues[multiKey] ?? '');
  const legacySelectedRaw = parseStrategyIdList((values && values.STRATEGY_IDS) ?? envValues.STRATEGY_IDS ?? '');
  const selectedRaw = splitSelectedRaw.length > 0 ? splitSelectedRaw : legacySelectedRaw;
  const selected = selectedRaw.filter((item) => options.indexOf(item) >= 0);
  const focus = selected.length > 0
    ? (selected.indexOf(focusRaw) >= 0 ? focusRaw : selected[0])
    : (options.indexOf(focusRaw) >= 0 ? focusRaw : (options[0] || ''));
  const mergedSelected = selected.slice();
  if (focus && mergedSelected.indexOf(focus) < 0) {
    mergedSelected.unshift(focus);
  }
  return {
    focus,
    selected: mergedSelected,
    options,
    multiKey,
    mode: strategyListKeyForMode('live') === multiKey ? 'live' : 'paper',
  };
}

function collectUnifiedStrategyValues(payload, currentValues) {
  const unified = resolveUnifiedStrategySelection(payload || state.config || {}, currentValues || {});
  const envValues = (payload && payload.env_values) || {};
  const mergedValues = {
    ...envValues,
    ...(currentValues || {}),
  };
  const selectedCsv = unified.selected.join(',');
  const values = {
    STRATEGY_ID: unified.focus,
    [unified.multiKey]: selectedCsv,
  };
  return values;
}

function currentUnifiedStrategyDraftValues() {
  const payload = state.config || {};
  const envValues = (payload && payload.env_values) || {};
  const focusNode = el('cfg_STRATEGY_ID');
  const multiNode = el('cfg_PAPER_STRATEGY_IDS');
  const multiKey = activeStrategyListKey(payload, {});
  const domStrategyListKey = multiNode ? String(multiNode.dataset.strategyListKey || '') : '';
  const domSelected = multiNode
    ? Array.from(multiNode.options || []).filter((option) => option.selected).map((option) => option.value).join(',')
    : '';
  const selectedValue = domStrategyListKey === multiKey ? domSelected : String(envValues[multiKey] || '');
  const output = {
    STRATEGY_ID: focusNode ? focusNode.value : String(envValues.STRATEGY_ID || ''),
    STRATEGY_IDS: String(envValues.STRATEGY_IDS || ''),
    PAPER_STRATEGY_IDS: String(envValues.PAPER_STRATEGY_IDS || ''),
    LIVE_STRATEGY_IDS: String(envValues.LIVE_STRATEGY_IDS || ''),
  };
  output[multiKey] = selectedValue;
  return output;
}

function refreshStrategyPanelDependentUi() {
  const liveValues = expandLiveToggleValues(collectConfigValues());
  renderStrategyGuide(state.config, liveValues);
  applyConfigFieldVisibility(liveValues);
  renderSharedPaperReportStrategySelector();
}

function renderStrategyPanel(payload, values) {
  const panelNode = el('cfgStrategyPanel');
  if (!panelNode) {
    return;
  }
  const unified = resolveUnifiedStrategySelection(payload, values);
  panelNode.innerHTML = '';

  const activeMode = normalizeTradeMode(((values || {}).TRADE_MODE) || (((payload || {}).env_values || {}).TRADE_MODE) || 'paper');
  if (activeMode === 'both') {
    const modeSwitch = document.createElement('div');
    modeSwitch.className = 'strategy-mode-switch';
    [
      ['paper', '编辑纸面'],
      ['live', '编辑实盘'],
    ].forEach(([mode, label]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-ghost' + (unified.mode === mode ? ' strategy-mode-active' : '');
      button.textContent = label;
      button.addEventListener('click', () => {
        state.strategyEditMode = mode;
        renderUnifiedStrategyToolbar(state.config || {}, currentUnifiedStrategyDraftValues());
        refreshStrategyPanelDependentUi();
      });
      modeSwitch.appendChild(button);
    });
    panelNode.appendChild(modeSwitch);
  }

  const actions = document.createElement('div');
  actions.className = 'strategy-panel-actions';

  const selectAllButton = document.createElement('button');
  selectAllButton.type = 'button';
  selectAllButton.className = 'btn btn-ghost';
  selectAllButton.textContent = '全选';
  selectAllButton.addEventListener('click', selectAllPaperStrategiesInPanel);
  actions.appendChild(selectAllButton);

  const clearButton = document.createElement('button');
  clearButton.type = 'button';
  clearButton.className = 'btn btn-ghost';
  clearButton.textContent = '清空';
  clearButton.addEventListener('click', clearPaperStrategies);
  actions.appendChild(clearButton);

  panelNode.appendChild(actions);

  const summary = document.createElement('div');
  summary.className = 'strategy-panel-summary';
  summary.textContent = activeMode === 'both'
    ? '\u6b63\u5728\u7f16\u8f91' + formatModeLabel(unified.mode) + '\u7b56\u7565\u7ec4\u5408\uff08' + unified.multiKey + '\uff09\uff0c\u53e6\u4e00\u4fa7\u4fdd\u6301\u4e0d\u53d8\u3002'
    : '\u6b63\u5728\u7f16\u8f91' + formatModeLabel(activeConfigModeFromValues(payload, values)) + '\u7b56\u7565\u7ec4\u5408\uff08' + unified.multiKey + '\uff09\u3002';
  panelNode.appendChild(summary);

  const list = document.createElement('div');
  list.className = 'strategy-panel';

  unified.options.forEach((opt) => {
    const meta = strategyMeta(payload, opt) || {};
    const isPrimary = String(unified.focus) === String(opt);
    const isSelected = unified.selected.indexOf(String(opt)) >= 0;

    const row = document.createElement('div');
    row.className = 'strategy-panel-row' + (isPrimary ? ' strategy-panel-row-primary' : '');

    const rowMain = document.createElement('div');
    rowMain.className = 'strategy-panel-row-main';

    const paperToggle = document.createElement('label');
    paperToggle.className = 'strategy-panel-toggle';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = isSelected;
    checkbox.disabled = isPrimary;
    checkbox.addEventListener('change', () => {
      togglePaperStrategySelection(opt, checkbox.checked);
    });
    paperToggle.appendChild(checkbox);
    paperToggle.appendChild(document.createTextNode('运行'));
    rowMain.appendChild(paperToggle);

    const metaWrap = document.createElement('div');
    metaWrap.className = 'strategy-panel-meta';
    const title = document.createElement('div');
    title.className = 'strategy-panel-title';
    title.innerHTML = '<span>' + esc(String(opt) + ' | ' + (meta.label || strategyShortLabel(payload, opt))) + '</span>' +
      (isPrimary ? '<span class="chip ok">主策略</span>' : '');
    metaWrap.appendChild(title);
    rowMain.appendChild(metaWrap);
    row.appendChild(rowMain);

    const primaryToggle = document.createElement('label');
    primaryToggle.className = 'strategy-panel-primary';
    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'cfgPrimaryStrategy';
    radio.checked = isPrimary;
    radio.addEventListener('change', () => {
      if (radio.checked) {
        setPrimaryStrategy(opt);
      }
    });
    primaryToggle.appendChild(radio);
    primaryToggle.appendChild(document.createTextNode('设为主策略'));
    row.appendChild(primaryToggle);

    list.appendChild(row);
  });

  panelNode.appendChild(list);
  const editor = document.createElement('div');
  editor.id = 'strategyProfileEditor';
  editor.className = 'strategy-profile-editor';
  panelNode.appendChild(editor);
}

function renderStrategyProfileEditor(payload, values) {
  const editor = el('strategyProfileEditor');
  if (!editor) {
    return;
  }
  const profilePayload = (payload || {}).strategy_profiles || {};
  const strategies = profilePayload.strategies || {};
  const mode = activeConfigModeFromValues(payload || {}, values || {});
  const unified = resolveUnifiedStrategySelection(payload || {}, values || {});
  const selected = unified.selected.length > 0 ? unified.selected : Object.keys(strategies);
  const cards = selected.map((strategyId) => {
    const profile = strategies[String(strategyId)] || {};
    const fields = (profile.fields || {});
    const fieldHtml = Object.entries(fields).map(([baseKey, field]) => {
      const key = String((field || {}).key || '');
      const value = String((field || {}).value ?? '');
      const inheritedValue = String((field || {}).inherited_value ?? '');
      const explicit = !(field || {}).inherited;
      const label = String((field || {}).label || CONFIG_KEY_NAMES[baseKey] || baseKey);
      const options = Array.isArray((field || {}).options) ? field.options : [];
      const attrs = ' data-strategy-config-key="' + esc(key) + '"' +
        ' data-strategy-config-inherited-value="' + esc(inheritedValue) + '"';
      const control = options.length > 0
        ? ('<select class="input-compact"' + attrs + '>' + options.map((opt) => {
            const selectedAttr = String(opt) === value ? ' selected' : '';
            return '<option value="' + esc(opt) + '"' + selectedAttr + '>' + esc(strategyOptionLabel(baseKey, opt, payload)) + '</option>';
          }).join('') + '</select>')
        : ('<input class="input-compact" type="text" value="' + esc(value) + '"' + attrs + '>');
      const chip = explicit ? '<span class="chip warn">策略专属</span>' : '<span class="chip">默认模板</span>';
      return '<div class="strategy-profile-field">' +
        '<label>' + esc(label) + '</label>' +
        control +
        chip +
        '</div>';
    }).join('');
    return '<section class="strategy-profile-card">' +
      '<div class="strategy-profile-head">' +
        '<div><div class="strategy-profile-title">策略 ' + esc(strategyId) + ' 参数</div>' +
        '<div class="strategy-profile-subtitle">' + esc(profile.label || strategyShortLabel(payload, strategyId)) + '</div></div>' +
        '<button type="button" class="btn btn-ghost" data-reset-strategy="' + esc(strategyId) + '">重置状态</button>' +
      '</div>' +
      '<div class="strategy-profile-grid">' + fieldHtml + '</div>' +
      '</section>';
  }).join('');
  editor.innerHTML = '<div class="strategy-profile-title">策略参数</div>' +
    '<div class="strategy-profile-subtitle">' + esc(formatModeLabel(mode)) + ' · 全局参数管安全边界，策略参数管下注与信号行为</div>' +
    (cards || '<div class="empty">暂无可配置策略</div>');
  editor.querySelectorAll('[data-reset-strategy]').forEach((node) => {
    node.addEventListener('click', () => {
      resetStrategyState(node.getAttribute('data-reset-strategy') || '');
    });
  });
}

function selectAllPaperStrategiesInPanel() {
  const draft = currentUnifiedStrategyDraftValues();
  const unified = resolveUnifiedStrategySelection(state.config || {}, draft);
  renderUnifiedStrategyToolbar(state.config || {}, {
    ...draft,
    [unified.multiKey]: unified.options.join(','),
  });
  refreshStrategyPanelDependentUi();
}

function clearPaperStrategies() {
  const draft = currentUnifiedStrategyDraftValues();
  const multiKey = activeStrategyListKey(state.config || {}, draft);
  renderUnifiedStrategyToolbar(state.config || {}, {
    ...draft,
    [multiKey]: '',
  });
  refreshStrategyPanelDependentUi();
}

function togglePaperStrategySelection(strategyId, selected) {
  const draft = currentUnifiedStrategyDraftValues();
  const multiKey = activeStrategyListKey(state.config || {}, draft);
  const values = parseStrategyIdList(draft[multiKey]);
  const target = String(strategyId);
  const nextSelected = selected
    ? [...values, target]
    : values.filter((item) => String(item) !== target);
  renderUnifiedStrategyToolbar(state.config || {}, {
    ...draft,
    [multiKey]: nextSelected.join(','),
  });
  refreshStrategyPanelDependentUi();
}

function setPrimaryStrategy(strategyId) {
  const draft = currentUnifiedStrategyDraftValues();
  renderUnifiedStrategyToolbar(state.config || {}, {
    ...draft,
    STRATEGY_ID: String(strategyId || ''),
  });
  refreshStrategyPanelDependentUi();
}

function renderUnifiedStrategyToolbar(payload, values) {
  const focusNode = el('cfg_STRATEGY_ID');
  const multiNode = el('cfg_PAPER_STRATEGY_IDS');
  if (!focusNode || !multiNode) {
    return;
  }
  const unified = resolveUnifiedStrategySelection(payload, values);
  const multiKey = unified.multiKey;
  const focusStrategy = unified.focus || 'all';
  focusNode.innerHTML = '';
  unified.options.forEach((opt) => {
    const option = document.createElement('option');
    option.value = opt;
    option.textContent = strategyOptionLabel('STRATEGY_ID', opt, payload);
    option.selected = String(unified.focus) === String(opt);
    focusNode.appendChild(option);
  });
  multiNode.innerHTML = '';
  multiNode.multiple = true;
  multiNode.size = Math.max(4, unified.options.length);
  multiNode.dataset.strategyListKey = multiKey;
  unified.options.forEach((opt) => {
    const option = document.createElement('option');
    option.value = opt;
    option.textContent = strategyOptionLabel(multiKey, opt, payload);
    option.selected = unified.selected.indexOf(String(opt)) >= 0;
    multiNode.appendChild(option);
  });
  renderStrategyPanel(payload, values);
  renderStrategyProfileEditor(payload, values);
  state.marketStrategyFilter = focusStrategy;
}

function currentUnifiedStrategyDraftForReport() {
  const focusNode = el('cfg_STRATEGY_ID');
  const multiNode = el('cfg_PAPER_STRATEGY_IDS');
  if (!focusNode || !multiNode) {
    return null;
  }
  return currentUnifiedStrategyDraftValues();
}

function effectiveReportMode() {
  return state.reportMode === 'live' ? 'live' : 'paper';
}

function syncReportModeWithRuntime(payload) {
  if (state.reportModeUserSelected) {
    return;
  }
  const runtime = (payload && payload.runtime_status) || {};
  const runtimeMode = String(runtime.desired_mode || runtime.saved_mode || runtime.active_mode || runtime.running_mode || '').toLowerCase();
  if (runtimeMode !== 'live' && runtimeMode !== 'paper') {
    return;
  }
  state.reportMode = runtimeMode === 'live' ? 'live' : 'paper';
}

function normalizePaperReportStrategyFilter(value, options) {
  const raw = String(value || 'all');
  return options.indexOf(raw) >= 0 ? raw : 'all';
}

function paperReportStrategyOptions() {
  const configured = reportStrategyOptionsForMode(effectiveReportMode());
  return ['all', ...configured];
}

function defaultPaperReportStrategyFilter() {
  const configured = reportStrategyOptionsForMode(effectiveReportMode());
  return configured.length === 1 ? String(configured[0]) : 'all';
}

function effectivePaperReportStrategyFilter() {
  const current = String(state.paperReportStrategyFilter || '');
  if (!current) {
    return defaultPaperReportStrategyFilter();
  }
  return current;
}

function effectivePaperSummaryStrategyFilter() {
  if (state.paperSummaryStrategyFilter !== null && state.paperSummaryStrategyFilter !== undefined && state.paperSummaryStrategyFilter !== '') {
    return String(state.paperSummaryStrategyFilter);
  }
  return effectivePaperReportStrategyFilter();
}

function effectivePaperRecentStrategyFilter() {
  if (state.paperRecentStrategyFilter !== null && state.paperRecentStrategyFilter !== undefined && state.paperRecentStrategyFilter !== '') {
    return String(state.paperRecentStrategyFilter);
  }
  return effectivePaperReportStrategyFilter();
}

function recentStrategyHeaderText() {
  const timeframe = effectivePaperTimeframeFilter();
  const strategy = effectivePaperRecentStrategyFilter();
  const reportMode = effectiveReportMode();
  const modeText = reportMode === 'live' ? '\u5b9e\u76d8' : '\u7eb8\u9762';
  const timeframeText = reportMode === 'live' ? '' : (' \u00b7 \u5f53\u524d\u9891\u6b21\uff1a' + timeframe);
  if (!strategy || strategy === 'all') {
    return '\u6309\u8f6e\u6b21\u5012\u5e8f\u663e\u793a\u6700\u8fd1 80 \u6761\u8bb0\u5f55 \u00b7 \u5f53\u524d\u6a21\u5f0f\uff1a' + modeText + timeframeText + ' \u00b7 \u5f53\u524d\u7b56\u7565\uff1a\u5168\u90e8';
  }
  return '\u6309\u8f6e\u6b21\u5012\u5e8f\u663e\u793a\u6700\u8fd1 80 \u6761\u8bb0\u5f55 \u00b7 \u5f53\u524d\u6a21\u5f0f\uff1a' + modeText + timeframeText + ' \u00b7 \u5f53\u524d\u7b56\u7565\uff1a\u7b56\u7565 ' + strategy;
  if (!strategy || strategy === 'all') {
    return '按轮次倒序显示最近 80 条记录 · 当前频次：' + timeframe + ' · 当前策略：全部';
  }
  return '按轮次倒序显示最近 80 条记录 · 当前频次：' + timeframe + ' · 当前策略：策略 ' + strategy;
}

function renderReportModeCopy() {
  const reportMode = effectiveReportMode();
  const title = reportMode === 'live' ? '实盘交易汇总' : '纸面交易汇总';
  const desc = '策略筛选同时作用于' + title + '与最近交易明细';
  const titleNode = el('reportSummaryTitle');
  const descNode = el('reportCardDesc');
  if (titleNode) {
    titleNode.textContent = title;
  }
  if (descNode) {
    descNode.textContent = desc;
  }
}

function renderSharedPaperReportStrategySelector() {
  renderReportModeCopy();
  const modeNode = el('reportModeSelect');
  if (modeNode) {
    modeNode.value = effectiveReportMode();
    modeNode.onchange = async () => {
      state.reportMode = modeNode.value === 'live' ? 'live' : 'paper';
      state.reportModeUserSelected = true;
      saveUiPrefs();
      state.paperSummaryStrategyFilter = '';
      state.paperRecentStrategyFilter = '';
      renderSharedPaperReportStrategySelector();
      await Promise.allSettled([refreshSummary(), refreshRecent()]);
    };
  }
  const options = paperReportStrategyOptions();
  const current = normalizePaperReportStrategyFilter(effectivePaperReportStrategyFilter(), options);
  const summaryCurrent = normalizePaperReportStrategyFilter(effectivePaperSummaryStrategyFilter(), options);
  const recentCurrent = normalizePaperReportStrategyFilter(effectivePaperRecentStrategyFilter(), options);
  state.paperReportStrategyFilter = current;
  if (state.paperSummaryStrategyFilter !== null && state.paperSummaryStrategyFilter !== undefined && state.paperSummaryStrategyFilter !== '') {
    state.paperSummaryStrategyFilter = summaryCurrent;
  }
  if (state.paperRecentStrategyFilter !== null && state.paperRecentStrategyFilter !== undefined && state.paperRecentStrategyFilter !== '') {
    state.paperRecentStrategyFilter = recentCurrent;
  }
  const node = el('paperReportStrategy');
  el('recentPanelDesc').textContent = recentStrategyHeaderText();
  if (!node) {
    return;
  }
  node.innerHTML = '';
  options.forEach((value) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value === 'all' ? '查看全部策略' : ('查看策略 ' + value);
    option.selected = value === current;
    node.appendChild(option);
  });
  node.onchange = async () => {
    state.paperReportStrategyFilter = node.value || 'all';
    state.paperSummaryStrategyFilter = '';
    state.paperRecentStrategyFilter = '';
    renderSharedPaperReportStrategySelector();
    await Promise.allSettled([refreshSummary(), refreshRecent()]);
  };

  const summaryNode = el('paperSummaryStrategy');
  if (summaryNode) {
    summaryNode.value = summaryCurrent;
    summaryNode.onchange = async () => {
      state.paperSummaryStrategyFilter = summaryNode.value || 'all';
      await refreshSummary();
    };
  }

  const recentNode = el('recentTradesStrategy');
  if (recentNode) {
    recentNode.value = recentCurrent;
    recentNode.onchange = async () => {
      state.paperRecentStrategyFilter = recentNode.value || 'all';
      await refreshRecent();
    };
  }
}

function renderStrategyGuide(payload, values) {
  const node = el('strategyGuideCard');
  if (!node) {
    return;
  }

  const currentValues = values || {};
  const unified = resolveUnifiedStrategySelection(payload, currentValues);
  const strategyId = String(unified.focus || '');
  const modeLabel = formatModeLabel(activeConfigModeFromValues(payload, currentValues));
  const meta = strategyMeta(payload, strategyId);
  if (!meta) {
    node.innerHTML = '<div class="empty">\u6682\u65e0\u7b56\u7565\u8bf4\u660e</div>';
    return;
  }

  let extra = '';
  if (strategyId === '5') {
    const envValues = (payload && payload.env_values) || {};
    const weakModeRaw = String(currentValues.SIGNAL_WEAK_SIGNAL_MODE ?? envValues.SIGNAL_WEAK_SIGNAL_MODE ?? '--');
    const weakModeText = (OPTION_LABELS.SIGNAL_WEAK_SIGNAL_MODE || {})[weakModeRaw] || weakModeRaw;
    extra = '<div class="strategy-guide-note">\u5f31\u4fe1\u53f7\u5904\u7406\uff1a' + esc(weakModeText) + '</div>';
  }

  node.innerHTML =
    '<div class="strategy-guide-head">' +
      '<div>' +
        '<div class="strategy-guide-title">' + esc(strategyId + ' | ' + meta.label) + '</div>' +
        '<div class="strategy-guide-subtitle">' + esc(meta.summary || '') + '</div>' +
      '</div>' +
      '<span class="chip ok">\u7edf\u4e00\u7b56\u7565</span>' +
    '</div>' +
    '<div class="strategy-guide-preview">' + renderStrategyPills(meta.preview || []) + '</div>' +
    '<div class="strategy-guide-note">\u67e5\u770b\u7b56\u7565\uff1a' + esc(strategyId) + '\uff1b' + esc(modeLabel) + '\u8fd0\u884c\uff1a' + esc(unified.selected.join(',') || '--') + '</div>' +
    '<div class="strategy-guide-note">' + esc(meta.detail || '') + '</div>' +
    extra;
}

function renderPaperProfiles(payload) {
  const node = el('paperProfilesRoot');
  if (!node) {
    return;
  }
  node.innerHTML = '';
}

function paperRuntimeCardsFromConfig(payload) {
  const timeframes = Array.isArray((payload || {}).paper_timeframes) ? payload.paper_timeframes : [];
  const runtime = ((payload || {}).runtime_status) || {};
  return timeframes.map((timeframe) => ({
    timeframe,
    active_mode: runtime.active_mode || runtime.running_mode || 'paper',
    desired_mode: runtime.desired_mode || runtime.saved_mode || 'paper',
    switch_state: runtime.switch_state || 'idle',
  }));
}

function renderPaperRuntimeCards(payload) {
  const node = el('paperRuntimeCards');
  if (!node) {
    return;
  }
  const cards = paperRuntimeCardsFromConfig(payload || {});
  if (!cards.length) {
    node.innerHTML = '';
    return;
  }
  const activeTimeframe = effectivePaperTimeframeFilter();
  node.innerHTML = cards.map((card) => {
    const detail = (state.paperRuntimeCards || {})[card.timeframe] || {};
    const roundSlug = (((detail || {}).round || {}).slug) || '--';
    const shouldTrade = (((detail || {}).plan || {}).should_trade);
    const selectedCls = String(card.timeframe) === String(activeTimeframe) ? ' strategy-panel-row-primary' : '';
    return ''
      + '<section class="strategy-guide-card' + selectedCls + '" data-paper-runtime-card="' + esc(card.timeframe) + '">'
      +   '<div class="strategy-guide-head">'
      +     '<div>'
      +       '<div class="strategy-guide-title">' + esc(paperTimeframeLabel(card.timeframe)) + ' 纸面运行</div>'
      +       '<div class="strategy-guide-subtitle">该时间频次的纸面运行状态</div>'
      +     '</div>'
      +     '<span class="chip ok">' + esc(formatModeLabel(card.active_mode || 'paper')) + '</span>'
      +   '</div>'
      +   '<div class="rows">'
      +     '<div class="row"><span class="label">目标模式</span><span class="value">' + esc(formatModeLabel(card.desired_mode || 'paper')) + '</span></div>'
      +     '<div class="row"><span class="label">切换状态</span><span class="value">' + esc(card.switch_state || '--') + '</span></div>'
      +     '<div class="row"><span class="label">当前轮次</span><span class="value">' + esc(roundSlug) + '</span></div>'
      +     '<div class="row"><span class="label">计划下单</span><span class="value">' + esc(shouldTrade === undefined ? '--' : (shouldTrade ? '是' : '否')) + '</span></div>'
      +   '</div>'
      + '</section>';
  }).join('');
  node.querySelectorAll('[data-paper-runtime-card]').forEach((cardNode) => {
    cardNode.addEventListener('click', async () => {
      state.paperTimeframeFilter = String(cardNode.getAttribute('data-paper-runtime-card') || '').toLowerCase();
      renderPaperRuntimeCards(state.config || {});
      await Promise.allSettled([refreshMarket(), refreshSummary(), refreshRecent()]);
    });
  });
}

async function refreshPaperRuntimeCard(timeframe) {
  const payload = state.config || {};
  const targetTimeframe = String(timeframe || '').toLowerCase();
  if (!targetTimeframe) {
    return;
  }
  const profiles = payload.paper_profiles || {};
  const profile = profiles[targetTimeframe] || {};
  const strategy = encodeURIComponent(String(profile.strategy_id || 'all'));
  const data = await apiGet('/api/market?strategy=' + strategy + '&timeframe=' + encodeURIComponent(targetTimeframe));
  state.paperRuntimeCards[targetTimeframe] = data;
  renderPaperRuntimeCards(payload);
}

async function refreshPaperRuntimeCards() {
  const payload = state.config || {};
  const timeframes = Array.isArray(payload.paper_timeframes) ? payload.paper_timeframes : [];
  if (!timeframes.length) {
    state.paperRuntimeCards = {};
    renderPaperRuntimeCards(payload);
    return;
  }
  await Promise.allSettled(timeframes.map((timeframe) => refreshPaperRuntimeCard(timeframe)));
}

function applyConfigFieldVisibility(values) {
  const strategyId = String(resolveUnifiedStrategySelection(state.config || {}, values || {}).focus || '');
  const isStrategyFive = strategyId === '5';

  document.querySelectorAll('.field[data-field-scope]').forEach((node) => {
    const scope = node.getAttribute('data-field-scope') || 'all';
    const shouldMute = scope === 'strategy_5_only' && !isStrategyFive;
    node.classList.toggle('field-muted', shouldMute);
    const note = node.querySelector('.field-scope-note');
    if (note) {
      note.textContent = shouldMute ? '\u5f53\u524d\u67e5\u770b\u7b56\u7565\u672a\u4f7f\u7528\u6b64\u53c2\u6570\uff0c\u4ec5\u7b56\u7565 5 \u4f7f\u7528' : '';
    }
  });

  document.querySelectorAll('.config-group[data-group-scope]').forEach((node) => {
    const scope = node.getAttribute('data-group-scope') || 'all';
    const shouldMute = scope === 'strategy_5_only' && !isStrategyFive;
    node.classList.toggle('config-group-muted', shouldMute);
  });
}

function isAdvancedConfigGroup(group) {
  const title = String((group || {}).title || '');
  return title === '运行模式' || title === '动量信号' || title === '实时连接保护';
}

function isAdvancedConfigKey(key) {
  const normalized = String(key || '');
  return normalized.startsWith('STRATEGY7_') || normalized.startsWith('STRATEGY9_');
}

function applyAdvancedConfigVisibility(values) {
  const panel = el('advancedConfigPanel');
  if (!panel) {
    return;
  }
  const strategyId = String(resolveUnifiedStrategySelection(state.config || {}, values || {}).focus || '');
  const isStrategyFive = strategyId === '5';
  const isStrategySeven = strategyId === '7' || strategyId === '8' || strategyId === '9' || strategyId === '10';
  const isStrategyNine = strategyId === '9';
  const isStrategyTen = strategyId === '10';

  panel.querySelectorAll('.config-group').forEach((section) => {
    const advancedGroup = section.dataset.advancedGroup === 'true';
    section.style.display = advancedGroup && panel.hidden ? 'none' : '';
  });

  panel.querySelectorAll('.field[data-advanced-field]').forEach((field) => {
    const scope = field.dataset.fieldScope || 'all';
    const shouldShow =
      scope === 'strategy_5_only' ? isStrategyFive
      : scope === 'strategy_7_only' ? isStrategySeven
      : scope === 'strategy_9_only' ? isStrategyNine
      : scope === 'strategy_10_only' ? isStrategyTen
      : true;
    field.style.display = panel.hidden ? 'none' : (shouldShow ? '' : 'none');
  });
}
function sourceText(source) {
  if (!source) {
    return '--';
  }
  const normalized = String(source).toLowerCase();
  if (normalized === 'websocket') {
    return '实时连接';
  }
  if (normalized === 'http') {
    return '接口回退';
  }
  return String(source);
}

function marketDeadlineText(value) {
  const formatted = fmtIso(value);
  if (!formatted || formatted === "--") {
    return "结束时间 --";
  }
  return "结束时间 " + formatted;
}

function marketTitleText(title) {
  if (!title) {
    return '--';
  }
  const raw = String(title).trim();
  const m = raw.match(/^Bitcoin Up or Down\\s*-\\s*(.+)\\s+ET$/i);
  if (m) {
    const timeRaw = m[1].trim();
    const t = timeRaw.match(/^([A-Za-z]+)\\s+(\\d{1,2}),\\s*(\\d{1,2}:\\d{2})(AM|PM)\\s*-\\s*(\\d{1,2}:\\d{2})(AM|PM)$/i);
    if (t) {
      const monthMap = {
        january: '1月',
        february: '2月',
        march: '3月',
        april: '4月',
        may: '5月',
        june: '6月',
        july: '7月',
        august: '8月',
        september: '9月',
        october: '10月',
        november: '11月',
        december: '12月',
      };
      const monthCn = monthMap[String(t[1]).toLowerCase()] || t[1];
      const day = String(Number(t[2]));

      const to24h = (hhmm, ampm) => {
        const [hRaw, mRaw] = hhmm.split(':');
        let h = Number(hRaw);
        const m = Number(mRaw);
        const isPM = String(ampm).toUpperCase() === 'PM';
        if (isPM && h !== 12) {
          h += 12;
        }
        if (!isPM && h === 12) {
          h = 0;
        }
        return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
      };

      const start = to24h(t[3], t[4]);
      const end = to24h(t[5], t[6]);
      return '比特币涨跌（美东时间 ' + monthCn + day + '日 ' + start + '-' + end + '）';
    }
    return '比特币涨跌（美东时间 ' + timeRaw + '）';
  }
  return raw;
}

function sideClass(side) {
  if (side === 'UP') return 'trade-up';
  if (side === 'DOWN') return 'trade-down';
  return 'trade-skip';
}

const RUNTIME_LABELS = {
  ws_enabled: '实时连接开关',
  ws_available: '实时连接可用',
  ws_connected: '实时连接状态',
  ws_connect_attempts: '连接尝试次数',
  ws_reconnect_count: '重连次数',
  ws_invalid_operation_count: '异常操作次数',
  ws_subscribed_asset_count: '已订阅资产数',
  ws_cached_asset_count: '缓存资产数',
  ws_opened_at: '建连时间',
  ws_last_message_at: '最近消息时间',
  ws_last_message_age_seconds: '消息延迟(秒)',
  ws_current_error: '当前错误',
  ws_last_error: '最近历史错误',
  reconnects: '重连次数',
  invalid_ops: '异常操作次数',
  connect_attempts: '连接尝试次数',
  subscribed_assets: '已订阅资产数',
  cached_assets: '缓存资产数',
  last_message_age_s: '消息延迟(秒)',
  current_error: '当前错误',
  last_error: '最近历史错误',
};

const STATUS_LABELS = {
  true: '是',
  false: '否',
};

function classifyPnl(value) {
  const n = toNum(value);
  if (n === null) return '';
  if (n > 0) return 'pnl-plus';
  if (n < 0) return 'pnl-minus';
  return '';
}

function setChip(id, text, kind = '') {
  const node = el(id);
  if (!node) {
    return;
  }
  node.textContent = text;
  node.className = 'chip';
  if (kind) {
    node.classList.add(kind);
  }
}

function setText(id, text) {
  const node = el(id);
  if (node) {
    node.textContent = text;
  }
}

function setHtml(id, html) {
  const node = el(id);
  if (node) {
    node.innerHTML = html;
  }
}

function setDisplay(id, display) {
  const node = el(id);
  if (node) {
    node.style.display = display;
  }
}

function setReportStatus(id, prefix, text, tone) {
  setChip(id, prefix + ': ' + text, tone);
}

function nextReportRequestSeq(key) {
  const stateKey = key + 'RequestSeq';
  const nextSeq = Number(state[stateKey] || 0) + 1;
  state[stateKey] = nextSeq;
  return nextSeq;
}

function isCurrentReportRequest(key, seq) {
  return Number(state[key + 'RequestSeq'] || 0) === Number(seq);
}

async function apiGet(path) {
  const resp = await fetch(path, { cache: 'no-store' });
  const data = await resp.json();
  if (!resp.ok) {
    throw buildApiError(data, resp.status);
  }
  return data;
}

function buildApiError(data, status) {
  const err = new Error((data && data.error) || ('HTTP ' + status));
  err.status = status;
  err.fieldErrors = (data && data.field_errors) || {};
  return err;
}

function setConfigError(message) {
  const node = el('cfgError');
  if (!node) {
    return;
  }
  node.textContent = message || '--';
}

let saveButtonResetTimer = null;
let savedAtFlashTimer = null;

function setSaveButtonState(state) {
  const button = el('btnSaveConfig');
  if (!button) {
    return;
  }
  if (saveButtonResetTimer) {
    clearTimeout(saveButtonResetTimer);
    saveButtonResetTimer = null;
  }
  button.disabled = state === 'saving';
  if (state === 'saving') {
    button.textContent = '\u4fdd\u5b58\u4e2d...';
    return;
  }
  if (state === 'saved') {
    button.textContent = '\u5df2\u4fdd\u5b58';
    saveButtonResetTimer = setTimeout(() => {
      button.textContent = '\u4fdd\u5b58\u53c2\u6570';
      button.disabled = false;
    }, 1800);
    return;
  }
  if (state === 'error') {
    button.textContent = '\u4fdd\u5b58\u5931\u8d25';
    saveButtonResetTimer = setTimeout(() => {
      button.textContent = '\u4fdd\u5b58\u53c2\u6570';
      button.disabled = false;
    }, 2200);
    return;
  }
  button.textContent = '\u4fdd\u5b58\u53c2\u6570';
  button.disabled = false;
}

function flashSavedAt() {
  const node = el('cfgSavedAt');
  if (!node) {
    return;
  }
  if (savedAtFlashTimer) {
    clearTimeout(savedAtFlashTimer);
    savedAtFlashTimer = null;
  }
  node.classList.add('flash-saved');
  savedAtFlashTimer = setTimeout(() => {
    node.classList.remove('flash-saved');
  }, 1800);
}

async function apiPost(path, payload) {
  const resp = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await resp.json();
  if (!resp.ok) {
    throw buildApiError(data, resp.status);
  }
  return data;
}

async function resetStrategyState(strategyId) {
  const strategyText = String(strategyId || '').trim();
  if (!strategyText) {
    return;
  }
  const mode = effectiveConfigMode(state.config || {});
  if (!window.confirm('确认重置策略 ' + strategyText + ' 的下注状态吗？')) {
    return;
  }
  try {
    setChip('cfgStatus', '重置中', 'warn');
    const data = await apiPost('/api/strategy/reset', {
      mode,
      strategy_id: strategyText,
      timeframe: effectivePaperTimeframeFilter(),
    });
    renderConfig(data);
    await Promise.allSettled([refreshMarket(), refreshSummary(), refreshRecent()]);
    setChip('cfgStatus', '已重置策略 ' + strategyText, 'ok');
  } catch (err) {
    setChip('cfgStatus', '重置失败', 'err');
    setConfigError(err && err.message ? err.message : '重置失败');
    console.error(err);
  }
}

function renderHelpSectionList(section) {
  return (section.sections || []).map((group) => {
    const items = (group.bullets || []).map((item) => '<li>' + esc(item) + '</li>').join('');
    return '<section class="help-section"><h3>' + esc(group.title || '') + '</h3><ul>' + items + '</ul></section>';
  }).join('');
}

function renderHelpQuickStart() {
  const section = HELP_SECTIONS.quickstart;
  return '<div class="help-intro">' + esc(section.intro || '') + '</div>' + renderHelpSectionList(section);
}

function renderHelpPageGuide() {
  return renderHelpSectionList(HELP_SECTIONS.pageguide);
}

function renderHelpRiskGuide() {
  return renderHelpSectionList(HELP_SECTIONS.riskguide);
}

function renderHelpConfigDictionary() {
  const payload = state.config || {};
  const groups = payload.field_groups || [];
  const help = payload.field_help || {};
  const scope = payload.field_scope || {};
  const labels = payload.labels || {};
  const helpGroups = typeof displayFieldGroups !== "undefined" && displayFieldGroups.length > 0 ? displayFieldGroups : groups;

  return helpGroups.map((group) => {
    const items = (group.keys || []).map((key) => {
      const scopeNote = scope[key] === 'strategy_5_only' ? '仅策略 5 重点使用' : '所有策略都可参考';
      return '<li>' +
        '<strong>' + esc(formatConfigLabel(key, labels)) + '</strong>' +
        '<div class="help-item-subkey">' + esc(key) + '</div>' +
        '<div>' + esc(help[key] || '暂无说明') + '</div>' +
        '<div class="help-item-scope">' + esc(scopeNote) + '</div>' +
        '</li>';
    }).join('');
    return '<section class="help-section">' +
      '<h3>' + esc(group.title || '参数分组') + '</h3>' +
      '<ul class="help-detail-list">' + items + '</ul>' +
      '</section>';
  }).join('');
}

function renderHelpStrategyGuide() {
  const payload = state.config || {};
  const envValues = payload.env_values || {};
  const activeId = String(resolveUnifiedStrategySelection(payload, envValues).focus || '');
  const catalog = payload.strategy_catalog || {};

  return Object.entries(catalog).map(([strategyId, meta]) => {
    const activeCls = strategyId === activeId ? ' help-strategy-card-active' : '';
    const preview = renderStrategyPills(meta.preview || []);
    let extra = '';
    if (strategyId === '5') {
      const weakModeRaw = String(envValues.SIGNAL_WEAK_SIGNAL_MODE || '--');
      const weakModeText = (OPTION_LABELS.SIGNAL_WEAK_SIGNAL_MODE || {})[weakModeRaw] || weakModeRaw;
      extra = '<div class="help-strategy-extra">' +
        '弱信号模式：' + esc(weakModeText) +
        '</div>';
    }
    return '<section class="help-strategy-card' + activeCls + '">' +
      '<h3>' + esc(strategyId + ' | ' + (meta.label || '')) + '</h3>' +
      '<div class="help-strategy-summary">' + esc(meta.summary || '') + '</div>' +
      '<div class="help-strategy-preview">' + preview + '</div>' +
      '<div class="help-strategy-detail">' + esc(meta.detail || '') + '</div>' +
      extra +
      '</section>';
  }).join('');
}

function renderHelpFaq() {
  return HELP_FAQ.map(([question, answer]) => {
    return '<section class="help-section">' +
      '<h3>' + esc(question) + '</h3>' +
      '<p>' + esc(answer) + '</p>' +
      '</section>';
  }).join('');
}

function renderHelpDrawer() {
  const backdrop = el('helpBackdrop');
  const drawer = el('helpDrawer');
  const tabs = el('helpTabs');
  const body = el('helpBody');
  const footer = el('helpFooter');
  if (!backdrop || !drawer || !tabs || !body || !footer) {
    return;
  }

  backdrop.classList.toggle('open', state.helpOpen);
  drawer.classList.toggle('open', state.helpOpen);
  drawer.setAttribute('aria-hidden', state.helpOpen ? 'false' : 'true');

  tabs.innerHTML = HELP_TABS.map((tab) => {
    const active = tab.id === state.helpTab ? ' help-tab-active' : '';
    return '<button class="help-tab' + active + '" data-help-tab="' + esc(tab.id) + '" type="button">' + esc(tab.label) + '</button>';
  }).join('');

  if (state.helpTab === 'quickstart') {
    body.innerHTML = renderHelpQuickStart();
  } else if (state.helpTab === 'pageguide') {
    body.innerHTML = renderHelpPageGuide();
  } else if (state.helpTab === 'configdict') {
    body.innerHTML = renderHelpConfigDictionary();
  } else if (state.helpTab === 'riskguide') {
    body.innerHTML = renderHelpRiskGuide();
  } else if (state.helpTab === 'strategyguide') {
    body.innerHTML = renderHelpStrategyGuide();
  } else {
    body.innerHTML = renderHelpFaq();
  }
  footer.innerHTML =
    '<a href="docs/dashboard_runbook.md" target="_blank" rel="noreferrer">监控面板操作说明</a>' +
    '<a href="docs/operations_runbook.md" target="_blank" rel="noreferrer">运行操作手册</a>' +
    '<a href="docs/daily_ops_checklist.md" target="_blank" rel="noreferrer">日常检查清单</a>';

  tabs.querySelectorAll('[data-help-tab]').forEach((node) => {
    node.addEventListener('click', () => {
      state.helpTab = node.getAttribute('data-help-tab') || 'quickstart';
      renderHelpDrawer();
    });
  });
}

function formatModeLabel(value) {
  const normalized = String(value || 'paper').toLowerCase();
  return OPTION_LABELS.TRADE_MODE[normalized] || normalized;
}

function isSingleLiveToggleKey(key) {
  return key === 'TRADE_MODE' || key === 'LIVE_TRADING_ENABLED';
}

function isCompactConfigField(key) {
  return [
    'BASE_ORDER_COST',
    'MIN_STAKE',
    'PAPER_SIMULATED_WALLET_BALANCE',
    'MAX_CONSECUTIVE_LOSSES',
    'MAX_STAKE',
    'MIN_ENTRY_PRICE',
    'MAX_ENTRY_PRICE',
    'MAX_PRICE_THRESHOLD',
    'SIGNAL_MOMENTUM_THRESHOLD',
    'SIGNAL_FALLBACK_STRATEGY_ID',
    'SIGNAL_HISTORY_FIDELITY_SECONDS',
    'SIGNAL_ANCHOR_MAX_OFFSET_SECONDS',
    'SIGNAL_DYNAMIC_THRESHOLD_K',
    'SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS',
    'SIGNAL_LOCK_BEFORE_ENTRY_SECONDS',
    'MAX_STAKE_SKIP_ALERT_THRESHOLD',
    'WS_QUOTE_STALE_SECONDS',
    'WS_TRADE_GUARD_STALE_SECONDS',
    'WS_CONNECT_TIMEOUT_SECONDS',
    'STRATEGY11_CONFIRM_BEFORE_ENTRY_SECONDS'
  ].indexOf(String(key || '')) >= 0;
}

function buildLiveToggleValue(values) {
  const enabled = String(values.LIVE_TRADING_ENABLED || 'false').toLowerCase();
  return enabled === 'true' ? 'true' : 'false';
}

function effectiveConfigMode(payload) {
  const envValues = (payload && payload.env_values) || {};
  return normalizeTradeMode(envValues.TRADE_MODE || 'paper');
}

function renderTaskflowVisibility(mode) {
  const normalizedMode = normalizeTradeMode(mode);
  const paperRoot = el('paperTaskflowRoot');
  const liveRoot = el('liveTaskflowRoot');
  if (paperRoot) {
    paperRoot.hidden = normalizedMode === 'live';
  }
  if (liveRoot) {
    liveRoot.hidden = normalizedMode === 'paper';
  }
}

function renderConfigModeShell(payload) {
  const selectNode = el('configModeSelect');
  const summaryNode = el('configContextSummary');
  if (!selectNode || !summaryNode) {
    return;
  }
  const runtime = (payload && payload.runtime_status) || {};
  const currentMode = effectiveConfigMode(payload);
  selectNode.value = currentMode;
  summaryNode.textContent =
    '当前按' + formatModeLabel(currentMode) +
    '配置展示 / 运行中 ' + formatModeLabel(runtime.active_mode || runtime.running_mode || 'paper') +
    ' / ' + (runtime.restart_required ? '切换待生效' : '无需切换');
  renderTaskflowVisibility(currentMode);
  if (selectNode.dataset.bound === 'true') {
    return;
  }
  selectNode.dataset.bound = 'true';
  selectNode.addEventListener('change', () => {
    const nextMode = normalizeTradeMode(selectNode.value || 'paper');
    const modeField = el('cfg_TRADE_MODE');
    if (modeField) {
      modeField.value = nextMode;
    }
    const liveToggleField = el('cfg_ENABLE_LIVE_TRADING');
    if (liveToggleField) {
      liveToggleField.value = modeRunsLive(nextMode) ? 'true' : 'false';
    }
    state.config = {
      ...(state.config || {}),
      env_values: {
        ...(((state.config || {}).env_values) || {}),
        TRADE_MODE: nextMode,
        LIVE_TRADING_ENABLED: modeRunsLive(nextMode) ? 'true' : 'false',
      },
    };
    const form = el('configForm');
    if (form && typeof form.oninput === 'function') {
      form.oninput();
    }
    renderUnifiedStrategyToolbar(state.config || {}, currentUnifiedStrategyDraftValues());
    renderSharedPaperReportStrategySelector();
    void Promise.allSettled([refreshSummary(), refreshRecent()]);
    renderConfigModeShell(state.config || {});
  });
}

function expandLiveToggleValues(values) {
  const expanded = { ...values };
  if (!expanded.TRADE_MODE) {
    const modeSelect = el('configModeSelect');
    if (modeSelect) {
      expanded.TRADE_MODE = normalizeTradeMode(modeSelect.value);
    }
  }
  if (!Object.prototype.hasOwnProperty.call(expanded, 'ENABLE_LIVE_TRADING')) {
    return expanded;
  }
  const normalized = String(expanded.ENABLE_LIVE_TRADING || 'false').toLowerCase() === 'true' ? 'true' : 'false';
  if (!expanded.TRADE_MODE) {
    expanded.TRADE_MODE = normalized === 'true' ? 'live' : 'paper';
  }
  expanded.LIVE_TRADING_ENABLED = normalized;
  delete expanded.ENABLE_LIVE_TRADING;
  return expanded;
}

function renderRuntimeStatus(payload) {
  maybeShowRuntimeAlert(payload);
  setText('runtimeSavedMode', formatModeLabel(payload.saved_mode || 'paper'));
  setText('runtimeRunningMode', formatModeLabel(payload.running_mode || 'paper'));
  setText('runtimeRestartRequired', payload.restart_required ? '需要' : '不需要');
  setText('runtimeLiveReady', payload.live_ready ? '已就绪' : '未就绪');
  setText('runtimeLiveError', payload.live_validation_error || '--');
  setText('runtimeAlertMessage', payload.runtime_alert_message || '--');
  setText('runtimeSummaryText',
    '当前模式 ' + formatModeLabel(payload.running_mode || 'paper') +
    ' / 目标模式 ' + formatModeLabel(payload.saved_mode || 'paper') +
    ' / 是否待切换 ' + (payload.restart_required ? '需要' : '不需要') +
    ' / 实盘就绪 ' + (payload.live_ready ? '已就绪' : '未就绪'));
  setText('runtimeOptimizerEnabled', payload.optimizer_enabled ? '已开启' : '未开启');
  setText('runtimeOptimizerChampion', payload.optimizer_champion_id || '--');
  setText('runtimeOptimizerChallengers', String((payload.optimizer_active_challengers || []).length));
  setText('runtimeOptimizerPromotable', String(payload.optimizer_promotable_count ?? 0));
  setText('runtimeOptimizerLastRun', payload.optimizer_last_run_at ? fmtIso(payload.optimizer_last_run_at) : '--');
  setHtml('runtimeOptimizerChallengerList', renderOptimizerCandidateList(payload.optimizer_active_challengers || []));
  setHtml('runtimeOptimizerPromotableList', renderOptimizerCandidateList(payload.optimizer_promotable_candidates || []));
}

function renderLiveHealth(payload) {
  payload = payload || {};
  const constraints = payload.constraints || {};
  setChip('liveHealthStatus', payload.ok ? '检查通过' : '需要处理', payload.ok ? 'ok' : 'warn');
  setText('liveHealthBalance', constraints.available_balance === null || constraints.available_balance === undefined ? '--' : fmtNum(constraints.available_balance, 6));
  setText('liveHealthOrderType', constraints.order_type || '--');
  setText('liveHealthMinOrder', constraints.order_min_size === null || constraints.order_min_size === undefined ? '--' : fmtNum(constraints.order_min_size, 2));
  setText('liveHealthTickSize', constraints.minimum_tick_size || '--');
  const feesEnabled = constraints.fees_enabled;
  const makerFee = constraints.maker_base_fee === null || constraints.maker_base_fee === undefined ? '--' : String(constraints.maker_base_fee);
  const takerFee = constraints.taker_base_fee === null || constraints.taker_base_fee === undefined ? '--' : String(constraints.taker_base_fee);
  setText('liveHealthFees', (feesEnabled === null || feesEnabled === undefined ? '--' : (feesEnabled ? '开启' : '关闭')) + ' / M ' + makerFee + ' / T ' + takerFee);
  const paperStrategies = Array.isArray(constraints.paper_strategy_ids) ? constraints.paper_strategy_ids.join(',') : '--';
  const liveStrategies = Array.isArray(constraints.live_strategy_ids) ? constraints.live_strategy_ids.join(',') : '--';
  setText('liveHealthStrategies', '纸面 ' + paperStrategies + ' / 实盘 ' + liveStrategies);
  const checks = Array.isArray(payload.checks) ? payload.checks : [];
  setHtml('liveHealthList', checks.map((item) => {
    const tone = item.ok ? 'ok' : 'err';
    const stateText = item.ok ? '通过' : '异常';
    return ''
      + '<div class="runtime-item">'
      +   '<span class="rk">' + esc(item.label || item.id || '--') + '</span>'
      +   '<span class="rv"><span class="chip ' + tone + '">' + stateText + '</span> ' + esc(item.detail || '--') + '</span>'
      + '</div>';
  }).join('') || '<div class="runtime-item"><span class="rk">检查项</span><span class="rv">--</span></div>');
}

async function refreshLiveHealth() {
  setChip('liveHealthStatus', '检查中', 'warn');
  try {
    const data = await apiGet('/api/live/health');
    renderLiveHealth(data);
  } catch (err) {
    setChip('liveHealthStatus', '检查失败', 'err');
    setHtml('liveHealthList', '<div class="runtime-item"><span class="rk">错误</span><span class="rv">' + esc(err && err.message ? err.message : '检查失败') + '</span></div>');
    console.error(err);
  }
}

function maybeShowRuntimeAlert(payload) {
  const message = payload && payload.runtime_alert_message;
  const code = payload && payload.runtime_alert_code;
  if (!message || !code) {
    return;
  }
  const key = [code, payload.runtime_alert_at || message].join('|');
  if (state.lastRuntimeAlertKey === key) {
    return;
  }
  state.lastRuntimeAlertKey = key;
  window.alert(message);
}

async function refreshRuntimeStatus() {
  try {
    const data = await apiGet('/api/config');
    state.config = {
      ...(state.config || {}),
      runtime_status: data.runtime_status || {},
    };
    renderRuntimeStatus(data.runtime_status || {});
  } catch (err) {
    console.error(err);
  }
}

function renderOptimizerCandidateList(items) {
  if (!Array.isArray(items) || items.length === 0) {
    return '--';
  }
  return items.map((item) => {
    const candidateId = String((item || {}).candidate_id || '--');
    const strategyId = String((item || {}).base_strategy_id || '--');
    const score = (item || {}).validation_score;
    const scoreText = (score === null || score === undefined || score === '') ? '--' : String(score);
    const decision = (item || {}).promotion_decision || {};
    const decisionState = String(decision.state || '--');
    const decisionReason = String(decision.reason || '--');
    return esc(candidateId) + ' / S' + esc(strategyId) + ' / score=' + esc(scoreText) + ' / state=' + esc(decisionState) + ' / reason=' + esc(decisionReason);
  }).join('<br>');
}

function timeframeMeta(payload) {
  const raw = String((((payload || {}).env_values || {}).MARKET_TIMEFRAME || '5m')).toLowerCase();
  return TIMEFRAME_META[raw] || TIMEFRAME_META['5m'];
}

function applyTimeframeCopy(payload) {
  const meta = timeframeMeta(payload);
  document.title = 'BTC ' + meta.label + '预测控制台';
  const brand = el('brandTitle');
  if (brand) {
    brand.textContent = meta.brand;
  }
  const panel = el('marketPanelDesc');
  if (panel) {
    panel.textContent = meta.marketDesc;
  }
}

function flattenTimeframePreset(preset) {
  if (!preset) {
    return {};
  }
  return {
    ...(preset.shared || {}),
    ...(preset.strategy5 || {}),
    ...(preset.strategy6 || {}),
    ...(preset.strategy7 || {}),
  };
}


function applyTimeframePreset(timeframe) {
  const presets = (((state.config || {}).timeframe_presets) || {});
  const preset = presets[String(timeframe || '').toLowerCase()];
  const flatPreset = flattenTimeframePreset(preset);
  if (!Object.keys(flatPreset).length) {
    return;
  }
  Object.entries(flatPreset).forEach(([key, value]) => {
    const field = el('cfg_' + key);
    if (!field) {
      return;
    }
    field.value = String(value);
  });
}


function shouldConfirmLiveModeSwitch(previousMode, nextMode, nextLiveEnabled) {
  previousMode = normalizeTradeMode(previousMode || 'paper');
  nextMode = normalizeTradeMode(nextMode || 'paper');
  nextLiveEnabled = String(nextLiveEnabled || 'false').toLowerCase() === 'true';
  return !modeRunsLive(previousMode) && modeRunsLive(nextMode) && nextLiveEnabled;
}

function renderConfig(payload) {
  state.config = payload;
  syncReportModeWithRuntime(payload);
  state.paperTimeframeFilter = effectivePaperTimeframeFilter();
  applyTimeframeCopy(payload);
  renderConfigWarnings(payload);
  const savedAtNode = el('cfgSavedAt');
  if (savedAtNode) {
    savedAtNode.textContent = payload.saved_at ? fmtIso(payload.saved_at) : '--';
  }
  renderRuntimeStatus(payload.runtime_status || {});

  const form = el('configForm');
  form.innerHTML = '';
  const keys = payload.editable_keys || [];
  const labels = payload.labels || {};
  const values = payload.env_values || {};
  const displayValues = { ...values, ENABLE_LIVE_TRADING: buildLiveToggleValue(values) };
  const options = payload.select_options || {};
  const fieldHelp = payload.field_help || {};
  const fieldScope = payload.field_scope || {};
  const validationErrors = payload.validation_errors || {};
  const fieldGroups = Array.isArray(payload.field_groups) && payload.field_groups.length > 0
    ? payload.field_groups
    : [{ title: '\u5168\u90e8\u53c2\u6570', description: '', keys }];
  const editableKeySet = new Set(['ENABLE_LIVE_TRADING', 'TRADE_MODE', ...keys.filter((key) => !isSingleLiveToggleKey(key))]);
  const hiddenKeys = new Set(['STRATEGY_IDS', 'PAPER_STRATEGY_IDS', 'LIVE_STRATEGY_IDS', 'PAPER_TIMEFRAMES', 'MARKET_TIMEFRAME', 'ENABLE_LIVE_TRADING']);
  const advancedPanel = el('advancedConfigPanel');
  if (advancedPanel) {
    advancedPanel.innerHTML = '';
  }
  const displayFieldGroups = fieldGroups.map((group) => {
    return {
      ...group,
      keys: (group.keys || [])
        .filter((key) => !isSingleLiveToggleKey(key) || key === 'TRADE_MODE')
        .map((key) => (key === 'TRADE_MODE' ? 'ENABLE_LIVE_TRADING' : key))
        .filter((key, index, arr) => editableKeySet.has(key) && arr.indexOf(key) === index && !hiddenKeys.has(key) && !isPaperProfileConfigKey(key)),
    };
  });

  for (const group of displayFieldGroups) {
    const groupKeys = (group.keys || []).filter((key) => editableKeySet.has(key));
    if (groupKeys.length === 0) {
      continue;
    }

    const section = document.createElement('section');
    section.className = 'config-group';
    if (group.scope) {
      section.dataset.groupScope = group.scope;
    }
    const advancedGroup = isAdvancedConfigGroup(group);
    section.dataset.advancedGroup = advancedGroup ? 'true' : 'false';

    const head = document.createElement('div');
    head.className = 'config-group-head';
    head.innerHTML =
      '<div class="config-group-title">' + esc(group.title || '\u53c2\u6570\u5206\u7ec4') + '</div>' +
      '<div class="config-group-desc">' + esc(group.description || '') + '</div>';
    section.appendChild(head);

    const grid = document.createElement('div');
    grid.className = 'group-grid';

    for (const key of groupKeys) {
      const wrap = document.createElement('div');
      wrap.className = 'field';
      wrap.dataset.fieldScope = fieldScope[key] || 'all';
      if (isAdvancedConfigKey(key)) {
        wrap.dataset.advancedField = 'true';
      }

      const label = document.createElement('label');
      label.setAttribute('for', 'cfg_' + key);
      label.textContent = formatConfigLabel(key, labels);
      wrap.appendChild(label);

      if (key === 'STRATEGY_ID') {
        wrap.classList.add('field-wide');
        label.setAttribute('for', 'cfgStrategyPanel');
        label.textContent = '策略面板';
        const unifiedWrap = document.createElement('div');
        unifiedWrap.className = 'rows';

        const focusLabel = document.createElement('div');
        focusLabel.className = 'field-help';
        focusLabel.textContent = '\u7b56\u7565\u9762\u677f\uff1a\u6839\u636e\u4e0a\u65b9\u4ea4\u6613\u6a21\u5f0f\uff0c\u81ea\u52a8\u5c55\u793a\u7eb8\u9762\u6216\u5b9e\u76d8\u5df2\u9009\u7684\u57fa\u7840\u7b56\u7565\u3002';
        unifiedWrap.appendChild(focusLabel);

        const panel = document.createElement('div');
        panel.id = 'cfgStrategyPanel';
        panel.className = 'strategy-panel-host';
        unifiedWrap.appendChild(panel);

        const focusSelect = document.createElement('select');
        focusSelect.id = 'cfg_STRATEGY_ID';
        focusSelect.className = 'strategy-panel-hidden-input';
        unifiedWrap.appendChild(focusSelect);

        const multiLabel = document.createElement('div');
        multiLabel.className = 'field-help';
        multiLabel.textContent = '\u4fdd\u5b58\u65f6\u53ea\u66f4\u65b0\u5f53\u524d\u6a21\u5f0f\u5bf9\u5e94\u7684\u7b56\u7565\u5217\u8868\u3002';
        unifiedWrap.appendChild(multiLabel);

        const multiSelect = document.createElement('select');
        multiSelect.id = 'cfg_PAPER_STRATEGY_IDS';
        multiSelect.multiple = true;
        multiSelect.className = 'strategy-panel-hidden-input';
        unifiedWrap.appendChild(multiSelect);
        wrap.appendChild(unifiedWrap);
      } else if (Array.isArray(options[key]) && options[key].length > 0) {
        const select = document.createElement('select');
        select.id = 'cfg_' + key;
        if (isCompactConfigField(key)) {
          select.classList.add('input-compact');
        }
        for (const opt of options[key]) {
          const option = document.createElement('option');
          option.value = opt;
          option.textContent = strategyOptionLabel(key, opt, payload);
          if (String(displayValues[key] ?? '') === String(opt)) {
            option.selected = true;
          }
          select.appendChild(option);
        }
        wrap.appendChild(select);
      } else {
        const input = document.createElement('input');
        input.id = 'cfg_' + key;
        input.type = 'text';
        input.value = String(displayValues[key] ?? '');
        if (isCompactConfigField(key)) {
          input.classList.add('input-compact');
        }
        wrap.appendChild(input);
      }

      if (fieldHelp[key]) {
        const help = document.createElement('div');
        help.className = 'field-help';
        help.textContent = fieldHelp[key];
        wrap.appendChild(help);
      }

      const scopeNote = document.createElement('div');
      scopeNote.className = 'field-scope-note';
      wrap.appendChild(scopeNote);

      if (validationErrors[key]) {
        const err = document.createElement('div');
        err.className = 'field-error';
        err.textContent = validationErrors[key];
        wrap.appendChild(err);
      }
      if (key === 'STRATEGY_ID' && validationErrors.PAPER_STRATEGY_IDS) {
        const err = document.createElement('div');
        err.className = 'field-error';
        err.textContent = validationErrors.PAPER_STRATEGY_IDS;
        wrap.appendChild(err);
      }
      if (key === 'STRATEGY_ID' && validationErrors.STRATEGY_IDS) {
        const err = document.createElement('div');
        err.className = 'field-error';
        err.textContent = validationErrors.STRATEGY_IDS;
        wrap.appendChild(err);
      }
      if (key === 'STRATEGY_ID' && validationErrors.LIVE_STRATEGY_IDS) {
        const err = document.createElement('div');
        err.className = 'field-error';
        err.textContent = validationErrors.LIVE_STRATEGY_IDS;
        wrap.appendChild(err);
      }

      grid.appendChild(wrap);
    }

    section.appendChild(grid);
    if (advancedGroup && advancedPanel) {
      advancedPanel.appendChild(section);
    } else {
      form.appendChild(section);
    }
  }

  renderUnifiedStrategyToolbar(payload, displayValues);
  renderConfigModeShell(payload);
  form.oninput = (event) => {
    if (event && event.target && event.target.getAttribute && event.target.getAttribute('data-strategy-config-key')) {
      return;
    }
    const liveValues = expandLiveToggleValues(collectConfigValues());
    renderUnifiedStrategyToolbar(state.config, liveValues);
    renderStrategyGuide(state.config, liveValues);
    renderConfigModeShell({
      ...(state.config || {}),
      env_values: {
        ...((state.config || {}).env_values || {}),
        ...liveValues,
      },
    });
    applyConfigFieldVisibility(liveValues);
    applyAdvancedConfigVisibility(liveValues);
  };
  form.onchange = form.oninput;

  renderStrategyGuide(payload, displayValues);
  renderPaperProfiles(payload);
  renderPaperRuntimeCards(payload);
  renderSharedPaperReportStrategySelector();
  applyConfigFieldVisibility(expandLiveToggleValues(displayValues));
  applyAdvancedConfigVisibility(expandLiveToggleValues(displayValues));
  const timeframeNode = el('cfg_MARKET_TIMEFRAME');
  if (timeframeNode) {
    timeframeNode.value = String(displayValues['MARKET_TIMEFRAME'] || '5m');
    if (timeframeNode.dataset.bound !== 'true') {
      timeframeNode.dataset.bound = 'true';
      timeframeNode.addEventListener('change', () => {
        if (form.oninput) form.oninput();
        applyTimeframePreset(timeframeNode.value);
      });
    }
  }

  const liveNode = el('cfg_ENABLE_LIVE_TRADING');
  if (liveNode) {
    liveNode.value = String(displayValues['ENABLE_LIVE_TRADING'] || 'false');
    if (liveNode.dataset.bound !== 'true') {
      liveNode.dataset.bound = 'true';
      liveNode.addEventListener('change', () => {
        if (form.oninput) form.oninput();
      });
    }
  }
  setConfigError('--');
  setChip('cfgStatus', '\u5df2\u52a0\u8f7d', 'ok');
  setSaveButtonState('idle');
}

function renderConfigWarnings(payload) {
  const banner = el('configWarningBanner');
  const summary = el('configWarningSummary');
  const list = el('configWarningList');
  if (!banner || !summary || !list) {
    return;
  }
  const warnings = (payload && payload.config_warnings) || {};
  const entries = Object.entries(warnings)
    .filter(([key, message]) => key && message)
    .sort(([leftKey], [rightKey]) => String(leftKey).localeCompare(String(rightKey)));
  if (entries.length === 0) {
    banner.hidden = true;
    summary.textContent = '--';
    list.innerHTML = '';
    return;
  }
  banner.hidden = false;
  summary.textContent = '检测到 ' + entries.length + ' 项配置警告；这些值已按默认或有效值回退。';
  list.innerHTML = entries.map(([key, message]) => (
    '<div class="config-warning-item">' +
      '<span class="config-warning-key">' + esc(formatConfigLabel(key, ((payload || {}).labels) || {})) + '</span>' +
      '<span class="config-warning-message">' + esc(String(message || '')) + '</span>' +
    '</div>'
  )).join('');
}

function collectConfigValues(options) {
  const settings = options || {};
  const payload = {};
  const unifiedStrategyKeys = ['STRATEGY_ID', 'STRATEGY_IDS', 'PAPER_STRATEGY_IDS', 'LIVE_STRATEGY_IDS'];
  const keys = ['ENABLE_LIVE_TRADING', ...(((state.config && state.config.editable_keys) || []).filter((key) => !isSingleLiveToggleKey(key)))];
  for (const key of keys) {
    if (unifiedStrategyKeys.indexOf(key) >= 0) {
      continue;
    }
    const node = el('cfg_' + key);
    if (!node) {
      continue;
    }
    payload[key] = node.value;
  }
  const modeSelect = el('configModeSelect');
  if (modeSelect) {
    payload.TRADE_MODE = normalizeTradeMode(modeSelect.value);
  }
  document.querySelectorAll('[data-strategy-config-key]').forEach((node) => {
    const key = String(node.getAttribute('data-strategy-config-key') || '');
    if (!key) {
      return;
    }
    const value = String(node.value ?? '').trim();
    payload[key] = value;
  });
  if (settings.includeUnified === false) {
    return payload;
  }
  const focusNode = el('cfg_STRATEGY_ID');
  const multiNode = el('cfg_PAPER_STRATEGY_IDS');
  const baseValues = {
    ...(((state.config || {}).env_values) || {}),
    ...payload,
  };
  const multiKey = activeStrategyListKey(state.config || {}, baseValues);
  const domStrategyListKey = multiNode ? String(multiNode.dataset.strategyListKey || '') : '';
  const selectedStrategyList = domStrategyListKey === multiKey
    ? Array.from(multiNode.options || []).filter((option) => option.selected).map((option) => option.value).join(',')
    : String(baseValues[multiKey] || '');
  const rawValues = {
    ...baseValues,
    STRATEGY_ID: focusNode ? focusNode.value : '',
    [multiKey]: selectedStrategyList,
  };
  const unifiedValues = collectUnifiedStrategyValues(state.config || {}, rawValues);
  if (normalizeTradeMode(baseValues.TRADE_MODE) === 'both') {
    ['PAPER_STRATEGY_IDS', 'LIVE_STRATEGY_IDS'].forEach((key) => {
      if (key !== multiKey) {
        unifiedValues[key] = String(baseValues[key] || '');
      }
    });
  }
  Object.entries(unifiedValues).forEach(([key, value]) => {
    payload[key] = value;
  });
  return payload;
}
function areConfigValuesEqual(left, right) {
  const keys = new Set(Object.keys(left || {}));
  for (const key of keys) {
    if (String((left || {})[key] ?? '') !== String((right || {})[key] ?? '')) {
      return false;
    }
  }
  return true;
}

async function refreshConfig() {
  try {
    const data = await apiGet('/api/config');
    renderConfig(data);
  } catch (err) {
    setConfigError(err && err.message ? err.message : '\u8bfb\u53d6\u914d\u7f6e\u5931\u8d25');
    setChip('cfgStatus', '\u8bfb\u53d6\u5931\u8d25', 'err');
    console.error(err);
  }
}

async function saveConfig() {
  let values = {};
  try {
    values = expandLiveToggleValues(collectConfigValues());
    const currentValues = expandLiveToggleValues({ ...(((state.config || {}).env_values) || {}) });
    if (areConfigValuesEqual(values, currentValues)) {
      setChip('cfgStatus', '\u6ca1\u6709\u53d8\u66f4', 'warn');
      setSaveButtonState('idle');
      return;
    }
    setChip('cfgStatus', '保存中', 'warn');
    setSaveButtonState('saving');
    const previousMode = String((((state.config || {}).env_values || {}).TRADE_MODE || 'paper')).toLowerCase();
    const nextMode = String((values.TRADE_MODE || previousMode || 'paper')).toLowerCase();
    const nextLiveEnabled = String(values.LIVE_TRADING_ENABLED || 'false').toLowerCase() === 'true';
    if (shouldConfirmLiveModeSwitch(previousMode, nextMode, nextLiveEnabled) && !window.confirm('开启并行实盘后，纸面交易会继续运行，同时实盘会按实盘配置执行。确认继续吗？')) {
      setChip('cfgStatus', '已取消', 'warn');
      setSaveButtonState('idle');
      return;
    }
    const data = await apiPost('/api/config', { env_values: values });
    renderConfig(data);
    flashSavedAt();
    setChip('cfgStatus', '已保存', 'ok');
    setSaveButtonState('saved');
  } catch (err) {
    const fieldErrors = err && err.fieldErrors ? err.fieldErrors : {};
    if (Object.keys(fieldErrors).length > 0 && state.config) {
      renderConfig({
        ...state.config,
        env_values: values,
        validation_errors: fieldErrors,
      });
      setChip('cfgStatus', '校验失败', 'err');
      setSaveButtonState('error');
    } else {
      setChip('cfgStatus', '保存失败', 'err');
      setSaveButtonState('error');
    }
    console.error(err);
  }
}

function renderWsRuntime(ws, staleGuard) {
  const list = el('wsRuntimeList');
  const wsPayload = ws || {};
  const runtimeValue = (payload, shortKey, rawKey) => {
    if (!payload) {
      return undefined;
    }
    return payload[shortKey] ?? payload[rawKey];
  };
  const basePairs = [
    ['ws_enabled', wsPayload.ws_enabled],
    ['ws_available', wsPayload.ws_available],
    ['ws_connected', wsPayload.ws_connected],
    ['reconnects', runtimeValue(wsPayload, 'reconnects', 'ws_reconnect_count')],
    ['invalid_ops', runtimeValue(wsPayload, 'invalid_ops', 'ws_invalid_operation_count')],
    ['connect_attempts', runtimeValue(wsPayload, 'connect_attempts', 'ws_connect_attempts')],
    ['subscribed_assets', runtimeValue(wsPayload, 'subscribed_assets', 'ws_subscribed_asset_count')],
    ['cached_assets', runtimeValue(wsPayload, 'cached_assets', 'ws_cached_asset_count')],
    ['last_message_age_s', runtimeValue(wsPayload, 'last_message_age_s', 'ws_last_message_age_seconds')],
    ['current_error', runtimeValue(wsPayload, 'current_error', 'ws_current_error')],
    ['last_error', runtimeValue(wsPayload, 'last_error', 'ws_last_error')],
  ];

  const used = new Set([
    ...basePairs.map((item) => item[0]),
    'ws_reconnect_count',
    'ws_invalid_operation_count',
    'ws_connect_attempts',
    'ws_subscribed_asset_count',
    'ws_cached_asset_count',
    'ws_last_message_age_seconds',
    'ws_current_error',
    'ws_last_error',
  ]);
  const extraPairs = Object.entries(wsPayload).filter(([k]) => !used.has(k));
  const pairs = basePairs.concat(extraPairs);

  const rows = pairs.map(([key, value]) => {
    let shown = (value === null || value === undefined || value === '') ? '--' : String(value);
    if (key in STATUS_LABELS && (value === true || value === false)) {
      shown = STATUS_LABELS[value];
    }
    if (key === 'last_error' || key === 'current_error' || key === 'ws_last_error' || key === 'ws_current_error') {
      shown = reasonText(shown);
    }
    if (key === 'last_message_age_s' && shown !== '--') {
      const n = toNum(shown);
      shown = n === null ? shown : n.toFixed(3);
    }
    const displayKey = RUNTIME_LABELS[key] || key;
    return '<div class=\"runtime-item\"><span class=\"rk\">' + esc(displayKey) + '</span><span class=\"rv\">' + esc(shown) + '</span></div>';
  }).join('');

  if (list) {
    list.innerHTML = rows || '<div class=\"empty\">暂无实时连接运行数据</div>';
  }

  const lastAge = runtimeValue(wsPayload, 'last_message_age_s', 'ws_last_message_age_seconds');
  const ageNumber = toNum(lastAge);
  const ageText = ageNumber === null ? '--' : ageNumber.toFixed(1) + 's';
  const subscribed = runtimeValue(wsPayload, 'subscribed_assets', 'ws_subscribed_asset_count');
  const detailParts = [];
  detailParts.push('延迟 ' + ageText);
  if (subscribed !== null && subscribed !== undefined && subscribed !== '') {
    detailParts.push('订阅 ' + String(subscribed));
  }
  const currentError = runtimeValue(wsPayload, 'current_error', 'ws_current_error');
  if (currentError) {
    detailParts.push(reasonText(currentError));
  }
  setText('topConnectionDetail', detailParts.join(' / '));

  if (staleGuard) {
    setChip('wsHealth', '连接陈旧', 'err');
  } else if (wsPayload.ws_connected) {
    setChip('wsHealth', '连接正常', 'ok');
  } else if (wsPayload.ws_enabled === false) {
    setChip('wsHealth', '未启用', 'warn');
  } else {
    setChip('wsHealth', '连接异常', 'warn');
  }
}

function renderDecisionCard(payload) {
  const signal = payload.signal || {};
  const plan = payload.plan || {};
  const signalSide = signal.side || 'SKIP';

  el('planShouldTrade').textContent = plan.should_trade ? '执行' : '跳过';
  el('planSide').textContent = sideText(plan.side || signalSide);
  el('planPrice').textContent = fmtNum(plan.price, 4);
  el('planOrderCost').textContent = fmtNum(plan.order_cost, 4);
  el('planOrderSize').textContent = fmtNum(plan.order_size, 6);
  el('planExpectedProfit').textContent = fmtPnl(plan.expected_profit, 4);
  el('planSkipReason').textContent = reasonText(plan.skip_reason);
  el('planStopLoss').textContent = plan.stop_loss_triggered ? '是' : '否';

  const signalNode = el('signalSide');
  signalNode.textContent = sideText(signalSide);
  signalNode.className = 'value ' + sideClass(signalSide);
  el('signalReason').textContent = reasonText(signal.reason);
  el('signalOpenUp').textContent = fmtNum(signal.open_up, 4);
  el('signalCurrentUp').textContent = fmtNum(signal.current_up, 4);
  el('signalThreshold').textContent = fmtNum(signal.threshold, 4);
  const deltaNode = el('signalDelta');
  deltaNode.textContent = fmtPnl(signal.delta, 4);
  const dn = toNum(signal.delta);
  deltaNode.className = 'v ' + (dn > 0 ? 'pos' : (dn < 0 ? 'neg' : ''));
  el('signalLocked').textContent = signal.locked ? '是' : '否';
}

function renderMarket(payload) {
  state.market = payload;
  const strategyView = payload.strategy_view || {};
  state.marketStrategyFilter = String(strategyView.selected || state.marketStrategyFilter || 'all');
  renderSharedPaperReportStrategySelector();
  const round = payload.round || null;
  const quote = payload.quote || {};
  const strategy6 = payload.strategy6 || {};
   const strategy7 = payload.strategy7 || {};
  const ss = payload.session_state || {};

  if (!round) {
    el('marketDeadline').textContent = '结束时间 --';
    el('marketSlug').textContent = '暂无可用轮次';
    el('marketTitle').textContent = payload.message || '当前时段没有可交易轮次';
    renderEntryCountdown(null);
    setChip('marketHealth', '无轮次', 'warn');
  } else {
    el('marketDeadline').textContent = marketDeadlineText(round.end_time);
    el('marketSlug').textContent = round.slug || '--';
    el('marketTitle').textContent = marketTitleText(round.title);
    renderEntryCountdown(round.seconds_to_entry);
    setChip('marketHealth', round.is_current ? '当前轮次' : '下一轮次', 'ok');
  }

  el('upPrice').textContent = fmtNum(quote.up_price, 4);
  el('downPrice').textContent = fmtNum(quote.down_price, 4);
  el('upAsk').textContent = fmtNum(quote.up_best_ask, 4);
  el('downAsk').textContent = fmtNum(quote.down_best_ask, 4);
  el('quoteSource').textContent = sourceText(quote.source);
  el('quoteAccepting').textContent = quote.accepting_orders ? '是' : '否';
  el('quoteFetchedAt').textContent = fmtIso(quote.fetched_at);

  const strategy6Enabled = !!strategy6.enabled;
  const strategy6Panel = el('strategy6Panel');
  if (strategy6Panel) {
    strategy6Panel.style.display = strategy6Enabled ? '' : 'none';
  }
  el('strategy6OfiScore').textContent = strategy6Enabled ? fmtNum(strategy6.ofi_score, 4) : '--';
  el('strategy6SignalAt').textContent = strategy6Enabled ? fmtIso(strategy6.signal_at) : '--';
  el('strategy6Stale').textContent = strategy6Enabled ? (strategy6.stale ? '是' : '否') : '--';
  el('strategy6BidPrice').textContent = strategy6Enabled ? fmtNum(strategy6.bid_price, 2) : '--';
  el('strategy6BidQty').textContent = strategy6Enabled ? fmtNum(strategy6.bid_qty, 4) : '--';
  el('strategy6AskPrice').textContent = strategy6Enabled ? fmtNum(strategy6.ask_price, 2) : '--';
  el('strategy6AskQty').textContent = strategy6Enabled ? fmtNum(strategy6.ask_qty, 4) : '--';

  const strategy7Enabled = !!strategy7.enabled;
  const strategy7Panel = el('strategy7Panel');
  if (strategy7Panel) {
    strategy7Panel.style.display = strategy7Enabled ? '' : 'none';
  }
  el('strategy7OfiScore').textContent = strategy7Enabled ? fmtNum(strategy7.ofi_score, 4) : '--';
  el('strategy7MomentumDelta').textContent = strategy7Enabled ? fmtPnl(strategy7.momentum_delta, 4) : '--';
  el('strategy7Agreement').textContent = strategy7Enabled
    ? (strategy7.agreement === 'agree' ? '一致' : (strategy7.agreement === 'conflict' ? '冲突' : '--'))
    : '--';
  el('strategy7QualityGate').textContent = strategy7Enabled
    ? (strategy7.quality_gate === 'passed' ? '通过' : reasonText(strategy7.quality_gate))
    : '--';
  el('strategy7FinalReason').textContent = strategy7Enabled ? reasonText(strategy7.final_reason) : '--';
  renderDecisionCard(payload);

  el('ssRoundIndex').textContent = String(ss.round_index ?? '--');

  const cashNode = el('ssCashPnl');
  cashNode.textContent = fmtPnl(ss.cash_pnl, 4);
  cashNode.className = 'v ' + classifyPnl(ss.cash_pnl);

  const recNode = el('ssRecoveryLoss');
  recNode.textContent = fmtNum(ss.recovery_loss, 4);
  recNode.className = 'v ' + (toNum(ss.recovery_loss) > 0 ? 'warn' : '');

  el('ssConsecutiveLosses').textContent = String(ss.consecutive_losses ?? '--');
  el('ssStopLossCount').textContent = String(ss.stop_loss_count ?? '--');

  const dayNode = el('ssDailyPnl');
  dayNode.textContent = fmtPnl(ss.daily_realized_pnl, 4);
  dayNode.className = 'v ' + classifyPnl(ss.daily_realized_pnl);

  const pendingPaperTrades = Array.isArray(ss.pending_paper_trades) ? ss.pending_paper_trades : [];
  const serialHintNode = el('paperSerialHint');
  serialHintNode.textContent = pendingPaperTrades.length > 0 ? ('上一轮未结算，当前按串行模式等待，共 ' + pendingPaperTrades.length + ' 笔待结算') : '当前没有待结算轮次';
  serialHintNode.className = pendingPaperTrades.length > 0 ? 'serial-hint warn' : 'serial-hint';

  const guardNode = el('wsGuard');
  guardNode.textContent = payload.ws_stale_guard_triggered ? '触发' : '正常';
  guardNode.className = 'value ' + (payload.ws_stale_guard_triggered ? 'trade-down' : 'trade-up');

  renderWsRuntime(payload.ws_runtime || {}, !!payload.ws_stale_guard_triggered);
}

function compareStrategySummaryRows(a, b) {
  const dateCompare = String(b.date || '').localeCompare(String(a.date || ''));
  if (dateCompare !== 0) {
    return dateCompare;
  }
  const aStrategy = Number(a.strategy);
  const bStrategy = Number(b.strategy);
  if (Number.isFinite(aStrategy) && Number.isFinite(bStrategy)) {
    return aStrategy - bStrategy;
  }
  return String(a.strategy || '').localeCompare(String(b.strategy || ''));
}

function renderStrategyDays(payload, emptyText) {
  const tbody = el('strategyDaysTbody');
  if (!tbody) {
    return;
  }
  const rows = (payload.strategy_days || []).slice().sort(compareStrategySummaryRows);
  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan=\"7\" class=\"empty\">' + emptyText + '</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map((row) => {
    const dailyPnlCls = classifyPnl(row.total_pnl);
    const cumulativePnlCls = classifyPnl(row.cumulative_pnl);
    const strategyLabel = row.strategy ? ('策略 ' + row.strategy) : '--';
    return '<tr>' +
      '<td>' + esc(row.date || '--') + '</td>' +
      '<td>' + esc(strategyLabel) + '</td>' +
      '<td>' + esc(String(row.trade_rows ?? '--')) + '</td>' +
      '<td>' + esc(String(row.skip_rows ?? '--')) + '</td>' +
      '<td>' + esc(fmtPct(row.hit_rate, 1)) + '</td>' +
      '<td class=\"' + esc(dailyPnlCls) + '\">' + esc(fmtPnl(row.total_pnl, 4)) + '</td>' +
      '<td class=\"' + esc(cumulativePnlCls) + '\">' + esc(fmtPnl(row.cumulative_pnl, 4)) + '</td>' +
      '</tr>';
  }).join('');
}

function renderSummary(payload) {
  state.summary = payload;
  const latest = payload.latest || null;
  const reportMode = effectiveReportMode();
  renderReportModeCopy();
  const emptyText = reportMode === 'live' ? '暂无实盘数据' : '暂无纸面数据';

  if (!latest) {
    el('sumDate').textContent = '--';
    el('sumTrades').textContent = '--';
    el('sumHitRate').textContent = '--';
    el('sumTotalPnl').textContent = '--';
    el('sumDrawdown').textContent = '--';
    el('sumStrongRate').textContent = '--';
    el('daysTbody').innerHTML = '<tr><td colspan=\"5\" class=\"empty\">' + emptyText + '</td></tr>';
    renderStrategyDays(payload, emptyText);
    setReportStatus('paperStatus', '汇总', '暂无数据', 'warn');
    return;
  }

  el('sumDate').textContent = latest.date || '--';
  el('sumTrades').textContent = String(latest.trade_rows ?? '--');
  el('sumHitRate').textContent = fmtPct(latest.hit_rate, 2);

  const totalNode = el('sumTotalPnl');
  totalNode.textContent = fmtPnl(latest.total_pnl, 4);
  totalNode.className = 'v ' + classifyPnl(latest.total_pnl);

  const ddNode = el('sumDrawdown');
  ddNode.textContent = fmtNum(latest.max_drawdown, 4);
  ddNode.className = 'v warn';

  el('sumStrongRate').textContent = fmtPct(latest.strong_signal_rate, 2);

  const days = (payload.days || []).slice(-14).reverse();
  const rows = days.map((day) => {
    const pnlCls = classifyPnl(day.total_pnl);
    return '<tr>' +
      '<td>' + esc(day.date || '--') + '</td>' +
      '<td>' + esc(String(day.trade_rows ?? '--')) + '</td>' +
      '<td>' + esc(fmtPct(day.hit_rate, 1)) + '</td>' +
      '<td class=\"' + esc(pnlCls) + '\">' + esc(fmtPnl(day.total_pnl, 4)) + '</td>' +
      '<td>' + esc(fmtNum(day.max_drawdown, 4)) + '</td>' +
      '</tr>';
  }).join('');

  el('daysTbody').innerHTML = rows || '<tr><td colspan=\"5\" class=\"empty\">' + emptyText + '</td></tr>';
  renderStrategyDays(payload, emptyText);
  setReportStatus('paperStatus', '汇总', '已更新', 'ok');
}

function renderRecent(payload) {
  state.recent = payload;
  const rows = payload.rows || [];
  const tbody = el('recentTbody');
  const runningMode = String((((state.config || {}).runtime_status || {}).active_mode || (((state.config || {}).runtime_status || {}).running_mode) || 'paper')).toLowerCase();
  const reportMode = effectiveReportMode();
  el('recentPanelDesc').textContent = recentStrategyHeaderText();
  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan=14 class=empty>' + (runningMode === 'live' ? '最近没有实盘交易记录' : '最近没有纸面交易记录') + '</td></tr>';
    setReportStatus('recentStatus', '明细', '0 行', 'warn');
    return;
  }

  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="14" class="empty">最近没有纸面交易记录</td></tr>';
    setReportStatus('recentStatus', '明细', '0 行', 'warn');
    return;
  }

  const pendingCount = rows.filter((row) => row.pending_status === 'pending_settlement').length;
  const html = rows.map((row) => {
    const side = String(row.side || '').toUpperCase();
    const sideCls = sideClass(side);
    const pnlCls = classifyPnl(row.trade_pnl);
    const cashCls = classifyPnl(row.cash_pnl);
    const isPending = row.pending_status === 'pending_settlement';
    const isMissedEntry = row.skip_reason === 'entry_window_missed';
    const resultText = isPending ? '待结算' : tradeResultText(row.result);
    const checkText = resultCheckText(row.result_check_status);
    const checkCls = row.result_check_status === 'match' ? 'trade-up' : ((row.result_check_status === 'mismatch') ? 'trade-down' : 'trade-skip');
    const checkTitle = '官方结果: ' + tradeResultText(row.resolved_expected_result) + (row.result_check_error ? (' · 错误: ' + row.result_check_error) : '');
    const priceCheckLabel = String(row.price_check_label || '').trim();
    const priceCheckStatus = String(row.price_check_status || '').trim();
    const priceCheckDetail = String(row.price_check_detail || '').trim();
    const priceValue = esc(fmtNum(row.price, 4));
    const priceHtml = priceCheckLabel
      ? (priceValue + '<br><span class="recent-price-check ' + esc(priceCheckClass(priceCheckStatus)) + '" title="' + esc(priceCheckDetail) + '">' + esc(priceCheckLabel) + '</span>')
      : priceValue;
    const rowClass = isPending ? 'recent-pending' : (isMissedEntry ? 'recent-missed-entry' : '');
    const balanceError = String(row.balance_error || '').trim();
    const reasonTitle = balanceError ? (reasonText(row.skip_reason) + ' / ' + balanceError) : reasonText(row.skip_reason);
    const reasonHtml = isMissedEntry
      ? ('<span class="skip-reason-badge missed-entry">' + esc(reasonText(row.skip_reason)) + '</span>')
      : esc(reasonText(row.skip_reason));

    const roundDisplay = row.round_display_time ? fmtIso(row.round_display_time) : (row.end_time ? fmtIso(row.end_time) : (row.round_index ? String(row.round_index) : formatRoundSlug(row.event_slug)));
    return '<tr class="' + esc(rowClass) + '">' +
      '<td>' + esc(fmtIso(row.timestamp)) + '</td>' +
      '<td title="' + esc(row.event_slug || '--') + '">' + esc(roundDisplay) + '</td>' +
      '<td>' + esc(row.strategy || '--') + '</td>' +
      '<td class="' + esc(sideCls) + '">' + esc(sideText(side)) + '</td>' +
      '<td>' + priceHtml + '</td>' +
      '<td>' + esc(fmtNum(row.order_cost, 4)) + '</td>' +
      '<td class="' + (isPending ? 'trade-skip' : '') + '">' + esc(resultText) + '</td>' +
      '<td class="' + esc(checkCls) + '" title="' + esc(checkTitle) + '">' + esc(checkText) + '</td>' +
      '<td>' + esc(fmtNum(row.resolved_price_to_beat, 2)) + '</td>' +
      '<td>' + esc(fmtNum(row.resolved_final_price, 2)) + '</td>' +
      '<td class="' + esc(pnlCls) + '">' + esc(fmtPnl(row.trade_pnl, 4)) + '</td>' +
      '<td class="' + esc(cashCls) + '">' + esc(fmtPnl(row.cash_pnl, 4)) + '</td>' +
      '<td title="' + esc(reasonTitle) + '">' + reasonHtml + '</td>' +
      '<td>' + esc(fmtPnl(row.signal_delta, 4)) + '</td>' +
      '</tr>';
  }).join('');

  tbody.innerHTML = html;
  setReportStatus('recentStatus', '明细', pendingCount > 0 ? (rows.length + ' 行 · ' + pendingCount + ' 待结算') : (rows.length + ' 行'), pendingCount > 0 ? 'warn' : 'ok');
  if (pendingCount === 0) {
    setReportStatus('recentStatus', '明细', rows.length + ' 行' + (reportMode === 'live' ? ' · 实盘' : ''), pendingCount > 0 ? 'warn' : 'ok');
  }
}

async function refreshMarket() {
  try {
    const strategy = encodeURIComponent(String(state.marketStrategyFilter || 'all'));
    const timeframe = encodeURIComponent(effectivePaperTimeframeFilter());
    const data = await apiGet('/api/market?strategy=' + strategy + '&timeframe=' + timeframe);
    renderMarket(data);
  } catch (err) {
    setChip('marketHealth', '刷新失败', 'err');
    console.error(err);
  }
}

async function refreshSummary() {
  const requestSeq = nextReportRequestSeq('summary');
  try {
    const reportMode = effectiveReportMode();
    const strategy = encodeURIComponent(effectivePaperSummaryStrategyFilter());
    const timeframe = encodeURIComponent(effectivePaperTimeframeFilter());
    const summaryEndpoint = reportMode === 'live' ? '/api/live/summary?strategy=' + strategy : '/api/paper/summary?strategy=' + strategy + '&timeframe=' + timeframe;
    const data = await apiGet(summaryEndpoint);
    if (!isCurrentReportRequest('summary', requestSeq)) {
      return;
    }
    renderSummary(data);
  } catch (err) {
    if (!isCurrentReportRequest('summary', requestSeq)) {
      return;
    }
    setReportStatus('paperStatus', '汇总', '刷新失败', 'err');
    console.error(err);
  }
}

async function refreshRecent() {
  if (state.recentRefreshInFlight) {
    return;
  }
  state.recentRefreshInFlight = true;
  const requestSeq = nextReportRequestSeq('recent');
  try {
    const reportMode = effectiveReportMode();
    const strategy = encodeURIComponent(effectivePaperRecentStrategyFilter());
    const timeframe = encodeURIComponent(effectivePaperTimeframeFilter());
    const recentEndpoint = reportMode === 'live' ? '/api/live/recent?limit=80&strategy=' + strategy : '/api/paper/recent?limit=80&strategy=' + strategy + '&timeframe=' + timeframe;
    const data = await apiGet(recentEndpoint);
    if (!isCurrentReportRequest('recent', requestSeq)) {
      return;
    }
    renderRecent(data);
  } catch (err) {
    if (!isCurrentReportRequest('recent', requestSeq)) {
      return;
    }
    setReportStatus('recentStatus', '明细', '刷新失败', 'err');
    console.error(err);
  } finally {
    state.recentRefreshInFlight = false;
  }
}

async function refreshAll() {
  await refreshConfig();
  await refreshMarket();
  await Promise.allSettled([refreshSummary(), refreshRecent(), refreshLiveHealth()]);
}

function tickClock() {
  const now = new Date();
  el('clockLocal').textContent = '本地 ' + now.toLocaleString('zh-CN', { hour12: false });
  tickEntryCountdown();
}

function bindActions() {
  syncToggleButtonText();
  toggleFoldSection('runtimeDetails', state.runtimeDetailsOpen);
  toggleFoldSection('diagnostics', state.diagnosticsOpen);
  toggleFoldSection('signalDetails', state.signalDetailsOpen);
  toggleFoldSection('advancedConfig', state.advancedConfigOpen);
  el('btnHelp').addEventListener('click', () => {
    state.helpReturnFocusId = 'btnHelp';
    openHelpDrawer('quickstart');
  });
  el('btnHelpClose').addEventListener('click', closeHelpDrawer);
  el('helpBackdrop').addEventListener('click', closeHelpDrawer);
  const runtimeDetailsToggle = el('runtimeDetailsToggle');
  if (runtimeDetailsToggle) {
    runtimeDetailsToggle.addEventListener('click', () => {
      state.runtimeDetailsOpen = !state.runtimeDetailsOpen;
      toggleFoldSection('runtimeDetails', state.runtimeDetailsOpen);
    });
  }
  el('diagnosticsToggle').addEventListener('click', () => {
    state.diagnosticsOpen = !state.diagnosticsOpen;
    toggleFoldSection('diagnostics', state.diagnosticsOpen);
  });
  el('signalDetailsToggle').addEventListener('click', () => {
    state.signalDetailsOpen = !state.signalDetailsOpen;
    toggleFoldSection('signalDetails', state.signalDetailsOpen);
  });
  el('advancedConfigToggle').addEventListener('click', () => {
    state.advancedConfigOpen = !state.advancedConfigOpen;
    toggleFoldSection('advancedConfig', state.advancedConfigOpen);
    applyAdvancedConfigVisibility(expandLiveToggleValues(collectConfigValues()));
  });
  el('btnRefreshNow').addEventListener('click', () => {
    refreshAll();
  });
  const liveHealthButton = el('btnLiveHealthCheck');
  if (liveHealthButton) {
    liveHealthButton.addEventListener('click', () => {
      refreshLiveHealth();
    });
  }
  el('btnSaveConfig').addEventListener('click', () => {
    saveConfig();
  });
}

function startPolling() {
  setInterval(refreshMarket, POLL_MS.market);
  setInterval(refreshRuntimeStatus, POLL_MS.market);
  setInterval(refreshSummary, POLL_MS.summary);
  setInterval(refreshRecent, POLL_MS.recent);
  setInterval(tickClock, POLL_MS.clock);
}

async function bootstrap() {
  loadUiPrefs();
  bindActions();
  renderHelpDrawer();
  tickClock();
  await refreshAll();
  startPolling();
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && state.helpOpen) {
    closeHelpDrawer();
  }
});

document.addEventListener('DOMContentLoaded', bootstrap);
"""
