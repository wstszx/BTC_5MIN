from __future__ import annotations

from datetime import datetime, timezone

from config import AppConfig
from models import MarketWindow, PendingPaperTrade, SessionState, TradePlan
from strategy_decision import SideDecision


def pending_paper_trade_exists(state: SessionState, slug: str) -> bool:
    return any(item.event_slug == slug for item in state.pending_paper_trades)


def build_pending_paper_trade(
    *,
    state: SessionState,
    window: MarketWindow,
    plan: TradePlan,
    side: str,
    cfg: AppConfig,
    side_decision: SideDecision,
    experiment_id: str | None,
) -> PendingPaperTrade:
    return PendingPaperTrade(
        round_index=state.round_index,
        event_slug=window.slug,
        start_time=window.start_time.isoformat(),
        end_time=window.end_time.isoformat(),
        side=side,
        price=float(plan.price or 0.0),
        order_size=plan.order_size,
        order_cost=plan.order_cost,
        expected_profit=plan.expected_profit,
        strategy=cfg.strategy_id,
        entry_timing=cfg.entry_timing,
        signal_open_up_price=side_decision.signal_open_up_price,
        signal_current_up_price=side_decision.signal_current_up_price,
        signal_threshold=side_decision.signal_threshold,
        signal_delta=side_decision.signal_delta,
        signal_locked=side_decision.signal_locked,
        signal_reason=side_decision.reason,
        signal_max_entry_price=side_decision.max_entry_price,
        sizing_multiplier=plan.order_cost_multiplier,
        queued_at=datetime.now(timezone.utc).isoformat(),
        experiment_id=experiment_id,
        tracks_recovery_loss=plan.tracks_recovery_loss,
    )


def queue_pending_paper_trade(
    *,
    state: SessionState,
    window: MarketWindow,
    plan: TradePlan,
    side: str,
    cfg: AppConfig,
    side_decision: SideDecision,
    experiment_id: str | None,
) -> bool:
    if pending_paper_trade_exists(state, window.slug):
        return False
    state.pending_paper_trades.append(
        build_pending_paper_trade(
            state=state,
            window=window,
            plan=plan,
            side=side,
            cfg=cfg,
            side_decision=side_decision,
            experiment_id=experiment_id,
        )
    )
    return True
