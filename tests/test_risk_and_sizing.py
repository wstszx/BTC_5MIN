from pathlib import Path

import pytest

from models import SessionState
from risk_and_sizing import apply_round_outcome, build_trade_plan, reset_after_stop_loss
from trader import load_session_state, save_session_state


def test_build_trade_plan_defaults_to_flat_base_cost():
    state = SessionState(recovery_loss=3.0)
    plan = build_trade_plan(
        state=state,
        side="UP",
        price=0.5,
        max_price_threshold=0.65,
        max_stake=10,
        max_consecutive_losses=8,
        base_order_cost=1.2,
    )

    assert plan.should_trade is True
    assert round(plan.order_size, 4) == 2.4
    assert round(plan.order_cost, 4) == 1.2
    assert plan.tracks_recovery_loss is False


def test_build_trade_plan_applies_order_cost_multiplier_after_base_sizing():
    state = SessionState()
    plan = build_trade_plan(
        state=state,
        side="UP",
        price=0.5,
        max_price_threshold=0.65,
        max_stake=10,
        max_consecutive_losses=8,
        order_cost_multiplier=0.5,
    )

    assert plan.should_trade is True
    assert plan.order_cost_multiplier == 0.5
    assert round(plan.order_cost, 4) == 0.5
    assert round(plan.order_size, 4) == 1.0
    assert round(plan.expected_profit, 4) == 0.5


def test_build_trade_plan_checks_min_stake_after_order_cost_multiplier():
    state = SessionState()
    plan = build_trade_plan(
        state=state,
        side="UP",
        price=0.5,
        max_price_threshold=0.65,
        min_stake=0.6,
        max_stake=10,
        max_consecutive_losses=8,
        order_cost_multiplier=0.5,
    )

    assert plan.should_trade is False
    assert plan.order_cost_multiplier == 0.5
    assert plan.skip_reason == "order_cost_below_min_stake"


def test_apply_round_outcome_win_clears_legacy_recovery_loss_without_recovering_it():
    state = SessionState(recovery_loss=2.0, consecutive_losses=2)
    plan = build_trade_plan(
        state=state,
        side="UP",
        price=0.5,
        max_price_threshold=0.65,
        max_stake=10,
        max_consecutive_losses=8,
        order_cost_multiplier=0.5,
    )

    updated = apply_round_outcome(state, plan, won=True)

    assert plan.expected_profit == 0.5
    assert updated.cash_pnl == 0.5
    assert updated.recovery_loss == 0.0
    assert updated.consecutive_losses == 0


def test_apply_round_outcome_loss_does_not_update_recovery_pool():
    state = SessionState()
    plan = build_trade_plan(
        state=state,
        side="DOWN",
        price=0.5,
        max_price_threshold=0.65,
        max_stake=10,
        max_consecutive_losses=8,
    )
    updated = apply_round_outcome(state, plan, won=False)
    assert round(updated.recovery_loss, 4) == 0.0
    assert updated.consecutive_losses == 1


def test_build_trade_plan_with_recovery_loss_ignores_recovery_formula():
    state = SessionState(recovery_loss=3.1)
    plan = build_trade_plan(
        state=state,
        side="UP",
        price=0.62,
        max_price_threshold=0.65,
        max_stake=10,
        max_consecutive_losses=8,
    )
    assert round(plan.order_size, 4) == 1.6129
    assert round(plan.order_cost, 4) == 1.0
    assert plan.tracks_recovery_loss is False


def test_build_trade_plan_skips_when_price_above_threshold():
    state = SessionState()
    plan = build_trade_plan(
        state=state,
        side="UP",
        price=0.7,
        max_price_threshold=0.65,
        max_stake=10,
        max_consecutive_losses=8,
    )
    assert plan.should_trade is False
    assert plan.skip_reason == "price_above_threshold"


def test_build_trade_plan_checks_max_entry_price_against_raw_price():
    state = SessionState()
    plan = build_trade_plan(
        state=state,
        side="UP",
        price=0.54,
        max_entry_price=0.54,
        max_stake=10,
        max_consecutive_losses=8,
    )

    assert plan.should_trade is True
    assert plan.price == pytest.approx(0.54)
    assert plan.max_entry_price == pytest.approx(0.54)


def test_build_trade_plan_skips_when_order_cost_exceeds_max_stake():
    state = SessionState()
    plan = build_trade_plan(
        state=state,
        side="UP",
        price=0.5,
        max_price_threshold=0.65,
        max_stake=0.4,
        max_consecutive_losses=8,
    )
    assert plan.should_trade is False
    assert plan.skip_reason == "order_cost_above_max_stake"


def test_build_trade_plan_skips_when_order_cost_below_min_stake():
    state = SessionState()
    plan = build_trade_plan(
        state=state,
        side="UP",
        price=0.5,
        max_price_threshold=0.65,
        min_stake=1.1,
        max_stake=10,
        max_consecutive_losses=8,
    )
    assert plan.should_trade is False
    assert plan.skip_reason == "order_cost_below_min_stake"


def test_build_trade_plan_does_not_skip_when_daily_realized_pnl_is_negative():
    state = SessionState(daily_realized_pnl=-20)
    plan = build_trade_plan(
        state=state,
        side="DOWN",
        price=0.5,
        max_price_threshold=0.65,
        max_stake=10,
        max_consecutive_losses=8,
    )
    assert plan.should_trade is True
    assert plan.skip_reason is None


def test_build_trade_plan_allows_unlimited_max_stake_when_value_is_none():
    state = SessionState(recovery_loss=3.1)
    plan = build_trade_plan(
        state=state,
        side="UP",
        price=0.62,
        max_price_threshold=0.65,
        max_stake=None,
        max_consecutive_losses=8,
    )
    assert plan.should_trade is True
    assert plan.skip_reason is None


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
    assert restored.recovery_loss == 0.0


def test_fixed_base_cost_ignores_and_clears_recovery_loss():
    state = SessionState(recovery_loss=3.0, consecutive_losses=2)
    plan = build_trade_plan(
        state=state,
        side="UP",
        price=0.5,
        max_price_threshold=0.65,
        max_stake=100,
        max_consecutive_losses=8,
        base_order_cost=1.0,
    )

    assert plan.should_trade is True
    assert round(plan.order_cost, 4) == 1.0
    assert round(plan.order_size, 4) == 2.0
    assert round(plan.expected_profit, 4) == 1.0
    assert plan.tracks_recovery_loss is False

    after_loss = apply_round_outcome(state, plan, won=False)
    assert after_loss.cash_pnl == -1.0
    assert after_loss.recovery_loss == 0.0
    assert after_loss.consecutive_losses == 3


def test_fixed_base_cost_uses_same_cost_after_loss():
    state = SessionState(recovery_loss=1.2, consecutive_losses=1)
    plan = build_trade_plan(
        state=state,
        side="DOWN",
        price=0.54,
        max_price_threshold=0.56,
        max_stake=100,
        max_consecutive_losses=8,
        base_order_cost=1.2,
    )

    assert plan.should_trade is True
    assert plan.order_cost == 1.2
