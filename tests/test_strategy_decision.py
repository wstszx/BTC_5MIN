from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import AppConfig, build_config_from_env_values
from models import MarketQuote, MarketWindow, SessionState
from strategy_decision import SideDecision, resolve_side_from_strategy
from trader import SideDecision as TraderSideDecision
from trader import _resolve_side_from_strategy


def test_strategy_decision_resolves_strategy6_and_trader_reexports_helpers():
    cfg = AppConfig(strategy_id=6, ofi_threshold=0.5, binance_signal_stale_seconds=10.0)
    state = SessionState()
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    quote = MarketQuote(slug="s1", strategy6_ofi_score=0.7, strategy6_signal_at=now)

    decision = resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)

    assert decision.side == "UP"
    assert state.strategy6_last_ofi_score == 0.7
    assert TraderSideDecision is SideDecision
    assert _resolve_side_from_strategy is resolve_side_from_strategy


def test_strategy7_uses_general_max_entry_price():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=now - timedelta(seconds=30),
        end_time=now + timedelta(minutes=15),
    )
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "7",
            "MAX_ENTRY_PRICE": "0.54",
            "STRATEGY7_MAX_ENTRY_PRICE": "0.90",
            "STRATEGY7_OFI_THRESHOLD": "0.5",
            "STRATEGY7_MOMENTUM_THRESHOLD": "0.01",
            "STRATEGY7_MIN_SIGNAL_GAP": "0.0",
            "STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "0",
            "BINANCE_SIGNAL_STALE_SECONDS": "10.0",
        }
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.56,
        up_best_ask=0.56,
        strategy6_ofi_score=0.7,
        strategy6_signal_at=now,
    )

    decision = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        window=window,
        entry_time=now,
        now=now,
    )

    assert decision.side is None
    assert decision.reason == "strategy7_price_too_high"
