from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import AppConfig
from models import MarketWindow, SessionState
from runtime_helpers import entry_time_for_round, refresh_daily_session_state
from trader import _entry_time_for_round, _refresh_daily_session_state


def test_runtime_helpers_refresh_daily_state_and_trader_reexports_helpers():
    state = SessionState(current_day="2026-04-29", daily_realized_pnl=4.2)
    changed = refresh_daily_session_state(state, datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc))

    assert changed is True
    assert state.current_day == "2026-04-30"
    assert state.daily_realized_pnl == 0.0
    assert _refresh_daily_session_state is refresh_daily_session_state


def test_runtime_helpers_compute_preclose_entry_time_and_trader_reexports_helper():
    window = MarketWindow(
        event_id="evt",
        market_id="mkt",
        slug="btc-updown-5m",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, tzinfo=timezone.utc),
    )
    cfg = AppConfig(entry_timing="PRE_CLOSE", preclose_seconds=20)

    assert entry_time_for_round(cfg, window) == window.end_time - timedelta(seconds=20)
    assert _entry_time_for_round is entry_time_for_round
