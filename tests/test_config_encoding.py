from __future__ import annotations

from pathlib import Path

from config import AppConfig, build_config_from_env_values, collect_config_warnings, load_env_file_values


def test_load_env_file_values_reads_gbk_encoded_dashboard_file(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_bytes('COMMENT=abc\nMAX_STAKE=9.5\n'.encode('gbk'))

    values = load_env_file_values(env_file)

    assert values == {'COMMENT': 'abc', 'MAX_STAKE': '9.5'}


def test_build_config_from_gbk_encoded_dashboard_file(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_bytes('TRADE_MODE=paper\nMAX_STAKE=9.5\nPOLYMARKET_FUNDER=addr\n'.encode('gbk'))

    values = load_env_file_values(env_file)
    cfg = build_config_from_env_values(values)

    assert cfg.trade_mode == 'paper'
    assert cfg.max_stake == 9.5
    assert cfg.live_funder == 'addr'


def test_collect_config_warnings_reports_invalid_scalar_values():
    warnings = collect_config_warnings(
        {
            'MAX_STAKE': 'abc',
            'WS_ENABLED': 'maybe',
            'STRATEGY_ID': '10',
            'MARKET_TIMEFRAME': '7m',
            'TARGET_PROFIT': '1.2',
        }
    )

    assert warnings['MAX_STAKE'] == "Invalid value for MAX_STAKE: expected number, got 'abc'"
    assert warnings['WS_ENABLED'] == "Invalid value for WS_ENABLED: expected true/false, got 'maybe'"
    assert warnings['STRATEGY_ID'] == "Invalid value for STRATEGY_ID: expected strategy id 1-9, got '10'"
    assert warnings['MARKET_TIMEFRAME'] == "Invalid value for MARKET_TIMEFRAME: expected one of 5m, 15m, got '7m'"
    assert 'TARGET_PROFIT' not in warnings


def test_collect_config_warnings_reports_profile_and_strategy_list_values():
    warnings = collect_config_warnings(
        {
            'STRATEGY_IDS': '2,x,9',
            'PAPER_15M_TARGET_PROFIT': 'oops',
            'LIVE_STRATEGY_7_BASE_ORDER_COST': 'bad',
        }
    )

    assert warnings['STRATEGY_IDS'] == "Invalid entries for STRATEGY_IDS ignored: x"
    assert warnings['PAPER_15M_TARGET_PROFIT'] == "Invalid value for PAPER_15M_TARGET_PROFIT: expected number, got 'oops'"
    assert warnings['LIVE_STRATEGY_7_BASE_ORDER_COST'] == "Invalid value for LIVE_STRATEGY_7_BASE_ORDER_COST: expected number, got 'bad'"


def test_build_config_uses_paper_strategy_ids_when_present():
    cfg = build_config_from_env_values({'STRATEGY_ID': '2', 'PAPER_STRATEGY_IDS': '6,2,6,1'})

    assert cfg.strategy_id == 2
    assert cfg.paper_strategy_ids == [6, 2, 1]


def test_build_config_keeps_paper_and_live_strategy_ids_separate_from_legacy_strategy_ids():
    cfg = build_config_from_env_values(
        {
            'STRATEGY_ID': '2',
            'STRATEGY_IDS': '8,7,8,3',
            'PAPER_STRATEGY_IDS': '1,2',
            'LIVE_STRATEGY_IDS': '6',
        }
    )

    assert cfg.strategy_ids == [8, 7, 3]
    assert cfg.paper_strategy_ids == [1, 2]
    assert cfg.live_strategy_ids == [6]
    assert list(cfg.live_profiles) == [6]


def test_build_config_uses_legacy_strategy_ids_only_when_split_strategy_ids_are_missing():
    cfg = build_config_from_env_values({'STRATEGY_ID': '2', 'STRATEGY_IDS': '8,7,8,3'})

    assert cfg.strategy_ids == [8, 7, 3]
    assert cfg.paper_strategy_ids == [8, 7, 3]
    assert cfg.live_strategy_ids == [8, 7, 3]


def test_build_config_falls_back_to_strategy_id_for_paper():
    cfg = build_config_from_env_values({'STRATEGY_ID': '5'})

    assert cfg.paper_strategy_ids == [5]


def test_build_config_uses_live_strategy_ids_when_present():
    cfg = build_config_from_env_values({'STRATEGY_ID': '2', 'LIVE_STRATEGY_IDS': '6,2,6,1'})

    assert cfg.strategy_id == 2
    assert cfg.live_strategy_ids == [6, 2, 1]


def test_build_config_falls_back_to_strategy_id_for_live_strategy():
    cfg = build_config_from_env_values({'STRATEGY_ID': '5'})

    assert cfg.live_strategy_ids == [5]


def test_build_config_applies_live_strategy_profile_overrides():
    cfg = build_config_from_env_values(
        {
            'STRATEGY_ID': '2',
            'TARGET_PROFIT': '1.1',
            'SIGNAL_WEAK_SIGNAL_MODE': 'skip',
            'LIVE_STRATEGY_IDS': '5,2',
            'LIVE_STRATEGY_5_TARGET_PROFIT': '0.8',
            'LIVE_STRATEGY_5_BASE_ORDER_COST': '12.5',
            'LIVE_STRATEGY_5_SIGNAL_WEAK_SIGNAL_MODE': 'force',
            'LIVE_STRATEGY_5_STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS': '18',
        }
    )

    assert cfg.live_strategy_ids == [5, 2]
    assert cfg.live_profiles[5].strategy_id == 5
    assert cfg.live_profiles[5].target_profit == 0.8
    assert cfg.live_profiles[5].base_order_cost == 12.5
    assert cfg.live_profiles[5].signal_weak_signal_mode == 'FORCE'
    assert cfg.live_profiles[5].strategy7_confirm_before_entry_seconds == 18
    assert cfg.live_profiles[2].strategy_id == 2
    assert cfg.live_profiles[2].target_profit == 1.1
    assert cfg.live_profiles[2].signal_weak_signal_mode == 'SKIP'


def test_build_config_accepts_flat_base_cost_for_strategy_profiles():
    warnings = collect_config_warnings(
        {
            'BET_SIZING_MODE': 'FLAT_BASE_COST',
            'PAPER_STRATEGY_7_BET_SIZING_MODE': 'FLAT_BASE_COST',
            'LIVE_STRATEGY_7_BET_SIZING_MODE': 'FLAT_BASE_COST',
        }
    )
    cfg = build_config_from_env_values(
        {
            'STRATEGY_ID': '7',
            'PAPER_STRATEGY_IDS': '7',
            'LIVE_STRATEGY_IDS': '7',
            'BET_SIZING_MODE': 'FLAT_BASE_COST',
            'PAPER_STRATEGY_7_BET_SIZING_MODE': 'FLAT_BASE_COST',
            'LIVE_STRATEGY_7_BET_SIZING_MODE': 'FLAT_BASE_COST',
        }
    )

    assert 'BET_SIZING_MODE' not in warnings
    assert cfg.bet_sizing_mode == 'FLAT_BASE_COST'
    assert cfg.paper_strategy_profiles[7].bet_sizing_mode == 'FLAT_BASE_COST'
    assert cfg.live_profiles[7].bet_sizing_mode == 'FLAT_BASE_COST'


def test_build_config_applies_strategy_overrides_with_split_strategy_ids_and_global_stake_cap():
    cfg = build_config_from_env_values(
        {
            'STRATEGY_ID': '3',
            'PAPER_STRATEGY_IDS': '3,7',
            'LIVE_STRATEGY_IDS': '3,7',
            'MIN_STAKE': '1',
            'MAX_STAKE': '20',
            'BASE_ORDER_COST': '2',
            'LIVE_STRATEGY_7_BASE_ORDER_COST': '5.5',
            'LIVE_STRATEGY_7_MIN_STAKE': '0.5',
            'LIVE_STRATEGY_7_MAX_STAKE': '99',
            'LIVE_STRATEGY_7_MIN_ENTRY_PRICE': '0.42',
            'LIVE_STRATEGY_7_MAX_ENTRY_PRICE': '0.55',
            'LIVE_STRATEGY_7_STRATEGY7_OFI_THRESHOLD': '0.58',
            'PAPER_STRATEGY_3_BASE_ORDER_COST': '4.0',
            'PAPER_STRATEGY_3_MIN_STAKE': '3',
            'PAPER_STRATEGY_3_MAX_STAKE': '30',
        }
    )

    assert cfg.live_strategy_ids == [3, 7]
    assert cfg.live_profiles[3].base_order_cost == 2.0
    assert cfg.live_profiles[7].base_order_cost == 5.5
    assert cfg.live_profiles[7].min_stake == 1.0
    assert cfg.live_profiles[7].max_stake == 20.0
    assert cfg.live_profiles[7].min_entry_price == 0.42
    assert cfg.live_profiles[7].max_entry_price == 0.55
    assert cfg.live_profiles[7].strategy7_ofi_threshold == 0.58
    assert cfg.live_profiles[7].strategy7_max_momentum_delta is None
    assert cfg.paper_strategy_profiles[3].base_order_cost == 4.0
    assert cfg.paper_strategy_profiles[3].min_stake == 3.0
    assert cfg.paper_strategy_profiles[3].max_stake == 20.0
    assert cfg.paper_strategy_profiles[7].base_order_cost == 2.0


def test_build_config_supports_strategy7_max_momentum_delta_overrides():
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "7",
            "PAPER_STRATEGY_IDS": "7",
            "LIVE_STRATEGY_IDS": "7",
            "STRATEGY7_MAX_MOMENTUM_DELTA": "0.015",
            "PAPER_STRATEGY_7_STRATEGY7_MAX_MOMENTUM_DELTA": "0.012",
            "LIVE_STRATEGY_7_STRATEGY7_MAX_MOMENTUM_DELTA": "0.018",
        }
    )

    assert cfg.strategy7_max_momentum_delta == 0.015
    assert cfg.paper_strategy_profiles[7].strategy7_max_momentum_delta == 0.012
    assert cfg.live_profiles[7].strategy7_max_momentum_delta == 0.018


def test_build_config_ignores_invalid_paper_strategy_entries():
    cfg = build_config_from_env_values({'STRATEGY_ID': '3', 'PAPER_STRATEGY_IDS': '6,x,9,2,6'})

    assert cfg.paper_strategy_ids == [6, 9, 2]


def test_build_config_accepts_strategy_9_in_strategy_lists():
    cfg = build_config_from_env_values({'STRATEGY_ID': '3', 'PAPER_STRATEGY_IDS': '9,8,6', 'LIVE_STRATEGY_IDS': '9'})

    assert cfg.paper_strategy_ids == [9, 8, 6]
    assert cfg.live_strategy_ids == [9]


def test_build_config_reads_strategy7_dynamic_sizing_values():
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
        }
    )

    assert cfg.strategy7_dynamic_sizing_enabled is True
    assert cfg.strategy7_sizing_reference_price == 0.51
    assert cfg.strategy7_sizing_price_step == 0.02
    assert cfg.strategy7_sizing_price_step_reduction == 0.15
    assert cfg.strategy7_sizing_min_multiplier == 0.45
    assert cfg.strategy7_sizing_max_multiplier == 1.10
    assert cfg.strategy7_sizing_strong_signal_gap == 0.03
    assert cfg.strategy7_sizing_strong_signal_boost == 0.25
    assert cfg.paper_strategy_profiles[7].strategy7_dynamic_sizing_enabled is False
    assert cfg.paper_strategy_profiles[7].strategy7_sizing_min_multiplier == 0.60


def test_build_config_supports_btc_15m_market_timeframe():
    cfg = build_config_from_env_values({'MARKET_TIMEFRAME': '15m'})

    assert cfg.market_timeframe == '15m'
    assert cfg.series_id == 10192
    assert cfg.series_slug == 'btc-up-or-down-15m'


def test_build_config_defaults_invalid_market_timeframe_to_btc_5m():
    cfg = build_config_from_env_values({'MARKET_TIMEFRAME': '7m'})

    assert cfg.market_timeframe == '5m'
    assert cfg.series_id == 10684
    assert cfg.series_slug == 'btc-up-or-down-5m'


def test_build_config_supports_open_delay_override():
    cfg = build_config_from_env_values({'OPEN_DELAY_SECONDS': '15'})

    assert cfg.open_delay_seconds == 15


def test_build_config_supports_runtime_poll_interval_overrides():
    cfg = build_config_from_env_values(
        {
            'POLL_INTERVAL_SECONDS': '2',
            'FAST_POLL_INTERVAL_SECONDS': '1.5',
            'NEAR_ENTRY_POLL_WINDOW_SECONDS': '20',
        }
    )

    assert cfg.poll_interval_seconds == 2
    assert cfg.fast_poll_interval_seconds == 1.5
    assert cfg.near_entry_poll_window_seconds == 20


def test_build_config_parses_enabled_paper_timeframes_and_profile_values():
    cfg = build_config_from_env_values(
        {
            'TRADE_MODE': 'paper',
            'PAPER_TIMEFRAMES': '5m,15m',
            'PAPER_5M_STRATEGY_ID': '5',
            'PAPER_5M_STRATEGY_IDS': '5,6',
            'PAPER_5M_TARGET_PROFIT': '0.8',
            'PAPER_15M_STRATEGY_ID': '2',
            'PAPER_15M_STRATEGY_IDS': '1,2,7',
            'PAPER_15M_TARGET_PROFIT': '1.0',
        }
    )

    assert cfg.paper_timeframes == ['5m', '15m']
    assert cfg.paper_profiles['5m'].strategy_id == 5
    assert cfg.paper_profiles['5m'].paper_strategy_ids == [5, 6]
    assert cfg.paper_profiles['5m'].target_profit == 0.8
    assert cfg.paper_profiles['15m'].strategy_id == 2
    assert cfg.paper_profiles['15m'].paper_strategy_ids == [1, 2, 7]
    assert cfg.paper_profiles['15m'].target_profit == 1.0


def test_build_config_keeps_live_single_timeframe_when_paper_profiles_exist():
    cfg = build_config_from_env_values(
        {
            'TRADE_MODE': 'live',
            'MARKET_TIMEFRAME': '15m',
            'PAPER_TIMEFRAMES': '5m,15m',
            'PAPER_5M_STRATEGY_IDS': '5,6',
            'PAPER_15M_STRATEGY_IDS': '1,2',
        }
    )

    assert cfg.market_timeframe == '15m'
    assert cfg.series_slug == 'btc-up-or-down-15m'
    assert cfg.paper_timeframes == ['5m', '15m']


def test_build_config_uses_legacy_single_timeframe_paper_fields_when_paper_timeframes_missing():
    cfg = build_config_from_env_values(
        {
            'TRADE_MODE': 'paper',
            'MARKET_TIMEFRAME': '15m',
            'STRATEGY_ID': '7',
            'PAPER_STRATEGY_IDS': '7,6',
            'TARGET_PROFIT': '1.1',
        }
    )

    assert cfg.paper_timeframes == ['15m']
    assert cfg.paper_profiles['15m'].strategy_id == 7
    assert cfg.paper_profiles['15m'].paper_strategy_ids == [7, 6]
    assert cfg.paper_profiles['15m'].target_profit == 1.1


def test_direct_app_config_constructor_aligns_paper_timeframes_with_market_timeframe():
    cfg = AppConfig(market_timeframe='15m')

    assert cfg.paper_timeframes == ['15m']
    assert cfg.paper_profiles['15m'].timeframe == '15m'


def test_direct_app_config_constructor_aligns_live_strategy_ids_with_strategy_id():
    cfg = AppConfig(strategy_id=5)

    assert cfg.live_strategy_ids == [5]
    assert list(cfg.live_profiles) == [5]
    assert cfg.live_profiles[5].strategy_id == 5
