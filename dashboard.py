from __future__ import annotations

import csv
import errno
import json
import os
import re
import shutil
import threading
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from atomic_io import atomic_write_text
from config import (
    AppConfig,
    MARKET_TIMEFRAME_DEFINITIONS,
    build_config_from_env_values,
    collect_config_warnings,
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
from risk_and_sizing import build_trade_plan
from strategy import get_side_for_round
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
from redeem_worker import (
    load_live_redeem_state,
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


SUPPORTED_STRATEGY_ID_TEXTS: set[str] = {"1", "2", "3", "4", "5", "6", "7", "8"}
SUPPORTED_STRATEGY_SELECT_OPTIONS: list[str] = ["1", "2", "3", "4", "5", "6", "7", "8"]


def _normalize_strategy_id_list_for_key(value: str, key: str, attr_name: str) -> str:
    cfg = build_config_from_env_values({key: value})
    raw = [item.strip() for item in str(value).split(',') if item.strip()]
    normalized_ids = list(getattr(cfg, attr_name))
    if raw and len(normalized_ids) == 1 and normalized_ids[0] == cfg.strategy_id:
        has_valid = any(item in SUPPORTED_STRATEGY_ID_TEXTS for item in raw)
        if not has_valid:
            raise ValueError(f"Invalid value for {key}: expected comma-separated strategy ids 1-8, got {value!r}")
    normalized = [str(item) for item in normalized_ids]
    if not normalized:
        raise ValueError(f"Invalid value for {key}: expected comma-separated strategy ids 1-8, got {value!r}")
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


def _collapse_live_recent_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
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
    collapsed.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
    return collapsed


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
    "TARGET_PROFIT",
    "BET_SIZING_MODE",
    "BASE_ORDER_COST",
    "MIN_STAKE",
    "MAX_CONSECUTIVE_LOSSES",
    "MAX_STAKE",
    "MIN_ENTRY_PRICE",
    "MAX_ENTRY_PRICE",
    "OPEN_DELAY_SECONDS",
    "SIGNAL_MOMENTUM_THRESHOLD",
    "OFI_THRESHOLD",
    "BINANCE_SIGNAL_STALE_SECONDS",
    "STRATEGY7_OFI_THRESHOLD",
    "STRATEGY7_MOMENTUM_THRESHOLD",
)

STRATEGY_PROFILE_EDITABLE_FIELDS: tuple[str, ...] = (
    "TARGET_PROFIT",
    "BET_SIZING_MODE",
    "BASE_ORDER_COST",
    "MIN_STAKE",
    "MAX_STAKE",
    "MIN_ENTRY_PRICE",
    "MAX_ENTRY_PRICE",
    "MAX_CONSECUTIVE_LOSSES",
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
    "STRATEGY7_MIN_SIGNAL_GAP",
    "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS",
    "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP",
    "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS",
)

STRATEGY_PROFILE_COMMON_FIELDS: tuple[str, ...] = (
    "TARGET_PROFIT",
    "BET_SIZING_MODE",
    "BASE_ORDER_COST",
    "MIN_STAKE",
    "MAX_STAKE",
    "MIN_ENTRY_PRICE",
    "MAX_ENTRY_PRICE",
    "MAX_CONSECUTIVE_LOSSES",
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


def _strategy_profile_prefix(mode: str, strategy_id: int | str) -> str:
    normalized_mode = "live" if str(mode or "").lower() == "live" else "paper"
    return f"{normalized_mode.upper()}_STRATEGY_{strategy_id}"


def _split_strategy_profile_key(key: str) -> tuple[str, str, str] | None:
    match = re.match(r"^(LIVE|PAPER)_STRATEGY_([1-8])_(.+)$", str(key or ""))
    if not match:
        return None
    mode = match.group(1).lower()
    strategy_id = match.group(2)
    base_key = match.group(3)
    if base_key not in STRATEGY_PROFILE_EDITABLE_FIELDS:
        return None
    return mode, strategy_id, base_key


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
    if strategy_text in {"7", "8"}:
        fields.extend(
            [
                "OFI_THRESHOLD",
                "BINANCE_SIGNAL_STALE_SECONDS",
                "STRATEGY7_OFI_THRESHOLD",
                "STRATEGY7_MOMENTUM_THRESHOLD",
                "STRATEGY7_MIN_SIGNAL_GAP",
                "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS",
                "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP",
                "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS",
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
        target_profit=profile.target_profit,
        bet_sizing_mode=profile.bet_sizing_mode,
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
        strategy7_max_entry_price=profile.strategy7_max_entry_price,
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


def _official_winning_outcome(event_payload: dict[str, Any]) -> str:
    market = (event_payload.get("markets") or [{}])[0]
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


def _validate_recent_trade_row(
    row: dict[str, str],
    *,
    client: PolymarketClient | Any,
    validation_cache: dict[str, dict[str, str]] | None = None,
    fill_missing_result: bool = False,
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
    if not slug or (missing_result and not fill_missing_result):
        return validated
    if not _slug_matches_client_series(slug, client):
        return validated

    cache_key = slug
    resolved = dict((validation_cache or {}).get(cache_key) or {})
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
            if validation_cache is not None:
                validation_cache[cache_key] = dict(resolved)
            if should_validate_result:
                validated['result_check_status'] = 'error'
                validated['result_check_error'] = resolved['result_check_error']
            return validated

        metadata = event_payload.get("eventMetadata") or {}
        price_to_beat = _optional_float(metadata.get("priceToBeat"))
        final_price = _optional_float(metadata.get("finalPrice"))
        if final_price is None:
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

        official_result = _official_winning_outcome(event_payload)
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


def _live_result_value(row: dict[str, str]) -> str:
    result = str(row.get("result") or "").strip().upper()
    return result if result in {"UP", "DOWN"} else ""


def _live_float_value(row: dict[str, str], key: str) -> float:
    return _optional_float(row.get(key)) or 0.0


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
    if result not in {"UP", "DOWN"} or side not in {"UP", "DOWN"}:
        return False
    if str(row.get("skip_reason") or "").strip():
        return False
    return _live_float_value(row, "order_cost") > 0.0


def _recompute_live_ledger_rows(rows: list[dict[str, str]]) -> dict[str, dict[str, float | int]]:
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
        result = _live_result_value(row)
        trade_pnl = 0.0
        if _live_row_counts_for_ledger(row, result):
            side = str(row.get("side") or "").strip().upper()
            order_cost = _live_float_value(row, "order_cost")
            expected_profit = _live_float_value(row, "expected_profit")
            if result == side:
                trade_pnl = expected_profit
                state["cash_pnl"] = float(state["cash_pnl"]) + trade_pnl
                state["daily_realized_pnl"] = float(state["daily_realized_pnl"]) + trade_pnl
                state["recovery_loss"] = 0.0
                state["consecutive_losses"] = 0
            else:
                trade_pnl = -order_cost
                state["cash_pnl"] = float(state["cash_pnl"]) + trade_pnl
                state["recovery_loss"] = float(state["recovery_loss"]) + order_cost
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
) -> None:
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
    active_state = ledger_states.get(str(active_strategy_id))
    if active_state is not None:
        for key in ("cash_pnl", "daily_realized_pnl", "recovery_loss", "consecutive_losses"):
            payload[key] = active_state[key]
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


def _auto_reconcile_live_ledger(
    *,
    live_csv: Path,
    state_path: Path,
    client: PolymarketClient | Any,
    validation_cache: dict[str, dict[str, str]] | None,
    active_strategy_id: int,
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
        if _live_result_value(row) not in {"UP", "DOWN"}:
            continue
        validated = _validate_recent_trade_row(
            row,
            client=client,
            validation_cache=validation_cache,
        )
        if validated.get("result_check_status") != "mismatch":
            continue
        official_result = str(validated.get("resolved_expected_result") or "").strip().upper()
        if official_result not in {"UP", "DOWN"}:
            continue
        row["result"] = official_result
        changed += 1

    if changed <= 0:
        return 0

    _backup_live_ledger_files(live_csv=live_csv, state_path=state_path)
    ledger_states = _recompute_live_ledger_rows(rows)
    with live_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    _update_live_session_state_from_ledger(
        state_path=state_path,
        ledger_states=ledger_states,
        active_strategy_id=active_strategy_id,
    )
    return changed



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
            "summary": "币安盘口失衡与预测市场动量同向时加分，冲突时按加权评分决定方向。",
            "preview": ["OFI", "MOMENTUM", "THRESHOLD", "SKIP"],
            "detail": "盘口失衡权重 60%，动量权重 40%，仍需满足信号强度与入场风控。",
        },
        "8": {
            "label": "状态切换",
            "summary": "趋势时跟随共识，强冲突时按盘口方向做反转，否则跳过。",
            "preview": ["REGIME", "OFI", "MOMENTUM", "REVERSAL"],
            "detail": "用同一套信号在趋势和过热冲突之间切换，纸面和实盘行为保持一致。",
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
                "POLYMARKET_BUILDER_API_KEY",
                "POLYMARKET_BUILDER_SECRET",
                "POLYMARKET_BUILDER_PASSPHRASE",
                "POLYMARKET_RELAYER_API_KEY",
                "POLYMARKET_RELAYER_API_KEY_ADDRESS",
                "LIVE_AUTO_REDEEM_ENABLED",
                "LIVE_AUTO_REDEEM_DRY_RUN",
                "LIVE_AUTO_REDEEM_POLL_SECONDS",
                "LIVE_AUTO_REDEEM_MAX_RETRIES",
                "LIVE_AUTO_REDEEM_INITIAL_BACKOFF_SECONDS",
                "LIVE_AUTO_REDEEM_MAX_BACKOFF_SECONDS",
            ],
        },
        {
            "title": "基础策略",
            "description": "决定方向节奏、下注模式和主要风险边界。",
            "keys": [
                "STRATEGY_ID",
                "STRATEGY_IDS",
                "OPEN_DELAY_SECONDS",
                "TARGET_PROFIT",
                "BET_SIZING_MODE",
                "BASE_ORDER_COST",
                "PAPER_SIMULATED_WALLET_BALANCE",
                "MAX_CONSECUTIVE_LOSSES",
                "MIN_STAKE",
                "MAX_STAKE",
                "MIN_ENTRY_PRICE",
                "MAX_ENTRY_PRICE",
                "OFI_THRESHOLD",
            ],
        },
        {
            "title": "动量信号",
            "description": "仅策略 5 使用，用于判定强弱信号、回退逻辑和锁边。",
            "scope": "strategy_5_only",
            "keys": [
                "SIGNAL_MOMENTUM_THRESHOLD",
                "SIGNAL_WEAK_SIGNAL_MODE",
                "SIGNAL_FALLBACK_STRATEGY_ID",
                "SIGNAL_HISTORY_FIDELITY_SECONDS",
                "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS",
                "SIGNAL_DYNAMIC_THRESHOLD_K",
                "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS",
                "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS",
                "BINANCE_SIGNAL_STALE_SECONDS",
                "STRATEGY7_OFI_THRESHOLD",
                "STRATEGY7_MOMENTUM_THRESHOLD",
                "STRATEGY7_MIN_SIGNAL_GAP",
                "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS",
                "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP",
                "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS",
            ],
        },
        {
            "title": "风险与告警",
            "description": "控制连续超限提醒，避免异常状态被忽略。",
            "keys": ["MAX_STAKE_SKIP_ALERT_THRESHOLD"],
        },
        {
            "title": "实时连接保护",
            "description": "控制 WS 行情刷新与交易陈旧保护阈值。",
            "keys": [
                "WS_ENABLED",
                "WS_QUOTE_STALE_SECONDS",
                "WS_TRADE_GUARD_STALE_SECONDS",
                "WS_CONNECT_TIMEOUT_SECONDS",
            ],
        },
    ]


TIMEFRAME_PRESETS: dict[str, dict[str, dict[str, str]]] = {
    "5m": {
        "shared": {
            "OPEN_DELAY_SECONDS": "12",
            "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "10",
        },
        "strategy5": {
            "SIGNAL_MOMENTUM_THRESHOLD": "0.020",
            "SIGNAL_FALLBACK_STRATEGY_ID": "2",
            "MAX_PRICE_THRESHOLD": "0.60",
            "TARGET_PROFIT": "0.8",
        },
        "strategy6": {
            "OFI_THRESHOLD": "0.72",
            "BINANCE_SIGNAL_STALE_SECONDS": "1.0",
            "TARGET_PROFIT": "0.8",
        },
        "strategy7": {
            "STRATEGY7_OFI_THRESHOLD": "0.58",
            "STRATEGY7_MOMENTUM_THRESHOLD": "0.008",
            "MAX_ENTRY_PRICE": "0.54",
            "STRATEGY7_MIN_SIGNAL_GAP": "0.015",
            "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "2",
            "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP": "0.035",
            "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS": "2",
        },
    },
    "15m": {
        "shared": {
            "OPEN_DELAY_SECONDS": "25",
            "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "20",
        },
        "strategy5": {
            "SIGNAL_MOMENTUM_THRESHOLD": "0.015",
            "SIGNAL_FALLBACK_STRATEGY_ID": "2",
            "MAX_PRICE_THRESHOLD": "0.65",
            "TARGET_PROFIT": "1.0",
        },
        "strategy6": {
            "OFI_THRESHOLD": "0.65",
            "BINANCE_SIGNAL_STALE_SECONDS": "2.0",
            "TARGET_PROFIT": "1.0",
        },
        "strategy7": {
            "STRATEGY7_OFI_THRESHOLD": "0.50",
            "STRATEGY7_MOMENTUM_THRESHOLD": "0.005",
            "MAX_ENTRY_PRICE": "0.55",
            "STRATEGY7_MIN_SIGNAL_GAP": "0.01",
            "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "3",
            "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP": "0.03",
            "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS": "3",
        },
    },
}


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
        "POLYMARKET_BUILDER_API_KEY",
        "POLYMARKET_BUILDER_SECRET",
        "POLYMARKET_BUILDER_PASSPHRASE",
        "POLYMARKET_RELAYER_API_KEY",
        "POLYMARKET_RELAYER_API_KEY_ADDRESS",
        "LIVE_AUTO_REDEEM_ENABLED",
        "LIVE_AUTO_REDEEM_DRY_RUN",
        "LIVE_AUTO_REDEEM_POLL_SECONDS",
        "LIVE_AUTO_REDEEM_MAX_RETRIES",
        "LIVE_AUTO_REDEEM_INITIAL_BACKOFF_SECONDS",
        "LIVE_AUTO_REDEEM_MAX_BACKOFF_SECONDS",
        "STRATEGY_ID",
        "STRATEGY_IDS",
        "LIVE_STRATEGY_IDS",
        "PAPER_STRATEGY_IDS",
        "OPEN_DELAY_SECONDS",
        "TARGET_PROFIT",
        "BET_SIZING_MODE",
        "BASE_ORDER_COST",
        "MIN_STAKE",
        "PAPER_SIMULATED_WALLET_BALANCE",
        "MAX_CONSECUTIVE_LOSSES",
        "MAX_STAKE",
        "MIN_ENTRY_PRICE",
        "MAX_ENTRY_PRICE",
        "MAX_PRICE_THRESHOLD",
        "OFI_THRESHOLD",
        "STRATEGY7_OFI_THRESHOLD",
        "STRATEGY7_MOMENTUM_THRESHOLD",
        "STRATEGY7_MIN_SIGNAL_GAP",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS",
        "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP",
        "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS",
        "SIGNAL_MOMENTUM_THRESHOLD",
        "SIGNAL_WEAK_SIGNAL_MODE",
        "SIGNAL_FALLBACK_STRATEGY_ID",
        "SIGNAL_HISTORY_FIDELITY_SECONDS",
        "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS",
        "SIGNAL_DYNAMIC_THRESHOLD_K",
        "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS",
        "BINANCE_SIGNAL_STALE_SECONDS",
        "MAX_STAKE_SKIP_ALERT_THRESHOLD",
        "WS_ENABLED",
        "WS_QUOTE_STALE_SECONDS",
        "WS_TRADE_GUARD_STALE_SECONDS",
        "WS_CONNECT_TIMEOUT_SECONDS",
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
        "POLYMARKET_BUILDER_API_KEY": "Builder 自动赎回接口密钥",
        "POLYMARKET_BUILDER_SECRET": "Builder 自动赎回签名密钥",
        "POLYMARKET_BUILDER_PASSPHRASE": "Builder 自动赎回口令",
        "POLYMARKET_RELAYER_API_KEY": "Relayer 接口密钥",
        "POLYMARKET_RELAYER_API_KEY_ADDRESS": "Relayer 密钥地址",
        "LIVE_AUTO_REDEEM_ENABLED": "实盘自动赎回",
        "LIVE_AUTO_REDEEM_DRY_RUN": "自动赎回演练模式",
        "LIVE_AUTO_REDEEM_POLL_SECONDS": "自动赎回轮询秒数",
        "LIVE_AUTO_REDEEM_MAX_RETRIES": "自动赎回最大重试次数",
        "LIVE_AUTO_REDEEM_INITIAL_BACKOFF_SECONDS": "自动赎回初始退避秒数",
        "LIVE_AUTO_REDEEM_MAX_BACKOFF_SECONDS": "自动赎回最大退避秒数",
        "STRATEGY_ID": "基础策略",
        "STRATEGY_IDS": "统一策略组合",
        "LIVE_STRATEGY_IDS": "实盘策略组合",
        "PAPER_STRATEGY_IDS": "纸面策略组合",
        "OPEN_DELAY_SECONDS": "开盘后入场秒数",
        "TARGET_PROFIT": "每次目标净利",
        "BET_SIZING_MODE": "下注模式",
        "BASE_ORDER_COST": "固定起始下注金额",
        "MIN_STAKE": "单笔最小下注金额",
        "PAPER_SIMULATED_WALLET_BALANCE": "纸面模拟钱包余额",
        "MAX_CONSECUTIVE_LOSSES": "连亏重置轮数",
        "MAX_STAKE": "单笔最大下注金额",
        "MIN_ENTRY_PRICE": "最低买入价格",
        "MAX_ENTRY_PRICE": "最高买入价格",
        "MAX_PRICE_THRESHOLD": "最高买入价格阈值",
        "OFI_THRESHOLD": "盘口失衡阈值",
        "STRATEGY7_OFI_THRESHOLD": "策略7 盘口失衡阈值",
        "STRATEGY7_MOMENTUM_THRESHOLD": "策略7 动量阈值",
        "STRATEGY7_MIN_SIGNAL_GAP": "策略7 最小信号优势",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "策略7 最晚确认秒数",
        "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP": "策略7 强信号额外优势",
        "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS": "策略7 强信号放宽秒数",
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
    }

    SELECT_OPTIONS: dict[str, list[str]] = {
        "ENABLE_LIVE_TRADING": ["false", "true"],
        "TRADE_MODE": ["paper", "live"],
        "MARKET_TIMEFRAME": ["5m", "15m"],
        "LIVE_TRADING_ENABLED": ["true", "false"],
        "LIVE_AUTO_REDEEM_ENABLED": ["false", "true"],
        "LIVE_AUTO_REDEEM_DRY_RUN": ["false", "true"],
        "STRATEGY_ID": SUPPORTED_STRATEGY_SELECT_OPTIONS,
        "STRATEGY_IDS": SUPPORTED_STRATEGY_SELECT_OPTIONS,
        "LIVE_STRATEGY_IDS": SUPPORTED_STRATEGY_SELECT_OPTIONS,
        "PAPER_STRATEGY_IDS": SUPPORTED_STRATEGY_SELECT_OPTIONS,
        "BET_SIZING_MODE": ["FIXED_BASE_COST", "TARGET_PROFIT"],
        "SIGNAL_WEAK_SIGNAL_MODE": ["SKIP", "FALLBACK"],
        "SIGNAL_FALLBACK_STRATEGY_ID": ["1", "2", "3", "4"],
        "WS_ENABLED": ["true", "false"],
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
        "POLYMARKET_BUILDER_API_KEY": "live_redeem_builder_api_key",
        "POLYMARKET_BUILDER_SECRET": "live_redeem_builder_secret",
        "POLYMARKET_BUILDER_PASSPHRASE": "live_redeem_builder_passphrase",
        "POLYMARKET_RELAYER_API_KEY": "live_redeem_relayer_api_key",
        "POLYMARKET_RELAYER_API_KEY_ADDRESS": "live_redeem_relayer_api_key_address",
        "LIVE_AUTO_REDEEM_ENABLED": "live_auto_redeem_enabled",
        "LIVE_AUTO_REDEEM_DRY_RUN": "live_auto_redeem_dry_run",
        "LIVE_AUTO_REDEEM_POLL_SECONDS": "live_auto_redeem_poll_seconds",
        "LIVE_AUTO_REDEEM_MAX_RETRIES": "live_auto_redeem_max_retries",
        "LIVE_AUTO_REDEEM_INITIAL_BACKOFF_SECONDS": "live_auto_redeem_initial_backoff_seconds",
        "LIVE_AUTO_REDEEM_MAX_BACKOFF_SECONDS": "live_auto_redeem_max_backoff_seconds",
        "STRATEGY_ID": "strategy_id",
        "STRATEGY_IDS": "strategy_ids",
        "LIVE_STRATEGY_IDS": "live_strategy_ids",
        "PAPER_STRATEGY_IDS": "paper_strategy_ids",
        "OPEN_DELAY_SECONDS": "open_delay_seconds",
        "TARGET_PROFIT": "target_profit",
        "BET_SIZING_MODE": "bet_sizing_mode",
        "BASE_ORDER_COST": "base_order_cost",
        "MIN_STAKE": "min_stake",
        "PAPER_SIMULATED_WALLET_BALANCE": "paper_simulated_wallet_balance",
        "MAX_CONSECUTIVE_LOSSES": "max_consecutive_losses",
        "MAX_STAKE": "max_stake",
        "MIN_ENTRY_PRICE": "min_entry_price",
        "MAX_ENTRY_PRICE": "max_entry_price",
        "MAX_PRICE_THRESHOLD": "max_price_threshold",
        "OFI_THRESHOLD": "ofi_threshold",
        "STRATEGY7_OFI_THRESHOLD": "strategy7_ofi_threshold",
        "STRATEGY7_MOMENTUM_THRESHOLD": "strategy7_momentum_threshold",
        "STRATEGY7_MIN_SIGNAL_GAP": "strategy7_min_signal_gap",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "strategy7_confirm_before_entry_seconds",
        "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP": "strategy7_late_confirm_strong_signal_gap",
        "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS": "strategy7_late_confirm_relax_seconds",
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
    }

    INT_CONFIG_KEYS: tuple[str, ...] = (
        "STRATEGY_ID",
        "MAX_CONSECUTIVE_LOSSES",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS",
        "OPEN_DELAY_SECONDS",
        "SIGNAL_FALLBACK_STRATEGY_ID",
        "SIGNAL_HISTORY_FIDELITY_SECONDS",
        "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS",
        "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS",
        "MAX_STAKE_SKIP_ALERT_THRESHOLD",
        "WS_QUOTE_STALE_SECONDS",
        "WS_CONNECT_TIMEOUT_SECONDS",
        "LIVE_AUTO_REDEEM_POLL_SECONDS",
        "LIVE_AUTO_REDEEM_MAX_RETRIES",
        "LIVE_AUTO_REDEEM_INITIAL_BACKOFF_SECONDS",
        "LIVE_AUTO_REDEEM_MAX_BACKOFF_SECONDS",
    )

    FLOAT_CONFIG_KEYS: tuple[str, ...] = (
        "TARGET_PROFIT",
        "BASE_ORDER_COST",
        "MIN_STAKE",
        "PAPER_SIMULATED_WALLET_BALANCE",
        "MAX_STAKE",
        "MIN_ENTRY_PRICE",
        "MAX_ENTRY_PRICE",
        "MAX_PRICE_THRESHOLD",
        "BINANCE_SIGNAL_STALE_SECONDS",
        "OFI_THRESHOLD",
        "STRATEGY7_OFI_THRESHOLD",
        "STRATEGY7_MOMENTUM_THRESHOLD",
        "STRATEGY7_MIN_SIGNAL_GAP",
        "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP",
        "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS",
        "SIGNAL_MOMENTUM_THRESHOLD",
        "SIGNAL_DYNAMIC_THRESHOLD_K",
        "WS_TRADE_GUARD_STALE_SECONDS",
    )

    BOOL_CONFIG_KEYS: tuple[str, ...] = ("LIVE_TRADING_ENABLED", "WS_ENABLED", "LIVE_AUTO_REDEEM_ENABLED", "LIVE_AUTO_REDEEM_DRY_RUN")
    STRING_CONFIG_KEYS: tuple[str, ...] = (
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_FUNDER",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
        "POLYMARKET_BUILDER_API_KEY",
        "POLYMARKET_BUILDER_SECRET",
        "POLYMARKET_BUILDER_PASSPHRASE",
        "POLYMARKET_RELAYER_API_KEY",
        "POLYMARKET_RELAYER_API_KEY_ADDRESS",
    )
    SECRET_CONFIG_KEYS: tuple[str, ...] = (
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_FUNDER",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
        "POLYMARKET_BUILDER_API_KEY",
        "POLYMARKET_BUILDER_SECRET",
        "POLYMARKET_BUILDER_PASSPHRASE",
        "POLYMARKET_RELAYER_API_KEY",
        "POLYMARKET_RELAYER_API_KEY_ADDRESS",
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
        "STRATEGY7_MIN_SIGNAL_GAP": "strategy_7_only",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "strategy_7_only",
        "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP": "strategy_7_only",
        "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS": "strategy_7_only",
    }
    FIELD_HELP: dict[str, str] = {
        "STRATEGY_ID": "策略 1-4 是固定节奏策略，策略 5 是动量策略，策略 6 是 Binance OFI，策略 7/8 是组合信号策略。",
        "STRATEGY_IDS": "统一策略组合。设置后纸面和实盘都使用同一组策略和同一套参数，切换模式只改变执行环境。",
        "LIVE_STRATEGY_IDS": "实盘运行时可轮询的策略列表，按输入顺序去重，例如 2,6。未填写时会回退到 STRATEGY_ID。",
        "PAPER_STRATEGY_IDS": "纸面测试可同时运行多个策略，按输入顺序去重，例如 1,2,6。",
        "TARGET_PROFIT": "在目标收益模式下，这里表示每轮期望净利；在固定金额模式下，它更多用于观察研究，不直接决定首笔下注金额。",
        "BET_SIZING_MODE": "固定金额模式会使用固定首笔下注额；目标收益模式会根据目标净利反推下注金额。",
        "ENABLE_LIVE_TRADING": "关闭时仅运行纸面测试；开启后，纸面交易继续运行，同时启动通过校验的实盘交易。",
        "MARKET_TIMEFRAME": "选择当前要玩的 Polymarket BTC 预测频次，仅支持 5 分钟和 15 分钟。",
        "OPEN_DELAY_SECONDS": "OPEN 模式下，从每轮开始后延迟多少秒再尝试入场。",
        "POLYMARKET_PRIVATE_KEY": "实盘钱包私钥，仅在并行实盘开启时需要。",
        "POLYMARKET_FUNDER": "与私钥对应的实盘钱包地址（0x...），并且需要实际承担实盘订单资金。",
        "POLYMARKET_API_KEY": "CLOB 实盘下单凭证，仅用于实盘下单私有接口。",
        "POLYMARKET_API_SECRET": "CLOB 实盘下单签名密钥，仅用于实盘下单私有接口。",
        "POLYMARKET_API_PASSPHRASE": "CLOB 实盘下单通行口令，仅用于实盘下单私有接口。",
        "POLYMARKET_BUILDER_API_KEY": "官方免 Gas 赎回的 Builder 接口密钥，仅用于自动赎回。",
        "POLYMARKET_BUILDER_SECRET": "官方免 Gas 赎回的 Builder 签名密钥，仅用于自动赎回。",
        "POLYMARKET_BUILDER_PASSPHRASE": "官方免 Gas 赎回的 Builder 口令，仅用于自动赎回。",
        "POLYMARKET_RELAYER_API_KEY": "官方免 Gas 赎回的 Relayer 接口密钥，仅用于自动赎回。",
        "POLYMARKET_RELAYER_API_KEY_ADDRESS": "与 Relayer 接口密钥配套的地址，仅用于自动赎回认证。",
        "LIVE_AUTO_REDEEM_ENABLED": "仅并行实盘使用。开启后会启动独立的自动赎回线程，扫描可赎回的获胜仓位并自动尝试赎回。",
        "LIVE_AUTO_REDEEM_DRY_RUN": "仅在开启自动赎回后才有意义。设为 true 时，自动赎回线程仍会检测仓位、写入状态并更新监控页面，但不会发送真实的 Polygon 链上赎回交易。正常实盘请保持 false，只在首次验证或调试时临时开启。",
        "LIVE_AUTO_REDEEM_POLL_SECONDS": "检查可赎回实盘仓位的轮询间隔，单位秒。",
        "LIVE_AUTO_REDEEM_MAX_RETRIES": "单个 condition 在遇到临时 RPC 或链上错误时的最大重试次数。",
        "LIVE_AUTO_REDEEM_INITIAL_BACKOFF_SECONDS": "自动赎回失败后的首次退避等待秒数。",
        "LIVE_AUTO_REDEEM_MAX_BACKOFF_SECONDS": "自动赎回重试之间的最大退避上限秒数。",
        "BASE_ORDER_COST": "仅固定金额模式使用；获胜后策略会重置回这个起始下注金额。",
        "MIN_STAKE": "单笔订单允许投入的最小 USDC；低于它时会跳过本轮。",
        "PAPER_SIMULATED_WALLET_BALANCE": "仅纸面模式使用，作为 dry-run 的模拟钱包余额；纸面不会读取真实钱包，但会经过与实盘相同的预算检查节点。",
        "MAX_CONSECUTIVE_LOSSES": "连续亏损达到这个次数后，策略会执行一次止损重置。",
        "MAX_STAKE": "单笔订单允许投入的最大 USDC；超过后会直接跳过本轮。",
        "MIN_ENTRY_PRICE": "目标方向价格低于该值时不入场；留空则不设置下限。",
        "MAX_ENTRY_PRICE": "目标方向价格高于该值时不入场。",
        "MAX_PRICE_THRESHOLD": "目标方向价格高于该阈值时不入场。",
        "OFI_THRESHOLD": "策略 6 的 Binance OFI 最小强度要求，低于该阈值直接跳过。",
        "STRATEGY7_OFI_THRESHOLD": "策略 7 对 Binance OFI 的最小强度要求，低于该阈值直接跳过。",
        "STRATEGY7_MOMENTUM_THRESHOLD": "策略 7 对 Polymarket 轮内动量确认的最小要求。",
        "STRATEGY7_MIN_SIGNAL_GAP": "策略 7 要求 OFI 和动量超过阈值的最小额外优势，避免擦线交易。",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "策略 7 需要在计划入场前至少提前这么多秒完成双信号确认。",
        "STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP": "策略 7 只有在 OFI 和动量都额外强于阈值时，才允许走晚确认放宽通道。",
        "STRATEGY7_LATE_CONFIRM_RELAX_SECONDS": "满足强信号条件后，可从策略 7 的最晚确认要求里减去的秒数。",
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
        if cfg.strategy_id not in {6, 7, 8} and not any(strategy_id in {6, 7, 8} for strategy_id in strategy_ids):
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
            profile_map = (
                getattr(self._cfg, "live_profiles", {})
                if mode == "live"
                else getattr(self._cfg, "paper_strategy_profiles", {})
            )
            profile = profile_map.get(int(strategy_id_text))
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
                prefixed_key = f"{_strategy_profile_prefix(mode, strategy_text)}_{base_key}"
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

        redeem_cfg = validated_live_cfg if validated_live_cfg is not None else self._build_config(validation_values)
        live_strategy_ids = [str(item) for item in (getattr(redeem_cfg, "live_strategy_ids", None) or [redeem_cfg.strategy_id])]
        live_session_state = load_session_state(
            redeem_cfg.logs_dir / "live_session_state.json",
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
        redeem_state = load_live_redeem_state(redeem_cfg.logs_dir / "live_redeem_state.json")
        redeem_runtime = redeem_state.get("runtime") if isinstance(redeem_state, dict) else {}
        redeem_runtime = redeem_runtime if isinstance(redeem_runtime, dict) else {}
        redeem_conditions = redeem_state.get("conditions") if isinstance(redeem_state, dict) else {}
        redeem_conditions = redeem_conditions if isinstance(redeem_conditions, dict) else {}
        redeem_pending_count = redeem_runtime.get("pending_redeem_count")
        if redeem_pending_count in (None, ""):
            redeem_pending_count = sum(
                1
                for raw_entry in redeem_conditions.values()
                if isinstance(raw_entry, dict) and str(raw_entry.get("status") or "pending").strip().lower() not in {"completed", "terminal_error"}
            )
        try:
            redeem_pending_count = max(0, int(redeem_pending_count or 0))
        except (TypeError, ValueError):
            redeem_pending_count = 0
        redeem_enabled = bool(getattr(redeem_cfg, "live_auto_redeem_enabled", False) or redeem_runtime.get("enabled"))
        redeem_visible = bool(redeem_enabled or active_mode == "live" or saved_mode == "live")
        optimizer_runtime = _load_optimizer_runtime(redeem_cfg.logs_dir / "optimizer_state.json")

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
            "redeem_visible": redeem_visible,
            "redeem_enabled": redeem_enabled,
            "redeem_auth_mode": getattr(redeem_cfg, "live_redeem_auth_mode", "unconfigured"),
            "redeem_pending_count": redeem_pending_count,
            "redeem_last_result": redeem_runtime.get("last_result") or None,
            "redeem_last_attempt_at": redeem_runtime.get("last_attempt_at") or None,
            "redeem_last_submission_id": redeem_runtime.get("last_submission_id") or None,
            "redeem_last_submission_status": redeem_runtime.get("last_submission_status") or None,
            "redeem_last_tx_hash": redeem_runtime.get("last_tx_hash") or None,
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
        return merged, validation_errors

    def get_config_payload(self) -> dict[str, Any]:
        with self._lock:
            env_values, validation_errors = self._merged_env_values()
            for key, value in self._env_values.items():
                if _split_strategy_profile_key(key) is not None:
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
                    "target_profit": _fmt_env(profile.target_profit),
                    "bet_sizing_mode": profile.bet_sizing_mode,
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
                    "strategy7_max_entry_price": _fmt_env(profile.strategy7_max_entry_price),
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

        for key, value in values.items():
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
            _write_env_file(self.env_file, self._env_values)
            self._last_saved_at = datetime.now(timezone.utc)

        previous_mode = None
        next_mode = None
        previous_timeframe = None
        with self._lock:
            previous_mode = str(self._cfg.trade_mode or 'paper').strip().lower() or 'paper'
            previous_timeframe = getattr(self._cfg, 'market_timeframe', '5m')
            next_mode = str(self._env_values.get('TRADE_MODE') or self._cfg.trade_mode or 'paper').strip().lower() or 'paper'
        self._refresh_runtime()
        next_timeframe = getattr(self._cfg, 'market_timeframe', '5m')
        if self.notify_runtime_reload is not None and previous_timeframe != next_timeframe:
            self.notify_runtime_reload('market_timeframe')
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
                    target_profit=effective_cfg.target_profit,
                    min_price_threshold=getattr(effective_cfg, 'min_price_threshold', None),
                    max_price_threshold=effective_cfg.max_price_threshold,
                    max_stake=effective_cfg.max_stake,
                    max_consecutive_losses=effective_cfg.max_consecutive_losses,
                    min_stake=getattr(effective_cfg, "min_stake", None),
                    min_entry_price=getattr(effective_cfg, "min_entry_price", getattr(effective_cfg, "min_price_threshold", None)),
                    max_entry_price=getattr(effective_cfg, "max_entry_price", effective_cfg.max_price_threshold),
                    bet_sizing_mode=effective_cfg.bet_sizing_mode,
                    base_order_cost=effective_cfg.base_order_cost,
                )
                plan = {
                    "should_trade": plan_obj.should_trade,
                    "side": plan_obj.side,
                    "price": plan_obj.price,
                    "order_size": plan_obj.order_size,
                    "order_cost": plan_obj.order_cost,
                    "expected_profit": plan_obj.expected_profit,
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
                "enabled": selected_strategy in {7, 8},
                "ofi_score": quote.strategy6_ofi_score,
                "momentum_delta": side_decision.signal_delta,
                "agreement": (
                    "agree"
                    if selected_strategy in {7, 8} and side_decision.side in {"UP", "DOWN"} and side_decision.reason not in {"strategy7_weighted_conflict", "strategy8_conflict_reversal"}
                    else ("conflict" if side_decision.reason in {"strategy7_signal_conflict", "strategy7_weighted_conflict", "strategy8_conflict_reversal"} else None)
                ),
                "quality_gate": (
                    "passed"
                    if selected_strategy in {7, 8} and side_decision.side in {"UP", "DOWN"}
                    else (side_decision.reason if selected_strategy in {7, 8} else None)
                ),
                "final_reason": side_decision.reason,
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
                    filtered_csv = paper_csv.with_name(f"{paper_csv.stem}_strategy_{strategy_filter}_summary{paper_csv.suffix}")
                    with filtered_csv.open("w", newline="", encoding="utf-8") as handle:
                        writer = csv.DictWriter(handle, fieldnames=list(filtered_rows[0].keys()))
                        writer.writeheader()
                        writer.writerows(filtered_rows)
                    try:
                        daily = summarize_paper_trades(filtered_csv, tz_offset="+08:00")
                        strategy_daily = summarize_paper_trades_by_strategy(filtered_csv, tz_offset="+08:00")
                    finally:
                        if filtered_csv.exists():
                            filtered_csv.unlink()
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
                filtered_csv = live_csv.with_name(f"{live_csv.stem}_summary_work{live_csv.suffix}")
                with filtered_csv.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=_csv_fieldnames_for_rows(summary_rows), extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(summary_rows)
                try:
                    daily = summarize_paper_trades(filtered_csv, tz_offset="+08:00")
                    strategy_daily = summarize_paper_trades_by_strategy(filtered_csv, tz_offset="+08:00")
                finally:
                    if filtered_csv.exists():
                        filtered_csv.unlink()
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
        rows = _tail_csv_rows(paper_csv, limit=capped_limit * 4)
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
        merged_rows.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
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
        explicit_all_strategy_filter = _is_explicit_all_strategy_filter(strategy)
        rows = _tail_csv_rows(live_csv, limit=capped_limit * 6)
        if strategy_filter is None and explicit_live_strategy_scope and not explicit_all_strategy_filter:
            rows = _filter_trade_rows_by_strategy_ids(rows, effective_live_strategy_ids)
        elif strategy_filter is not None:
            rows = _filter_trade_rows_by_strategy(rows, strategy_filter)
        rows = _collapse_live_recent_rows(rows)
        rows = rows[:capped_limit]
        client = PolymarketClient(cfg)
        try:
            rows = [
                _validate_recent_trade_row(
                    row,
                    client=client,
                    validation_cache=validation_cache,
                    fill_missing_result=True,
                )
                for row in rows
            ]
            if any(row.get("result_check_status") == "mismatch" for row in rows):
                corrected_count = _auto_reconcile_live_ledger(
                    live_csv=live_csv,
                    state_path=cfg.logs_dir / "live_session_state.json",
                    client=client,
                    validation_cache=validation_cache,
                    active_strategy_id=cfg.strategy_id,
                )
                if corrected_count > 0:
                    rows = _tail_csv_rows(live_csv, limit=capped_limit * 6)
                    if strategy_filter is None and explicit_live_strategy_scope and not explicit_all_strategy_filter:
                        rows = _filter_trade_rows_by_strategy_ids(rows, effective_live_strategy_ids)
                    elif strategy_filter is not None:
                        rows = _filter_trade_rows_by_strategy(rows, strategy_filter)
                    rows = _collapse_live_recent_rows(rows)
                    rows = rows[:capped_limit]
                    rows = [
                        _validate_recent_trade_row(
                            row,
                            client=client,
                            validation_cache=validation_cache,
                            fill_missing_result=True,
                        )
                        for row in rows
                    ]
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
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
    if 'MIN_PRICE_THRESHOLD' not in DashboardState.EDITABLE_CONFIG_KEYS:
        editable = list(DashboardState.EDITABLE_CONFIG_KEYS)
        editable.insert(editable.index('MAX_PRICE_THRESHOLD'), 'MIN_PRICE_THRESHOLD')
        DashboardState.EDITABLE_CONFIG_KEYS = tuple(editable)

    DashboardState.CONFIG_LABELS['MIN_PRICE_THRESHOLD'] = '最低买入价格阈值'
    DashboardState.CONFIG_ATTR_MAP['MIN_PRICE_THRESHOLD'] = 'min_price_threshold'
    DashboardState.FIELD_HELP['MIN_PRICE_THRESHOLD'] = '目标方向价格低于此阈值就不入场，和最高价格阈值一起构成允许入场的价格区间。'

    if 'MIN_PRICE_THRESHOLD' not in DashboardState.FLOAT_CONFIG_KEYS:
        float_keys = list(DashboardState.FLOAT_CONFIG_KEYS)
        float_keys.insert(float_keys.index('MAX_PRICE_THRESHOLD'), 'MIN_PRICE_THRESHOLD')
        DashboardState.FLOAT_CONFIG_KEYS = tuple(float_keys)

    for group in DashboardState.FIELD_GROUPS:
        keys = group.get('keys') or []
        if 'MAX_PRICE_THRESHOLD' in keys and 'MIN_PRICE_THRESHOLD' not in keys:
            keys.insert(keys.index('MAX_PRICE_THRESHOLD'), 'MIN_PRICE_THRESHOLD')
            break


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
              <select id="configModeSelect" class="input-compact" aria-label="左侧配置视角选择" title="配置视角">
                <option value="paper">纸面视角</option>
                <option value="live">实盘视角</option>
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
          <div id="recentPanelDesc" class="section-desc">按时间倒序显示最近 80 条记录 · 当前策略：全部</div>
          <div class=\"report-recent-table table-wrap\">
            <table>
              <thead>
                <tr>
                  <th>时间</th>
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
  paperRuntimeCards: {},
  marketStrategyFilter: 'all',
  paperReportStrategyFilter: 'all',
  paperSummaryStrategyFilter: null,
  paperRecentStrategyFilter: null,
  reportMode: 'paper',
  paperTimeframeFilter: '',
  countdownSnapshotAtMs: null,
  countdownBaseSeconds: null,
  showInternalKeys: false,
  runtimeDetailsOpen: false,
  diagnosticsOpen: false,
  signalDetailsOpen: false,
  advancedConfigOpen: false,
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
    intro: '先确认基础策略、下注模式和单笔最大下注金额，再连续观察 3-5 轮后再决定是否同时改多个参数。',
    sections: [
      {
        title: '实盘自动赎回',
        bullets: [
          '实盘自动赎回开关只在并行实盘开启后生效，开启后会启动独立的自动赎回线程。',
          '该线程会扫描可赎回的获胜仓位，并通过 Polygon 的 redeemPositions 方法尝试把它们转回可用余额。',
          '当自动赎回演练模式开启时，自动赎回线程只做检测、记录和监控页面展示，不会发送真实赎回交易。',
          '正常实盘请保持自动赎回演练模式关闭，只在首次验证或调试时临时打开。',
        ],
      },
      {
        title: '先看哪里',
        bullets: [
          '先看行情与信号，确认当前轮次、方向判断和倒计时是否正常。',
          '再看下注计划与风控，确认当前是否准备下注、为什么跳过、以及预期收益是多少。',
          '然后看会话状态，关注累计盈亏、待回补亏损和连续亏损轮数。',
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
          '重点关注基础策略、下注模式、风控边界，以及哪些字段只对策略 5 生效。',
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
          '用于看累计收益和当前恢复状态。',
          '重点关注累计盈亏、待回补亏损、连续亏损轮数和当日已实现盈亏。',
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
          '纸面和实盘都会先经过同一套下注计划检查：价格、下注金额、连续亏损、下注模式和止损重置都会先判断。',
          'MAX_CONSECUTIVE_LOSSES 控制连续亏损达到多少轮后触发止损重置，重置会清空待回补亏损和连续亏损计数。',
          'MAX_STAKE 是单笔最大下注金额；计算出的 order_cost 超过它时，本轮会跳过并显示 order_cost_above_max_stake。',
          'MAX_PRICE_THRESHOLD 限制目标方向最高买入价；MIN_PRICE_THRESHOLD 可选限制最低买入价。',
          'BET_SIZING_MODE 决定下注金额计算方式；FIXED_BASE_COST 从 BASE_ORDER_COST 起步，TARGET_PROFIT 按目标利润反推下注额。',
        ],
      },
      {
        title: '纸面与实盘差异',
        bullets: [
          '纸面模式不会读取真实钱包，会使用 PAPER_SIMULATED_WALLET_BALANCE 作为 dry-run 钱包预算。',
          '并行实盘会读取真实钱包余额；余额不可用会显示“实盘钱包余额不可用”，余额不足会显示“实盘钱包余额不足”。',
          '纸面和实盘的信号、风控和预算检查路径应保持一致，差异只在最终是否发送真实订单。',
        ],
      },
      {
        title: '常见跳过原因',
        bullets: [
          'price_above_threshold 表示目标方向价格高于 MAX_PRICE_THRESHOLD；price_below_threshold 表示低于 MIN_PRICE_THRESHOLD。',
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
          '策略 7 同向时加分，冲突时按 OFI 60% / 动量 40% 加权，仍受 STRATEGY7_OFI_THRESHOLD、STRATEGY7_MOMENTUM_THRESHOLD、MAX_ENTRY_PRICE 和确认时间限制影响。',
        ],
      },
      {
        title: '重置与告警',
        bullets: [
          '每次亏损会增加 recovery_loss 和 consecutive_losses；获胜后会清空 recovery_loss 和 consecutive_losses。',
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
  ['为什么触发最大下注金额后会连续跳过？', '待回补亏损和当前价格条件可能会把本轮所需金额推高到最大下注金额之上，需要结合待回补亏损一起判断。'],
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
  },
  LIVE_TRADING_ENABLED: {
    true: '开启',
    false: '关闭',
  },
  BET_SIZING_MODE: {
    FIXED_BASE_COST: '固定金额模式',
    TARGET_PROFIT: '目标收益模式',
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
  invalid_base_order_cost: '固定起始下注金额无效',
  invalid_bet_sizing_mode: '下注模式无效',
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
  strategy7_weighted_conflict: '策略7 冲突信号按权重确认',
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
  live_fok_not_filled: '策略7 实时 FOK 订单未成交',
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
  ENABLE_LIVE_TRADING: '并行实盘',
  TRADE_MODE: '运行视角',
  LIVE_TRADING_ENABLED: '并行实盘开关',
  POLYMARKET_PRIVATE_KEY: '实盘私钥',
  POLYMARKET_FUNDER: '\u5b9e\u76d8\u94b1\u5305\u5730\u5740',
  STRATEGY_ID: '基础策略',
  STRATEGY_IDS: '统一策略组合',
  TARGET_PROFIT: '每次目标净利',
  BET_SIZING_MODE: '下注模式',
  BASE_ORDER_COST: '固定起始下注金额',
  MIN_STAKE: '单笔最小下注金额',
  PAPER_SIMULATED_WALLET_BALANCE: '纸面模拟钱包余额',
  MAX_CONSECUTIVE_LOSSES: '连亏重置轮数',
  MAX_STAKE: '单笔最大下注金额',
  MIN_ENTRY_PRICE: '最低买入价格',
  MAX_ENTRY_PRICE: '最高买入价格',
  MAX_PRICE_THRESHOLD: '最高买入价格阈值',
  STRATEGY7_OFI_THRESHOLD: '策略7 盘口失衡阈值',
  STRATEGY7_MOMENTUM_THRESHOLD: '策略7 动量阈值',
  STRATEGY7_MIN_SIGNAL_GAP: '策略7 最小信号优势',
  STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS: '策略7 最晚确认秒数',
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
  } catch (_err) {
    state.showInternalKeys = false;
    state.reportMode = 'paper';
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

function resultCheckText(status) {
  if (status === 'match') return '已对官方';
  if (status === 'mismatch') return '与官方不符';
  if (status === 'pending') return '待结算';
  if (status === 'official') return '页面补官方';
  if (status === 'official_pending') return '待官方结算';
  if (status === 'error') return '校验异常';
  return '--';
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

function activeConfigModeFromValues(payload, values) {
  const envValues = (payload && payload.env_values) || {};
  const mergedValues = {
    ...envValues,
    ...(values || {}),
  };
  return buildLiveToggleValue(mergedValues) === 'true' ? 'live' : 'paper';
}

function strategyListKeyForMode(mode) {
  const normalized = String(mode || '').toLowerCase();
  return normalized === 'live' ? 'LIVE_STRATEGY_IDS' : 'PAPER_STRATEGY_IDS';
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
  };
}

function collectUnifiedStrategyValues(payload, currentValues) {
  const unified = resolveUnifiedStrategySelection(payload || state.config || {}, currentValues || {});
  return {
    STRATEGY_ID: unified.focus,
    [unified.multiKey]: unified.selected.join(','),
  };
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
  summary.textContent = '\u6b63\u5728\u7f16\u8f91' + formatModeLabel(activeConfigModeFromValues(payload, values)) + '\u7b56\u7565\u7ec4\u5408\uff08' + unified.multiKey + '\uff09\uff0c\u7eb8\u9762\u548c\u5b9e\u76d8\u4e92\u4e0d\u5171\u4eab\u3002';
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
        ' data-strategy-config-explicit="' + (explicit ? 'true' : 'false') + '"' +
        ' data-strategy-config-inherited-value="' + esc(inheritedValue) + '"';
      const control = options.length > 0
        ? ('<select class="input-compact"' + attrs + '>' + options.map((opt) => {
            const selectedAttr = String(opt) === value ? ' selected' : '';
            return '<option value="' + esc(opt) + '"' + selectedAttr + '>' + esc(strategyOptionLabel(baseKey, opt, payload)) + '</option>';
          }).join('') + '</select>')
        : ('<input class="input-compact" type="text" value="' + esc(value) + '"' + attrs + '>');
      const chip = explicit ? '<span class="chip warn">单独配置</span>' : '<span class="chip">继承全局</span>';
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

function configuredPaperReportStrategyOptions() {
  const payload = state.config || {};
  const envValues = (payload && payload.env_values) || {};
  const multiKey = strategyListKeyForMode(effectiveReportMode());
  const selectOptions = ((payload || {}).select_options) || {};
  const strategyOptions = ((selectOptions[multiKey] || selectOptions.STRATEGY_ID || selectOptions.PAPER_STRATEGY_IDS || [])).map((item) => String(item));
  const draftValues = currentUnifiedStrategyDraftForReport();
  const baseStrategyIds = String((draftValues && draftValues[multiKey]) || envValues[multiKey] || '').trim();
  const baseStrategyId = (draftValues && draftValues.STRATEGY_ID) ?? envValues.STRATEGY_ID ?? '';
  if (baseStrategyIds) {
    return resolveUnifiedStrategySelection(payload, {
      STRATEGY_ID: baseStrategyId,
      [multiKey]: baseStrategyIds,
    }).selected;
  }
  if (multiKey === 'LIVE_STRATEGY_IDS') {
    const runtimeStrategyIds = (((payload || {}).runtime_status || {}).live_strategy_ids || []).map((item) => String(item));
    if (runtimeStrategyIds.length > 0) {
      return runtimeStrategyIds;
    }
    return baseStrategyId ? [String(baseStrategyId)] : [];
  }
  const timeframe = effectivePaperTimeframeFilter();
  const profiles = (payload && payload.paper_profiles) || {};
  const profile = profiles[timeframe] || {};
  const profileStrategyIds = Array.isArray(profile.paper_strategy_ids)
    ? profile.paper_strategy_ids.map((item) => String(item)).join(',')
    : '';
  const profileSelected = resolveUnifiedStrategySelection(payload, {
    STRATEGY_ID: profile.strategy_id ?? envValues.STRATEGY_ID ?? '',
    PAPER_STRATEGY_IDS: profileStrategyIds,
  }).selected;
  const selectedSet = new Set(profileSelected.map((item) => String(item)));
  const ordered = strategyOptions.filter((item) => selectedSet.has(String(item)));
  const extras = [...selectedSet].filter((item) => ordered.indexOf(item) < 0);
  return [...ordered, ...extras];
}

function normalizePaperReportStrategyFilter(value, options) {
  const raw = String(value || 'all');
  return options.indexOf(raw) >= 0 ? raw : 'all';
}

function paperReportStrategyOptions() {
  const payload = state.config || {};
  const multiKey = strategyListKeyForMode(effectiveReportMode());
  const selectOptions = ((payload || {}).select_options) || {};
  const strategyOptions = ((selectOptions[multiKey] || selectOptions.STRATEGY_ID || selectOptions.PAPER_STRATEGY_IDS || [])).map((item) => String(item));
  const strategyView = ((state.market || {}).strategy_view) || {};
  const available = Array.isArray(strategyView.available) ? strategyView.available.map((item) => String(item)) : [];
  const configured = configuredPaperReportStrategyOptions();
  const merged = configured.length > 0 ? configured : available;
  const selectedSet = new Set(merged.filter((item) => item && item !== 'all').map((item) => String(item)));
  const ordered = strategyOptions.filter((item) => selectedSet.has(String(item)));
  const extras = [...selectedSet].filter((item) => ordered.indexOf(item) < 0);
  return ['all', ...ordered, ...extras];
}

function defaultPaperReportStrategyFilter() {
  const configured = configuredPaperReportStrategyOptions();
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
    return '\u6309\u65f6\u95f4\u5012\u5e8f\u663e\u793a\u6700\u8fd1 80 \u6761\u8bb0\u5f55 \u00b7 \u5f53\u524d\u6a21\u5f0f\uff1a' + modeText + timeframeText + ' \u00b7 \u5f53\u524d\u7b56\u7565\uff1a\u5168\u90e8';
  }
  return '\u6309\u65f6\u95f4\u5012\u5e8f\u663e\u793a\u6700\u8fd1 80 \u6761\u8bb0\u5f55 \u00b7 \u5f53\u524d\u6a21\u5f0f\uff1a' + modeText + timeframeText + ' \u00b7 \u5f53\u524d\u7b56\u7565\uff1a\u7b56\u7565 ' + strategy;
  if (!strategy || strategy === 'all') {
    return '按时间倒序显示最近 80 条记录 · 当前频次：' + timeframe + ' · 当前策略：全部';
  }
  return '按时间倒序显示最近 80 条记录 · 当前频次：' + timeframe + ' · 当前策略：策略 ' + strategy;
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
  return;
  const timeframes = Array.isArray((payload || {}).paper_timeframes) ? payload.paper_timeframes : [];
  const profiles = ((payload || {}).paper_profiles) || {};
  const envValues = ((payload || {}).env_values) || {};
  const selectOptions = ((payload || {}).select_options) || {};
  const labels = ((payload || {}).labels) || {};
  const enabled = parsePaperTimeframeList(envValues.PAPER_TIMEFRAMES || timeframes.join(','));
  if (!timeframes.length) {
    node.innerHTML = '';
    return;
  }

  const timeframeToggleHtml = timeframes.map((timeframe) => {
    const checked = enabled.indexOf(String(timeframe).toLowerCase()) >= 0 ? ' checked' : '';
    return '<label class="strategy-panel-toggle"><input type="checkbox" data-paper-timeframe="' + esc(timeframe) + '"' + checked + '>运行 ' + esc(paperTimeframeLabel(timeframe)) + '</label>';
  }).join('');

  const fieldNames = [
    'STRATEGY_ID',
    'STRATEGY_IDS',
    'TARGET_PROFIT',
    'BET_SIZING_MODE',
    'BASE_ORDER_COST',
    'MIN_STAKE',
    'MAX_CONSECUTIVE_LOSSES',
    'MAX_STAKE',
    'MIN_ENTRY_PRICE',
    'MAX_ENTRY_PRICE',
    'OPEN_DELAY_SECONDS',
    'SIGNAL_MOMENTUM_THRESHOLD',
    'OFI_THRESHOLD',
    'BINANCE_SIGNAL_STALE_SECONDS',
    'STRATEGY7_OFI_THRESHOLD',
    'STRATEGY7_MOMENTUM_THRESHOLD',
  ];

  node.innerHTML = ''
    + '<section class="strategy-guide-card">'
    +   '<div class="strategy-guide-head">'
    +     '<div><div class="strategy-guide-title">纸面配置组</div><div class="strategy-guide-subtitle">按时间频次独立编辑纸面配置。</div></div>'
    +     '<span class="chip ok">已配置</span>'
    +   '</div>'
    +   '<div class="rows">' + timeframeToggleHtml + '<input id="cfg_PAPER_TIMEFRAMES" type="hidden" value="' + esc(enabled.join(',')) + '"></div>'
    + '</section>'
    + timeframes.map((timeframe) => {
      const profile = profiles[timeframe] || {};
      const lowerTimeframe = String(timeframe).toLowerCase();
      const hiddenStyle = enabled.indexOf(lowerTimeframe) >= 0 ? '' : ' style="display:none"';
      const fieldsHtml = fieldNames.map((fieldName) => {
        const scopedKey = 'PAPER_' + String(timeframe).toUpperCase() + '_' + fieldName;
        const label = (labels && labels[scopedKey]) || scopedKey;
        const normalizedValue = fieldName === 'STRATEGY_IDS' && Array.isArray(profile.paper_strategy_ids)
          ? String(envValues[scopedKey] ?? profile.paper_strategy_ids.join(','))
          : String(envValues[scopedKey] ?? profile[
              fieldName === 'STRATEGY_ID' ? 'strategy_id'
              : fieldName === 'TARGET_PROFIT' ? 'target_profit'
              : fieldName === 'BET_SIZING_MODE' ? 'bet_sizing_mode'
              : fieldName === 'BASE_ORDER_COST' ? 'base_order_cost'
              : fieldName === 'MAX_CONSECUTIVE_LOSSES' ? 'max_consecutive_losses'
              : fieldName === 'MIN_STAKE' ? 'min_stake'
              : fieldName === 'MAX_STAKE' ? 'max_stake'
              : fieldName === 'MIN_ENTRY_PRICE' ? 'min_entry_price'
              : fieldName === 'MAX_ENTRY_PRICE' ? 'max_entry_price'
              : fieldName === 'OPEN_DELAY_SECONDS' ? 'open_delay_seconds'
              : fieldName === 'SIGNAL_MOMENTUM_THRESHOLD' ? 'signal_momentum_threshold'
              : fieldName === 'OFI_THRESHOLD' ? 'ofi_threshold'
              : fieldName === 'BINANCE_SIGNAL_STALE_SECONDS' ? 'binance_signal_stale_seconds'
              : fieldName === 'STRATEGY7_OFI_THRESHOLD' ? 'strategy7_ofi_threshold'
              : 'strategy7_momentum_threshold'
            ] ?? '');
        const options = selectOptions[scopedKey] || selectOptions[fieldName] || null;
        const controlHtml = Array.isArray(options)
          ? ('<select id="cfg_' + esc(scopedKey) + '">' + options.map((opt) => {
              const selected = String(opt) === normalizedValue ? ' selected' : '';
              return '<option value="' + esc(opt) + '"' + selected + '>' + esc(String(opt)) + '</option>';
            }).join('') + '</select>')
          : ('<input id="cfg_' + esc(scopedKey) + '" type="text" value="' + esc(normalizedValue) + '">');
        return '<div class="field"><label for="cfg_' + esc(scopedKey) + '">' + esc(label) + '</label>' + controlHtml + '</div>';
      }).join('');
      return ''
        + '<section class="strategy-guide-card" data-paper-profile="' + esc(timeframe) + '"' + hiddenStyle + '>'
        +   '<div class="strategy-guide-head">'
        +     '<div><div class="strategy-guide-title">' + esc(paperTimeframeLabel(timeframe)) + ' 纸面配置</div><div class="strategy-guide-subtitle">独立纸面配置</div></div>'
        +     '<span class="chip ok">可编辑</span>'
        +   '</div>'
        +   '<div class="group-grid">' + fieldsHtml + '</div>'
        + '</section>';
    }).join('');

  const hiddenInput = el('cfg_PAPER_TIMEFRAMES');
  const syncVisibility = () => {
    const selected = Array.from(node.querySelectorAll('input[data-paper-timeframe]'))
      .filter((checkbox) => checkbox.checked)
      .map((checkbox) => String(checkbox.getAttribute('data-paper-timeframe') || '').toLowerCase());
    if (hiddenInput) {
      hiddenInput.value = selected.join(',');
    }
    node.querySelectorAll('[data-paper-profile]').forEach((section) => {
      const sectionTimeframe = String(section.getAttribute('data-paper-profile') || '').toLowerCase();
      section.style.display = selected.indexOf(sectionTimeframe) >= 0 ? '' : 'none';
    });
  };
  node.querySelectorAll('input[data-paper-timeframe]').forEach((checkbox) => {
    checkbox.addEventListener('change', syncVisibility);
  });
  syncVisibility();
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
  return normalized.startsWith('STRATEGY7_');
}

function applyAdvancedConfigVisibility(values) {
  const panel = el('advancedConfigPanel');
  if (!panel) {
    return;
  }
  const strategyId = String(resolveUnifiedStrategySelection(state.config || {}, values || {}).focus || '');
  const isStrategyFive = strategyId === '5';
  const isStrategySeven = strategyId === '7';

  panel.querySelectorAll('.config-group').forEach((section) => {
    const advancedGroup = section.dataset.advancedGroup === 'true';
    section.style.display = advancedGroup && panel.hidden ? 'none' : '';
  });

  panel.querySelectorAll('.field[data-advanced-field]').forEach((field) => {
    const scope = field.dataset.fieldScope || 'all';
    const shouldShow =
      scope === 'strategy_5_only' ? isStrategyFive
      : scope === 'strategy_7_only' ? isStrategySeven
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
    'TARGET_PROFIT',
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
    'WS_CONNECT_TIMEOUT_SECONDS'
  ].indexOf(String(key || '')) >= 0;
}

function buildLiveToggleValue(values) {
  const mode = String(values.TRADE_MODE || 'paper').toLowerCase();
  const enabled = String(values.LIVE_TRADING_ENABLED || 'false').toLowerCase();
  return mode === 'live' && enabled === 'true' ? 'true' : 'false';
}

function effectiveConfigMode(payload) {
  const envValues = (payload && payload.env_values) || {};
  return String(envValues.TRADE_MODE || 'paper').toLowerCase() === 'live' ? 'live' : 'paper';
}

function renderTaskflowVisibility(mode) {
  const normalizedMode = String(mode || 'paper').toLowerCase() === 'live' ? 'live' : 'paper';
  const paperRoot = el('paperTaskflowRoot');
  const liveRoot = el('liveTaskflowRoot');
  if (paperRoot) {
    paperRoot.hidden = normalizedMode !== 'paper';
  }
  if (liveRoot) {
    liveRoot.hidden = normalizedMode !== 'live';
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
    const nextMode = String(selectNode.value || 'paper').toLowerCase() === 'live' ? 'live' : 'paper';
    const modeField = el('cfg_TRADE_MODE');
    if (modeField) {
      modeField.value = nextMode;
    }
    state.config = {
      ...(state.config || {}),
      env_values: {
        ...(((state.config || {}).env_values) || {}),
        TRADE_MODE: nextMode,
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
  const redeemVisible = !!(payload.redeem_visible || payload.redeem_enabled || ((payload.running_mode || payload.active_mode || 'paper') === 'live'));
  setDisplay('runtimeRedeemRows', redeemVisible ? '' : 'none');
  setText('runtimeRedeemEnabled', payload.redeem_enabled ? '已开启' : '未开启');
  setText('runtimeRedeemPending', String(payload.redeem_pending_count ?? 0));
  setText('runtimeRedeemResult', payload.redeem_last_result || '--');
  setText('runtimeRedeemAttempt', payload.redeem_last_attempt_at ? fmtIso(payload.redeem_last_attempt_at) : '--');
  setText('runtimeRedeemTxHash', payload.redeem_last_tx_hash || '--');
  setText('runtimeOptimizerEnabled', payload.optimizer_enabled ? '已开启' : '未开启');
  setText('runtimeOptimizerChampion', payload.optimizer_champion_id || '--');
  setText('runtimeOptimizerChallengers', String((payload.optimizer_active_challengers || []).length));
  setText('runtimeOptimizerPromotable', String(payload.optimizer_promotable_count ?? 0));
  setText('runtimeOptimizerLastRun', payload.optimizer_last_run_at ? fmtIso(payload.optimizer_last_run_at) : '--');
  setHtml('runtimeOptimizerChallengerList', renderOptimizerCandidateList(payload.optimizer_active_challengers || []));
  setHtml('runtimeOptimizerPromotableList', renderOptimizerCandidateList(payload.optimizer_promotable_candidates || []));
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


function shouldConfirmLiveModeSwitch(previousMode, nextMode) {
  previousMode = String(previousMode || 'paper').toLowerCase();
  nextMode = String(nextMode || 'paper').toLowerCase();
  return previousMode !== 'live' && nextMode === 'live';
}

function renderConfig(payload) {
  state.config = payload;
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
  document.querySelectorAll('[data-strategy-config-key]').forEach((node) => {
    const key = String(node.getAttribute('data-strategy-config-key') || '');
    if (!key) {
      return;
    }
    const value = String(node.value ?? '').trim();
    const inheritedValue = String(node.getAttribute('data-strategy-config-inherited-value') || '');
    const explicit = String(node.getAttribute('data-strategy-config-explicit') || '') === 'true';
    if (explicit || value !== inheritedValue) {
      payload[key] = value;
    }
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
  payload.STRATEGY_ID = unifiedValues.STRATEGY_ID;
  payload[multiKey] = unifiedValues[multiKey];
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
    if (shouldConfirmLiveModeSwitch(previousMode, nextMode) && !window.confirm('开启并行实盘后，纸面交易会继续运行，同时实盘会按实盘配置执行。确认继续吗？')) {
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
    const resultText = isPending ? '待结算' : (row.result || '--');
    const checkText = resultCheckText(row.result_check_status);
    const checkCls = row.result_check_status === 'match' ? 'trade-up' : ((row.result_check_status === 'mismatch') ? 'trade-down' : 'trade-skip');
    const checkTitle = '官方结果: ' + (row.resolved_expected_result || '--') + (row.result_check_error ? (' · 错误: ' + row.result_check_error) : '');
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
      '<td>' + esc(fmtNum(row.price, 4)) + '</td>' +
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
  }
}

async function refreshAll() {
  await refreshConfig();
  await refreshMarket();
  await Promise.allSettled([refreshSummary(), refreshRecent()]);
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


