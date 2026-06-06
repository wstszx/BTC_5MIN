from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from config import AppConfig, build_config_from_env_values
from models import MarketQuote, MarketWindow, SessionState
from runtime_config import cfg_for_paper_strategy
from runtime_helpers import signal_record_kwargs
from strategy_decision import SideDecision, effective_decision_order_cost_multiplier, resolve_side_from_strategy, strategy7_order_cost_multiplier
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
            "PAPER_STRATEGY_IDS": "7",
            "STRATEGY_7_MAX_ENTRY_PRICE": "0.54",
            "STRATEGY_7_MAX_MOMENTUM_DELTA": "",
            "STRATEGY_7_OFI_THRESHOLD": "0.5",
            "STRATEGY_7_MOMENTUM_THRESHOLD": "0.01",
            "STRATEGY_7_MIN_SIGNAL_GAP": "0.0",
            "STRATEGY_7_CONFIRM_BEFORE_ENTRY_SECONDS": "0",
            "STRATEGY_7_BINANCE_SIGNAL_STALE_SECONDS": "10.0",
        }
    )
    cfg = cfg_for_paper_strategy(cfg, 7)
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


def test_strategy7_max_entry_price_uses_raw_entry_price():
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
            "PAPER_STRATEGY_IDS": "7",
            "STRATEGY_7_MAX_ENTRY_PRICE": "0.54",
            "STRATEGY_7_MAX_MOMENTUM_DELTA": "",
            "STRATEGY_7_OFI_THRESHOLD": "0.5",
            "STRATEGY_7_MOMENTUM_THRESHOLD": "0.01",
            "STRATEGY_7_MIN_SIGNAL_GAP": "0.0",
            "STRATEGY_7_CONFIRM_BEFORE_ENTRY_SECONDS": "0",
            "STRATEGY_7_BINANCE_SIGNAL_STALE_SECONDS": "10.0",
        }
    )
    cfg = cfg_for_paper_strategy(cfg, 7)
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.54,
        up_best_ask=0.54,
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

    assert decision.side == "UP"
    assert decision.signal_current_up_price == pytest.approx(0.54)
    assert decision.reason is None


def test_strategy7_order_cost_multiplier_reduces_high_price_weak_signal():
    cfg = AppConfig(
        strategy_id=7,
        strategy7_dynamic_sizing_enabled=True,
        strategy7_sizing_reference_price=0.50,
        strategy7_sizing_price_step=0.01,
        strategy7_sizing_price_step_reduction=0.10,
        strategy7_sizing_min_multiplier=0.50,
        strategy7_sizing_max_multiplier=1.00,
        strategy7_sizing_strong_signal_gap=0.02,
        strategy7_sizing_strong_signal_boost=0.20,
    )
    decision = SideDecision(
        side="UP",
        candidate_price=0.54,
        signal_delta=0.03,
    )

    multiplier = strategy7_order_cost_multiplier(
        cfg=cfg,
        decision=decision,
        price=0.54,
        ofi_score=0.72,
    )

    assert multiplier == pytest.approx(0.60)


def test_strategy7_order_cost_multiplier_preserves_full_size_for_strong_signal_near_fair_price():
    cfg = AppConfig(
        strategy_id=7,
        strategy7_dynamic_sizing_enabled=True,
        strategy7_sizing_reference_price=0.50,
        strategy7_sizing_price_step=0.01,
        strategy7_sizing_price_step_reduction=0.10,
        strategy7_sizing_min_multiplier=0.50,
        strategy7_sizing_max_multiplier=1.00,
        strategy7_sizing_strong_signal_gap=0.02,
        strategy7_sizing_strong_signal_boost=0.20,
    )
    decision = SideDecision(
        side="UP",
        candidate_price=0.51,
        signal_delta=0.055,
    )

    multiplier = strategy7_order_cost_multiplier(
        cfg=cfg,
        decision=decision,
        price=0.51,
        ofi_score=0.76,
    )

    assert multiplier == pytest.approx(1.00)


def test_strategy9_order_cost_multiplier_reuses_dynamic_sizing_when_enabled():
    cfg = AppConfig(
        strategy_id=9,
        strategy9_dynamic_sizing_enabled=True,
        strategy9_sizing_reference_price=0.50,
        strategy9_sizing_price_step=0.01,
        strategy9_sizing_price_step_reduction=0.10,
        strategy9_sizing_min_multiplier=0.50,
        strategy9_sizing_max_multiplier=1.00,
        strategy9_sizing_strong_signal_gap=0.02,
        strategy9_sizing_strong_signal_boost=0.20,
    )
    decision = SideDecision(
        side="UP",
        candidate_price=0.54,
        signal_delta=0.03,
        ofi_score=0.72,
    )

    multiplier = effective_decision_order_cost_multiplier(
        cfg=cfg,
        decision=decision,
        price=0.54,
    )

    assert multiplier == pytest.approx(0.60)


def test_strategy7_zero_confirm_window_allows_entry_inside_grace_window():
    now = datetime(2026, 4, 30, 1, 0, 13, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, tzinfo=timezone.utc),
    )
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "7",
            "ENTRY_GRACE_SECONDS": "18",
            "PAPER_STRATEGY_IDS": "7",
            "STRATEGY_7_OPEN_DELAY_SECONDS": "12",
            "STRATEGY_7_OFI_THRESHOLD": "0.5",
            "STRATEGY_7_MOMENTUM_THRESHOLD": "0.01",
            "STRATEGY_7_MIN_SIGNAL_GAP": "0.0",
            "STRATEGY_7_CONFIRM_BEFORE_ENTRY_SECONDS": "0",
            "STRATEGY_7_BINANCE_SIGNAL_STALE_SECONDS": "10.0",
        }
    )
    cfg = cfg_for_paper_strategy(cfg, 7)
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.54,
        up_best_ask=0.54,
        strategy6_ofi_score=0.7,
        strategy6_signal_at=now,
    )

    decision = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        window=window,
        entry_time=window.start_time + timedelta(seconds=cfg.open_delay_seconds),
        now=now,
    )

    assert decision.side == "UP"
    assert decision.reason is None


def test_strategy7_skips_when_ofi_and_momentum_conflict():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.5,
        strategy7_momentum_threshold=0.01,
        strategy7_min_signal_gap=0.0,
        strategy7_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.47,
        up_best_ask=0.47,
        strategy6_ofi_score=0.7,
        strategy6_signal_at=now,
    )

    decision = resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)

    assert decision.side is None
    assert decision.reason == "strategy7_signal_conflict"
    assert decision.signal_delta == pytest.approx(-0.03)
    assert state.signal_round_locked_side is None


def test_strategy7_skips_when_momentum_is_too_hot():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.5,
        strategy7_momentum_threshold=0.005,
        strategy7_min_signal_gap=0.003,
        strategy7_max_momentum_delta=0.015,
        strategy7_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.53,
        up_best_ask=0.53,
        strategy6_ofi_score=0.7,
        strategy6_signal_at=now,
    )

    decision = resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)

    assert decision.side is None
    assert decision.reason == "strategy7_momentum_too_hot"
    assert decision.signal_delta == pytest.approx(0.03)
    assert state.signal_round_locked_side is None


def test_strategy7_revalidates_locked_side_before_entry():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.5,
        strategy7_momentum_threshold=0.01,
        strategy7_min_signal_gap=0.0,
        strategy7_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState(
        signal_round_slug="s1",
        signal_round_open_up_price=0.50,
        signal_round_locked_side="UP",
    )
    quote = MarketQuote(
        slug="s1",
        up_price=0.47,
        up_best_ask=0.47,
        strategy6_ofi_score=0.7,
        strategy6_signal_at=now,
    )

    decision = resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)

    assert decision.side is None
    assert decision.reason == "strategy7_signal_conflict"
    assert decision.signal_delta == pytest.approx(-0.03)
    assert decision.signal_locked is True
    assert state.signal_round_locked_side == "UP"


def test_strategy7_locked_side_cannot_reverse_within_same_round():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.5,
        strategy7_momentum_threshold=0.01,
        strategy7_min_signal_gap=0.0,
        strategy7_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState(
        signal_round_slug="s1",
        signal_round_open_up_price=0.50,
        signal_round_locked_side="UP",
    )
    quote = MarketQuote(
        slug="s1",
        up_price=0.47,
        up_best_ask=0.47,
        down_price=0.53,
        down_best_ask=0.53,
        strategy6_ofi_score=-0.7,
        strategy6_signal_at=now,
    )

    first = resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)
    second = resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)

    assert first.side is None
    assert first.reason == "strategy7_signal_conflict"
    assert first.signal_locked is True
    assert second.side is None
    assert second.reason == "strategy7_signal_conflict"
    assert second.signal_locked is True
    assert state.signal_round_locked_side == "UP"


def test_strategy7_reports_signal_conflict_before_late_timing():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.5,
        strategy7_momentum_threshold=0.01,
        strategy7_min_signal_gap=0.0,
        strategy7_confirm_before_entry_seconds=15,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.47,
        up_best_ask=0.47,
        strategy6_ofi_score=0.7,
        strategy6_signal_at=now,
    )

    decision = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        now=now,
        entry_time=now + timedelta(seconds=10),
    )

    assert decision.side is None
    assert decision.reason == "strategy7_signal_conflict"
    assert decision.signal_delta == pytest.approx(-0.03)


def test_strategy7_reports_confidence_too_low_before_price_gate():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.5,
        strategy7_momentum_threshold=0.01,
        strategy7_min_signal_gap=0.05,
        max_entry_price=0.52,
        strategy7_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.53,
        up_best_ask=0.53,
        strategy6_ofi_score=0.52,
        strategy6_signal_at=now,
    )

    decision = resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)

    assert decision.side is None
    assert decision.reason == "strategy7_confidence_too_low"
    assert decision.signal_delta == pytest.approx(0.03)


def test_strategy7_only_locks_confirmed_side_inside_lock_window():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.5,
        strategy7_momentum_threshold=0.01,
        strategy7_min_signal_gap=0.0,
        strategy7_confirm_before_entry_seconds=0,
        signal_lock_before_entry_seconds=10,
        binance_signal_stale_seconds=60.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.53,
        up_best_ask=0.53,
        strategy6_ofi_score=0.7,
        strategy6_signal_at=now,
    )

    early = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        now=now,
        entry_time=now + timedelta(seconds=30),
    )

    assert early.side == "UP"
    assert early.signal_locked is False
    assert state.signal_round_locked_side is None

    late = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        now=now + timedelta(seconds=25),
        entry_time=now + timedelta(seconds=30),
    )

    assert late.side == "UP"
    assert late.signal_locked is True
    assert state.signal_round_locked_side == "UP"


def test_strategy8_ignores_strategy7_momentum_overheat_gate():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=8,
        strategy7_ofi_threshold=0.5,
        strategy7_momentum_threshold=0.01,
        strategy7_min_signal_gap=0.0,
        max_entry_price=0.58,
        strategy7_max_momentum_delta=0.02,
        strategy7_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.55,
        up_best_ask=0.55,
        strategy6_ofi_score=0.7,
        strategy6_signal_at=now,
    )

    decision = resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)

    assert decision.side == "UP"
    assert decision.reason is None
    assert decision.signal_delta == pytest.approx(0.05)


def test_strategy9_requires_stable_consensus_samples_before_entry():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=9,
        strategy7_ofi_threshold=0.5,
        strategy7_momentum_threshold=0.01,
        strategy7_min_signal_gap=0.0,
        strategy7_confirm_before_entry_seconds=0,
        strategy9_stability_sample_count=3,
        strategy9_stability_required_count=2,
        strategy9_stability_window_seconds=6,
        strategy9_base_max_entry_price=0.55,
        strategy9_strong_max_entry_price=0.55,
        strategy9_ultra_max_entry_price=0.55,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)

    first = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=MarketQuote(
            slug="s1",
            up_price=0.53,
            up_best_ask=0.53,
            strategy6_ofi_score=0.7,
            strategy6_signal_at=now,
        ),
        now=now,
    )
    second = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=MarketQuote(
            slug="s1",
            up_price=0.535,
            up_best_ask=0.535,
            strategy6_ofi_score=0.72,
            strategy6_signal_at=now + timedelta(seconds=2),
        ),
        now=now + timedelta(seconds=2),
    )
    third = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=MarketQuote(
            slug="s1",
            up_price=0.53,
            up_best_ask=0.53,
            strategy6_ofi_score=0.74,
            strategy6_signal_at=now + timedelta(seconds=4),
        ),
        now=now + timedelta(seconds=4),
    )

    assert first.side is None
    assert first.reason == "strategy9_signal_unstable"
    assert second.side is None
    assert second.reason == "strategy9_signal_unstable"
    assert third.side == "UP"
    assert third.reason is None
    assert len(state.strategy9_signal_samples) == 3


def test_strategy9_skips_when_recent_signal_is_decaying():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=9,
        strategy7_ofi_threshold=0.5,
        strategy7_momentum_threshold=0.01,
        strategy7_min_signal_gap=0.0,
        strategy7_confirm_before_entry_seconds=0,
        strategy9_stability_sample_count=3,
        strategy9_stability_required_count=2,
        strategy9_stability_window_seconds=6,
        strategy9_max_signal_decay=0.35,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)

    for offset, up_price in ((0, 0.56), (2, 0.55), (4, 0.53)):
        decision = resolve_side_from_strategy(
            cfg=cfg,
            state=state,
            slug="s1",
            quote=MarketQuote(
                slug="s1",
                up_price=up_price,
                up_best_ask=up_price,
                strategy6_ofi_score=0.75,
                strategy6_signal_at=now + timedelta(seconds=offset),
            ),
            now=now + timedelta(seconds=offset),
        )

    assert decision.side is None
    assert decision.reason == "strategy9_signal_decaying"
    assert decision.signal_delta == pytest.approx(0.03)


def test_strategy9_uses_dynamic_price_cap_by_signal_strength():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=9,
        strategy7_ofi_threshold=0.5,
        strategy7_momentum_threshold=0.01,
        strategy7_min_signal_gap=0.0,
        strategy7_confirm_before_entry_seconds=0,
        strategy9_stability_sample_count=3,
        strategy9_stability_required_count=2,
        strategy9_stability_window_seconds=6,
        strategy9_base_max_entry_price=0.52,
        strategy9_strong_max_entry_price=0.55,
        strategy9_ultra_max_entry_price=0.54,
        strategy9_strong_signal_gap=0.015,
        strategy9_ultra_signal_gap=0.04,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)

    for offset in (0, 2):
        decision = resolve_side_from_strategy(
            cfg=cfg,
            state=state,
            slug="s1",
            quote=MarketQuote(
                slug="s1",
                up_price=0.525,
                up_best_ask=0.525,
                strategy6_ofi_score=0.51,
                strategy6_signal_at=now + timedelta(seconds=offset),
            ),
            now=now + timedelta(seconds=offset),
        )
    weak_third = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=MarketQuote(
            slug="s1",
            up_price=0.525,
            up_best_ask=0.525,
            strategy6_ofi_score=0.51,
            strategy6_signal_at=now + timedelta(seconds=4),
        ),
        now=now + timedelta(seconds=4),
    )

    assert weak_third.side is None
    assert weak_third.reason == "strategy9_dynamic_price_too_high"
    assert weak_third.candidate_price == pytest.approx(0.525)
    assert weak_third.max_entry_price == pytest.approx(0.52)

    strong_state = SessionState(signal_round_slug="s2", signal_round_open_up_price=0.50)
    for offset in (0, 2, 4):
        strong_third = resolve_side_from_strategy(
            cfg=cfg,
            state=strong_state,
            slug="s2",
            quote=MarketQuote(
                slug="s2",
                up_price=0.525,
                up_best_ask=0.525,
                strategy6_ofi_score=0.56,
                strategy6_signal_at=now + timedelta(seconds=offset),
            ),
            now=now + timedelta(seconds=offset),
        )

    assert strong_third.side == "UP"
    assert strong_third.max_entry_price == pytest.approx(0.55)


def test_strategy10_buys_underpriced_up_edge():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=10,
        strategy7_ofi_threshold=0.5,
        strategy7_momentum_threshold=0.01,
        strategy7_min_signal_gap=0.0,
        strategy7_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
        strategy10_min_edge=0.04,
        strategy10_ofi_weight=0.10,
        strategy10_momentum_weight=1.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.52,
        up_best_ask=0.52,
        down_price=0.48,
        down_best_ask=0.48,
        strategy6_ofi_score=0.8,
        strategy6_signal_at=now,
    )

    decision = resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)

    assert decision.side == "UP"
    assert decision.candidate_price == pytest.approx(0.52)
    assert decision.signal_delta == pytest.approx(0.02)
    assert decision.signal_threshold == pytest.approx(0.04)
    assert decision.signal_probability == pytest.approx(0.62, abs=0.001)
    assert decision.signal_edge == pytest.approx(0.0775, abs=0.001)
    assert decision.reason is None


def test_strategy10_max_entry_price_uses_raw_entry_price_not_fee_adjusted_price():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=10,
        max_entry_price=0.54,
        strategy7_ofi_threshold=0.5,
        strategy7_momentum_threshold=0.01,
        strategy7_min_signal_gap=0.0,
        strategy7_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
        strategy10_min_edge=0.04,
        strategy10_ofi_weight=0.10,
        strategy10_momentum_weight=1.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.53,
        up_best_ask=0.53,
        down_price=0.47,
        down_best_ask=0.47,
        strategy6_ofi_score=0.8,
        strategy6_signal_at=now,
    )

    decision = resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)

    assert decision.side == "UP"
    assert decision.candidate_price == pytest.approx(0.53)
    assert decision.signal_delta == pytest.approx(0.03)
    assert decision.reason is None


def test_strategy10_skips_when_best_edge_is_below_threshold():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=10,
        strategy7_ofi_threshold=0.5,
        strategy7_momentum_threshold=0.01,
        strategy7_min_signal_gap=0.0,
        strategy7_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
        strategy10_min_edge=0.12,
        strategy10_ofi_weight=0.10,
        strategy10_momentum_weight=1.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.52,
        up_best_ask=0.52,
        down_price=0.48,
        down_best_ask=0.48,
        strategy6_ofi_score=0.8,
        strategy6_signal_at=now,
    )

    decision = resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)

    assert decision.side is None
    assert decision.reason == "strategy10_edge_too_low"
    assert decision.candidate_side == "UP"
    assert decision.candidate_price == pytest.approx(0.52)
    assert decision.signal_delta == pytest.approx(0.02)
    assert decision.signal_threshold == pytest.approx(0.12)
    assert decision.signal_probability == pytest.approx(0.62, abs=0.001)
    assert decision.signal_edge == pytest.approx(0.0775, abs=0.001)


def test_strategy10_skips_when_momentum_is_outside_configured_range():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=10,
        binance_signal_stale_seconds=10.0,
        strategy10_min_edge=0.04,
        strategy10_ofi_weight=0.10,
        strategy10_momentum_weight=1.0,
        strategy10_max_momentum_delta=0.02,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.53,
        up_best_ask=0.53,
        down_price=0.47,
        down_best_ask=0.47,
        strategy6_ofi_score=0.8,
        strategy6_signal_at=now,
    )

    decision = resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)

    assert decision.side is None
    assert decision.reason == "strategy10_momentum_too_hot"
    assert decision.signal_delta == pytest.approx(0.03)
    assert decision.signal_threshold == pytest.approx(0.02)


def test_strategy10_edge_uses_fee_adjusted_effective_price():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=10,
        strategy7_ofi_threshold=0.5,
        strategy7_momentum_threshold=0.01,
        strategy7_min_signal_gap=0.0,
        strategy7_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
        strategy10_min_edge=0.04,
        strategy10_ofi_weight=0.025,
        strategy10_momentum_weight=1.0,
        strategy10_edge_buffer=0.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.53,
        up_best_ask=0.53,
        down_price=0.47,
        down_best_ask=0.47,
        strategy6_ofi_score=0.8,
        strategy6_signal_at=now,
    )

    decision = resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)

    assert decision.side is None
    assert decision.reason == "strategy10_edge_too_low"
    assert decision.candidate_side == "UP"
    assert decision.candidate_price == pytest.approx(0.53)
    assert decision.signal_delta == pytest.approx(0.03)


def test_strategy10_can_choose_underpriced_down_from_fair_value():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(
        strategy_id=10,
        strategy7_ofi_threshold=0.5,
        strategy7_momentum_threshold=0.01,
        strategy7_min_signal_gap=0.0,
        strategy7_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
        strategy10_min_edge=0.04,
        strategy10_ofi_weight=0.10,
        strategy10_momentum_weight=1.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.47,
        up_best_ask=0.47,
        down_price=0.46,
        down_best_ask=0.46,
        strategy6_ofi_score=-0.7,
        strategy6_signal_at=now,
    )

    decision = resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)

    assert decision.side == "DOWN"
    assert decision.candidate_price == pytest.approx(0.46)
    assert decision.signal_delta == pytest.approx(-0.03)


def test_strategy10_uses_own_confirmation_window_instead_of_strategy7_window():
    now = datetime(2026, 4, 30, 1, 4, 59, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
    )
    cfg = AppConfig(
        strategy_id=10,
        strategy7_confirm_before_entry_seconds=12,
        strategy10_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
        strategy10_min_edge=0.04,
        strategy10_ofi_weight=0.10,
        strategy10_momentum_weight=1.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.52,
        up_best_ask=0.52,
        down_price=0.48,
        down_best_ask=0.48,
        strategy6_ofi_score=0.8,
        strategy6_signal_at=now,
    )

    decision = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        window=window,
        entry_time=window.end_time,
        now=now,
    )

    assert decision.side == "UP"
    assert decision.reason is None


def test_strategy11_buys_underpriced_up_from_btc_distance_probability():
    now = datetime(2026, 4, 30, 1, 2, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
    )
    cfg = AppConfig(
        strategy_id=11,
        strategy11_min_edge=0.04,
        strategy11_edge_buffer=0.0,
        strategy11_volatility_bps_per_sqrt_minute=12.0,
        strategy11_min_probability=0.55,
        strategy11_max_probability=0.95,
        strategy11_confirm_before_entry_seconds=0,
    )
    state = SessionState(signal_round_slug="s1", strategy11_round_start_btc_price=100000.0)
    quote = MarketQuote(
        slug="s1",
        up_best_ask=0.54,
        down_best_ask=0.46,
        binance_mid_price=100120.0,
        binance_signal_at=now,
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

    assert decision.side == "UP"
    assert decision.candidate_price == pytest.approx(0.54)
    assert decision.signal_threshold == pytest.approx(0.04)
    assert decision.signal_delta == pytest.approx(120.0)
    assert decision.reason is None


def test_strategy11_uses_window_price_to_beat_as_round_start_anchor():
    now = datetime(2026, 4, 30, 1, 2, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
        price_to_beat=100000.0,
    )
    cfg = AppConfig(
        strategy_id=11,
        strategy11_min_edge=0.04,
        strategy11_edge_buffer=0.0,
        strategy11_volatility_bps_per_sqrt_minute=12.0,
        strategy11_min_probability=0.55,
        strategy11_max_probability=0.95,
        strategy11_confirm_before_entry_seconds=0,
    )
    state = SessionState()
    quote = MarketQuote(
        slug="s1",
        up_best_ask=0.54,
        down_best_ask=0.46,
        binance_mid_price=100120.0,
        binance_signal_at=now,
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

    assert state.strategy11_round_start_btc_price == pytest.approx(100000.0)
    assert decision.side == "UP"
    assert decision.signal_open_up_price == pytest.approx(100000.0)
    assert decision.signal_delta == pytest.approx(120.0)


def test_strategy11_skips_when_probability_edge_is_too_low():
    now = datetime(2026, 4, 30, 1, 2, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
    )
    cfg = AppConfig(
        strategy_id=11,
        strategy11_min_edge=0.08,
        strategy11_edge_buffer=0.0,
        strategy11_volatility_bps_per_sqrt_minute=30.0,
        strategy11_min_probability=0.55,
        strategy11_max_probability=0.95,
        strategy11_confirm_before_entry_seconds=0,
    )
    state = SessionState(signal_round_slug="s1", strategy11_round_start_btc_price=100000.0)
    quote = MarketQuote(
        slug="s1",
        up_best_ask=0.54,
        down_best_ask=0.46,
        binance_mid_price=100080.0,
        binance_signal_at=now,
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
    assert decision.reason == "strategy11_edge_too_low"
    assert decision.candidate_side == "UP"
    assert decision.candidate_price == pytest.approx(0.54)
    assert decision.signal_threshold == pytest.approx(0.08)
    assert decision.signal_probability == pytest.approx(0.5612, abs=0.001)
    assert decision.signal_edge == pytest.approx(0.0038, abs=0.001)


def test_strategy11_edge_too_low_skip_keeps_probability_diagnostics():
    decision = SideDecision(
        side=None,
        reason="strategy11_edge_too_low",
        candidate_side="UP",
        candidate_price=0.53,
        signal_open_up_price=100000.0,
        signal_current_up_price=100090.0,
        signal_threshold=0.01,
        signal_delta=90.0,
        signal_probability=0.61,
        signal_edge=0.006,
    )

    fields = signal_record_kwargs(decision)

    assert fields["signal_probability"] == pytest.approx(0.61)
    assert fields["signal_edge"] == pytest.approx(0.006)
    assert fields["signal_reason"] == "strategy11_edge_too_low"


def test_strategy11_paper_tuning_can_emit_trial_signal_without_relaxing_live_defaults():
    now = datetime(2026, 4, 30, 1, 2, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
        price_to_beat=100000.0,
    )
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "11",
            "PAPER_STRATEGY_IDS": "11",
            "LIVE_STRATEGY_IDS": "7,10",
            "STRATEGY_11_MIN_EDGE": "0.04",
            "STRATEGY_11_VOLATILITY_BPS_PER_SQRT_MINUTE": "18",
            "PAPER_STRATEGY_11_MIN_EDGE": "0.005",
            "PAPER_STRATEGY_11_MIN_PROBABILITY": "0.54",
            "PAPER_STRATEGY_11_VOLATILITY_BPS_PER_SQRT_MINUTE": "24",
            "PAPER_STRATEGY_11_MAX_ENTRY_PRICE": "0.56",
            "PAPER_STRATEGY_11_CONFIRM_BEFORE_ENTRY_SECONDS": "0",
        }
    )
    paper_cfg = cfg_for_paper_strategy(cfg, 11)
    state = SessionState()
    quote = MarketQuote(
        slug="s1",
        up_best_ask=0.56,
        down_best_ask=0.44,
        binance_mid_price=100120.0,
        binance_signal_at=now,
    )

    decision = resolve_side_from_strategy(
        cfg=paper_cfg,
        state=state,
        slug="s1",
        quote=quote,
        window=window,
        entry_time=now,
        now=now,
    )

    assert cfg.live_strategy_ids == [7, 10]
    assert cfg.live_profiles[10].strategy11_min_edge == pytest.approx(0.04)
    assert decision.side == "UP"
    assert decision.candidate_price == pytest.approx(0.56)
    assert decision.signal_edge is not None
    assert decision.signal_edge >= paper_cfg.strategy11_min_edge


def test_strategy12_buys_when_probability_edge_and_microstructure_confirm():
    now = datetime(2026, 4, 30, 1, 2, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
        price_to_beat=100000.0,
    )
    cfg = AppConfig(
        strategy_id=12,
        max_entry_price=0.56,
        strategy7_ofi_threshold=0.50,
        strategy7_momentum_threshold=0.005,
        strategy7_min_signal_gap=0.0,
        strategy7_max_momentum_delta=0.12,
        strategy11_min_edge=0.005,
        strategy11_edge_buffer=0.0,
        strategy11_volatility_bps_per_sqrt_minute=24.0,
        strategy11_min_probability=0.54,
        strategy11_max_probability=0.95,
        strategy11_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
        strategy9_stability_sample_count=1,
        strategy9_stability_required_count=1,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.52,
        up_best_ask=0.52,
        down_price=0.48,
        down_best_ask=0.48,
        strategy6_ofi_score=0.72,
        strategy6_signal_at=now,
        binance_mid_price=100120.0,
        binance_signal_at=now,
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

    assert decision.side == "UP"
    assert decision.reason is None
    assert decision.candidate_side == "UP"
    assert decision.candidate_price == pytest.approx(0.52)
    assert decision.signal_probability is not None
    assert decision.signal_probability >= cfg.strategy11_min_probability
    assert decision.signal_edge is not None
    assert decision.signal_edge >= cfg.strategy11_min_edge
    assert decision.ofi_score == pytest.approx(0.72)


def test_strategy12_skips_when_probability_and_microstructure_conflict():
    now = datetime(2026, 4, 30, 1, 2, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
        price_to_beat=100000.0,
    )
    cfg = AppConfig(
        strategy_id=12,
        max_entry_price=0.56,
        strategy7_ofi_threshold=0.50,
        strategy7_momentum_threshold=0.005,
        strategy7_min_signal_gap=0.0,
        strategy7_max_momentum_delta=0.12,
        strategy11_min_edge=0.005,
        strategy11_edge_buffer=0.0,
        strategy11_volatility_bps_per_sqrt_minute=24.0,
        strategy11_min_probability=0.54,
        strategy11_max_probability=0.95,
        strategy11_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.52,
        up_best_ask=0.52,
        down_price=0.48,
        down_best_ask=0.48,
        strategy6_ofi_score=-0.72,
        strategy6_signal_at=now,
        binance_mid_price=100120.0,
        binance_signal_at=now,
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
    assert decision.reason == "strategy12_signal_conflict"
    assert decision.candidate_side == "UP"
    assert decision.candidate_price == pytest.approx(0.52)
    assert decision.signal_probability is not None
    assert decision.signal_edge is not None
    assert decision.ofi_score == pytest.approx(-0.72)


def test_strategy12_requires_microstructure_stability_before_entry():
    now = datetime(2026, 4, 30, 1, 2, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
        price_to_beat=100000.0,
    )
    cfg = AppConfig(
        strategy_id=12,
        max_entry_price=0.56,
        strategy7_ofi_threshold=0.58,
        strategy7_momentum_threshold=0.008,
        strategy7_min_signal_gap=0.01,
        strategy7_max_momentum_delta=0.12,
        strategy9_stability_sample_count=2,
        strategy9_stability_required_count=2,
        strategy9_stability_window_seconds=6.0,
        strategy11_min_edge=0.005,
        strategy11_edge_buffer=0.0,
        strategy11_volatility_bps_per_sqrt_minute=24.0,
        strategy11_min_probability=0.54,
        strategy11_max_probability=0.95,
        strategy11_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.52,
        up_best_ask=0.52,
        down_price=0.48,
        down_best_ask=0.48,
        strategy6_ofi_score=0.72,
        strategy6_signal_at=now,
        binance_mid_price=100120.0,
        binance_signal_at=now,
    )

    first = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        window=window,
        entry_time=now,
        now=now,
    )
    second = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        window=window,
        entry_time=now + timedelta(seconds=2),
        now=now + timedelta(seconds=2),
    )

    assert first.side is None
    assert first.reason == "strategy12_micro_unstable"
    assert first.candidate_side == "UP"
    assert first.signal_edge is not None
    assert second.side == "UP"
    assert second.reason is None


def test_strategy12_skips_when_microstructure_signal_decays():
    now = datetime(2026, 4, 30, 1, 2, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
        price_to_beat=100000.0,
    )
    cfg = AppConfig(
        strategy_id=12,
        max_entry_price=0.56,
        strategy7_ofi_threshold=0.58,
        strategy7_momentum_threshold=0.008,
        strategy7_min_signal_gap=0.01,
        strategy7_max_momentum_delta=0.12,
        strategy9_stability_sample_count=1,
        strategy9_stability_required_count=1,
        strategy9_reversal_lookback_seconds=6.0,
        strategy9_max_signal_decay=0.35,
        strategy11_min_edge=0.005,
        strategy11_edge_buffer=0.0,
        strategy11_volatility_bps_per_sqrt_minute=24.0,
        strategy11_min_probability=0.54,
        strategy11_max_probability=0.95,
        strategy11_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    strong_quote = MarketQuote(
        slug="s1",
        up_price=0.56,
        up_best_ask=0.56,
        down_price=0.44,
        down_best_ask=0.44,
        strategy6_ofi_score=0.90,
        strategy6_signal_at=now,
        binance_mid_price=100120.0,
        binance_signal_at=now,
    )
    weak_quote = MarketQuote(
        slug="s1",
        up_price=0.52,
        up_best_ask=0.52,
        down_price=0.48,
        down_best_ask=0.48,
        strategy6_ofi_score=0.79,
        strategy6_signal_at=now + timedelta(seconds=2),
        binance_mid_price=100120.0,
        binance_signal_at=now + timedelta(seconds=2),
    )

    first = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=strong_quote,
        window=window,
        entry_time=now,
        now=now,
    )
    second = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=weak_quote,
        window=window,
        entry_time=now + timedelta(seconds=2),
        now=now + timedelta(seconds=2),
    )

    assert first.side == "UP"
    assert second.side is None
    assert second.reason == "strategy12_micro_decaying"
    assert second.candidate_side == "UP"
    assert second.signal_edge is not None


def test_strategy12_probability_skip_keeps_strategy12_reason_and_diagnostics():
    now = datetime(2026, 4, 30, 1, 2, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
        price_to_beat=100000.0,
    )
    cfg = AppConfig(
        strategy_id=12,
        max_entry_price=0.56,
        strategy7_ofi_threshold=0.50,
        strategy7_momentum_threshold=0.005,
        strategy7_min_signal_gap=0.0,
        strategy11_min_edge=0.08,
        strategy11_edge_buffer=0.0,
        strategy11_volatility_bps_per_sqrt_minute=30.0,
        strategy11_min_probability=0.55,
        strategy11_max_probability=0.95,
        strategy11_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
    )
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.54,
        up_best_ask=0.54,
        down_price=0.46,
        down_best_ask=0.46,
        strategy6_ofi_score=0.72,
        strategy6_signal_at=now,
        binance_mid_price=100080.0,
        binance_signal_at=now,
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
    assert decision.reason == "strategy12_edge_too_low"
    assert decision.candidate_side == "UP"
    assert decision.candidate_price == pytest.approx(0.54)
    assert decision.signal_probability is not None
    assert decision.signal_edge is not None


def test_strategy11_edge_uses_fee_adjusted_effective_price():
    now = datetime(2026, 4, 30, 1, 2, 0, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=datetime(2026, 4, 30, 1, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 30, 1, 5, 0, tzinfo=timezone.utc),
    )
    cfg = AppConfig(
        strategy_id=11,
        max_entry_price=0.90,
        strategy11_min_edge=0.04,
        strategy11_edge_buffer=0.0,
        strategy11_volatility_bps_per_sqrt_minute=30.0,
        strategy11_min_probability=0.55,
        strategy11_max_probability=0.95,
        strategy11_confirm_before_entry_seconds=0,
    )
    state = SessionState(signal_round_slug="s1", strategy11_round_start_btc_price=100000.0)
    quote = MarketQuote(
        slug="s1",
        up_best_ask=0.55,
        down_best_ask=0.45,
        binance_mid_price=100140.0,
        binance_signal_at=now,
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
    assert decision.reason == "strategy11_edge_too_low"
    assert decision.candidate_side == "UP"
    assert decision.candidate_price == pytest.approx(0.55)
    assert decision.signal_probability == pytest.approx(0.6062, abs=0.001)


def test_strategy10_skips_stale_ofi_before_edge_model():
    now = datetime(2026, 4, 30, 1, 0, tzinfo=timezone.utc)
    cfg = AppConfig(strategy_id=10, binance_signal_stale_seconds=1.0)
    state = SessionState(signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.52,
        up_best_ask=0.52,
        down_price=0.48,
        down_best_ask=0.48,
        strategy6_ofi_score=0.8,
        strategy6_signal_at=now - timedelta(seconds=5),
    )

    decision = resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)

    assert decision.side is None
    assert decision.reason == "strategy10_ofi_stale"
