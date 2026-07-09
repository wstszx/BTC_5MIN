from __future__ import annotations

import csv
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import AppConfig
from binance_signal import BinanceDepth5SignalService
from live_pending import (
    normalize_pending_live_trades as _normalize_pending_live_trades,
    queue_pending_live_trade as _queue_pending_live_trade,
    sync_legacy_pending_live_from_queue as _sync_legacy_pending_live_from_queue,
)
from models import LiveStrategyState, MarketQuote, MarketWindow, PaperStrategyState, PendingPaperTrade, SessionState, TradePlan, TradeRecord
from optimizer_runtime import (
    candidate_cfg_with_params as _candidate_cfg_with_params,
    load_active_optimizer_challengers as _load_active_optimizer_challengers,
    paper_experiment_id as _paper_experiment_id,
    save_active_optimizer_challengers as _save_active_optimizer_challengers,
)
from paper_pending import (
    build_pending_paper_trade as _build_pending_paper_trade,
    pending_paper_trade_exists as _pending_paper_trade_exists,
    queue_pending_paper_trade as _queue_pending_paper_trade,
)
from polymarket_api import PolymarketClient, extract_token_ids
from risk_and_sizing import build_trade_plan
from risk_skip import (
    apply_post_entry_risk_gate_skip as _apply_post_entry_risk_gate_skip,
    emit_max_stake_skip_alert as _emit_max_stake_skip_alert,
    should_reset_after_risk_gate_skip as _should_reset_after_risk_gate_skip,
    update_max_stake_skip_streak as _update_max_stake_skip_streak,
)
from settlement import (
    append_settled_live_trade_log as _append_settled_live_trade_log,
    append_provisional_live_loss_trade_log as _append_provisional_live_loss_trade_log,
    PROVISIONAL_LOSS_RESULT,
    build_frozen_pending_paper_plan as _build_frozen_pending_paper_plan,
    build_pending_live_trade_plan as _build_pending_live_trade_plan,
    cached_ws_market_result as _cached_ws_market_result,
    clear_pending_live_trade as _clear_pending_live_trade,
    settle_paper_trade as _settle_paper_trade,
    settle_pending_live_trade_if_needed as _settle_pending_live_trade_if_needed,
    settle_pending_live_trades_if_needed as _settle_pending_live_trades_if_needed,
    settle_pending_paper_trade as _settle_pending_paper_trade,
    settle_pending_paper_trades as _settle_pending_paper_trades,
    timeframe_duration_seconds as _timeframe_duration_seconds,
)
from strategy_decision import (
    SideDecision,
    apply_strategy6_signal_to_quote as _apply_strategy6_signal_to_quote,
    compute_signal_threshold as _compute_signal_threshold,
    effective_strategy7_confirm_before_entry_seconds as _effective_strategy7_confirm_before_entry_seconds,
    effective_decision_max_entry_price as _effective_decision_max_entry_price,
    effective_decision_order_cost_multiplier as _effective_decision_order_cost_multiplier,
    is_strategy6_signal_stale as _is_strategy6_signal_stale,
    is_valid_signal_price as _is_valid_signal_price,
    resolve_quote_price,
    resolve_side_from_strategy as _resolve_side_from_strategy,
    resolve_signal_round_open_up_price as _resolve_signal_round_open_up_price,
    resolve_signal_up_price as _resolve_signal_up_price,
    resolve_strategy6_ofi_score as _resolve_strategy6_ofi_score,
)
from trade_log import (
    append_trade_log,
    live_trade_log_upsert_key as _live_trade_log_upsert_key,
    merge_live_trade_log_rows as _merge_live_trade_log_rows,
    row_has_result as _row_has_result,
)
from runtime_control import RuntimeControl
from runtime_config import (
    cfg_for_live_strategy as _cfg_for_live_strategy,
    cfg_for_paper_strategy as _cfg_for_paper_strategy,
    live_strategy_ids_for_runtime as _live_strategy_ids_for_runtime,
    paper_strategy_ids_for_runtime as _paper_strategy_ids_for_runtime,
    validate_live_runtime_config,
)
from utils import _is_stop_requested, _runtime_log, _safe_stop_requested, _sleep_if_not_stopped
from runtime_helpers import (
    append_live_strategy_error_log as _append_live_strategy_error_log,
    describe_quote_source as _describe_quote_source,
    describe_side_decision as _describe_side_decision,
    describe_ws_runtime as _describe_ws_runtime,
    entry_time_for_round as _entry_time_for_round,
    entry_window_missed as _entry_window_missed,
    fmt_price as _fmt_price,
    poll_interval_for_live_result as _poll_interval_for_live_result,
    poll_interval_for_target_round as _poll_interval_for_target_round,
    refresh_daily_session_state as _refresh_daily_session_state,
    runtime_alert_changes_for_live_result as _runtime_alert_changes_for_live_result,
    select_target_round as _select_target_round,
    session_day_key as _session_day_key,
    signal_record_kwargs as _signal_record_kwargs,
    strategy_exception_skip_reason as _strategy_exception_skip_reason,
    update_runtime_control as _update_runtime_control,
    ws_is_stale_for_trade as _ws_is_stale_for_trade,
)
from clob_adapter import (
    OrderExecutionResult,
    POLYMARKET_CRYPTO_TAKER_FEE_RATE,
    build_verified_pending_live_trade_plan as _build_verified_pending_live_trade_plan,
    create_live_clob_client as _create_live_clob_client,
    execute_order_plan as _adapter_execute_order_plan,
    is_live_fok_not_filled_error as _is_live_fok_not_filled_error,
    is_live_trading_restricted_error as _is_live_trading_restricted_error,
    is_retryable_live_clob_error as _is_retryable_live_clob_error,
    is_retryable_live_io_error as _is_retryable_live_io_error,
    live_clob_client_config_key as _live_clob_client_config_key,
    live_execution_price_floor as _live_execution_price_floor,
    price_improvement_floor as _price_improvement_floor,
    read_available_live_balance as _adapter_read_available_live_balance,
    submit_live_strategy_order as _adapter_submit_live_strategy_order,
)
from state_manager import (
    apply_live_strategy_state_to_session_state as _apply_live_strategy_state_to_session_state,
    clone_session_state as _clone_session_state,
    copy_session_state_into as _copy_session_state_into,
    live_strategy_state_from_payload as _live_strategy_state_from_payload,
    load_session_state,
    save_session_state,
)
from strategy_state_sync import (
    ensure_live_strategy_state_map as _ensure_live_strategy_state_map,
    ensure_paper_strategy_state_map as _ensure_paper_strategy_state_map,
    managed_live_strategy_ids as _managed_live_strategy_ids,
    paper_strategy_state_to_session_state as _paper_strategy_state_to_session_state,
    session_state_to_paper_strategy_state as _session_state_to_paper_strategy_state,
    strategy_has_pending_live_trade as _strategy_has_pending_live_trade,
    sync_current_live_strategy_state as _sync_current_live_strategy_state,
    sync_legacy_live_state_fields as _sync_legacy_live_state_fields,
    sync_legacy_paper_state_fields as _sync_legacy_paper_state_fields,
)
def _binance_signal_service_url(cfg: AppConfig) -> str:
    return cfg.binance_ws_url.rstrip("/") + "/" + cfg.binance_depth_stream.lstrip("/")


def _sync_paper_binance_signal_service(
    *,
    cfg: AppConfig,
    strategy_ids: list[int],
    service: BinanceDepth5SignalService | None,
) -> BinanceDepth5SignalService | None:
    needs_service = any(strategy_id in {6, 7, 8, 9, 10, 11, 12, 13} for strategy_id in strategy_ids)
    expected_url = _binance_signal_service_url(cfg)

    if not needs_service:
        if service is not None:
            service.close()
        return None

    if service is not None and getattr(service, "ws_url", None) == expected_url:
        start = getattr(service, "start", None)
        if callable(start):
            start()
        return service

    if service is not None:
        service.close()

    service = BinanceDepth5SignalService(ws_url=cfg.binance_ws_url, stream=cfg.binance_depth_stream)
    service.start()
    return service


def _load_session_state_for_paper_runtime(path: Path, strategy_ids: list[int]) -> SessionState:
    try:
        return load_session_state(path, effective_paper_strategy_ids=strategy_ids)
    except TypeError:
        return load_session_state(path)


def _sync_live_binance_signal_service(
    *,
    cfg: AppConfig,
    strategy_ids: list[int],
    service: BinanceDepth5SignalService | None,
) -> BinanceDepth5SignalService | None:
    return _sync_paper_binance_signal_service(cfg=cfg, strategy_ids=strategy_ids, service=service)


def _load_session_state_for_live_runtime(path: Path, strategy_ids: list[int]) -> SessionState:
    try:
        return load_session_state(path, effective_live_strategy_ids=strategy_ids)
    except TypeError:
        return load_session_state(path)


def _trade_log_float(row: dict[str, str], key: str) -> float:
    try:
        return float(row.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _trade_log_session_day(row: dict[str, str]) -> str:
    for key in ("end_time", "start_time", "timestamp"):
        raw_value = str(row.get(key) or "").strip()
        if not raw_value:
            continue
        try:
            return _session_day_key(datetime.fromisoformat(raw_value))
        except ValueError:
            continue
    return ""


def _strategy_uses_recovery_loss(cfg: AppConfig | None, strategy_id: int) -> bool:
    return False


def _strategy_can_continue_with_pending(strategy_cfg: AppConfig | None) -> bool:
    return True


def _trade_log_explicit_tracks_recovery_loss(row: dict[str, str]) -> bool | None:
    return False


def _trade_log_row_tracks_recovery_loss(row: dict[str, str], cfg: AppConfig | None, strategy_id: int) -> bool:
    return False


def _trade_log_row_tracks_loss_streak(row: dict[str, str], cfg: AppConfig | None, strategy_id: int) -> bool:
    return True


def _sync_live_state_ledger_from_trade_log(
    state: SessionState,
    *,
    live_csv: Path,
    active_strategy_ids: list[int],
    cfg: AppConfig | None = None,
) -> None:
    if not live_csv.exists():
        return
    ledger_states = {
        strategy_id: {
            "cash_pnl": 0.0,
            "daily_realized_pnl": 0.0,
            "recovery_loss": 0.0,
            "consecutive_losses": 0,
            "seen": False,
        }
        for strategy_id in active_strategy_ids
    }
    with live_csv.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                strategy_id = int(str(row.get("strategy") or "").strip())
            except ValueError:
                continue
            ledger_state = ledger_states.get(strategy_id)
            if ledger_state is None:
                continue
            ledger_state["seen"] = True
            if str(row.get("stop_loss_triggered") or "").strip().lower() == "true":
                ledger_state["recovery_loss"] = 0.0
                ledger_state["consecutive_losses"] = 0
                continue
            result = str(row.get("result") or "").strip().upper()
            side = str(row.get("side") or "").strip().upper()
            if result not in {"UP", "DOWN", PROVISIONAL_LOSS_RESULT} or side not in {"UP", "DOWN"}:
                continue
            if str(row.get("skip_reason") or "").strip():
                continue
            order_cost = _trade_log_float(row, "order_cost")
            if order_cost <= 0:
                continue
            expected_profit = _trade_log_float(row, "expected_profit")
            tracks_recovery_loss = _trade_log_row_tracks_recovery_loss(row, cfg, strategy_id)
            tracks_loss_streak = _trade_log_row_tracks_loss_streak(row, cfg, strategy_id)
            if result == side:
                ledger_state["cash_pnl"] += expected_profit
                trade_pnl = expected_profit
                if tracks_recovery_loss:
                    ledger_state["recovery_loss"] = 0.0
                if tracks_loss_streak:
                    ledger_state["consecutive_losses"] = 0
            else:
                existing_trade_pnl = _trade_log_float(row, "trade_pnl")
                trade_pnl = existing_trade_pnl if existing_trade_pnl < 0 else -order_cost
                ledger_state["cash_pnl"] += trade_pnl
                if tracks_recovery_loss:
                    ledger_state["recovery_loss"] += order_cost
                if tracks_loss_streak:
                    ledger_state["consecutive_losses"] += 1
            session_day = _trade_log_session_day(row)
            if session_day:
                if session_day > str(ledger_state.get("current_day") or ""):
                    ledger_state["current_day"] = session_day
                    ledger_state["daily_realized_pnl"] = 0.0
                if session_day == ledger_state.get("current_day"):
                    ledger_state["daily_realized_pnl"] += trade_pnl

    for strategy_id, ledger_state in ledger_states.items():
        if not ledger_state["seen"]:
            continue
        strategy_state = state.live_strategies.get(strategy_id)
        if strategy_state is None:
            continue
        strategy_state.cash_pnl = float(ledger_state["cash_pnl"])
        strategy_state.daily_realized_pnl = float(ledger_state["daily_realized_pnl"])
        if _strategy_uses_recovery_loss(cfg, strategy_id):
            strategy_state.recovery_loss = float(ledger_state["recovery_loss"])
        else:
            strategy_state.recovery_loss = 0.0
        strategy_state.consecutive_losses = int(ledger_state["consecutive_losses"])
    _sync_legacy_live_state_fields(state, active_strategy_ids)


def _side_decision_log_price(side_decision: SideDecision) -> float | None:
    return side_decision.candidate_price


def _max_entry_price_for_decision(cfg: AppConfig, side_decision: SideDecision) -> float | None:
    return _effective_decision_max_entry_price(cfg, side_decision)


def _order_cost_multiplier_for_decision(cfg: AppConfig, side_decision: SideDecision, price: float | None) -> float:
    multiplier = _effective_decision_order_cost_multiplier(cfg=cfg, decision=side_decision, price=price)
    side_decision.order_cost_multiplier = multiplier
    return multiplier


def _entry_delay_seconds(now: datetime, entry_time: datetime) -> float:
    return max(0.0, (now - entry_time).total_seconds())


def _market_min_order_size(market: dict[str, Any]) -> float | None:
    for key in (
        "orderMinSize",
        "minimum_order_size",
        "min_order_size",
        "minimumOrderSize",
        "minOrderSize",
    ):
        try:
            value = float(market.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _effective_min_order_cost(cfg: AppConfig, market: dict[str, Any]) -> float | None:
    configured_min_stake = getattr(cfg, "min_stake", None)
    if configured_min_stake is not None:
        try:
            configured_value = float(configured_min_stake)
        except (TypeError, ValueError):
            configured_value = 0.0
        if configured_value > 0:
            return configured_value
    return None


def _runtime_backoff_seconds(cfg: AppConfig, consecutive_errors: int) -> int:
    scaled = cfg.runtime_error_backoff_base_seconds * (2 ** max(0, consecutive_errors - 1))
    return max(1, min(cfg.runtime_error_backoff_max_seconds, scaled))


def _read_available_live_balance(*, cfg: AppConfig, clob_client: Any | None) -> float:
    live_client = clob_client or _create_live_clob_client(cfg)
    return _adapter_read_available_live_balance(cfg=cfg, clob_client=live_client)


def _load_paper_mirror_state(state_path: Path, strategy_ids: list[int]) -> SessionState:
    state = load_session_state(state_path, effective_paper_strategy_ids=strategy_ids)
    _ensure_paper_strategy_state_map(state, strategy_ids)
    _sync_legacy_paper_state_fields(state, strategy_ids)
    return state


def _mirror_live_decision_to_paper_skip(
    *,
    mirror_state_path: Path | None,
    mirror_log_path: Path | None,
    cfg: AppConfig,
    strategy_id: int,
    window: MarketWindow,
    strategy_state: LiveStrategyState,
    side: str,
    price: float | None,
    side_decision: SideDecision,
    skip_reason: str | None,
) -> None:
    if mirror_state_path is None or mirror_log_path is None:
        return
    mirror_state = _load_paper_mirror_state(mirror_state_path, [strategy_id])
    paper_state = mirror_state.paper_strategies.setdefault(strategy_id, PaperStrategyState())
    if paper_state.last_processed_paper_event_slug == window.slug:
        return
    append_trade_log(
        mirror_log_path,
        TradeRecord(
            timestamp=datetime.now(timezone.utc),
            mode="paper",
            round_index=paper_state.round_index,
            strategy=strategy_id,
            entry_timing=cfg.entry_timing,
            event_slug=window.slug,
            start_time=window.start_time,
            end_time=window.end_time,
            side=side,
            price=price,
            order_size=0.0,
            order_cost=0.0,
            expected_profit=0.0,
            result=None,
            trade_pnl=0.0,
            cash_pnl=paper_state.cash_pnl,
            recovery_loss=paper_state.recovery_loss,
            consecutive_losses=paper_state.consecutive_losses,
            skip_reason=skip_reason or "signal_unavailable",
            **_signal_record_kwargs(side_decision),
        ),
    )
    paper_state.last_processed_paper_event_slug = window.slug
    paper_state.round_index += 1
    mirror_state.paper_strategies[strategy_id] = paper_state
    _sync_legacy_paper_state_fields(mirror_state, [strategy_id])
    save_session_state(mirror_state_path, mirror_state)


def _mirror_live_decision_to_pending_paper_trade(
    *,
    mirror_state_path: Path | None,
    mirror_log_path: Path | None,
    cfg: AppConfig,
    strategy_id: int,
    window: MarketWindow,
    plan: TradePlan,
    side: str,
    side_decision: SideDecision,
) -> None:
    if mirror_state_path is None or mirror_log_path is None:
        return
    mirror_state = _load_paper_mirror_state(mirror_state_path, [strategy_id])
    paper_state = mirror_state.paper_strategies.setdefault(strategy_id, PaperStrategyState())
    if paper_state.last_processed_paper_event_slug == window.slug:
        return
    paper_session = _paper_strategy_state_to_session_state(paper_state, mirror_state)
    strategy_cfg = replace(cfg, strategy_id=strategy_id)
    experiment_id = _paper_experiment_id(strategy_id, paper_state)
    queued = _queue_pending_paper_trade(
        state=paper_session,
        window=window,
        plan=plan,
        side=side,
        cfg=strategy_cfg,
        side_decision=side_decision,
        experiment_id=experiment_id,
    )
    if queued:
        paper_session.last_processed_paper_event_slug = window.slug
        paper_session.round_index += 1
    mirror_state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(paper_session)
    _sync_legacy_paper_state_fields(mirror_state, [strategy_id])
    save_session_state(mirror_state_path, mirror_state)


def _settle_mirrored_paper_trades_if_needed(
    *,
    mirror_state_path: Path | None,
    mirror_log_path: Path | None,
    market_client: PolymarketClient,
    strategy_id: int,
) -> None:
    if mirror_state_path is None or mirror_log_path is None:
        return
    mirror_state = _load_paper_mirror_state(mirror_state_path, [strategy_id])
    paper_state = mirror_state.paper_strategies.setdefault(strategy_id, PaperStrategyState())
    if not paper_state.pending_paper_trades:
        return
    paper_session = _paper_strategy_state_to_session_state(paper_state, mirror_state)
    paper_session, changed = _settle_pending_paper_trades(
        client=market_client,
        state=paper_session,
        log_path=mirror_log_path,
    )
    if not changed:
        return
    mirror_state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(paper_session)
    _sync_legacy_paper_state_fields(mirror_state, [strategy_id])
    save_session_state(mirror_state_path, mirror_state)


def _submit_live_strategy_order(
    *,
    cfg: AppConfig,
    clob_client: Any | None,
    token_id: str,
    plan: TradePlan,
    user_usdc_balance: float | None = None,
) -> tuple[str, Any]:
    return _adapter_submit_live_strategy_order(
        cfg=cfg,
        clob_client=clob_client,
        token_id=token_id,
        plan=plan,
        user_usdc_balance=user_usdc_balance,
        client_factory=_create_live_clob_client,
    )


def _live_market_order_price_cap_for_plan(cfg: AppConfig, plan: TradePlan) -> float | None:
    return plan.max_entry_price if plan.max_entry_price is not None else getattr(cfg, "max_entry_price", None)


def _opposite_binary_side(side: str | None) -> str | None:
    normalized = str(side or "").strip().upper()
    if normalized == "UP":
        return "DOWN"
    if normalized == "DOWN":
        return "UP"
    return None


def _opposite_token_id_for_side(token_ids: dict[str, str], side: str | None) -> str | None:
    opposite_side = _opposite_binary_side(side)
    return token_ids.get(opposite_side) if opposite_side is not None else None


def _valid_binary_quote_price(value: float | None) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if 0 < price < 1:
        return price
    return None


def _best_possible_quote_binary_buy_price(side: str, quote: MarketQuote) -> float | None:
    candidates: list[float] = []
    same_side_price = _valid_binary_quote_price(resolve_quote_price(side, quote))
    if same_side_price is not None:
        candidates.append(same_side_price)
    opposite_side = _opposite_binary_side(side)
    opposite_bid = None
    if opposite_side == "UP":
        opposite_bid = quote.up_best_bid
    elif opposite_side == "DOWN":
        opposite_bid = quote.down_best_bid
    opposite_bid_price = _valid_binary_quote_price(opposite_bid)
    if opposite_bid_price is not None:
        candidates.append(1.0 - opposite_bid_price)
    return min(candidates) if candidates else None


def _live_quote_execution_guard_enabled(cfg: AppConfig) -> bool:
    return str(getattr(cfg, "live_order_type", "FOK") or "FOK").strip().upper() == "FAK"


def _live_quote_execution_skip_reason(
    *,
    cfg: AppConfig,
    plan: TradePlan,
    side: str,
    quote: MarketQuote,
) -> str | None:
    if not _live_quote_execution_guard_enabled(cfg):
        return None
    best_buy_price = _best_possible_quote_binary_buy_price(side, quote)
    decision_price = plan.raw_price if plan.raw_price is not None else plan.price
    min_entry_price = getattr(cfg, "min_entry_price", None)
    execution_floor = _live_execution_price_floor(
        decision_price=decision_price,
        min_entry_price=min_entry_price,
        max_improvement=getattr(cfg, "live_max_price_improvement", None),
    )
    if (
        best_buy_price is None
        or execution_floor is None
        or best_buy_price + 1e-9 >= execution_floor
    ):
        return None
    if min_entry_price is not None and best_buy_price + 1e-9 < min_entry_price:
        return "live_order_book_price_below_min_entry"
    return "live_order_book_price_improved_too_much"


def _execute_order_plan(
    *,
    mode: str,
    cfg: AppConfig,
    clob_client: Any | None,
    strategy_id: int,
    slug: str,
    token_id: str | None,
    plan: TradePlan,
    remaining_budget: float | None,
    opposite_token_id: str | None = None,
    balance_error: str | None = None,
) -> OrderExecutionResult:
    return _adapter_execute_order_plan(
        mode=mode,
        cfg=cfg,
        clob_client=clob_client,
        strategy_id=strategy_id,
        slug=slug,
        token_id=token_id,
        plan=plan,
        remaining_budget=remaining_budget,
        opposite_token_id=opposite_token_id,
        balance_error=balance_error,
        client_factory=_create_live_clob_client,
    )


def _plan_with_verified_live_fill(
    *,
    plan: TradePlan,
    side: str,
    order_id: str | None,
    clob_client: Any | None,
    min_entry_price: float | None = None,
    max_entry_price: float | None = None,
    max_price_improvement: float | None = None,
    strategy_id: int | None = None,
    slug: str | None = None,
) -> tuple[TradePlan, str]:
    if not order_id or clob_client is None:
        return plan, "submitted_plan"
    try:
        verified_plan = _build_verified_pending_live_trade_plan(
            LiveStrategyState(
                pending_live_side=side,
                pending_live_order_id=order_id,
                pending_live_tracks_recovery_loss=plan.tracks_recovery_loss,
            ),
            clob_client=clob_client,
            fee_rate=POLYMARKET_CRYPTO_TAKER_FEE_RATE,
        )
    except Exception as exc:
        _runtime_log(f"live order fill lookup failed order_id={order_id}: {exc}")
        return plan, "submitted_plan"
    if verified_plan is None:
        return plan, "submitted_plan"
    verified_raw_price = verified_plan.raw_price if verified_plan.raw_price is not None else verified_plan.price
    if (
        max_entry_price is not None
        and verified_raw_price is not None
        and verified_raw_price > max_entry_price
    ):
        _runtime_log(
            "official_fill_price_above_max_entry_price"
            + " order_id=" + str(order_id)
            + " strategy=" + str(strategy_id or "")
            + " round=" + str(slug or "")
            + " fill_price=" + f"{verified_raw_price:.6f}"
            + " max_entry_price=" + str(max_entry_price)
        )
    decision_price = plan.raw_price if plan.raw_price is not None else plan.price
    improvement_floor = _price_improvement_floor(decision_price, max_price_improvement)
    if (
        min_entry_price is not None
        and verified_raw_price is not None
        and verified_raw_price + 1e-9 < min_entry_price
    ):
        _runtime_log(
            "official_fill_price_below_min_entry"
            + " order_id=" + str(order_id)
            + " strategy=" + str(strategy_id or "")
            + " round=" + str(slug or "")
            + " fill_price=" + f"{verified_raw_price:.6f}"
            + " min_entry_price=" + str(min_entry_price)
        )
        return verified_plan, "official_fill_price_below_min_entry"
    if (
        improvement_floor is not None
        and verified_raw_price is not None
        and verified_raw_price + 1e-9 < improvement_floor
    ):
        _runtime_log(
            "official_fill_price_below_decision_floor"
            + " order_id=" + str(order_id)
            + " strategy=" + str(strategy_id or "")
            + " round=" + str(slug or "")
            + " fill_price=" + f"{verified_raw_price:.6f}"
            + " decision_price=" + (f"{decision_price:.6f}" if decision_price is not None else "")
            + " floor=" + f"{improvement_floor:.6f}"
        )
        return verified_plan, "official_fill_price_below_decision_floor"
    return verified_plan, "official_fill"


def _sleep_until_round_end(
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


def _build_live_trade_plan_for_decision(
    *,
    cfg: AppConfig,
    state: SessionState | LiveStrategyState,
    market: dict[str, Any],
    side_decision: SideDecision,
    side: str,
    price: float | None,
) -> TradePlan:
    return build_trade_plan(
        state=state,
        side=side,
        price=price,
        min_entry_price=getattr(cfg, "min_entry_price", getattr(cfg, "min_price_threshold", None)),
        max_entry_price=_max_entry_price_for_decision(cfg, side_decision),
        min_price_threshold=getattr(cfg, 'min_price_threshold', None),
        max_price_threshold=cfg.max_price_threshold,
        min_stake=_effective_min_order_cost(cfg, market),
        max_stake=cfg.max_stake,
        max_consecutive_losses=cfg.max_consecutive_losses,
        base_order_cost=cfg.base_order_cost,
        order_cost_multiplier=_order_cost_multiplier_for_decision(cfg, side_decision, price),
        martingale_enabled=cfg.martingale_enabled,
        martingale_multiplier=cfg.martingale_multiplier,
        martingale_max_stake=cfg.martingale_max_stake,
    )


def _execute_live_order_with_fak_retry(
    *,
    cfg: AppConfig,
    market_client: Any,
    market: dict[str, Any],
    live_client: Any | None,
    strategy_id: int,
    strategy_state: SessionState | LiveStrategyState,
    target_round: MarketWindow,
    entry_time: datetime,
    now: datetime,
    quote: MarketQuote,
    side_decision: SideDecision,
    side: str,
    price: float | None,
    token_id: str,
    opposite_token_id: str | None,
    plan: TradePlan,
    remaining_live_budget: float | None,
    balance_error: str | None = None,
    binance_signal_service: BinanceDepth5SignalService | None = None,
) -> tuple[OrderExecutionResult, TradePlan, SideDecision, str, float | None, MarketQuote]:
    current_quote = quote
    current_decision = side_decision
    current_side = side
    current_price = price
    current_plan = plan
    current_token_id = token_id
    current_opposite_token_id = opposite_token_id
    execution = OrderExecutionResult(
        status="skipped",
        remaining_budget=remaining_live_budget,
        skip_reason="not_attempted",
        balance_error=balance_error,
    )
    retry_count = max(0, int(getattr(cfg, "live_fak_no_match_retry_count", 0) or 0))
    retry_delay = max(0.0, float(getattr(cfg, "live_fak_no_match_retry_delay_seconds", 0.0) or 0.0))
    attempt = 0
    low_entry_revalidated = False
    low_entry_revalidation_strategy_ids = {7, 11}
    while True:
        quote_skip_reason = (
            _live_quote_execution_skip_reason(
                cfg=cfg,
                plan=current_plan,
                side=current_side,
                quote=current_quote,
            )
            if remaining_live_budget is not None
            else None
        )
        execution = (
            OrderExecutionResult(
                status="skipped",
                remaining_budget=remaining_live_budget,
                skip_reason=quote_skip_reason,
                balance_error=balance_error,
            )
            if quote_skip_reason is not None
            else _execute_order_plan(
                mode="live",
                cfg=cfg,
                clob_client=live_client,
                strategy_id=strategy_id,
                slug=target_round.slug,
                token_id=current_token_id,
                plan=current_plan,
                remaining_budget=remaining_live_budget,
                opposite_token_id=current_opposite_token_id,
                balance_error=balance_error,
            )
        )
        if (
            strategy_id in low_entry_revalidation_strategy_ids
            and not low_entry_revalidated
            and execution.skip_reason == "live_order_book_price_below_min_entry"
        ):
            low_entry_revalidated = True
            current_quote = market_client.quote_from_market(market)
            refreshed_now = datetime.now(timezone.utc)
            _apply_strategy6_signal_to_quote(
                cfg=cfg,
                quote=current_quote,
                binance_signal_service=binance_signal_service,
                now=refreshed_now,
                diagnostic_log=_runtime_log,
            )
            refreshed_decision = _resolve_side_from_strategy(
                cfg=cfg,
                state=strategy_state,
                slug=target_round.slug,
                quote=current_quote,
                market_client=market_client,
                window=target_round,
                now=refreshed_now,
                entry_time=entry_time,
            )
            if refreshed_decision.side != current_side:
                execution = OrderExecutionResult(
                    status="skipped",
                    remaining_budget=remaining_live_budget,
                    skip_reason=refreshed_decision.reason or f"strategy{strategy_id}_revalidation_signal_changed",
                    balance_error=balance_error,
                )
                return execution, current_plan, refreshed_decision, current_side, current_price, current_quote
            refreshed_price = resolve_quote_price(current_side, current_quote)
            refreshed_plan = _build_live_trade_plan_for_decision(
                cfg=cfg,
                state=strategy_state,
                market=market,
                side_decision=refreshed_decision,
                side=current_side,
                price=refreshed_price,
            )
            if not refreshed_plan.should_trade:
                execution = OrderExecutionResult(
                    status="skipped",
                    remaining_budget=remaining_live_budget,
                    skip_reason=refreshed_plan.skip_reason,
                    balance_error=balance_error,
                )
                return execution, refreshed_plan, refreshed_decision, current_side, refreshed_price, current_quote
            current_decision = refreshed_decision
            current_price = refreshed_price
            current_plan = refreshed_plan
            continue
        if execution.skip_reason != "live_fak_not_matched" or attempt >= retry_count:
            return execution, current_plan, current_decision, current_side, current_price, current_quote
        attempt += 1
        if retry_delay > 0:
            time.sleep(retry_delay)
        current_quote = market_client.quote_from_market(market)
        retry_now = datetime.now(timezone.utc)
        _apply_strategy6_signal_to_quote(
            cfg=cfg,
            quote=current_quote,
            binance_signal_service=binance_signal_service,
            now=retry_now,
            diagnostic_log=_runtime_log,
        )
        refreshed_decision = _resolve_side_from_strategy(
            cfg=cfg,
            state=strategy_state,
            slug=target_round.slug,
            quote=current_quote,
            market_client=market_client,
            window=target_round,
            now=retry_now,
            entry_time=entry_time,
        )
        if refreshed_decision.side != current_side:
            execution = OrderExecutionResult(
                status="skipped",
                remaining_budget=remaining_live_budget,
                skip_reason=refreshed_decision.reason or "live_fak_retry_signal_changed",
                balance_error=balance_error,
            )
            return execution, current_plan, refreshed_decision, current_side, current_price, current_quote
        refreshed_price = resolve_quote_price(current_side, current_quote)
        refreshed_plan = _build_live_trade_plan_for_decision(
            cfg=cfg,
            state=strategy_state,
            market=market,
            side_decision=refreshed_decision,
            side=current_side,
            price=refreshed_price,
        )
        refreshed_quote_skip_reason = _live_quote_execution_skip_reason(
            cfg=cfg,
            plan=refreshed_plan,
            side=current_side,
            quote=current_quote,
        )
        if refreshed_quote_skip_reason is not None:
            execution = OrderExecutionResult(
                status="skipped",
                remaining_budget=remaining_live_budget,
                skip_reason=refreshed_quote_skip_reason,
                balance_error=balance_error,
            )
            return execution, refreshed_plan, refreshed_decision, current_side, refreshed_price, current_quote
        if not refreshed_plan.should_trade:
            execution = OrderExecutionResult(
                status="skipped",
                remaining_budget=remaining_live_budget,
                skip_reason=refreshed_plan.skip_reason,
                balance_error=balance_error,
            )
            return execution, refreshed_plan, refreshed_decision, current_side, refreshed_price, current_quote
        current_decision = refreshed_decision
        current_price = refreshed_price
        current_plan = refreshed_plan


def place_live_order(
    cfg: AppConfig | None = None,
    *,
    market_client: PolymarketClient | None = None,
    binance_signal_service: BinanceDepth5SignalService | None = None,
    clob_client: Any | None = None,
    state_path: Path | None = None,
    log_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg = cfg or AppConfig()
    market_client = market_client or PolymarketClient(cfg)
    state_path = state_path or cfg.logs_dir / "live_session_state.json"
    log_path = log_path or cfg.logs_dir / "live_orders.csv"
    state = load_session_state(state_path)
    persist_state = not dry_run
    live_client = clob_client

    now = datetime.now(timezone.utc)
    daily_state_changed = _refresh_daily_session_state(state, now)
    _normalize_pending_live_trades(state, strategy_id=cfg.strategy_id, entry_timing=cfg.entry_timing)
    if _strategy_has_pending_live_trade(
        LiveStrategyState(pending_live_trades=list(state.pending_live_trades), pending_live_slug=state.pending_live_slug)
    ) and live_client is None and cfg.live_private_key:
        live_client = _create_live_clob_client(cfg)
    strategy_state = _live_strategy_state_from_payload(asdict(state))
    strategy_state, pending_items, settled_previous_trade = _settle_pending_live_trades_if_needed(
        market_client=market_client,
        clob_client=live_client,
        strategy_state=strategy_state,
        strategy_id=cfg.strategy_id,
        now=now,
        funder=cfg.live_funder,
        entry_timing=cfg.entry_timing,
        final_price_wait_seconds=cfg.final_price_wait_seconds,
    )
    _apply_live_strategy_state_to_session_state(state, strategy_state)
    pending_status = next(
        (status for _prior, _updated, status in pending_items if status.get("status") == "pending_settlement"),
        None,
    )
    if pending_status is not None and not _strategy_can_continue_with_pending(cfg):
        if daily_state_changed and persist_state:
            _sync_current_live_strategy_state(state, cfg.strategy_id)
            save_session_state(state_path, state)
        return pending_status
    if settled_previous_trade and persist_state:
        for prior_item_state, updated_item_state, item_status in pending_items:
            if item_status.get("status") == "provisional_loss":
                _append_provisional_live_loss_trade_log(
                    log_path=log_path,
                    cfg=cfg,
                    strategy_id=cfg.strategy_id,
                    prior_state=prior_item_state,
                    updated_state=updated_item_state,
                    settlement_status=item_status,
                )
            elif item_status.get("status") == "settled":
                _append_settled_live_trade_log(
                    log_path=log_path,
                    cfg=cfg,
                    strategy_id=cfg.strategy_id,
                    prior_state=prior_item_state,
                    updated_state=updated_item_state,
                    settlement_status=item_status,
                )
        _sync_current_live_strategy_state(state, cfg.strategy_id)
        save_session_state(state_path, state)

    current_round, next_round = market_client.find_current_and_next_rounds(now=now)
    target_round = _select_target_round(cfg, now=now, current_round=current_round, next_round=next_round)
    if target_round is None:
        if daily_state_changed and persist_state:
            _sync_current_live_strategy_state(state, cfg.strategy_id)
            save_session_state(state_path, state)
        return {"status": "no_market"}

    entry_time = _entry_time_for_round(cfg, target_round)
    market = market_client.get_market_by_slug(target_round.slug)
    quote = market_client.quote_from_market(market)
    _apply_strategy6_signal_to_quote(
        cfg=cfg,
        quote=quote,
        binance_signal_service=binance_signal_service,
        now=now,
        diagnostic_log=_runtime_log,
    )
    print('[live] quote {' + _describe_quote_source(quote) + '}', flush=True)
    print('[live] ws_runtime {' + _describe_ws_runtime(market_client) + '}', flush=True)
    side_decision = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug=target_round.slug,
        quote=quote,
        market_client=market_client,
        window=target_round,
        now=now,
        entry_time=entry_time,
    )
    if side_decision.side is None:
        if dry_run:
            return {
                "status": "dry_run",
                "slug": target_round.slug,
                "side": None,
                "token_id": None,
                "price": None,
                "should_trade": False,
                "skip_reason": side_decision.reason or "signal_unavailable",
                "entry_time": entry_time.isoformat(),
                "signal_open_up_price": side_decision.signal_open_up_price,
                "signal_current_up_price": side_decision.signal_current_up_price,
                "signal_threshold": side_decision.signal_threshold,
                "signal_delta": side_decision.signal_delta,
                "signal_locked": side_decision.signal_locked,
            }
        if (entry_time - now).total_seconds() > 1:
            if persist_state:
                save_session_state(state_path, state)
            return {
                "status": "waiting_for_entry",
                "slug": target_round.slug,
                "side": None,
                "token_id": None,
                "price": None,
                "should_trade": False,
                "skip_reason": side_decision.reason or "signal_unavailable",
                "entry_time": entry_time.isoformat(),
                "signal_open_up_price": side_decision.signal_open_up_price,
                "signal_current_up_price": side_decision.signal_current_up_price,
                "signal_threshold": side_decision.signal_threshold,
                "signal_delta": side_decision.signal_delta,
                "signal_locked": side_decision.signal_locked,
            }
        if persist_state:
            append_trade_log(
                log_path,
                TradeRecord(
                    timestamp=datetime.now(timezone.utc),
                    mode="live",
                    round_index=state.round_index,
                    strategy=cfg.strategy_id,
                    entry_timing=cfg.entry_timing,
                    event_slug=target_round.slug,
                    start_time=target_round.start_time,
                    end_time=target_round.end_time,
                    side="SKIP",
                    price=_side_decision_log_price(side_decision),
                    order_size=0.0,
                    order_cost=0.0,
                    expected_profit=0.0,
                    result=None,
                    trade_pnl=0.0,
                    cash_pnl=state.cash_pnl,
                    recovery_loss=state.recovery_loss,
                    consecutive_losses=state.consecutive_losses,
                    skip_reason=side_decision.reason or "signal_unavailable",
                    **_signal_record_kwargs(side_decision),
                ),
            )
        if persist_state:
            save_session_state(state_path, state)
        return {
            "status": "skipped",
            "slug": target_round.slug,
            "side": None,
            "token_id": None,
            "price": None,
            "should_trade": False,
            "skip_reason": side_decision.reason or "signal_unavailable",
            "entry_time": entry_time.isoformat(),
            "signal_open_up_price": side_decision.signal_open_up_price,
            "signal_current_up_price": side_decision.signal_current_up_price,
            "signal_threshold": side_decision.signal_threshold,
            "signal_delta": side_decision.signal_delta,
            "signal_locked": side_decision.signal_locked,
        }
    side = side_decision.side
    price = resolve_quote_price(side, quote)

    if _ws_is_stale_for_trade(market_client, cfg):
        skip_reason = "ws_stale"
        if dry_run:
            return {
                "status": "dry_run",
                "slug": target_round.slug,
                "side": side,
                "token_id": None,
                "price": price,
                "should_trade": False,
                "skip_reason": skip_reason,
                "entry_time": entry_time.isoformat(),
                "signal_open_up_price": side_decision.signal_open_up_price,
                "signal_current_up_price": side_decision.signal_current_up_price,
                "signal_threshold": side_decision.signal_threshold,
                "signal_delta": side_decision.signal_delta,
                "signal_locked": side_decision.signal_locked,
            }
        if (entry_time - now).total_seconds() > 1:
            if persist_state:
                save_session_state(state_path, state)
            return {
                "status": "waiting_for_entry",
                "slug": target_round.slug,
                "side": side,
                "token_id": None,
                "price": price,
                "should_trade": False,
                "skip_reason": skip_reason,
                "entry_time": entry_time.isoformat(),
                "signal_open_up_price": side_decision.signal_open_up_price,
                "signal_current_up_price": side_decision.signal_current_up_price,
                "signal_threshold": side_decision.signal_threshold,
                "signal_delta": side_decision.signal_delta,
                "signal_locked": side_decision.signal_locked,
            }
        if persist_state:
            append_trade_log(
                log_path,
                TradeRecord(
                    timestamp=datetime.now(timezone.utc),
                    mode="live",
                    round_index=state.round_index,
                    strategy=cfg.strategy_id,
                    entry_timing=cfg.entry_timing,
                    event_slug=target_round.slug,
                    start_time=target_round.start_time,
                    end_time=target_round.end_time,
                    side=side,
                    price=price,
                    order_size=0.0,
                    order_cost=0.0,
                    expected_profit=0.0,
                    result=None,
                    trade_pnl=0.0,
                    cash_pnl=state.cash_pnl,
                    recovery_loss=state.recovery_loss,
                    consecutive_losses=state.consecutive_losses,
                    skip_reason=skip_reason,
                    **_signal_record_kwargs(side_decision),
                ),
            )
        if persist_state:
            save_session_state(state_path, state)
        return {
            "status": "skipped",
            "slug": target_round.slug,
            "side": side,
            "token_id": None,
            "price": price,
            "should_trade": False,
            "skip_reason": skip_reason,
            "entry_time": entry_time.isoformat(),
            "signal_open_up_price": side_decision.signal_open_up_price,
            "signal_current_up_price": side_decision.signal_current_up_price,
            "signal_threshold": side_decision.signal_threshold,
            "signal_delta": side_decision.signal_delta,
            "signal_locked": side_decision.signal_locked,
        }
    if _entry_window_missed(now, entry_time, grace_seconds=cfg.entry_grace_seconds):
        skip_reason = "entry_window_missed"
        entry_delay_seconds = _entry_delay_seconds(now, entry_time)
        if dry_run:
            return {
                "status": "dry_run",
                "slug": target_round.slug,
                "side": side,
                "token_id": None,
                "price": price,
                "should_trade": False,
                "skip_reason": skip_reason,
                "entry_time": entry_time.isoformat(),
                "entry_delay_seconds": entry_delay_seconds,
                "projected_max_stake_skip_streak": 0,
                "signal_open_up_price": side_decision.signal_open_up_price,
                "signal_current_up_price": side_decision.signal_current_up_price,
                "signal_threshold": side_decision.signal_threshold,
                "signal_delta": side_decision.signal_delta,
                "signal_locked": side_decision.signal_locked,
            }
        append_trade_log(
            log_path,
            TradeRecord(
                timestamp=datetime.now(timezone.utc),
                mode="live",
                round_index=state.round_index,
                strategy=cfg.strategy_id,
                entry_timing=cfg.entry_timing,
                event_slug=target_round.slug,
                start_time=target_round.start_time,
                end_time=target_round.end_time,
                side=side,
                price=price,
                order_size=0.0,
                order_cost=0.0,
                expected_profit=0.0,
                result=None,
                trade_pnl=0.0,
                cash_pnl=state.cash_pnl,
                recovery_loss=state.recovery_loss,
                consecutive_losses=state.consecutive_losses,
                skip_reason=skip_reason,
                entry_time=entry_time,
                entry_delay_seconds=entry_delay_seconds,
                **_signal_record_kwargs(side_decision),
            ),
        )
        state.round_index += 1
        if persist_state:
            save_session_state(state_path, state)
        return {
            "status": "skipped",
            "slug": target_round.slug,
            "side": side,
            "token_id": None,
            "price": price,
            "should_trade": False,
            "skip_reason": skip_reason,
            "entry_time": entry_time.isoformat(),
            "entry_delay_seconds": entry_delay_seconds,
            "projected_max_stake_skip_streak": 0,
            "signal_open_up_price": side_decision.signal_open_up_price,
            "signal_current_up_price": side_decision.signal_current_up_price,
            "signal_threshold": side_decision.signal_threshold,
            "signal_delta": side_decision.signal_delta,
            "signal_locked": side_decision.signal_locked,
        }
    plan = _build_live_trade_plan_for_decision(
        cfg=cfg,
        state=state,
        market=market,
        side_decision=side_decision,
        side=side,
        price=price,
    )
    token_ids = extract_token_ids(market.get("clobTokenIds"), market.get("outcomes"))
    token_id = token_ids.get(side)
    opposite_token_id = _opposite_token_id_for_side(token_ids, side)

    if dry_run:
        projected_streak = (
            state.consecutive_max_stake_skips + 1
            if plan.skip_reason == "order_cost_above_max_stake"
            else 0
        )
        return {
            "status": "dry_run",
            "slug": target_round.slug,
            "side": side,
            "token_id": token_id,
            "price": price,
            "should_trade": plan.should_trade,
            "skip_reason": plan.skip_reason,
            "order_size": plan.order_size,
            "order_cost": plan.order_cost,
            "expected_profit": plan.expected_profit,
            "order_type": cfg.live_order_type.upper(),
            "projected_max_stake_skip_streak": projected_streak,
            "signal_open_up_price": side_decision.signal_open_up_price,
            "signal_current_up_price": side_decision.signal_current_up_price,
            "signal_threshold": side_decision.signal_threshold,
            "signal_delta": side_decision.signal_delta,
            "signal_locked": side_decision.signal_locked,
        }

    if not cfg.live_trading_enabled:
        raise RuntimeError("Live trading is disabled. Set LIVE_TRADING_ENABLED=true (or config flag) to submit orders.")
    if not plan.should_trade:
        skip_stop_loss_triggered = _should_reset_after_risk_gate_skip(
            state,
            skip_reason=plan.skip_reason,
            cfg=cfg,
            stop_loss_triggered=plan.stop_loss_triggered,
        )
        if (entry_time - now).total_seconds() > 1:
            if persist_state:
                save_session_state(state_path, state)
            return {
                "status": "waiting_for_entry",
                "slug": target_round.slug,
                "side": side,
                "token_id": token_id,
                "price": price,
                "should_trade": False,
                "skip_reason": plan.skip_reason,
                "order_size": plan.order_size,
                "order_cost": plan.order_cost,
                "expected_profit": plan.expected_profit,
                "entry_time": entry_time.isoformat(),
                "signal_open_up_price": side_decision.signal_open_up_price,
                "signal_current_up_price": side_decision.signal_current_up_price,
                "signal_threshold": side_decision.signal_threshold,
                "signal_delta": side_decision.signal_delta,
                "signal_locked": side_decision.signal_locked,
            }
        append_trade_log(
            log_path,
            TradeRecord(
                timestamp=datetime.now(timezone.utc),
                mode="live",
                round_index=state.round_index,
                strategy=cfg.strategy_id,
                entry_timing=cfg.entry_timing,
                event_slug=target_round.slug,
                start_time=target_round.start_time,
                end_time=target_round.end_time,
                side=side,
                price=price,
                order_size=0.0,
                order_cost=0.0,
                expected_profit=0.0,
                result=None,
                trade_pnl=0.0,
                cash_pnl=state.cash_pnl,
                recovery_loss=state.recovery_loss,
                consecutive_losses=state.consecutive_losses,
                skip_reason=plan.skip_reason,
                stop_loss_triggered=skip_stop_loss_triggered,
                **_signal_record_kwargs(side_decision),
            ),
        )
        state, should_alert, skip_stop_loss_triggered, skip_streak = _apply_post_entry_risk_gate_skip(
            state,
            skip_reason=plan.skip_reason,
            cfg=cfg,
            stop_loss_triggered=plan.stop_loss_triggered,
        )
        if should_alert:
            _emit_max_stake_skip_alert(
                slug=target_round.slug,
                side=side,
                price=price,
                state=state,
                cfg=cfg,
                skip_streak=skip_streak,
            )
        if persist_state:
            save_session_state(state_path, state)
        return {
            "status": "skipped",
            "slug": target_round.slug,
            "side": side,
            "price": price,
            "skip_reason": plan.skip_reason,
            "max_stake_skip_streak": state.consecutive_max_stake_skips,
            "stop_loss_triggered": skip_stop_loss_triggered,
            "signal_open_up_price": side_decision.signal_open_up_price,
            "signal_current_up_price": side_decision.signal_current_up_price,
            "signal_threshold": side_decision.signal_threshold,
            "signal_delta": side_decision.signal_delta,
            "signal_locked": side_decision.signal_locked,
        }
    state.consecutive_max_stake_skips = 0
    if token_id is None:
        raise RuntimeError(f"Missing token id for side={side} on market={target_round.slug}")
    if (entry_time - now).total_seconds() > 1:
        if persist_state:
            save_session_state(state_path, state)
        return {
            "status": "waiting_for_entry",
            "slug": target_round.slug,
            "side": side,
            "token_id": token_id,
            "price": price,
            "should_trade": True,
            "skip_reason": None,
            "entry_time": entry_time.isoformat(),
            "order_size": plan.order_size,
            "order_cost": plan.order_cost,
            "expected_profit": plan.expected_profit,
            "order_type": cfg.live_order_type.upper(),
            "signal_open_up_price": side_decision.signal_open_up_price,
            "signal_current_up_price": side_decision.signal_current_up_price,
            "signal_threshold": side_decision.signal_threshold,
            "signal_delta": side_decision.signal_delta,
            "signal_locked": side_decision.signal_locked,
        }

    remaining_live_budget = _read_available_live_balance(cfg=cfg, clob_client=live_client)
    execution, plan, side_decision, side, price, quote = _execute_live_order_with_fak_retry(
        cfg=cfg,
        market_client=market_client,
        market=market,
        live_client=live_client,
        strategy_id=cfg.strategy_id,
        strategy_state=state,
        target_round=target_round,
        entry_time=entry_time,
        now=now,
        quote=quote,
        side_decision=side_decision,
        side=side,
        price=price,
        token_id=token_id,
        opposite_token_id=opposite_token_id,
        plan=plan,
        remaining_live_budget=remaining_live_budget,
    )
    if execution.status == "skipped":
        append_trade_log(
            log_path,
            TradeRecord(
                timestamp=datetime.now(timezone.utc),
                mode="live",
                round_index=state.round_index,
                strategy=cfg.strategy_id,
                entry_timing=cfg.entry_timing,
                event_slug=target_round.slug,
                start_time=target_round.start_time,
                end_time=target_round.end_time,
                side=side,
                price=price,
                order_size=0.0,
                order_cost=0.0,
                expected_profit=0.0,
                result=None,
                trade_pnl=0.0,
                cash_pnl=state.cash_pnl,
                recovery_loss=state.recovery_loss,
                consecutive_losses=state.consecutive_losses,
                skip_reason=execution.skip_reason,
                balance_error=execution.balance_error,
                live_order_book_price=execution.live_order_book_price,
                live_price_cap=execution.live_price_cap,
                **_signal_record_kwargs(side_decision),
            ),
        )
        if persist_state:
            _sync_current_live_strategy_state(state, cfg.strategy_id)
            save_session_state(state_path, state)
        return {
            "status": "skipped",
            "slug": target_round.slug,
            "side": side,
            "token_id": token_id,
            "price": price,
            "should_trade": False,
            "skip_reason": execution.skip_reason,
            "order_size": plan.order_size,
            "order_cost": plan.order_cost,
            "expected_profit": plan.expected_profit,
            "order_type": cfg.live_order_type.upper(),
            "balance_error": execution.balance_error,
            "live_order_book_price": execution.live_order_book_price,
            "live_price_cap": execution.live_price_cap,
            "signal_open_up_price": side_decision.signal_open_up_price,
            "signal_current_up_price": side_decision.signal_current_up_price,
            "signal_threshold": side_decision.signal_threshold,
            "signal_delta": side_decision.signal_delta,
                "signal_locked": side_decision.signal_locked,
            }

    live_price_cap = _live_market_order_price_cap_for_plan(cfg, plan)
    executed_plan, fill_source = _plan_with_verified_live_fill(
        plan=plan,
        side=side,
        order_id=execution.order_id,
        clob_client=live_client,
        min_entry_price=getattr(cfg, "min_entry_price", None),
        max_entry_price=plan.max_entry_price if plan.max_entry_price is not None else getattr(cfg, "max_entry_price", None),
        max_price_improvement=getattr(cfg, "live_max_price_improvement", None),
        strategy_id=cfg.strategy_id,
        slug=target_round.slug,
    )
    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=datetime.now(timezone.utc),
            mode="live",
            round_index=state.round_index,
            strategy=cfg.strategy_id,
            entry_timing=cfg.entry_timing,
            event_slug=target_round.slug,
            start_time=target_round.start_time,
            end_time=target_round.end_time,
            side=side,
            price=executed_plan.price,
            order_size=executed_plan.order_size,
            order_cost=executed_plan.order_cost,
            expected_profit=executed_plan.expected_profit,
            result=None,
            trade_pnl=0.0,
            cash_pnl=state.cash_pnl,
            recovery_loss=state.recovery_loss,
            consecutive_losses=state.consecutive_losses,
            order_id=execution.order_id,
            fill_source=fill_source,
            raw_price=executed_plan.raw_price,
            raw_order_cost=executed_plan.raw_order_cost,
            fee=executed_plan.fee,
            live_order_book_price=execution.live_order_book_price,
            live_price_cap=live_price_cap,
            raw_price_cap_sent=execution.raw_price_cap_sent,
            tracks_recovery_loss=executed_plan.tracks_recovery_loss,
            **_signal_record_kwargs(side_decision),
        ),
    )
    _queue_pending_live_trade(
        state,
        window=target_round,
        plan=executed_plan,
        side=side,
        strategy_id=cfg.strategy_id,
        entry_timing=cfg.entry_timing,
        order_id=execution.order_id,
    )
    state.round_index += 1
    if persist_state:
        _sync_current_live_strategy_state(state, cfg.strategy_id)
        save_session_state(state_path, state)

    return {
        "status": "submitted",
        "slug": target_round.slug,
        "side": side,
        "token_id": token_id,
        "price": executed_plan.price,
        "raw_price": executed_plan.raw_price,
        "order_size": executed_plan.order_size,
        "raw_order_cost": executed_plan.raw_order_cost,
        "fee": executed_plan.fee,
        "effective_price_with_fee": executed_plan.price,
        "effective_order_cost_with_fee": executed_plan.order_cost,
        "live_order_book_price": execution.live_order_book_price,
        "live_price_cap": live_price_cap,
        "order_cost": executed_plan.order_cost,
        "expected_profit": executed_plan.expected_profit,
        "order_type": cfg.live_order_type.upper(),
        "order_id": execution.order_id,
        "response": execution.response,
        "signal_open_up_price": side_decision.signal_open_up_price,
        "signal_current_up_price": side_decision.signal_current_up_price,
        "signal_threshold": side_decision.signal_threshold,
        "signal_delta": side_decision.signal_delta,
        "signal_locked": side_decision.signal_locked,
    }


def run_live_trading(
    cfg: AppConfig | None = None,
    *,
    market_client: PolymarketClient | None = None,
    clob_client: Any | None = None,
    state_path: Path | None = None,
    log_path: Path | None = None,
    mirror_paper_state_path: Path | None = None,
    mirror_paper_log_path: Path | None = None,
    stop_event: threading.Event | None = None,
    config_provider: Callable[[], AppConfig] | None = None,
    runtime_control: RuntimeControl | None = None,
    stop_when_safe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    cfg = cfg or AppConfig()
    validate_live_runtime_config(cfg)
    configured_strategy_ids = _live_strategy_ids_for_runtime(cfg)
    client_provided = market_client is not None
    state_path_provided = state_path is not None
    log_path_provided = log_path is not None
    mirror_paper_state_path_provided = mirror_paper_state_path is not None
    mirror_paper_log_path_provided = mirror_paper_log_path is not None
    market_client = market_client or PolymarketClient(cfg)
    binance_signal_service = _sync_live_binance_signal_service(
        cfg=cfg,
        strategy_ids=configured_strategy_ids,
        service=None,
    )
    state_path = state_path or cfg.logs_dir / 'live_session_state.json'
    log_path = log_path or cfg.logs_dir / 'live_orders.csv'
    if cfg.trade_mode == "both":
        mirror_paper_state_path = mirror_paper_state_path or cfg.logs_dir / "paper" / cfg.market_timeframe / "session_state.json"
        mirror_paper_log_path = mirror_paper_log_path or cfg.logs_dir / "paper" / cfg.market_timeframe / "paper_trades.csv"
    cached_live_client = clob_client
    cached_live_client_key = _live_clob_client_config_key(cfg) if clob_client is None else None

    def _discard_cached_live_client() -> None:
        nonlocal cached_live_client, cached_live_client_key
        if clob_client is not None or cached_live_client is None:
            return
        close_client = getattr(cached_live_client, "close", None)
        if callable(close_client):
            try:
                close_client()
            except Exception:
                pass
        cached_live_client = None
        cached_live_client_key = None

    initial_state = _load_session_state_for_live_runtime(state_path, configured_strategy_ids)
    _ensure_live_strategy_state_map(initial_state, configured_strategy_ids)
    managed_strategy_ids = _managed_live_strategy_ids(configured_strategy_ids, initial_state)
    initial_pending_strategy = next(
        (
            strategy_id
            for strategy_id in managed_strategy_ids
            if _strategy_has_pending_live_trade(initial_state.live_strategies.get(strategy_id))
        ),
        None,
    )
    _update_runtime_control(
        runtime_control,
        current_round_slug=(
            initial_state.live_strategies[initial_pending_strategy].pending_live_slug
            if initial_pending_strategy is not None
            else None
        ),
        round_in_progress=initial_pending_strategy is not None,
        safe_to_switch=initial_pending_strategy is None,
        pending_live_order=initial_pending_strategy is not None,
    )

    try:
        consecutive_errors = 0
        while True:
            if _is_stop_requested(stop_event):
                return {'status': 'stopped'}
            if _safe_stop_requested(stop_when_safe) and runtime_control is not None:
                snapshot = runtime_control.snapshot()
                if snapshot.safe_to_switch and not snapshot.round_in_progress and not snapshot.pending_live_order:
                    return {'status': 'stopped'}
            if config_provider is not None:
                candidate_cfg = config_provider()
                if candidate_cfg is not None:
                    if str(getattr(candidate_cfg, "trade_mode", "") or "").strip().lower() == "live" and not bool(
                        getattr(candidate_cfg, "live_trading_enabled", False)
                    ):
                        _update_runtime_control(
                            runtime_control,
                            current_round_slug=None,
                            round_in_progress=False,
                            safe_to_switch=True,
                            pending_live_order=False,
                        )
                        return {"status": "stopped", "skip_reason": "live_trading_disabled"}
                    validate_live_runtime_config(candidate_cfg)
                    cfg = candidate_cfg
                    configured_strategy_ids = _live_strategy_ids_for_runtime(cfg)
                    binance_signal_service = _sync_live_binance_signal_service(
                        cfg=cfg,
                        strategy_ids=configured_strategy_ids,
                        service=binance_signal_service,
                    )
                    if not client_provided:
                        market_client.config = cfg
                    if not state_path_provided:
                        state_path = cfg.logs_dir / 'live_session_state.json'
                    if not log_path_provided:
                        log_path = cfg.logs_dir / 'live_orders.csv'
                    if not mirror_paper_state_path_provided and cfg.trade_mode == "both":
                        mirror_paper_state_path = cfg.logs_dir / "paper" / cfg.market_timeframe / "session_state.json"
                    if not mirror_paper_log_path_provided and cfg.trade_mode == "both":
                        mirror_paper_log_path = cfg.logs_dir / "paper" / cfg.market_timeframe / "paper_trades.csv"
            now = datetime.now(timezone.utc)
            try:
                if clob_client is not None:
                    live_client = clob_client
                else:
                    candidate_live_client_key = _live_clob_client_config_key(cfg)
                    if cached_live_client is None or cached_live_client_key != candidate_live_client_key:
                        cached_live_client = _create_live_clob_client(cfg)
                        cached_live_client_key = candidate_live_client_key
                    live_client = cached_live_client
                consecutive_errors = 0
            except Exception as exc:
                if not _is_retryable_live_clob_error(exc):
                    raise
                consecutive_errors += 1
                backoff = _runtime_backoff_seconds(cfg, consecutive_errors)
                _runtime_log(
                    "live CLOB client transient error #"
                    + str(consecutive_errors)
                    + ": "
                    + str(exc)
                    + " | backoff="
                    + str(backoff)
                    + "s"
                )
                _update_runtime_control(
                    runtime_control,
                    current_round_slug=None,
                    round_in_progress=False,
                    safe_to_switch=True,
                    pending_live_order=False,
                )
                if not _sleep_if_not_stopped(stop_event, backoff):
                    return {"status": "stopped"}
                continue
            state = _load_session_state_for_live_runtime(state_path, configured_strategy_ids)
            _ensure_live_strategy_state_map(state, configured_strategy_ids)
            managed_strategy_ids = _managed_live_strategy_ids(configured_strategy_ids, state)
            _sync_live_state_ledger_from_trade_log(
                state,
                live_csv=log_path,
                active_strategy_ids=managed_strategy_ids,
                cfg=cfg,
            )
            strategy_results: list[dict[str, Any]] = []
            pending_strategy_ids: list[int] = []
            handled_strategy_ids: set[int] = set()
            for strategy_id in managed_strategy_ids:
                strategy_state = state.live_strategies.setdefault(strategy_id, LiveStrategyState())
                strategy_cfg = _cfg_for_live_strategy(cfg, strategy_id)
                can_continue_with_pending = _strategy_can_continue_with_pending(strategy_cfg)
                try:
                    _settle_mirrored_paper_trades_if_needed(
                        mirror_state_path=mirror_paper_state_path,
                        mirror_log_path=mirror_paper_log_path,
                        market_client=market_client,
                        strategy_id=strategy_id,
                    )
                    _refresh_daily_session_state(strategy_state, now)
                    strategy_state, pending_items, _ = _settle_pending_live_trades_if_needed(
                        market_client=market_client,
                        clob_client=live_client,
                        strategy_state=strategy_state,
                        strategy_id=strategy_id,
                        now=now,
                        funder=cfg.live_funder,
                        entry_timing=strategy_cfg.entry_timing,
                        final_price_wait_seconds=cfg.final_price_wait_seconds,
                    )
                    for prior_item_state, updated_item_state, pending_status in pending_items:
                        if pending_status.get("status") == "settled":
                            _append_settled_live_trade_log(
                                log_path=log_path,
                                cfg=strategy_cfg,
                                strategy_id=strategy_id,
                                prior_state=prior_item_state,
                                updated_state=updated_item_state,
                                settlement_status=pending_status,
                            )
                            strategy_results.append({"strategy_id": strategy_id, **pending_status})
                        elif pending_status.get("status") == "provisional_loss":
                            _append_provisional_live_loss_trade_log(
                                log_path=log_path,
                                cfg=strategy_cfg,
                                strategy_id=strategy_id,
                                prior_state=prior_item_state,
                                updated_state=updated_item_state,
                                settlement_status=pending_status,
                            )
                            strategy_results.append({"strategy_id": strategy_id, **pending_status})
                        elif pending_status.get("status") == "pending_settlement":
                            if not can_continue_with_pending:
                                pending_strategy_ids.append(strategy_id)
                            handled_strategy_ids.add(strategy_id)
                            strategy_results.append({"strategy_id": strategy_id, **pending_status})
                    state.live_strategies[strategy_id] = strategy_state
                except Exception as exc:
                    state.live_strategies[strategy_id] = strategy_state
                    handled_strategy_ids.add(strategy_id)
                    if _is_retryable_live_clob_error(exc):
                        _discard_cached_live_client()
                        if not can_continue_with_pending:
                            pending_strategy_ids.append(strategy_id)
                        _sync_legacy_pending_live_from_queue(strategy_state)
                        _runtime_log(
                            f"live strategy {strategy_id} settlement retryable CLOB error: {exc}"
                        )
                        strategy_results.append(
                            {
                                "strategy_id": strategy_id,
                                "status": "pending_settlement",
                                "phase": "settlement",
                                "slug": strategy_state.pending_live_slug,
                                "side": strategy_state.pending_live_side,
                                "skip_reason": "settlement_retryable_clob_error",
                                "pending_end_time": strategy_state.pending_live_end_time,
                                "order_id": strategy_state.pending_live_order_id,
                                "error": str(exc),
                            }
                        )
                        continue
                    _runtime_log(
                        f"live strategy {strategy_id} settlement error: {exc}"
                    )
                    if _strategy_has_pending_live_trade(strategy_state):
                        if not can_continue_with_pending:
                            pending_strategy_ids.append(strategy_id)
                        _sync_legacy_pending_live_from_queue(strategy_state)
                        strategy_results.append(
                            {
                                "strategy_id": strategy_id,
                                "status": "pending_settlement",
                                "phase": "settlement",
                                "slug": strategy_state.pending_live_slug,
                                "side": strategy_state.pending_live_side,
                                "skip_reason": "settlement_error",
                                "pending_end_time": strategy_state.pending_live_end_time,
                                "order_id": strategy_state.pending_live_order_id,
                                "error": str(exc),
                            }
                        )
                    else:
                        strategy_results.append(
                            {
                                "strategy_id": strategy_id,
                                "status": "error",
                                "phase": "settlement",
                                "error": str(exc),
                            }
                        )

            pending_live_order = any(
                _strategy_has_pending_live_trade(state.live_strategies.get(strategy_id))
                for strategy_id in managed_strategy_ids
            )
            current_round_slug = next(
                (
                    state.live_strategies[strategy_id].pending_live_slug
                    for strategy_id in managed_strategy_ids
                    if _strategy_has_pending_live_trade(state.live_strategies.get(strategy_id))
                ),
                None,
            )

            if pending_live_order and _safe_stop_requested(stop_when_safe):
                _sync_legacy_live_state_fields(state, managed_strategy_ids)
                save_session_state(state_path, state)
                result = {
                    "status": "pending_settlement",
                    "slug": current_round_slug,
                    "strategies": strategy_results,
                    "remaining_live_budget": None,
                }
                _update_runtime_control(
                    runtime_control,
                    current_round_slug=current_round_slug,
                    round_in_progress=True,
                    safe_to_switch=False,
                    pending_live_order=True,
                    **_runtime_alert_changes_for_live_result(result),
                )
                return result

            if pending_strategy_ids:
                _sync_legacy_live_state_fields(state, managed_strategy_ids)
                save_session_state(state_path, state)
                result = {
                    "status": "pending_settlement",
                    "slug": state.live_strategies[pending_strategy_ids[0]].pending_live_slug,
                    "strategies": strategy_results,
                    "remaining_live_budget": None,
                }
                _update_runtime_control(
                    runtime_control,
                    current_round_slug=state.live_strategies[pending_strategy_ids[0]].pending_live_slug,
                    round_in_progress=True,
                    safe_to_switch=False,
                    pending_live_order=True,
                    **_runtime_alert_changes_for_live_result(result),
                )
                if not _sleep_if_not_stopped(stop_event, _poll_interval_for_live_result(cfg=cfg, result=result)):
                    return {"status": "stopped"}
                continue

            try:
                remaining_live_budget: float | None = _read_available_live_balance(cfg=cfg, clob_client=live_client)
                balance_read_error: str | None = None
            except RuntimeError as exc:
                remaining_live_budget = None
                balance_read_error = str(exc)
                if _is_retryable_live_clob_error(exc):
                    _discard_cached_live_client()
            except Exception as exc:
                if not _is_retryable_live_clob_error(exc):
                    raise
                _discard_cached_live_client()
                remaining_live_budget = None
                balance_read_error = str(exc)

            pending_live_order = any(
                _strategy_has_pending_live_trade(state.live_strategies.get(strategy_id))
                for strategy_id in managed_strategy_ids
            )
            current_round_slug = next(
                (
                    state.live_strategies[strategy_id].pending_live_slug
                    for strategy_id in managed_strategy_ids
                    if _strategy_has_pending_live_trade(state.live_strategies.get(strategy_id))
                ),
                None,
            )

            try:
                current_round, next_round = market_client.find_current_and_next_rounds(now=now)
                current_entry_time = _entry_time_for_round(cfg, current_round) if current_round is not None else None
                should_log_missed_current_round = (
                    current_round is not None
                    and next_round is not None
                    and current_entry_time is not None
                    and _entry_window_missed(now, current_entry_time, grace_seconds=cfg.entry_grace_seconds)
                )
                target_round = (
                    current_round
                    if should_log_missed_current_round
                    else _select_target_round(cfg, now=now, current_round=current_round, next_round=next_round)
                )
                if target_round is not None:
                    market = market_client.get_market_by_slug(target_round.slug)
                    quote = market_client.quote_from_market(market)
            except Exception as exc:
                if not _is_retryable_live_io_error(exc):
                    raise
                consecutive_errors += 1
                backoff = _runtime_backoff_seconds(cfg, consecutive_errors)
                _sync_legacy_live_state_fields(state, managed_strategy_ids)
                save_session_state(state_path, state)
                _runtime_log(
                    "live market data transient error #"
                    + str(consecutive_errors)
                    + ": "
                    + str(exc)
                    + " | backoff="
                    + str(backoff)
                    + "s"
                )
                _update_runtime_control(
                    runtime_control,
                    current_round_slug=current_round_slug,
                    round_in_progress=pending_live_order,
                    safe_to_switch=not pending_live_order,
                    pending_live_order=pending_live_order,
                )
                if not _sleep_if_not_stopped(stop_event, backoff):
                    return {"status": "stopped"}
                continue
            consecutive_errors = 0
            if target_round is None:
                for strategy_id in configured_strategy_ids:
                    if strategy_id in handled_strategy_ids:
                        continue
                    strategy_results.append({"strategy_id": strategy_id, "status": "no_market"})
                _sync_legacy_live_state_fields(state, managed_strategy_ids)
                save_session_state(state_path, state)
                result = {
                    "status": "pending_settlement" if pending_strategy_ids else "no_market",
                    "slug": (
                        state.live_strategies[pending_strategy_ids[0]].pending_live_slug
                        if pending_strategy_ids
                        else None
                    ),
                    "strategies": strategy_results,
                    "remaining_live_budget": remaining_live_budget,
                }
            else:
                entry_time = _entry_time_for_round(cfg, target_round)
                print('[live] quote {' + _describe_quote_source(quote) + '}', flush=True)
                print('[live] ws_runtime {' + _describe_ws_runtime(market_client) + '}', flush=True)
                for strategy_id in configured_strategy_ids:
                    if strategy_id in pending_strategy_ids:
                        continue
                    try:
                        strategy_cfg = _cfg_for_live_strategy(cfg, strategy_id)
                        strategy_state = state.live_strategies.setdefault(strategy_id, LiveStrategyState())
                        if strategy_state.last_processed_live_event_slug == target_round.slug:
                            strategy_results.append(
                                {
                                    "strategy_id": strategy_id,
                                    "status": "skipped",
                                    "slug": target_round.slug,
                                    "side": None,
                                    "price": None,
                                    "should_trade": False,
                                    "skip_reason": "already_processed_round",
                                }
                            )
                            continue
                        append_trade_log(
                            log_path,
                            TradeRecord(
                                timestamp=datetime.now(timezone.utc),
                                mode="live",
                                round_index=strategy_state.round_index,
                                strategy=strategy_id,
                                entry_timing=strategy_cfg.entry_timing,
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
                                skip_reason="observed_waiting_for_entry",
                            ),
                        )
                        strategy_quote = replace(quote)
                        _apply_strategy6_signal_to_quote(
                            cfg=strategy_cfg,
                            quote=strategy_quote,
                            binance_signal_service=binance_signal_service,
                            now=now,
                            diagnostic_log=_runtime_log,
                        )
                        side_decision = _resolve_side_from_strategy(
                            cfg=strategy_cfg,
                            state=strategy_state,
                            slug=target_round.slug,
                            quote=strategy_quote,
                            market_client=market_client,
                            window=target_round,
                            now=now,
                            entry_time=entry_time,
                        )
                        if side_decision.side is None:
                            status = "waiting_for_entry" if (entry_time - now).total_seconds() > 1 else "skipped"
                            skip_reason = side_decision.reason or "signal_unavailable"
                            if status == "skipped":
                                append_trade_log(
                                    log_path,
                                    TradeRecord(
                                        timestamp=datetime.now(timezone.utc),
                                        mode="live",
                                        round_index=strategy_state.round_index,
                                        strategy=strategy_id,
                                        entry_timing=strategy_cfg.entry_timing,
                                        event_slug=target_round.slug,
                                        start_time=target_round.start_time,
                                        end_time=target_round.end_time,
                                        side="SKIP",
                                        price=_side_decision_log_price(side_decision),
                                        order_size=0.0,
                                        order_cost=0.0,
                                        expected_profit=0.0,
                                        result=None,
                                        trade_pnl=0.0,
                                        cash_pnl=strategy_state.cash_pnl,
                                        recovery_loss=strategy_state.recovery_loss,
                                        consecutive_losses=strategy_state.consecutive_losses,
                                        skip_reason=skip_reason,
                                        **_signal_record_kwargs(side_decision),
                                    ),
                                )
                                strategy_state.last_processed_live_event_slug = target_round.slug
                                strategy_state.round_index += 1
                                state.live_strategies[strategy_id] = strategy_state
                                _mirror_live_decision_to_paper_skip(
                                    mirror_state_path=mirror_paper_state_path,
                                    mirror_log_path=mirror_paper_log_path,
                                    cfg=strategy_cfg,
                                    strategy_id=strategy_id,
                                    window=target_round,
                                    strategy_state=strategy_state,
                                    side="SKIP",
                                    price=_side_decision_log_price(side_decision),
                                    side_decision=side_decision,
                                    skip_reason=skip_reason,
                                )
                            strategy_results.append(
                                {
                                    "strategy_id": strategy_id,
                                    "status": status,
                                    "slug": target_round.slug,
                                    "side": None,
                                    "price": None,
                                    "should_trade": False,
                                    "skip_reason": skip_reason,
                                    "entry_time": entry_time.isoformat(),
                                    "signal_open_up_price": side_decision.signal_open_up_price,
                                    "signal_current_up_price": side_decision.signal_current_up_price,
                                    "signal_threshold": side_decision.signal_threshold,
                                    "signal_delta": side_decision.signal_delta,
                                    "signal_locked": side_decision.signal_locked,
                                }
                            )
                            continue
                        side = side_decision.side
                        price = resolve_quote_price(side, strategy_quote)
                        if _ws_is_stale_for_trade(market_client, strategy_cfg):
                            status = "waiting_for_entry" if (entry_time - now).total_seconds() > 1 else "skipped"
                            if status == "skipped":
                                append_trade_log(
                                    log_path,
                                    TradeRecord(
                                        timestamp=datetime.now(timezone.utc),
                                        mode="live",
                                        round_index=strategy_state.round_index,
                                        strategy=strategy_id,
                                        entry_timing=strategy_cfg.entry_timing,
                                        event_slug=target_round.slug,
                                        start_time=target_round.start_time,
                                        end_time=target_round.end_time,
                                        side=side,
                                        price=price,
                                        order_size=0.0,
                                        order_cost=0.0,
                                        expected_profit=0.0,
                                        result=None,
                                        trade_pnl=0.0,
                                        cash_pnl=strategy_state.cash_pnl,
                                        recovery_loss=strategy_state.recovery_loss,
                                        consecutive_losses=strategy_state.consecutive_losses,
                                        skip_reason="ws_stale",
                                        **_signal_record_kwargs(side_decision),
                                    ),
                                )
                                strategy_state.last_processed_live_event_slug = target_round.slug
                                strategy_state.round_index += 1
                                state.live_strategies[strategy_id] = strategy_state
                                _mirror_live_decision_to_paper_skip(
                                    mirror_state_path=mirror_paper_state_path,
                                    mirror_log_path=mirror_paper_log_path,
                                    cfg=strategy_cfg,
                                    strategy_id=strategy_id,
                                    window=target_round,
                                    strategy_state=strategy_state,
                                    side=side,
                                    price=price,
                                    side_decision=side_decision,
                                    skip_reason="ws_stale",
                                )
                            strategy_results.append(
                                {
                                    "strategy_id": strategy_id,
                                    "status": status,
                                    "slug": target_round.slug,
                                    "side": side,
                                    "price": price,
                                    "should_trade": False,
                                    "skip_reason": "ws_stale",
                                    "entry_time": entry_time.isoformat(),
                                    "signal_open_up_price": side_decision.signal_open_up_price,
                                    "signal_current_up_price": side_decision.signal_current_up_price,
                                    "signal_threshold": side_decision.signal_threshold,
                                    "signal_delta": side_decision.signal_delta,
                                    "signal_locked": side_decision.signal_locked,
                                }
                            )
                            continue
                        if _entry_window_missed(now, entry_time, grace_seconds=strategy_cfg.entry_grace_seconds):
                            entry_delay_seconds = _entry_delay_seconds(now, entry_time)
                            append_trade_log(
                                log_path,
                                TradeRecord(
                                    timestamp=datetime.now(timezone.utc),
                                    mode="live",
                                    round_index=strategy_state.round_index,
                                    strategy=strategy_id,
                                    entry_timing=strategy_cfg.entry_timing,
                                    event_slug=target_round.slug,
                                    start_time=target_round.start_time,
                                    end_time=target_round.end_time,
                                    side=side,
                                    price=price,
                                    order_size=0.0,
                                    order_cost=0.0,
                                    expected_profit=0.0,
                                    result=None,
                                    trade_pnl=0.0,
                                    cash_pnl=strategy_state.cash_pnl,
                                    recovery_loss=strategy_state.recovery_loss,
                                    consecutive_losses=strategy_state.consecutive_losses,
                                    skip_reason="entry_window_missed",
                                    entry_time=entry_time,
                                    entry_delay_seconds=entry_delay_seconds,
                                    **_signal_record_kwargs(side_decision),
                                ),
                            )
                            strategy_state.round_index += 1
                            strategy_state.last_processed_live_event_slug = target_round.slug
                            state.live_strategies[strategy_id] = strategy_state
                            _mirror_live_decision_to_paper_skip(
                                mirror_state_path=mirror_paper_state_path,
                                mirror_log_path=mirror_paper_log_path,
                                cfg=strategy_cfg,
                                strategy_id=strategy_id,
                                window=target_round,
                                strategy_state=strategy_state,
                                side=side,
                                price=price,
                                side_decision=side_decision,
                                skip_reason="entry_window_missed",
                            )
                            strategy_results.append(
                                {
                                    "strategy_id": strategy_id,
                                    "status": "skipped",
                                    "slug": target_round.slug,
                                    "side": side,
                                    "price": price,
                                    "should_trade": False,
                                    "skip_reason": "entry_window_missed",
                                    "order_size": 0.0,
                                    "order_cost": 0.0,
                                    "expected_profit": 0.0,
                                    "entry_time": entry_time.isoformat(),
                                    "entry_delay_seconds": entry_delay_seconds,
                                    "signal_open_up_price": side_decision.signal_open_up_price,
                                    "signal_current_up_price": side_decision.signal_current_up_price,
                                    "signal_threshold": side_decision.signal_threshold,
                                    "signal_delta": side_decision.signal_delta,
                                    "signal_locked": side_decision.signal_locked,
                                }
                            )
                            continue
                        plan = _build_live_trade_plan_for_decision(
                            cfg=strategy_cfg,
                            state=strategy_state,
                            market=market,
                            side_decision=side_decision,
                            side=side,
                            price=price,
                        )
                        token_ids = extract_token_ids(market.get("clobTokenIds"), market.get("outcomes"))
                        token_id = token_ids.get(side)
                        opposite_token_id = _opposite_token_id_for_side(token_ids, side)
                        if not plan.should_trade:
                            skip_stop_loss_triggered = _should_reset_after_risk_gate_skip(
                                strategy_state,
                                skip_reason=plan.skip_reason,
                                cfg=strategy_cfg,
                                stop_loss_triggered=plan.stop_loss_triggered,
                            )
                            status = "waiting_for_entry" if (entry_time - now).total_seconds() > 1 else "skipped"
                            if status == "skipped":
                                append_trade_log(
                                    log_path,
                                    TradeRecord(
                                        timestamp=datetime.now(timezone.utc),
                                        mode="live",
                                        round_index=strategy_state.round_index,
                                        strategy=strategy_id,
                                        entry_timing=strategy_cfg.entry_timing,
                                        event_slug=target_round.slug,
                                        start_time=target_round.start_time,
                                        end_time=target_round.end_time,
                                        side=side,
                                        price=price,
                                        order_size=0.0,
                                        order_cost=0.0,
                                        expected_profit=0.0,
                                        result=None,
                                        trade_pnl=0.0,
                                        cash_pnl=strategy_state.cash_pnl,
                                        recovery_loss=strategy_state.recovery_loss,
                                        consecutive_losses=strategy_state.consecutive_losses,
                                        skip_reason=plan.skip_reason,
                                        stop_loss_triggered=skip_stop_loss_triggered,
                                        **_signal_record_kwargs(side_decision),
                                    ),
                                )
                                strategy_state, should_alert, skip_stop_loss_triggered, skip_streak = _apply_post_entry_risk_gate_skip(
                                    strategy_state,
                                    skip_reason=plan.skip_reason,
                                    cfg=strategy_cfg,
                                    stop_loss_triggered=plan.stop_loss_triggered,
                                )
                                strategy_state.last_processed_live_event_slug = target_round.slug
                                if should_alert:
                                    _emit_max_stake_skip_alert(
                                        slug=target_round.slug,
                                        side=side,
                                        price=price,
                                        state=strategy_state,
                                        cfg=strategy_cfg,
                                        skip_streak=skip_streak,
                                    )
                                state.live_strategies[strategy_id] = strategy_state
                                _mirror_live_decision_to_paper_skip(
                                    mirror_state_path=mirror_paper_state_path,
                                    mirror_log_path=mirror_paper_log_path,
                                    cfg=strategy_cfg,
                                    strategy_id=strategy_id,
                                    window=target_round,
                                    strategy_state=strategy_state,
                                    side=side,
                                    price=price,
                                    side_decision=side_decision,
                                    skip_reason=plan.skip_reason,
                                )
                            strategy_results.append(
                                {
                                    "strategy_id": strategy_id,
                                    "status": status,
                                    "slug": target_round.slug,
                                    "side": side,
                                    "price": price,
                                    "should_trade": False,
                                    "skip_reason": plan.skip_reason,
                                    "order_size": plan.order_size,
                                    "order_cost": plan.order_cost,
                                    "expected_profit": plan.expected_profit,
                                    "entry_time": entry_time.isoformat(),
                                    "stop_loss_triggered": skip_stop_loss_triggered,
                                    "max_stake_skip_streak": strategy_state.consecutive_max_stake_skips,
                                    "signal_open_up_price": side_decision.signal_open_up_price,
                                    "signal_current_up_price": side_decision.signal_current_up_price,
                                    "signal_threshold": side_decision.signal_threshold,
                                    "signal_delta": side_decision.signal_delta,
                                    "signal_locked": side_decision.signal_locked,
                                }
                            )
                            continue
                        strategy_state.consecutive_max_stake_skips = 0
                        if token_id is None:
                            raise RuntimeError(f"Missing token id for side={side} on market={target_round.slug}")
                        if (entry_time - now).total_seconds() > 1:
                            strategy_results.append(
                                {
                                    "strategy_id": strategy_id,
                                    "status": "waiting_for_entry",
                                    "slug": target_round.slug,
                                    "side": side,
                                    "token_id": token_id,
                                    "price": price,
                                    "should_trade": True,
                                    "skip_reason": None,
                                    "entry_time": entry_time.isoformat(),
                                    "order_size": plan.order_size,
                                    "order_cost": plan.order_cost,
                                    "expected_profit": plan.expected_profit,
                                    "order_type": strategy_cfg.live_order_type.upper(),
                                    "signal_open_up_price": side_decision.signal_open_up_price,
                                    "signal_current_up_price": side_decision.signal_current_up_price,
                                    "signal_threshold": side_decision.signal_threshold,
                                    "signal_delta": side_decision.signal_delta,
                                    "signal_locked": side_decision.signal_locked,
                                }
                            )
                            continue
                        execution, plan, side_decision, side, price, strategy_quote = _execute_live_order_with_fak_retry(
                            cfg=strategy_cfg,
                            market_client=market_client,
                            market=market,
                            live_client=live_client,
                            strategy_id=strategy_id,
                            strategy_state=strategy_state,
                            target_round=target_round,
                            entry_time=entry_time,
                            now=now,
                            quote=strategy_quote,
                            side_decision=side_decision,
                            side=side,
                            price=price,
                            token_id=token_id,
                            opposite_token_id=opposite_token_id,
                            plan=plan,
                            remaining_live_budget=remaining_live_budget,
                            balance_error=balance_read_error,
                            binance_signal_service=binance_signal_service,
                        )
                        remaining_live_budget = execution.remaining_budget
                        if execution.status == "skipped":
                            if execution.skip_reason == "live_retryable_clob_error":
                                _discard_cached_live_client()
                            append_trade_log(
                                log_path,
                                TradeRecord(
                                    timestamp=datetime.now(timezone.utc),
                                    mode="live",
                                    round_index=strategy_state.round_index,
                                    strategy=strategy_id,
                                    entry_timing=strategy_cfg.entry_timing,
                                    event_slug=target_round.slug,
                                    start_time=target_round.start_time,
                                    end_time=target_round.end_time,
                                    side=side,
                                    price=price,
                                    order_size=0.0,
                                    order_cost=0.0,
                                    expected_profit=0.0,
                                    result=None,
                                    trade_pnl=0.0,
                                    cash_pnl=strategy_state.cash_pnl,
                                    recovery_loss=strategy_state.recovery_loss,
                                    consecutive_losses=strategy_state.consecutive_losses,
                                    skip_reason=execution.skip_reason,
                                    balance_error=execution.balance_error,
                                    live_order_book_price=execution.live_order_book_price,
                                    live_price_cap=execution.live_price_cap,
                                    **_signal_record_kwargs(side_decision),
                                ),
                            )
                            strategy_state.last_processed_live_event_slug = target_round.slug
                            state.live_strategies[strategy_id] = strategy_state
                            _mirror_live_decision_to_paper_skip(
                                mirror_state_path=mirror_paper_state_path,
                                mirror_log_path=mirror_paper_log_path,
                                cfg=strategy_cfg,
                                strategy_id=strategy_id,
                                window=target_round,
                                strategy_state=strategy_state,
                                side=side,
                                price=price,
                                side_decision=side_decision,
                                skip_reason=execution.skip_reason,
                            )
                            strategy_results.append(
                                {
                                    "strategy_id": strategy_id,
                                    "status": "skipped",
                                    "slug": target_round.slug,
                                    "side": side,
                                    "price": price,
                                    "should_trade": False,
                                    "skip_reason": execution.skip_reason,
                                    "balance_error": execution.balance_error,
                                    "order_size": plan.order_size,
                                    "order_cost": plan.order_cost,
                                    "expected_profit": plan.expected_profit,
                                    "live_order_book_price": execution.live_order_book_price,
                                    "live_price_cap": execution.live_price_cap,
                                    "signal_open_up_price": side_decision.signal_open_up_price,
                                    "signal_current_up_price": side_decision.signal_current_up_price,
                                    "signal_threshold": side_decision.signal_threshold,
                                    "signal_delta": side_decision.signal_delta,
                                    "signal_locked": side_decision.signal_locked,
                                }
                            )
                            continue
                        executed_plan, fill_source = _plan_with_verified_live_fill(
                            plan=plan,
                            side=side,
                            order_id=execution.order_id,
                            clob_client=live_client,
                            min_entry_price=getattr(strategy_cfg, "min_entry_price", None),
                            max_entry_price=plan.max_entry_price if plan.max_entry_price is not None else getattr(strategy_cfg, "max_entry_price", None),
                            max_price_improvement=getattr(strategy_cfg, "live_max_price_improvement", None),
                            strategy_id=strategy_id,
                            slug=target_round.slug,
                        )
                        append_trade_log(
                            log_path,
                            TradeRecord(
                                timestamp=datetime.now(timezone.utc),
                                mode="live",
                                round_index=strategy_state.round_index,
                                strategy=strategy_id,
                                entry_timing=strategy_cfg.entry_timing,
                                event_slug=target_round.slug,
                                start_time=target_round.start_time,
                                end_time=target_round.end_time,
                                side=side,
                                price=executed_plan.price,
                                order_size=executed_plan.order_size,
                                order_cost=executed_plan.order_cost,
                                expected_profit=executed_plan.expected_profit,
                                result=None,
                                trade_pnl=0.0,
                                cash_pnl=strategy_state.cash_pnl,
                                recovery_loss=strategy_state.recovery_loss,
                                consecutive_losses=strategy_state.consecutive_losses,
                                order_id=execution.order_id,
                                fill_source=fill_source,
                                raw_price=executed_plan.raw_price,
                                raw_order_cost=executed_plan.raw_order_cost,
                                fee=executed_plan.fee,
                                live_order_book_price=execution.live_order_book_price,
                                live_price_cap=execution.live_price_cap,
                                raw_price_cap_sent=execution.raw_price_cap_sent,
                                **_signal_record_kwargs(side_decision),
                            ),
                        )
                        _queue_pending_live_trade(
                            strategy_state,
                            window=target_round,
                            plan=executed_plan,
                            side=side,
                            strategy_id=strategy_id,
                            entry_timing=strategy_cfg.entry_timing,
                            order_id=execution.order_id,
                        )
                        strategy_state.round_index += 1
                        strategy_state.last_processed_live_event_slug = target_round.slug
                        state.live_strategies[strategy_id] = strategy_state
                        _mirror_live_decision_to_pending_paper_trade(
                            mirror_state_path=mirror_paper_state_path,
                            mirror_log_path=mirror_paper_log_path,
                            cfg=strategy_cfg,
                            strategy_id=strategy_id,
                            window=target_round,
                            plan=plan,
                            side=side,
                            side_decision=side_decision,
                        )
                        strategy_results.append(
                            {
                                "strategy_id": strategy_id,
                                "status": "submitted",
                                "slug": target_round.slug,
                                "side": side,
                                "token_id": token_id,
                                "price": executed_plan.price,
                                "raw_price": executed_plan.raw_price,
                                "order_size": executed_plan.order_size,
                                "raw_order_cost": executed_plan.raw_order_cost,
                                "fee": executed_plan.fee,
                                "effective_price_with_fee": executed_plan.price,
                                "effective_order_cost_with_fee": executed_plan.order_cost,
                                "order_cost": executed_plan.order_cost,
                                "expected_profit": executed_plan.expected_profit,
                                "live_order_book_price": execution.live_order_book_price,
                                "live_price_cap": execution.live_price_cap,
                                "order_type": strategy_cfg.live_order_type.upper(),
                                "order_id": execution.order_id,
                                "response": execution.response,
                                "signal_open_up_price": side_decision.signal_open_up_price,
                                "signal_current_up_price": side_decision.signal_current_up_price,
                                "signal_threshold": side_decision.signal_threshold,
                                "signal_delta": side_decision.signal_delta,
                                "signal_locked": side_decision.signal_locked,
                            }
                        )
                    except Exception as exc:
                        _runtime_log(
                            f"live strategy {strategy_id} evaluation error: {exc}"
                        )
                        status = "blocked" if _is_live_trading_restricted_error(exc) else "error"
                        error_code = "trading_restricted" if status == "blocked" else None
                        try:
                            error_cfg = _cfg_for_live_strategy(cfg, strategy_id)
                        except Exception:
                            error_cfg = replace(cfg, strategy_id=strategy_id)
                        error_state = state.live_strategies.get(strategy_id) or LiveStrategyState()
                        _append_live_strategy_error_log(
                            log_path=log_path,
                            cfg=error_cfg,
                            strategy_id=strategy_id,
                            strategy_state=error_state,
                            target_round=target_round,
                            skip_reason=_strategy_exception_skip_reason("strategy_evaluation_error", exc),
                        )
                        strategy_results.append(
                            {
                                "strategy_id": strategy_id,
                                "status": status,
                                "phase": "evaluation",
                                "slug": target_round.slug,
                                "error_code": error_code,
                                "error": str(exc),
                            }
                        )
                _sync_legacy_live_state_fields(state, managed_strategy_ids)
                save_session_state(state_path, state)
                if any(item.get("status") == "submitted" for item in strategy_results):
                    overall_status = "submitted"
                elif any(item.get("status") == "pending_settlement" for item in strategy_results):
                    overall_status = "pending_settlement"
                elif any(item.get("status") == "waiting_for_entry" for item in strategy_results):
                    overall_status = "waiting_for_entry"
                elif any(item.get("status") == "blocked" for item in strategy_results):
                    overall_status = "blocked"
                elif any(item.get("status") == "error" for item in strategy_results):
                    overall_status = "error"
                else:
                    overall_status = "skipped"
                result = {
                    "status": overall_status,
                    "slug": target_round.slug,
                    "strategies": strategy_results,
                    "remaining_live_budget": remaining_live_budget,
                }
            _update_runtime_control(
                runtime_control,
                current_round_slug=current_round_slug,
                round_in_progress=pending_live_order,
                safe_to_switch=not pending_live_order,
                pending_live_order=pending_live_order,
                **_runtime_alert_changes_for_live_result(result),
            )
            if _is_stop_requested(stop_event):
                return {'status': 'stopped'}
            if _safe_stop_requested(stop_when_safe) and result.get('status') == 'pending_settlement':
                return result
            if result.get('status') in {'submitted', 'skipped', 'waiting_for_entry', 'pending_settlement', 'no_market', 'blocked'}:
                if not _sleep_if_not_stopped(stop_event, _poll_interval_for_live_result(cfg=cfg, result=result)):
                    return {'status': 'stopped'}
                continue
            return result
    finally:
        if binance_signal_service is not None:
            binance_signal_service.close()


def run_paper_trading(
    cfg: AppConfig | None = None,
    *,
    client: PolymarketClient | None = None,
    binance_signal_service: BinanceDepth5SignalService | None = None,
    state_path: Path | None = None,
    log_path: Path | None = None,
    dry_run_once: bool = False,
    stop_event: threading.Event | None = None,
    config_provider: Callable[[], AppConfig] | None = None,
    runtime_control: RuntimeControl | None = None,
    stop_when_safe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    cfg = cfg or AppConfig()
    client_provided = client is not None
    state_path_provided = state_path is not None
    log_path_provided = log_path is not None
    client = client or PolymarketClient(cfg)
    state_path = state_path or cfg.logs_dir / "session_state.json"
    log_path = log_path or cfg.logs_dir / "paper_trades.csv"
    optimizer_state_path = cfg.logs_dir / "optimizer_state.json"
    strategy_ids = _paper_strategy_ids_for_runtime(cfg)
    optimizer_state_payload, active_challengers = _load_active_optimizer_challengers(optimizer_state_path)
    binance_signal_service = _sync_paper_binance_signal_service(
        cfg=cfg,
        strategy_ids=strategy_ids,
        service=binance_signal_service,
    )
    loaded_state = _load_session_state_for_paper_runtime(state_path, strategy_ids)
    state = _clone_session_state(loaded_state)
    _ensure_paper_strategy_state_map(state, strategy_ids)
    _sync_legacy_paper_state_fields(state, strategy_ids)
    consecutive_errors = 0
    _update_runtime_control(
        runtime_control,
        current_round_slug=None,
        round_in_progress=False,
        safe_to_switch=True,
        pending_live_order=False,
    )
    _runtime_log(
        'paper-trade started | strategies=' + ','.join(str(item) for item in strategy_ids)
        + ' entry_timing=' + cfg.entry_timing
        + ' poll=' + str(cfg.poll_interval_seconds)
        + 's dry_run_once=' + str(dry_run_once)
    )

    try:
        while True:
            if _is_stop_requested(stop_event):
                return {"status": "stopped"}
            try:
                if config_provider is not None:
                    candidate_cfg = config_provider()
                    if candidate_cfg is not None:
                        cfg = candidate_cfg
                        strategy_ids = _paper_strategy_ids_for_runtime(cfg)
                        binance_signal_service = _sync_paper_binance_signal_service(
                            cfg=cfg,
                            strategy_ids=strategy_ids,
                            service=binance_signal_service,
                        )
                        if not client_provided:
                            client.config = cfg
                        if not state_path_provided:
                            state_path = cfg.logs_dir / "session_state.json"
                        if not log_path_provided:
                            log_path = cfg.logs_dir / "paper_trades.csv"
                        optimizer_state_path = cfg.logs_dir / "optimizer_state.json"
                        optimizer_state_payload, active_challengers = _load_active_optimizer_challengers(optimizer_state_path)
                        _ensure_paper_strategy_state_map(state, strategy_ids)
                        _sync_legacy_paper_state_fields(state, strategy_ids)
                now = datetime.now(timezone.utc)
                pending_strategy_ids: list[int] = []
                settled_any_pending = False
                state_changed = False
                challenger_state_changed = False
                for strategy_id in strategy_ids:
                    strategy_state = state.paper_strategies.setdefault(strategy_id, PaperStrategyState())
                    strategy_session = _paper_strategy_state_to_session_state(strategy_state, state)
                    if _refresh_daily_session_state(strategy_session, now):
                        state_changed = True
                    strategy_session, settled_changed = _settle_pending_paper_trades(
                        client=client,
                        state=strategy_session,
                        log_path=log_path,
                    )
                    if settled_changed:
                        settled_any_pending = True
                        state_changed = True
                    state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                    if _is_stop_requested(stop_event):
                        return {"status": "stopped"}
                    strategy_cfg = _cfg_for_paper_strategy(cfg, strategy_id)
                    if (
                        state.paper_strategies[strategy_id].pending_paper_trades
                        and not _strategy_can_continue_with_pending(strategy_cfg)
                    ):
                        pending_strategy_ids.append(strategy_id)
                for challenger in active_challengers:
                    strategy_state = challenger.get("_paper_state")
                    if not isinstance(strategy_state, PaperStrategyState):
                        continue
                    strategy_session = _paper_strategy_state_to_session_state(strategy_state, state)
                    strategy_session, settled_changed = _settle_pending_paper_trades(
                        client=client,
                        state=strategy_session,
                        log_path=log_path,
                    )
                    next_state = _session_state_to_paper_strategy_state(strategy_session)
                    next_state.experiment_id = str(challenger.get("candidate_id") or next_state.experiment_id or "").strip() or None
                    challenger["_paper_state"] = next_state
                    if settled_changed:
                        settled_any_pending = True
                        challenger_state_changed = True
                if state_changed or settled_any_pending:
                    _sync_legacy_paper_state_fields(state, strategy_ids)
                    round_completed = True
                    _copy_session_state_into(loaded_state, state)
                    save_session_state(state_path, state)
                if challenger_state_changed:
                    _save_active_optimizer_challengers(optimizer_state_path, optimizer_state_payload, active_challengers)
                if pending_strategy_ids:
                    _update_runtime_control(
                        runtime_control,
                        current_round_slug=state.paper_strategies[pending_strategy_ids[0]].pending_paper_trades[0].event_slug,
                        round_in_progress=True,
                        safe_to_switch=False,
                        pending_live_order=False,
                    )
                    if _safe_stop_requested(stop_when_safe):
                        return {"status": "pending_settlement"}
                    if not _sleep_if_not_stopped(
                        stop_event,
                        _poll_interval_for_live_result(
                            cfg=cfg,
                            result={"status": "pending_settlement"},
                        ),
                    ):
                        return {"status": "stopped"}
                    continue
                elif _safe_stop_requested(stop_when_safe):
                    _update_runtime_control(
                        runtime_control,
                        current_round_slug=None,
                        round_in_progress=False,
                        safe_to_switch=True,
                        pending_live_order=False,
                    )
                    return {"status": "stopped"}
                current_round, next_round = client.find_current_and_next_rounds(now=now)
                current_entry_time = _entry_time_for_round(cfg, current_round) if current_round is not None else None
                should_log_missed_current_round = (
                    not dry_run_once
                    and current_round is not None
                    and next_round is not None
                    and current_entry_time is not None
                    and _entry_window_missed(now, current_entry_time, grace_seconds=cfg.entry_grace_seconds)
                )
                target_round = (
                    current_round
                    if should_log_missed_current_round
                    else _select_target_round(cfg, now=now, current_round=current_round, next_round=next_round)
                )
                if target_round is None:
                    if dry_run_once:
                        return {"status": "no_market"}
                    _runtime_log('no active round found; waiting ' + str(cfg.poll_interval_seconds) + 's')
                    consecutive_errors = 0
                    if not _sleep_if_not_stopped(
                        stop_event,
                        _poll_interval_for_target_round(cfg=cfg, now=now, target_round=None),
                    ):
                        return {"status": "stopped"}
                    continue
    
                entry_time = _entry_time_for_round(cfg, target_round)
                market = client.get_market_by_slug(target_round.slug)
                quote = client.quote_from_market(market)
                remaining_paper_budget: float | None = max(0.0, float(cfg.paper_simulated_wallet_balance))
                any_processed = False
                round_completed = False
                for strategy_id in strategy_ids:
                    strategy_state = state.paper_strategies.setdefault(strategy_id, PaperStrategyState())
                    experiment_id = _paper_experiment_id(strategy_id, strategy_state)
                    strategy_cfg = _cfg_for_paper_strategy(cfg, strategy_id)
                    if (
                        strategy_state.pending_paper_trades
                        and not _strategy_can_continue_with_pending(strategy_cfg)
                    ):
                        continue
                    if strategy_state.last_processed_paper_event_slug == target_round.slug:
                        any_processed = True
                        round_completed = True
                        continue
                    strategy_session = _paper_strategy_state_to_session_state(strategy_state, state)
                    strategy_quote = replace(quote)
                    _apply_strategy6_signal_to_quote(
                        cfg=strategy_cfg,
                        quote=strategy_quote,
                        binance_signal_service=binance_signal_service,
                        now=now,
                        diagnostic_log=_runtime_log,
                    )
                    _runtime_log('strategy=' + str(strategy_id) + ' round=' + target_round.slug + ' quote {' + _describe_quote_source(strategy_quote) + '}')
                    _runtime_log('strategy=' + str(strategy_id) + ' round=' + target_round.slug + ' ws_runtime {' + _describe_ws_runtime(client) + '}')
                    side_decision = _resolve_side_from_strategy(
                        cfg=strategy_cfg,
                        state=strategy_session,
                        slug=target_round.slug,
                        quote=strategy_quote,
                        market_client=client,
                        window=target_round,
                        now=now,
                        entry_time=entry_time,
                    )
                    _runtime_log(
                        'strategy=' + str(strategy_id)
                        + ' round=' + target_round.slug
                        + ' side=' + str(side_decision.side)
                        + ' entry_at=' + entry_time.isoformat()
                        + ' signal={' + _describe_side_decision(side_decision) + '}'
                        + ' quote_source=' + str(strategy_quote.source)
                    )
                    state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                    any_processed = True
    
                    if side_decision.side is None:
                        if dry_run_once:
                            _runtime_log(
                                'dry-run strategy=' + str(strategy_id)
                                + ' round=' + target_round.slug
                                + ' skip due to signal; reason=' + str(side_decision.reason or 'signal_unavailable')
                            )
                            return {
                                "status": "dry_run",
                                "slug": target_round.slug,
                                "side": None,
                                "price": None,
                                "should_trade": False,
                                "skip_reason": side_decision.reason or "signal_unavailable",
                                "entry_time": entry_time.isoformat(),
                                "signal_open_up_price": side_decision.signal_open_up_price,
                                "signal_current_up_price": side_decision.signal_current_up_price,
                                "signal_threshold": side_decision.signal_threshold,
                                "signal_delta": side_decision.signal_delta,
                                "signal_locked": side_decision.signal_locked,
                            }
                        if (entry_time - now).total_seconds() > 1:
                            continue
                        append_trade_log(
                            log_path,
                            TradeRecord(
                                timestamp=datetime.now(timezone.utc),
                                mode="paper",
                                experiment_id=experiment_id,
                                round_index=strategy_session.round_index,
                                strategy=strategy_id,
                                entry_timing=strategy_cfg.entry_timing,
                                event_slug=target_round.slug,
                                start_time=target_round.start_time,
                                end_time=target_round.end_time,
                                side="SKIP",
                                price=_side_decision_log_price(side_decision),
                                order_size=0.0,
                                order_cost=0.0,
                                expected_profit=0.0,
                                result=None,
                                trade_pnl=0.0,
                                cash_pnl=strategy_session.cash_pnl,
                                recovery_loss=strategy_session.recovery_loss,
                                consecutive_losses=strategy_session.consecutive_losses,
                                skip_reason=side_decision.reason or "signal_unavailable",
                                **_signal_record_kwargs(side_decision),
                            ),
                        )
                        strategy_session.last_processed_paper_event_slug = target_round.slug
                        strategy_session.round_index += 1
                        state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                        _sync_legacy_paper_state_fields(state, strategy_ids)
                        round_completed = True
                        _copy_session_state_into(loaded_state, state)
                        save_session_state(state_path, state)
                        continue
    
                    side = side_decision.side
                    price = resolve_quote_price(side, strategy_quote)
                    if _ws_is_stale_for_trade(client, strategy_cfg):
                        if dry_run_once:
                            return {
                                "status": "dry_run",
                                "slug": target_round.slug,
                                "side": side,
                                "price": price,
                                "should_trade": False,
                                "skip_reason": "ws_stale",
                                "entry_time": entry_time.isoformat(),
                                "projected_max_stake_skip_streak": 0,
                                "signal_open_up_price": side_decision.signal_open_up_price,
                                "signal_current_up_price": side_decision.signal_current_up_price,
                                "signal_threshold": side_decision.signal_threshold,
                                "signal_delta": side_decision.signal_delta,
                                "signal_locked": side_decision.signal_locked,
                            }
                        if (entry_time - now).total_seconds() > 1:
                            continue
                        append_trade_log(
                            log_path,
                            TradeRecord(
                                timestamp=datetime.now(timezone.utc),
                                mode="paper",
                                experiment_id=experiment_id,
                                round_index=strategy_session.round_index,
                                strategy=strategy_id,
                                entry_timing=strategy_cfg.entry_timing,
                                event_slug=target_round.slug,
                                start_time=target_round.start_time,
                                end_time=target_round.end_time,
                                side=side,
                                price=price,
                                order_size=0.0,
                                order_cost=0.0,
                                expected_profit=0.0,
                                result=None,
                                trade_pnl=0.0,
                                cash_pnl=strategy_session.cash_pnl,
                                recovery_loss=strategy_session.recovery_loss,
                                consecutive_losses=strategy_session.consecutive_losses,
                                skip_reason="ws_stale",
                                **_signal_record_kwargs(side_decision),
                            ),
                        )
                        strategy_session.last_processed_paper_event_slug = target_round.slug
                        strategy_session.round_index += 1
                        state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                        _sync_legacy_paper_state_fields(state, strategy_ids)
                        round_completed = True
                        _copy_session_state_into(loaded_state, state)
                        save_session_state(state_path, state)
                        continue
    
                    if _entry_window_missed(now, entry_time, grace_seconds=strategy_cfg.entry_grace_seconds):
                        if dry_run_once:
                            return {
                                "status": "dry_run",
                                "slug": target_round.slug,
                                "side": side,
                                "price": price,
                                "should_trade": False,
                                "skip_reason": "entry_window_missed",
                                "entry_time": entry_time.isoformat(),
                                "projected_max_stake_skip_streak": 0,
                                "signal_open_up_price": side_decision.signal_open_up_price,
                                "signal_current_up_price": side_decision.signal_current_up_price,
                                "signal_threshold": side_decision.signal_threshold,
                                "signal_delta": side_decision.signal_delta,
                                "signal_locked": side_decision.signal_locked,
                            }
                        append_trade_log(
                            log_path,
                            TradeRecord(
                                timestamp=datetime.now(timezone.utc),
                                mode="paper",
                                experiment_id=experiment_id,
                                round_index=strategy_session.round_index,
                                strategy=strategy_id,
                                entry_timing=strategy_cfg.entry_timing,
                                event_slug=target_round.slug,
                                start_time=target_round.start_time,
                                end_time=target_round.end_time,
                                side=side,
                                price=price,
                                order_size=0.0,
                                order_cost=0.0,
                                expected_profit=0.0,
                                result=None,
                                trade_pnl=0.0,
                                cash_pnl=strategy_session.cash_pnl,
                                recovery_loss=strategy_session.recovery_loss,
                                consecutive_losses=strategy_session.consecutive_losses,
                                skip_reason="entry_window_missed",
                                **_signal_record_kwargs(side_decision),
                            ),
                        )
                        strategy_session.last_processed_paper_event_slug = target_round.slug
                        strategy_session.round_index += 1
                        state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                        _sync_legacy_paper_state_fields(state, strategy_ids)
                        round_completed = True
                        _copy_session_state_into(loaded_state, state)
                        save_session_state(state_path, state)
                        continue
    
                    plan = build_trade_plan(
                        state=strategy_session,
                        side=side,
                        price=price,
                        min_entry_price=getattr(strategy_cfg, "min_entry_price", getattr(strategy_cfg, "min_price_threshold", None)),
                        max_entry_price=_max_entry_price_for_decision(strategy_cfg, side_decision),
                        min_price_threshold=getattr(strategy_cfg, 'min_price_threshold', None),
                        max_price_threshold=strategy_cfg.max_price_threshold,
                        min_stake=_effective_min_order_cost(strategy_cfg, market),
                        max_stake=strategy_cfg.max_stake,
                        max_consecutive_losses=strategy_cfg.max_consecutive_losses,
                        base_order_cost=strategy_cfg.base_order_cost,
                        order_cost_multiplier=_order_cost_multiplier_for_decision(strategy_cfg, side_decision, price),
                        martingale_enabled=strategy_cfg.martingale_enabled,
                        martingale_multiplier=strategy_cfg.martingale_multiplier,
                        martingale_max_stake=strategy_cfg.martingale_max_stake,
                    )
                    if dry_run_once:
                        projected_streak = (
                            strategy_session.consecutive_max_stake_skips + 1
                            if plan.skip_reason == "order_cost_above_max_stake"
                            else 0
                        )
                        return {
                            "status": "dry_run",
                            "slug": target_round.slug,
                            "side": side,
                            "price": price,
                            "should_trade": plan.should_trade,
                            "skip_reason": plan.skip_reason,
                            "order_size": plan.order_size,
                            "order_cost": plan.order_cost,
                            "expected_profit": plan.expected_profit,
                            "entry_time": entry_time.isoformat(),
                            "projected_max_stake_skip_streak": projected_streak,
                            "signal_open_up_price": side_decision.signal_open_up_price,
                            "signal_current_up_price": side_decision.signal_current_up_price,
                            "signal_threshold": side_decision.signal_threshold,
                            "signal_delta": side_decision.signal_delta,
                            "signal_locked": side_decision.signal_locked,
                        }
                    if not plan.should_trade:
                        if (entry_time - now).total_seconds() > 1:
                            continue
                        skip_stop_loss_triggered = _should_reset_after_risk_gate_skip(
                            strategy_session,
                            skip_reason=plan.skip_reason,
                            cfg=strategy_cfg,
                            stop_loss_triggered=plan.stop_loss_triggered,
                        )
                        append_trade_log(
                            log_path,
                            TradeRecord(
                                timestamp=datetime.now(timezone.utc),
                                mode="paper",
                                experiment_id=experiment_id,
                                round_index=strategy_session.round_index,
                                strategy=strategy_id,
                                entry_timing=strategy_cfg.entry_timing,
                                event_slug=target_round.slug,
                                start_time=target_round.start_time,
                                end_time=target_round.end_time,
                                side=side,
                                price=price,
                                order_size=0.0,
                                order_cost=0.0,
                                expected_profit=0.0,
                                result=None,
                                trade_pnl=0.0,
                                cash_pnl=strategy_session.cash_pnl,
                                recovery_loss=strategy_session.recovery_loss,
                                consecutive_losses=strategy_session.consecutive_losses,
                                stop_loss_triggered=skip_stop_loss_triggered,
                                skip_reason=plan.skip_reason,
                                **_signal_record_kwargs(side_decision),
                            ),
                        )
                        strategy_session.last_processed_paper_event_slug = target_round.slug
                        strategy_session, should_alert, _, skip_streak = _apply_post_entry_risk_gate_skip(
                            strategy_session,
                            skip_reason=plan.skip_reason,
                            cfg=strategy_cfg,
                            stop_loss_triggered=plan.stop_loss_triggered,
                        )
                        if should_alert:
                            _emit_max_stake_skip_alert(
                                slug=target_round.slug,
                                side=side,
                                price=price,
                                state=strategy_session,
                                cfg=strategy_cfg,
                                skip_streak=skip_streak,
                            )
                        state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                        _sync_legacy_paper_state_fields(state, strategy_ids)
                        round_completed = True
                        _copy_session_state_into(loaded_state, state)
                        save_session_state(state_path, state)
                        continue
    
                    strategy_session.consecutive_max_stake_skips = 0
                    execution_skip_reason = _live_quote_execution_skip_reason(
                        cfg=strategy_cfg,
                        plan=plan,
                        side=side,
                        quote=strategy_quote,
                    )
                    if execution_skip_reason is not None:
                        append_trade_log(
                            log_path,
                            TradeRecord(
                                timestamp=datetime.now(timezone.utc),
                                mode="paper",
                                experiment_id=experiment_id,
                                round_index=strategy_session.round_index,
                                strategy=strategy_id,
                                entry_timing=strategy_cfg.entry_timing,
                                event_slug=target_round.slug,
                                start_time=target_round.start_time,
                                end_time=target_round.end_time,
                                side=side,
                                price=price,
                                order_size=0.0,
                                order_cost=0.0,
                                expected_profit=0.0,
                                result=None,
                                trade_pnl=0.0,
                                cash_pnl=strategy_session.cash_pnl,
                                recovery_loss=strategy_session.recovery_loss,
                                consecutive_losses=strategy_session.consecutive_losses,
                                skip_reason=execution_skip_reason,
                                **_signal_record_kwargs(side_decision),
                            ),
                        )
                        strategy_session.last_processed_paper_event_slug = target_round.slug
                        strategy_session.round_index += 1
                        state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                        _sync_legacy_paper_state_fields(state, strategy_ids)
                        round_completed = True
                        _copy_session_state_into(loaded_state, state)
                        save_session_state(state_path, state)
                        continue
                    if (entry_time - now).total_seconds() > 1:
                        state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                        continue
                    token_ids = extract_token_ids(market.get("clobTokenIds"), market.get("outcomes"))
                    token_id = token_ids.get(side)
                    execution = _execute_order_plan(
                        mode="paper",
                        cfg=strategy_cfg,
                        clob_client=None,
                        strategy_id=strategy_id,
                        slug=target_round.slug,
                        token_id=token_id,
                        plan=plan,
                        remaining_budget=remaining_paper_budget,
                        opposite_token_id=_opposite_token_id_for_side(token_ids, side),
                    )
                    remaining_paper_budget = execution.remaining_budget
                    if execution.status == "skipped":
                        append_trade_log(
                            log_path,
                            TradeRecord(
                                timestamp=datetime.now(timezone.utc),
                                mode="paper",
                                experiment_id=experiment_id,
                                round_index=strategy_session.round_index,
                                strategy=strategy_id,
                                entry_timing=strategy_cfg.entry_timing,
                                event_slug=target_round.slug,
                                start_time=target_round.start_time,
                                end_time=target_round.end_time,
                                side=side,
                                price=price,
                                order_size=0.0,
                                order_cost=0.0,
                                expected_profit=0.0,
                                result=None,
                                trade_pnl=0.0,
                                cash_pnl=strategy_session.cash_pnl,
                                recovery_loss=strategy_session.recovery_loss,
                                consecutive_losses=strategy_session.consecutive_losses,
                                skip_reason=execution.skip_reason,
                                **_signal_record_kwargs(side_decision),
                            ),
                        )
                        round_completed = True
                        continue
                    queued = _queue_pending_paper_trade(
                        state=strategy_session,
                        window=target_round,
                        plan=plan,
                        side=side,
                        cfg=strategy_cfg,
                        side_decision=side_decision,
                        experiment_id=experiment_id,
                    )
                    if queued:
                        strategy_session.last_processed_paper_event_slug = target_round.slug
                        strategy_session.round_index += 1
                    state.paper_strategies[strategy_id] = _session_state_to_paper_strategy_state(strategy_session)
                    _sync_legacy_paper_state_fields(state, strategy_ids)
                    round_completed = True
                    _copy_session_state_into(loaded_state, state)
                    save_session_state(state_path, state)
    
                for challenger in active_challengers:
                    strategy_state = challenger.get("_paper_state")
                    if not isinstance(strategy_state, PaperStrategyState):
                        continue
                    experiment_id = str(challenger.get("candidate_id") or "").strip() or _paper_experiment_id(
                        int(challenger.get("base_strategy_id") or 0),
                        strategy_state,
                    )
                    base_strategy_id = int(challenger.get("base_strategy_id") or 0)
                    if base_strategy_id < 1:
                        continue
                    strategy_cfg = _candidate_cfg_with_params(cfg, base_strategy_id, challenger.get("params"))
                    if (
                        strategy_state.pending_paper_trades
                        and not _strategy_can_continue_with_pending(strategy_cfg)
                    ):
                        continue
                    strategy_session = _paper_strategy_state_to_session_state(strategy_state, state)
                    strategy_quote = replace(quote)
                    _apply_strategy6_signal_to_quote(
                        cfg=strategy_cfg,
                        quote=strategy_quote,
                        binance_signal_service=binance_signal_service,
                        now=now,
                        diagnostic_log=_runtime_log,
                    )
                    side_decision = _resolve_side_from_strategy(
                        cfg=strategy_cfg,
                        state=strategy_session,
                        slug=target_round.slug,
                        quote=strategy_quote,
                        market_client=client,
                        window=target_round,
                        now=now,
                        entry_time=entry_time,
                    )
                    if side_decision.side is None:
                        next_state = _session_state_to_paper_strategy_state(strategy_session)
                        next_state.experiment_id = experiment_id
                        challenger["_paper_state"] = next_state
                        continue
    
                    side = side_decision.side
                    price = resolve_quote_price(side, strategy_quote)
                    if _ws_is_stale_for_trade(client, strategy_cfg):
                        next_state = _session_state_to_paper_strategy_state(strategy_session)
                        next_state.experiment_id = experiment_id
                        challenger["_paper_state"] = next_state
                        continue
    
                    if _entry_window_missed(now, entry_time, grace_seconds=strategy_cfg.entry_grace_seconds):
                        next_state = _session_state_to_paper_strategy_state(strategy_session)
                        next_state.experiment_id = experiment_id
                        challenger["_paper_state"] = next_state
                        continue
    
                    plan = build_trade_plan(
                        state=strategy_session,
                        side=side,
                        price=price,
                        min_entry_price=getattr(strategy_cfg, "min_entry_price", getattr(strategy_cfg, "min_price_threshold", None)),
                        max_entry_price=_max_entry_price_for_decision(strategy_cfg, side_decision),
                        min_price_threshold=getattr(strategy_cfg, 'min_price_threshold', None),
                        max_price_threshold=strategy_cfg.max_price_threshold,
                        min_stake=_effective_min_order_cost(strategy_cfg, market),
                        max_stake=strategy_cfg.max_stake,
                        max_consecutive_losses=strategy_cfg.max_consecutive_losses,
                        base_order_cost=strategy_cfg.base_order_cost,
                        order_cost_multiplier=_order_cost_multiplier_for_decision(strategy_cfg, side_decision, price),
                        martingale_enabled=strategy_cfg.martingale_enabled,
                        martingale_multiplier=strategy_cfg.martingale_multiplier,
                        martingale_max_stake=strategy_cfg.martingale_max_stake,
                    )
                    if not plan.should_trade:
                        next_state = _session_state_to_paper_strategy_state(strategy_session)
                        next_state.experiment_id = experiment_id
                        challenger["_paper_state"] = next_state
                        continue
    
                    if (entry_time - now).total_seconds() > 1:
                        next_state = _session_state_to_paper_strategy_state(strategy_session)
                        next_state.experiment_id = experiment_id
                        challenger["_paper_state"] = next_state
                        continue
    
                    queued = _queue_pending_paper_trade(
                        state=strategy_session,
                        window=target_round,
                        plan=plan,
                        side=side,
                        cfg=strategy_cfg,
                        side_decision=side_decision,
                        experiment_id=experiment_id,
                    )
                    if queued:
                        strategy_session.round_index += 1
                    next_state = _session_state_to_paper_strategy_state(strategy_session)
                    next_state.experiment_id = experiment_id
                    challenger["_paper_state"] = next_state
                    challenger_state_changed = True
    
                if challenger_state_changed:
                    _save_active_optimizer_challengers(optimizer_state_path, optimizer_state_payload, active_challengers)
    
                consecutive_errors = 0
                if round_completed:
                    if not _sleep_until_round_end(cfg, target_round, stop_event):
                        return {"status": "stopped"}
                    continue
                if not any_processed and pending_strategy_ids:
                    if not _sleep_if_not_stopped(
                        stop_event,
                        _poll_interval_for_target_round(
                            cfg=cfg,
                            now=datetime.now(timezone.utc),
                            target_round=target_round,
                        ),
                    ):
                        return {"status": "stopped"}
                    continue
                if any_processed and datetime.now(timezone.utc) < target_round.end_time:
                    if not _sleep_if_not_stopped(
                        stop_event,
                        _poll_interval_for_target_round(
                            cfg=cfg,
                            now=datetime.now(timezone.utc),
                            target_round=target_round,
                        ),
                    ):
                        return {"status": "stopped"}
                    continue
                if not _sleep_if_not_stopped(
                    stop_event,
                    _poll_interval_for_target_round(
                        cfg=cfg,
                        now=datetime.now(timezone.utc),
                        target_round=target_round,
                    ),
                ):
                    return {"status": "stopped"}
                continue
            except Exception as exc:
                if dry_run_once:
                    return {"status": "error", "error": str(exc)}
                consecutive_errors += 1
                backoff = _runtime_backoff_seconds(cfg, consecutive_errors)
                _runtime_log('runtime error #' + str(consecutive_errors) + ': ' + str(exc) + ' | backoff=' + str(backoff) + 's')
                if not _sleep_if_not_stopped(stop_event, backoff):
                    return {"status": "stopped"}
    finally:
        if binance_signal_service is not None:
            binance_signal_service.close()
