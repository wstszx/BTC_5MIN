from __future__ import annotations

from models import LiveStrategyState, PaperStrategyState, PendingPaperTrade, SessionState
from strategy_state_sync import (
    ensure_live_strategy_state_map,
    ensure_paper_strategy_state_map,
    managed_live_strategy_ids,
    paper_strategy_state_to_session_state,
    session_state_to_paper_strategy_state,
    strategy_has_pending_live_trade,
    sync_current_live_strategy_state,
    sync_legacy_live_state_fields,
    sync_legacy_paper_state_fields,
)
from trader import _ensure_live_strategy_state_map
from trader import _ensure_paper_strategy_state_map


def test_strategy_state_sync_wraps_legacy_live_state_and_preserves_pending_priority():
    state = SessionState(
        round_index=4,
        cash_pnl=1.25,
        pending_live_slug="btc-updown-5m-live",
        pending_live_order_id="oid-live",
    )

    ensure_live_strategy_state_map(state, [7, 3])

    assert state.live_strategies[7].round_index == 4
    assert state.live_strategies[7].pending_live_slug == "btc-updown-5m-live"
    assert state.live_strategies[3].pending_live_slug is None
    assert managed_live_strategy_ids([3], state) == [3, 7]
    assert strategy_has_pending_live_trade(state.live_strategies[7]) is True

    sync_legacy_live_state_fields(state, [3, 7])

    assert state.pending_live_slug == "btc-updown-5m-live"
    assert state.pending_live_order_id == "oid-live"


def test_strategy_state_sync_roundtrips_paper_strategy_state_with_base_live_fields():
    pending = PendingPaperTrade(
        round_index=2,
        event_slug="btc-updown-5m-paper",
        start_time="2026-04-30T01:00:00+00:00",
        end_time="2026-04-30T01:05:00+00:00",
        side="UP",
        price=0.52,
        order_size=2.0,
        order_cost=1.04,
        expected_profit=0.96,
        strategy=6,
        entry_timing="open",
    )
    paper_state = PaperStrategyState(
        round_index=2,
        cash_pnl=3.5,
        pending_paper_trades=[pending],
        experiment_id="exp-6",
    )
    base_state = SessionState(pending_live_slug="btc-updown-5m-live", pending_live_tracks_recovery_loss=False)

    session_state = paper_strategy_state_to_session_state(paper_state, base_state)
    restored_paper_state = session_state_to_paper_strategy_state(session_state)

    assert session_state.pending_live_slug == "btc-updown-5m-live"
    assert session_state.pending_live_tracks_recovery_loss is False
    assert session_state.pending_paper_trades == [pending]
    assert restored_paper_state.round_index == 2
    assert restored_paper_state.cash_pnl == 3.5
    assert restored_paper_state.pending_paper_trades == [pending]


def test_strategy_state_sync_updates_legacy_paper_and_current_live_fields():
    state = SessionState(round_index=5, cash_pnl=9.0, pending_live_slug="btc-updown-5m-live")

    ensure_paper_strategy_state_map(state, [2])
    state.paper_strategies[2].round_index = 8
    state.paper_strategies[2].cash_pnl = 4.25
    sync_legacy_paper_state_fields(state, [2])

    assert state.round_index == 8
    assert state.cash_pnl == 4.25

    state.live_strategies = {2: LiveStrategyState()}
    sync_current_live_strategy_state(state, 2)

    assert state.live_strategies[2].round_index == 8
    assert state.live_strategies[2].pending_live_slug == "btc-updown-5m-live"


def test_trader_reexports_strategy_state_sync_helpers():
    assert _ensure_live_strategy_state_map is ensure_live_strategy_state_map
    assert _ensure_paper_strategy_state_map is ensure_paper_strategy_state_map
