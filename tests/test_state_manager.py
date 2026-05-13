from __future__ import annotations

import json
from pathlib import Path

from models import PendingPaperTrade, SessionState, Strategy9SignalSample
from state_manager import load_session_state, save_session_state
from trader import load_session_state as trader_load_session_state
from trader import save_session_state as trader_save_session_state


def test_state_manager_loads_strategy_maps_without_trader_runtime_dependency(tmp_path: Path):
    state_path = tmp_path / "session_state.json"
    state_path.write_text(
        json.dumps(
            {
                "paper_strategies": {
                    "6": {
                        "round_index": 4,
                        "cash_pnl": 1.25,
                        "pending_paper_trades": [
                            {
                                "round_index": 4,
                                "event_slug": "btc-updown-5m-queued",
                                "start_time": "2026-04-30T01:00:00+00:00",
                                "end_time": "2026-04-30T01:05:00+00:00",
                                "side": "UP",
                                "price": 0.52,
                                "order_size": 2.0,
                                "order_cost": 1.04,
                                "expected_profit": 0.96,
                                "strategy": 6,
                                "entry_timing": "open",
                            }
                        ],
                    }
                },
                "live_strategies": {
                    "7": {
                        "round_index": 2,
                        "cash_pnl": -0.5,
                        "pending_live_slug": "btc-updown-5m-live",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    state = load_session_state(
        state_path,
        effective_paper_strategy_ids=[6, 8],
        effective_live_strategy_ids=[7, 3],
    )

    assert state.paper_strategies[6].pending_paper_trades == [
        PendingPaperTrade(
            round_index=4,
            event_slug="btc-updown-5m-queued",
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
    ]
    assert state.paper_strategies[8].round_index == 0
    assert state.live_strategies[7].pending_live_slug == "btc-updown-5m-live"
    assert state.live_strategies[3].pending_live_slug is None


def test_trader_reexports_session_state_persistence_helpers():
    assert trader_load_session_state is load_session_state
    assert trader_save_session_state is save_session_state


def test_state_manager_save_session_state_roundtrips(tmp_path: Path):
    state_path = tmp_path / "session_state.json"

    save_session_state(
        state_path,
        SessionState(
            cash_pnl=1.25,
            strategy9_signal_samples=[
                Strategy9SignalSample(
                    observed_at="2026-04-30T01:00:00+00:00",
                    ofi_score=0.7,
                    momentum_delta=0.03,
                    current_up_price=0.53,
                )
            ],
        ),
    )

    loaded = load_session_state(state_path)

    assert loaded.cash_pnl == 1.25
    assert loaded.strategy9_signal_samples[0].ofi_score == 0.7
