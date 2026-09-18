from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from clob_adapter import effective_price_after_fee
from config import _STRATEGY_ID_MAX, _parse_strategy_id_list, AppConfig, build_config_from_env_values, collect_config_warnings
from models import MarketQuote, MarketWindow, SessionState
from runtime_config import cfg_for_paper_strategy
from strategy_decision import (
    estimate_strategy14_fair_value,
    evaluate_strategy14_fade,
    resolve_side_from_strategy,
)

NOW = datetime(2026, 4, 30, 1, 2, 0, tzinfo=timezone.utc)


def _window(*, start_offset_seconds: int = -120, end_offset_seconds: int = 240) -> MarketWindow:
    return MarketWindow(
        event_id="e1",
        market_id="m1",
        slug="s1",
        title="BTC",
        start_time=NOW + timedelta(seconds=start_offset_seconds),
        end_time=NOW + timedelta(seconds=end_offset_seconds),
    )


def _cfg(**overrides) -> AppConfig:
    base = dict(
        strategy_id=14,
        max_entry_price=0.56,
        strategy14_min_price_gap=0.05,
        strategy14_ofi_weight=0.08,
        strategy14_min_edge=0.03,
        strategy14_edge_buffer=0.005,
        strategy14_min_entry_price=0.30,
        strategy14_max_entry_price=0.52,
        strategy14_extreme_move_price=0.85,
        strategy14_min_remaining_seconds=60,
        strategy14_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=10.0,
    )
    base.update(overrides)
    return AppConfig(**base)


def _quote(*, up_price: float, up_ask: float | None = None, down_ask: float | None = None, ofi: float | None = 0.0, ofi_at: datetime | None = None) -> MarketQuote:
    return MarketQuote(
        slug="s1",
        up_price=up_price,
        down_price=1.0 - up_price,
        up_best_ask=up_ask if up_ask is not None else up_price,
        down_best_ask=down_ask if down_ask is not None else 1.0 - up_price,
        strategy6_ofi_score=ofi,
        strategy6_signal_at=ofi_at if ofi_at is not None else NOW,
        fetched_at=NOW,
    )


def _state(**overrides) -> SessionState:
    base = dict(signal_round_slug="s1", signal_round_open_up_price=0.50)
    base.update(overrides)
    return SessionState(**base)


# ---------------------------------------------------------------------------
# estimate_strategy14_fair_value
# ---------------------------------------------------------------------------


def test_strategy14_fair_value_neutral_ofi_anchors_at_open_price():
    cfg = _cfg()
    quote = _quote(up_price=0.58)

    fair_value = estimate_strategy14_fair_value(
        cfg=cfg,
        quote=quote,
        open_up_price=0.50,
        current_up_price=0.58,
        ofi_score=0.0,
    )

    assert fair_value.fair_up_probability == pytest.approx(0.50)
    assert fair_value.fair_down_probability == pytest.approx(0.50)
    assert fair_value.move == pytest.approx(0.08)
    assert fair_value.overshoot == pytest.approx(0.08)
    assert fair_value.fade_side == "DOWN"
    assert fair_value.fade_price == pytest.approx(0.42)
    assert fair_value.fade_fair_probability == pytest.approx(0.50)
    expected_edge = 0.50 - effective_price_after_fee(0.42) - 0.005
    assert fair_value.fade_edge == pytest.approx(expected_edge)


def test_strategy14_fair_value_ofi_supporting_move_shrinks_overshoot():
    cfg = _cfg()
    quote = _quote(up_price=0.58, ofi=0.5)

    fair_value = estimate_strategy14_fair_value(
        cfg=cfg,
        quote=quote,
        open_up_price=0.50,
        current_up_price=0.58,
        ofi_score=0.5,
    )

    # OFI moves the fair line up with the market: 0.50 + 0.08 * 0.5 = 0.54
    assert fair_value.fair_up_probability == pytest.approx(0.54)
    assert fair_value.overshoot == pytest.approx(0.04)
    # Overshoot below the 0.05 gap: a confirmed trend is not faded.
    assert fair_value.fade_side is None
    assert fair_value.fade_edge is None


def test_strategy14_fair_value_opposing_ofi_widens_overshoot():
    cfg = _cfg()
    quote = _quote(up_price=0.56, ofi=-0.5)

    fair_value = estimate_strategy14_fair_value(
        cfg=cfg,
        quote=quote,
        open_up_price=0.50,
        current_up_price=0.56,
        ofi_score=-0.5,
    )

    assert fair_value.fair_up_probability == pytest.approx(0.46)
    assert fair_value.overshoot == pytest.approx(0.10)
    assert fair_value.fade_side == "DOWN"
    assert fair_value.fade_edge is not None
    assert fair_value.fade_edge > 0.03


def test_strategy14_fair_value_downward_overreaction_fades_up():
    cfg = _cfg()
    quote = _quote(up_price=0.42)

    fair_value = estimate_strategy14_fair_value(
        cfg=cfg,
        quote=quote,
        open_up_price=0.50,
        current_up_price=0.42,
        ofi_score=0.0,
    )

    assert fair_value.overshoot == pytest.approx(-0.08)
    assert fair_value.fade_side == "UP"
    assert fair_value.fade_price == pytest.approx(0.42)
    expected_edge = 0.50 - effective_price_after_fee(0.42) - 0.005
    assert fair_value.fade_edge == pytest.approx(expected_edge)


def test_strategy14_fair_value_clamps_extreme_anchors():
    cfg = _cfg()
    quote = _quote(up_price=0.60, ofi=1.0)

    fair_value = estimate_strategy14_fair_value(
        cfg=cfg,
        quote=quote,
        open_up_price=0.97,
        current_up_price=0.60,
        ofi_score=1.0,
    )

    assert fair_value.fair_up_probability == pytest.approx(0.95)
    assert fair_value.fair_down_probability == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# evaluate_strategy14_fade
# ---------------------------------------------------------------------------


def test_strategy14_skips_when_ofi_unavailable():
    decision = evaluate_strategy14_fade(
        cfg=_cfg(),
        quote=_quote(up_price=0.58, ofi=None),
        now=NOW,
        window=_window(),
        signal_open_up_price=0.50,
        signal_current_up_price=0.58,
    )

    assert decision.side is None
    assert decision.reason == "strategy14_ofi_unavailable"


def test_strategy14_skips_when_ofi_stale():
    decision = evaluate_strategy14_fade(
        cfg=_cfg(),
        quote=_quote(up_price=0.58, ofi_at=NOW - timedelta(seconds=30)),
        now=NOW,
        window=_window(),
        signal_open_up_price=0.50,
        signal_current_up_price=0.58,
    )

    assert decision.side is None
    assert decision.reason == "strategy14_ofi_stale"
    assert decision.ofi_score == pytest.approx(0.0)


def test_strategy14_skips_when_anchor_unavailable():
    decision = evaluate_strategy14_fade(
        cfg=_cfg(),
        quote=_quote(up_price=0.58),
        now=NOW,
        window=_window(),
        signal_open_up_price=None,
        signal_current_up_price=0.58,
    )

    assert decision.side is None
    assert decision.reason == "strategy14_anchor_unavailable"


def test_strategy14_skips_when_move_too_small():
    decision = evaluate_strategy14_fade(
        cfg=_cfg(),
        quote=_quote(up_price=0.54),
        now=NOW,
        window=_window(),
        signal_open_up_price=0.50,
        signal_current_up_price=0.54,
    )

    assert decision.side is None
    assert decision.reason == "strategy14_move_too_small"
    assert decision.signal_delta == pytest.approx(0.04)


def test_strategy14_confirmed_trend_is_not_faded():
    # The move is "confirmed" by OFI, so the fair line follows the price
    # and no overshoot remains.
    decision = evaluate_strategy14_fade(
        cfg=_cfg(),
        quote=_quote(up_price=0.58, ofi=0.5),
        now=NOW,
        window=_window(),
        signal_open_up_price=0.50,
        signal_current_up_price=0.58,
    )

    assert decision.side is None
    assert decision.reason == "strategy14_move_too_small"


def test_strategy14_skips_when_move_too_extreme():
    decision = evaluate_strategy14_fade(
        cfg=_cfg(),
        quote=_quote(up_price=0.90),
        now=NOW,
        window=_window(),
        signal_open_up_price=0.50,
        signal_current_up_price=0.90,
    )

    assert decision.side is None
    assert decision.reason == "strategy14_move_too_extreme"


def test_strategy14_skips_when_edge_too_low():
    decision = evaluate_strategy14_fade(
        cfg=_cfg(strategy14_min_edge=0.08),
        quote=_quote(up_price=0.58),
        now=NOW,
        window=_window(),
        signal_open_up_price=0.50,
        signal_current_up_price=0.58,
    )

    assert decision.side is None
    assert decision.reason == "strategy14_edge_too_low"
    assert decision.candidate_side == "DOWN"
    assert decision.signal_edge is not None
    assert decision.signal_edge < 0.08
    assert decision.signal_threshold == pytest.approx(0.08)


def test_strategy14_skips_when_not_enough_time_remains():
    decision = evaluate_strategy14_fade(
        cfg=_cfg(),
        quote=_quote(up_price=0.58),
        now=NOW,
        window=_window(end_offset_seconds=30),
        signal_open_up_price=0.50,
        signal_current_up_price=0.58,
    )

    assert decision.side is None
    assert decision.reason == "strategy14_entry_too_late"
    assert decision.candidate_side == "DOWN"


def test_strategy14_buys_cheap_side_when_up_overreacts():
    decision = evaluate_strategy14_fade(
        cfg=_cfg(),
        quote=_quote(up_price=0.58),
        now=NOW,
        window=_window(),
        signal_open_up_price=0.50,
        signal_current_up_price=0.58,
    )

    assert decision.side == "DOWN"
    assert decision.reason is None
    assert decision.candidate_side == "DOWN"
    assert decision.candidate_price == pytest.approx(0.42)
    assert decision.signal_probability == pytest.approx(0.50)
    assert decision.signal_edge is not None
    assert decision.signal_edge >= 0.03
    assert decision.signal_threshold == pytest.approx(0.03)
    assert decision.signal_delta == pytest.approx(0.08)
    assert decision.ofi_score == pytest.approx(0.0)
    assert decision.max_entry_price == pytest.approx(0.52)


def test_strategy14_buys_cheap_side_when_down_overreacts():
    decision = evaluate_strategy14_fade(
        cfg=_cfg(),
        quote=_quote(up_price=0.42),
        now=NOW,
        window=_window(),
        signal_open_up_price=0.50,
        signal_current_up_price=0.42,
    )

    assert decision.side == "UP"
    assert decision.reason is None
    assert decision.candidate_price == pytest.approx(0.42)
    assert decision.signal_probability == pytest.approx(0.50)
    assert decision.signal_delta == pytest.approx(-0.08)


# ---------------------------------------------------------------------------
# resolve_side_from_strategy dispatch (strategy_id=14)
# ---------------------------------------------------------------------------


def test_strategy14_dispatch_buys_fade_side_and_records_ofi():
    cfg = _cfg()
    state = _state()

    decision = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=_quote(up_price=0.58),
        window=_window(),
        entry_time=NOW,
        now=NOW,
    )

    assert decision.side == "DOWN"
    assert decision.signal_locked is True
    assert state.strategy6_last_ofi_score == pytest.approx(0.0)
    assert decision.candidate_price == pytest.approx(0.42)


def test_strategy14_dispatch_skips_when_fade_side_ask_above_band():
    cfg = _cfg()
    state = _state()

    decision = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=_quote(up_price=0.58, down_ask=0.55),
        window=_window(),
        entry_time=NOW,
        now=NOW,
    )

    # An ask of 0.55 for DOWN pushes the fee-adjusted edge below the
    # minimum, so the edge gate fires before the price band check.
    assert decision.side is None
    assert decision.reason == "strategy14_edge_too_low"


def test_strategy14_dispatch_skips_when_fade_side_ask_below_band():
    cfg = _cfg()
    state = _state()

    decision = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=_quote(up_price=0.72, down_ask=0.28),
        window=_window(),
        entry_time=NOW,
        now=NOW,
    )

    assert decision.side is None
    assert decision.reason == "strategy14_price_too_low"
    assert decision.candidate_side == "DOWN"
    assert decision.candidate_price == pytest.approx(0.28)


def test_strategy14_locked_branch_keeps_locked_side_when_signal_agrees():
    cfg = _cfg()
    state = _state(signal_round_locked_side="DOWN")

    decision = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=_quote(up_price=0.58),
        window=_window(),
        entry_time=NOW,
        now=NOW,
    )

    assert decision.side == "DOWN"
    assert decision.signal_locked is True
    assert decision.candidate_price == pytest.approx(0.42)


def test_strategy14_locked_branch_reports_conflict_when_signal_flips():
    cfg = _cfg()
    state = _state(signal_round_locked_side="DOWN")

    # Price now overshot downward instead: the fresh fade wants UP.
    decision = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=_quote(up_price=0.42),
        window=_window(),
        entry_time=NOW,
        now=NOW,
    )

    assert decision.side is None
    assert decision.reason == "strategy14_signal_conflict"


def test_strategy14_locked_branch_rechecks_price_band():
    cfg = _cfg()
    state = _state(signal_round_locked_side="DOWN")

    decision = resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=_quote(up_price=0.72, down_ask=0.28),
        window=_window(),
        entry_time=NOW,
        now=NOW,
    )

    assert decision.side is None
    assert decision.reason == "strategy14_price_too_low"


# ---------------------------------------------------------------------------
# config integration
# ---------------------------------------------------------------------------


def test_strategy14_id_is_supported_and_15_is_rejected():
    assert _STRATEGY_ID_MAX == 14
    assert _parse_strategy_id_list("14", fallback=2) == [14]
    assert _parse_strategy_id_list("15", fallback=2) == [2]
    warnings = collect_config_warnings({"PAPER_STRATEGY_IDS": "15"})
    assert "PAPER_STRATEGY_IDS" in warnings
    assert "15" in warnings["PAPER_STRATEGY_IDS"]
    warnings = collect_config_warnings({"STRATEGY_ID": "15"})
    assert warnings["STRATEGY_ID"] == "Invalid value for STRATEGY_ID: expected strategy id 1-14, got '15'"
    assert collect_config_warnings({"PAPER_STRATEGY_IDS": "14"}) == {}


def test_strategy14_reads_env_defaults():
    # The AppConfig field factories read the underscore-free env keys
    # (same convention as STRATEGY11_MIN_EDGE & friends).
    cfg = build_config_from_env_values(
        {
            "STRATEGY14_MIN_PRICE_GAP": "0.06",
            "STRATEGY14_OFI_WEIGHT": "0.10",
            "STRATEGY14_MIN_EDGE": "0.04",
            "STRATEGY14_EDGE_BUFFER": "0.01",
            "STRATEGY14_MIN_ENTRY_PRICE": "0.35",
            "STRATEGY14_MAX_ENTRY_PRICE": "0.50",
            "STRATEGY14_EXTREME_MOVE_PRICE": "0.80",
            "STRATEGY14_MIN_REMAINING_SECONDS": "90",
            "STRATEGY14_CONFIRM_BEFORE_ENTRY_SECONDS": "3",
        }
    )

    assert cfg.strategy14_min_price_gap == pytest.approx(0.06)
    assert cfg.strategy14_ofi_weight == pytest.approx(0.10)
    assert cfg.strategy14_min_edge == pytest.approx(0.04)
    assert cfg.strategy14_edge_buffer == pytest.approx(0.01)
    assert cfg.strategy14_min_entry_price == pytest.approx(0.35)
    assert cfg.strategy14_max_entry_price == pytest.approx(0.50)
    assert cfg.strategy14_extreme_move_price == pytest.approx(0.80)
    assert cfg.strategy14_min_remaining_seconds == 90
    assert cfg.strategy14_confirm_before_entry_seconds == 3


def test_strategy14_defaults_are_conservative():
    cfg = AppConfig()

    assert cfg.strategy14_min_price_gap == pytest.approx(0.05)
    assert cfg.strategy14_ofi_weight == pytest.approx(0.08)
    assert cfg.strategy14_min_edge == pytest.approx(0.03)
    assert cfg.strategy14_edge_buffer == pytest.approx(0.005)
    assert cfg.strategy14_min_entry_price == pytest.approx(0.30)
    assert cfg.strategy14_max_entry_price == pytest.approx(0.52)
    assert cfg.strategy14_extreme_move_price == pytest.approx(0.85)
    assert cfg.strategy14_min_remaining_seconds == 60
    assert cfg.strategy14_confirm_before_entry_seconds == 0


def test_strategy14_profile_override_applies_to_paper_strategy_cfg():
    # The dashboard-facing underscore key (STRATEGY_14_MIN_EDGE) flows through
    # the per-strategy profile and overrides the runtime cfg.
    cfg = build_config_from_env_values(
        {
            "PAPER_STRATEGY_IDS": "14",
            "STRATEGY_14_MIN_EDGE": "0.045",
        }
    )
    paper_cfg = cfg_for_paper_strategy(cfg, 14)

    assert paper_cfg.strategy_id == 14
    assert paper_cfg.strategy14_min_edge == pytest.approx(0.045)


def test_strategy14_is_not_added_to_default_strategy_lists():
    cfg = AppConfig()

    assert 14 not in cfg.paper_strategy_ids
    assert 14 not in cfg.live_strategy_ids


def test_strategy14_is_excluded_from_legacy_unified_live_fallback():
    # Legacy configs that only set STRATEGY_IDS derive the live default from
    # that list; like strategy 13, strategy 14 must not join live by default.
    cfg = build_config_from_env_values({"STRATEGY_IDS": "5,6,14"})

    assert 14 in cfg.paper_strategy_ids
    assert 14 not in cfg.live_strategy_ids
