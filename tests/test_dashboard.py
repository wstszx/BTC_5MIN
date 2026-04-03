from __future__ import annotations

import os
from pathlib import Path

from dashboard import DashboardState, _dashboard_html, _dashboard_js


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


def test_dashboard_rejects_invalid_config_values(tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    state = DashboardState(env_file=env_file)
    try:
        try:
            state.update_config({"MAX_STAKE": "abc", "WS_ENABLED": "maybe"})
        except ValueError as exc:
            message = str(exc)
        else:
            raise AssertionError("Expected ValueError")

        assert "MAX_STAKE" in message
        assert "WS_ENABLED" in message
        assert not env_file.exists()
    finally:
        state.close()


def test_dashboard_payload_uses_effective_values_for_invalid_env_file(tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    env_file.write_text("MAX_STAKE=abc\nWS_ENABLED=maybe\nTARGET_PROFIT=1.2\n", encoding="utf-8")
    state = DashboardState(env_file=env_file)
    try:
        payload = state.get_config_payload()

        assert payload["env_values"]["MAX_STAKE"] == "15.0"
        assert payload["env_values"]["WS_ENABLED"] == "true"
        assert payload["env_values"]["TARGET_PROFIT"] == "1.2"
        assert payload["validation_errors"]["MAX_STAKE"]
        assert payload["validation_errors"]["WS_ENABLED"]
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


def test_dashboard_payload_includes_strategy_catalog_and_field_groups(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    try:
        payload = state.get_config_payload()

        assert payload["strategy_catalog"]["2"]["label"] == "双轮分组交替"
        assert payload["strategy_catalog"]["2"]["preview"] == ["UP", "UP", "DOWN", "DOWN"]
        assert payload["strategy_catalog"]["5"]["label"] == "动量信号 V2"
        assert payload["field_help"]["STRATEGY_ID"]
        assert payload["field_scope"]["SIGNAL_MOMENTUM_THRESHOLD"] == "strategy_5_only"
        assert payload["field_groups"][0]["title"] == "基础策略"
    finally:
        state.close()


def test_dashboard_assets_include_strategy_guide_and_human_labels():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="strategyGuideCard"' in html
    assert "function renderStrategyGuide(" in js
    assert "双轮分组交替" in js
    assert "动量信号 V2" in js


def test_dashboard_assets_include_help_center_shell():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="btnHelp"' in html
    assert 'id="helpDrawer"' in html
    assert 'id="helpBackdrop"' in html
    assert "const HELP_TABS = [" in js
    assert "helpOpen: false" in js
    assert "helpTab: 'quickstart'" in js


def test_dashboard_assets_include_help_center_renderers():
    js = _dashboard_js()

    assert "function renderHelpDrawer()" in js
    assert "function renderHelpQuickStart()" in js
    assert "function renderHelpPageGuide()" in js
    assert "function renderHelpConfigDictionary()" in js
    assert "function renderHelpStrategyGuide()" in js
    assert "function renderHelpFaq()" in js


def test_dashboard_help_center_includes_quickstart_copy():
    js = _dashboard_js()

    assert "先看哪里" in js
    assert "怎么安全改参数" in js
    assert "怎么判断当前能不能跑" in js
    assert "出问题先看哪里" in js
    assert "页面元素说明" in js


def test_dashboard_help_center_reuses_strategy_and_field_metadata():
    js = _dashboard_js()

    assert "function renderHelpConfigDictionary()" in js
    assert "function renderHelpStrategyGuide()" in js
    assert "payload.field_groups" in js
    assert "payload.strategy_catalog" in js
    assert "仅策略 5 重点使用" in js
    assert "help-strategy-card-active" in js


def test_dashboard_help_center_includes_faq_and_doc_links():
    html = _dashboard_html()
    js = _dashboard_js()

    assert "常见问题" in js
    assert "docs/dashboard_runbook.md" in js or "dashboard_runbook.md" in html
    assert "docs/operations_runbook.md" in js or "operations_runbook.md" in html
    assert "docs/daily_ops_checklist.md" in js or "daily_ops_checklist.md" in html
