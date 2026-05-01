from pathlib import Path

from dashboard import DashboardState, _dashboard_css, _dashboard_js


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


def test_strategy_config_layout_can_collapse_inside_left_panel():
    css = _dashboard_css()

    assert "grid-template-columns: repeat(auto-fit, minmax(min(100%, 150px), 1fr));" in css
    assert ".strategy-profile-grid {" in css
    assert "grid-template-columns: repeat(auto-fit, minmax(min(100%, 340px), 1fr));" in css
    assert "grid-template-columns: repeat(auto-fit, minmax(min(100%, 150px), 1fr));" in css
    assert ".strategy-profile-field {" in css
    assert "grid-template-columns: minmax(0, 1fr);" in css


def test_report_sections_keep_content_pinned_to_top_when_columns_stretch():
    css = _dashboard_css()

    report_section_rule = css.split(".report-section {", 1)[1].split("}", 1)[0]

    assert "grid-auto-rows: max-content;" in report_section_rule
