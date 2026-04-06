from dashboard import _dashboard_js


def test_render_config_builds_display_values_before_using_them():
    js = _dashboard_js()

    assert "const displayValues = { ...values, ENABLE_LIVE_TRADING: buildLiveToggleValue(values) };" in js
