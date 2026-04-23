from pathlib import Path

from dashboard import DashboardState, _dashboard_js


def test_render_config_builds_display_values_before_using_them():
    js = _dashboard_js()

    assert "const displayValues = { ...values, ENABLE_LIVE_TRADING: buildLiveToggleValue(values) };" in js


def test_dashboard_config_metadata_includes_live_strategy_ids(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    try:
        payload = state.get_config_payload()
        assert "LIVE_STRATEGY_IDS" in payload["editable_keys"]
        assert payload["labels"]["LIVE_STRATEGY_IDS"] == "实盘策略组合"
    finally:
        state.close()
