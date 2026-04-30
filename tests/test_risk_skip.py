from __future__ import annotations

from config import AppConfig
from models import SessionState
from risk_skip import apply_post_entry_risk_gate_skip, update_max_stake_skip_streak
from trader import _apply_post_entry_risk_gate_skip, _update_max_stake_skip_streak


def test_risk_skip_updates_alert_threshold_and_trader_reexports_helpers():
    state = SessionState()

    assert update_max_stake_skip_streak(state, skip_reason="order_cost_above_max_stake", threshold=2) is False
    assert update_max_stake_skip_streak(state, skip_reason="order_cost_above_max_stake", threshold=2) is True
    assert update_max_stake_skip_streak(state, skip_reason="invalid_price", threshold=2) is False
    assert state.consecutive_max_stake_skips == 0
    assert _update_max_stake_skip_streak is update_max_stake_skip_streak


def test_risk_skip_resets_after_repeated_max_stake_skips_and_advances_round():
    state = SessionState(round_index=4, recovery_loss=5.0, consecutive_losses=2, consecutive_max_stake_skips=2)
    cfg = AppConfig(max_consecutive_losses=3, max_stake_skip_alert_threshold=3)

    updated_state, should_alert, should_reset, skip_streak = apply_post_entry_risk_gate_skip(
        state,
        skip_reason="order_cost_above_max_stake",
        cfg=cfg,
        stop_loss_triggered=False,
    )

    assert should_alert is True
    assert should_reset is True
    assert skip_streak == 3
    assert updated_state.round_index == 5
    assert updated_state.recovery_loss == 0.0
    assert updated_state.consecutive_losses == 0
    assert updated_state.consecutive_max_stake_skips == 0
    assert updated_state.stop_loss_count == 1
    assert _apply_post_entry_risk_gate_skip is apply_post_entry_risk_gate_skip
