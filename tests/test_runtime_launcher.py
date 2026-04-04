from pathlib import Path

from config import AppConfig, build_config_from_env_values, load_env_file_values


def test_load_env_file_values_reads_simple_key_value_pairs(tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    env_file.write_text("STRATEGY_ID=5\nMAX_STAKE=9.5\n", encoding="utf-8")

    values = load_env_file_values(env_file)

    assert values == {"STRATEGY_ID": "5", "MAX_STAKE": "9.5"}


def test_build_config_from_env_values_applies_dashboard_values():
    cfg = build_config_from_env_values(
        {
            "STRATEGY_ID": "5",
            "MAX_STAKE": "9.5",
            "TARGET_PROFIT": "0.8",
        }
    )

    assert isinstance(cfg, AppConfig)
    assert cfg.strategy_id == 5
    assert cfg.max_stake == 9.5
    assert cfg.target_profit == 0.8
