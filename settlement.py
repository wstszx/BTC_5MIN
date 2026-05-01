from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from clob_adapter import build_verified_pending_live_trade_plan
from config import AppConfig
from models import LiveStrategyState, MarketWindow, PendingPaperTrade, SessionState, TradePlan, TradeRecord
from polymarket_api import normalize_outcome_label, parse_iso_datetime, parse_outcome_prices
from risk_and_sizing import apply_round_outcome, build_trade_plan
from trade_log import append_trade_log
from utils import _runtime_log


def clear_pending_live_trade(strategy_state: LiveStrategyState) -> None:
    strategy_state.pending_live_slug = None
    strategy_state.pending_live_side = None
    strategy_state.pending_live_price = None
    strategy_state.pending_live_order_size = None
    strategy_state.pending_live_order_cost = None
    strategy_state.pending_live_expected_profit = None
    strategy_state.pending_live_order_id = None
    strategy_state.pending_live_end_time = None


def timeframe_duration_seconds(timeframe: str | None) -> int:
    return 900 if str(timeframe or "").strip().lower() == "15m" else 300


def append_settled_live_trade_log(
    *,
    log_path: Path,
    cfg: AppConfig,
    strategy_id: int,
    prior_state: LiveStrategyState,
    updated_state: LiveStrategyState,
    settlement_status: dict[str, Any] | None,
) -> None:
    if not settlement_status or settlement_status.get("status") != "settled":
        return

    end_time = parse_iso_datetime(prior_state.pending_live_end_time) or datetime.now(timezone.utc)
    start_time = end_time - timedelta(seconds=timeframe_duration_seconds(getattr(cfg, "market_timeframe", None)))
    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=datetime.now(timezone.utc),
            mode="live",
            round_index=max(0, prior_state.round_index - 1),
            strategy=strategy_id,
            entry_timing=cfg.entry_timing,
            event_slug=str(prior_state.pending_live_slug or settlement_status.get("slug") or ""),
            start_time=start_time,
            end_time=end_time,
            side=str(settlement_status.get("side") or prior_state.pending_live_side or ""),
            price=settlement_status.get("price") if settlement_status.get("price") is not None else prior_state.pending_live_price,
            order_size=float(
                settlement_status.get("order_size")
                if settlement_status.get("order_size") is not None
                else (prior_state.pending_live_order_size or 0.0)
            ),
            order_cost=float(
                settlement_status.get("order_cost")
                if settlement_status.get("order_cost") is not None
                else (prior_state.pending_live_order_cost or 0.0)
            ),
            expected_profit=float(
                settlement_status.get("expected_profit")
                if settlement_status.get("expected_profit") is not None
                else (prior_state.pending_live_expected_profit or 0.0)
            ),
            result=str(settlement_status.get("result") or ""),
            trade_pnl=float(settlement_status.get("trade_pnl") or 0.0),
            cash_pnl=updated_state.cash_pnl,
            recovery_loss=updated_state.recovery_loss,
            consecutive_losses=updated_state.consecutive_losses,
        ),
    )


def build_pending_live_trade_plan(state: SessionState) -> TradePlan:
    if state.pending_live_side not in {"UP", "DOWN"}:
        raise RuntimeError("Pending live trade is missing a valid side.")
    if state.pending_live_price is None:
        raise RuntimeError("Pending live trade is missing entry price.")
    if state.pending_live_order_size is None or state.pending_live_order_size <= 0:
        raise RuntimeError("Pending live trade is missing order size.")
    if state.pending_live_order_cost is None or state.pending_live_order_cost <= 0:
        raise RuntimeError("Pending live trade is missing order cost.")
    if state.pending_live_expected_profit is None:
        raise RuntimeError("Pending live trade is missing expected profit.")

    return TradePlan(
        True,
        side=state.pending_live_side,
        price=state.pending_live_price,
        order_size=state.pending_live_order_size,
        order_cost=state.pending_live_order_cost,
        expected_profit=state.pending_live_expected_profit,
    )


def cached_ws_market_result(market_client: Any, market: dict[str, Any]) -> str:
    get_resolution = getattr(market_client, "get_ws_market_resolution", None)
    if not callable(get_resolution):
        return ""
    try:
        resolution = get_resolution(market)
    except Exception:
        return ""
    if not isinstance(resolution, dict):
        return ""
    outcome = normalize_outcome_label(str(resolution.get("winning_outcome") or ""))
    return outcome if outcome in {"UP", "DOWN"} else ""


def settle_pending_live_trade_if_needed(
    *,
    market_client: Any,
    clob_client: Any | None,
    strategy_state: LiveStrategyState,
    now: datetime,
    pending_plan_resolver=build_verified_pending_live_trade_plan,
) -> tuple[LiveStrategyState, dict[str, Any] | None, bool]:
    if not strategy_state.pending_live_slug:
        return strategy_state, None, False

    end_time = parse_iso_datetime(strategy_state.pending_live_end_time)
    if end_time is None:
        raise RuntimeError("Pending live trade is missing round end time.")

    if now < end_time:
        return (
            strategy_state,
            {
                "status": "pending_settlement",
                "slug": strategy_state.pending_live_slug,
                "side": strategy_state.pending_live_side,
                "skip_reason": "round_in_progress",
                "pending_end_time": strategy_state.pending_live_end_time,
            },
            False,
        )

    plan = pending_plan_resolver(strategy_state, clob_client=clob_client)
    if plan is None:
        return (
            strategy_state,
            {
                "status": "pending_settlement",
                "slug": strategy_state.pending_live_slug,
                "side": strategy_state.pending_live_side,
                "skip_reason": "awaiting_fill_confirmation",
                "pending_end_time": strategy_state.pending_live_end_time,
                "order_id": strategy_state.pending_live_order_id,
            },
            False,
        )

    event = market_client.get_event_by_slug(strategy_state.pending_live_slug)
    metadata = event.get("eventMetadata") or {}
    market = (event.get("markets") or [{}])[0]
    if metadata.get("priceToBeat") is not None and metadata.get("finalPrice") is not None:
        result = "UP" if float(metadata["finalPrice"]) >= float(metadata["priceToBeat"]) else "DOWN"
    else:
        cached_result = cached_ws_market_result(market_client, market)
        if cached_result:
            result = cached_result
        else:
            prices = parse_outcome_prices(market.get("outcomePrices"), market.get("outcomes"))
            up_price = prices.get("UP")
            down_price = prices.get("DOWN")
            is_closed = bool(event.get("closed") or market.get("closed"))
            if (
                not is_closed
                or up_price is None
                or down_price is None
                or {up_price, down_price} != {0.0, 1.0}
            ):
                return (
                    strategy_state,
                    {
                        "status": "pending_settlement",
                        "slug": strategy_state.pending_live_slug,
                        "side": strategy_state.pending_live_side,
                        "skip_reason": "round_unresolved",
                        "pending_end_time": strategy_state.pending_live_end_time,
                    },
                    False,
                )
            result = "UP" if up_price > down_price else "DOWN"
    updated_state = apply_round_outcome(strategy_state, plan, won=(result == plan.side))
    trade_pnl = updated_state.cash_pnl - strategy_state.cash_pnl
    clear_pending_live_trade(updated_state)
    return (
        updated_state,
        {
            "status": "settled",
            "slug": strategy_state.pending_live_slug,
            "side": plan.side,
            "price": plan.price,
            "order_size": plan.order_size,
            "order_cost": plan.order_cost,
            "expected_profit": plan.expected_profit,
            "result": result,
            "trade_pnl": trade_pnl,
        },
        True,
    )


def build_frozen_pending_paper_plan(item: PendingPaperTrade) -> TradePlan:
    return TradePlan(
        True,
        side=item.side,
        price=item.price,
        order_size=item.order_size,
        order_cost=item.order_cost,
        expected_profit=item.expected_profit,
    )


def settle_pending_paper_trade(
    *,
    client: Any,
    state: SessionState,
    item: PendingPaperTrade,
) -> tuple[SessionState, str, float]:
    event = client.get_event_by_slug(item.event_slug)
    metadata = event.get("eventMetadata") or {}
    market = (event.get("markets") or [{}])[0]
    if metadata.get("priceToBeat") is not None and metadata.get("finalPrice") is not None:
        result = "UP" if float(metadata["finalPrice"]) >= float(metadata["priceToBeat"]) else "DOWN"
    else:
        cached_result = cached_ws_market_result(client, market)
        if cached_result:
            result = cached_result
        else:
            prices = parse_outcome_prices(market.get("outcomePrices"), market.get("outcomes"))
            up_price = prices.get("UP")
            down_price = prices.get("DOWN")
            is_closed = bool(event.get("closed") or market.get("closed"))
            if (
                not is_closed
                or up_price is None
                or down_price is None
                or {up_price, down_price} != {0.0, 1.0}
            ):
                raise RuntimeError(f"Round {item.event_slug} is not resolved yet.")
            result = "UP" if up_price > down_price else "DOWN"
    plan = build_frozen_pending_paper_plan(item)
    updated_state = apply_round_outcome(state, plan, won=(result == item.side))
    trade_pnl = updated_state.cash_pnl - state.cash_pnl
    return updated_state, result, trade_pnl


def settle_pending_paper_trades(
    *,
    client: Any,
    state: SessionState,
    log_path: Path,
) -> tuple[SessionState, bool]:
    if not state.pending_paper_trades:
        return state, False

    updated_state = state
    changed = False
    remaining: list[PendingPaperTrade] = []
    for item in updated_state.pending_paper_trades:
        try:
            next_state, result, trade_pnl = settle_pending_paper_trade(
                client=client,
                state=updated_state,
                item=item,
            )
        except RuntimeError as exc:
            if "is not resolved yet" in str(exc):
                _runtime_log("round=" + item.event_slug + " pending resolution")
                remaining.append(item)
                continue
            raise

        updated_state = next_state
        append_trade_log(
            log_path,
            TradeRecord(
                timestamp=datetime.now(timezone.utc),
                mode="paper",
                experiment_id=item.experiment_id,
                round_index=item.round_index,
                strategy=item.strategy,
                entry_timing=item.entry_timing,
                event_slug=item.event_slug,
                start_time=parse_iso_datetime(item.start_time) or datetime.now(timezone.utc),
                end_time=parse_iso_datetime(item.end_time) or datetime.now(timezone.utc),
                side=item.side,
                price=item.price,
                order_size=item.order_size,
                order_cost=item.order_cost,
                expected_profit=item.expected_profit,
                result=result,
                trade_pnl=trade_pnl,
                cash_pnl=updated_state.cash_pnl,
                recovery_loss=updated_state.recovery_loss,
                consecutive_losses=updated_state.consecutive_losses,
                signal_open_up_price=item.signal_open_up_price,
                signal_current_up_price=item.signal_current_up_price,
                signal_threshold=item.signal_threshold,
                signal_delta=item.signal_delta,
                signal_locked=item.signal_locked,
                signal_reason=item.signal_reason,
            ),
        )
        _runtime_log(
            "round=" + item.event_slug
            + " settled result=" + result
            + " trade_pnl=" + f"{trade_pnl:.4f}"
            + " total_cash_pnl=" + f"{updated_state.cash_pnl:.4f}"
            + " consecutive_losses=" + str(updated_state.consecutive_losses)
        )
        changed = True

    updated_state.pending_paper_trades = remaining
    return updated_state, changed


def settle_paper_trade(
    client: Any,
    state: SessionState,
    window: MarketWindow,
    price: float,
    *,
    side: str,
    cfg: AppConfig,
) -> tuple[SessionState, str]:
    event = client.get_event_by_slug(window.slug)
    metadata = event.get("eventMetadata") or {}
    if metadata.get("priceToBeat") is None or metadata.get("finalPrice") is None:
        raise RuntimeError(f"Round {window.slug} is not resolved yet.")

    result = "UP" if float(metadata["finalPrice"]) >= float(metadata["priceToBeat"]) else "DOWN"
    plan = build_trade_plan(
        state=state,
        side=side,
        price=price,
        target_profit=cfg.target_profit,
        min_entry_price=getattr(cfg, "min_entry_price", getattr(cfg, "min_price_threshold", None)),
        max_entry_price=getattr(cfg, "max_entry_price", cfg.max_price_threshold),
        min_price_threshold=getattr(cfg, "min_price_threshold", None),
        max_price_threshold=cfg.max_price_threshold,
        min_stake=getattr(cfg, "min_stake", None),
        max_stake=cfg.max_stake,
        max_consecutive_losses=cfg.max_consecutive_losses,
        bet_sizing_mode=cfg.bet_sizing_mode,
        base_order_cost=cfg.base_order_cost,
    )
    updated_state = apply_round_outcome(state, plan, won=(result == side))
    return updated_state, result
