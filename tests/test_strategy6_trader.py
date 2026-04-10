from datetime import datetime, timedelta, timezone

from config import AppConfig
from models import MarketQuote, SessionState
from trader import _resolve_side_from_strategy


def test_strategy6_resolves_up_for_strong_ofi_signal():
    cfg = AppConfig(strategy_id=6, ofi_threshold=0.65, binance_signal_stale_seconds=2.0)
    state = SessionState(round_index=0)
    now = datetime.now(timezone.utc)
    quote = MarketQuote(
        slug='s1',
        strategy6_ofi_score=0.8,
        strategy6_signal_at=now,
        up_best_ask=0.55,
        fetched_at=now,
    )

    decision = _resolve_side_from_strategy(cfg=cfg, state=state, slug='s1', quote=quote, now=now)

    assert decision.side == 'UP'
    assert decision.reason is None


def test_strategy6_skips_weak_ofi_signal():
    cfg = AppConfig(strategy_id=6, ofi_threshold=0.65, binance_signal_stale_seconds=2.0)
    state = SessionState(round_index=0)
    now = datetime.now(timezone.utc)
    quote = MarketQuote(
        slug='s1',
        strategy6_ofi_score=0.2,
        strategy6_signal_at=now,
        up_best_ask=0.55,
        fetched_at=now,
    )

    decision = _resolve_side_from_strategy(cfg=cfg, state=state, slug='s1', quote=quote, now=now)

    assert decision.side is None
    assert decision.reason == 'ofi_too_weak'


def test_strategy6_skips_stale_ofi_signal():
    cfg = AppConfig(strategy_id=6, ofi_threshold=0.65, binance_signal_stale_seconds=2.0)
    state = SessionState(round_index=0)
    now = datetime.now(timezone.utc)
    quote = MarketQuote(
        slug='s1',
        strategy6_ofi_score=0.8,
        strategy6_signal_at=now - timedelta(seconds=10),
        up_best_ask=0.55,
        fetched_at=now - timedelta(seconds=10),
    )

    decision = _resolve_side_from_strategy(cfg=cfg, state=state, slug='s1', quote=quote, now=now)

    assert decision.side is None
    assert decision.reason == 'ofi_stale'
