from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import AppConfig
from models import MarketWindow, SessionState, TradePlan
from paper_pending import queue_pending_paper_trade
from strategy_decision import SideDecision
from trader import _queue_pending_paper_trade


def test_paper_pending_queues_trade_once_and_trader_reexports_helper():
    state = SessionState(round_index=3)
    start = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="evt",
        market_id="mkt",
        slug="btc-updown-5m-pending",
        title="BTC",
        start_time=start,
        end_time=start + timedelta(minutes=5),
    )
    plan = TradePlan(True, "UP", price=0.51, order_size=2.0, order_cost=1.02, expected_profit=0.98)
    decision = SideDecision(side="UP", signal_open_up_price=0.5, signal_delta=0.01, signal_locked=True)
    cfg = AppConfig(strategy_id=5, entry_timing="OPEN")

    assert queue_pending_paper_trade(
        state=state,
        window=window,
        plan=plan,
        side="UP",
        cfg=cfg,
        side_decision=decision,
        experiment_id="strategy-5",
    )
    assert not queue_pending_paper_trade(
        state=state,
        window=window,
        plan=plan,
        side="UP",
        cfg=cfg,
        side_decision=decision,
        experiment_id="strategy-5",
    )

    [pending] = state.pending_paper_trades
    assert pending.event_slug == "btc-updown-5m-pending"
    assert pending.strategy == 5
    assert pending.signal_locked is True
    assert pending.experiment_id == "strategy-5"
    assert _queue_pending_paper_trade is queue_pending_paper_trade


def test_paper_pending_preserves_flat_sizing_recovery_policy():
    state = SessionState(round_index=3)
    start = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="evt",
        market_id="mkt",
        slug="btc-updown-5m-flat-pending",
        title="BTC",
        start_time=start,
        end_time=start + timedelta(minutes=5),
    )
    plan = TradePlan(
        True,
        "UP",
        price=0.51,
        order_size=2.0,
        order_cost=1.02,
        expected_profit=0.98,
        tracks_recovery_loss=False,
    )
    decision = SideDecision(side="UP")
    cfg = AppConfig(strategy_id=7, entry_timing="OPEN", bet_sizing_mode="FLAT_BASE_COST")

    assert queue_pending_paper_trade(
        state=state,
        window=window,
        plan=plan,
        side="UP",
        cfg=cfg,
        side_decision=decision,
        experiment_id="strategy-7",
    )

    assert state.pending_paper_trades[0].tracks_recovery_loss is False
