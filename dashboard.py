from __future__ import annotations

import csv
import json
import os
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from config import AppConfig
from paper_report import summarize_paper_trades
from polymarket_api import PolymarketClient
from risk_and_sizing import build_trade_plan
from trader import (
    _entry_time_for_round,
    _resolve_side_from_strategy,
    _ws_is_stale_for_trade,
    load_session_state,
    resolve_quote_price,
)


def _fmt_env(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@contextmanager
def _patched_environ(overrides: dict[str, str]):
    previous: dict[str, str | None] = {}
    for key, value in overrides.items():
        previous[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


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
        "STRATEGY_ID": "策略编号",
        "TARGET_PROFIT": "每次目标净利",
        "BET_SIZING_MODE": "下注模式",
        "BASE_ORDER_COST": "固定起始下注金额",
        "MAX_CONSECUTIVE_LOSSES": "连亏重置轮数",
        "MAX_STAKE": "单笔最大下注金额",
        "MAX_PRICE_THRESHOLD": "最高买入价格阈值",
        "SIGNAL_MOMENTUM_THRESHOLD": "动量阈值",
        "SIGNAL_WEAK_SIGNAL_MODE": "弱信号处理",
        "SIGNAL_FALLBACK_STRATEGY_ID": "弱信号回退策略",
        "SIGNAL_HISTORY_FIDELITY_SECONDS": "信号采样秒数",
        "SIGNAL_ANCHOR_MAX_OFFSET_SECONDS": "开盘锚点最大偏移秒",
        "SIGNAL_DYNAMIC_THRESHOLD_K": "动态阈值系数K",
        "SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS": "动态阈值最少样本点",
        "SIGNAL_LOCK_BEFORE_ENTRY_SECONDS": "入场前锁边秒数",
        "MAX_STAKE_SKIP_ALERT_THRESHOLD": "超额跳过告警阈值",
        "WS_ENABLED": "WebSocket 开关",
        "WS_QUOTE_STALE_SECONDS": "行情过期秒",
        "WS_TRADE_GUARD_STALE_SECONDS": "交易防陈旧阈值秒",
        "WS_CONNECT_TIMEOUT_SECONDS": "WS 连接超时秒",
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

    def __init__(self, *, env_file: Path) -> None:
        self.env_file = Path(env_file)
        self._lock = threading.RLock()
        self._env_values = _read_env_file(self.env_file)
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
        with _patched_environ(env_values):
            return AppConfig()

    def _refresh_runtime(self) -> None:
        with self._lock:
            old_client = self._client
            self._cfg = self._build_config(self._env_values)
            self._client = PolymarketClient(self._cfg)
        old_client.close()

    def _merged_env_values(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for key in self.EDITABLE_CONFIG_KEYS:
            if key in self._env_values:
                merged[key] = self._env_values[key]
            else:
                merged[key] = _fmt_env(getattr(self._cfg, self.CONFIG_ATTR_MAP[key]))
        return merged

    def get_config_payload(self) -> dict[str, Any]:
        with self._lock:
            return {
                "env_file": str(self.env_file),
                "env_values": self._merged_env_values(),
                "editable_keys": list(self.EDITABLE_CONFIG_KEYS),
                "labels": self.CONFIG_LABELS,
                "select_options": self.SELECT_OPTIONS,
                "saved_at": _iso(self._last_saved_at),
            }

    def update_config(self, values: dict[str, str]) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise ValueError("Config payload must be an object.")
        unsupported = sorted(key for key in values.keys() if key not in self.EDITABLE_CONFIG_KEYS)
        if unsupported:
            raise ValueError(f"Unsupported keys: {', '.join(unsupported)}")

        with self._lock:
            for key, value in values.items():
                normalized = "" if value is None else str(value).strip()
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
        target_round = current_round or next_round
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

        if side in {"UP", "DOWN"} and not ws_stale:
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
            reason = "ws_stale" if ws_stale else (side_decision.reason or "signal_unavailable")
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

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_text(self, text: str, *, content_type: str) -> None:
        raw = text.encode("utf-8")
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

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
        except Exception as exc:  # pragma: no cover
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

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
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)


def run_dashboard(*, host: str = "127.0.0.1", port: int = 8787, env_file: Path = Path(".env.dashboard")) -> None:
    state = DashboardState(env_file=Path(env_file))

    class Handler(_DashboardRequestHandler):
        dashboard_state = state

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Dashboard running at http://{host}:{port}")
    print(f"Config file: {env_file}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
        state.close()


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
      <button id=\"btnRefreshNow\" class=\"btn btn-ghost\" type=\"button\">立即刷新</button>
    </div>
  </header>

  <main class=\"layout\">
    <section class=\"panel left-stack\">
      <div class=\"panel-head\">
        <div>
          <div class=\"head-title\">CONFIG ENGINE</div>
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

        <form id=\"configForm\" class=\"form-grid\"></form>

        <div class=\"actions\">
          <button id=\"btnReloadConfig\" class=\"btn btn-ghost\" type=\"button\">重新读取参数</button>
          <button id=\"btnSaveConfig\" class=\"btn btn-primary\" type=\"button\">保存参数</button>
        </div>
      </div>
    </section>

    <section class=\"panel center-stack\">
      <div class=\"panel-head\">
        <div>
          <div class=\"head-title\">MARKET + SIGNAL</div>
          <div class=\"head-desc\">5分钟轮次行情 / 方向信号 / 下注计划</div>
        </div>
        <div id=\"marketHealth\" class=\"chip\">待刷新</div>
      </div>
      <div class=\"panel-body market-grid\">
        <div class=\"market-header\">
          <div>
            <div id=\"marketSlug\" class=\"slug\">--</div>
            <div id=\"marketTitle\" class=\"title\">--</div>
          </div>
          <div class=\"timer-wrap\">
            <div class=\"timer-label\">距离入场</div>
            <div id=\"entryCountdown\" class=\"timer-val\">--:--</div>
          </div>
        </div>

        <div class=\"split\">
          <div class=\"box\">
            <div class=\"box-title\">盘口价格</div>
            <div class=\"kv-grid\">
              <div class=\"kv\"><div class=\"k\">UP 买价</div><div id=\"upPrice\" class=\"v cyan\">--</div></div>
              <div class=\"kv\"><div class=\"k\">DOWN 买价</div><div id=\"downPrice\" class=\"v cyan\">--</div></div>
              <div class=\"kv\"><div class=\"k\">UP 最优挂单</div><div id=\"upAsk\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">DOWN 最优挂单</div><div id=\"downAsk\" class=\"v\">--</div></div>
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
              <div class=\"kv\"><div class=\"k\">open_up</div><div id=\"signalOpenUp\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">current_up</div><div id=\"signalCurrentUp\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">threshold</div><div id=\"signalThreshold\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">delta</div><div id=\"signalDelta\" class=\"v\">--</div></div>
            </div>
            <div class=\"row\">
              <span class=\"label\">已锁边</span>
              <span id=\"signalLocked\" class=\"value\">--</span>
            </div>
          </div>
        </div>

        <div class=\"split\">
          <div class=\"box\">
            <div class=\"box-title\">下注计划 / 风控闸门</div>
            <div class=\"rows\">
              <div class=\"row\"><span class=\"label\">should_trade</span><span id=\"planShouldTrade\" class=\"value\">--</span></div>
              <div class=\"row\"><span class=\"label\">side</span><span id=\"planSide\" class=\"value\">--</span></div>
              <div class=\"row\"><span class=\"label\">price</span><span id=\"planPrice\" class=\"value\">--</span></div>
              <div class=\"row\"><span class=\"label\">order_cost</span><span id=\"planOrderCost\" class=\"value\">--</span></div>
              <div class=\"row\"><span class=\"label\">order_size</span><span id=\"planOrderSize\" class=\"value\">--</span></div>
              <div class=\"row\"><span class=\"label\">expected_profit</span><span id=\"planExpectedProfit\" class=\"value\">--</span></div>
              <div class=\"row\"><span class=\"label\">skip_reason</span><span id=\"planSkipReason\" class=\"value\">--</span></div>
              <div class=\"row\"><span class=\"label\">stop_loss_triggered</span><span id=\"planStopLoss\" class=\"value\">--</span></div>
            </div>
          </div>

          <div class=\"box\">
            <div class=\"box-title\">会话状态</div>
            <div class=\"kv-grid\">
              <div class=\"kv\"><div class=\"k\">round_index</div><div id=\"ssRoundIndex\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">cash_pnl</div><div id=\"ssCashPnl\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">recovery_loss</div><div id=\"ssRecoveryLoss\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">consecutive_losses</div><div id=\"ssConsecutiveLosses\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">stop_loss_count</div><div id=\"ssStopLossCount\" class=\"v\">--</div></div>
              <div class=\"kv\"><div class=\"k\">daily_realized_pnl</div><div id=\"ssDailyPnl\" class=\"v\">--</div></div>
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
            <div class=\"head-title\">WS RUNTIME</div>
            <div class=\"head-desc\">连接质量与订阅状态</div>
          </div>
          <div id=\"wsHealth\" class=\"chip\">待刷新</div>
        </div>
        <div class=\"panel-body\">
          <div id=\"wsRuntimeList\" class=\"runtime-list\"></div>
          <div class=\"footnote\">说明: `source=websocket` 表示直接使用 WS 缓存盘口, `source=http` 表示回退 HTTP。</div>
        </div>
      </div>

      <div class=\"panel\">
        <div class=\"panel-head\">
          <div>
            <div class=\"head-title\">PAPER SUMMARY</div>
            <div class=\"head-desc\">按北京时间聚合的纸面成绩</div>
          </div>
          <div id=\"paperStatus\" class=\"chip\">待刷新</div>
        </div>
        <div class=\"panel-body\">
          <div class=\"kv-grid\" style=\"margin-bottom: 10px;\">
            <div class=\"kv\"><div class=\"k\">日期</div><div id=\"sumDate\" class=\"v\">--</div></div>
            <div class=\"kv\"><div class=\"k\">trade_rows</div><div id=\"sumTrades\" class=\"v\">--</div></div>
            <div class=\"kv\"><div class=\"k\">hit_rate</div><div id=\"sumHitRate\" class=\"v\">--</div></div>
            <div class=\"kv\"><div class=\"k\">total_pnl</div><div id=\"sumTotalPnl\" class=\"v\">--</div></div>
            <div class=\"kv\"><div class=\"k\">max_drawdown</div><div id=\"sumDrawdown\" class=\"v\">--</div></div>
            <div class=\"kv\"><div class=\"k\">strong_signal_rate</div><div id=\"sumStrongRate\" class=\"v\">--</div></div>
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
          <div class=\"head-title\">RECENT PAPER TRADES</div>
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
              <th>skip_reason</th>
              <th>signal_delta</th>
            </tr>
          </thead>
          <tbody id=\"recentTbody\"></tbody>
        </table>
      </div>
    </section>
  </main>
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
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  max-height: 560px;
  overflow: auto;
  padding-right: 4px;
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
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
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

.slug {
  font-family: var(--mono);
  font-size: 17px;
  color: #c2f2ff;
  word-break: break-all;
  font-weight: 700;
}

.title {
  margin-top: 4px;
  color: var(--muted);
  font-size: 12px;
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
  .form-grid { grid-template-columns: 1fr; }
  .market-header { grid-template-columns: 1fr; }
  .timer-wrap { text-align: left; }
  .subtitle { max-width: 56vw; }
}
"""


def _dashboard_js() -> str:
    return """
const state = {
  config: null,
  market: null,
  summary: null,
  recent: null,
};

const POLL_MS = {
  market: 3000,
  summary: 20000,
  recent: 12000,
  clock: 1000,
};

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

function sideText(side) {
  if (side === 'UP') return '看涨';
  if (side === 'DOWN') return '看跌';
  if (side === 'SKIP') return '跳过';
  return '待定';
}

function sideClass(side) {
  if (side === 'UP') return 'trade-up';
  if (side === 'DOWN') return 'trade-down';
  return 'trade-skip';
}

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
    throw new Error(data.error || ('HTTP ' + resp.status));
  }
  return data;
}

async function apiPost(path, payload) {
  const resp = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data.error || ('HTTP ' + resp.status));
  }
  return data;
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

  for (const key of keys) {
    const wrap = document.createElement('div');
    wrap.className = 'field';

    const label = document.createElement('label');
    label.setAttribute('for', 'cfg_' + key);
    const title = labels[key] || key;
    label.textContent = title + ' (' + key + ')';
    wrap.appendChild(label);

    if (Array.isArray(options[key]) && options[key].length > 0) {
      const select = document.createElement('select');
      select.id = 'cfg_' + key;
      for (const opt of options[key]) {
        const option = document.createElement('option');
        option.value = opt;
        option.textContent = opt;
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

    form.appendChild(wrap);
  }

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
  try {
    setChip('cfgStatus', '保存中', 'warn');
    const values = collectConfigValues();
    const data = await apiPost('/api/config', { env_values: values });
    renderConfig(data);
    setChip('cfgStatus', '已保存', 'ok');
  } catch (err) {
    setChip('cfgStatus', '保存失败', 'err');
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
    const shown = (value === null || value === undefined || value === '') ? '--' : String(value);
    return '<div class=\"runtime-item\"><span class=\"rk\">' + esc(key) + '</span><span class=\"rv\">' + esc(shown) + '</span></div>';
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
    el('marketSlug').textContent = '暂无可用轮次';
    el('marketTitle').textContent = payload.message || '当前时段没有可交易轮次';
    el('entryCountdown').textContent = '--:--';
    setChip('marketHealth', '无轮次', 'warn');
  } else {
    el('marketSlug').textContent = round.slug || '--';
    el('marketTitle').textContent = round.title || '--';
    el('entryCountdown').textContent = fmtSeconds(round.seconds_to_entry);
    setChip('marketHealth', round.is_current ? '当前轮次' : '下一轮次', 'ok');
  }

  el('upPrice').textContent = fmtNum(quote.up_price, 4);
  el('downPrice').textContent = fmtNum(quote.down_price, 4);
  el('upAsk').textContent = fmtNum(quote.up_best_ask, 4);
  el('downAsk').textContent = fmtNum(quote.down_best_ask, 4);
  el('quoteSource').textContent = quote.source ? String(quote.source).toUpperCase() : '--';
  el('quoteAccepting').textContent = quote.accepting_orders ? '是' : '否';
  el('quoteFetchedAt').textContent = fmtIso(quote.fetched_at);

  const signalSide = signal.side || 'SKIP';
  const signalNode = el('signalSide');
  signalNode.textContent = sideText(signalSide);
  signalNode.className = 'value ' + sideClass(signalSide);

  el('signalReason').textContent = signal.reason || '--';
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
  el('planSkipReason').textContent = plan.skip_reason || '--';
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
      '<td>' + esc(row.skip_reason || '--') + '</td>' +
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
}

function bindActions() {
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
  bindActions();
  tickClock();
  await refreshAll();
  startPolling();
}

document.addEventListener('DOMContentLoaded', bootstrap);
"""
