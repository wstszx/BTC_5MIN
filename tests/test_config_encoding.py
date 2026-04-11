from __future__ import annotations

from pathlib import Path

from config import build_config_from_env_values, load_env_file_values


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
