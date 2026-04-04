from pathlib import Path

from config import AppConfig, build_config_from_env_values, load_env_file_values
from dashboard import DashboardState


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


def test_dashboard_state_ignores_malformed_env_lines(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env.dashboard"
    env_file.write_text("STRATEGY_ID=5\n=oops\n", encoding="utf-8")

    class DummyClient:
        def __init__(self, cfg):
            self.cfg = cfg

        def close(self):
            pass

    monkeypatch.setattr("dashboard.PolymarketClient", DummyClient)
    state = DashboardState(env_file=env_file)
    try:
        assert state._env_values == {"STRATEGY_ID": "5"}
    finally:
        state.close()
