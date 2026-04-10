from __future__ import annotations

from pathlib import Path

from config import build_config_from_env_values, load_env_file_values


def test_load_env_file_values_reads_gbk_encoded_dashboard_file(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_bytes('COMMENT=中文\nMAX_STAKE=9.5\n'.encode('gbk'))

    values = load_env_file_values(env_file)

    assert values == {'COMMENT': '中文', 'MAX_STAKE': '9.5'}


def test_build_config_from_gbk_encoded_dashboard_file(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_bytes(
        'TRADE_MODE=paper\nMAX_STAKE=9.5\nPOLYMARKET_FUNDER=中文地址\n'.encode('gbk')
    )

    values = load_env_file_values(env_file)
    cfg = build_config_from_env_values(values)

    assert cfg.trade_mode == 'paper'
    assert cfg.max_stake == 9.5
    assert cfg.live_funder == '中文地址'
