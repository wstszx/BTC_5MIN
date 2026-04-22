from __future__ import annotations

from pathlib import Path

from config import AppConfig, build_config_from_env_values, load_env_file_values


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


def test_build_config_uses_paper_strategy_ids_when_present():
    cfg = build_config_from_env_values({'STRATEGY_ID': '2', 'PAPER_STRATEGY_IDS': '6,2,6,1'})

    assert cfg.strategy_id == 2
    assert cfg.paper_strategy_ids == [6, 2, 1]


def test_build_config_falls_back_to_strategy_id_for_paper():
    cfg = build_config_from_env_values({'STRATEGY_ID': '5'})

    assert cfg.paper_strategy_ids == [5]


def test_build_config_ignores_invalid_paper_strategy_entries():
    cfg = build_config_from_env_values({'STRATEGY_ID': '3', 'PAPER_STRATEGY_IDS': '6,x,9,2,6'})

    assert cfg.paper_strategy_ids == [6, 2]


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
