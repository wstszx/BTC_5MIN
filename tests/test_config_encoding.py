from __future__ import annotations

from pathlib import Path

import pytest

from config import AppConfig, build_config_from_env_values, collect_config_warnings, load_env_file_values


def test_load_env_file_values_reads_gbk_encoded_dashboard_file(tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    env_file.write_bytes("COMMENT=abc\nMAX_STAKE=9.5\n".encode("gbk"))

    values = load_env_file_values(env_file)

    assert values == {"COMMENT": "abc", "MAX_STAKE": "9.5"}


def test_build_config_from_gbk_encoded_dashboard_file_ignores_global_strategy_params(tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    env_file.write_bytes("TRADE_MODE=paper\nMAX_STAKE=9.5\nPOLYMARKET_FUNDER=addr\n".encode("gbk"))

    values = load_env_file_values(env_file)
    cfg = build_config_from_env_values(values)

    assert cfg.trade_mode == "paper"
    assert cfg.max_stake is None
    assert cfg.live_funder == "addr"


def test_collect_config_warnings_reports_invalid_scalar_values():
    warnings = collect_config_warnings(
        {
            "STRATEGY_7_MAX_STAKE": "abc",
            "WS_ENABLED": "maybe",
            "STRATEGY_ID": "13",
            "MARKET_TIMEFRAME": "7m",
            "TARGET_PROFIT": "1.2",
        }
    )

    assert warnings["STRATEGY_7_MAX_STAKE"] == "Invalid value for STRATEGY_7_MAX_STAKE: expected number, got 'abc'"
    assert warnings["WS_ENABLED"] == "Invalid value for WS_ENABLED: expected true/false, got 'maybe'"
    assert warnings["STRATEGY_ID"] == "Invalid value for STRATEGY_ID: expected strategy id 1-12, got '13'"
    assert warnings["MARKET_TIMEFRAME"] == "Invalid value for MARKET_TIMEFRAME: expected one of 5m, 15m, got '7m'"
    assert "TARGET_PROFIT" not in warnings


def test_collect_config_warnings_validates_mode_specific_profile_keys():
    warnings = collect_config_warnings(
        {
            "STRATEGY_IDS": "2,x,9",
            "PAPER_15M_TARGET_PROFIT": "oops",
            "LIVE_STRATEGY_7_BASE_ORDER_COST": "bad",
            "STRATEGY_7_BASE_ORDER_COST": "bad",
        }
    )

    assert warnings["STRATEGY_IDS"] == "Invalid entries for STRATEGY_IDS ignored: x"
    assert warnings["STRATEGY_7_BASE_ORDER_COST"] == "Invalid value for STRATEGY_7_BASE_ORDER_COST: expected number, got 'bad'"
    assert warnings["LIVE_STRATEGY_7_BASE_ORDER_COST"] == "Invalid value for LIVE_STRATEGY_7_BASE_ORDER_COST: expected number, got 'bad'"
    assert "PAPER_15M_TARGET_PROFIT" not in warnings


def test_build_config_uses_paper_strategy_ids_when_present():
    cfg = build_config_from_env_values({"STRATEGY_ID": "2", "PAPER_STRATEGY_IDS": "6,2,6,1"})

    assert cfg.strategy_id == 2
    assert cfg.paper_strategy_ids == [6, 2, 1]


def test_build_config_keeps_paper_and_live_strategy_ids_separate_from_unified_strategy_ids():
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "2",
            "STRATEGY_IDS": "8,7,8,3",
            "PAPER_STRATEGY_IDS": "1,2",
            "LIVE_STRATEGY_IDS": "6",
        }
    )

    assert cfg.strategy_ids == [8, 7, 3]
    assert cfg.paper_strategy_ids == [1, 2]
    assert cfg.live_strategy_ids == [6]
    assert list(cfg.live_profiles) == [6]


def test_build_config_uses_unified_strategy_ids_when_split_strategy_ids_are_missing():
    cfg = build_config_from_env_values({"STRATEGY_ID": "2", "STRATEGY_IDS": "8,7,8,3"})

    assert cfg.strategy_ids == [8, 7, 3]
    assert cfg.paper_strategy_ids == [8, 7, 3]
    assert cfg.live_strategy_ids == [8, 7, 3]


def test_build_config_falls_back_to_strategy_id_for_paper():
    cfg = build_config_from_env_values({"STRATEGY_ID": "5"})

    assert cfg.paper_strategy_ids == [5]


def test_build_config_uses_live_strategy_ids_when_present():
    cfg = build_config_from_env_values({"STRATEGY_ID": "2", "LIVE_STRATEGY_IDS": "6,2,6,1"})

    assert cfg.strategy_id == 2
    assert cfg.live_strategy_ids == [6, 2, 1]


def test_build_config_falls_back_to_strategy_id_for_live_strategy():
    cfg = build_config_from_env_values({"STRATEGY_ID": "5"})

    assert cfg.live_strategy_ids == [5]


def test_build_config_applies_shared_strategy_profile_overrides_to_paper_and_live():
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "2",
            "TARGET_PROFIT": "1.1",
            "SIGNAL_WEAK_SIGNAL_MODE": "force",
            "PAPER_STRATEGY_IDS": "5,2",
            "LIVE_STRATEGY_IDS": "5,2",
            "STRATEGY_5_TARGET_PROFIT": "0.8",
            "STRATEGY_5_BASE_ORDER_COST": "12.5",
            "STRATEGY_5_SIGNAL_WEAK_SIGNAL_MODE": "force",
            "STRATEGY_5_STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS": "18",
        }
    )

    assert cfg.live_strategy_ids == [5, 2]
    assert cfg.signal_weak_signal_mode == "SKIP"
    assert 5 not in cfg.paper_strategy_profiles
    profile = cfg.live_profiles[5]
    assert profile.strategy_id == 5
    assert profile.base_order_cost == 12.5
    assert profile.signal_weak_signal_mode == "FORCE"
    assert profile.strategy7_confirm_before_entry_seconds == 18
    assert cfg.live_profiles[2].signal_weak_signal_mode == "SKIP"


def test_build_config_ignores_removed_bet_sizing_mode_keys():
    warnings = collect_config_warnings(
        {
            "STRATEGY_7_BET_SIZING_MODE": "FLAT_BASE_COST",
            "STRATEGY_2_BET_SIZING_MODE": "FIXED_BASE_COST",
            "STRATEGY_3_BET_SIZING_MODE": "TARGET_PROFIT",
        }
    )
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "7",
            "PAPER_USE_LIVE_PROFILES": "false",
            "PAPER_STRATEGY_IDS": "7",
            "LIVE_STRATEGY_IDS": "7",
            "BET_SIZING_MODE": "TARGET_PROFIT",
            "STRATEGY_7_BET_SIZING_MODE": "FLAT_BASE_COST",
        }
    )

    assert "STRATEGY_7_BET_SIZING_MODE" not in warnings
    assert "STRATEGY_2_BET_SIZING_MODE" not in warnings
    assert "STRATEGY_3_BET_SIZING_MODE" not in warnings
    assert not hasattr(cfg, "bet_sizing_mode")
    assert 7 not in cfg.paper_strategy_profiles
    assert not hasattr(cfg.live_profiles[7], "bet_sizing_mode")


def test_build_config_ignores_old_mode_specific_strategy_profile_keys():
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "7",
            "PAPER_USE_LIVE_PROFILES": "false",
            "PAPER_STRATEGY_IDS": "7",
            "LIVE_STRATEGY_IDS": "7",
            "PAPER_STRATEGY_7_BET_SIZING_MODE": "TARGET_PROFIT",
            "LIVE_STRATEGY_7_BET_SIZING_MODE": "FLAT_BASE_COST",
        }
    )

    assert 7 not in cfg.paper_strategy_profiles
    assert not hasattr(cfg.live_profiles[7], "bet_sizing_mode")


def test_build_config_ignores_mode_specific_profile_values_and_uses_shared_strategy_values():
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "7",
            "PAPER_USE_LIVE_PROFILES": "false",
            "PAPER_STRATEGY_IDS": "7",
            "LIVE_STRATEGY_IDS": "7",
            "TARGET_PROFIT": "1.0",
            "BASE_ORDER_COST": "2",
            "MAX_STAKE": "20",
            "PAPER_STRATEGY_7_BASE_ORDER_COST": "3",
            "LIVE_STRATEGY_7_BASE_ORDER_COST": "4",
            "PAPER_STRATEGY_7_MAX_STAKE": "30",
            "LIVE_STRATEGY_7_MAX_STAKE": "40",
            "STRATEGY_7_TARGET_PROFIT": "0.8",
            "STRATEGY_7_BASE_ORDER_COST": "1.2",
            "STRATEGY_7_MAX_STAKE": "60",
            "STRATEGY_7_MIN_ENTRY_PRICE": "0.50",
            "STRATEGY_7_MAX_ENTRY_PRICE": "0.54",
            "STRATEGY_7_LIVE_MAX_PRICE_IMPROVEMENT": "0.04",
            "STRATEGY_7_MAX_CONSECUTIVE_LOSSES": "7",
            "STRATEGY_7_OFI_THRESHOLD": "0.58",
            "STRATEGY_7_MOMENTUM_THRESHOLD": "0.008",
        }
    )

    assert cfg.base_order_cost == 1.0
    assert cfg.max_stake is None
    live = cfg.live_profiles[7]
    assert 7 not in cfg.paper_strategy_profiles
    assert live.base_order_cost == 1.2
    assert live.max_stake == 60.0
    assert live.min_entry_price == 0.50
    assert live.max_entry_price == 0.54
    assert live.live_max_price_improvement == 0.04
    assert live.max_consecutive_losses == 7
    assert live.strategy7_ofi_threshold == 0.58
    assert live.strategy7_momentum_threshold == 0.008


def test_build_config_supports_fok_fallback_to_fak_toggle():
    default_cfg = build_config_from_env_values({})
    disabled_cfg = build_config_from_env_values({"POLYMARKET_FOK_FALLBACK_TO_FAK": "false"})

    assert default_cfg.live_fok_fallback_to_fak is True
    assert disabled_cfg.live_fok_fallback_to_fak is False


def test_build_config_supports_order_book_depth_precheck_toggle():
    default_cfg = build_config_from_env_values({})
    disabled_cfg = build_config_from_env_values({"POLYMARKET_PRECHECK_ORDER_BOOK_DEPTH": "false"})

    assert default_cfg.live_precheck_order_book_depth is True
    assert disabled_cfg.live_precheck_order_book_depth is False


def test_build_config_applies_strategy_overrides_with_split_strategy_ids():
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "3",
            "PAPER_STRATEGY_IDS": "3,7",
            "LIVE_STRATEGY_IDS": "3,7",
            "MIN_STAKE": "1",
            "MAX_STAKE": "20",
            "BASE_ORDER_COST": "2",
            "LIVE_STRATEGY_7_BASE_ORDER_COST": "5.5",
            "PAPER_STRATEGY_3_BASE_ORDER_COST": "4.0",
            "STRATEGY_7_BASE_ORDER_COST": "5.5",
            "STRATEGY_7_MIN_STAKE": "0.5",
            "STRATEGY_7_MAX_STAKE": "99",
            "STRATEGY_7_MIN_ENTRY_PRICE": "0.42",
            "STRATEGY_7_MAX_ENTRY_PRICE": "0.55",
            "STRATEGY_7_OFI_THRESHOLD": "0.58",
            "STRATEGY_3_BASE_ORDER_COST": "4.0",
            "STRATEGY_3_MIN_STAKE": "3",
            "STRATEGY_3_MAX_STAKE": "30",
        }
    )

    assert cfg.live_strategy_ids == [3, 7]
    assert cfg.live_profiles[3].base_order_cost == 4.0
    assert cfg.live_profiles[7].base_order_cost == 5.5
    assert cfg.live_profiles[7].min_stake == 0.5
    assert cfg.live_profiles[7].max_stake == 99.0
    assert cfg.live_profiles[7].min_entry_price == 0.42
    assert cfg.live_profiles[7].max_entry_price == 0.55
    assert cfg.live_profiles[7].strategy7_ofi_threshold == 0.58
    assert cfg.live_profiles[7].strategy7_max_momentum_delta is None
    assert 3 not in cfg.paper_strategy_profiles
    assert 7 not in cfg.paper_strategy_profiles


def test_build_config_supports_strategy7_max_momentum_delta_overrides():
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "7",
            "PAPER_USE_LIVE_PROFILES": "false",
            "PAPER_STRATEGY_IDS": "7",
            "LIVE_STRATEGY_IDS": "7",
            "STRATEGY7_MAX_MOMENTUM_DELTA": "0.015",
            "PAPER_STRATEGY_7_STRATEGY7_MAX_MOMENTUM_DELTA": "0.012",
            "LIVE_STRATEGY_7_STRATEGY7_MAX_MOMENTUM_DELTA": "0.018",
            "STRATEGY_7_MAX_MOMENTUM_DELTA": "0.02",
        }
    )

    assert cfg.strategy7_max_momentum_delta is None
    assert 7 not in cfg.paper_strategy_profiles
    assert cfg.live_profiles[7].strategy7_max_momentum_delta == 0.02


def test_build_config_ignores_invalid_paper_strategy_entries():
    cfg = build_config_from_env_values({"STRATEGY_ID": "3", "PAPER_STRATEGY_IDS": "6,x,9,2,6"})

    assert cfg.paper_strategy_ids == [6, 9, 2]


def test_build_config_accepts_strategy_9_in_strategy_lists():
    cfg = build_config_from_env_values({"STRATEGY_ID": "3", "PAPER_STRATEGY_IDS": "9,8,6", "LIVE_STRATEGY_IDS": "9"})

    assert cfg.paper_strategy_ids == [8, 6]
    assert cfg.live_strategy_ids == [9]


def test_build_config_excludes_live_strategies_from_paper_and_uses_one_shared_profile():
    cfg = build_config_from_env_values(
        {
            "TRADE_MODE": "both",
            "PAPER_STRATEGY_IDS": "9,11,12",
            "LIVE_STRATEGY_IDS": "9,11,12",
            "STRATEGY_9_BASE_ORDER_COST": "1.2",
            "STRATEGY_9_MAX_ENTRY_PRICE": "0.55",
            "STRATEGY_9_STABILITY_SAMPLE_COUNT": "1",
            "STRATEGY_9_STABILITY_REQUIRED_COUNT": "1",
            "STRATEGY_11_MIN_EDGE": "0.04",
            "STRATEGY_11_EDGE_BUFFER": "0.008",
            "STRATEGY_11_MIN_PROBABILITY": "0.56",
            "STRATEGY_12_MAX_ENTRY_PRICE": "0.56",
            "STRATEGY_12_MIN_EDGE": "0.006",
            "STRATEGY_12_OFI_THRESHOLD": "0.58",
        }
    )

    assert cfg.paper_strategy_ids == []
    assert cfg.live_strategy_ids == [9, 11, 12]
    for strategy_id in cfg.live_strategy_ids:
        assert strategy_id not in cfg.paper_strategy_profiles
    assert cfg.live_profiles[9].base_order_cost == pytest.approx(1.2)
    assert cfg.live_profiles[9].max_entry_price == pytest.approx(0.55)
    assert cfg.live_profiles[9].strategy9_stability_sample_count == 1
    assert cfg.live_profiles[9].strategy9_stability_required_count == 1
    assert cfg.live_profiles[11].strategy11_min_edge == pytest.approx(0.04)
    assert cfg.live_profiles[11].strategy11_edge_buffer == pytest.approx(0.008)
    assert cfg.live_profiles[11].strategy11_min_probability == pytest.approx(0.56)
    assert cfg.live_profiles[12].max_entry_price == pytest.approx(0.56)
    assert cfg.live_profiles[12].strategy11_min_edge == pytest.approx(0.006)
    assert cfg.live_profiles[12].strategy7_ofi_threshold == pytest.approx(0.58)


def test_build_config_accepts_strategy10_and_reads_edge_values():
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "10",
            "PAPER_USE_LIVE_PROFILES": "false",
            "PAPER_STRATEGY_IDS": "10,7",
            "LIVE_STRATEGY_IDS": "10",
            "STRATEGY10_MIN_EDGE": "0.045",
            "STRATEGY10_OFI_WEIGHT": "0.12",
            "STRATEGY10_MOMENTUM_WEIGHT": "1.4",
            "STRATEGY10_EDGE_BUFFER": "0.01",
            "PAPER_STRATEGY_10_STRATEGY10_MIN_EDGE": "0.06",
            "PAPER_STRATEGY_10_STRATEGY10_MIN_MOMENTUM_DELTA": "-0.02",
            "PAPER_STRATEGY_10_STRATEGY10_MAX_MOMENTUM_DELTA": "0.02",
            "PAPER_STRATEGY_10_STRATEGY10_DOWN_MIN_EDGE": "0.07",
            "STRATEGY_10_MIN_EDGE": "0.07",
            "STRATEGY_10_OFI_WEIGHT": "0.12",
            "STRATEGY_10_MOMENTUM_WEIGHT": "1.4",
            "STRATEGY_10_EDGE_BUFFER": "0.01",
            "STRATEGY_10_CONFIRM_BEFORE_ENTRY_SECONDS": "1",
        }
    )

    assert cfg.strategy_id == 10
    assert cfg.paper_strategy_ids == [7]
    assert cfg.live_strategy_ids == [10]
    assert cfg.strategy10_min_edge == 0.04
    assert cfg.strategy10_ofi_weight == 0.08
    assert cfg.strategy10_momentum_weight == 1.0
    assert cfg.strategy10_edge_buffer == 0.005
    assert 10 not in cfg.paper_strategy_profiles
    assert cfg.live_profiles[10].strategy10_min_edge == 0.07
    assert cfg.live_profiles[10].strategy10_min_momentum_delta is None
    assert cfg.live_profiles[10].strategy10_max_momentum_delta is None


def test_build_config_accepts_strategy11_and_reads_probability_values():
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "11",
            "PAPER_STRATEGY_IDS": "11,10",
            "LIVE_STRATEGY_IDS": "11",
            "STRATEGY_11_MIN_EDGE": "0.06",
            "STRATEGY_11_EDGE_BUFFER": "0.01",
            "STRATEGY_11_VOLATILITY_BPS_PER_SQRT_MINUTE": "18",
            "STRATEGY_11_MIN_PROBABILITY": "0.57",
            "STRATEGY_11_MAX_PROBABILITY": "0.93",
            "STRATEGY_11_CONFIRM_BEFORE_ENTRY_SECONDS": "2",
        }
    )

    assert cfg.strategy_id == 11
    assert cfg.paper_strategy_ids == [10]
    assert cfg.live_strategy_ids == [11]
    assert 11 not in cfg.paper_strategy_profiles
    assert cfg.live_profiles[11].strategy11_min_edge == 0.06
    assert cfg.live_profiles[11].strategy11_edge_buffer == 0.01
    assert cfg.live_profiles[11].strategy11_volatility_bps_per_sqrt_minute == 18.0
    assert cfg.live_profiles[11].strategy11_min_probability == 0.57
    assert cfg.live_profiles[11].strategy11_max_probability == 0.93
    assert cfg.live_profiles[11].strategy11_confirm_before_entry_seconds == 2


def test_build_config_reads_strategy11_paper_trial_overrides_without_live_relaxation():
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "11",
            "PAPER_STRATEGY_IDS": "11",
            "LIVE_STRATEGY_IDS": "7,10",
            "STRATEGY_11_MIN_EDGE": "0.04",
            "STRATEGY_11_EDGE_BUFFER": "0.005",
            "STRATEGY_11_VOLATILITY_BPS_PER_SQRT_MINUTE": "18",
            "STRATEGY_11_MIN_PROBABILITY": "0.55",
            "STRATEGY_11_CONFIRM_BEFORE_ENTRY_SECONDS": "2",
            "PAPER_STRATEGY_11_MIN_EDGE": "0.005",
            "PAPER_STRATEGY_11_EDGE_BUFFER": "0.0",
            "PAPER_STRATEGY_11_VOLATILITY_BPS_PER_SQRT_MINUTE": "24",
            "PAPER_STRATEGY_11_MIN_PROBABILITY": "0.54",
            "PAPER_STRATEGY_11_MAX_ENTRY_PRICE": "0.56",
            "PAPER_STRATEGY_11_CONFIRM_BEFORE_ENTRY_SECONDS": "0",
        }
    )

    assert cfg.live_strategy_ids == [7, 10]
    assert cfg.paper_strategy_profiles[11].strategy11_min_edge == 0.04
    assert cfg.paper_strategy_profiles[11].strategy11_edge_buffer == 0.005
    assert cfg.paper_strategy_profiles[11].strategy11_volatility_bps_per_sqrt_minute == 18.0
    assert cfg.paper_strategy_profiles[11].strategy11_min_probability == 0.55
    assert cfg.paper_strategy_profiles[11].max_entry_price == 0.56
    assert cfg.paper_strategy_profiles[11].strategy11_confirm_before_entry_seconds == 2
    assert cfg.live_profiles[10].strategy11_min_edge == 0.04
    assert cfg.live_profiles[10].strategy11_edge_buffer == 0.005
    assert cfg.live_profiles[10].strategy11_volatility_bps_per_sqrt_minute == 18.0
    assert cfg.live_profiles[10].strategy11_min_probability == 0.55


def test_build_config_ignores_mode_specific_strategy11_profile_overrides():
    cfg = build_config_from_env_values(
        {
            "TRADE_MODE": "both",
            "STRATEGY_ID": "7",
            "PAPER_STRATEGY_IDS": "7,9,10,11,12",
            "LIVE_STRATEGY_IDS": "11",
            "STRATEGY_11_BASE_ORDER_COST": "5",
            "STRATEGY_11_MAX_ENTRY_PRICE": "0.54",
            "STRATEGY_11_MIN_EDGE": "0.04",
            "STRATEGY_11_EDGE_BUFFER": "0.005",
            "STRATEGY_11_MIN_PROBABILITY": "0.55",
            "STRATEGY_11_VOLATILITY_BPS_PER_SQRT_MINUTE": "18",
            "STRATEGY_11_CONFIRM_BEFORE_ENTRY_SECONDS": "2",
            "PAPER_STRATEGY_11_MAX_ENTRY_PRICE": "0.56",
            "PAPER_STRATEGY_11_MIN_EDGE": "0.005",
            "PAPER_STRATEGY_11_EDGE_BUFFER": "0.0",
            "PAPER_STRATEGY_11_MIN_PROBABILITY": "0.54",
            "PAPER_STRATEGY_11_VOLATILITY_BPS_PER_SQRT_MINUTE": "24",
            "PAPER_STRATEGY_11_CONFIRM_BEFORE_ENTRY_SECONDS": "0",
            "LIVE_STRATEGY_11_BASE_ORDER_COST": "2",
            "LIVE_STRATEGY_11_MAX_ENTRY_PRICE": "0.54",
            "LIVE_STRATEGY_11_MIN_EDGE": "0.04",
            "LIVE_STRATEGY_11_EDGE_BUFFER": "0.008",
            "LIVE_STRATEGY_11_MIN_PROBABILITY": "0.56",
            "LIVE_STRATEGY_11_VOLATILITY_BPS_PER_SQRT_MINUTE": "18",
            "LIVE_STRATEGY_11_CONFIRM_BEFORE_ENTRY_SECONDS": "2",
        }
    )

    live = cfg.live_profiles[11]

    assert 11 not in cfg.paper_strategy_ids
    assert 11 not in cfg.paper_strategy_profiles
    assert live.base_order_cost == 5.0
    assert live.max_entry_price == 0.54
    assert live.strategy11_min_edge == 0.04
    assert live.strategy11_edge_buffer == 0.005
    assert live.strategy11_min_probability == 0.55
    assert live.strategy11_volatility_bps_per_sqrt_minute == 18.0
    assert live.strategy11_confirm_before_entry_seconds == 2


def test_build_config_accepts_strategy12_hybrid_short_keys():
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "12",
            "PAPER_STRATEGY_IDS": "12,7",
            "LIVE_STRATEGY_IDS": "7,10",
            "PAPER_STRATEGY_12_MIN_EDGE": "0.006",
            "PAPER_STRATEGY_12_EDGE_BUFFER": "0.001",
            "PAPER_STRATEGY_12_VOLATILITY_BPS_PER_SQRT_MINUTE": "24",
            "PAPER_STRATEGY_12_MIN_PROBABILITY": "0.54",
            "PAPER_STRATEGY_12_MAX_PROBABILITY": "0.93",
            "PAPER_STRATEGY_12_CONFIRM_BEFORE_ENTRY_SECONDS": "0",
            "PAPER_STRATEGY_12_OFI_THRESHOLD": "0.58",
            "PAPER_STRATEGY_12_MOMENTUM_THRESHOLD": "0.008",
            "PAPER_STRATEGY_12_MAX_MOMENTUM_DELTA": "0.12",
            "PAPER_STRATEGY_12_MIN_SIGNAL_GAP": "0.01",
        }
    )

    profile = cfg.paper_strategy_profiles[12]

    assert cfg.strategy_id == 12
    assert cfg.paper_strategy_ids == [12]
    assert cfg.live_strategy_ids == [7, 10]
    assert profile.strategy11_min_edge == pytest.approx(0.04)
    assert profile.strategy11_edge_buffer == pytest.approx(0.005)
    assert profile.strategy11_volatility_bps_per_sqrt_minute == pytest.approx(18.0)
    assert profile.strategy11_min_probability == pytest.approx(0.55)
    assert profile.strategy11_max_probability == pytest.approx(0.95)
    assert profile.strategy11_confirm_before_entry_seconds == 2
    assert profile.strategy7_ofi_threshold == pytest.approx(0.7)
    assert profile.strategy7_momentum_threshold == pytest.approx(0.025)
    assert profile.strategy7_max_momentum_delta is None
    assert profile.strategy7_min_signal_gap == pytest.approx(0.03)


def test_build_config_reads_strategy7_dynamic_sizing_values_from_strategy_profile_only():
    cfg = build_config_from_env_values(
        {
            "STRATEGY7_DYNAMIC_SIZING_ENABLED": "true",
            "STRATEGY7_SIZING_REFERENCE_PRICE": "0.51",
            "STRATEGY7_SIZING_PRICE_STEP": "0.02",
            "STRATEGY7_SIZING_PRICE_STEP_REDUCTION": "0.15",
            "STRATEGY7_SIZING_MIN_MULTIPLIER": "0.45",
            "STRATEGY7_SIZING_MAX_MULTIPLIER": "1.10",
            "STRATEGY7_SIZING_STRONG_SIGNAL_GAP": "0.03",
            "STRATEGY7_SIZING_STRONG_SIGNAL_BOOST": "0.25",
            "PAPER_STRATEGY_IDS": "7",
            "PAPER_STRATEGY_7_STRATEGY7_DYNAMIC_SIZING_ENABLED": "false",
            "PAPER_STRATEGY_7_STRATEGY7_SIZING_MIN_MULTIPLIER": "0.60",
            "STRATEGY_7_DYNAMIC_SIZING_ENABLED": "true",
            "STRATEGY_7_SIZING_REFERENCE_PRICE": "0.51",
            "STRATEGY_7_SIZING_PRICE_STEP": "0.02",
            "STRATEGY_7_SIZING_PRICE_STEP_REDUCTION": "0.15",
            "STRATEGY_7_SIZING_MIN_MULTIPLIER": "0.45",
            "STRATEGY_7_SIZING_MAX_MULTIPLIER": "1.10",
            "STRATEGY_7_SIZING_STRONG_SIGNAL_GAP": "0.03",
            "STRATEGY_7_SIZING_STRONG_SIGNAL_BOOST": "0.25",
        }
    )

    assert cfg.strategy7_dynamic_sizing_enabled is False
    assert cfg.strategy7_sizing_reference_price == 0.50
    assert cfg.paper_strategy_profiles[7].strategy7_dynamic_sizing_enabled is True
    assert cfg.paper_strategy_profiles[7].strategy7_sizing_reference_price == 0.51
    assert cfg.paper_strategy_profiles[7].strategy7_sizing_price_step == 0.02
    assert cfg.paper_strategy_profiles[7].strategy7_sizing_price_step_reduction == 0.15
    assert cfg.paper_strategy_profiles[7].strategy7_sizing_min_multiplier == 0.45
    assert cfg.paper_strategy_profiles[7].strategy7_sizing_max_multiplier == 1.10
    assert cfg.paper_strategy_profiles[7].strategy7_sizing_strong_signal_gap == 0.03
    assert cfg.paper_strategy_profiles[7].strategy7_sizing_strong_signal_boost == 0.25


def test_build_config_reads_strategy9_dynamic_sizing_values_from_strategy_profile_only():
    cfg = build_config_from_env_values(
        {
            "STRATEGY9_DYNAMIC_SIZING_ENABLED": "true",
            "STRATEGY9_SIZING_REFERENCE_PRICE": "0.51",
            "STRATEGY9_SIZING_PRICE_STEP": "0.02",
            "STRATEGY9_SIZING_PRICE_STEP_REDUCTION": "0.15",
            "STRATEGY9_SIZING_MIN_MULTIPLIER": "0.45",
            "STRATEGY9_SIZING_MAX_MULTIPLIER": "1.10",
            "STRATEGY9_SIZING_STRONG_SIGNAL_GAP": "0.03",
            "STRATEGY9_SIZING_STRONG_SIGNAL_BOOST": "0.25",
            "PAPER_STRATEGY_IDS": "9",
            "PAPER_STRATEGY_9_STRATEGY9_DYNAMIC_SIZING_ENABLED": "false",
            "PAPER_STRATEGY_9_STRATEGY9_SIZING_MIN_MULTIPLIER": "0.60",
            "STRATEGY_9_DYNAMIC_SIZING_ENABLED": "true",
            "STRATEGY_9_SIZING_REFERENCE_PRICE": "0.51",
            "STRATEGY_9_SIZING_PRICE_STEP": "0.02",
            "STRATEGY_9_SIZING_PRICE_STEP_REDUCTION": "0.15",
            "STRATEGY_9_SIZING_MIN_MULTIPLIER": "0.45",
            "STRATEGY_9_SIZING_MAX_MULTIPLIER": "1.10",
            "STRATEGY_9_SIZING_STRONG_SIGNAL_GAP": "0.03",
            "STRATEGY_9_SIZING_STRONG_SIGNAL_BOOST": "0.25",
        }
    )

    assert cfg.strategy9_dynamic_sizing_enabled is False
    assert cfg.strategy9_sizing_reference_price == 0.50
    assert cfg.paper_strategy_profiles[9].strategy9_dynamic_sizing_enabled is True
    assert cfg.paper_strategy_profiles[9].strategy9_sizing_reference_price == 0.51
    assert cfg.paper_strategy_profiles[9].strategy9_sizing_price_step == 0.02
    assert cfg.paper_strategy_profiles[9].strategy9_sizing_price_step_reduction == 0.15
    assert cfg.paper_strategy_profiles[9].strategy9_sizing_min_multiplier == 0.45
    assert cfg.paper_strategy_profiles[9].strategy9_sizing_max_multiplier == 1.10
    assert cfg.paper_strategy_profiles[9].strategy9_sizing_strong_signal_gap == 0.03
    assert cfg.paper_strategy_profiles[9].strategy9_sizing_strong_signal_boost == 0.25


def test_build_config_supports_btc_15m_market_timeframe():
    cfg = build_config_from_env_values({"MARKET_TIMEFRAME": "15m"})

    assert cfg.market_timeframe == "15m"
    assert cfg.series_id == 10192
    assert cfg.series_slug == "btc-up-or-down-15m"


def test_build_config_defaults_invalid_market_timeframe_to_btc_5m():
    cfg = build_config_from_env_values({"MARKET_TIMEFRAME": "7m"})

    assert cfg.market_timeframe == "5m"
    assert cfg.series_id == 10684
    assert cfg.series_slug == "btc-up-or-down-5m"


def test_build_config_ignores_global_open_delay_override():
    cfg = build_config_from_env_values({"OPEN_DELAY_SECONDS": "15"})

    assert cfg.open_delay_seconds == 5


def test_build_config_supports_runtime_poll_interval_overrides():
    cfg = build_config_from_env_values(
        {
            "POLL_INTERVAL_SECONDS": "2",
            "FAST_POLL_INTERVAL_SECONDS": "1.5",
            "NEAR_ENTRY_POLL_WINDOW_SECONDS": "20",
        }
    )

    assert cfg.poll_interval_seconds == 2
    assert cfg.fast_poll_interval_seconds == 1.5
    assert cfg.near_entry_poll_window_seconds == 20


def test_build_config_parses_enabled_paper_timeframes_and_ignores_profile_param_values():
    cfg = build_config_from_env_values(
        {
            "TRADE_MODE": "paper",
            "PAPER_TIMEFRAMES": "5m,15m",
            "PAPER_5M_STRATEGY_ID": "5",
            "PAPER_5M_STRATEGY_IDS": "5,6",
            "PAPER_5M_TARGET_PROFIT": "0.8",
            "PAPER_15M_STRATEGY_ID": "2",
            "PAPER_15M_STRATEGY_IDS": "1,2,7",
            "PAPER_15M_TARGET_PROFIT": "1.0",
        }
    )

    assert cfg.paper_timeframes == ["5m", "15m"]
    assert cfg.paper_profiles["5m"].strategy_id == 5
    assert cfg.paper_profiles["5m"].paper_strategy_ids == [5, 6]
    assert cfg.paper_profiles["15m"].strategy_id == 2
    assert cfg.paper_profiles["15m"].paper_strategy_ids == [1, 2, 7]


def test_build_config_keeps_live_single_timeframe_when_paper_profiles_exist():
    cfg = build_config_from_env_values(
        {
            "TRADE_MODE": "live",
            "MARKET_TIMEFRAME": "15m",
            "PAPER_TIMEFRAMES": "5m,15m",
            "PAPER_5M_STRATEGY_IDS": "5,6",
            "PAPER_15M_STRATEGY_IDS": "1,2",
        }
    )

    assert cfg.market_timeframe == "15m"
    assert cfg.series_slug == "btc-up-or-down-15m"
    assert cfg.paper_timeframes == ["5m", "15m"]


def test_build_config_uses_single_timeframe_strategy_selection_when_paper_timeframes_missing():
    cfg = build_config_from_env_values(
        {
            "TRADE_MODE": "paper",
            "MARKET_TIMEFRAME": "15m",
            "STRATEGY_ID": "7",
            "PAPER_STRATEGY_IDS": "7,6",
            "TARGET_PROFIT": "1.1",
        }
    )

    assert cfg.paper_timeframes == ["15m"]
    assert cfg.paper_profiles["15m"].strategy_id == 7
    assert cfg.paper_profiles["15m"].paper_strategy_ids == [7, 6]


def test_direct_app_config_constructor_aligns_paper_timeframes_with_market_timeframe():
    cfg = AppConfig(market_timeframe="15m")

    assert cfg.paper_timeframes == ["15m"]
    assert cfg.paper_profiles["15m"].timeframe == "15m"


def test_direct_app_config_constructor_aligns_live_strategy_ids_with_strategy_id():
    cfg = AppConfig(strategy_id=5)

    assert cfg.live_strategy_ids == [5]
    assert list(cfg.live_profiles) == [5]
    assert cfg.live_profiles[5].strategy_id == 5
