from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from models import LiveStrategyState, MarketWindow, PendingLiveTrade, SessionState, TradePlan


LivePendingState = LiveStrategyState | SessionState


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clear_legacy_pending_live_trade(state: LivePendingState) -> None:
    state.pending_live_slug = None
    state.pending_live_side = None
    state.pending_live_price = None
    state.pending_live_order_size = None
    state.pending_live_order_cost = None
    state.pending_live_expected_profit = None
    state.pending_live_order_id = None
    state.pending_live_end_time = None
    state.pending_live_tracks_recovery_loss = True


def pending_live_trade_from_legacy(
    state: LivePendingState,
    *,
    strategy_id: int,
    entry_timing: str = "",
) -> PendingLiveTrade | None:
    if not state.pending_live_slug:
        return None
    if not state.pending_live_end_time:
        return None
    return PendingLiveTrade(
        round_index=max(0, int(getattr(state, "round_index", 0) or 0) - 1),
        event_slug=str(state.pending_live_slug),
        start_time=None,
        end_time=str(state.pending_live_end_time),
        side=str(state.pending_live_side or ""),
        price=_to_float(state.pending_live_price),
        order_size=_to_float(state.pending_live_order_size),
        order_cost=_to_float(state.pending_live_order_cost),
        expected_profit=_to_float(state.pending_live_expected_profit),
        strategy=strategy_id,
        entry_timing=entry_timing,
        order_id=state.pending_live_order_id,
        queued_at=None,
        tracks_recovery_loss=bool(state.pending_live_tracks_recovery_loss),
    )


def apply_pending_live_trade_to_legacy(state: LivePendingState, item: PendingLiveTrade) -> None:
    state.pending_live_slug = item.event_slug
    state.pending_live_side = item.side
    state.pending_live_price = item.price
    state.pending_live_order_size = item.order_size
    state.pending_live_order_cost = item.order_cost
    state.pending_live_expected_profit = item.expected_profit
    state.pending_live_order_id = item.order_id
    state.pending_live_end_time = item.end_time
    state.pending_live_tracks_recovery_loss = item.tracks_recovery_loss


def pending_live_trade_exists(
    state: LivePendingState,
    *,
    event_slug: str,
    order_id: str | None = None,
) -> bool:
    for item in state.pending_live_trades:
        if item.event_slug != event_slug:
            continue
        if order_id is not None and item.order_id != order_id:
            continue
        return True
    return False


def sync_pending_live_queue_from_legacy(
    state: LivePendingState,
    *,
    strategy_id: int,
    entry_timing: str = "",
) -> None:
    item = pending_live_trade_from_legacy(
        state,
        strategy_id=strategy_id,
        entry_timing=entry_timing,
    )
    if item is None:
        return
    if pending_live_trade_exists(state, event_slug=item.event_slug, order_id=item.order_id):
        return
    state.pending_live_trades.append(item)


def sync_legacy_pending_live_from_queue(state: LivePendingState) -> None:
    if state.pending_live_trades:
        apply_pending_live_trade_to_legacy(state, state.pending_live_trades[-1])
        return
    clear_legacy_pending_live_trade(state)


def normalize_pending_live_trades(
    state: LivePendingState,
    *,
    strategy_id: int,
    entry_timing: str = "",
) -> None:
    sync_pending_live_queue_from_legacy(
        state,
        strategy_id=strategy_id,
        entry_timing=entry_timing,
    )
    if state.pending_live_trades:
        sync_legacy_pending_live_from_queue(state)


def queue_pending_live_trade(
    state: LivePendingState,
    *,
    window: MarketWindow,
    plan: TradePlan,
    side: str,
    strategy_id: int,
    entry_timing: str,
    order_id: str | None,
) -> bool:
    normalize_pending_live_trades(
        state,
        strategy_id=strategy_id,
        entry_timing=entry_timing,
    )
    if pending_live_trade_exists(state, event_slug=window.slug, order_id=order_id):
        return False
    item = PendingLiveTrade(
        round_index=int(getattr(state, "round_index", 0) or 0),
        event_slug=window.slug,
        start_time=window.start_time.isoformat(),
        end_time=window.end_time.isoformat(),
        side=side,
        price=float(plan.price or 0.0),
        order_size=float(plan.order_size),
        order_cost=float(plan.order_cost),
        expected_profit=float(plan.expected_profit),
        strategy=strategy_id,
        entry_timing=entry_timing,
        order_id=order_id,
        queued_at=datetime.now(timezone.utc).isoformat(),
        tracks_recovery_loss=plan.tracks_recovery_loss,
    )
    state.pending_live_trades.append(item)
    apply_pending_live_trade_to_legacy(state, item)
    return True
