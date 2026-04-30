from __future__ import annotations

from datetime import datetime, timezone

from config import AppConfig
from models import MarketQuote, SessionState
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
