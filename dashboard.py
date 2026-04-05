from __future__ import annotations

import csv
import errno
import json
import os
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from config import AppConfig, build_config_from_env_values, load_env_file_values
from models import MarketWindow
from paper_report import summarize_paper_trades
from polymarket_api import PolymarketClient
from risk_and_sizing import build_trade_plan
from strategy import get_side_for_round
from trader import (
    _entry_window_missed,
    _entry_time_for_round,
    _resolve_side_from_strategy,
    _ws_is_stale_for_trade,
    load_session_state,
    resolve_quote_price,
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


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


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
    }


def _field_groups() -> list[dict[str, Any]]:
    return [
        {
            "title": "基础策略",
            "description": "决定方向节奏、下注模式和主要风险边界。",
            "keys": [
                "STRATEGY_ID",
                "TARGET_PROFIT",
                "BET_SIZING_MODE",
                "BASE_ORDER_COST",
                "MAX_CONSECUTIVE_LOSSES",
                "MAX_STAKE",
                "MAX_PRICE_THRESHOLD",
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
        "STRATEGY_ID",
        "TARGET_PROFIT",
        "BET_SIZING_MODE",
        "BASE_ORDER_COST",
        "MAX_CONSECUTIVE_LOSSES",
        "MAX_STAKE",
        "MAX_PRICE_THRESHOLD",
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
        "STRATEGY_ID": "基础策略",
        "TARGET_PROFIT": "每次目标净利",
        "BET_SIZING_MODE": "下注模式",
        "BASE_ORDER_COST": "固定起始下注金额",
        "MAX_CONSECUTIVE_LOSSES": "连亏重置轮数",
        "MAX_STAKE": "单笔最大下注金额",
        "MAX_PRICE_THRESHOLD": "最高买入价格阈值",
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
        "STRATEGY_ID": ["1", "2", "3", "4", "5"],
        "BET_SIZING_MODE": ["FIXED_BASE_COST", "TARGET_PROFIT"],
        "SIGNAL_WEAK_SIGNAL_MODE": ["SKIP", "FALLBACK"],
        "SIGNAL_FALLBACK_STRATEGY_ID": ["1", "2", "3", "4"],
        "WS_ENABLED": ["true", "false"],
    }

    CONFIG_ATTR_MAP: dict[str, str] = {
        "STRATEGY_ID": "strategy_id",
        "TARGET_PROFIT": "target_profit",
        "BET_SIZING_MODE": "bet_sizing_mode",
        "BASE_ORDER_COST": "base_order_cost",
        "MAX_CONSECUTIVE_LOSSES": "max_consecutive_losses",
        "MAX_STAKE": "max_stake",
        "MAX_PRICE_THRESHOLD": "max_price_threshold",
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
        "SIGNAL_FALLBACK_STRATEGY_ID",
        "SIGNAL_HISTORY_FIDELITY_SECONDS",
        "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS",
        "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS",
        "MAX_STAKE_SKIP_ALERT_THRESHOLD",
        "WS_QUOTE_STALE_SECONDS",
        "WS_CONNECT_TIMEOUT_SECONDS",
    )

    FLOAT_CONFIG_KEYS: tuple[str, ...] = (
        "TARGET_PROFIT",
        "BASE_ORDER_COST",
        "MAX_STAKE",
        "MAX_PRICE_THRESHOLD",
        "SIGNAL_MOMENTUM_THRESHOLD",
        "SIGNAL_DYNAMIC_THRESHOLD_K",
        "WS_TRADE_GUARD_STALE_SECONDS",
    )

    BOOL_CONFIG_KEYS: tuple[str, ...] = ("WS_ENABLED",)

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
    }
    FIELD_HELP: dict[str, str] = {
        "STRATEGY_ID": "1~4 是固定节奏策略；5 是动量信号策略，会额外用到下方 signal_* 参数。",
        "TARGET_PROFIT": "TARGET_PROFIT 模式下，每轮希望净赚多少；FIXED_BASE_COST 模式下主要用于研究，不直接决定起始下注额。",
        "BET_SIZING_MODE": "FIXED_BASE_COST 为固定起始金额，TARGET_PROFIT 为按目标盈利反推下单金额。",
        "BASE_ORDER_COST": "仅 FIXED_BASE_COST 模式下生效，赢后回到这个起始金额。",
        "MAX_CONSECUTIVE_LOSSES": "连续亏损达到该轮数后，策略会触发止损重置。",
        "MAX_STAKE": "单笔实际花费的 USDC 上限，超过会直接跳过。",
        "MAX_PRICE_THRESHOLD": "目标方向价格高于此阈值就不入场，避免买得太贵。",
        "SIGNAL_MOMENTUM_THRESHOLD": "策略 5 的基础阈值，比较 current_up - open_up 的绝对变化。",
        "SIGNAL_WEAK_SIGNAL_MODE": "弱信号时可直接跳过，或回退到一个固定节奏策略。",
        "SIGNAL_FALLBACK_STRATEGY_ID": "仅在策略 5 且弱信号回退时使用，建议选 1~4 中最熟悉的一种。",
        "SIGNAL_HISTORY_FIDELITY_SECONDS": "拉取历史价格序列时的采样粒度，越小越细但请求更重。",
        "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS": "对齐开盘锚点允许的最大时间偏移，过大可能把信号锚点拉偏。",
        "SIGNAL_DYNAMIC_THRESHOLD_K": "动态阈值系数，实际阈值取 max(基础阈值, k * sigma)。",
        "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS": "动态阈值至少需要的样本点数，不足时退回基础阈值。",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "离入场很近时锁定方向，避免最后几秒来回跳边。",
        "MAX_STAKE_SKIP_ALERT_THRESHOLD": "连续多少次因超过 MAX_STAKE 跳过后打印告警。",
        "WS_ENABLED": "开启后优先使用 WebSocket 缓存盘口，失败时自动回退 HTTP。",
        "WS_QUOTE_STALE_SECONDS": "超过这个秒数未更新，就认为 WS 行情过期。",
        "WS_TRADE_GUARD_STALE_SECONDS": "交易前若 WS 行情陈旧超过该阈值，会直接阻止下单。",
        "WS_CONNECT_TIMEOUT_SECONDS": "建立 WebSocket 连接时允许等待的超时时间。",
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

        if key in cls.BOOL_CONFIG_KEYS:
            return cls._normalize_bool_config_value(key, normalized)

        if key in cls.SELECT_OPTIONS:
            allowed = cls.SELECT_OPTIONS[key]
            upper_value = normalized.upper()
            if upper_value in allowed:
                return upper_value
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

        raise ValueError(f"Unsupported config key: {key}")

    def __init__(self, *, env_file: Path) -> None:
        self.env_file = Path(env_file)
        self._lock = threading.RLock()
        self._env_values = load_env_file_values(self.env_file)
        self._cfg = self._build_config(self._env_values)
        self._client = PolymarketClient(self._cfg)
        self._last_saved_at: datetime | None = None

    def close(self) -> None:
        with self._lock:
            client = self._client
            self._client = None  # type: ignore[assignment]
        if client is not None:
            client.close()

    def _build_config(self, env_values: dict[str, str]) -> AppConfig:
        return build_config_from_env_values(env_values)

    def _refresh_runtime(self) -> None:
        with self._lock:
            old_client = self._client
            self._cfg = self._build_config(self._env_values)
            self._client = PolymarketClient(self._cfg)
        old_client.close()

    def _merged_env_values(self) -> tuple[dict[str, str], dict[str, str]]:
        merged: dict[str, str] = {}
        validation_errors: dict[str, str] = {}
        for key in self.EDITABLE_CONFIG_KEYS:
            effective_value = _fmt_env(getattr(self._cfg, self.CONFIG_ATTR_MAP[key]))
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
            return {
                "env_file": str(self.env_file),
                "env_values": env_values,
                "editable_keys": list(self.EDITABLE_CONFIG_KEYS),
                "labels": self.CONFIG_LABELS,
                "select_options": self.SELECT_OPTIONS,
                "strategy_catalog": self.STRATEGY_CATALOG,
                "field_groups": self.FIELD_GROUPS,
                "field_scope": self.FIELD_SCOPE,
                "field_help": self.FIELD_HELP,
                "validation_errors": validation_errors,
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
        for key, value in values.items():
            normalized = "" if value is None else str(value).strip()
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

        self._refresh_runtime()
        return self.get_config_payload()

    def get_market_payload(self) -> dict[str, Any]:
        with self._lock:
            cfg = self._cfg
            client = self._client

        now = datetime.now(timezone.utc)
        session_state = load_session_state(cfg.logs_dir / "session_state.json")
        current_round, next_round = client.find_current_and_next_rounds(now=now)
        display_round = _select_display_round(current_round=current_round, next_round=next_round)
        target_round = display_round
        ws_runtime = client.get_ws_runtime_stats()

        if target_round is None:
            return {
                "ok": True,
                "timestamp": _iso(now),
                "round": None,
                "quote": None,
                "signal": None,
                "plan": None,
                "session_state": asdict(session_state),
                "ws_runtime": ws_runtime,
                "ws_stale_guard_triggered": False,
                "message": "当前没有可用的5分钟轮次。",
            }

        market = client.get_market_by_slug(target_round.slug)
        quote = client.quote_from_market(market)
        entry_time = _entry_time_for_round(cfg, target_round)

        side_decision = _resolve_side_from_strategy(
            cfg=cfg,
            state=session_state,
            slug=target_round.slug,
            quote=quote,
            market_client=client,
            window=target_round,
            now=now,
            entry_time=entry_time,
        )

        side = side_decision.side
        price = resolve_quote_price(side, quote) if side in {"UP", "DOWN"} else None
        ws_stale = _ws_is_stale_for_trade(client, cfg)

        if side in {"UP", "DOWN"} and not ws_stale and not _entry_window_missed(
            now,
            entry_time,
            grace_seconds=cfg.entry_grace_seconds,
        ):
            plan_obj = build_trade_plan(
                state=session_state,
                side=side,
                price=price,
                target_profit=cfg.target_profit,
                max_price_threshold=cfg.max_price_threshold,
                max_stake=cfg.max_stake,
                daily_loss_cap=cfg.daily_loss_cap,
                max_consecutive_losses=cfg.max_consecutive_losses,
                bet_sizing_mode=cfg.bet_sizing_mode,
                base_order_cost=cfg.base_order_cost,
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
                grace_seconds=cfg.entry_grace_seconds,
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
            "session_state": asdict(session_state),
            "ws_runtime": ws_runtime,
            "ws_stale_guard_triggered": ws_stale,
        }

    def get_paper_summary_payload(self) -> dict[str, Any]:
        with self._lock:
            paper_csv = self._cfg.logs_dir / "paper_trades.csv"
        try:
            daily = summarize_paper_trades(paper_csv, tz_offset="+08:00")
        except (FileNotFoundError, ValueError):
            daily = []
        days = [asdict(item) for item in daily[-14:]]
        return {
            "csv_path": str(paper_csv),
            "tz_offset": "+08:00",
            "days": days,
            "latest": days[-1] if days else None,
        }

    def get_recent_trades_payload(self, *, limit: int) -> dict[str, Any]:
        with self._lock:
            paper_csv = self._cfg.logs_dir / "paper_trades.csv"
        rows = _tail_csv_rows(paper_csv, limit=max(1, min(300, int(limit))))
        return {"csv_path": str(paper_csv), "count": len(rows), "rows": rows}


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
                self._send_json(self.dashboard_state.get_market_payload())
                return
            if parsed.path == "/api/paper/summary":
                self._send_json(self.dashboard_state.get_paper_summary_payload())
                return
            if parsed.path == "/api/paper/recent":
                query = parse_qs(parsed.query)
                limit = int((query.get("limit") or ["20"])[0])
                self._send_json(self.dashboard_state.get_recent_trades_payload(limit=limit))
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


def create_dashboard_runtime(
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    env_file: Path = Path(".env.dashboard"),
) -> DashboardRuntime:
    env_path = Path(env_file)
    state = DashboardState(env_file=env_path)

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
  <title>BTC 5分钟量化控制台</title>
  <link rel=\"stylesheet\" href=\"/dashboard.css\">
</head>
<body>
  <header class=\"topbar\">
    <div class=\"brand-wrap\">
      <div class=\"brand\">QUANT_CMD · BTC_5M</div>
      <div class=\"subtitle\">策略参数、实时盘口、信号决策、纸面收益一屏联动</div>
    </div>
    <div class=\"top-actions\">
      <div id=\"clockLocal\" class=\"clock\">本地时间 --</div>
      <div id=\"clockUtc\" class=\"clock\">UTC --</div>
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
        <div class=\"meta\">
          <div class=\"meta-item\">
            <span class=\"meta-label\">配置文件</span>
            <span id=\"cfgEnvFile\" class=\"meta-value\">--</span>
          </div>
          <div class=\"meta-item\">
            <span class=\"meta-label\">最近保存</span>
            <span id=\"cfgSavedAt\" class=\"meta-value\">--</span>
          </div>
        </div>

        <div id=\"strategyGuideCard\" class=\"strategy-guide-card\"></div>

        <form id=\"configForm\" class=\"form-grid\"></form>

        <div class=\"actions\">
          <button id=\"btnToggleKeys\" class=\"btn btn-ghost\" type=\"button\">显示内部键名：关</button>
          <button id=\"btnReloadConfig\" class=\"btn btn-ghost\" type=\"button\">重新读取参数</button>
          <button id=\"btnSaveConfig\" class=\"btn btn-primary\" type=\"button\">保存参数</button>
        </div>
      </div>
    </section>

    <section class=\"panel center-stack\">
      <div class=\"panel-head\">
        <div>
          <div class=\"head-title\">行情与信号</div>
          <div class=\"head-desc\">5分钟轮次行情 / 方向信号 / 下注计划</div>
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

          <div class=\"box\">
            <div class=\"box-title\">信号判断</div>
            <div class=\"row\">
              <span class=\"label\">方向</span>
              <span id=\"signalSide\" class=\"value\">--</span>
            </div>
            <div class=\"row\">
              <span class=\"label\">原因</span>
              <span id=\"signalReason\" class=\"value\">--</span>
            </div>
            <div class=\"kv-grid\">
              <div class=\"kv\"><div class=\"k\">开盘看涨价</div><div id=\"signalOpenUp\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">当前看涨价</div><div id=\"signalCurrentUp\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">信号阈值</div><div id=\"signalThreshold\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">信号偏移</div><div id=\"signalDelta\" class=\"v\">--</div></div>
            </div>
            <div class=\"row\">
              <span class=\"label\">已锁边</span>
              <span id=\"signalLocked\" class=\"value\">--</span>
            </div>
          </div>
        </div>

        <div class=\"split\">
          <div class=\"box\">
            <div class=\"box-title\">下注计划与风控</div>
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
          </div>

          <div class=\"box\">
            <div class=\"box-title\">会话状态</div>
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
    </section>

    <section class=\"stack right-stack\">
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
          <div class=\"footnote\">说明: 行情来源为 websocket 表示使用 WS 缓存盘口, 为 http 表示回退 HTTP 拉取。</div>
        </div>
      </div>

      <div class=\"panel\">
        <div class=\"panel-head\">
          <div>
            <div class=\"head-title\">纸面交易汇总</div>
            <div class=\"head-desc\">按北京时间聚合的纸面成绩</div>
          </div>
          <div id=\"paperStatus\" class=\"chip\">待刷新</div>
        </div>
        <div class=\"panel-body\">
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
        </div>
      </div>
    </section>

    <section class=\"panel trades-panel\">
      <div class=\"panel-head\">
        <div>
          <div class=\"head-title\">最近纸面交易明细</div>
          <div class=\"head-desc\">最近纸面交易流水 (默认 80 行)</div>
        </div>
        <div id=\"recentStatus\" class=\"chip\">待刷新</div>
      </div>
      <div class=\"table-wrap\">
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>轮次</th>
              <th>方向</th>
              <th>价格</th>
              <th>下注金额</th>
              <th>结果</th>
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

.field label {
  font-size: 11px;
  color: var(--muted);
  line-height: 1.45;
}

.field input,
.field select {
  width: 100%;
  border: 1px solid #2f4b70;
  border-radius: 8px;
  background: #0a1528;
  color: var(--text);
  padding: 6px 8px;
  font-size: 12px;
  font-family: var(--mono);
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

.trades-panel {
  grid-column: 1 / -1;
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

  .right-stack {
    grid-column: span 2;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
  }
}

@media (max-width: 1024px) {
  .layout { grid-template-columns: 1fr; }
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
  countdownSnapshotAtMs: null,
  countdownBaseSeconds: null,
  showInternalKeys: false,
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
    intro: '先确认基础策略、下注模式、最大下注金额，再观察 3~5 个轮次，不要频繁同时改动多项参数。',
    sections: [
      {
        title: '先看哪里',
        bullets: [
          '先看 行情与信号，确认当前轮次、方向判断和倒计时。',
          '再看 下注计划与风控，重点关注是否下单、跳过原因和预期收益。',
          '然后看 会话状态，确认累计盈亏、待回补亏损和连续亏损轮数。',
          '最后看 实时连接状态，判断 websocket 行情是否可靠。',
        ],
      },
      {
        title: '怎么安全改参数',
        bullets: [
          '先确认当前基础策略，固定节奏策略和策略 5 的可调参数不同。',
          '每次只改一类参数，不要同时改策略、阈值和下注金额。',
          '保存后以页面显示的有效值和字段提示为准，不要只看自己输入了什么。',
          '如果字段出现校验错误，说明该输入没有真正生效，需要先修正。',
        ],
      },
      {
        title: '怎么判断当前能不能跑',
        bullets: [
          '是否下单=执行，说明当前轮次、价格、风控和 WS 状态都允许。',
          '是否下单=跳过，先看跳过原因，不要先怀疑策略失效。',
          '价格超过阈值、信号太弱、金额超限，属于常见可接受跳过。',
          'WS 数据陈旧、当日亏损上限、连续亏损重置，属于要优先排查的跳过。',
        ],
      },
      {
        title: '出问题先看哪里',
        bullets: [
          '保存了但效果不对：先看参数区字段提示和有效值。',
          '一直不下单：先看 跳过原因 和 实时连接状态。',
          '方向看不懂：去 策略说明，对照固定节奏或动量逻辑。',
          '当天收益异常：看 纸面交易汇总 和 最近纸面交易明细。',
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
          '用于判断 websocket 行情是否可信。',
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
        title: '最近纸面交易明细',
        bullets: [
          '用于排查最近交易到底发生了什么。',
          '重点关注时间、方向、结果、跳过原因和信号偏移。',
        ],
      },
    ],
  },
};

const HELP_FAQ = [
  ['为什么我保存了参数，但感觉没生效？', '先看参数区字段提示和有效值；如果输入非法，系统会回退到有效配置，而不是按错误值运行。'],
  ['为什么当前显示不下单？', '先看下注计划与风控里的跳过原因，再区分是价格、风控、信号还是 WS 保护导致。'],
  ['为什么策略 5 经常没信号？', '策略 5 不是固定节奏，需要价格变化达到阈值；弱信号时会按 SKIP 或 FALLBACK 处理。'],
  ['为什么方向和我想的不一样？', '固定节奏策略先看轮次编号；动量策略则要看开盘价、当前价、阈值和偏移。'],
  ['为什么 WS 保护会触发？', '说明 websocket 行情太旧，系统为了避免使用过期数据下单而阻止执行。'],
  ['为什么当日已实现盈亏归零了？', '这是日切后的日内统计重置；累计盈亏仍然保留在会话状态里。'],
  ['为什么超过最大下注金额后一直跳过？', '当前恢复亏损和价格条件共同推高了所需下单金额，先看待回补亏损和 MAX_STAKE。'],
  ['新手最容易改错什么？', '一次改太多参数、没分清固定节奏和动量策略、把 WS 保护误以为是策略问题。'],
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
};

const OPTION_LABELS = {
  BET_SIZING_MODE: {
    FIXED_BASE_COST: '固定金额模式',
    TARGET_PROFIT: '目标收益模式',
  },
  SIGNAL_WEAK_SIGNAL_MODE: {
    SKIP: '弱信号跳过',
    FALLBACK: '弱信号回退',
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
  price_above_threshold: '价格超过上限阈值',
  order_cost_above_max_stake: '下单金额超过单笔上限',
  order_size_not_positive: '下单份额无效',
  daily_loss_cap_reached: '触发当日亏损上限',
  max_consecutive_losses_reached: '达到连续亏损重置阈值',
  stop_loss_triggered: '触发止损重置',
  manual_skip: '人工跳过',
};

const CONFIG_KEY_NAMES = {
  STRATEGY_ID: '基础策略',
  TARGET_PROFIT: '每次目标净利',
  BET_SIZING_MODE: '下注模式',
  BASE_ORDER_COST: '固定起始下注金额',
  MAX_CONSECUTIVE_LOSSES: '连亏重置轮数',
  MAX_STAKE: '单笔最大下注金额',
  MAX_PRICE_THRESHOLD: '最高买入价格阈值',
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
  return '未识别原因：' + String(reason) + '（可尝试刷新页面）';
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
  el('btnToggleKeys').textContent = '显示内部键名：' + (state.showInternalKeys ? '开' : '关');
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
  if (key === 'STRATEGY_ID' || key === 'SIGNAL_FALLBACK_STRATEGY_ID') {
    return String(opt) + ' | ' + strategyShortLabel(payload, opt);
  }
  const optMap = OPTION_LABELS[key] || {};
  return optMap[opt] || opt;
}

function strategyPreviewText(token) {
  if (token === 'UP') return '看涨';
  if (token === 'DOWN') return '看跌';
  if (token === 'MOMENTUM') return '动量判断';
  if (token === 'THRESHOLD') return '阈值过滤';
  if (token === 'FALLBACK') return '弱信号回退';
  return String(token || '--');
}

function strategyPreviewClass(token) {
  if (token === 'UP') return 'trade-up';
  if (token === 'DOWN') return 'trade-down';
  return 'strategy-info';
}

function renderStrategyPills(tokens) {
  if (!Array.isArray(tokens) || tokens.length === 0) {
    return '<span class="strategy-pill strategy-info">暂无节奏预览</span>';
  }
  return tokens.map((token) => {
    return '<span class="strategy-pill ' + esc(strategyPreviewClass(token)) + '">' + esc(strategyPreviewText(token)) + '</span>';
  }).join('');
}

function renderStrategyGuide(payload, values) {
  const node = el('strategyGuideCard');
  if (!node) {
    return;
  }

  const currentValues = values || {};
  const envValues = (payload && payload.env_values) || {};
  const strategyId = String(currentValues.STRATEGY_ID ?? envValues.STRATEGY_ID ?? '');
  const meta = strategyMeta(payload, strategyId);
  if (!meta) {
    node.innerHTML = '<div class="empty">暂无策略说明</div>';
    return;
  }

  let extra = '';
  if (strategyId === '5') {
    const weakModeRaw = String(currentValues.SIGNAL_WEAK_SIGNAL_MODE ?? envValues.SIGNAL_WEAK_SIGNAL_MODE ?? '--');
    const weakModeText = (OPTION_LABELS.SIGNAL_WEAK_SIGNAL_MODE || {})[weakModeRaw] || weakModeRaw;
    const fallbackId = String(currentValues.SIGNAL_FALLBACK_STRATEGY_ID ?? envValues.SIGNAL_FALLBACK_STRATEGY_ID ?? '');
    const fallbackMeta = strategyMeta(payload, fallbackId);
    const fallbackPreview = fallbackMeta && Array.isArray(fallbackMeta.preview) ? renderStrategyPills(fallbackMeta.preview) : '';
    extra =
      '<div class="strategy-guide-note">弱信号处理：' + esc(weakModeText) +
      '；回退策略：' + esc(strategyShortLabel(payload, fallbackId)) + '</div>' +
      '<div class="strategy-guide-meta">' + fallbackPreview + '</div>';
  }

  node.innerHTML =
    '<div class="strategy-guide-head">' +
      '<div>' +
        '<div class="strategy-guide-title">' + esc(strategyId + ' | ' + meta.label) + '</div>' +
        '<div class="strategy-guide-subtitle">' + esc(meta.summary || '') + '</div>' +
      '</div>' +
      '<span class="chip ok">配置解读</span>' +
    '</div>' +
    '<div class="strategy-guide-preview">' + renderStrategyPills(meta.preview || []) + '</div>' +
    '<div class="strategy-guide-note">' + esc(meta.detail || '') + '</div>' +
    extra;
}

function applyConfigFieldVisibility(values) {
  const strategyId = String((values && values.STRATEGY_ID) || '');
  const isStrategyFive = strategyId === '5';

  document.querySelectorAll('.field[data-field-scope]').forEach((node) => {
    const scope = node.getAttribute('data-field-scope') || 'all';
    const shouldMute = scope === 'strategy_5_only' && !isStrategyFive;
    node.classList.toggle('field-muted', shouldMute);
    const note = node.querySelector('.field-scope-note');
    if (note) {
      note.textContent = shouldMute ? '当前基础策略未使用此参数，仅策略 5 使用' : '';
    }
  });

  document.querySelectorAll('.config-group[data-group-scope]').forEach((node) => {
    const scope = node.getAttribute('data-group-scope') || 'all';
    const shouldMute = scope === 'strategy_5_only' && !isStrategyFive;
    node.classList.toggle('config-group-muted', shouldMute);
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

  return groups.map((group) => {
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
  const activeId = String(envValues.STRATEGY_ID || '');
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
        '；回退策略：' + esc(strategyShortLabel(payload, envValues.SIGNAL_FALLBACK_STRATEGY_ID)) +
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
    '<a href="docs/dashboard_runbook.md" target="_blank" rel="noreferrer">Dashboard Runbook</a>' +
    '<a href="docs/operations_runbook.md" target="_blank" rel="noreferrer">Operations Runbook</a>' +
    '<a href="docs/daily_ops_checklist.md" target="_blank" rel="noreferrer">Daily Checklist</a>';

  tabs.querySelectorAll('[data-help-tab]').forEach((node) => {
    node.addEventListener('click', () => {
      state.helpTab = node.getAttribute('data-help-tab') || 'quickstart';
      renderHelpDrawer();
    });
  });
}

function renderConfig(payload) {
  state.config = payload;
  el('cfgEnvFile').textContent = payload.env_file || '--';
  el('cfgSavedAt').textContent = payload.saved_at ? fmtIso(payload.saved_at) : '--';

  const form = el('configForm');
  form.innerHTML = '';

  const keys = payload.editable_keys || [];
  const labels = payload.labels || {};
  const values = payload.env_values || {};
  const options = payload.select_options || {};
  const fieldHelp = payload.field_help || {};
  const fieldScope = payload.field_scope || {};
  const validationErrors = payload.validation_errors || {};
  const fieldGroups = Array.isArray(payload.field_groups) && payload.field_groups.length > 0
    ? payload.field_groups
    : [{ title: '全部参数', description: '', keys }];
  const editableKeySet = new Set(keys);

  for (const group of fieldGroups) {
    const groupKeys = (group.keys || []).filter((key) => editableKeySet.has(key));
    if (groupKeys.length === 0) {
      continue;
    }

    const section = document.createElement('section');
    section.className = 'config-group';
    if (group.scope) {
      section.dataset.groupScope = group.scope;
    }

    const head = document.createElement('div');
    head.className = 'config-group-head';
    head.innerHTML =
      '<div class="config-group-title">' + esc(group.title || '参数分组') + '</div>' +
      '<div class="config-group-desc">' + esc(group.description || '') + '</div>';
    section.appendChild(head);

    const grid = document.createElement('div');
    grid.className = 'group-grid';

    for (const key of groupKeys) {
      const wrap = document.createElement('div');
      wrap.className = 'field';
      wrap.dataset.fieldScope = fieldScope[key] || 'all';

      const label = document.createElement('label');
      label.setAttribute('for', 'cfg_' + key);
      label.textContent = formatConfigLabel(key, labels);
      wrap.appendChild(label);

      if (Array.isArray(options[key]) && options[key].length > 0) {
        const select = document.createElement('select');
        select.id = 'cfg_' + key;
        for (const opt of options[key]) {
          const option = document.createElement('option');
          option.value = opt;
          option.textContent = strategyOptionLabel(key, opt, payload);
          if (String(values[key] ?? '') === String(opt)) {
            option.selected = true;
          }
          select.appendChild(option);
        }
        wrap.appendChild(select);
      } else {
        const input = document.createElement('input');
        input.id = 'cfg_' + key;
        input.type = 'text';
        input.value = String(values[key] ?? '');
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

      grid.appendChild(wrap);
    }

    section.appendChild(grid);
    form.appendChild(section);
  }

  form.oninput = () => {
    const liveValues = collectConfigValues();
    renderStrategyGuide(state.config, liveValues);
    applyConfigFieldVisibility(liveValues);
  };
  form.onchange = form.oninput;

  renderStrategyGuide(payload, values);
  applyConfigFieldVisibility(values);
  setChip('cfgStatus', '已加载', 'ok');
}

function collectConfigValues() {
  const payload = {};
  const keys = (state.config && state.config.editable_keys) || [];
  for (const key of keys) {
    const node = el('cfg_' + key);
    if (node) {
      payload[key] = node.value;
    }
  }
  return payload;
}

async function refreshConfig() {
  try {
    const data = await apiGet('/api/config');
    renderConfig(data);
  } catch (err) {
    setChip('cfgStatus', '读取失败', 'err');
    console.error(err);
  }
}

async function saveConfig() {
  let values = {};
  try {
    setChip('cfgStatus', '保存中', 'warn');
    values = collectConfigValues();
    const data = await apiPost('/api/config', { env_values: values });
    renderConfig(data);
    setChip('cfgStatus', '已保存', 'ok');
  } catch (err) {
    const fieldErrors = err && err.fieldErrors ? err.fieldErrors : {};
    if (Object.keys(fieldErrors).length > 0 && state.config) {
      renderConfig({
        ...state.config,
        env_values: values,
        validation_errors: fieldErrors,
      });
      setChip('cfgStatus', '校验失败', 'err');
    } else {
      setChip('cfgStatus', '保存失败', 'err');
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

function renderMarket(payload) {
  state.market = payload;
  const round = payload.round || null;
  const quote = payload.quote || {};
  const signal = payload.signal || {};
  const plan = payload.plan || {};
  const ss = payload.session_state || {};

  if (!round) {
    el('marketDeadline').textContent = '结束时间 --';
    el('marketSlug').textContent = '暂无可用轮次';
    el('marketTitle').textContent = payload.message || '当前时段没有可交易轮次';
    renderEntryCountdown(null);
    setChip('marketHealth', '???', 'warn');
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

  const signalSide = signal.side || 'SKIP';
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

  el('planShouldTrade').textContent = plan.should_trade ? '执行' : '跳过';
  el('planSide').textContent = sideText(plan.side || signalSide);
  el('planPrice').textContent = fmtNum(plan.price, 4);
  el('planOrderCost').textContent = fmtNum(plan.order_cost, 4);
  el('planOrderSize').textContent = fmtNum(plan.order_size, 6);
  el('planExpectedProfit').textContent = fmtPnl(plan.expected_profit, 4);
  el('planSkipReason').textContent = reasonText(plan.skip_reason);
  el('planStopLoss').textContent = plan.stop_loss_triggered ? '是' : '否';

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
    setChip('paperStatus', '暂无数据', 'warn');
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
  setChip('paperStatus', '已更新', 'ok');
}

function renderRecent(payload) {
  state.recent = payload;
  const rows = payload.rows || [];
  const tbody = el('recentTbody');

  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan=\"10\" class=\"empty\">最近没有纸面交易记录</td></tr>';
    setChip('recentStatus', '0 行', 'warn');
    return;
  }

  const html = rows.map((row) => {
    const side = String(row.side || '').toUpperCase();
    const sideCls = sideClass(side);
    const pnlCls = classifyPnl(row.trade_pnl);
    const cashCls = classifyPnl(row.cash_pnl);

    return '<tr>' +
      '<td>' + esc(fmtIso(row.timestamp)) + '</td>' +
      '<td>' + esc(row.event_slug || '--') + '</td>' +
      '<td class=\"' + esc(sideCls) + '\">' + esc(sideText(side)) + '</td>' +
      '<td>' + esc(fmtNum(row.price, 4)) + '</td>' +
      '<td>' + esc(fmtNum(row.order_cost, 4)) + '</td>' +
      '<td>' + esc(row.result || '--') + '</td>' +
      '<td class=\"' + esc(pnlCls) + '\">' + esc(fmtPnl(row.trade_pnl, 4)) + '</td>' +
      '<td class=\"' + esc(cashCls) + '\">' + esc(fmtPnl(row.cash_pnl, 4)) + '</td>' +
      '<td>' + esc(reasonText(row.skip_reason)) + '</td>' +
      '<td>' + esc(fmtPnl(row.signal_delta, 4)) + '</td>' +
      '</tr>';
  }).join('');

  tbody.innerHTML = html;
  setChip('recentStatus', rows.length + ' 行', 'ok');
}

async function refreshMarket() {
  try {
    const data = await apiGet('/api/market');
    renderMarket(data);
  } catch (err) {
    setChip('marketHealth', '刷新失败', 'err');
    console.error(err);
  }
}

async function refreshSummary() {
  try {
    const data = await apiGet('/api/paper/summary');
    renderSummary(data);
  } catch (err) {
    setChip('paperStatus', '刷新失败', 'err');
    console.error(err);
  }
}

async function refreshRecent() {
  try {
    const data = await apiGet('/api/paper/recent?limit=80');
    renderRecent(data);
  } catch (err) {
    setChip('recentStatus', '刷新失败', 'err');
    console.error(err);
  }
}

async function refreshAll() {
  await Promise.allSettled([
    refreshConfig(),
    refreshMarket(),
    refreshSummary(),
    refreshRecent(),
  ]);
}

function tickClock() {
  const now = new Date();
  el('clockLocal').textContent = '本地 ' + now.toLocaleString('zh-CN', { hour12: false });
  el('clockUtc').textContent = 'UTC ' + now.toISOString().replace('T', ' ').slice(0, 19);
  tickEntryCountdown();
}

function bindActions() {
  syncToggleButtonText();
  el('btnHelp').addEventListener('click', () => {
    state.helpReturnFocusId = 'btnHelp';
    openHelpDrawer('quickstart');
  });
  el('btnHelpClose').addEventListener('click', closeHelpDrawer);
  el('helpBackdrop').addEventListener('click', closeHelpDrawer);
  el('btnToggleKeys').addEventListener('click', () => {
    state.showInternalKeys = !state.showInternalKeys;
    saveUiPrefs();
    syncToggleButtonText();
    if (state.config) {
      renderConfig(state.config);
    }
  });
  el('btnRefreshNow').addEventListener('click', () => {
    refreshAll();
  });
  el('btnReloadConfig').addEventListener('click', () => {
    refreshConfig();
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
