from __future__ import annotations

import os
from pathlib import Path

from dashboard import DashboardState


def test_dashboard_config_roundtrip_updates_env_file(tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    state = DashboardState(env_file=env_file)
    try:
        payload = state.update_config(
            {
                "STRATEGY_ID": "5",
                "SIGNAL_MOMENTUM_THRESHOLD": "0.012",
                "WS_TRADE_GUARD_STALE_SECONDS": "1.2",
            }
        )
        assert payload["env_values"]["STRATEGY_ID"] == "5"
        assert payload["env_values"]["SIGNAL_MOMENTUM_THRESHOLD"] == "0.012"
        assert payload["env_values"]["WS_TRADE_GUARD_STALE_SECONDS"] == "1.2"
        text = env_file.read_text(encoding="utf-8")
        assert "STRATEGY_ID=5" in text
        assert "SIGNAL_MOMENTUM_THRESHOLD=0.012" in text
    finally:
        state.close()


def test_dashboard_rejects_unknown_config_keys(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    try:
        try:
            state.update_config({"UNKNOWN_KEY": "1"})
        except ValueError as exc:
            assert "Unsupported keys" in str(exc)
        else:
            raise AssertionError("Expected ValueError")
    finally:
        state.close()


def test_recent_trades_payload_handles_missing_csv(tmp_path: Path):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    try:
        payload = state.get_recent_trades_payload(limit=10)
        assert payload["count"] == 0
        assert payload["rows"] == []
    finally:
        state.close()
        os.chdir(old_cwd)
