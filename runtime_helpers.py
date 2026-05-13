from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import AppConfig
from models import LiveStrategyState, MarketQuote, MarketWindow, SessionState, TradeRecord
from runtime_control import RuntimeControl
from strategy_decision import SideDecision
from trade_log import append_trade_log
from utils import _is_stop_requested, _sleep_if_not_stopped


SESSION_DAY_TZ = timezone(timedelta(hours=8))


def session_day_key(now: datetime) -> str:
    return now.astimezone(SESSION_DAY_TZ).date().isoformat()


def refresh_daily_session_state(state: SessionState, now: datetime) -> bool:
    session_day = session_day_key(now)
    if state.current_day == session_day:
        return False
    state.current_day = session_day
    state.daily_realized_pnl = 0.0
    return True


def entry_time_for_round(cfg: AppConfig, window: MarketWindow) -> datetime:
    if cfg.entry_timing.upper() == "PRE_CLOSE":
        return window.end_time - timedelta(seconds=cfg.preclose_seconds)
    return window.start_time + timedelta(seconds=cfg.open_delay_seconds)


def entry_window_missed(now: datetime, entry_time: datetime, *, grace_seconds: float = 0.0) -> bool:
    return now > (entry_time + timedelta(seconds=max(0.0, grace_seconds)))


def poll_interval_for_target_round(
    *,
    cfg: AppConfig,
    now: datetime,
    target_round: MarketWindow | None,
) -> float:
    base_interval = max(0.0, float(cfg.poll_interval_seconds))
    if target_round is None:
        return base_interval
    entry_time = entry_time_for_round(cfg, target_round)
    if entry_window_missed(now, entry_time, grace_seconds=cfg.entry_grace_seconds):
        return base_interval
    remaining = (entry_time - now).total_seconds()
    near_entry_window = max(0.0, float(cfg.near_entry_poll_window_seconds))
    if remaining <= near_entry_window:
        return min(base_interval, max(0.0, float(cfg.fast_poll_interval_seconds)))
    return base_interval


def poll_interval_for_live_result(*, cfg: AppConfig, result: dict[str, Any]) -> float:
    base_interval = max(0.0, float(cfg.poll_interval_seconds))
    if result.get("status") == "pending_settlement":
        if result.get("skip_reason") == "awaiting_final_price":
            return min(base_interval, max(0.0, float(cfg.final_price_poll_interval_seconds)))
        return min(base_interval, max(0.0, float(cfg.fast_poll_interval_seconds)))
    return base_interval


def select_target_round(
    cfg: AppConfig,
    *,
    now: datetime,
    current_round: MarketWindow | None,
    next_round: MarketWindow | None,
) -> MarketWindow | None:
    if current_round is not None:
        current_entry_time = entry_time_for_round(cfg, current_round)
        if not entry_window_missed(now, current_entry_time, grace_seconds=cfg.entry_grace_seconds):
            return current_round
    return next_round if next_round is not None else current_round


def sleep_until_round_end(
    cfg: AppConfig,
    window: MarketWindow,
    stop_event: threading.Event | None = None,
) -> bool:
    if _is_stop_requested(stop_event):
        return False
    while datetime.now(timezone.utc) < window.end_time:
        if not _sleep_if_not_stopped(stop_event, cfg.poll_interval_seconds):
            return False
    return True


def update_runtime_control(
    runtime_control: RuntimeControl | None,
    **changes,
) -> None:
    if runtime_control is None:
        return
    runtime_control.update_worker_state(**changes)


def fmt_price(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.4f}"


def signal_record_kwargs(side_decision: SideDecision) -> dict[str, Any]:
    return {
        "signal_open_up_price": side_decision.signal_open_up_price,
        "signal_current_up_price": side_decision.signal_current_up_price,
        "signal_threshold": side_decision.signal_threshold,
        "signal_delta": side_decision.signal_delta,
        "signal_locked": side_decision.signal_locked,
        "signal_reason": side_decision.reason,
        "signal_max_entry_price": side_decision.max_entry_price,
    }


def strategy_exception_skip_reason(code: str, exc: Exception) -> str:
    message = " ".join(str(exc).split()) or exc.__class__.__name__
    return f"{code}: {message}"


def append_live_strategy_error_log(
    *,
    log_path: Path,
    cfg: AppConfig,
    strategy_id: int,
    strategy_state: LiveStrategyState,
    target_round: MarketWindow,
    skip_reason: str,
) -> None:
    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=datetime.now(timezone.utc),
            mode="live",
            round_index=strategy_state.round_index,
            strategy=strategy_id,
            entry_timing=cfg.entry_timing,
            event_slug=target_round.slug,
            start_time=target_round.start_time,
            end_time=target_round.end_time,
            side="SKIP",
            price=None,
            order_size=0.0,
            order_cost=0.0,
            expected_profit=0.0,
            result=None,
            trade_pnl=0.0,
            cash_pnl=strategy_state.cash_pnl,
            recovery_loss=strategy_state.recovery_loss,
            consecutive_losses=strategy_state.consecutive_losses,
            skip_reason=skip_reason,
        ),
    )


def describe_side_decision(side_decision: SideDecision) -> str:
    signal_bits = []
    if side_decision.signal_open_up_price is not None:
        signal_bits.append("open_up=" + fmt_price(side_decision.signal_open_up_price))
    if side_decision.signal_current_up_price is not None:
        signal_bits.append("current_up=" + fmt_price(side_decision.signal_current_up_price))
    if side_decision.signal_threshold is not None:
        signal_bits.append("threshold=" + fmt_price(side_decision.signal_threshold))
    if side_decision.signal_delta is not None:
        signal_bits.append("delta=" + fmt_price(side_decision.signal_delta))
    signal_bits.append("locked=" + str(side_decision.signal_locked))
    if side_decision.reason:
        signal_bits.append("reason=" + side_decision.reason)
    return ", ".join(signal_bits)


def describe_quote_source(quote: MarketQuote) -> str:
    source = quote.source or "http"
    return (
        "source=" + source
        + ", up_best_ask=" + fmt_price(quote.up_best_ask)
        + ", up_price=" + fmt_price(quote.up_price)
        + ", down_best_ask=" + fmt_price(quote.down_best_ask)
        + ", down_price=" + fmt_price(quote.down_price)
    )


def describe_ws_runtime(client: Any) -> str:
    get_stats = getattr(client, "get_ws_runtime_stats", None)
    if not callable(get_stats):
        return "ws_stats_unavailable"
    stats = get_stats()
    return (
        "ws_enabled=" + str(stats.get("ws_enabled"))
        + ", ws_available=" + str(stats.get("ws_available"))
        + ", ws_connected=" + str(stats.get("ws_connected"))
        + ", reconnects=" + str(stats.get("ws_reconnect_count"))
        + ", invalid_ops=" + str(stats.get("ws_invalid_operation_count"))
        + ", connect_attempts=" + str(stats.get("ws_connect_attempts"))
        + ", subscribed_assets=" + str(stats.get("ws_subscribed_asset_count"))
        + ", cached_assets=" + str(stats.get("ws_cached_asset_count"))
        + ", last_message_age_s=" + fmt_price(stats.get("ws_last_message_age_seconds"))
        + ", current_error=" + str(stats.get("ws_current_error", stats.get("ws_last_error")))
    )


def runtime_alert_changes_for_live_result(result: dict[str, Any]) -> dict[str, str | None]:
    strategies = result.get("strategies")
    if isinstance(strategies, list):
        for item in strategies:
            if not isinstance(item, dict):
                continue
            if item.get("status") == "blocked" and item.get("error_code") == "trading_restricted":
                return {
                    "runtime_alert_code": "trading_restricted",
                    "runtime_alert_message": str(item.get("error") or "Trading restricted in your region."),
                    "runtime_alert_level": "error",
                }
    if result.get("status") in {"submitted", "skipped", "waiting_for_entry", "pending_settlement", "no_market"}:
        return {
            "runtime_alert_code": None,
            "runtime_alert_message": None,
            "runtime_alert_level": None,
        }
    return {}


def ws_is_stale_for_trade(client: Any, cfg: AppConfig) -> bool:
    get_stats = getattr(client, "get_ws_runtime_stats", None)
    if not callable(get_stats):
        return False
    stats = get_stats()
    if not stats.get("ws_enabled"):
        return False
    if not stats.get("ws_available"):
        return False
    age = stats.get("ws_last_message_age_seconds")
    if not isinstance(age, (int, float)):
        return False
    return age > max(0.0, cfg.ws_trade_guard_stale_seconds)
