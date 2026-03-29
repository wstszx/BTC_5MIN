from pathlib import Path

from models import SessionState
from risk_and_sizing import apply_round_outcome, build_trade_plan, reset_after_stop_loss
from trader import load_session_state, save_session_state


def test_build_trade_plan_without_loss_uses_target_profit_formula():
    state = SessionState()
    plan = build_trade_plan(
        state=state,
        side="UP",
        price=0.5,
        target_profit=0.5,
        max_price_threshold=0.65,
        max_stake=10,
        daily_loss_cap=20,
        max_consecutive_losses=8,
    )
    assert round(plan.order_size, 4) == 1.0
    assert round(plan.order_cost, 4) == 0.5


def test_apply_round_outcome_loss_updates_recovery_pool():
    state = SessionState()
    plan = build_trade_plan(
        state=state,
        side="DOWN",
        price=0.5,
        target_profit=0.5,
        max_price_threshold=0.65,
        max_stake=10,
        daily_loss_cap=20,
        max_consecutive_losses=8,
    )
    updated = apply_round_outcome(state, plan, won=False)
    assert round(updated.recovery_loss, 4) == 0.5
    assert updated.consecutive_losses == 1


def test_build_trade_plan_with_recovery_loss_uses_recovery_formula():
    state = SessionState(recovery_loss=3.1)
    plan = build_trade_plan(
        state=state,
        side="UP",
        price=0.62,
        target_profit=0.5,
        max_price_threshold=0.65,
        max_stake=10,
        daily_loss_cap=20,
        max_consecutive_losses=8,
    )
    assert round(plan.order_size, 4) == 9.4737
    assert round(plan.order_cost, 4) == 5.8737


def test_build_trade_plan_skips_when_price_above_threshold():
    state = SessionState()
    plan = build_trade_plan(
        state=state,
        side="UP",
        price=0.7,
        target_profit=0.5,
        max_price_threshold=0.65,
        max_stake=10,
        daily_loss_cap=20,
        max_consecutive_losses=8,
    )
    assert plan.should_trade is False
    assert plan.skip_reason == "price_above_threshold"


def test_build_trade_plan_skips_when_order_cost_exceeds_max_stake():
    state = SessionState()
    plan = build_trade_plan(
        state=state,
        side="UP",
        price=0.5,
        target_profit=0.5,
        max_price_threshold=0.65,
        max_stake=0.4,
        daily_loss_cap=20,
        max_consecutive_losses=8,
    )
    assert plan.should_trade is False
    assert plan.skip_reason == "order_cost_above_max_stake"


def test_build_trade_plan_skips_when_daily_loss_cap_is_reached():
    state = SessionState(daily_realized_pnl=-20)
    plan = build_trade_plan(
        state=state,
        side="DOWN",
        price=0.5,
        target_profit=0.5,
        max_price_threshold=0.65,
        max_stake=10,
        daily_loss_cap=20,
        max_consecutive_losses=8,
    )
    assert plan.should_trade is False
    assert plan.skip_reason == "daily_loss_cap_reached"


def test_reset_after_stop_loss_clears_recovery_pool_and_counts_event():
    state = SessionState(recovery_loss=2.75, consecutive_losses=8, stop_loss_count=1)
    updated = reset_after_stop_loss(state)
    assert updated.recovery_loss == 0.0
    assert updated.consecutive_losses == 0
    assert updated.stop_loss_count == 2


def test_session_state_round_trip(tmp_path: Path):
    state = SessionState(round_index=3, recovery_loss=1.25)
    path = tmp_path / "session_state.json"
    save_session_state(path, state)
    restored = load_session_state(path)
    assert restored.round_index == 3
    assert restored.recovery_loss == 1.25
