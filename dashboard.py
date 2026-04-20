from __future__ import annotations

import csv
import errno
import json
import os
import threading
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from config import AppConfig, build_config_from_env_values, load_env_file_values, PAPER_STRATEGY_IDS
from binance_signal import BinanceDepth5SignalService
from models import MarketWindow, PendingPaperTrade
from paper_report import summarize_paper_trades
from polymarket_api import PolymarketClient, build_resolved_round
from risk_and_sizing import build_trade_plan
from strategy import get_side_for_round
from runtime_control import RuntimeControl
from trader import (
    _entry_window_missed,
    _entry_time_for_round,
    _resolve_side_from_strategy,
    _apply_strategy6_signal_to_quote,
    _is_strategy6_signal_stale,
    _ws_is_stale_for_trade,
    load_session_state,
    resolve_quote_price,
    load_live_redeem_state,
    validate_live_runtime_config,
)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={values[key]}" for key in sorted(values.keys())]
    text = "\n".join(lines)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _normalize_strategy_id_list_value(value: str) -> str:
    cfg = build_config_from_env_values({PAPER_STRATEGY_IDS: value})
    raw = [item.strip() for item in str(value).split(',') if item.strip()]
    if raw and len(cfg.paper_strategy_ids) == 1 and cfg.paper_strategy_ids[0] == cfg.strategy_id:
        has_valid = any(item in {"1", "2", "3", "4", "5", "6", "7"} for item in raw)
        if not has_valid:
            raise ValueError(f"Invalid value for PAPER_STRATEGY_IDS: expected comma-separated strategy ids 1-7, got {value!r}")
    normalized = [str(item) for item in cfg.paper_strategy_ids]
    if not normalized:
        raise ValueError(f"Invalid value for PAPER_STRATEGY_IDS: expected comma-separated strategy ids 1-7, got {value!r}")
    return ",".join(normalized)

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
    if raw in {"1", "2", "3", "4", "5", "6", "7"}:
        return raw
    raise ValueError(f"Invalid strategy filter: {strategy!r}")


def _filter_trade_rows_by_strategy(rows: list[dict[str, str]], strategy: int | str | None) -> list[dict[str, str]]:
    strategy_filter = _normalize_strategy_filter(strategy)
    if strategy_filter is None:
        return list(rows)
    return [row for row in rows if str(row.get("strategy") or "").strip() == strategy_filter]


def _filter_pending_paper_trades_by_strategy(items: list[PendingPaperTrade], strategy: int | str | None) -> list[PendingPaperTrade]:
    strategy_filter = _normalize_strategy_filter(strategy)
    if strategy_filter is None:
        return list(items)
    return [item for item in items if str(item.strategy) == strategy_filter]


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


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _validate_recent_trade_row(
    row: dict[str, str],
    *,
    client: PolymarketClient | Any,
) -> dict[str, str]:
    validated = dict(row)
    validated.setdefault('resolved_price_to_beat', '')
    validated.setdefault('resolved_final_price', '')
    validated.setdefault('resolved_expected_result', '')
    validated.setdefault('result_check_status', '')

    if validated.get('pending_status') == 'pending_settlement':
        validated['result_check_status'] = 'pending'
        return validated

    slug = str(validated.get('event_slug') or '').strip()
    result = str(validated.get('result') or '').strip().upper()
    if not slug or not result or result == '--':
        return validated

    try:
        resolved = build_resolved_round(client.get_event_by_slug(slug))
    except Exception:
        validated['result_check_status'] = 'error'
        return validated

    validated['resolved_price_to_beat'] = str(resolved.price_to_beat)
    validated['resolved_final_price'] = str(resolved.final_price)
    validated['resolved_expected_result'] = resolved.result
    validated['result_check_status'] = 'match' if result == resolved.result else 'mismatch'
    return validated



def _localize_runtime_message(message: str | None) -> str | None:
    if not message:
        return message
    mapping = {
        "Live trading is disabled.": "实盘交易未开启。",
        "Live trading is disabled. Set LIVE_TRADING_ENABLED=true (or config flag) to submit orders.": "实盘交易未开启。请先打开实盘交易开关。",
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
            "label": "Binance OFI 失衡",
            "summary": "根据 Binance 深度盘口的 OFI 强弱决定方向。",
            "preview": ["OFI", "THRESHOLD", "SKIP"],
            "detail": "仅在 OFI 信号足够强且未过期时给出方向，否则按规则跳过。",
        },
        "7": {
            "label": "OFI+动量共识",
            "summary": "只有 Binance OFI 和 Polymarket 动量同向时才允许交易。",
            "preview": ["OFI", "MOMENTUM", "THRESHOLD", "SKIP"],
            "detail": "更偏向少做、做高质量信号。",
        },
    }


def _field_groups() -> list[dict[str, Any]]:
    return [
        {
            "title": "运行模式",
            "description": "控制是否启用实盘，并配置实盘所需的钱包凭证。",
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
                "PAPER_STRATEGY_IDS",
                "TARGET_PROFIT",
                "BET_SIZING_MODE",
                "BASE_ORDER_COST",
                "MAX_CONSECUTIVE_LOSSES",
                "MAX_STAKE",
                "MAX_PRICE_THRESHOLD",
                "STRATEGY7_MAX_ENTRY_PRICE",
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
                "STRATEGY7_OFI_THRESHOLD",
                "STRATEGY7_MOMENTUM_THRESHOLD",
                "STRATEGY7_MIN_SIGNAL_GAP",
                "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS",
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


class ConfigValidationError(ValueError):
    def __init__(self, field_errors: dict[str, str]):
        self.field_errors = dict(field_errors)
        super().__init__("; ".join(self.field_errors.values()))


class DashboardState:
    EDITABLE_CONFIG_KEYS: tuple[str, ...] = (
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
        "STRATEGY_ID",
        "PAPER_STRATEGY_IDS",
        "TARGET_PROFIT",
        "BET_SIZING_MODE",
        "BASE_ORDER_COST",
        "MAX_CONSECUTIVE_LOSSES",
        "MAX_STAKE",
        "MAX_PRICE_THRESHOLD",
        "STRATEGY7_OFI_THRESHOLD",
        "STRATEGY7_MOMENTUM_THRESHOLD",
        "STRATEGY7_MAX_ENTRY_PRICE",
        "STRATEGY7_MIN_SIGNAL_GAP",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS",
        "SIGNAL_MOMENTUM_THRESHOLD",
        "SIGNAL_WEAK_SIGNAL_MODE",
        "SIGNAL_FALLBACK_STRATEGY_ID",
        "SIGNAL_HISTORY_FIDELITY_SECONDS",
        "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS",
        "SIGNAL_DYNAMIC_THRESHOLD_K",
        "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS",
        "MAX_STAKE_SKIP_ALERT_THRESHOLD",
        "WS_ENABLED",
        "WS_QUOTE_STALE_SECONDS",
        "WS_TRADE_GUARD_STALE_SECONDS",
        "WS_CONNECT_TIMEOUT_SECONDS",
    )

    CONFIG_LABELS: dict[str, str] = {
        "ENABLE_LIVE_TRADING": "启用实盘",
        "TRADE_MODE": "交易模式",
        "MARKET_TIMEFRAME": "市场频次",
        "LIVE_TRADING_ENABLED": "实盘交易开关",
        "POLYMARKET_PRIVATE_KEY": "实盘私钥",
        "POLYMARKET_FUNDER": "\u5b9e\u76d8\u94b1\u5305\u5730\u5740",
        "POLYMARKET_API_KEY": "官方 API 访问密钥",
        "POLYMARKET_API_SECRET": "官方 API 签名密钥",
        "POLYMARKET_API_PASSPHRASE": "官方 API 通行口令",
        "POLYMARKET_BUILDER_API_KEY": "Builder Redeem API Key",
        "POLYMARKET_BUILDER_SECRET": "Builder Redeem Secret",
        "POLYMARKET_BUILDER_PASSPHRASE": "Builder Redeem Passphrase",
        "POLYMARKET_RELAYER_API_KEY": "Relayer API Key",
        "POLYMARKET_RELAYER_API_KEY_ADDRESS": "Relayer Key Address",
        "LIVE_AUTO_REDEEM_ENABLED": "实盘自动赎回",
        "LIVE_AUTO_REDEEM_DRY_RUN": "自动赎回演练模式",
        "LIVE_AUTO_REDEEM_POLL_SECONDS": "自动赎回轮询秒数",
        "LIVE_AUTO_REDEEM_MAX_RETRIES": "自动赎回最大重试次数",
        "LIVE_AUTO_REDEEM_INITIAL_BACKOFF_SECONDS": "自动赎回初始退避秒数",
        "LIVE_AUTO_REDEEM_MAX_BACKOFF_SECONDS": "自动赎回最大退避秒数",
        "STRATEGY_ID": "基础策略",
        "PAPER_STRATEGY_IDS": "纸面策略组合",
        "TARGET_PROFIT": "每次目标净利",
        "BET_SIZING_MODE": "下注模式",
        "BASE_ORDER_COST": "固定起始下注金额",
        "MAX_CONSECUTIVE_LOSSES": "连亏重置轮数",
        "MAX_STAKE": "单笔最大下注金额",
        "MAX_PRICE_THRESHOLD": "最高买入价格阈值",
        "STRATEGY7_OFI_THRESHOLD": "策略7 OFI阈值",
        "STRATEGY7_MOMENTUM_THRESHOLD": "策略7 动量阈值",
        "STRATEGY7_MAX_ENTRY_PRICE": "策略7 最高入场价",
        "STRATEGY7_MIN_SIGNAL_GAP": "策略7 最小信号优势",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "策略7 最晚确认秒数",
        "SIGNAL_MOMENTUM_THRESHOLD": "动量阈值",
        "SIGNAL_WEAK_SIGNAL_MODE": "弱信号处理",
        "SIGNAL_FALLBACK_STRATEGY_ID": "弱信号回退基础策略",
        "SIGNAL_HISTORY_FIDELITY_SECONDS": "信号采样秒数",
        "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS": "开盘锚点最大偏移秒",
        "SIGNAL_DYNAMIC_THRESHOLD_K": "动态阈值系数K",
        "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS": "动态阈值最少样本点",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "入场前锁边秒数",
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
        "STRATEGY_ID": ["1", "2", "3", "4", "5", "6", "7"],
        "PAPER_STRATEGY_IDS": ["1", "2", "3", "4", "5", "6", "7"],
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
        "PAPER_STRATEGY_IDS": "paper_strategy_ids",
        "TARGET_PROFIT": "target_profit",
        "BET_SIZING_MODE": "bet_sizing_mode",
        "BASE_ORDER_COST": "base_order_cost",
        "MAX_CONSECUTIVE_LOSSES": "max_consecutive_losses",
        "MAX_STAKE": "max_stake",
        "MAX_PRICE_THRESHOLD": "max_price_threshold",
        "STRATEGY7_OFI_THRESHOLD": "strategy7_ofi_threshold",
        "STRATEGY7_MOMENTUM_THRESHOLD": "strategy7_momentum_threshold",
        "STRATEGY7_MAX_ENTRY_PRICE": "strategy7_max_entry_price",
        "STRATEGY7_MIN_SIGNAL_GAP": "strategy7_min_signal_gap",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "strategy7_confirm_before_entry_seconds",
        "SIGNAL_MOMENTUM_THRESHOLD": "signal_momentum_threshold",
        "SIGNAL_WEAK_SIGNAL_MODE": "signal_weak_signal_mode",
        "SIGNAL_FALLBACK_STRATEGY_ID": "signal_fallback_strategy_id",
        "SIGNAL_HISTORY_FIDELITY_SECONDS": "signal_history_fidelity_seconds",
        "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS": "signal_anchor_max_offset_seconds",
        "SIGNAL_DYNAMIC_THRESHOLD_K": "signal_dynamic_threshold_k",
        "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS": "signal_dynamic_threshold_min_points",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "signal_lock_before_entry_seconds",
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
        "MAX_STAKE",
        "MAX_PRICE_THRESHOLD",
        "STRATEGY7_OFI_THRESHOLD",
        "STRATEGY7_MOMENTUM_THRESHOLD",
        "STRATEGY7_MAX_ENTRY_PRICE",
        "STRATEGY7_MIN_SIGNAL_GAP",
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
        "STRATEGY7_MAX_ENTRY_PRICE": "strategy_7_only",
        "STRATEGY7_MIN_SIGNAL_GAP": "strategy_7_only",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "strategy_7_only",
    }
    FIELD_HELP: dict[str, str] = {
        "STRATEGY_ID": "策略 1-4 是固定节奏策略，策略 5 是动量策略，策略 6 是 Binance OFI 盘口失衡策略。",
        "PAPER_STRATEGY_IDS": "纸面测试可同时运行多个策略，按输入顺序去重，例如 1,2,6。",
        "TARGET_PROFIT": "在目标收益模式下，这里表示每轮期望净利；在固定金额模式下，它更多用于观察研究，不直接决定首笔下注金额。",
        "BET_SIZING_MODE": "固定金额模式会使用固定首笔下注额；目标收益模式会根据目标净利反推下注金额。",
        "ENABLE_LIVE_TRADING": "关闭时仅运行纸面测试；开启后，运行时可以切换到实盘配置并允许真实下单。",
        "MARKET_TIMEFRAME": "选择当前要玩的 Polymarket BTC 预测频次，仅支持 5 分钟和 15 分钟。",
        "POLYMARKET_PRIVATE_KEY": "实盘钱包私钥，仅在实盘模式下需要。",
        "POLYMARKET_FUNDER": "与私钥对应的实盘钱包地址（0x...），并且需要实际承担实盘订单资金。",
        "POLYMARKET_API_KEY": "CLOB 实盘下单凭证，仅用于 live order 私有接口。",
        "POLYMARKET_API_SECRET": "CLOB 实盘下单签名密钥，仅用于 live order 私有接口。",
        "POLYMARKET_API_PASSPHRASE": "CLOB 实盘下单通行口令，仅用于 live order 私有接口。",
        "POLYMARKET_BUILDER_API_KEY": "官方 gasless redeem 的 Builder API key，仅用于自动赎回。",
        "POLYMARKET_BUILDER_SECRET": "官方 gasless redeem 的 Builder secret，仅用于自动赎回。",
        "POLYMARKET_BUILDER_PASSPHRASE": "官方 gasless redeem 的 Builder passphrase，仅用于自动赎回。",
        "POLYMARKET_RELAYER_API_KEY": "官方 gasless redeem 的 Relayer API key，仅用于自动赎回。",
        "POLYMARKET_RELAYER_API_KEY_ADDRESS": "与 Relayer API key 配套的地址，仅用于自动赎回认证。",
        "LIVE_AUTO_REDEEM_ENABLED": "仅实盘模式使用。开启后会启动独立的自动赎回线程，扫描可赎回的获胜仓位并自动尝试赎回。",
        "LIVE_AUTO_REDEEM_DRY_RUN": "仅在开启自动赎回后才有意义。设为 true 时，自动赎回线程仍会检测仓位、写入状态并更新监控页面，但不会发送真实的 Polygon 链上赎回交易。正常实盘请保持 false，只在首次验证或调试时临时开启。",
        "LIVE_AUTO_REDEEM_POLL_SECONDS": "检查可赎回实盘仓位的轮询间隔，单位秒。",
        "LIVE_AUTO_REDEEM_MAX_RETRIES": "单个 condition 在遇到临时 RPC 或链上错误时的最大重试次数。",
        "LIVE_AUTO_REDEEM_INITIAL_BACKOFF_SECONDS": "自动赎回失败后的首次退避等待秒数。",
        "LIVE_AUTO_REDEEM_MAX_BACKOFF_SECONDS": "自动赎回重试之间的最大退避上限秒数。",
        "BASE_ORDER_COST": "仅固定金额模式使用；获胜后策略会重置回这个起始下注金额。",
        "MAX_CONSECUTIVE_LOSSES": "连续亏损达到这个次数后，策略会执行一次止损重置。",
        "MAX_STAKE": "单笔订单允许投入的最大 USDC；超过后会直接跳过本轮。",
        "MAX_PRICE_THRESHOLD": "目标方向价格高于该阈值时不入场。",
        "STRATEGY7_OFI_THRESHOLD": "策略 7 对 Binance OFI 的最小强度要求，低于该阈值直接跳过。",
        "STRATEGY7_MOMENTUM_THRESHOLD": "策略 7 对 Polymarket 轮内动量确认的最小要求。",
        "STRATEGY7_MAX_ENTRY_PRICE": "策略 7 的更严格入场价格上限，高于该价格不入场。",
        "STRATEGY7_MIN_SIGNAL_GAP": "策略 7 要求 OFI 和动量超过阈值的最小额外优势，避免擦线交易。",
        "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "策略 7 需要在计划入场前至少提前这么多秒完成双信号确认。",
        "SIGNAL_MOMENTUM_THRESHOLD": "策略 5 的基础动量阈值，比较 abs(current_up - open_up)。",
        "SIGNAL_WEAK_SIGNAL_MODE": "弱动量信号的处理方式：直接跳过，或回退到固定节奏策略。",
        "SIGNAL_FALLBACK_STRATEGY_ID": "仅当策略 5 在弱信号下回退时使用。",
        "SIGNAL_HISTORY_FIDELITY_SECONDS": "历史价格拉取的采样粒度；数值越小越精确，但请求也更重。",
        "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS": "动量逻辑对齐开盘锚点时允许的最大时间偏移。",
        "SIGNAL_DYNAMIC_THRESHOLD_K": "动态阈值系数，运行时会使用 max(基础阈值, k * sigma)。",
        "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS": "启用动态阈值前要求的最少样本点数量。",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "在计划入场前提前锁定方向，避免最后几秒来回翻边。",
        "MAX_STAKE_SKIP_ALERT_THRESHOLD": "连续因超过 MAX_STAKE 而跳过多少次后触发提醒。",
        "WS_ENABLED": "优先使用 WS(WebSocket) 行情缓存；必要时再回退到 HTTP。",
        "WS_QUOTE_STALE_SECONDS": "WS(WebSocket) 行情在多久未更新后视为过期。",
        "WS_TRADE_GUARD_STALE_SECONDS": "入场前若 WS 行情年龄超过该阈值，则禁止本轮交易。",
        "WS_CONNECT_TIMEOUT_SECONDS": "建立 WebSocket 会话时的连接超时秒数。",
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
        strategy_ids = list(getattr(cfg, "paper_strategy_ids", []) or [])
        if cfg.strategy_id not in {6, 7} and not any(strategy_id in {6, 7} for strategy_id in strategy_ids):
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
        value = getattr(self._cfg, self.CONFIG_ATTR_MAP[key])
        if value is None:
            return ""
        if key == "PAPER_STRATEGY_IDS":
            return ",".join(str(item) for item in value)
        return _fmt_env(value)

    def _masked_env_values(self, env_values: dict[str, str]) -> dict[str, str]:
        masked = dict(env_values)
        for key in self.SECRET_CONFIG_KEYS:
            masked[key] = self._mask_secret(masked.get(key))
        return masked

    def _build_runtime_status(self, env_values: dict[str, str]) -> dict[str, Any]:
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
            "pending_live_order": runtime_snapshot.pending_live_order if runtime_snapshot is not None else False,
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
            runtime_status = self._build_runtime_status(env_values)
            strategy_catalog = json.loads(json.dumps(self.STRATEGY_CATALOG))
            field_groups = json.loads(json.dumps(self.FIELD_GROUPS))
            if field_groups:
                field_groups[0]["title"] = "运行模式"
            select_options = json.loads(json.dumps(self.SELECT_OPTIONS))
            select_options["STRATEGY_ID"] = ["1", "2", "3", "4", "5", "6", "7"]
            return {
                "env_file": str(self.env_file),
                "env_values": self._masked_env_values(env_values),
                "editable_keys": list(self.EDITABLE_CONFIG_KEYS),
                "labels": self.CONFIG_LABELS,
                "select_options": select_options,
                "strategy_catalog": strategy_catalog,
                "field_groups": field_groups,
                "field_scope": self.FIELD_SCOPE,
                "field_help": self.FIELD_HELP,
                "validation_errors": validation_errors,
                "runtime_status": runtime_status,
                "saved_at": _iso(self._last_saved_at),
            }

    def update_config(self, values: dict[str, str]) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise ValueError("Config payload must be an object.")
        unsupported = sorted(key for key in values.keys() if key not in self.EDITABLE_CONFIG_KEYS)
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

    def get_market_payload(self, *, strategy: int | str | None = None) -> dict[str, Any]:
        with self._lock:
            cfg = self._cfg
            client = self._client
            binance_signal_service = self._binance_signal_service

        now = datetime.now(timezone.utc)
        strategy_filter = _normalize_strategy_filter(strategy)
        selected_strategy = int(strategy_filter or str(cfg.strategy_id))
        effective_cfg = replace(cfg, strategy_id=selected_strategy)
        effective_paper_strategy_ids = list(getattr(cfg, "paper_strategy_ids", []) or [cfg.strategy_id])
        if selected_strategy not in effective_paper_strategy_ids:
            effective_paper_strategy_ids.append(selected_strategy)
        session_state = load_session_state(
            cfg.logs_dir / "session_state.json",
            effective_paper_strategy_ids=effective_paper_strategy_ids,
        )
        strategy_session = session_state
        if getattr(session_state, "paper_strategies", None):
            strategy_session = session_state.paper_strategies.get(selected_strategy) or session_state
        current_round, next_round = client.find_current_and_next_rounds(now=now)
        display_round = _select_display_round(current_round=current_round, next_round=next_round)
        target_round = display_round
        ws_runtime = client.get_ws_runtime_stats()
        strategy_view = {
            "selected": str(selected_strategy),
            "paper_strategy_ids": [str(item) for item in effective_paper_strategy_ids],
            "available": [str(item) for item in effective_paper_strategy_ids],
        }

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
                "message": "\u5f53\u524d\u6ca1\u6709\u53ef\u7528\u76845\u5206\u949f\u8f6e\u6b21\u3002",
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
            "enabled": selected_strategy == 7,
            "ofi_score": quote.strategy6_ofi_score,
            "momentum_delta": side_decision.signal_delta,
            "agreement": (
                "agree"
                if selected_strategy == 7 and side_decision.side in {"UP", "DOWN"}
                else ("conflict" if side_decision.reason == "strategy7_signal_conflict" else None)
            ),
            "quality_gate": (
                "passed"
                if selected_strategy == 7 and side_decision.side in {"UP", "DOWN"}
                else (side_decision.reason if selected_strategy == 7 else None)
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
    def get_paper_summary_payload(self, *, strategy: int | str | None = None) -> dict[str, Any]:
        with self._lock:
            paper_csv = self._cfg.logs_dir / "paper_trades.csv"
        strategy_filter = _normalize_strategy_filter(strategy)
        try:
            if strategy_filter is None:
                daily = summarize_paper_trades(paper_csv, tz_offset="+08:00")
            else:
                filtered_rows = list(reversed(_filter_trade_rows_by_strategy(_tail_csv_rows(paper_csv, limit=1000000), strategy_filter)))
                if not filtered_rows:
                    daily = []
                else:
                    filtered_csv = paper_csv.with_name(f"{paper_csv.stem}_strategy_{strategy_filter}_summary{paper_csv.suffix}")
                    with filtered_csv.open("w", newline="", encoding="utf-8") as handle:
                        writer = csv.DictWriter(handle, fieldnames=list(filtered_rows[0].keys()))
                        writer.writeheader()
                        writer.writerows(filtered_rows)
                    try:
                        daily = summarize_paper_trades(filtered_csv, tz_offset="+08:00")
                    finally:
                        if filtered_csv.exists():
                            filtered_csv.unlink()
        except (FileNotFoundError, ValueError):
            daily = []
        days = [asdict(item) for item in daily[-14:]]
        return {
            "csv_path": str(paper_csv),
            "tz_offset": "+08:00",
            "strategy": strategy_filter or "all",
            "days": days,
            "latest": days[-1] if days else None,
        }

    def get_recent_trades_payload(self, *, limit: int, strategy: int | str | None = None) -> dict[str, Any]:
        with self._lock:
            cfg = self._cfg
            paper_csv = cfg.logs_dir / "paper_trades.csv"
            state_path = cfg.logs_dir / "session_state.json"
        capped_limit = max(1, min(300, int(limit)))
        strategy_filter = _normalize_strategy_filter(strategy)
        rows = _filter_trade_rows_by_strategy(_tail_csv_rows(paper_csv, limit=capped_limit * 4), strategy_filter)
        pending_rows: list[dict[str, str]] = []
        session_state = load_session_state(state_path, effective_paper_strategy_ids=cfg.paper_strategy_ids)
        pending_items = list(getattr(session_state, "pending_paper_trades", []) or [])
        if getattr(session_state, "paper_strategies", None):
            pending_items = []
            for strategy_state in session_state.paper_strategies.values():
                pending_items.extend(getattr(strategy_state, "pending_paper_trades", []) or [])
        for item in _filter_pending_paper_trades_by_strategy(pending_items, strategy_filter):
            pending_rows.append(_pending_paper_trade_to_recent_row(item))
        merged_rows = pending_rows + rows
        merged_rows.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
        merged_rows = merged_rows[:capped_limit]

        client = PolymarketClient(cfg)
        try:
            validated_rows = [_validate_recent_trade_row(row, client=client) for row in merged_rows]
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

        return {"csv_path": str(paper_csv), "strategy": strategy_filter or "all", "count": len(validated_rows), "rows": validated_rows}

    def get_live_recent_orders_payload(self, *, limit: int) -> dict[str, Any]:
        with self._lock:
            live_csv = self._cfg.logs_dir / "live_orders.csv"
        rows = _tail_csv_rows(live_csv, limit=max(1, min(300, int(limit))))
        return {"csv_path": str(live_csv), "count": len(rows), "rows": rows}


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
                self._send_json(self.dashboard_state.get_market_payload(strategy=strategy))
                return
            if parsed.path == "/api/paper/summary":
                query = parse_qs(parsed.query)
                strategy = (query.get("strategy") or [None])[0]
                self._send_json(self.dashboard_state.get_paper_summary_payload(strategy=strategy))
                return
            if parsed.path == "/api/paper/recent":
                query = parse_qs(parsed.query)
                limit = int((query.get("limit") or ["20"])[0])
                strategy = (query.get("strategy") or [None])[0]
                self._send_json(self.dashboard_state.get_recent_trades_payload(limit=limit, strategy=strategy))
                return
            if parsed.path == "/api/live/recent":
                query = parse_qs(parsed.query)
                limit = int((query.get("limit") or ["20"])[0])
                self._send_json(self.dashboard_state.get_live_recent_orders_payload(limit=limit))
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
      <div id=\"clockLocal\" class=\"clock\">本地时间 --</div>
      <button id=\"btnHelp\" class=\"btn btn-ghost\" type=\"button\">帮助</button>
      <button id=\"btnRefreshNow\" class=\"btn btn-ghost\" type=\"button\">立即刷新</button>
    </div>
  </header>

  <main class=\"layout\">
    <section class=\"panel left-stack\">
      <div class=\"panel-head\">
        <div>
          <div class=\"head-title\">参数引擎</div>
          <div class=\"head-desc\">参数可编辑并写回 .env</div>
        </div>
        <div id=\"cfgStatus\" class=\"chip\">未保存</div>
      </div>
      <div class=\"panel-body\">
        <div class=\"meta-item\">
          <span class=\"meta-label\">\u8bfb\u53d6\u72b6\u6001</span>
          <span id=\"cfgError\" class=\"meta-value\">--</span>
        </div>
        <div class=\"meta\">
          <div class=\"meta-item\">
            <span class=\"meta-label\">最近保存</span>
            <span id=\"cfgSavedAt\" class=\"meta-value\">--</span>
          </div>
        </div>

        <div id=\"strategyGuideCard\" class=\"strategy-guide-card\"></div>

        <div id="runtimeSummaryBar" class="strategy-guide-card fold-summary">
          <div class="strategy-guide-head">
            <div>
              <div class="strategy-guide-title">系统状态</div>
              <div id="runtimeSummaryText" class="strategy-guide-subtitle">当前模式 -- / 目标模式 -- / 是否待切换 -- / 实盘就绪 --</div>
            </div>
            <button id="runtimeDetailsToggle" class="btn btn-ghost" type="button" aria-expanded="false" aria-controls="runtimeDetailsPanel">展开运行详情</button>
          </div>
        </div>

        <div id="runtimeDetailsPanel" hidden>
          <div id="runtimeModeCard" class="strategy-guide-card">
            <div class="strategy-guide-head">
              <div>
                <div class="strategy-guide-title">运行模式</div>
                <div class="strategy-guide-subtitle">显示配置目标、当前实际状态、切换进度和实盘条件。</div>
              </div>
              <span class="chip warn">热切换受控</span>
            </div>
            <div class="rows">
              <div class="row"><span class="label">目标模式</span><span id="runtimeSavedMode" class="value">--</span></div>
              <div class="row"><span class="label">当前模式</span><span id="runtimeRunningMode" class="value">--</span></div>
              <div class="row"><span class="label">是否待切换</span><span id="runtimeRestartRequired" class="value">--</span></div>
              <div class="row"><span class="label">实盘就绪</span><span id="runtimeLiveReady" class="value">--</span></div>
              <div class="row"><span class="label">校验结果</span><span id="runtimeLiveError" class="value">--</span></div>
              <div id="runtimeRedeemRows">
                <div class="row"><span class="label">自动赎回</span><span id="runtimeRedeemEnabled" class="value">--</span></div>
                <div class="row"><span class="label">待赎回数量</span><span id="runtimeRedeemPending" class="value">--</span></div>
                <div class="row"><span class="label">最近结果</span><span id="runtimeRedeemResult" class="value">--</span></div>
                <div class="row"><span class="label">最近尝试</span><span id="runtimeRedeemAttempt" class="value">--</span></div>
                <div class="row"><span class="label">最近交易哈希</span><span id="runtimeRedeemTxHash" class="value">--</span></div>
              </div>
              <div id="runtimeOptimizerRows">
                <div class="row"><span class="label">优化器</span><span id="runtimeOptimizerEnabled" class="value">--</span></div>
                <div class="row"><span class="label">当前冠军</span><span id="runtimeOptimizerChampion" class="value">--</span></div>
                <div class="row"><span class="label">活动挑战者</span><span id="runtimeOptimizerChallengers" class="value">--</span></div>
                <div class="row"><span class="label">可晋级数量</span><span id="runtimeOptimizerPromotable" class="value">--</span></div>
                <div class="row"><span class="label">最近运行</span><span id="runtimeOptimizerLastRun" class="value">--</span></div>
                <div class="row"><span class="label">挑战者明细</span><span id="runtimeOptimizerChallengerList" class="value">--</span></div>
                <div class="row"><span class="label">可晋级明细</span><span id="runtimeOptimizerPromotableList" class="value">--</span></div>
              </div>
            </div>
          </div>
        </div>

        <div class="strategy-guide-card fold-summary">
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

    <section class=\"panel center-stack\">
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
            <div class=\"row\">
              <span class=\"label\">最近刷新</span>
              <span id=\"marketUpdatedAt\" class=\"value\">--</span>
            </div>
          </div>

          </div>
        </div>
      </div>
    </section>

    <section class="stack right-stack">
      <div class=\"panel\">
        <div class=\"panel-head\">
          <div>
            <div class=\"head-title\">实时连接状态</div>
            <div class=\"head-desc\">连接质量与订阅状态</div>
          </div>
          <div id=\"wsHealth\" class=\"chip\">待刷新</div>
        </div>
        <div class=\"panel-body\">
          <div id=\"wsRuntimeList\" class=\"runtime-list\"></div>
          <div class=\"footnote\">说明：行情来源显示为 websocket 时，表示使用 WS 缓存盘口；显示为 http 时，表示回退到 HTTP 拉取。</div>
        </div>
      </div>

    </section>

    <section class="panel unified-report-card">
      <div class="report-card-head">
        <div>
          <div class="head-title">交易报告</div>
          <div class="head-desc">策略筛选同时作用于纸面交易汇总与最近交易明细</div>
        </div>
        <div class="top-actions report-card-actions">
          <select id="paperReportStrategy" class="btn btn-ghost"></select>
          <div class="report-status-group">
            <div id="paperStatus" class="chip">待刷新</div>
            <div id="recentStatus" class="chip">待刷新</div>
          </div>
        </div>
      </div>
      <div class="report-card-body">
        <section id="reportSummarySection" class="report-section">
          <div class="section-title">纸面交易汇总</div>
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
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 16px;
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
  grid-template-columns: 360px minmax(560px, 1fr) 360px;
  align-items: start;
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
}

.strategy-guide-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--text);
}

.strategy-guide-subtitle {
  font-size: 12px;
  color: var(--muted);
  line-height: 1.5;
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
  grid-template-columns: 1fr 1fr;
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
}

.field input,
.field select {
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
}

.field input.input-compact,
.field select.input-compact {
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
  grid-template-columns: auto minmax(0, 1fr) auto;
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
  display: inline-flex;
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
  white-space: nowrap;
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
}

.strategy-panel-summary {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.45;
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
  grid-template-columns: minmax(320px, 0.95fr) minmax(0, 1.45fr);
  gap: 14px;
  padding: 14px;
}

.report-section {
  min-width: 0;
  display: grid;
  gap: 10px;
}

.report-recent-table {
  max-height: 420px;
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: rgba(6, 12, 22, 0.66);
}

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
    grid-template-columns: 350px minmax(500px, 1fr);
  }

  .report-card-body {
    grid-template-columns: minmax(280px, 0.95fr) minmax(0, 1.25fr);
  }

  .right-stack {
    grid-column: span 2;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
  }
}

@media (max-width: 1024px) {
  .layout { grid-template-columns: 1fr; }
  .report-card-body { grid-template-columns: 1fr; }
  .right-stack { grid-column: auto; grid-template-columns: 1fr; }
  .split,
  .kv-grid,
  .group-grid { grid-template-columns: 1fr; }
  .market-header { grid-template-columns: 1fr; }
  .timer-wrap { text-align: left; }
  .subtitle { max-width: 56vw; }
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
  paperStrategyFilter: 'all',
  paperReportStrategyFilter: 'all',
  paperSummaryStrategyFilter: null,
  paperRecentStrategyFilter: null,
  countdownSnapshotAtMs: null,
  countdownBaseSeconds: null,
  showInternalKeys: false,
  runtimeDetailsOpen: false,
  diagnosticsOpen: false,
  signalDetailsOpen: false,
  advancedConfigOpen: false,
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
          'LIVE_AUTO_REDEEM_ENABLED 只在实盘模式下生效，开启后会启动独立的自动赎回线程。',
          '该线程会扫描可赎回的获胜仓位，并通过 Polygon 的 redeemPositions 方法尝试把它们转回可用余额。',
          '当 LIVE_AUTO_REDEEM_DRY_RUN=true 时，自动赎回线程只做检测、记录和监控页面展示，不会发送真实赎回交易。',
          '正常实盘请保持 LIVE_AUTO_REDEEM_DRY_RUN=false，只在首次验证或调试时临时打开。',
        ],
      },
      {
        title: '先看哪里',
        bullets: [
          '先看行情与信号，确认当前轮次、方向判断和倒计时是否正常。',
          '再看下注计划与风控，确认当前是否准备下注、为什么跳过、以及预期收益是多少。',
          '然后看会话状态，关注累计盈亏、待回补亏损和连续亏损轮数。',
          '最后看实时连接状态，判断 WS(WebSocket) 数据是否可靠。',
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
          'should_trade=true 说明当前轮次、价格、风控检查和 WS 防护都允许执行。',
          'should_trade=false 时先结合 skip_reason 字段一起看，不要先默认程序坏了。',
          '价格超过阈值、弱信号、超过下注上限，这些都属于正常的跳过原因。',
          '如果是 WS 陈旧、当日亏损限制或止损重置，则应立即复核。',
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
          '用于判断 WS(WebSocket) 行情是否可信。',
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
};

const HELP_FAQ = [
  ['为什么保存了参数却像是没生效？', '先看字段提示和页面上的生效值。无效输入会回退到上一次有效配置，不会带着错误参数直接运行。'],
  ['为什么现在没有下注？', '先看下注计划与风控里的 skip_reason，再判断是价格、风控、信号还是 WS 保护在阻止执行。'],
  ['为什么策略 5 经常没有信号？', '策略 5 不是固定节奏，它需要价格波动幅度超过阈值；弱信号会按照 SKIP 或 FALLBACK 这两种规则处理。'],
  ['为什么方向和我预期的不一样？', '固定节奏策略取决于当前轮次索引；动量策略则取决于开盘价、当前价、阈值和偏移。'],
  ['为什么会触发 WS 保护？', '这表示 WS(WebSocket) 行情数据已经过旧，系统会选择阻止执行，而不是拿陈旧数据去下注。'],
  ['为什么当日已实现盈亏会重置？', '这是按天统计的自然切日行为；累计盈亏仍然保留在会话状态里。'],
  ['LIVE_AUTO_REDEEM_ENABLED 是什么意思？', '这是实盘自动赎回开关。开启后，实盘模式会启动单独的自动赎回线程，自动扫描并尝试赎回获胜仓位。'],
  ['LIVE_AUTO_REDEEM_DRY_RUN 需要去掉吗？', '不需要。它是一个安全阀：true 表示只做演练，不会发送真实的 Polygon 赎回交易。正常实盘应保持 false，仅在首次验证或调试时开启。'],
  ['为什么触发 max stake 后会连续跳过？', '待回补亏损和当前价格条件可能会把本轮所需金额推高到 MAX_STAKE 之上，需要结合 recovery loss 一起判断。'],
  ['新用户最容易配错什么？', '最常见的是一次改太多参数、把固定节奏和动量逻辑混在一起看，以及把 WS 保护误当成策略故障。'],
];

const STORAGE_KEYS = {
  showInternalKeys: 'dashboard_show_internal_keys',
};

const STRATEGY_LABELS = {
  1: '单轮交替',
  2: '双轮分组交替',
  3: '三轮分组交替',
  4: '四轮分组交替',
  5: '动量信号 V2',
  6: 'Binance OFI 失衡',
  7: 'OFI+动量共识',
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
  order_cost_above_max_stake: '下单金额超过单笔上限',
  order_size_not_positive: '下单份额无效',
  max_consecutive_losses_reached: '达到连续亏损重置阈值',
  stop_loss_triggered: '触发止损重置',
  manual_skip: '人工跳过',
  ofi_unavailable: 'OFI 信号不可用',
  ofi_stale: 'OFI 信号已过期',
  ofi_too_weak: 'OFI 信号过弱',
  strategy7_ofi_unavailable: '策略7 OFI 信号不可用',
  strategy7_ofi_stale: '策略7 OFI 信号已过期',
  strategy7_ofi_too_weak: '策略7 OFI 信号过弱',
  strategy7_momentum_unavailable: '策略7 动量信号不可用',
  strategy7_momentum_too_weak: '策略7 动量信号过弱',
  strategy7_signal_conflict: 'OFI+动量需同向确认',
  strategy7_entry_too_late: '策略7 确认出现过晚',
  strategy7_price_too_high: '策略7 入场价格过高',
  strategy7_confidence_too_low: '策略7 信号优势不足',
  awaiting_fill_confirmation: '等待成交确认',
  market_timeframe: '市场频次切换待生效',
  'INVALID OPERATION': 'WebSocket 订阅请求无效',
};

const CONFIG_KEY_NAMES = {
  ENABLE_LIVE_TRADING: '启用实盘',
  TRADE_MODE: '交易模式',
  LIVE_TRADING_ENABLED: '实盘交易开关',
  POLYMARKET_PRIVATE_KEY: '实盘私钥',
  POLYMARKET_FUNDER: '\u5b9e\u76d8\u94b1\u5305\u5730\u5740',
  STRATEGY_ID: '基础策略',
  TARGET_PROFIT: '每次目标净利',
  BET_SIZING_MODE: '下注模式',
  BASE_ORDER_COST: '固定起始下注金额',
  MAX_CONSECUTIVE_LOSSES: '连亏重置轮数',
  MAX_STAKE: '单笔最大下注金额',
  MAX_PRICE_THRESHOLD: '最高买入价格阈值',
  STRATEGY7_OFI_THRESHOLD: '策略7 OFI阈值',
  STRATEGY7_MOMENTUM_THRESHOLD: '策略7 动量阈值',
  STRATEGY7_MAX_ENTRY_PRICE: '策略7 最高入场价',
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
  return String(reason);
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
      return;
    }
    state.showInternalKeys = raw === '1';
  } catch (_err) {
    state.showInternalKeys = false;
  }
}

function saveUiPrefs() {
  try {
    localStorage.setItem(STORAGE_KEYS.showInternalKeys, state.showInternalKeys ? '1' : '0');
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
  if (status === 'match') return '一致';
  if (status === 'mismatch') return '不一致';
  if (status === 'pending') return '待结算';
  if (status === 'error') return '校验失败';
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
  if (key === 'STRATEGY_ID' || key === 'PAPER_STRATEGY_IDS' || key === 'SIGNAL_FALLBACK_STRATEGY_ID') {
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
  if (token === 'OFI') return 'OFI \u5224\u65ad';
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

function resolveUnifiedStrategySelection(payload, values) {
  const envValues = (payload && payload.env_values) || {};
  const options = (((payload || {}).select_options || {}).PAPER_STRATEGY_IDS || []).map((item) => String(item));
  const focusRaw = String((values && values.STRATEGY_ID) ?? envValues.STRATEGY_ID ?? options[0] ?? '');
  const selectedRaw = parseStrategyIdList((values && values.PAPER_STRATEGY_IDS) ?? envValues.PAPER_STRATEGY_IDS ?? '');
  const selected = selectedRaw.filter((item) => options.indexOf(item) >= 0);
  const focus = options.indexOf(focusRaw) >= 0 ? focusRaw : (selected[0] || options[0] || '');
  const mergedSelected = selected.slice();
  if (focus && mergedSelected.indexOf(focus) < 0) {
    mergedSelected.unshift(focus);
  }
  return {
    focus,
    selected: mergedSelected,
    options,
  };
}

function collectUnifiedStrategyValues(payload, currentValues) {
  const unified = resolveUnifiedStrategySelection(payload || state.config || {}, currentValues || {});
  return {
    STRATEGY_ID: unified.focus,
    PAPER_STRATEGY_IDS: unified.selected.join(','),
  };
}

function currentUnifiedStrategyDraftValues() {
  const focusNode = el('cfg_STRATEGY_ID');
  const multiNode = el('cfg_PAPER_STRATEGY_IDS');
  return {
    STRATEGY_ID: focusNode ? focusNode.value : '',
    PAPER_STRATEGY_IDS: multiNode
      ? Array.from(multiNode.options || []).filter((option) => option.selected).map((option) => option.value).join(',')
      : '',
  };
}

function refreshStrategyPanelDependentUi() {
  const liveValues = expandLiveToggleValues(collectConfigValues());
  renderStrategyGuide(state.config, liveValues);
  applyConfigFieldVisibility(liveValues);
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
  summary.textContent = '勾选纸面运行策略，并指定一个主策略用于当前页面查看与策略说明。';
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
}

function selectAllPaperStrategiesInPanel() {
  const draft = currentUnifiedStrategyDraftValues();
  const options = resolveUnifiedStrategySelection(state.config || {}, draft).options;
  renderUnifiedStrategyToolbar(state.config || {}, {
    ...draft,
    PAPER_STRATEGY_IDS: options.join(','),
  });
  refreshStrategyPanelDependentUi();
}

function clearPaperStrategies() {
  const draft = currentUnifiedStrategyDraftValues();
  renderUnifiedStrategyToolbar(state.config || {}, {
    ...draft,
    PAPER_STRATEGY_IDS: '',
  });
  refreshStrategyPanelDependentUi();
}

function togglePaperStrategySelection(strategyId, selected) {
  const draft = currentUnifiedStrategyDraftValues();
  const values = parseStrategyIdList(draft.PAPER_STRATEGY_IDS);
  const target = String(strategyId);
  const nextSelected = selected
    ? [...values, target]
    : values.filter((item) => String(item) !== target);
  renderUnifiedStrategyToolbar(state.config || {}, {
    ...draft,
    PAPER_STRATEGY_IDS: nextSelected.join(','),
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
  unified.options.forEach((opt) => {
    const option = document.createElement('option');
    option.value = opt;
    option.textContent = strategyOptionLabel('PAPER_STRATEGY_IDS', opt, payload);
    option.selected = unified.selected.indexOf(String(opt)) >= 0;
    multiNode.appendChild(option);
  });
  renderStrategyPanel(payload, values);
  state.paperStrategyFilter = focusStrategy;
}

function paperReportStrategyOptions() {
  const strategyView = ((state.market || {}).strategy_view) || {};
  const available = Array.isArray(strategyView.available) ? strategyView.available.map((item) => String(item)) : [];
  return ['all', ...available.filter((item, index, arr) => item && arr.indexOf(item) === index)];
}

function effectivePaperReportStrategyFilter() {
  return String(state.paperReportStrategyFilter || state.paperStrategyFilter || 'all');
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
  const strategy = effectivePaperRecentStrategyFilter();
  if (!strategy || strategy === 'all') {
    return '按时间倒序显示最近 80 条记录 · 当前策略：全部';
  }
  return '按时间倒序显示最近 80 条记录 · 当前策略：策略 ' + strategy;
}

function renderSharedPaperReportStrategySelector() {
  const options = paperReportStrategyOptions();
  const current = effectivePaperReportStrategyFilter();
  const summaryCurrent = effectivePaperSummaryStrategyFilter();
  const recentCurrent = effectivePaperRecentStrategyFilter();
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
    '<div class="strategy-guide-note">\u67e5\u770b\u7b56\u7565\uff1a' + esc(strategyId) + '\uff1b\u7eb8\u9762\u8fd0\u884c\uff1a' + esc(unified.selected.join(',') || '--') + '</div>' +
    '<div class="strategy-guide-note">' + esc(meta.detail || '') + '</div>' +
    extra;
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
    return 'HTTP回退';
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
  ws_last_error: '最近错误',
  reconnects: '重连次数',
  invalid_ops: '异常操作次数',
  connect_attempts: '连接尝试次数',
  subscribed_assets: '已订阅资产数',
  cached_assets: '缓存资产数',
  last_message_age_s: '消息延迟(秒)',
  last_error: '最近错误',
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

function setReportStatus(id, prefix, text, tone) {
  setChip(id, prefix + ': ' + text, tone);
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
  el('cfgError').textContent = message || '--';
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
  } else if (state.helpTab === 'strategyguide') {
    body.innerHTML = renderHelpStrategyGuide();
  } else {
    body.innerHTML = renderHelpFaq();
  }
  footer.innerHTML =
    '<a href="docs/dashboard_runbook.md" target="_blank" rel="noreferrer">Dashboard 操作说明</a>' +
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
    'MAX_CONSECUTIVE_LOSSES',
    'MAX_STAKE',
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

function expandLiveToggleValues(values) {
  const expanded = { ...values };
  if (!Object.prototype.hasOwnProperty.call(expanded, 'ENABLE_LIVE_TRADING')) {
    return expanded;
  }
  const normalized = String(expanded.ENABLE_LIVE_TRADING || 'false').toLowerCase() === 'true' ? 'true' : 'false';
  expanded.TRADE_MODE = normalized === 'true' ? 'live' : 'paper';
  expanded.LIVE_TRADING_ENABLED = normalized;
  delete expanded.ENABLE_LIVE_TRADING;
  return expanded;
}

function renderRuntimeStatus(payload) {
  el('runtimeSavedMode').textContent = formatModeLabel(payload.saved_mode || 'paper');
  el('runtimeRunningMode').textContent = formatModeLabel(payload.running_mode || 'paper');
  el('runtimeRestartRequired').textContent = payload.restart_required ? '需要' : '不需要';
  el('runtimeLiveReady').textContent = payload.live_ready ? '已就绪' : '未就绪';
  el('runtimeLiveError').textContent = payload.live_validation_error || '--';
  el('runtimeSummaryText').textContent =
    '当前模式 ' + formatModeLabel(payload.running_mode || 'paper') +
    ' / 目标模式 ' + formatModeLabel(payload.saved_mode || 'paper') +
    ' / 是否待切换 ' + (payload.restart_required ? '需要' : '不需要') +
    ' / 实盘就绪 ' + (payload.live_ready ? '已就绪' : '未就绪');
  const redeemVisible = !!(payload.redeem_visible || payload.redeem_enabled || ((payload.running_mode || payload.active_mode || 'paper') === 'live'));
  el('runtimeRedeemRows').style.display = redeemVisible ? '' : 'none';
  el('runtimeRedeemEnabled').textContent = payload.redeem_enabled ? '已开启' : '未开启';
  el('runtimeRedeemPending').textContent = String(payload.redeem_pending_count ?? 0);
  el('runtimeRedeemResult').textContent = payload.redeem_last_result || '--';
  el('runtimeRedeemAttempt').textContent = payload.redeem_last_attempt_at ? fmtIso(payload.redeem_last_attempt_at) : '--';
  el('runtimeRedeemTxHash').textContent = payload.redeem_last_tx_hash || '--';
  el('runtimeOptimizerEnabled').textContent = payload.optimizer_enabled ? '已开启' : '未开启';
  el('runtimeOptimizerChampion').textContent = payload.optimizer_champion_id || '--';
  el('runtimeOptimizerChallengers').textContent = String((payload.optimizer_active_challengers || []).length);
  el('runtimeOptimizerPromotable').textContent = String(payload.optimizer_promotable_count ?? 0);
  el('runtimeOptimizerLastRun').textContent = payload.optimizer_last_run_at ? fmtIso(payload.optimizer_last_run_at) : '--';
  el('runtimeOptimizerChallengerList').innerHTML = renderOptimizerCandidateList(payload.optimizer_active_challengers || []);
  el('runtimeOptimizerPromotableList').innerHTML = renderOptimizerCandidateList(payload.optimizer_promotable_candidates || []);
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


function shouldConfirmLiveModeSwitch(previousMode, nextMode) {
  previousMode = String(previousMode || 'paper').toLowerCase();
  nextMode = String(nextMode || 'paper').toLowerCase();
  return previousMode !== 'live' && nextMode === 'live';
}

function renderConfig(payload) {
  state.config = payload;
  applyTimeframeCopy(payload);
  el('cfgSavedAt').textContent = payload.saved_at ? fmtIso(payload.saved_at) : '--';
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
  const editableKeySet = new Set(['ENABLE_LIVE_TRADING', ...keys.filter((key) => !isSingleLiveToggleKey(key))]);
  const hiddenKeys = new Set(['PAPER_STRATEGY_IDS']);
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
        .filter((key, index, arr) => editableKeySet.has(key) && arr.indexOf(key) === index && !hiddenKeys.has(key)),
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
        focusLabel.textContent = '策略面板：勾选模拟盘策略，并单独指定一个当前主策略。';
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
        multiLabel.textContent = '保存时会自动确保主策略包含在模拟盘策略里。';
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
  form.oninput = () => {
    const liveValues = expandLiveToggleValues(collectConfigValues());
    renderUnifiedStrategyToolbar(state.config, liveValues);
    renderStrategyGuide(state.config, liveValues);
    applyConfigFieldVisibility(liveValues);
    applyAdvancedConfigVisibility(liveValues);
  };
  form.onchange = form.oninput;

  renderStrategyGuide(payload, displayValues);
  applyConfigFieldVisibility(expandLiveToggleValues(displayValues));
  applyAdvancedConfigVisibility(expandLiveToggleValues(displayValues));
  setConfigError('--');
  setChip('cfgStatus', '\u5df2\u52a0\u8f7d', 'ok');
  setSaveButtonState('idle');
}

function collectConfigValues(options) {
  const settings = options || {};
  const payload = {};
  const unifiedStrategyKeys = ['STRATEGY_ID', 'PAPER_STRATEGY_IDS'];
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
  if (settings.includeUnified === false) {
    return payload;
  }
  const focusNode = el('cfg_STRATEGY_ID');
  const multiNode = el('cfg_PAPER_STRATEGY_IDS');
  const rawValues = {
    ...payload,
    STRATEGY_ID: focusNode ? focusNode.value : '',
    PAPER_STRATEGY_IDS: multiNode
      ? Array.from(multiNode.options || []).filter((option) => option.selected).map((option) => option.value).join(',')
      : '',
  };
  const unifiedValues = collectUnifiedStrategyValues(state.config || {}, rawValues);
  payload.STRATEGY_ID = unifiedValues.STRATEGY_ID;
  payload.PAPER_STRATEGY_IDS = unifiedValues.PAPER_STRATEGY_IDS;
  return payload;
}
function areConfigValuesEqual(left, right) {
  const keys = new Set([
    ...Object.keys(left || {}),
    ...Object.keys(right || {}),
  ]);
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
    if (shouldConfirmLiveModeSwitch(previousMode, nextMode) && !window.confirm('切换为实盘后，后续下单会按实盘配置执行。确认继续吗？')) {
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
  const basePairs = [
    ['ws_enabled', ws.ws_enabled],
    ['ws_available', ws.ws_available],
    ['ws_connected', ws.ws_connected],
    ['reconnects', ws.reconnects],
    ['invalid_ops', ws.invalid_ops],
    ['connect_attempts', ws.connect_attempts],
    ['subscribed_assets', ws.subscribed_assets],
    ['cached_assets', ws.cached_assets],
    ['last_message_age_s', ws.last_message_age_s],
    ['last_error', ws.last_error],
  ];

  const used = new Set(basePairs.map((item) => item[0]));
  const extraPairs = Object.entries(ws || {}).filter(([k]) => !used.has(k));
  const pairs = basePairs.concat(extraPairs);

  const rows = pairs.map(([key, value]) => {
    let shown = (value === null || value === undefined || value === '') ? '--' : String(value);
    if (key in STATUS_LABELS && (value === true || value === false)) {
      shown = STATUS_LABELS[value];
    }
    if (key === 'last_error') {
      shown = reasonText(shown);
    }
    if (key === 'last_message_age_s' && shown !== '--') {
      const n = toNum(shown);
      shown = n === null ? shown : n.toFixed(3);
    }
    const displayKey = RUNTIME_LABELS[key] || key;
    return '<div class=\"runtime-item\"><span class=\"rk\">' + esc(displayKey) + '</span><span class=\"rv\">' + esc(shown) + '</span></div>';
  }).join('');

  list.innerHTML = rows || '<div class=\"empty\">暂无 WS 运行数据</div>';

  if (staleGuard) {
    setChip('wsHealth', '已触发陈旧保护', 'err');
  } else if (ws && ws.ws_connected) {
    setChip('wsHealth', '连接正常', 'ok');
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
  state.paperStrategyFilter = String(strategyView.selected || state.paperStrategyFilter || 'all');
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

  el('marketUpdatedAt').textContent = fmtIso(payload.timestamp);

  renderWsRuntime(payload.ws_runtime || {}, !!payload.ws_stale_guard_triggered);
}

function renderSummary(payload) {
  state.summary = payload;
  const latest = payload.latest || null;

  if (!latest) {
    el('sumDate').textContent = '--';
    el('sumTrades').textContent = '--';
    el('sumHitRate').textContent = '--';
    el('sumTotalPnl').textContent = '--';
    el('sumDrawdown').textContent = '--';
    el('sumStrongRate').textContent = '--';
    el('daysTbody').innerHTML = '<tr><td colspan=\"5\" class=\"empty\">暂无纸面数据</td></tr>';
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

  el('daysTbody').innerHTML = rows || '<tr><td colspan=\"5\" class=\"empty\">暂无纸面数据</td></tr>';
  setReportStatus('paperStatus', '汇总', '已更新', 'ok');
}

function renderRecent(payload) {
  state.recent = payload;
  const rows = payload.rows || [];
  const tbody = el('recentTbody');
  const runningMode = String((((state.config || {}).runtime_status || {}).active_mode || (((state.config || {}).runtime_status || {}).running_mode) || 'paper')).toLowerCase();
  el('recentPanelDesc').textContent = recentStrategyHeaderText();
  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan=13 class=empty>' + (runningMode === 'live' ? '最近没有实盘交易记录' : '最近没有纸面交易记录') + '</td></tr>';
    setReportStatus('recentStatus', '明细', '0 行', 'warn');
    return;
  }

  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="13" class="empty">最近没有纸面交易记录</td></tr>';
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
    const checkTitle = '官方结果: ' + (row.resolved_expected_result || '--');
    const rowClass = isPending ? 'recent-pending' : (isMissedEntry ? 'recent-missed-entry' : '');
    const reasonHtml = isMissedEntry
      ? ('<span class="skip-reason-badge missed-entry">' + esc(reasonText(row.skip_reason)) + '</span>')
      : esc(reasonText(row.skip_reason));

    return '<tr class="' + esc(rowClass) + '">' +
      '<td>' + esc(fmtIso(row.timestamp)) + '</td>' +
      '<td title="' + esc(row.event_slug || '--') + '">' + esc(formatRoundSlug(row.event_slug)) + '</td>' +
      '<td class="' + esc(sideCls) + '">' + esc(sideText(side)) + '</td>' +
      '<td>' + esc(fmtNum(row.price, 4)) + '</td>' +
      '<td>' + esc(fmtNum(row.order_cost, 4)) + '</td>' +
      '<td class="' + (isPending ? 'trade-skip' : '') + '">' + esc(resultText) + '</td>' +
      '<td class="' + esc(checkCls) + '" title="' + esc(checkTitle) + '">' + esc(checkText) + '</td>' +
      '<td>' + esc(fmtNum(row.resolved_price_to_beat, 2)) + '</td>' +
      '<td>' + esc(fmtNum(row.resolved_final_price, 2)) + '</td>' +
      '<td class="' + esc(pnlCls) + '">' + esc(fmtPnl(row.trade_pnl, 4)) + '</td>' +
      '<td class="' + esc(cashCls) + '">' + esc(fmtPnl(row.cash_pnl, 4)) + '</td>' +
      '<td>' + reasonHtml + '</td>' +
      '<td>' + esc(fmtPnl(row.signal_delta, 4)) + '</td>' +
      '</tr>';
  }).join('');

  tbody.innerHTML = html;
  setReportStatus('recentStatus', '明细', pendingCount > 0 ? (rows.length + ' 行 · ' + pendingCount + ' 待结算') : (rows.length + ' 行'), pendingCount > 0 ? 'warn' : 'ok');
  if (pendingCount === 0) {
    setReportStatus('recentStatus', '明细', rows.length + ' 行' + (runningMode === 'live' ? ' · 实盘' : ''), pendingCount > 0 ? 'warn' : 'ok');
  }
}

async function refreshMarket() {
  try {
    const strategy = encodeURIComponent(String(state.paperStrategyFilter || 'all'));
    const data = await apiGet('/api/market?strategy=' + strategy);
    renderMarket(data);
  } catch (err) {
    setChip('marketHealth', '刷新失败', 'err');
    console.error(err);
  }
}

async function refreshSummary() {
  try {
    const strategy = encodeURIComponent(effectivePaperSummaryStrategyFilter());
    const summaryEndpoint = '/api/paper/summary?strategy=' + strategy;
    const data = await apiGet(summaryEndpoint);
    renderSummary(data);
  } catch (err) {
    setReportStatus('paperStatus', '汇总', '刷新失败', 'err');
    console.error(err);
  }
}

async function refreshRecent() {
  try {
    const runningMode = String((((state.config || {}).runtime_status || {}).active_mode || (((state.config || {}).runtime_status || {}).running_mode) || 'paper')).toLowerCase();
    const strategy = encodeURIComponent(effectivePaperRecentStrategyFilter());
    const recentEndpoint = runningMode === 'live' ? '/api/live/recent?limit=80' : '/api/paper/recent?limit=80&strategy=' + strategy;
    const data = await apiGet(recentEndpoint);
    renderRecent(data);
  } catch (err) {
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
  el('runtimeDetailsToggle').addEventListener('click', () => {
    state.runtimeDetailsOpen = !state.runtimeDetailsOpen;
    toggleFoldSection('runtimeDetails', state.runtimeDetailsOpen);
  });
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


