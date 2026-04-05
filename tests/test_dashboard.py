from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dashboard
from dashboard import (
    ConfigValidationError,
    DashboardState,
    _dashboard_html,
    _dashboard_js,
    create_dashboard_runtime,
)
from models import MarketQuote, MarketWindow


def test_create_dashboard_runtime_uses_requested_env_file(tmp_path: Path):
    runtime = create_dashboard_runtime(host="127.0.0.1", port=0, env_file=tmp_path / ".env.dashboard")
    try:
        assert runtime.state.env_file == tmp_path / ".env.dashboard"
        assert runtime.server.server_address[0] == "127.0.0.1"
        payload = runtime.state.get_config_payload()
        assert payload["env_file"] == str(tmp_path / ".env.dashboard")
    finally:
        runtime.close()


def test_dashboard_runtime_can_shutdown_cleanly(tmp_path: Path):
    runtime = create_dashboard_runtime(host="127.0.0.1", port=0, env_file=tmp_path / ".env.dashboard")
    thread = threading.Thread(target=runtime.serve_forever)
    thread.start()
    runtime.shutdown()
    thread.join(timeout=2)
    assert not thread.is_alive()
    runtime.close()


def test_dashboard_runtime_close_stops_active_server(tmp_path: Path):
    runtime = create_dashboard_runtime(host="127.0.0.1", port=0, env_file=tmp_path / ".env.dashboard")
    thread = threading.Thread(target=runtime.serve_forever)
    thread.start()
    time.sleep(0.1)
    runtime.close()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_dashboard_runtime_shutdown_close_idempotent(tmp_path: Path):
    runtime = create_dashboard_runtime(host="127.0.0.1", port=0, env_file=tmp_path / ".env.dashboard")
    runtime.shutdown()
    runtime.shutdown()
    thread = threading.Thread(target=runtime.serve_forever)
    thread.start()
    runtime.close()
    runtime.close()
    thread.join(timeout=2)
    assert not thread.is_alive()

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
        except ConfigValidationError as exc:
            message = str(exc)
            field_errors = exc.field_errors
        else:
            raise AssertionError("Expected ConfigValidationError")

        assert "MAX_STAKE" in message
        assert "WS_ENABLED" in message
        assert field_errors["MAX_STAKE"].startswith("Invalid value for MAX_STAKE")
        assert field_errors["WS_ENABLED"].startswith("Invalid value for WS_ENABLED")
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


def test_market_payload_marks_entry_window_missed_for_current_round(tmp_path: Path, monkeypatch):
    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

        def find_current_and_next_rounds(self, *, now):
            window = MarketWindow(
                event_id="evt-1",
                market_id="mkt-1",
                slug="btc-updown-5m-current",
                title="BTC 5m Current",
                start_time=now - timedelta(minutes=1),
                end_time=now + timedelta(minutes=4),
                up_token_id="up-token",
                down_token_id="down-token",
            )
            return window, None

        def get_market_by_slug(self, slug: str):
            return {"slug": slug}

        def quote_from_market(self, market):
            return MarketQuote(
                slug=str(market.get("slug", "")),
                up_price=0.55,
                down_price=0.45,
                up_best_ask=0.56,
                fetched_at=datetime.now(timezone.utc),
            )

        def get_ws_runtime_stats(self):
            return {}

    monkeypatch.setattr(dashboard, "PolymarketClient", StubClient)

    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    try:
        payload = state.get_market_payload()
        assert payload["round"]["is_current"] is True
        assert payload["round"]["seconds_to_entry"] < 0
        assert payload["plan"]["should_trade"] is False
        assert payload["plan"]["skip_reason"] == "entry_window_missed"
    finally:
        state.close()


def test_market_payload_allows_trade_within_entry_grace_window(tmp_path: Path, monkeypatch):
    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

        def find_current_and_next_rounds(self, *, now):
            window = MarketWindow(
                event_id="evt-grace",
                market_id="mkt-grace",
                slug="btc-updown-5m-grace",
                title="BTC 5m Grace",
                start_time=now - timedelta(seconds=7),
                end_time=now + timedelta(minutes=4, seconds=53),
                up_token_id="up-token",
                down_token_id="down-token",
            )
            return window, None

        def get_market_by_slug(self, slug: str):
            return {"slug": slug}

        def quote_from_market(self, market):
            return MarketQuote(
                slug=str(market.get("slug", "")),
                up_price=0.55,
                down_price=0.45,
                up_best_ask=0.56,
                fetched_at=datetime.now(timezone.utc),
            )

        def get_ws_runtime_stats(self):
            return {}

    monkeypatch.setattr(dashboard, "PolymarketClient", StubClient)

    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    try:
        payload = state.get_market_payload()
        assert payload["round"]["is_current"] is True
        assert payload["round"]["seconds_to_entry"] < 0
        assert payload["plan"]["should_trade"] is True
        assert payload["plan"]["skip_reason"] is None
    finally:
        state.close()


def test_market_payload_keeps_showing_current_round_when_current_entry_window_has_closed(tmp_path: Path, monkeypatch):
    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

        def find_current_and_next_rounds(self, *, now):
            current = MarketWindow(
                event_id="evt-current",
                market_id="mkt-current",
                slug="btc-updown-5m-current",
                title="BTC 5m Current",
                start_time=now - timedelta(minutes=1),
                end_time=now + timedelta(minutes=4),
                up_token_id="up-token",
                down_token_id="down-token",
            )
            upcoming = MarketWindow(
                event_id="evt-next",
                market_id="mkt-next",
                slug="btc-updown-5m-next",
                title="BTC 5m Next",
                start_time=now + timedelta(minutes=4),
                end_time=now + timedelta(minutes=9),
                up_token_id="up-token",
                down_token_id="down-token",
            )
            return current, upcoming

        def get_market_by_slug(self, slug: str):
            return {"slug": slug}

        def quote_from_market(self, market):
            return MarketQuote(
                slug=str(market.get("slug", "")),
                up_price=0.55,
                down_price=0.45,
                up_best_ask=0.56,
                fetched_at=datetime.now(timezone.utc),
            )

        def get_ws_runtime_stats(self):
            return {}

    monkeypatch.setattr(dashboard, "PolymarketClient", StubClient)

    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    try:
        payload = state.get_market_payload()
        assert payload["round"]["slug"] == "btc-updown-5m-current"
        assert payload["round"]["is_current"] is True
        assert payload["round"]["seconds_to_entry"] < 0
        assert payload["plan"]["should_trade"] is False
        assert payload["plan"]["skip_reason"] == "entry_window_missed"
    finally:
        state.close()


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


def test_dashboard_assets_surface_field_errors_after_failed_save():
    js = _dashboard_js()

    assert "field_errors" in js
    assert "fieldErrors" in js
    assert "validation_errors: fieldErrors" in js
    assert "env_values: values" in js


def test_dashboard_assets_include_entry_window_missed_reason_label():
    js = _dashboard_js()

    assert "entry_window_missed" in js


def test_dashboard_assets_use_planned_entry_copy():
    html = _dashboard_html()
    js = _dashboard_js()

    assert "计划入场" in html
    assert "距离计划入场" in js
    assert "已过计划入场" in js


def test_dashboard_market_header_prioritizes_human_time_over_slug():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="marketDeadline"' in html
    assert "function marketDeadlineText(" in js
    assert "结束时间 --" in js
    assert "el('marketDeadline').textContent = marketDeadlineText(round.end_time);" in js


def test_dashboard_reason_fallback_is_human_friendly():
    js = _dashboard_js()

    assert "未识别原因：" in js
    assert "可尝试刷新页面" in js
