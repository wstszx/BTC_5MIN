from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from config import AppConfig, build_config_from_env_values
from models import MarketQuote, MarketWindow, SessionState
from runtime_config import cfg_for_paper_strategy
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
        strategy9_strong_max_entry_price=0.53,
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
    assert strong_third.max_entry_price == pytest.approx(0.53)


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
