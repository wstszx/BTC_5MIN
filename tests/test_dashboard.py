from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import dashboard
from config import LIVE_STRATEGY_IDS
from dashboard import (
    ConfigValidationError,
    DashboardState,
    _dashboard_html,
    _dashboard_js,
    _write_env_file,
    create_dashboard_runtime,
)
from models import MarketQuote, MarketWindow
from runtime_control import RuntimeControl

PLAYWRIGHT_CLI_PACKAGE = "@playwright/cli"


def _playwright_cli_command(npx_path: str, session: str, *args: str) -> list[str]:
    return [npx_path, "--yes", "--package", PLAYWRIGHT_CLI_PACKAGE, "playwright-cli", f"-s={session}", *args]


def _require_dashboard_browser_regressions() -> str:
    if os.getenv("DASHBOARD_BROWSER_REGRESSION") != "1":
        pytest.skip("set DASHBOARD_BROWSER_REGRESSION=1 to run browser regression coverage")
    npx_path = shutil.which("npx")
    if npx_path is None:
        pytest.skip("npx is required for browser regression coverage")
    return npx_path


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


def test_dashboard_browser_regressions_are_opt_in_for_ci_stability():
    source = Path(__file__).read_text(encoding="utf-8")

    assert 'os.getenv("DASHBOARD_BROWSER_REGRESSION") != "1"' in source
    assert "set DASHBOARD_BROWSER_REGRESSION=1 to run browser regression coverage" in source


def test_write_env_file_uses_atomic_replace(monkeypatch, tmp_path: Path):
    env_path = tmp_path / '.env.dashboard'
    replace_calls: list[tuple[Path, Path]] = []
    original_replace = Path.replace

    def spy_replace(self: Path, target: Path):
        replace_calls.append((self, Path(target)))
        return original_replace(self, target)

    monkeypatch.setattr(Path, 'replace', spy_replace)

    _write_env_file(env_path, {'B': '2', 'A': '1'})

    assert replace_calls
    assert replace_calls[-1][1] == env_path
    assert env_path.read_text(encoding='utf-8') == 'A=1\nB=2\n'


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

        assert payload["env_values"]["MAX_STAKE"] == ""
        assert payload["env_values"]["WS_ENABLED"] == "true"
        assert payload["env_values"]["TARGET_PROFIT"] == "1.2"
        assert payload["validation_errors"]["MAX_STAKE"]
        assert payload["validation_errors"]["WS_ENABLED"]
    finally:
        state.close()


def test_recent_trades_payload_includes_pending_paper_trades(tmp_path: Path):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    try:
        logs_dir = tmp_path / 'logs'
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / 'paper_trades.csv').write_text(
            "timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason\n"
            "2026-04-06T08:00:00+00:00,paper,10,2,OPEN,settled-slug,2026-04-06T07:55:00+00:00,2026-04-06T08:00:00+00:00,UP,0.50,2.0,1.0,1.0,UP,1.0,5.0,0.0,0,False,,,,,,False,\n",
            encoding='utf-8',
        )
        (logs_dir / 'session_state.json').write_text(
            json.dumps(
                {
                    'round_index': 12,
                    'cash_pnl': 5.0,
                    'recovery_loss': 0.0,
                    'consecutive_losses': 0,
                    'consecutive_max_stake_skips': 0,
                    'signal_round_slug': None,
                    'signal_round_open_up_price': None,
                    'signal_round_locked_side': None,
                    'stop_loss_count': 0,
                    'daily_realized_pnl': 5.0,
                    'current_day': '2026-04-06',
                    'pending_live_slug': None,
                    'pending_live_side': None,
                    'pending_live_price': None,
                    'pending_live_order_size': None,
                    'pending_live_order_cost': None,
                    'pending_live_expected_profit': None,
                    'pending_live_order_id': None,
                    'pending_live_end_time': None,
                    'pending_paper_trades': [
                        {
                            'round_index': 11,
                            'event_slug': 'pending-slug',
                            'start_time': '2026-04-06T08:05:00+00:00',
                            'end_time': '2026-04-06T08:10:00+00:00',
                            'side': 'DOWN',
                            'price': 0.47,
                            'order_size': 2.5,
                            'order_cost': 1.175,
                            'expected_profit': 1.325,
                            'strategy': 2,
                            'entry_timing': 'OPEN',
                            'signal_open_up_price': None,
                            'signal_current_up_price': None,
                            'signal_threshold': None,
                            'signal_delta': None,
                            'signal_locked': False,
                            'signal_reason': None,
                            'queued_at': '2026-04-06T08:05:05+00:00'
                        }
                    ],
                }
            ),
            encoding='utf-8',
        )

        payload = state.get_recent_trades_payload(limit=10)
        assert payload['count'] == 2
        assert payload['rows'][0]['event_slug'] == 'pending-slug'
        assert payload['rows'][0]['result'] == '--'
        assert payload['rows'][0]['pending_status'] == 'pending_settlement'
        assert payload['rows'][1]['event_slug'] == 'settled-slug'
    finally:
        state.close()
        os.chdir(old_cwd)


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

        assert payload["strategy_catalog"]["2"]["label"] == "\u53cc\u8f6e\u5206\u7ec4\u4ea4\u66ff"
        assert payload["strategy_catalog"]["2"]["preview"] == ["UP", "UP", "DOWN", "DOWN"]
        assert payload["strategy_catalog"]["5"]["label"] == "\u52a8\u91cf\u4fe1\u53f7 V2"
        assert payload["field_help"]["STRATEGY_ID"]
        assert payload["field_scope"]["SIGNAL_MOMENTUM_THRESHOLD"] == "strategy_5_only"
        assert payload["field_groups"][0]["title"] == "\u8fd0\u884c\u6a21\u5f0f"
    finally:
        state.close()


def test_dashboard_assets_include_strategy_guide_and_human_labels():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="strategyGuideCard"' in html
    assert "function renderStrategyGuide(" in js
    assert "\u53cc\u8f6e\u5206\u7ec4\u4ea4\u66ff" in js
    assert "\u52a8\u91cf\u4fe1\u53f7 V2" in js


def test_dashboard_assets_include_left_panel_mode_selector_shell():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="configModeSelect"' in html
    assert 'id="configContextSummary"' in html
    assert 'id="paperTaskflowRoot"' in html
    assert 'id="liveTaskflowRoot"' in html
    assert "function effectiveConfigMode(payload)" in js
    assert "function renderConfigModeShell(payload)" in js
    assert "function renderTaskflowVisibility(mode)" in js
    assert "function activeStrategyListKey(payload, values)" in js
    assert "const envValues = (payload && payload.env_values) || {};" in js
    assert "return buildLiveToggleValue(envValues) === 'true' ? 'live' : 'paper';" in js


def test_dashboard_assets_distinguish_current_and_historical_ws_errors():
    js = _dashboard_js()

    assert "ws_current_error: '当前错误'" in js
    assert "ws_last_error: '最近历史错误'" in js
    assert "['current_error', runtimeValue(wsPayload, 'current_error', 'ws_current_error')]" in js


def test_dashboard_reason_text_translates_prefixed_runtime_errors():
    js = _dashboard_js()

    assert "strategy_evaluation_error: '策略评估异常'" in js
    assert "const reasonCode = rawReason.split(':', 1)[0];" in js
    assert "return REASON_LABELS[reasonCode];" in js


def test_dashboard_assets_hide_paper_and_live_sections_by_active_mode():
    js = _dashboard_js()

    assert "const paperRoot = el('paperTaskflowRoot');" in js
    assert "const liveRoot = el('liveTaskflowRoot');" in js
    assert "paperRoot.hidden = normalizedMode !== 'paper';" in js
    assert "liveRoot.hidden = normalizedMode !== 'live';" in js
    assert "state.config = {" in js
    assert "const hiddenModeField = el('cfg_ENABLE_LIVE_TRADING');" in js
    assert "hiddenModeField.value = nextMode === 'live' ? 'true' : 'false';" in js
    assert "renderUnifiedStrategyToolbar(state.config || {}, currentUnifiedStrategyDraftValues());" in js
    assert "renderSharedPaperReportStrategySelector();" in js
    assert "void Promise.allSettled([refreshSummary(), refreshRecent()]);" in js
    assert "if (form && typeof form.oninput === 'function') {" in js
    assert "form.oninput();" in js
    assert "renderConfigModeShell(state.config || {});" in js


def test_dashboard_assets_mode_selector_propagates_into_save_payload_path():
    js = _dashboard_js()

    assert "const hiddenModeField = el('cfg_ENABLE_LIVE_TRADING');" in js
    assert "hiddenModeField.value = nextMode === 'live' ? 'true' : 'false';" in js
    assert "const keys = ['ENABLE_LIVE_TRADING', ...(((state.config && state.config.editable_keys) || []).filter((key) => !isSingleLiveToggleKey(key)))];" in js
    assert "payload[key] = node.value;" in js
    assert "expanded.TRADE_MODE = normalized === 'true' ? 'live' : 'paper';" in js
    assert "expanded.LIVE_TRADING_ENABLED = normalized;" in js
    assert "const hiddenKeys = new Set(['STRATEGY_IDS', 'PAPER_STRATEGY_IDS', 'LIVE_STRATEGY_IDS', 'PAPER_TIMEFRAMES', 'MARKET_TIMEFRAME', 'ENABLE_LIVE_TRADING']);" in js
    assert "const unifiedStrategyKeys = ['STRATEGY_ID', 'STRATEGY_IDS', 'PAPER_STRATEGY_IDS', 'LIVE_STRATEGY_IDS'];" in js
    assert "payload[multiKey] = unifiedValues[multiKey];" in js


def test_dashboard_assets_use_unified_strategy_selection():
    js = _dashboard_js()

    assert "function strategyListKeyForMode(mode) {" in js
    assert "return normalized === 'live' ? 'LIVE_STRATEGY_IDS' : 'PAPER_STRATEGY_IDS';" in js
    assert "return strategyListKeyForMode(mode);" in js
    assert "const multiKey = activeStrategyListKey(payload, values);" in js
    assert "const legacySelectedRaw = parseStrategyIdList((values && values.STRATEGY_IDS) ?? envValues.STRATEGY_IDS ?? '');" in js
    assert "strategyOptionLabel(multiKey, opt, payload)" in js
    assert "const selectedStrategyList = domStrategyListKey === multiKey" in js




def test_dashboard_config_payload_exposes_live_auto_redeem_fields(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        payload = state.get_config_payload()
        assert 'LIVE_AUTO_REDEEM_ENABLED' in payload['editable_keys']
        assert 'LIVE_AUTO_REDEEM_DRY_RUN' in payload['editable_keys']
        assert payload['labels']['LIVE_AUTO_REDEEM_ENABLED'] == '实盘自动赎回'
        assert payload['labels']['LIVE_AUTO_REDEEM_DRY_RUN'] == '自动赎回演练模式'
        assert payload['field_help']['LIVE_AUTO_REDEEM_ENABLED'].startswith('仅并行实盘使用')
        assert 'Polygon 链上赎回交易' in payload['field_help']['LIVE_AUTO_REDEEM_DRY_RUN']
        runtime_group = payload['field_groups'][0]
        assert 'LIVE_AUTO_REDEEM_ENABLED' in runtime_group['keys']
        assert 'LIVE_AUTO_REDEEM_DRY_RUN' in runtime_group['keys']
        assert payload['select_options']['LIVE_AUTO_REDEEM_ENABLED'] == ['false', 'true']
        assert payload['select_options']['LIVE_AUTO_REDEEM_DRY_RUN'] == ['false', 'true']
    finally:
        state.close()


def test_dashboard_help_center_includes_live_auto_redeem_copy():
    js = _dashboard_js()

    assert '实盘自动赎回开关' in js
    assert '自动赎回演练模式' in js
    assert '实盘自动赎回' in js
    assert '演练' in js
    assert 'Polygon' in js


def test_dashboard_config_payload_exposes_live_strategy_ids(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        payload = state.get_config_payload()
        assert LIVE_STRATEGY_IDS in payload['editable_keys']
        assert payload['labels'][LIVE_STRATEGY_IDS] == '实盘策略组合'
        assert payload['field_help'][LIVE_STRATEGY_IDS].startswith('实盘运行时可轮询的策略列表')
        assert payload['select_options'][LIVE_STRATEGY_IDS] == ['1', '2', '3', '4', '5', '6', '7', '8']
        assert LIVE_STRATEGY_IDS not in payload['field_groups'][1]['keys']
    finally:
        state.close()


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

    assert "实盘自动赎回" in js
    assert "先看哪里" in js
    assert "怎么安全改参数" in js
    assert "怎么判断当前能不能跑" in js
    assert "监控面板操作说明" in js
    assert "运行操作手册" in js
    assert "日常检查清单" in js
    assert "\u9875\u9762\u5143\u7d20\u8bf4\u660e" in js


def test_dashboard_help_center_includes_risk_limit_guide():
    js = _dashboard_js()

    assert "{ id: 'riskguide', label: '风控限制' }" in js
    assert "function renderHelpRiskGuide()" in js
    assert "MAX_CONSECUTIVE_LOSSES" in js
    assert "PAPER_SIMULATED_WALLET_BALANCE" in js
    assert "insufficient_live_wallet_balance" in js
    assert "策略 7" in js


def test_dashboard_help_center_reuses_strategy_and_field_metadata():
    js = _dashboard_js()

    assert "function renderHelpConfigDictionary()" in js
    assert "function renderHelpStrategyGuide()" in js
    assert "payload.field_groups" in js
    assert "payload.strategy_catalog" in js
    assert "\u4ec5\u7b56\u7565 5 \u4f7f\u7528" in js
    assert "help-strategy-card-active" in js


def test_dashboard_help_center_includes_faq_and_doc_links():
    html = _dashboard_html()
    js = _dashboard_js()

    assert "\u5e38\u89c1\u95ee\u9898" in js
    assert "docs/dashboard_runbook.md" in js or "dashboard_runbook.md" in html
    assert "docs/operations_runbook.md" in js or "operations_runbook.md" in html
    assert "docs/daily_ops_checklist.md" in js or "daily_ops_checklist.md" in html


def test_dashboard_assets_surface_field_errors_after_failed_save():
    js = _dashboard_js()

    assert "field_errors" in js
    assert "fieldErrors" in js
    assert "validation_errors: fieldErrors" in js
    assert "env_values: values" in js


def test_dashboard_assets_render_config_warning_banner():
    html = _dashboard_html()
    css = dashboard._dashboard_css()
    js = _dashboard_js()

    assert 'id="configWarningBanner"' in html
    assert 'id="configWarningList"' in html
    assert '.config-warning-banner' in css
    assert 'function renderConfigWarnings' in js
    assert 'payload.config_warnings' in js
    assert '配置警告' in js


def test_dashboard_assets_include_entry_window_missed_reason_label():
    js = _dashboard_js()

    assert "entry_window_missed" in js


def test_dashboard_assets_use_planned_entry_copy():
    html = _dashboard_html()
    js = _dashboard_js()

    assert "\u8ba1\u5212\u5165\u573a" in html
    assert "\u8ddd\u79bb\u8ba1\u5212\u5165\u573a" in js
    assert "\u5df2\u8fc7\u8ba1\u5212\u5165\u573a" in js


def test_dashboard_market_header_prioritizes_human_time_over_slug():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="marketDeadline"' in html
    assert "function marketDeadlineText(" in js
    assert "\u7ed3\u675f\u65f6\u95f4 --" in js
    assert "el('marketDeadline').textContent = marketDeadlineText(round.end_time);" in js


def test_dashboard_reason_fallback_is_human_friendly():
    js = _dashboard_js()

    assert "invalid_price" in js
    assert "price_below_threshold" in js
    assert "invalid_base_order_cost" in js
    assert "invalid_bet_sizing_mode" in js
    assert "signal_too_weak_fallback" in js
    assert "signal_price_unavailable" in js
    assert "signal_price_unavailable_fallback" in js
    assert "ofi_unavailable" in js
    assert "ofi_stale" in js
    assert "ofi_too_weak" in js
    assert "awaiting_fill_confirmation" in js
    assert "round_in_progress" in js
    assert "轮次仍在进行中" in js
    assert "round_unresolved" in js
    assert "轮次尚未结算" in js
    assert "live_wallet_balance_unavailable" in js
    assert "实盘钱包余额不可用" in js
    assert "insufficient_live_wallet_balance" in js
    assert "实盘钱包余额不足" in js
    assert "market_timeframe" in js
    assert "INVALID OPERATION" in js
    assert "return rawReason;" in js



def test_dashboard_assets_include_daily_strategy_performance_table():
    html = _dashboard_html()
    js = _dashboard_js()

    assert "每日策略表现" in html
    assert 'id="strategyDaysTbody"' in html
    assert "<th>下单</th>" in html
    assert "<th>跳过</th>" in html
    assert "<th>胜率</th>" in html
    assert "<th>单日盈亏</th>" in html
    assert "<th>累计盈亏</th>" in html
    assert "function renderStrategyDays(" in js
    assert "payload.strategy_days" in js
    assert "cumulative_pnl" in js


def test_dashboard_assets_mark_pending_recent_trades_clearly():
    js = _dashboard_js()

    assert "const isPending = row.pending_status === 'pending_settlement';" in js
    assert "const resultText = isPending ? '待结算' : (row.result || '--');" in js
    assert "const rowClass = isPending ? 'recent-pending' : (isMissedEntry ? 'recent-missed-entry' : '');" in js
    assert "setReportStatus('recentStatus', '明细', pendingCount > 0 ? (rows.length + ' 行 · ' + pendingCount + ' 待结算') : (rows.length + ' 行'), pendingCount > 0 ? 'warn' : 'ok');" in js

def test_dashboard_assets_highlight_recent_missed_entry_rows():
    js = _dashboard_js()
    css = dashboard._dashboard_css()

    assert "const isMissedEntry = row.skip_reason === 'entry_window_missed';" in js
    assert "const rowClass = isPending ? 'recent-pending' : (isMissedEntry ? 'recent-missed-entry' : '');" in js
    assert "const reasonHtml = isMissedEntry" in js
    assert "skip-reason-badge missed-entry" in js
    assert '.recent-missed-entry td {' in css
    assert '.skip-reason-badge {' in css
    assert '.skip-reason-badge.missed-entry {' in css

def test_dashboard_assets_show_serial_waiting_hint_for_pending_paper_trades():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="paperSerialHint"' in html
    assert "const pendingPaperTrades = Array.isArray(ss.pending_paper_trades) ? ss.pending_paper_trades : [];" in js
    assert "const serialHintNode = el('paperSerialHint');" in js
    assert "serialHintNode.textContent = pendingPaperTrades.length > 0 ? ('上一轮未结算，当前按串行模式等待，共 ' + pendingPaperTrades.length + ' 笔待结算') : '当前没有待结算轮次';" in js
    assert "serialHintNode.className = pendingPaperTrades.length > 0 ? 'serial-hint warn' : 'serial-hint';" in js

def test_dashboard_config_payload_masks_live_private_key_and_exposes_mode_fields(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text(
        'TRADE_MODE=live\n'
        'LIVE_TRADING_ENABLED=true\n'
        'POLYMARKET_PRIVATE_KEY=super-secret-private-key\n'
        'POLYMARKET_FUNDER=0xfunder\n',
        encoding='utf-8',
    )
    state = DashboardState(env_file=env_file)
    try:
        payload = state.get_config_payload()

        assert 'TRADE_MODE' in payload['editable_keys']
        assert 'LIVE_TRADING_ENABLED' in payload['editable_keys']
        assert 'POLYMARKET_PRIVATE_KEY' in payload['editable_keys']
        assert 'POLYMARKET_FUNDER' in payload['editable_keys']
        assert payload['env_values']['TRADE_MODE'] == 'live'
        assert payload['env_values']['LIVE_TRADING_ENABLED'] == 'true'
        assert payload['env_values']['POLYMARKET_PRIVATE_KEY'] != 'super-secret-private-key'
        assert payload['env_values']['POLYMARKET_PRIVATE_KEY']
        assert payload['env_values']['POLYMARKET_FUNDER'] != '0xfunder'
        assert payload['env_values']['POLYMARKET_FUNDER']
        assert payload['runtime_status']['saved_mode'] == 'live'
        assert payload['runtime_status']['running_mode'] == 'paper'
        assert payload['runtime_status']['restart_required'] is True
    finally:
        state.close()



def test_dashboard_config_payload_exposes_official_api_credential_fields(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text(
        'TRADE_MODE=live\n'
        'LIVE_TRADING_ENABLED=true\n'
        'POLYMARKET_API_KEY=builder-key\n'
        'POLYMARKET_API_SECRET=builder-secret\n'
        'POLYMARKET_API_PASSPHRASE=builder-passphrase\n',
        encoding='utf-8',
    )
    state = DashboardState(env_file=env_file)
    try:
        payload = state.get_config_payload()

        assert 'POLYMARKET_API_KEY' in payload['editable_keys']
        assert 'POLYMARKET_API_SECRET' in payload['editable_keys']
        assert 'POLYMARKET_API_PASSPHRASE' in payload['editable_keys']
        assert payload['env_values']['POLYMARKET_API_KEY'] != 'builder-key'
        assert payload['env_values']['POLYMARKET_API_SECRET'] != 'builder-secret'
        assert payload['env_values']['POLYMARKET_API_PASSPHRASE'] != 'builder-passphrase'
        assert payload['env_values']['POLYMARKET_API_KEY']
        assert payload['env_values']['POLYMARKET_API_SECRET']
        assert payload['env_values']['POLYMARKET_API_PASSPHRASE']
    finally:
        state.close()


def test_dashboard_config_payload_exposes_relayer_redeem_credentials(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        payload = state.get_config_payload()

        assert 'POLYMARKET_BUILDER_API_KEY' in payload['editable_keys']
        assert 'POLYMARKET_BUILDER_SECRET' in payload['editable_keys']
        assert 'POLYMARKET_BUILDER_PASSPHRASE' in payload['editable_keys']
        assert 'POLYMARKET_RELAYER_API_KEY' in payload['editable_keys']
        assert 'POLYMARKET_RELAYER_API_KEY_ADDRESS' in payload['editable_keys']
    finally:
        state.close()


def test_dashboard_runtime_payload_includes_redeem_auth_mode(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text(
        'TRADE_MODE=live\n'
        'LIVE_TRADING_ENABLED=true\n'
        'POLYMARKET_PRIVATE_KEY=private-key\n'
        'POLYMARKET_FUNDER=0xfunder\n'
        'LIVE_AUTO_REDEEM_ENABLED=true\n'
        'POLYMARKET_RELAYER_API_KEY=relayer-key\n'
        'POLYMARKET_RELAYER_API_KEY_ADDRESS=0xrelayer\n',
        encoding='utf-8',
    )
    state = DashboardState(env_file=env_file)
    try:
        payload = state.get_config_payload()
        runtime = payload['runtime_status']
        assert 'redeem_auth_mode' in runtime
        assert runtime['redeem_auth_mode'] == 'relayer'
    finally:
        state.close()


def test_dashboard_help_text_distinguishes_trading_and_redeem_credentials():
    assert '实盘下单私有接口' in DashboardState.FIELD_HELP['POLYMARKET_API_KEY']
    assert '自动赎回' in DashboardState.FIELD_HELP['POLYMARKET_BUILDER_API_KEY']

def test_dashboard_update_config_preserves_masked_private_key_on_unrelated_save(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text(
        'TRADE_MODE=live\n'
        'LIVE_TRADING_ENABLED=true\n'
        'POLYMARKET_PRIVATE_KEY=super-secret-private-key\n'
        'POLYMARKET_FUNDER=0xfunder\n',
        encoding='utf-8',
    )
    state = DashboardState(env_file=env_file)
    try:
        initial = state.get_config_payload()
        masked = initial['env_values']['POLYMARKET_PRIVATE_KEY']

        payload = state.update_config(
            {
                'TRADE_MODE': 'live',
                'TARGET_PROFIT': '2.5',
                'POLYMARKET_PRIVATE_KEY': masked,
            }
        )

        text = env_file.read_text(encoding='utf-8')
        assert 'POLYMARKET_PRIVATE_KEY=super-secret-private-key' in text
        assert payload['env_values']['POLYMARKET_PRIVATE_KEY'] == masked
        assert payload['env_values']['TARGET_PROFIT'] == '2.5'
    finally:
        state.close()



def test_dashboard_runtime_status_reports_live_ready_and_restart_requirement(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text(
        'TRADE_MODE=live\n'
        'LIVE_TRADING_ENABLED=true\n'
        'POLYMARKET_PRIVATE_KEY=super-secret-private-key\n'
        'POLYMARKET_FUNDER=0xfunder\n',
        encoding='utf-8',
    )
    state = DashboardState(env_file=env_file)
    try:
        payload = state.get_config_payload()
        status = payload['runtime_status']

        assert status['saved_mode'] == 'live'
        assert status['running_mode'] == 'paper'
        assert status['restart_required'] is True
        assert status['live_ready'] is True
        assert status['live_validation_error'] is None
    finally:
        state.close()



def test_dashboard_live_recent_orders_reads_live_specific_csv(tmp_path: Path):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        live_csv = tmp_path / 'logs' / 'live_orders.csv'
        live_csv.parent.mkdir(parents=True, exist_ok=True)
        live_csv.write_text(
            'timestamp,mode,event_slug,side,price,order_cost,trade_pnl,skip_reason\n'
            '2026-04-05T00:00:00+00:00,live,slug-one,UP,0.51,10.0,0.0,\n'
            '2026-04-05T00:05:00+00:00,live,slug-two,DOWN,0.49,12.0,1.5,\n',
            encoding='utf-8',
        )

        payload = state.get_live_recent_orders_payload(limit=10)

        assert payload['count'] == 2
        assert payload['csv_path'].endswith('live_orders.csv')
        assert payload['rows'][0]['event_slug'] == 'slug-two'
        assert payload['rows'][1]['event_slug'] == 'slug-one'
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_live_recent_orders_filters_by_strategy(tmp_path: Path):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        live_csv = tmp_path / 'logs' / 'live_orders.csv'
        live_csv.parent.mkdir(parents=True, exist_ok=True)
        live_csv.write_text(
            'timestamp,mode,round_index,strategy,event_slug,side,price,order_cost,trade_pnl,skip_reason\n'
            '2026-04-05T00:00:00+00:00,live,1,1,slug-one,UP,0.51,10.0,0.0,\n'
            '2026-04-05T00:05:00+00:00,live,1,7,slug-seven,DOWN,0.49,12.0,1.5,\n',
            encoding='utf-8',
        )

        payload = state.get_live_recent_orders_payload(limit=10, strategy='7')

        assert payload['strategy'] == '7'
        assert payload['count'] == 1
        assert payload['rows'][0]['event_slug'] == 'slug-seven'
        assert payload['rows'][0]['strategy'] == '7'
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_config_payload_exposes_runtime_config_warnings(tmp_path: Path):
    env_file = tmp_path / ".env.dashboard"
    env_file.write_text("MAX_STAKE=abc\nSTRATEGY_IDS=2,x\n", encoding="utf-8")
    state = DashboardState(env_file=env_file)
    try:
        payload = state.get_config_payload()

        assert payload["config_warnings"]["MAX_STAKE"].startswith("Invalid value for MAX_STAKE")
        assert payload["config_warnings"]["STRATEGY_IDS"] == "Invalid entries for STRATEGY_IDS ignored: x"
        assert payload["runtime_status"]["config_warning_count"] == 2
    finally:
        state.close()


def test_dashboard_live_recent_orders_all_scopes_to_configured_live_strategies(tmp_path: Path):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    env_file = tmp_path / ".env.dashboard"
    env_file.write_text("TRADE_MODE=live\nSTRATEGY_IDS=7\n", encoding="utf-8")
    state = DashboardState(env_file=env_file)
    try:
        live_csv = tmp_path / "logs" / "live_orders.csv"
        live_csv.parent.mkdir(parents=True, exist_ok=True)
        live_csv.write_text(
            "timestamp,mode,round_index,strategy,event_slug,side,price,order_cost,trade_pnl,skip_reason\n"
            "2026-04-28T07:41:06+00:00,live,0,2,btc-updown-5m-test,UP,0.56,1.0,0.0,\n"
            "2026-04-28T07:15:32+00:00,live,21,7,btc-updown-15m-1777360500,SKIP,,0.0,0.0,strategy7_signal_conflict\n",
            encoding="utf-8",
        )

        payload = state.get_live_recent_orders_payload(limit=10)

        assert payload["strategy"] == "all"
        assert payload["count"] == 1
        assert payload["rows"][0]["strategy"] == "7"
        assert payload["rows"][0]["event_slug"] == "btc-updown-15m-1777360500"
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_live_recent_orders_collapses_entry_and_settlement_rows(tmp_path: Path):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    env_file = tmp_path / ".env.dashboard"
    env_file.write_text("TRADE_MODE=live\nSTRATEGY_IDS=7\nMARKET_TIMEFRAME=15m\n", encoding="utf-8")
    state = DashboardState(env_file=env_file)
    try:
        live_csv = tmp_path / "logs" / "live_orders.csv"
        live_csv.parent.mkdir(parents=True, exist_ok=True)
        live_csv.write_text(
            "timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason\n"
            "2026-04-28T05:30:34+00:00,live,20,7,OPEN,btc-updown-15m-1777354200,2026-04-28T05:30:00+00:00,2026-04-28T05:45:00+00:00,UP,0.38,4.222010080645161,1.604363830645161,2.61764625,,0.0,-0.589373,1.61764625,1,False,,0.485,0.375,,,-0.11,True,\n"
            "2026-04-28T05:48:44+00:00,live,21,7,OPEN,btc-updown-15m-1777354200,2026-04-28T05:30:00+00:00,2026-04-28T05:45:00+00:00,UP,0.55,4.210525,2.31578875,1.89473625,DOWN,-2.31578875,-2.90516175,3.933435,2,False,,,,,,False,\n",
            encoding="utf-8",
        )

        payload = state.get_live_recent_orders_payload(limit=10)

        assert payload["count"] == 1
        assert payload["rows"][0]["timestamp"] == "2026-04-28T05:30:34+00:00"
        assert payload["rows"][0]["round_index"] == "20"
        assert payload["rows"][0]["result"] == "DOWN"
        assert payload["rows"][0]["trade_pnl"] == "-2.31578875"
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_live_recent_orders_exposes_timeframe_round_display_time(tmp_path: Path):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    env_file = tmp_path / ".env.dashboard"
    env_file.write_text("MARKET_TIMEFRAME=15m\n", encoding="utf-8")
    state = DashboardState(env_file=env_file)
    try:
        live_csv = tmp_path / "logs" / "live_orders.csv"
        live_csv.parent.mkdir(parents=True, exist_ok=True)
        live_csv.write_text(
            "timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason\n"
            "2026-04-24T11:41:04.611595+00:00,live,0,2,OPEN,btc-updown-5m-test,2026-04-24T11:40:04.611595+00:00,2026-04-24T11:45:04.611595+00:00,UP,0.56,1.7857142857142856,1.0,0.7857142857142856,,0.0,0.0,0.0,0,False,,,,,,False,\n",
            encoding="utf-8",
        )

        payload = state.get_live_recent_orders_payload(limit=10)

        assert payload["timeframe"] == "15m"
        assert payload["rows"][0]["round_display_time"] == "2026-04-24T11:30:00+00:00"
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_live_recent_orders_skips_official_lookup_for_synthetic_live_slug(tmp_path: Path, monkeypatch):
    client = dashboard.PolymarketClient(dashboard.AppConfig())

    def fail_lookup(slug: str):
        raise AssertionError(f"Unexpected official lookup for {slug}")

    monkeypatch.setattr(client, "get_event_by_slug", fail_lookup)

    try:
        row = dashboard._validate_recent_trade_row(
            {"event_slug": "btc-updown-5m-test", "result": "UP"},
            client=client,
            fill_missing_result=True,
        )
        assert row["result_check_status"] == ""
    finally:
        client.close()


def test_dashboard_live_recent_orders_resolves_official_result_fields(tmp_path: Path, monkeypatch):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)

    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

        def get_event_by_slug(self, slug: str):
            return {
                "id": "evt-live",
                "slug": slug,
                "eventMetadata": {
                    "priceToBeat": 68420.5,
                    "finalPrice": 68501.25,
                },
                "markets": [
                    {
                        "id": "mkt-live",
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["1", "0"]',
                    }
                ],
            }

    monkeypatch.setattr(dashboard, "PolymarketClient", StubClient)
    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    try:
        live_csv = tmp_path / "logs" / "live_orders.csv"
        live_csv.parent.mkdir(parents=True, exist_ok=True)
        live_csv.write_text(
            "timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason\n"
            "2026-04-27T08:10:00+00:00,live,4,2,OPEN,btc-updown-5m-live,2026-04-27T08:05:00+00:00,2026-04-27T08:10:00+00:00,UP,0.56,2.0,1.12,0.88,,0.0,0.0,0.0,0,False,,,,,,False,\n",
            encoding="utf-8",
        )

        payload = state.get_live_recent_orders_payload(limit=10)
        row = payload["rows"][0]

        assert row["result"] == "UP"
        assert row["resolved_price_to_beat"] == "68420.5"
        assert row["resolved_final_price"] == "68501.25"
        assert row["resolved_expected_result"] == "UP"
        assert row["result_check_status"] == "official"
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_live_recent_orders_only_validates_submitted_orders_but_prices_all_rows(tmp_path: Path, monkeypatch):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)

    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

        def get_event_by_slug(self, slug: str):
            return {
                "id": "evt-live",
                "slug": slug,
                "eventMetadata": {
                    "priceToBeat": 68420.5,
                    "finalPrice": 68501.25,
                },
                "markets": [
                    {
                        "id": "mkt-live",
                        "outcomes": '["Up", "Down"]',
                        "outcomePrices": '["1", "0"]',
                    }
                ],
            }

    monkeypatch.setattr(dashboard, "PolymarketClient", StubClient)
    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    try:
        live_csv = tmp_path / "logs" / "live_orders.csv"
        live_csv.parent.mkdir(parents=True, exist_ok=True)
        live_csv.write_text(
            "timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason\n"
            "2026-04-27T08:10:00+00:00,live,4,2,OPEN,btc-updown-5m-live,2026-04-27T08:05:00+00:00,2026-04-27T08:10:00+00:00,UP,0.56,2.0,1.12,0.88,,0.0,0.0,0.0,0,False,,,,,,False,\n"
            "2026-04-27T08:11:00+00:00,live,4,7,OPEN,btc-updown-5m-live,2026-04-27T08:05:00+00:00,2026-04-27T08:10:00+00:00,UP,0.56,0.0,0.0,0.0,,0.0,0.0,0.0,0,False,insufficient_live_wallet_balance,,,,,,False,\n",
            encoding="utf-8",
        )

        payload = state.get_live_recent_orders_payload(limit=10)
        skipped_row = payload["rows"][0]
        submitted_row = payload["rows"][1]

        assert skipped_row["resolved_price_to_beat"] == "68420.5"
        assert skipped_row["resolved_final_price"] == "68501.25"
        assert skipped_row["result"] == ""
        assert skipped_row["resolved_expected_result"] == ""
        assert skipped_row["result_check_status"] == ""
        assert submitted_row["result"] == "UP"
        assert submitted_row["resolved_expected_result"] == "UP"
        assert submitted_row["result_check_status"] == "official"
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_live_summary_reads_live_orders_and_filters_by_strategy(tmp_path: Path):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        live_csv = tmp_path / 'logs' / 'live_orders.csv'
        live_csv.parent.mkdir(parents=True, exist_ok=True)
        live_csv.write_text(
            'timestamp,mode,round_index,strategy,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_delta,signal_threshold,signal_locked\n'
            '2026-04-05T00:00:00+00:00,live,1,2,slug-one,2026-04-04T23:55:00+00:00,2026-04-05T00:00:00+00:00,UP,0.51,10,5,1,UP,1.5,1.5,0,0,False,,0.02,0.01,True\n'
            '2026-04-05T00:05:00+00:00,live,2,7,slug-seven,2026-04-05T00:00:00+00:00,2026-04-05T00:05:00+00:00,DOWN,0.49,10,5,1,UP,-2.0,-0.5,0,1,False,,0.03,0.01,True\n',
            encoding='utf-8',
        )

        summary_all = state.get_live_summary_payload()
        summary_seven = state.get_live_summary_payload(strategy='7')

        assert summary_all['mode'] == 'live'
        assert summary_all['csv_path'].endswith('live_orders.csv')
        assert summary_all['latest']['trade_rows'] == 2
        assert summary_all['latest']['total_pnl'] == -0.5
        assert summary_seven['strategy'] == '7'
        assert summary_seven['latest']['trade_rows'] == 1
        assert summary_seven['latest']['total_pnl'] == -2.0
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_live_summary_backfills_settled_submitted_rows(tmp_path: Path, monkeypatch):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)

    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

        def get_event_by_slug(self, slug: str):
            return {
                'id': 'evt-live',
                'slug': slug,
                'eventMetadata': {'priceToBeat': 100.0, 'finalPrice': 90.0},
                'markets': [{'id': 'mkt-live'}],
            }

    monkeypatch.setattr(dashboard, 'PolymarketClient', StubClient)
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        live_csv = tmp_path / 'logs' / 'live_orders.csv'
        live_csv.parent.mkdir(parents=True, exist_ok=True)
        live_csv.write_text(
            'timestamp,mode,round_index,strategy,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_delta,signal_threshold,signal_locked\n'
            '2026-04-05T00:00:00+00:00,live,1,2,btc-updown-5m-one,2026-04-04T23:55:00+00:00,2026-04-05T00:00:00+00:00,UP,0.50,2,1,1,,0.0,0.0,0,0,False,,0.02,0.01,True\n'
            '2026-04-05T00:05:00+00:00,live,2,2,btc-updown-5m-skip,2026-04-05T00:00:00+00:00,2026-04-05T00:05:00+00:00,UP,0.50,0,0,0,,0.0,0.0,0,0,False,insufficient_live_wallet_balance,0.02,0.01,True\n',
            encoding='utf-8',
        )

        summary = state.get_live_summary_payload(strategy='2')

        assert summary['latest']['trade_rows'] == 1
        assert summary['latest']['hit_rate'] == 0.0
        assert summary['latest']['total_pnl'] == -1.0
    finally:
        state.close()
        os.chdir(old_cwd)



def test_dashboard_runtime_factory_accepts_running_trade_mode(tmp_path: Path):
    runtime = create_dashboard_runtime(
        host="127.0.0.1",
        port=0,
        env_file=tmp_path / ".env.dashboard",
        running_trade_mode="live",
    )
    try:
        payload = runtime.state.get_config_payload()
        assert payload["runtime_status"]["running_mode"] == "live"
    finally:
        runtime.close()


def test_dashboard_assets_include_runtime_mode_status_shell():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="runtimeModeCard"' not in html
    assert 'id="runtimeSavedMode"' not in html
    assert 'id="runtimeRunningMode"' not in html
    assert 'id="runtimeRestartRequired"' not in html
    assert 'id="runtimeLiveReady"' not in html
    assert 'id="runtimeLiveError"' not in html
    assert 'function renderRuntimeStatus(' in js
    assert 'payload.runtime_status || {}' in js
    assert "setText('runtimeSavedMode', formatModeLabel(payload.saved_mode || 'paper'));" in js





def test_dashboard_html_includes_btc_favicon():
    html = _dashboard_html()

    assert 'rel="icon"' in html
    assert 'data:image/svg+xml' in html
    assert 'BTC' in html


def test_dashboard_runtime_status_includes_live_redeem_snapshot(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text(
        'TRADE_MODE=live\n'
        'LIVE_TRADING_ENABLED=true\n'
        'POLYMARKET_PRIVATE_KEY=super-secret-private-key\n'
        'POLYMARKET_FUNDER=0xfunder\n'
        'LIVE_AUTO_REDEEM_ENABLED=true\n',
        encoding='utf-8',
    )
    state = DashboardState(env_file=env_file)
    logs_dir = state._cfg.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / 'live_redeem_state.json').write_text(
        json.dumps(
            {
                'conditions': {
                    'cond-1': {
                        'status': 'pending',
                        'attempt_count': 1,
                        'event_slug': 'resolved-one',
                    },
                    'cond-2': {
                        'status': 'retry_wait',
                        'attempt_count': 2,
                        'event_slug': 'resolved-two',
                    },
                },
                'runtime': {
                    'enabled': True,
                    'last_poll_at': '2026-04-12T08:00:00+00:00',
                    'last_attempt_at': '2026-04-12T08:00:05+00:00',
                    'last_result': 'submitted',
                    'last_tx_hash': '0xabc123',
                    'pending_redeem_count': 2,
                },
            }
        ),
        encoding='utf-8',
    )
    state = DashboardState(env_file=env_file)
    try:
        payload = state.get_config_payload()
        runtime = payload['runtime_status']
        assert runtime['redeem_visible'] is True
        assert runtime['redeem_enabled'] is True
        assert runtime['redeem_pending_count'] == 2
        assert runtime['redeem_last_result'] == 'submitted'
        assert runtime['redeem_last_attempt_at'] == '2026-04-12T08:00:05+00:00'
        assert runtime['redeem_last_tx_hash'] == '0xabc123'
    finally:
        state.close()


def test_dashboard_assets_include_live_redeem_runtime_rows():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="runtimeRedeemRows"' not in html
    assert 'id="runtimeRedeemEnabled"' not in html
    assert 'id="runtimeRedeemPending"' not in html
    assert 'id="runtimeRedeemResult"' not in html
    assert 'id="runtimeRedeemAttempt"' not in html
    assert 'id="runtimeRedeemTxHash"' not in html
    assert "const redeemVisible = !!(payload.redeem_visible || payload.redeem_enabled || ((payload.running_mode || payload.active_mode || 'paper') === 'live'));" in js
    assert "setDisplay('runtimeRedeemRows', redeemVisible ? '' : 'none');" in js
    assert "setText('runtimeRestartRequired', payload.restart_required ? '需要' : '不需要');" in js
    assert "setText('runtimeLiveReady', payload.live_ready ? '已就绪' : '未就绪');" in js
    assert "setText('runtimeRedeemEnabled', payload.redeem_enabled ? '已开启' : '未开启');" in js
    assert "setText('runtimeRedeemPending', String(payload.redeem_pending_count ?? 0));" in js
    assert "setText('runtimeRedeemResult', payload.redeem_last_result || '--');" in js


def test_dashboard_assets_include_optimizer_runtime_rows():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="runtimeOptimizerRows"' not in html
    assert 'id="runtimeOptimizerEnabled"' not in html
    assert 'id="runtimeOptimizerChampion"' not in html
    assert 'id="runtimeOptimizerChallengers"' not in html
    assert 'id="runtimeOptimizerPromotable"' not in html
    assert 'id="runtimeOptimizerLastRun"' not in html
    assert "setText('runtimeOptimizerEnabled', payload.optimizer_enabled ? '已开启' : '未开启');" in js
    assert "setText('runtimeOptimizerChampion', payload.optimizer_champion_id || '--');" in js
    assert "setText('runtimeOptimizerChallengers', String((payload.optimizer_active_challengers || []).length));" in js
    assert "setText('runtimeOptimizerPromotable', String(payload.optimizer_promotable_count ?? 0));" in js
    assert "setText('runtimeOptimizerLastRun', payload.optimizer_last_run_at ? fmtIso(payload.optimizer_last_run_at) : '--');" in js
    assert 'id="runtimeOptimizerChallengerList"' not in html
    assert 'id="runtimeOptimizerPromotableList"' not in html
    assert "function renderOptimizerCandidateList(" in js
    assert "setHtml('runtimeOptimizerChallengerList'" in js
    assert "setHtml('runtimeOptimizerPromotableList'" in js
    assert "const decision = (item || {}).promotion_decision || {};" in js
    assert "const decisionState = String(decision.state || '--');" in js
    assert "const decisionReason = String(decision.reason || '--');" in js
    assert "setText('runtimeRedeemAttempt', payload.redeem_last_attempt_at ? fmtIso(payload.redeem_last_attempt_at) : '--');" in js
    assert "setText('runtimeRedeemTxHash', payload.redeem_last_tx_hash || '--');" in js


def test_dashboard_runtime_mode_labels_are_localized(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        payload = state.get_config_payload()

        assert payload['labels']['TRADE_MODE'] == '运行视角'
        assert payload['labels']['LIVE_TRADING_ENABLED'] == '并行实盘开关'
        assert payload['labels']['POLYMARKET_PRIVATE_KEY'] == '实盘私钥'
        assert payload['labels']['POLYMARKET_API_KEY'] == '官方 API 访问密钥'
        assert payload['labels']['POLYMARKET_API_SECRET'] == '官方 API 签名密钥'
        assert payload['labels']['POLYMARKET_API_PASSPHRASE'] == '官方 API 通行口令'
        assert payload['field_help']['ENABLE_LIVE_TRADING'].startswith('关闭时仅运行纸面测试')
        assert payload['field_help']['POLYMARKET_PRIVATE_KEY'].startswith('实盘钱包私钥')
        assert payload['field_help']['POLYMARKET_FUNDER'].startswith('与私钥对应的实盘钱包地址')
        assert payload['field_help']['POLYMARKET_API_KEY'].startswith('CLOB 实盘下单凭证')
    finally:
        state.close()

def test_dashboard_assets_switch_report_endpoints_by_selected_mode():
    js = _dashboard_js()

    assert "function effectiveReportMode()" in js
    assert "const reportMode = effectiveReportMode();" in js
    assert "const strategy = encodeURIComponent(effectivePaperRecentStrategyFilter());" in js
    assert "const timeframe = encodeURIComponent(effectivePaperTimeframeFilter());" in js
    assert "const recentEndpoint = reportMode === 'live' ? '/api/live/recent?limit=80&strategy=' + strategy : '/api/paper/recent?limit=80&strategy=' + strategy + '&timeframe=' + timeframe;" in js
    assert "const summaryEndpoint = reportMode === 'live' ? '/api/live/summary?strategy=' + strategy : '/api/paper/summary?strategy=' + strategy + '&timeframe=' + timeframe;" in js
    assert "renderReportModeCopy();" in js
    assert "renderSharedPaperReportStrategySelector();" in js


def test_dashboard_assets_localize_recent_panel_by_running_mode():
    js = _dashboard_js()

    assert "const runningMode = String((((state.config || {}).runtime_status || {}).active_mode || (((state.config || {}).runtime_status || {}).running_mode) || 'paper')).toLowerCase();" in js
    assert "tbody.innerHTML = '<tr><td colspan=14 class=empty>' + (runningMode === 'live' ? '最近没有实盘交易记录' : '最近没有纸面交易记录') + '</td></tr>';" in js
    assert "const reportMode = effectiveReportMode();" in js
    assert "setReportStatus('recentStatus', '明细', rows.length + ' 行' + (reportMode === 'live' ? ' · 实盘' : ''), pendingCount > 0 ? 'warn' : 'ok');" in js




def test_dashboard_assets_remove_low_value_operator_clutter():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="clockUtc"' not in html
    assert 'id="cfgEnvFile"' not in html
    assert 'id="btnToggleKeys"' not in html
    assert 'id="btnReloadConfig"' not in html
    assert "el('clockUtc')" not in js
    assert "el('cfgEnvFile')" not in js
    assert "el('btnToggleKeys')" not in js
    assert "el('btnReloadConfig')" not in js


def test_dashboard_assets_fold_runtime_and_strategy_diagnostics():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="runtimeSummaryBar"' not in html
    assert 'id="runtimeDetailsToggle"' not in html
    assert 'id="runtimeDetailsPanel"' not in html
    assert 'id="diagnosticsToggle"' in html
    assert 'id="diagnosticsPanel"' in html
    assert 'strategy6Panel' in html
    assert 'strategy7Panel' in html
    assert 'function toggleFoldSection(' in js


def test_dashboard_assets_rebalance_main_columns_for_config_and_decision():
    html = _dashboard_html()
    css = dashboard._dashboard_css()

    assert 'class="panel left-stack config-stack"' in html
    assert 'class="panel center-stack decision-stack"' in html
    assert 'class="stack right-stack monitor-stack"' not in html
    assert 'grid-template-columns: minmax(560px, 1.42fr) minmax(360px, 0.82fr);' in css


def test_dashboard_assets_use_two_column_primary_layout():
    css = dashboard._dashboard_css()

    assert 'grid-template-columns: minmax(560px, 1.42fr) minmax(360px, 0.82fr);' in css


def test_dashboard_assets_mid_width_layout_keeps_config_and_decision_columns():
    css = dashboard._dashboard_css()

    assert '@media (max-width: 1450px) {' in css
    assert 'grid-template-columns: minmax(500px, 1.28fr) minmax(340px, 0.9fr);' in css


def test_dashboard_assets_remove_visible_config_status_block():
    html = _dashboard_html()

    assert 'class="config-status-inline"' not in html
    assert '<span class="meta-label">状态</span>' not in html
    assert '<span class="meta-label">最近保存</span>' not in html
    assert 'id="cfgError"' not in html
    assert 'id="cfgSavedAt"' not in html


def test_dashboard_assets_allow_strategy_panel_rows_to_wrap_without_overflow():
    css = dashboard._dashboard_css()

    assert '.strategy-panel-row {' in css
    assert 'grid-template-columns: minmax(0, 1fr) auto;' in css
    assert '.strategy-panel-row-main {' in css
    assert 'grid-template-columns: auto minmax(0, 1fr);' in css
    assert '.strategy-panel-primary {' in css
    assert 'justify-self: end;' in css
    assert '@media (max-width: 1024px) {' in css
    assert '.strategy-panel-row { grid-template-columns: 1fr; }' in css
    assert '.strategy-panel-primary { justify-self: start; }' in css


def test_dashboard_assets_allow_strategy_guide_content_to_wrap_fully():
    css = dashboard._dashboard_css()

    assert '.strategy-guide-title {' in css
    assert 'white-space: normal;' in css
    assert 'overflow-wrap: anywhere;' in css
    assert '.strategy-guide-subtitle {' in css
    assert '.strategy-guide-head {' in css
    assert 'align-items: flex-start;' in css
    assert '.strategy-guide-head .chip {' in css
    assert 'max-width: 100%;' in css


def test_dashboard_assets_move_connection_status_to_topbar_and_remove_runtime_monitoring_panel():
    html = _dashboard_html()

    assert 'id="topConnectionStatus"' in html
    assert 'id="wsHealth"' in html
    assert 'id="topConnectionDetail"' in html
    assert 'id="monitorRuntimePanel"' not in html
    assert '运行与连接监控' not in html
    assert 'id="runtimeSummaryBar"' not in html
    assert 'id="runtimeDetailsToggle"' not in html
    assert 'id="runtimeDetailsPanel"' not in html
    assert 'id="wsRuntimeList"' not in html


def test_dashboard_assets_move_diagnostics_entry_into_decision_column():
    html = _dashboard_html()

    assert 'id="decisionDiagnosticsHost"' in html
    assert 'id="diagnosticsToggle"' in html
    assert 'id="diagnosticsPanel"' in html
    assert '诊断区' in html


def test_dashboard_assets_compress_config_status_into_inline_summary():
    html = _dashboard_html()

    assert 'class="config-status-inline"' not in html
    assert '<span class="meta-label">读取状态</span>' not in html
    assert '<span class="meta-label">最近保存</span>' not in html
    assert 'id="cfgError"' not in html
    assert 'id="cfgSavedAt"' not in html


def test_dashboard_assets_guard_removed_config_status_nodes_in_js():
    js = _dashboard_js()

    assert "const node = el('cfgError');" in js
    assert "if (!node) {" in js
    assert "node.textContent = message || '--';" in js
    assert "const savedAtNode = el('cfgSavedAt');" in js
    assert "if (savedAtNode) {" in js
    assert "savedAtNode.textContent = payload.saved_at ? fmtIso(payload.saved_at) : '--';" in js


def test_dashboard_assets_remove_redundant_market_updated_time_row():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="marketUpdatedAt"' not in html
    assert "el('marketUpdatedAt')" not in js


def test_dashboard_assets_style_monitor_and_decision_columns_for_rebalanced_roles():
    css = dashboard._dashboard_css()

    assert '.decision-stack {' in css
    assert '.config-stack {' in css
    assert '.top-connection-status {' in css
    assert '.top-connection-detail {' in css
    assert '.monitor-stack' not in css
    assert '.monitor-runtime-grid' not in css


def test_dashboard_assets_responsive_layout_preserves_priority_order():
    css = dashboard._dashboard_css()

    assert '@media (max-width: 1450px) {' in css
    assert 'grid-template-columns: minmax(500px, 1.28fr) minmax(340px, 0.9fr);' in css
    assert '@media (max-width: 1024px) {' in css
    assert '.layout { grid-template-columns: 1fr; }' in css


def test_dashboard_assets_use_primary_decision_card_with_folded_signal_details():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="decisionCard"' in html
    assert 'id="signalDetailsToggle"' in html
    assert 'id="signalDetailsPanel"' in html
    assert '盘口价格' in html
    assert '最终决策' in html
    assert '开盘看涨价' in html
    assert '当前看涨价' in html
    assert 'function renderDecisionCard(' in js


def test_dashboard_assets_use_shared_paper_report_strategy_filter():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="paperReportStrategy"' in html
    assert 'id="paperSummaryStrategy"' not in html
    assert 'id="recentTradesStrategy"' not in html
    assert 'function renderSharedPaperReportStrategySelector(' in js
    assert 'paperReportStrategyFilter' in js


def test_dashboard_assets_render_unified_report_card_shell():
    html = _dashboard_html()

    assert 'class="panel unified-report-card"' in html
    assert 'class="report-card-head"' in html
    assert '交易报告' in html
    assert '策略筛选同时作用于纸面交易汇总与最近交易明细' in html
    assert 'id="reportModeSelect"' in html
    assert '<option value="paper">纸面</option>' in html
    assert '<option value="live">实盘</option>' in html
    assert 'id="paperReportStrategy"' in html
    assert 'id="paperStatus"' in html
    assert 'id="recentStatus"' in html
    assert 'id="reportSummarySection"' in html
    assert 'id="reportRecentSection"' in html
    assert '纸面交易汇总' in html
    assert '最近交易明细' in html


def test_dashboard_assets_remove_old_report_panel_shells():
    html = _dashboard_html()

    assert '<div class=\\"head-title\\">报告视图</div>' not in html
    assert '<section class="panel trades-panel">' not in html
    assert '<div class=\\"head-title\\">纸面交易汇总</div>' not in html


def test_dashboard_assets_style_unified_report_card_layout():
    css = dashboard._dashboard_css()

    assert '.unified-report-card {' in css
    assert '.report-card-body {' in css
    assert 'grid-template-columns: minmax(220px, 0.55fr) minmax(0, 1.85fr);' in css
    assert '.report-status-group {' in css
    assert '.report-section {' in css
    assert '.report-recent-table {' in css


def test_dashboard_assets_keep_recent_trades_table_without_horizontal_scroll():
    css = dashboard._dashboard_css()

    assert 'grid-template-columns: minmax(220px, 0.55fr) minmax(0, 1.85fr);' in css
    assert '.report-recent-table table {' in css
    assert 'table-layout: fixed;' in css
    assert 'overflow-x: hidden;' in css
    assert '.report-recent-table th,\n.report-recent-table td {' in css
    assert 'white-space: normal;' in css
    assert 'overflow-wrap: anywhere;' in css


def test_dashboard_assets_stack_unified_report_card_on_narrow_layouts():
    css = dashboard._dashboard_css()

    assert '@media (max-width: 1450px) {' in css
    assert '.report-card-body {' in css
    assert 'grid-template-columns: minmax(200px, 0.48fr) minmax(0, 1.72fr);' in css
    assert '@media (max-width: 1024px) {' in css
    assert 'grid-template-columns: 1fr;' in css


def test_dashboard_assets_render_unified_report_header_status_and_recent_copy():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="reportModeSelect"' in html
    assert 'id="paperStatus"' in html
    assert 'id="recentStatus"' in html
    assert 'class="report-status-group"' in html
    assert 'id="reportCardDesc"' in html
    assert 'id="reportSummaryTitle"' in html
    assert 'id="recentPanelDesc"' in html
    assert "reportMode: 'paper'" in js
    assert "return state.reportMode === 'live' ? 'live' : 'paper';" in js
    assert 'function renderReportModeCopy()' in js
    assert "reportMode === 'live' ? '实盘交易汇总' : '纸面交易汇总'" in js
    assert "reportMode === 'live' ? '暂无实盘数据' : '暂无纸面数据'" in js
    assert 'function recentStrategyHeaderText()' in js
    assert 'function setReportStatus(' in js
    assert "setReportStatus('paperStatus', '汇总', '已更新', 'ok');" in js
    assert "setReportStatus('recentStatus', '明细', rows.length + ' 行' + (reportMode === 'live' ? ' · 实盘' : ''), pendingCount > 0 ? 'warn' : 'ok');" in js
    assert "el('recentPanelDesc').textContent = recentStrategyHeaderText();" in js


def test_dashboard_assets_refresh_shared_selector_still_updates_summary_and_recent():
    js = _dashboard_js()

    assert "const modeNode = el('reportModeSelect');" in js
    assert "state.reportMode = modeNode.value === 'live' ? 'live' : 'paper';" in js
    assert "state.paperReportStrategyFilter = node.value || 'all';" in js
    assert "state.paperSummaryStrategyFilter = '';" in js
    assert "state.paperRecentStrategyFilter = '';" in js
    assert "await Promise.allSettled([refreshSummary(), refreshRecent()]);" in js
    assert "const strategy = encodeURIComponent(effectivePaperSummaryStrategyFilter());" in js
    assert "const strategy = encodeURIComponent(effectivePaperRecentStrategyFilter());" in js
    assert "const multiKey = strategyListKeyForMode(effectiveReportMode());" in js


def test_dashboard_assets_use_strategy_panel_for_unified_strategy_selection():
    html = _dashboard_html()
    js = _dashboard_js()
    css = dashboard._dashboard_css()

    assert '策略面板' in js
    assert 'cfgStrategyPanel' in js
    assert 'strategy-panel' in js
    assert "'STRATEGY_ID', 'STRATEGY_IDS'" in js
    assert 'cfgPaperStrategiesSelectAll' not in js
    assert '全选全部策略' not in js
    assert 'function renderStrategyPanel(' in js
    assert 'function selectAllPaperStrategiesInPanel()' in js
    assert 'function clearPaperStrategies()' in js
    assert 'function togglePaperStrategySelection(' in js
    assert 'function setPrimaryStrategy(' in js
    assert "state.marketStrategyFilter = focusStrategy;" in js
    assert "const summaryEndpoint = reportMode === 'live' ? '/api/live/summary?strategy=' + strategy : '/api/paper/summary?strategy=' + strategy + '&timeframe=' + timeframe;" in js
    assert 'function resolveUnifiedStrategySelection(' in js
    assert 'function renderUnifiedStrategyToolbar(' in js
    assert 'function collectUnifiedStrategyValues(' in js
    assert 'strategy-panel-row-main' in js
    assert 'strategy-panel-summary' in js
    assert 'strategy-panel-subtitle' not in js
    assert 'field-wide' in js
    assert '.strategy-panel-row-main {' in css
    assert '.strategy-panel-summary {' in css
    assert '.field.field-wide {' in css
    assert '.strategy-panel-primary input {' in css
    assert 'width: 14px;' in css
    assert 'min-height: 14px;' in css
    assert 'id="strategyGuideCard"' in html


def test_dashboard_assets_include_strategy_profile_editor_and_reset_action():
    js = _dashboard_js()

    assert 'function renderStrategyProfileEditor(' in js
    assert 'strategy_profiles' in js
    assert 'data-strategy-config-key' in js
    assert 'function resetStrategyState(' in js
    assert "'/api/strategy/reset'" in js
    assert '重置状态' in js
    assert '策略参数' in js


def test_dashboard_assets_allow_strategy_panel_rows_to_wrap_without_overflow():
    css = dashboard._dashboard_css()

    assert '.strategy-panel-row {' in css
    assert 'grid-template-columns: minmax(0, 1fr) auto;' in css
    assert '.strategy-panel-row-main {' in css
    assert 'grid-template-columns: auto minmax(0, 1fr);' in css
    assert '.strategy-panel-primary {' in css
    assert 'justify-self: end;' in css
    assert '@media (max-width: 1024px) {' in css
    assert '.strategy-panel-row { grid-template-columns: 1fr; }' in css
    assert '.strategy-panel-primary { justify-self: start; }' in css


def test_dashboard_assets_group_strategy7_ws_and_live_controls_under_advanced_settings():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="advancedConfigToggle"' in html
    assert 'id="advancedConfigPanel"' in html
    assert '高级参数' in html
    assert 'function applyAdvancedConfigVisibility(' in js


def test_dashboard_assets_keep_summary_and_recent_filters_independent():
    js = _dashboard_js()

    assert "paperSummaryStrategyFilter: null" in js
    assert "paperRecentStrategyFilter: null" in js
    assert 'function effectivePaperSummaryStrategyFilter()' in js
    assert 'function effectivePaperRecentStrategyFilter()' in js
    assert "const summaryCurrent = normalizePaperReportStrategyFilter(effectivePaperSummaryStrategyFilter(), options);" in js
    assert "const recentCurrent = normalizePaperReportStrategyFilter(effectivePaperRecentStrategyFilter(), options);" in js
    assert "state.paperSummaryStrategyFilter = summaryNode.value || 'all';" in js
    assert "await refreshSummary();" in js
    assert "state.paperRecentStrategyFilter = recentNode.value || 'all';" in js
    assert "await refreshRecent();" in js


def test_dashboard_assets_market_refresh_does_not_reset_report_filters():
    js = _dashboard_js()

    assert "state.marketStrategyFilter = String(strategyView.selected || state.marketStrategyFilter || 'all');" in js
    assert "state.paperReportStrategyFilter || state.marketStrategyFilter || 'all'" not in js
    assert "state.paperSummaryStrategyFilter = String(strategyView.selected || state.marketStrategyFilter || 'all');" not in js
    assert "state.paperRecentStrategyFilter = String(strategyView.selected || state.marketStrategyFilter || 'all');" not in js


def test_dashboard_assets_summary_and_recent_use_their_own_filters():
    js = _dashboard_js()

    assert "const strategy = encodeURIComponent(effectivePaperSummaryStrategyFilter());" in js
    assert "const strategy = encodeURIComponent(effectivePaperRecentStrategyFilter());" in js


def test_dashboard_assets_show_current_strategy_in_recent_panel_header():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="recentPanelDesc"' in html
    assert 'function recentStrategyHeaderText()' in js
    assert "const timeframe = effectivePaperTimeframeFilter();" in js
    assert "const strategy = effectivePaperRecentStrategyFilter();" in js
    assert "el('recentPanelDesc').textContent = recentStrategyHeaderText();" in js
    assert "return '按时间倒序显示最近 80 条记录 · 当前频次：' + timeframe + ' · 当前策略：全部';" in js
    assert "return '按时间倒序显示最近 80 条记录 · 当前频次：' + timeframe + ' · 当前策略：策略 ' + strategy;" in js


def test_dashboard_assets_refresh_all_loads_market_before_report_panels():
    js = _dashboard_js()

    assert "await refreshConfig();" in js
    assert "await refreshMarket();" in js
    assert "await Promise.allSettled([refreshSummary(), refreshRecent()]);" in js


def test_dashboard_assets_render_compact_inputs_for_short_numeric_fields():
    js = _dashboard_js()
    css = dashboard._dashboard_css()

    assert "function isCompactConfigField(key) {" in js
    assert "'TARGET_PROFIT'" in js
    assert "input.classList.add('input-compact');" in js
    assert "select.classList.add('input-compact');" in js
    assert '.field input.input-compact,' in css
    assert 'width: min(100%, 156px);' in css
    assert 'min-height: 32px;' in css
    assert 'box-sizing: border-box;' in css


def test_dashboard_assets_confirm_before_switching_to_live_mode():
    js = _dashboard_js()

    assert 'function shouldConfirmLiveModeSwitch(' in js
    assert "previousMode !== 'live' && nextMode === 'live'" in js
    assert 'window.confirm(' in js


def test_dashboard_runtime_payload_uses_manager_snapshot(tmp_path: Path):
    control = RuntimeControl(initial_mode='paper')
    control.set_desired_mode('live')
    state = DashboardState(env_file=tmp_path / '.env.dashboard', runtime_control=control)
    try:
        payload = state.get_config_payload()
        runtime = payload['runtime_status']
        assert runtime['active_mode'] == 'paper'
        assert runtime['desired_mode'] == 'live'
        assert runtime['switch_state'] == 'pending'
        assert runtime['running_mode'] == 'paper'
        assert runtime['saved_mode'] == 'paper'
    finally:
        state.close()


def test_dashboard_runtime_payload_exposes_worker_runtime_alert(tmp_path: Path):
    control = RuntimeControl(initial_mode="live")
    control.update_worker_state(
        runtime_alert_code="trading_restricted",
        runtime_alert_message="Trading restricted in your region.",
        runtime_alert_level="error",
    )
    state = DashboardState(env_file=tmp_path / ".env.dashboard", runtime_control=control)
    try:
        payload = state.get_config_payload()
        runtime = payload["runtime_status"]
        assert runtime["runtime_alert_code"] == "trading_restricted"
        assert "Polymarket 限制当前地区实盘交易" in runtime["runtime_alert_message"]
        assert runtime["runtime_alert_level"] == "error"
        assert runtime["runtime_alert_at"] is not None
    finally:
        state.close()


def test_dashboard_assets_include_runtime_alert_popup():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="runtimeAlertMessage"' not in html
    assert "function maybeShowRuntimeAlert(" in js
    assert "setInterval(refreshRuntimeStatus, POLL_MS.market);" in js


def test_dashboard_runtime_payload_includes_optimizer_status(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    try:
        payload = state.get_config_payload()
        runtime = payload["runtime_status"]
        assert "optimizer_enabled" in runtime
        assert "optimizer_last_run_at" in runtime
        assert "optimizer_promotable_count" in runtime
    finally:
        state.close()


def test_dashboard_runtime_payload_reads_optimizer_state_file(tmp_path: Path):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "optimizer_state.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "last_run_at": "2026-04-16T10:00:00+00:00",
                "champion_id": "champion-1",
                "active_challengers": [{"candidate_id": "cand-a"}],
                "promotable_candidates": [{"candidate_id": "cand-a"}],
            }
        ),
        encoding="utf-8",
    )
    state = DashboardState(env_file=tmp_path / ".env.dashboard")
    try:
        payload = state.get_config_payload()
        runtime = payload["runtime_status"]
        assert runtime["optimizer_enabled"] is True
        assert runtime["optimizer_last_run_at"] == "2026-04-16T10:00:00+00:00"
        assert runtime["optimizer_champion_id"] == "champion-1"
        assert runtime["optimizer_promotable_count"] == 1
        assert runtime["optimizer_active_challengers"][0]["candidate_id"] == "cand-a"
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_runtime_status_includes_live_strategy_ids_and_states(tmp_path: Path):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text(
        'TRADE_MODE=live\n'
        'LIVE_TRADING_ENABLED=true\n'
        'POLYMARKET_PRIVATE_KEY=private-key\n'
        'POLYMARKET_FUNDER=0xfunder\n'
        'LIVE_STRATEGY_IDS=3,6\n',
        encoding='utf-8',
    )
    logs_dir = tmp_path / 'logs'
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / 'live_session_state.json').write_text(
        json.dumps(
            {
                'live_strategies': {
                    '3': {
                        'round_index': 12,
                        'cash_pnl': 1.5,
                        'recovery_loss': 0.0,
                        'consecutive_losses': 0,
                        'consecutive_max_stake_skips': 0,
                        'signal_round_slug': None,
                        'signal_round_open_up_price': None,
                        'signal_round_locked_side': None,
                        'strategy6_last_ofi_score': None,
                        'stop_loss_count': 0,
                        'daily_realized_pnl': 1.5,
                        'current_day': '2026-04-23',
                        'pending_live_slug': None,
                        'pending_live_side': None,
                        'pending_live_price': None,
                        'pending_live_order_size': None,
                        'pending_live_order_cost': None,
                        'pending_live_expected_profit': None,
                        'pending_live_order_id': None,
                        'pending_live_end_time': None,
                    },
                    '6': {
                        'round_index': 13,
                        'cash_pnl': 2.5,
                        'recovery_loss': 0.5,
                        'consecutive_losses': 1,
                        'consecutive_max_stake_skips': 0,
                        'signal_round_slug': None,
                        'signal_round_open_up_price': None,
                        'signal_round_locked_side': None,
                        'strategy6_last_ofi_score': 0.82,
                        'stop_loss_count': 0,
                        'daily_realized_pnl': 2.5,
                        'current_day': '2026-04-23',
                        'pending_live_slug': 'btc-updown-5m-current',
                        'pending_live_side': 'UP',
                        'pending_live_price': 0.51,
                        'pending_live_order_size': 25.0,
                        'pending_live_order_cost': 12.75,
                        'pending_live_expected_profit': 1.25,
                        'pending_live_order_id': 'order-6',
                        'pending_live_end_time': '2026-04-23T03:45:00+00:00',
                    },
                }
            }
        ),
        encoding='utf-8',
    )
    state = DashboardState(env_file=env_file)
    try:
        payload = state.get_config_payload()
        runtime = payload['runtime_status']
        assert runtime['live_strategy_ids'] == ['3', '6']
        assert runtime['pending_live_order'] is True
        assert runtime['live_strategy_states']['3']['round_index'] == 12
        assert runtime['live_strategy_states']['6']['pending_live_slug'] == 'btc-updown-5m-current'
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_runtime_status_keeps_pending_live_order_from_removed_strategy(tmp_path: Path):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text(
        'TRADE_MODE=live\n'
        'LIVE_TRADING_ENABLED=true\n'
        'POLYMARKET_PRIVATE_KEY=private-key\n'
        'POLYMARKET_FUNDER=0xfunder\n'
        'LIVE_STRATEGY_IDS=3\n',
        encoding='utf-8',
    )
    logs_dir = tmp_path / 'logs'
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / 'live_session_state.json').write_text(
        json.dumps(
            {
                'live_strategies': {
                    '6': {
                        'round_index': 13,
                        'cash_pnl': 2.5,
                        'recovery_loss': 0.5,
                        'consecutive_losses': 1,
                        'consecutive_max_stake_skips': 0,
                        'signal_round_slug': None,
                        'signal_round_open_up_price': None,
                        'signal_round_locked_side': None,
                        'strategy6_last_ofi_score': 0.82,
                        'stop_loss_count': 0,
                        'daily_realized_pnl': 2.5,
                        'current_day': '2026-04-23',
                        'pending_live_slug': 'btc-updown-5m-orphaned',
                        'pending_live_side': 'UP',
                        'pending_live_price': 0.51,
                        'pending_live_order_size': 25.0,
                        'pending_live_order_cost': 12.75,
                        'pending_live_expected_profit': 1.25,
                        'pending_live_order_id': 'order-6',
                        'pending_live_end_time': '2026-04-23T03:45:00+00:00',
                    }
                }
            }
        ),
        encoding='utf-8',
    )
    state = DashboardState(env_file=env_file)
    try:
        payload = state.get_config_payload()
        runtime = payload['runtime_status']
        assert runtime['live_strategy_ids'] == ['3']
        assert runtime['pending_live_order'] is True
        assert runtime['live_strategy_states']['6']['pending_live_slug'] == 'btc-updown-5m-orphaned'
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_config_payload_exposes_and_saves_strategy_profile_overrides(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text(
        'TRADE_MODE=live\n'
        'LIVE_TRADING_ENABLED=true\n'
        'LIVE_STRATEGY_IDS=3,7\n'
        'BASE_ORDER_COST=2\n'
        'LIVE_STRATEGY_7_BASE_ORDER_COST=5.5\n',
        encoding='utf-8',
    )
    state = DashboardState(env_file=env_file)
    try:
        payload = state.get_config_payload()

        assert payload['strategy_profiles']['mode'] == 'live'
        strategy_seven = payload['strategy_profiles']['strategies']['7']
        assert strategy_seven['fields']['BASE_ORDER_COST']['key'] == 'LIVE_STRATEGY_7_BASE_ORDER_COST'
        assert strategy_seven['fields']['BASE_ORDER_COST']['value'] == '5.5'
        assert strategy_seven['fields']['BASE_ORDER_COST']['inherited'] is False
        assert payload['strategy_profiles']['strategies']['3']['fields']['BASE_ORDER_COST']['value'] == '2.0'
        assert payload['strategy_profiles']['strategies']['3']['fields']['BASE_ORDER_COST']['inherited'] is True

        updated = state.update_config({'LIVE_STRATEGY_3_BASE_ORDER_COST': '4.25'})

        assert updated['strategy_profiles']['strategies']['3']['fields']['BASE_ORDER_COST']['value'] == '4.25'
        assert 'LIVE_STRATEGY_3_BASE_ORDER_COST=4.25' in env_file.read_text(encoding='utf-8')
    finally:
        state.close()


def test_dashboard_can_reset_one_live_strategy_sizing_state(tmp_path: Path):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text(
        'TRADE_MODE=live\nLIVE_TRADING_ENABLED=true\nLIVE_STRATEGY_IDS=3,7\n',
        encoding='utf-8',
    )
    logs_dir = tmp_path / 'logs'
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / 'live_session_state.json').write_text(
        json.dumps(
            {
                'live_strategies': {
                    '3': {
                        'round_index': 9,
                        'cash_pnl': -12.5,
                        'recovery_loss': 12.5,
                        'consecutive_losses': 4,
                        'consecutive_max_stake_skips': 2,
                        'stop_loss_count': 1,
                        'daily_realized_pnl': -12.5,
                        'current_day': '2026-04-29',
                    },
                    '7': {
                        'round_index': 5,
                        'cash_pnl': 3.0,
                        'recovery_loss': 0.0,
                        'consecutive_losses': 0,
                        'consecutive_max_stake_skips': 0,
                    },
                }
            }
        ),
        encoding='utf-8',
    )
    state = DashboardState(env_file=env_file)
    try:
        result = state.reset_strategy_state(mode='live', strategy=3)

        assert result['reset']['strategy'] == '3'
        reset_state = result['runtime_status']['live_strategy_states']['3']
        assert reset_state['round_index'] == 0
        assert reset_state['cash_pnl'] == -12.5
        assert reset_state['recovery_loss'] == 0.0
        assert reset_state['consecutive_losses'] == 0
        assert reset_state['consecutive_max_stake_skips'] == 0
        assert reset_state['stop_loss_count'] == 0
        assert result['runtime_status']['live_strategy_states']['7']['round_index'] == 5
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_refuses_to_reset_strategy_with_pending_live_order(tmp_path: Path):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text(
        'TRADE_MODE=live\nLIVE_TRADING_ENABLED=true\nLIVE_STRATEGY_IDS=7\n',
        encoding='utf-8',
    )
    logs_dir = tmp_path / 'logs'
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / 'live_session_state.json').write_text(
        json.dumps(
            {
                'live_strategies': {
                    '7': {
                        'round_index': 5,
                        'recovery_loss': 10.0,
                        'consecutive_losses': 3,
                        'pending_live_slug': 'btc-updown-5m-current',
                    }
                }
            }
        ),
        encoding='utf-8',
    )
    state = DashboardState(env_file=env_file)
    try:
        with pytest.raises(ValueError, match='待结算'):
            state.reset_strategy_state(mode='live', strategy=7)
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_update_config_notifies_runtime_manager(tmp_path: Path):
    calls: list[str] = []
    state = DashboardState(
        env_file=tmp_path / '.env.dashboard',
        notify_mode_change=lambda mode: calls.append(mode),
    )
    try:
        state.update_config({'TRADE_MODE': 'live', 'LIVE_TRADING_ENABLED': 'true'})
        assert calls == ['live']
    finally:
        state.close()


def test_dashboard_update_config_notifies_runtime_reload_for_market_timeframe(tmp_path: Path):
    calls: list[str] = []
    state = DashboardState(
        env_file=tmp_path / '.env.dashboard',
        notify_runtime_reload=lambda reason: calls.append(reason),
    )
    try:
        state.update_config({'MARKET_TIMEFRAME': '15m'})
        assert calls == ['market_timeframe']
    finally:
        state.close()

def test_dashboard_assets_format_recent_trade_round_slug_as_datetime():
    js = _dashboard_js()

    assert 'function formatRoundSlug(' in js
    assert "const match = raw.match(/-(\\d{10})(?:$|\\D)/);" in js
    assert "return dt.toLocaleString('zh-CN', { hour12: false });" in js
    assert "const roundDisplay = row.round_display_time ? fmtIso(row.round_display_time)" in js
    assert "'<td title=\"' + esc(row.event_slug || '--') + '\">' + esc(roundDisplay) + '</td>' +" in js

def test_recent_trades_payload_includes_result_validation_fields(tmp_path: Path, monkeypatch):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)

    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

        def get_event_by_slug(self, slug: str):
            return {
                'id': 'evt-1',
                'slug': slug,
                'title': 'Bitcoin Up or Down - April 6, 9:20AM-9:25AM ET',
                'startTime': '2026-04-06T13:20:00+00:00',
                'endDate': '2026-04-06T13:25:00+00:00',
                'eventMetadata': {
                    'priceToBeat': 69333.88601974999,
                    'finalPrice': 69412.14121057303,
                },
                'markets': [
                    {
                        'id': 'mkt-1',
                        'outcomes': '["Up", "Down"]',
                        'outcomePrices': '["1", "0"]',
                        'clobTokenIds': '["up-token", "down-token"]',
                    }
                ],
            }

    monkeypatch.setattr(dashboard, 'PolymarketClient', StubClient)

    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        logs_dir = tmp_path / 'logs'
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / 'paper_trades.csv').write_text(
            'timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason\n'
            '2026-04-06T13:32:38+00:00,paper,37,2,OPEN,btc-updown-5m-1775481600,2026-04-06T13:20:00+00:00,2026-04-06T13:25:00+00:00,UP,0.56,4.5454,2.5454,2.0,UP,2.0,4.7293,0.0,0,False,,,,,,False,\n',
            encoding='utf-8',
        )

        payload = state.get_recent_trades_payload(limit=10)
        row = payload['rows'][0]
        assert row['resolved_price_to_beat'] == '69333.88601974999'
        assert row['resolved_final_price'] == '69412.14121057303'
        assert row['resolved_expected_result'] == 'UP'
        assert row['result_check_status'] == 'match'
    finally:
        state.close()
        os.chdir(old_cwd)


def test_recent_trades_payload_caches_official_validation_by_slug(tmp_path: Path, monkeypatch):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    calls: list[str] = []

    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

        def get_event_by_slug(self, slug: str):
            calls.append(slug)
            return {
                'id': 'evt-1',
                'slug': slug,
                'eventMetadata': {
                    'priceToBeat': 69333.0,
                    'finalPrice': 69400.0,
                },
                'markets': [
                    {
                        'id': 'mkt-1',
                        'outcomes': '["Up", "Down"]',
                        'outcomePrices': '["1", "0"]',
                        'clobTokenIds': '["up-token", "down-token"]',
                    }
                ],
            }

    monkeypatch.setattr(dashboard, 'PolymarketClient', StubClient)
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        logs_dir = tmp_path / 'logs'
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / 'paper_trades.csv').write_text(
            'timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason\n'
            '2026-04-06T13:32:38+00:00,paper,37,2,OPEN,btc-updown-5m-same,2026-04-06T13:20:00+00:00,2026-04-06T13:25:00+00:00,UP,0.56,4.5454,2.5454,2.0,UP,2.0,4.7293,0.0,0,False,,,,,,False,\n'
            '2026-04-06T13:37:38+00:00,paper,38,2,OPEN,btc-updown-5m-same,2026-04-06T13:20:00+00:00,2026-04-06T13:25:00+00:00,UP,0.57,4.5454,2.5909,1.9545,UP,1.9545,6.6838,0.0,0,False,,,,,,False,\n',
            encoding='utf-8',
        )

        payload = state.get_recent_trades_payload(limit=10)

        assert payload['count'] == 2
        assert calls == ['btc-updown-5m-same']
        assert [row['result_check_status'] for row in payload['rows']] == ['match', 'match']
    finally:
        state.close()
        os.chdir(old_cwd)


def test_recent_trades_payload_validates_from_official_winning_outcome_when_final_price_missing(tmp_path: Path, monkeypatch):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)

    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

        def get_event_by_slug(self, slug: str):
            return {
                'id': 'evt-1',
                'slug': slug,
                'title': 'Bitcoin Up or Down - April 25, 11:15AM-11:30AM ET',
                'startTime': '2026-04-25T15:15:00+00:00',
                'endDate': '2026-04-25T15:30:00+00:00',
                'eventMetadata': {
                    'priceToBeat': 77688.48419,
                },
                'markets': [
                    {
                        'id': 'mkt-1',
                        'conditionId': 'condition-1',
                        'outcomes': '["Up", "Down"]',
                        'outcomePrices': '["1", "0"]',
                        'clobTokenIds': '["up-token", "down-token"]',
                        'closed': True,
                    }
                ],
            }

    monkeypatch.setattr(dashboard, 'PolymarketClient', StubClient)

    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        logs_dir = tmp_path / 'logs'
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / 'paper_trades.csv').write_text(
            'timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason\n'
            '2026-04-25T15:38:15+00:00,paper,33,4,OPEN,btc-updown-15m-1777130100,2026-04-25T15:15:00+00:00,2026-04-25T15:30:00+00:00,UP,0.52,1.9230,1.0,0.9230,UP,0.9230,11.7695,0.0,0,False,,,,,,False,\n',
            encoding='utf-8',
        )

        payload = state.get_recent_trades_payload(limit=10)
        row = payload['rows'][0]
        assert row['resolved_price_to_beat'] == '77688.48419'
        assert row['resolved_final_price'] == ''
        assert row['resolved_expected_result'] == 'UP'
        assert row['result_check_status'] == 'match'
    finally:
        state.close()
        os.chdir(old_cwd)


def test_recent_trades_payload_backfills_final_price_from_client_fallback_when_gamma_metadata_missing(tmp_path: Path, monkeypatch):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)

    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

        def get_event_by_slug(self, slug: str):
            return {
                'id': 'evt-1',
                'slug': slug,
                'eventMetadata': {
                    'priceToBeat': 77754.09567100879,
                },
                'markets': [
                    {
                        'id': 'mkt-1',
                        'outcomes': '["Up", "Down"]',
                        'outcomePrices': '["0", "1"]',
                        'closed': True,
                    }
                ],
            }

        def get_market_ahead_event_metadata(self, slug: str):
            assert slug == 'btc-updown-15m-1777293000'
            return {
                'finalPrice': 77745.88167854065,
                'priceToBeat': 77754.09567100879,
            }

    monkeypatch.setattr(dashboard, 'PolymarketClient', StubClient)

    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        logs_dir = tmp_path / 'logs'
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / 'paper_trades.csv').write_text(
            'timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason\n'
            '2026-04-27T12:48:40+00:00,paper,2,7,OPEN,btc-updown-15m-1777293000,2026-04-27T12:30:00+00:00,2026-04-27T12:45:00+00:00,UP,0.55,2.0,1.1,0.9,DOWN,-1.1,-0.2667,1.1,1,False,,,,,,False,\n',
            encoding='utf-8',
        )

        payload = state.get_recent_trades_payload(limit=10)
        row = payload['rows'][0]
        assert row['resolved_price_to_beat'] == '77754.09567100879'
        assert row['resolved_final_price'] == '77745.88167854065'
        assert row['resolved_expected_result'] == 'DOWN'
        assert row['result_check_status'] == 'match'
    finally:
        state.close()
        os.chdir(old_cwd)


def test_recent_trades_payload_marks_official_result_pending_when_winner_unavailable(tmp_path: Path, monkeypatch):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)

    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

        def get_event_by_slug(self, slug: str):
            return {
                'id': 'evt-1',
                'slug': slug,
                'eventMetadata': {'priceToBeat': 77688.48419},
                'markets': [
                    {
                        'id': 'mkt-1',
                        'outcomes': '["Up", "Down"]',
                        'outcomePrices': '["0.5", "0.5"]',
                        'clobTokenIds': '["up-token", "down-token"]',
                        'closed': False,
                    }
                ],
            }

    monkeypatch.setattr(dashboard, 'PolymarketClient', StubClient)

    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        logs_dir = tmp_path / 'logs'
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / 'paper_trades.csv').write_text(
            'timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason\n'
            '2026-04-25T15:38:15+00:00,paper,33,4,OPEN,btc-updown-15m-1777130100,2026-04-25T15:15:00+00:00,2026-04-25T15:30:00+00:00,UP,0.52,1.9230,1.0,0.9230,UP,0.9230,11.7695,0.0,0,False,,,,,,False,\n',
            encoding='utf-8',
        )

        payload = state.get_recent_trades_payload(limit=10)
        row = payload['rows'][0]
        assert row['resolved_expected_result'] == ''
        assert row['result_check_status'] == 'official_pending'
    finally:
        state.close()
        os.chdir(old_cwd)


def test_recent_trades_payload_validates_from_official_token_winner(tmp_path: Path, monkeypatch):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)

    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

        def get_event_by_slug(self, slug: str):
            return {
                'id': 'evt-1',
                'slug': slug,
                'eventMetadata': {},
                'markets': [
                    {
                        'id': 'mkt-1',
                        'tokens': [
                            {'token_id': 'up-token', 'outcome': 'Up', 'winner': True},
                            {'token_id': 'down-token', 'outcome': 'Down', 'winner': False},
                        ],
                    }
                ],
            }

    monkeypatch.setattr(dashboard, 'PolymarketClient', StubClient)

    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        logs_dir = tmp_path / 'logs'
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / 'paper_trades.csv').write_text(
            'timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason\n'
            '2026-04-25T15:38:15+00:00,paper,33,4,OPEN,btc-updown-15m-1777130100,2026-04-25T15:15:00+00:00,2026-04-25T15:30:00+00:00,UP,0.52,1.9230,1.0,0.9230,UP,0.9230,11.7695,0.0,0,False,,,,,,False,\n',
            encoding='utf-8',
        )

        payload = state.get_recent_trades_payload(limit=10)
        row = payload['rows'][0]
        assert row['resolved_expected_result'] == 'UP'
        assert row['result_check_status'] == 'match'
    finally:
        state.close()
        os.chdir(old_cwd)


def test_recent_trades_payload_caches_official_lookup_errors(tmp_path: Path, monkeypatch):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    calls: list[str] = []

    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

        def get_event_by_slug(self, slug: str):
            calls.append(slug)
            raise RuntimeError('gamma timeout')

    monkeypatch.setattr(dashboard, 'PolymarketClient', StubClient)
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        logs_dir = tmp_path / 'logs'
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / 'paper_trades.csv').write_text(
            'timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason\n'
            '2026-04-25T15:38:15+00:00,paper,33,4,OPEN,btc-updown-15m-1777130100,2026-04-25T15:15:00+00:00,2026-04-25T15:30:00+00:00,UP,0.52,1.9230,1.0,0.9230,UP,0.9230,11.7695,0.0,0,False,,,,,,False,\n',
            encoding='utf-8',
        )

        first = state.get_recent_trades_payload(limit=10)['rows'][0]
        second = state.get_recent_trades_payload(limit=10)['rows'][0]

        assert calls == ['btc-updown-15m-1777130100']
        assert first['result_check_status'] == 'error'
        assert first['result_check_error'] == 'RuntimeError: gamma timeout'
        assert second['result_check_status'] == 'error'
        assert second['result_check_error'] == 'RuntimeError: gamma timeout'
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_assets_include_recent_result_validation_column():
    html = _dashboard_html()
    js = _dashboard_js()

    assert '<th>校验</th>' in html
    assert 'function resultCheckText(' in js
    assert "row.result_check_status" in js
    assert "if (status === 'official_pending') return '待官方结算';" in js
    assert "if (status === 'error') return '校验异常';" in js
    assert "row.result_check_error" in js
    assert "resolved_price_to_beat" in js
    assert "resolved_final_price" in js


def test_dashboard_assets_include_recent_strategy_column():
    html = _dashboard_html()
    js = _dashboard_js()

    assert '<th>策略</th>' in html
    assert "'<td>' + esc(row.strategy || '--') + '</td>' +" in js
    assert "tbody.innerHTML = '<tr><td colspan=14 class=empty>'" in js


def test_dashboard_assets_include_recent_result_prices_and_status_styles():
    html = _dashboard_html()
    js = _dashboard_js()
    css = dashboard._dashboard_css()

    assert '<th>开盘价</th>' in html
    assert '<th>收盘价</th>' in html
    assert "fmtNum(row.resolved_price_to_beat, 2)" in js
    assert "fmtNum(row.resolved_final_price, 2)" in js
    assert "const checkCls = row.result_check_status === 'match' ? 'trade-up' : ((row.result_check_status === 'mismatch') ? 'trade-down' : 'trade-skip');" in js
    assert '.trade-up { color: var(--green); font-weight: 700; }' in css
    assert '.trade-down { color: var(--red); font-weight: 700; }' in css
    assert '.trade-skip { color: var(--amber); font-weight: 700; }' in css

def test_dashboard_assets_include_chinese_summary_table_headers():
    html = _dashboard_html()

    assert '<th>日期</th>' in html
    assert '<th>交易</th>' in html
    assert '<th>命中率</th>' in html
    assert '<th>总盈亏</th>' in html
    assert '<th>回撤</th>' in html

def test_dashboard_config_payload_includes_paper_strategy_ids(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        payload = state.get_config_payload()
        assert 'PAPER_STRATEGY_IDS' in payload['editable_keys']
        assert 'STRATEGY_IDS' in payload['editable_keys']
        assert payload['select_options']['STRATEGY_ID'] == ['1', '2', '3', '4', '5', '6', '7', '8']
        assert payload['select_options']['STRATEGY_IDS'] == ['1', '2', '3', '4', '5', '6', '7', '8']
        assert payload['select_options']['PAPER_STRATEGY_IDS'] == ['1', '2', '3', '4', '5', '6', '7', '8']
    finally:
        state.close()


def test_dashboard_config_payload_includes_paper_simulated_wallet_balance(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        payload = state.get_config_payload()
        assert 'PAPER_SIMULATED_WALLET_BALANCE' in payload['editable_keys']
        assert payload['env_values']['PAPER_SIMULATED_WALLET_BALANCE'] == '1000000.0'
        assert payload['labels']['PAPER_SIMULATED_WALLET_BALANCE'] == '纸面模拟钱包余额'
        assert payload['field_help']['PAPER_SIMULATED_WALLET_BALANCE'].startswith('仅纸面模式使用')
    finally:
        state.close()


def test_dashboard_config_payload_includes_market_timeframe_selector(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        payload = state.get_config_payload()
        assert 'MARKET_TIMEFRAME' in payload['editable_keys']
        assert payload['select_options']['MARKET_TIMEFRAME'] == ['5m', '15m']
        assert payload['labels']['MARKET_TIMEFRAME'] == '市场频次'
        assert 'MARKET_TIMEFRAME' in payload['field_groups'][0]['keys']
    finally:
        state.close()


def test_dashboard_config_payload_includes_structured_timeframe_presets(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        payload = state.get_config_payload()
        presets = payload['timeframe_presets']
        assert set(presets) == {'5m', '15m'}
        assert set(presets['5m']) == {'shared', 'strategy5', 'strategy6', 'strategy7'}
        assert set(presets['15m']) == {'shared', 'strategy5', 'strategy6', 'strategy7'}
        assert presets['5m']['shared']['OPEN_DELAY_SECONDS'] == '12'
        assert presets['5m']['strategy5']['SIGNAL_MOMENTUM_THRESHOLD'] == '0.020'
        assert presets['5m']['strategy6']['OFI_THRESHOLD'] == '0.72'
        assert presets['5m']['strategy7']['STRATEGY7_OFI_THRESHOLD'] == '0.58'
    finally:
        state.close()


def test_dashboard_rejects_invalid_market_timeframe(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        with pytest.raises(ConfigValidationError) as excinfo:
            state.update_config({'MARKET_TIMEFRAME': '10m'})
        assert 'MARKET_TIMEFRAME' in excinfo.value.field_errors
    finally:
        state.close()


def test_dashboard_assets_include_timeframe_copy_hooks():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="brandTitle"' in html
    assert 'id="marketPanelDesc"' in html
    assert "const TIMEFRAME_META = {" in js
    assert "function applyTimeframeCopy(payload)" in js
    assert '"15m": {' in js
    assert '"10m"' not in js


def test_dashboard_assets_include_timeframe_preset_auto_apply_logic():
    js = _dashboard_js()

    assert "timeframe_presets" in js
    assert "MARKET_TIMEFRAME" in js
    assert "function applyTimeframePreset(" in js
    assert "const preset = presets[String(timeframe || '').toLowerCase()];" in js
    assert "const flatPreset = flattenTimeframePreset(preset);" in js
    assert "Object.entries(flatPreset).forEach(([key, value]) => {" in js
    assert "const field = el('cfg_' + key);" in js


def test_dashboard_assets_merge_structured_timeframe_presets():
    js = _dashboard_js()

    assert "function flattenTimeframePreset(" in js
    assert "preset.shared" in js
    assert "preset.strategy5" in js
    assert "preset.strategy6" in js
    assert "preset.strategy7" in js


def test_dashboard_assets_keep_paper_profile_data_without_editor_copy():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="paperProfilesRoot"' in html
    assert "function renderPaperProfiles(" in js
    assert "paper_timeframes" in js
    assert "paper_profiles" in js
    assert "node.innerHTML = '';" in js


def test_dashboard_assets_include_multi_timeframe_paper_runtime_cards():
    html = _dashboard_html()
    js = _dashboard_js()

    assert 'id="paperRuntimeCards"' not in html
    assert "function renderPaperRuntimeCards(" in js
    assert "function refreshPaperRuntimeCard(" in js
    assert "纸面运行" in js
    assert "该时间频次的纸面运行状态" in js
    assert "setInterval(refreshPaperRuntimeCards, POLL_MS.market);" not in js


def test_dashboard_timeframe_presets_only_include_timeframe_sensitive_fields(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        payload = state.get_config_payload()
        presets = payload['timeframe_presets']
        for timeframe in ('5m', '15m'):
            preset = presets[timeframe]
            assert set(preset) == {'shared', 'strategy5', 'strategy6', 'strategy7'}
            assert 'OPEN_DELAY_SECONDS' in preset['shared']
            assert 'SIGNAL_LOCK_BEFORE_ENTRY_SECONDS' in preset['shared']
            assert 'SIGNAL_MOMENTUM_THRESHOLD' in preset['strategy5']
            assert 'SIGNAL_FALLBACK_STRATEGY_ID' in preset['strategy5']
            assert 'OFI_THRESHOLD' in preset['strategy6']
            assert 'BINANCE_SIGNAL_STALE_SECONDS' in preset['strategy6']
            assert 'STRATEGY7_OFI_THRESHOLD' in preset['strategy7']
            assert 'TRADE_MODE' not in preset['shared']
            assert 'POLYMARKET_PRIVATE_KEY' not in preset['strategy5']
            assert 'LIVE_TRADING_ENABLED' not in preset['strategy6']
    finally:
        state.close()


def test_dashboard_update_config_notifies_runtime_reload_after_market_timeframe_save_with_presets(tmp_path: Path):
    calls: list[str] = []
    state = DashboardState(
        env_file=tmp_path / '.env.dashboard',
        notify_runtime_reload=lambda reason: calls.append(reason),
    )
    try:
        payload = state.update_config(
            {
                'MARKET_TIMEFRAME': '15m',
                'OPEN_DELAY_SECONDS': '25',
                'SIGNAL_LOCK_BEFORE_ENTRY_SECONDS': '20',
                'SIGNAL_MOMENTUM_THRESHOLD': '0.015',
                'SIGNAL_FALLBACK_STRATEGY_ID': '2',
                'MAX_PRICE_THRESHOLD': '0.65',
                'TARGET_PROFIT': '1.0',
                'OFI_THRESHOLD': '0.65',
                'BINANCE_SIGNAL_STALE_SECONDS': '2.0',
                'STRATEGY7_OFI_THRESHOLD': '0.50',
                'STRATEGY7_MOMENTUM_THRESHOLD': '0.005',
                'MAX_ENTRY_PRICE': '0.55',
                'STRATEGY7_MIN_SIGNAL_GAP': '0.01',
                'STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS': '3',
                'STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP': '0.03',
                'STRATEGY7_LATE_CONFIRM_RELAX_SECONDS': '3',
            }
        )
        assert payload['env_values']['MARKET_TIMEFRAME'] == '15m'
        assert payload['env_values']['SIGNAL_MOMENTUM_THRESHOLD'] == '0.015'
        assert payload['env_values']['OFI_THRESHOLD'] == '0.65'
        assert calls == ['market_timeframe']
    finally:
        state.close()


def test_dashboard_update_config_normalizes_paper_strategy_ids(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    state = DashboardState(env_file=env_file)
    try:
        payload = state.update_config({'PAPER_STRATEGY_IDS': '6, 2, 6, 1'})
        assert payload['env_values']['PAPER_STRATEGY_IDS'] == '6,2,1'
        text = env_file.read_text(encoding='utf-8')
        assert 'PAPER_STRATEGY_IDS=6,2,1' in text
    finally:
        state.close()


def test_dashboard_config_payload_includes_paper_timeframes_and_profiles(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text(
        '\n'.join(
            [
                'TRADE_MODE=paper',
                'PAPER_TIMEFRAMES=5m,15m',
                'PAPER_5M_STRATEGY_ID=5',
                'PAPER_5M_STRATEGY_IDS=5,6',
                'PAPER_15M_STRATEGY_ID=2',
                'PAPER_15M_STRATEGY_IDS=1,2',
            ]
        ) + '\n',
        encoding='utf-8',
    )
    state = DashboardState(env_file=env_file)
    try:
        payload = state.get_config_payload()
        assert payload['paper_timeframes'] == ['5m', '15m']
        assert payload['paper_profiles']['5m']['strategy_id'] == '5'
        assert payload['paper_profiles']['5m']['paper_strategy_ids'] == ['5', '6']
        assert payload['paper_profiles']['15m']['strategy_id'] == '2'
        assert payload['paper_profiles']['15m']['paper_strategy_ids'] == ['1', '2']
    finally:
        state.close()


def test_dashboard_paper_profile_copy_is_localized(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        payload = state.get_config_payload()
        assert payload['labels']['PAPER_15M_STRATEGY_ID'] == '15m 纸面配置 · 基础策略'
        assert payload['labels']['PAPER_15M_STRATEGY_IDS'] == '15m 纸面配置 · 纸面策略组合'
        assert payload['labels']['PAPER_15M_TARGET_PROFIT'] == '15m 纸面配置 · 每次目标净利'
        assert payload['field_help']['PAPER_15M_TARGET_PROFIT'] == '仅作用于 15m 纸面配置。'
    finally:
        state.close()

    js = _dashboard_js()
    assert 'Paper Profiles' not in js
    assert '独立 paper profile' not in js
    assert 'paper runtime card' not in js
    assert '按 timeframe 独立编辑 paper 配置。' not in js
    assert '纸面配置组' in js
    assert '按时间频次独立编辑纸面配置。' in js
    assert '独立纸面配置' in js


def test_dashboard_config_labels_and_help_reduce_english_copy(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        payload = state.get_config_payload()
        assert payload['labels']['POLYMARKET_BUILDER_API_KEY'] == 'Builder 自动赎回接口密钥'
        assert payload['labels']['POLYMARKET_BUILDER_SECRET'] == 'Builder 自动赎回签名密钥'
        assert payload['labels']['POLYMARKET_BUILDER_PASSPHRASE'] == 'Builder 自动赎回口令'
        assert payload['labels']['POLYMARKET_RELAYER_API_KEY'] == 'Relayer 接口密钥'
        assert payload['labels']['POLYMARKET_RELAYER_API_KEY_ADDRESS'] == 'Relayer 密钥地址'
        assert '官方 gasless redeem 的 Builder API key' not in payload['field_help']['POLYMARKET_BUILDER_API_KEY']
        assert '官方 gasless redeem 的 Relayer API key' not in payload['field_help']['POLYMARKET_RELAYER_API_KEY']
        assert '仅用于自动赎回' in payload['field_help']['POLYMARKET_BUILDER_API_KEY']
        assert '仅用于自动赎回认证' in payload['field_help']['POLYMARKET_RELAYER_API_KEY_ADDRESS']
    finally:
        state.close()

    js = _dashboard_js()
    assert 'Builder Redeem API Key' not in js
    assert 'Builder Redeem Secret' not in js
    assert 'Builder Redeem Passphrase' not in js
    assert 'Relayer API Key' not in js
    assert 'Relayer Key Address' not in js


def test_dashboard_help_center_reduces_internal_english_terms():
    js = _dashboard_js()

    assert 'LIVE_AUTO_REDEEM_ENABLED 是什么意思？' not in js
    assert 'LIVE_AUTO_REDEEM_DRY_RUN 需要去掉吗？' not in js
    assert '先看下注计划与风控里的 skip_reason' not in js
    assert 'should_trade=true 说明当前轮次、价格、风控检查和 WS 防护都允许执行。' not in js
    assert 'should_trade=false 时先结合 skip_reason 字段一起看，不要先默认程序坏了。' not in js
    assert 'Dashboard 操作说明' not in js

    assert '实盘自动赎回开关是什么意思？' in js
    assert '自动赎回演练模式需要关闭吗？' in js
    assert '跳过原因' in js
    assert '允许下单=是' in js
    assert '允许下单=否' in js
    assert '监控面板操作说明' in js


def test_dashboard_user_copy_reduces_ws_http_and_ofi_terms(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        payload = state.get_config_payload()
        assert payload['labels']['OFI_THRESHOLD'] == '盘口失衡阈值'
        assert payload['labels']['STRATEGY7_OFI_THRESHOLD'] == '策略7 盘口失衡阈值'
        assert payload['labels']['BINANCE_SIGNAL_STALE_SECONDS'] == '盘口信号过期秒'
        assert payload['strategy_catalog']['6']['label'] == '币安盘口失衡'
        assert payload['strategy_catalog']['7']['label'] == '盘口+动量共识'
    finally:
        state.close()

    js = _dashboard_js()
    assert 'HTTP回退' not in js
    assert 'WebSocket 订阅请求无效' not in js
    assert '暂无 WS 运行数据' not in js
    assert 'OFI 判断' not in js
    assert 'Binance OFI 失衡' not in js

    assert '接口回退' in js
    assert '实时连接订阅请求无效' in js
    assert '暂无实时连接运行数据' in js
    assert '盘口失衡判断' in js
    assert '币安盘口失衡' in js


def test_dashboard_recent_trades_payload_reads_timeframe_specific_paths(tmp_path: Path, monkeypatch):
    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

        def get_event_by_slug(self, slug: str):
            return {
                'slug': slug,
                'eventMetadata': {'priceToBeat': 1.0, 'finalPrice': 2.0},
                'markets': [{'outcomes': '["Up", "Down"]', 'outcomePrices': '["1", "0"]'}],
            }

    monkeypatch.setattr(dashboard, 'PolymarketClient', StubClient)
    logs_dir = tmp_path / 'logs' / 'paper'
    (logs_dir / '5m').mkdir(parents=True, exist_ok=True)
    (logs_dir / '15m').mkdir(parents=True, exist_ok=True)
    header = (
        'timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,'
        'order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,'
        'skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,'
        'signal_reason,experiment_id\n'
    )
    (logs_dir / '5m' / 'paper_trades.csv').write_text(
        header
        + '2026-04-22T10:00:00+00:00,paper,1,5,OPEN,btc-updown-5m-a,2026-04-22T09:55:00+00:00,2026-04-22T10:00:00+00:00,UP,0.5,2,1,1,UP,1,1,0,0,False,,,,,,False,,\n',
        encoding='utf-8',
    )
    (logs_dir / '15m' / 'paper_trades.csv').write_text(
        header
        + '2026-04-22T10:15:00+00:00,paper,1,2,OPEN,btc-updown-15m-a,2026-04-22T10:00:00+00:00,2026-04-22T10:15:00+00:00,DOWN,0.4,2.5,1,1,DOWN,1,1,0,0,False,,,,,,False,,\n',
        encoding='utf-8',
    )
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        state._cfg.logs_dir = tmp_path / 'logs'
        payload_5m = state.get_recent_trades_payload(limit=20, timeframe='5m')
        payload_15m = state.get_recent_trades_payload(limit=20, timeframe='15m')
        assert payload_5m['rows'][0]['event_slug'] == 'btc-updown-5m-a'
        assert payload_15m['rows'][0]['event_slug'] == 'btc-updown-15m-a'
    finally:
        state.close()


def test_dashboard_update_config_accepts_paper_timeframe_profile_fields(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    state = DashboardState(env_file=env_file)
    try:
        payload = state.update_config(
            {
                'PAPER_TIMEFRAMES': '5m,15m',
                'PAPER_5M_STRATEGY_ID': '5',
                'PAPER_5M_STRATEGY_IDS': '5,6',
                'PAPER_5M_TARGET_PROFIT': '0.8',
                'PAPER_15M_STRATEGY_ID': '2',
                'PAPER_15M_STRATEGY_IDS': '1,2',
                'PAPER_15M_TARGET_PROFIT': '1.0',
            }
        )

        assert payload['env_values']['PAPER_TIMEFRAMES'] == '5m,15m'
        assert payload['env_values']['PAPER_5M_TARGET_PROFIT'] == '0.8'
        assert payload['env_values']['PAPER_15M_TARGET_PROFIT'] == '1.0'
        text = env_file.read_text(encoding='utf-8')
        assert 'PAPER_TIMEFRAMES=5m,15m' in text
        assert 'PAPER_5M_STRATEGY_IDS=5,6' in text
        assert 'PAPER_15M_STRATEGY_IDS=1,2' in text
    finally:
        state.close()


def test_dashboard_update_config_accepts_strategy_6_as_primary(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    state = DashboardState(env_file=env_file)
    try:
        payload = state.update_config({'STRATEGY_ID': '6', 'PAPER_STRATEGY_IDS': '1,6'})
        assert payload['env_values']['STRATEGY_ID'] == '6'
        assert payload['env_values']['PAPER_STRATEGY_IDS'] == '1,6'
        text = env_file.read_text(encoding='utf-8')
        assert 'STRATEGY_ID=6' in text
    finally:
        state.close()


def test_dashboard_config_payload_includes_strategy7_fields(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        payload = state.get_config_payload()
        assert payload['select_options']['STRATEGY_ID'] == ['1', '2', '3', '4', '5', '6', '7', '8']
        assert payload['labels']['STRATEGY7_OFI_THRESHOLD'] == '策略7 盘口失衡阈值'
        assert payload['labels']['STRATEGY7_MOMENTUM_THRESHOLD'] == '策略7 动量阈值'
        assert payload['labels']['STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP'] == '策略7 强信号额外优势'
        assert payload['labels']['STRATEGY7_LATE_CONFIRM_RELAX_SECONDS'] == '策略7 强信号放宽秒数'
        assert payload['field_scope']['STRATEGY7_OFI_THRESHOLD'] == 'strategy_7_only'
        assert payload['field_scope']['STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP'] == 'strategy_7_only'
        assert 'MAX_ENTRY_PRICE' in payload['editable_keys']
        assert 'STRATEGY7_LATE_CONFIRM_RELAX_SECONDS' in payload['editable_keys']
    finally:
        state.close()


def test_dashboard_update_config_accepts_strategy7_values(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    state = DashboardState(env_file=env_file)
    try:
        payload = state.update_config({
            'STRATEGY_ID': '7',
            'PAPER_STRATEGY_IDS': '7',
            'STRATEGY7_OFI_THRESHOLD': '0.7',
            'STRATEGY7_MOMENTUM_THRESHOLD': '0.025',
            'MAX_ENTRY_PRICE': '0.54',
            'STRATEGY7_MIN_SIGNAL_GAP': '0.03',
            'STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS': '12',
            'STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP': '0.02',
            'STRATEGY7_LATE_CONFIRM_RELAX_SECONDS': '4',
        })
        assert payload['env_values']['STRATEGY_ID'] == '7'
        assert payload['env_values']['STRATEGY7_OFI_THRESHOLD'] == '0.7'
        assert payload['env_values']['MAX_ENTRY_PRICE'] == '0.54'
        assert payload['env_values']['STRATEGY7_CONFIRM_BEFORE_ENTRY_SECONDS'] == '12'
        assert payload['env_values']['STRATEGY7_LATE_CONFIRM_STRONG_SIGNAL_GAP'] == '0.02'
        assert payload['env_values']['STRATEGY7_LATE_CONFIRM_RELAX_SECONDS'] == '4.0'
    finally:
        state.close()


def test_dashboard_rejects_invalid_paper_strategy_ids(tmp_path: Path):
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        with pytest.raises(ConfigValidationError) as excinfo:
            state.update_config({'PAPER_STRATEGY_IDS': '9,x'})
        assert 'PAPER_STRATEGY_IDS' in excinfo.value.field_errors
    finally:
        state.close()


def test_dashboard_paper_payloads_filter_by_strategy(tmp_path: Path):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        logs_dir = tmp_path / 'logs'
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / 'paper_trades.csv').write_text(
            'timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason\n'
            '2026-04-06T08:00:00+00:00,paper,10,1,OPEN,slug-one,2026-04-06T07:55:00+00:00,2026-04-06T08:00:00+00:00,UP,0.50,2.0,1.0,1.0,UP,1.0,1.0,0.0,0,False,,,,,,False,\n'
            '2026-04-06T08:05:00+00:00,paper,11,6,OPEN,slug-six,2026-04-06T08:00:00+00:00,2026-04-06T08:05:00+00:00,DOWN,0.40,2.5,1.0,1.5,DOWN,1.5,2.5,0.0,0,False,,,,,,False,\n',
            encoding='utf-8',
        )
        (logs_dir / 'session_state.json').write_text(
            json.dumps(
                {
                    'paper_strategies': {
                        '1': {
                            'round_index': 12,
                            'cash_pnl': 1.0,
                            'recovery_loss': 0.0,
                            'consecutive_losses': 0,
                            'consecutive_max_stake_skips': 0,
                            'signal_round_slug': None,
                            'signal_round_open_up_price': None,
                            'signal_round_locked_side': None,
                            'strategy6_last_ofi_score': None,
                            'stop_loss_count': 0,
                            'daily_realized_pnl': 1.0,
                            'current_day': '2026-04-06',
                            'pending_paper_trades': []
                        },
                        '6': {
                            'round_index': 13,
                            'cash_pnl': 2.5,
                            'recovery_loss': 0.0,
                            'consecutive_losses': 0,
                            'consecutive_max_stake_skips': 0,
                            'signal_round_slug': None,
                            'signal_round_open_up_price': None,
                            'signal_round_locked_side': None,
                            'strategy6_last_ofi_score': 0.7,
                            'stop_loss_count': 0,
                            'daily_realized_pnl': 2.5,
                            'current_day': '2026-04-06',
                            'pending_paper_trades': [
                                {
                                    'round_index': 12,
                                    'event_slug': 'pending-six',
                                    'start_time': '2026-04-06T08:10:00+00:00',
                                    'end_time': '2026-04-06T08:15:00+00:00',
                                    'side': 'UP',
                                    'price': 0.45,
                                    'order_size': 2.0,
                                    'order_cost': 0.9,
                                    'expected_profit': 1.1,
                                    'strategy': 6,
                                    'entry_timing': 'OPEN',
                                    'signal_open_up_price': None,
                                    'signal_current_up_price': None,
                                    'signal_threshold': 0.2,
                                    'signal_delta': 0.4,
                                    'signal_locked': False,
                                    'signal_reason': None,
                                    'queued_at': '2026-04-06T08:10:05+00:00'
                                }
                            ]
                        }
                    }
                }
            ),
            encoding='utf-8',
        )

        summary_all = state.get_paper_summary_payload()
        summary_six = state.get_paper_summary_payload(strategy=6)
        recent_six = state.get_recent_trades_payload(limit=10, strategy=6)

        assert summary_all['latest']['trade_rows'] == 2
        assert summary_six['latest']['trade_rows'] == 1
        assert summary_six['latest']['total_pnl'] == 1.5
        assert recent_six['count'] == 2
        assert recent_six['rows'][0]['event_slug'] == 'pending-six'
        assert all(row['strategy'] == '6' for row in recent_six['rows'])
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_recent_payload_preserves_experiment_id(tmp_path: Path):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        logs_dir = tmp_path / 'logs'
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / 'paper_trades.csv').write_text(
            'timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason,experiment_id\n'
            '2026-04-06T08:00:00+00:00,paper,10,5,OPEN,slug-one,2026-04-06T07:55:00+00:00,2026-04-06T08:00:00+00:00,UP,0.50,2.0,1.0,1.0,UP,1.0,1.0,0.0,0,False,,,,,,False,,challenger-s5-a\n',
            encoding='utf-8',
        )

        payload = state.get_recent_trades_payload(limit=10)

        assert payload['rows'][0]['experiment_id'] == 'challenger-s5-a'
    finally:
        state.close()
        os.chdir(old_cwd)

def test_dashboard_market_payload_can_switch_strategy_view(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text('STRATEGY_ID=1\nPAPER_STRATEGY_IDS=1,6\n', encoding='utf-8')
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    state = DashboardState(env_file=env_file)
    try:
        base_payload = state.get_market_payload()
        strategy_six_payload = state.get_market_payload(strategy=6)

        assert base_payload['strategy_view']['selected'] == '1'
        assert base_payload['strategy6']['enabled'] is False
        assert strategy_six_payload['strategy_view']['selected'] == '6'
        assert strategy_six_payload['strategy6']['enabled'] is True
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_assets_include_strategy7_copy_and_reasons(tmp_path: Path):
    html = _dashboard_html()
    js = _dashboard_js()
    state = DashboardState(env_file=tmp_path / '.env.dashboard')
    try:
        payload = state.get_config_payload()
    finally:
        state.close()

        assert payload['strategy_catalog']['7']['label'] == '盘口+动量共识'
    assert payload['strategy_catalog']['7']['summary'] == '只有币安盘口失衡和预测市场动量同向时才允许交易。'
    assert 'strategy7_signal_conflict' in js
    assert 'strategy7_confidence_too_low' in js
    assert '盘口失衡与动量需同向确认' in js
    assert '策略7 盘口失衡阈值' in js
    assert 'id=strategy7Panel' in html
    assert 'strategy7Agreement' in html
    assert 'strategy7QualityGate' in html
    assert 'strategy7FinalReason' in html


def test_dashboard_market_payload_can_show_strategy7_view(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text('STRATEGY_ID=7\nPAPER_STRATEGY_IDS=7\n', encoding='utf-8')
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    state = DashboardState(env_file=env_file)
    try:
        payload = state.get_market_payload(strategy='7')

        assert payload['strategy_view']['selected'] == '7'
        assert payload['strategy7']['enabled'] is True
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_report_strategy_selection_survives_market_refresh_browser_regression(tmp_path: Path, monkeypatch):
    npx_path = _require_dashboard_browser_regressions()

    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

    monkeypatch.setattr(dashboard, 'PolymarketClient', StubClient)

    strategy_catalog = json.loads(json.dumps(DashboardState.STRATEGY_CATALOG))

    def fake_get_config_payload(self):
        return {
            'env_file': str(tmp_path / '.env.dashboard'),
            'env_values': {
                'TRADE_MODE': 'paper',
                'MARKET_TIMEFRAME': '15m',
                'STRATEGY_ID': '1',
                'PAPER_STRATEGY_IDS': '1,7',
                'PAPER_TIMEFRAMES': '15m',
            },
            'timeframe_presets': {'5m': {}, '15m': {}},
            'editable_keys': ['TRADE_MODE', 'MARKET_TIMEFRAME', 'STRATEGY_ID', 'PAPER_STRATEGY_IDS'],
            'labels': DashboardState.CONFIG_LABELS,
            'select_options': {
                'TRADE_MODE': ['paper', 'live'],
                'MARKET_TIMEFRAME': ['5m', '15m'],
                'STRATEGY_ID': ['1', '7'],
                'PAPER_STRATEGY_IDS': ['1', '7'],
            },
            'strategy_catalog': strategy_catalog,
            'field_groups': [{'title': '基础策略', 'description': '', 'keys': ['STRATEGY_ID', 'PAPER_STRATEGY_IDS']}],
            'field_scope': {},
            'field_help': {},
            'validation_errors': {},
            'runtime_status': {
                'saved_mode': 'paper',
                'running_mode': 'paper',
                'restart_required': False,
                'live_ready': False,
                'live_validation_error': None,
                'active_mode': 'paper',
                'desired_mode': 'paper',
                'switch_state': 'idle',
                'switch_reason': None,
                'current_round_slug': None,
                'round_in_progress': False,
                'safe_to_switch': True,
                'pending_live_order': False,
                'redeem_visible': False,
                'redeem_enabled': False,
                'redeem_auth_mode': 'unconfigured',
                'redeem_pending_count': 0,
                'redeem_last_result': None,
                'redeem_last_attempt_at': None,
                'redeem_last_submission_id': None,
                'redeem_last_submission_status': None,
                'redeem_last_tx_hash': None,
                'optimizer_enabled': False,
                'optimizer_last_run_at': None,
                'optimizer_champion_id': None,
                'optimizer_active_challengers': [],
                'optimizer_promotable_count': 0,
            },
            'saved_at': None,
            'paper_timeframes': ['15m'],
            'paper_profiles': {
                '15m': {
                    'strategy_id': '1',
                    'paper_strategy_ids': ['1', '7'],
                    'target_profit': '1.0',
                    'bet_sizing_mode': 'FIXED_BASE_COST',
                    'base_order_cost': '1.0',
                    'max_consecutive_losses': '7',
                    'max_stake': '',
                    'open_delay_seconds': '25',
                    'signal_momentum_threshold': '0.015',
                    'ofi_threshold': '0.65',
                    'binance_signal_stale_seconds': '2.0',
                    'strategy7_ofi_threshold': '0.5',
                    'strategy7_momentum_threshold': '0.005',
                    'strategy7_max_entry_price': '0.55',
                }
            },
        }

    def fake_get_market_payload(self, *, strategy=None, timeframe=None):
        return {
            'ok': True,
            'timestamp': '2026-04-23T03:40:00+00:00',
            'round': {
                'slug': 'btc-updown-15m-current',
                'title': 'BTC 15m Current',
                'start_time': '2026-04-23T03:30:00+00:00',
                'end_time': '2026-04-23T03:45:00+00:00',
                'entry_time': '2026-04-23T03:30:25+00:00',
                'is_current': True,
                'seconds_to_entry': -10,
                'seconds_to_end': 300,
            },
            'quote': {
                'source': 'websocket',
                'accepting_orders': True,
                'up_price': 0.5,
                'up_best_bid': 0.49,
                'up_best_ask': 0.51,
                'down_price': 0.5,
                'down_best_bid': 0.49,
                'down_best_ask': 0.51,
                'fetched_at': '2026-04-23T03:40:00+00:00',
            },
            'signal': {'side': None, 'reason': 'signal_unavailable', 'open_up': None, 'current_up': None, 'threshold': None, 'delta': None, 'locked': False},
            'plan': {'should_trade': False, 'side': None, 'price': None, 'order_size': 0.0, 'order_cost': 0.0, 'expected_profit': 0.0, 'skip_reason': 'signal_unavailable', 'stop_loss_triggered': False},
            'session_state': {'round_index': 1, 'cash_pnl': 0.0, 'recovery_loss': 0.0, 'consecutive_losses': 0, 'stop_loss_count': 0, 'daily_realized_pnl': 0.0, 'pending_paper_trades': []},
            'ws_runtime': {},
            'ws_stale_guard_triggered': False,
            'strategy6': {'enabled': False, 'ofi_score': None, 'signal_at': None, 'stale': False, 'threshold': 0.65, 'max_entry_price': 0.56, 'bid_price': None, 'bid_qty': None, 'ask_price': None, 'ask_qty': None},
            'strategy7': {'enabled': False, 'ofi_score': None, 'momentum_delta': None, 'agreement': None, 'quality_gate': None, 'final_reason': None},
            'strategy_view': {'selected': '1', 'paper_strategy_ids': ['1', '7'], 'available': ['1', '7'], 'timeframe': '15m'},
        }

    def fake_get_paper_summary_payload(self, *, strategy=None, timeframe=None):
        return {'csv_path': 'paper.csv', 'tz_offset': '+08:00', 'strategy': str(strategy or 'all'), 'timeframe': str(timeframe or '15m'), 'days': [], 'latest': None}

    def fake_get_recent_trades_payload(self, *, limit, strategy=None, timeframe=None):
        return {'csv_path': 'paper.csv', 'strategy': str(strategy or 'all'), 'timeframe': str(timeframe or '15m'), 'count': 0, 'rows': []}

    monkeypatch.setattr(DashboardState, 'get_config_payload', fake_get_config_payload)
    monkeypatch.setattr(DashboardState, 'get_market_payload', fake_get_market_payload)
    monkeypatch.setattr(DashboardState, 'get_paper_summary_payload', fake_get_paper_summary_payload)
    monkeypatch.setattr(DashboardState, 'get_recent_trades_payload', fake_get_recent_trades_payload)

    runtime = create_dashboard_runtime(host='127.0.0.1', port=0, env_file=tmp_path / '.env.dashboard')
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()

    port = runtime.server.server_address[1]
    session = f"dashboard-report-{uuid.uuid4().hex}"

    def pw(*args: str) -> str:
        completed = subprocess.run(
            _playwright_cli_command(npx_path, session, *args),
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            check=True,
        )
        return completed.stdout.strip()

    def pw_eval(script: str) -> dict[str, object]:
        output = pw('eval', script, '--raw')
        return json.loads(output)

    try:
        pw('open', f'http://127.0.0.1:{port}')

        ready = None
        for _ in range(20):
            ready = pw_eval("() => ({ ready: !!document.getElementById('paperReportStrategy') && document.getElementById('paperReportStrategy').options.length >= 3 })")
            if ready.get('ready'):
                break
            time.sleep(0.5)
        assert ready and ready.get('ready') is True

        selected = pw_eval(
            "() => { const node = document.getElementById('paperReportStrategy');"
            "node.value = '7';"
            "node.dispatchEvent(new Event('change', { bubbles: true }));"
            "return { value: node.value, desc: document.getElementById('recentPanelDesc')?.textContent || '' }; }"
        )
        assert selected['value'] == '7'
        assert '策略 7' in str(selected['desc'])

        time.sleep(5)

        after_poll = pw_eval(
            "() => ({ value: document.getElementById('paperReportStrategy')?.value || '',"
            "desc: document.getElementById('recentPanelDesc')?.textContent || '' })"
        )
        assert after_poll['value'] == '7'
        assert '策略 7' in str(after_poll['desc'])
    finally:
        try:
            subprocess.run(
                _playwright_cli_command(npx_path, session, 'close'),
                cwd=str(Path.cwd()),
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                check=False,
            )
        finally:
            runtime.close()


def test_dashboard_report_strategy_selector_reflects_saved_paper_strategy_ids_browser_regression(tmp_path: Path, monkeypatch):
    npx_path = _require_dashboard_browser_regressions()

    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

    monkeypatch.setattr(dashboard, 'PolymarketClient', StubClient)

    strategy_catalog = json.loads(json.dumps(DashboardState.STRATEGY_CATALOG))
    all_strategy_ids = ['1', '2', '3', '4', '5', '6', '7']
    current_env_values = {
        'TRADE_MODE': 'paper',
        'MARKET_TIMEFRAME': '15m',
        'STRATEGY_ID': '1',
        'PAPER_STRATEGY_IDS': '1',
        'PAPER_TIMEFRAMES': '15m',
        'PAPER_15M_STRATEGY_ID': '4',
        'PAPER_15M_STRATEGY_IDS': '4',
    }

    def fake_get_config_payload(self):
        return {
            'env_file': str(tmp_path / '.env.dashboard'),
            'env_values': dict(current_env_values),
            'timeframe_presets': {'5m': {}, '15m': {}},
            'editable_keys': [
                'TRADE_MODE',
                'MARKET_TIMEFRAME',
                'STRATEGY_ID',
                'PAPER_STRATEGY_IDS',
                'PAPER_TIMEFRAMES',
                'PAPER_15M_STRATEGY_ID',
                'PAPER_15M_STRATEGY_IDS',
            ],
            'labels': DashboardState.CONFIG_LABELS,
            'select_options': {
                'TRADE_MODE': ['paper', 'live'],
                'MARKET_TIMEFRAME': ['5m', '15m'],
                'STRATEGY_ID': all_strategy_ids,
                'PAPER_STRATEGY_IDS': all_strategy_ids,
            },
            'strategy_catalog': strategy_catalog,
            'field_groups': [{'title': '基础策略', 'description': '', 'keys': ['STRATEGY_ID', 'PAPER_STRATEGY_IDS']}],
            'field_scope': {},
            'field_help': {},
            'validation_errors': {},
            'runtime_status': {
                'saved_mode': 'paper',
                'running_mode': 'paper',
                'restart_required': False,
                'live_ready': False,
                'live_validation_error': None,
                'active_mode': 'paper',
                'desired_mode': 'paper',
                'switch_state': 'idle',
                'switch_reason': None,
                'current_round_slug': None,
                'round_in_progress': False,
                'safe_to_switch': True,
                'pending_live_order': False,
                'redeem_visible': False,
                'redeem_enabled': False,
                'redeem_auth_mode': 'unconfigured',
                'redeem_pending_count': 0,
                'redeem_last_result': None,
                'redeem_last_attempt_at': None,
                'redeem_last_submission_id': None,
                'redeem_last_submission_status': None,
                'redeem_last_tx_hash': None,
                'optimizer_enabled': False,
                'optimizer_last_run_at': None,
                'optimizer_champion_id': None,
                'optimizer_active_challengers': [],
                'optimizer_promotable_count': 0,
            },
            'saved_at': None,
            'paper_timeframes': ['15m'],
            'paper_profiles': {
                '15m': {
                    'strategy_id': current_env_values['PAPER_15M_STRATEGY_ID'],
                    'paper_strategy_ids': current_env_values['PAPER_15M_STRATEGY_IDS'].split(','),
                    'target_profit': '1.0',
                    'bet_sizing_mode': 'FIXED_BASE_COST',
                    'base_order_cost': '1.0',
                    'max_consecutive_losses': '7',
                    'max_stake': '',
                    'open_delay_seconds': '25',
                    'signal_momentum_threshold': '0.015',
                    'ofi_threshold': '0.65',
                    'binance_signal_stale_seconds': '2.0',
                    'strategy7_ofi_threshold': '0.5',
                    'strategy7_momentum_threshold': '0.005',
                    'strategy7_max_entry_price': '0.55',
                }
            },
        }

    def fake_update_config(self, values):
        current_env_values.update({str(key): str(value) for key, value in values.items()})
        return fake_get_config_payload(self)

    def fake_get_market_payload(self, *, strategy=None, timeframe=None):
        return {
            'ok': True,
            'timestamp': '2026-04-23T03:40:00+00:00',
            'round': None,
            'quote': None,
            'signal': None,
            'plan': None,
            'session_state': {'round_index': 1, 'cash_pnl': 0.0, 'recovery_loss': 0.0, 'consecutive_losses': 0, 'stop_loss_count': 0, 'daily_realized_pnl': 0.0, 'pending_paper_trades': []},
            'ws_runtime': {},
            'ws_stale_guard_triggered': False,
            'message': 'no round',
            'strategy6': {'enabled': False},
            'strategy7': {'enabled': False},
            'strategy_view': {'selected': '4', 'paper_strategy_ids': ['4'], 'available': ['4'], 'timeframe': '15m'},
        }

    def fake_get_paper_summary_payload(self, *, strategy=None, timeframe=None):
        return {'csv_path': 'paper.csv', 'tz_offset': '+08:00', 'strategy': str(strategy or 'all'), 'timeframe': str(timeframe or '15m'), 'days': [], 'latest': None}

    def fake_get_recent_trades_payload(self, *, limit, strategy=None, timeframe=None):
        return {'csv_path': 'paper.csv', 'strategy': str(strategy or 'all'), 'timeframe': str(timeframe or '15m'), 'count': 0, 'rows': []}

    monkeypatch.setattr(DashboardState, 'get_config_payload', fake_get_config_payload)
    monkeypatch.setattr(DashboardState, 'update_config', fake_update_config)
    monkeypatch.setattr(DashboardState, 'get_market_payload', fake_get_market_payload)
    monkeypatch.setattr(DashboardState, 'get_paper_summary_payload', fake_get_paper_summary_payload)
    monkeypatch.setattr(DashboardState, 'get_recent_trades_payload', fake_get_recent_trades_payload)

    runtime = create_dashboard_runtime(host='127.0.0.1', port=0, env_file=tmp_path / '.env.dashboard')
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()

    port = runtime.server.server_address[1]
    session = f"dashboard-report-save-{uuid.uuid4().hex}"

    def pw(*args: str) -> str:
        completed = subprocess.run(
            _playwright_cli_command(npx_path, session, *args),
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            check=True,
        )
        return completed.stdout.strip()

    def pw_eval(script: str) -> dict[str, object]:
        output = pw('eval', script, '--raw')
        return json.loads(output)

    try:
        pw('open', f'http://127.0.0.1:{port}')

        ready = None
        for _ in range(20):
            ready = pw_eval("() => ({ ready: !!document.getElementById('paperReportStrategy') && document.getElementById('paperReportStrategy').options.length >= 2 })")
            if ready.get('ready'):
                break
            time.sleep(0.5)
        assert ready and ready.get('ready') is True

        after_edit_add = pw_eval(
            "() => {"
            "selectAllPaperStrategiesInPanel();"
            "const node = document.getElementById('paperReportStrategy');"
            "return { options: Array.from(node.options).map((option) => option.value) };"
            "}"
        )
        assert after_edit_add['options'] == ['all', '1', '2', '3', '4', '5', '6', '7']

        after_save = pw_eval(
            "async () => {"
            "selectAllPaperStrategiesInPanel();"
            "await saveConfig();"
            "const node = document.getElementById('paperReportStrategy');"
            "return { options: Array.from(node.options).map((option) => option.value), env: state.config.env_values.PAPER_STRATEGY_IDS };"
            "}"
        )

        assert after_save['env'] == '1,2,3,4,5,6,7'
        assert current_env_values['PAPER_15M_STRATEGY_IDS'] == '4'
        assert after_save['options'] == ['all', '1', '2', '3', '4', '5', '6', '7']

        after_edit_remove = pw_eval(
            "() => {"
            "const node = document.getElementById('paperReportStrategy');"
            "node.value = '4';"
            "node.dispatchEvent(new Event('change', { bubbles: true }));"
            "togglePaperStrategySelection('4', false);"
            "const nextNode = document.getElementById('paperReportStrategy');"
            "return {"
            "options: Array.from(nextNode.options).map((option) => option.value),"
            "selected: nextNode.value,"
            "filter: state.paperReportStrategyFilter"
            "};"
            "}"
        )
        assert after_edit_remove['options'] == ['all', '1', '2', '3', '5', '6', '7']
        assert after_edit_remove['selected'] == 'all'
        assert after_edit_remove['filter'] == 'all'

        after_remove = pw_eval(
            "async () => {"
            "const node = document.getElementById('paperReportStrategy');"
            "togglePaperStrategySelection('4', false);"
            "await saveConfig();"
            "const nextNode = document.getElementById('paperReportStrategy');"
            "return {"
            "options: Array.from(nextNode.options).map((option) => option.value),"
            "selected: nextNode.value,"
            "filter: state.paperReportStrategyFilter,"
            "env: state.config.env_values.PAPER_STRATEGY_IDS"
            "};"
            "}"
        )

        assert after_remove['env'] == '1,2,3,5,6,7'
        assert current_env_values['PAPER_15M_STRATEGY_IDS'] == '4'
        assert after_remove['options'] == ['all', '1', '2', '3', '5', '6', '7']
        assert after_remove['selected'] == 'all'
        assert after_remove['filter'] == 'all'
    finally:
        try:
            subprocess.run(
                _playwright_cli_command(npx_path, session, 'close'),
                cwd=str(Path.cwd()),
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                check=False,
            )
        finally:
            runtime.close()


def test_dashboard_report_strategy_switch_ignores_stale_browser_responses(tmp_path: Path, monkeypatch):
    npx_path = _require_dashboard_browser_regressions()

    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

    monkeypatch.setattr(dashboard, 'PolymarketClient', StubClient)

    strategy_catalog = json.loads(json.dumps(DashboardState.STRATEGY_CATALOG))

    def fake_get_config_payload(self):
        return {
            'env_file': str(tmp_path / '.env.dashboard'),
            'env_values': {
                'TRADE_MODE': 'paper',
                'MARKET_TIMEFRAME': '15m',
                'STRATEGY_ID': '1',
                'PAPER_STRATEGY_IDS': '1,7',
                'PAPER_TIMEFRAMES': '15m',
            },
            'timeframe_presets': {'5m': {}, '15m': {}},
            'editable_keys': ['TRADE_MODE', 'MARKET_TIMEFRAME', 'STRATEGY_ID', 'PAPER_STRATEGY_IDS'],
            'labels': DashboardState.CONFIG_LABELS,
            'select_options': {
                'TRADE_MODE': ['paper', 'live'],
                'MARKET_TIMEFRAME': ['5m', '15m'],
                'STRATEGY_ID': ['1', '7'],
                'PAPER_STRATEGY_IDS': ['1', '7'],
            },
            'strategy_catalog': strategy_catalog,
            'field_groups': [{'title': '基础策略', 'description': '', 'keys': ['STRATEGY_ID', 'PAPER_STRATEGY_IDS']}],
            'field_scope': {},
            'field_help': {},
            'validation_errors': {},
            'runtime_status': {
                'saved_mode': 'paper',
                'running_mode': 'paper',
                'restart_required': False,
                'live_ready': False,
                'live_validation_error': None,
                'active_mode': 'paper',
                'desired_mode': 'paper',
                'switch_state': 'idle',
                'switch_reason': None,
                'current_round_slug': None,
                'round_in_progress': False,
                'safe_to_switch': True,
                'pending_live_order': False,
                'redeem_visible': False,
                'redeem_enabled': False,
                'redeem_auth_mode': 'unconfigured',
                'redeem_pending_count': 0,
                'redeem_last_result': None,
                'redeem_last_attempt_at': None,
                'redeem_last_submission_id': None,
                'redeem_last_submission_status': None,
                'redeem_last_tx_hash': None,
                'optimizer_enabled': False,
                'optimizer_last_run_at': None,
                'optimizer_champion_id': None,
                'optimizer_active_challengers': [],
                'optimizer_promotable_count': 0,
            },
            'saved_at': None,
            'paper_timeframes': ['15m'],
            'paper_profiles': {
                '15m': {
                    'strategy_id': '1',
                    'paper_strategy_ids': ['1', '7'],
                    'target_profit': '1.0',
                    'bet_sizing_mode': 'FIXED_BASE_COST',
                    'base_order_cost': '1.0',
                    'max_consecutive_losses': '7',
                    'max_stake': '',
                    'open_delay_seconds': '25',
                    'signal_momentum_threshold': '0.015',
                    'ofi_threshold': '0.65',
                    'binance_signal_stale_seconds': '2.0',
                    'strategy7_ofi_threshold': '0.5',
                    'strategy7_momentum_threshold': '0.005',
                    'strategy7_max_entry_price': '0.55',
                }
            },
        }

    def fake_get_market_payload(self, *, strategy=None, timeframe=None):
        return {
            'ok': True,
            'timestamp': '2026-04-23T03:40:00+00:00',
            'round': {
                'slug': 'btc-updown-15m-current',
                'title': 'BTC 15m Current',
                'start_time': '2026-04-23T03:30:00+00:00',
                'end_time': '2026-04-23T03:45:00+00:00',
                'entry_time': '2026-04-23T03:30:25+00:00',
                'is_current': True,
                'seconds_to_entry': -10,
                'seconds_to_end': 300,
            },
            'quote': {
                'source': 'websocket',
                'accepting_orders': True,
                'up_price': 0.5,
                'up_best_bid': 0.49,
                'up_best_ask': 0.51,
                'down_price': 0.5,
                'down_best_bid': 0.49,
                'down_best_ask': 0.51,
                'fetched_at': '2026-04-23T03:40:00+00:00',
            },
            'signal': {'side': None, 'reason': 'signal_unavailable', 'open_up': None, 'current_up': None, 'threshold': None, 'delta': None, 'locked': False},
            'plan': {'should_trade': False, 'side': None, 'price': None, 'order_size': 0.0, 'order_cost': 0.0, 'expected_profit': 0.0, 'skip_reason': 'signal_unavailable', 'stop_loss_triggered': False},
            'session_state': {'round_index': 1, 'cash_pnl': 0.0, 'recovery_loss': 0.0, 'consecutive_losses': 0, 'stop_loss_count': 0, 'daily_realized_pnl': 0.0, 'pending_paper_trades': []},
            'ws_runtime': {},
            'ws_stale_guard_triggered': False,
            'strategy6': {'enabled': False, 'ofi_score': None, 'signal_at': None, 'stale': False, 'threshold': 0.65, 'max_entry_price': 0.56, 'bid_price': None, 'bid_qty': None, 'ask_price': None, 'ask_qty': None},
            'strategy7': {'enabled': False, 'ofi_score': None, 'momentum_delta': None, 'agreement': None, 'quality_gate': None, 'final_reason': None},
            'strategy_view': {'selected': '1', 'paper_strategy_ids': ['1', '7'], 'available': ['1', '7'], 'timeframe': '15m'},
        }

    def fake_get_paper_summary_payload(self, *, strategy=None, timeframe=None):
        return {'csv_path': 'paper.csv', 'tz_offset': '+08:00', 'strategy': str(strategy or 'all'), 'timeframe': str(timeframe or '15m'), 'days': [], 'latest': None}

    def fake_get_recent_trades_payload(self, *, limit, strategy=None, timeframe=None):
        return {'csv_path': 'paper.csv', 'strategy': str(strategy or 'all'), 'timeframe': str(timeframe or '15m'), 'count': 0, 'rows': []}

    monkeypatch.setattr(DashboardState, 'get_config_payload', fake_get_config_payload)
    monkeypatch.setattr(DashboardState, 'get_market_payload', fake_get_market_payload)
    monkeypatch.setattr(DashboardState, 'get_paper_summary_payload', fake_get_paper_summary_payload)
    monkeypatch.setattr(DashboardState, 'get_recent_trades_payload', fake_get_recent_trades_payload)

    runtime = create_dashboard_runtime(host='127.0.0.1', port=0, env_file=tmp_path / '.env.dashboard')
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()

    port = runtime.server.server_address[1]
    session = f"dashboard-report-stale-{uuid.uuid4().hex}"

    def pw(*args: str) -> str:
        completed = subprocess.run(
            _playwright_cli_command(npx_path, session, *args),
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            check=True,
        )
        return completed.stdout.strip()

    def pw_eval(script: str) -> dict[str, object]:
        output = pw('eval', script, '--raw')
        return json.loads(output)

    try:
        pw('open', f'http://127.0.0.1:{port}')

        ready = None
        for _ in range(20):
            ready = pw_eval("() => ({ ready: !!document.getElementById('paperReportStrategy') && document.getElementById('paperReportStrategy').options.length >= 3 })")
            if ready.get('ready'):
                break
            time.sleep(0.5)
        assert ready and ready.get('ready') is True

        raced = pw_eval(
            "() => (async () => {"
            "const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));"
            "const originalFetch = window.fetch.bind(window);"
            "const summaryPayload = (strategy) => ({"
            "csv_path: 'paper.csv', tz_offset: '+08:00', strategy, timeframe: '15m',"
            "days: [{ date: '2026-04-23', trade_rows: strategy === '7' ? 7 : 1, hit_rate: strategy === '7' ? 1 : 0, total_pnl: strategy === '7' ? 7.7 : 1.1, max_drawdown: 0.1 }],"
            "latest: { date: '2026-04-23', trade_rows: strategy === '7' ? 7 : 1, hit_rate: strategy === '7' ? 1 : 0, total_pnl: strategy === '7' ? 7.7 : 1.1, max_drawdown: 0.1, strong_signal_rate: strategy === '7' ? 1 : 0 }"
            "});"
            "const recentPayload = (strategy) => ({"
            "csv_path: 'paper.csv', strategy, timeframe: '15m', count: 1,"
            "rows: [{"
            "timestamp: '2026-04-23T03:40:00+00:00', event_slug: strategy === '7' ? 'seven-row' : 'all-row', side: 'UP', price: 0.5, order_cost: 1.0,"
            "result: 'UP', result_check_status: 'match', resolved_expected_result: 'UP', resolved_price_to_beat: 100000, resolved_final_price: 100100,"
            "trade_pnl: strategy === '7' ? 7.7 : 1.1, cash_pnl: strategy === '7' ? 7.7 : 1.1, skip_reason: '', signal_delta: strategy === '7' ? 0.7 : 0.1, pending_status: ''"
            "}]"
            "});"
            "window.fetch = async (input, init) => {"
            "const url = String(input);"
            "if (url.includes('/api/paper/summary')) {"
            "const strategy = new URL(url, window.location.origin).searchParams.get('strategy') || 'all';"
            "await wait(strategy === '7' ? 40 : 220);"
            "return { ok: true, json: async () => summaryPayload(strategy) };"
            "}"
            "if (url.includes('/api/paper/recent')) {"
            "const strategy = new URL(url, window.location.origin).searchParams.get('strategy') || 'all';"
            "await wait(strategy === '7' ? 40 : 220);"
            "return { ok: true, json: async () => recentPayload(strategy) };"
            "}"
            "return originalFetch(input, init);"
            "};"
            "try {"
            "const staleSummary = refreshSummary();"
            "const staleRecent = refreshRecent();"
            "const node = document.getElementById('paperReportStrategy');"
            "node.value = '7';"
            "node.dispatchEvent(new Event('change', { bubbles: true }));"
            "await Promise.allSettled([staleSummary, staleRecent]);"
            "await wait(260);"
            "return {"
            "selected: document.getElementById('paperReportStrategy')?.value || '',"
            "totalPnl: document.getElementById('sumTotalPnl')?.textContent || '',"
            "recentRow: document.querySelector('#recentTbody tr td:nth-child(2)')?.textContent || '',"
            "recentDesc: document.getElementById('recentPanelDesc')?.textContent || '',"
            "status: document.getElementById('paperStatus')?.textContent || ''"
            "};"
            "} finally {"
            "window.fetch = originalFetch;"
            "}"
            "})()"
        )

        assert raced['selected'] == '7'
        assert raced['totalPnl'] == '+7.7000'
        assert raced['recentRow'] == 'seven-row'
        assert '策略 7' in str(raced['recentDesc'])
    finally:
        try:
            subprocess.run(
                _playwright_cli_command(npx_path, session, 'close'),
                cwd=str(Path.cwd()),
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                check=False,
            )
        finally:
            runtime.close()


def test_dashboard_market_payload_can_switch_timeframe_view(tmp_path: Path, monkeypatch):
    class StubClient:
        def __init__(self, cfg):
            self.config = cfg

        def close(self) -> None:
            return

        def find_current_and_next_rounds(self, *, now):
            timeframe = self.config.market_timeframe
            window = MarketWindow(
                event_id=f'evt-{timeframe}',
                market_id=f'mkt-{timeframe}',
                slug=f'btc-updown-{timeframe}-current',
                title=f'BTC {timeframe} Current',
                start_time=now - timedelta(minutes=1),
                end_time=now + timedelta(minutes=4),
                up_token_id='up-token',
                down_token_id='down-token',
            )
            return window, None

        def get_market_by_slug(self, slug: str):
            return {'slug': slug}

        def quote_from_market(self, market):
            return MarketQuote(
                slug=str(market.get('slug', '')),
                up_price=0.55,
                down_price=0.45,
                up_best_ask=0.56,
                fetched_at=datetime.now(timezone.utc),
            )

        def get_ws_runtime_stats(self):
            return {}

    monkeypatch.setattr(dashboard, 'PolymarketClient', StubClient)

    env_file = tmp_path / '.env.dashboard'
    env_file.write_text(
        '\n'.join(
            [
                'TRADE_MODE=paper',
                'MARKET_TIMEFRAME=5m',
                'PAPER_TIMEFRAMES=5m,15m',
                'PAPER_5M_STRATEGY_ID=5',
                'PAPER_5M_STRATEGY_IDS=5,6',
                'PAPER_15M_STRATEGY_ID=2',
                'PAPER_15M_STRATEGY_IDS=1,2',
            ]
        ) + '\n',
        encoding='utf-8',
    )
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    state = DashboardState(env_file=env_file)
    try:
        payload = state.get_market_payload(timeframe='15m')

        assert payload['round']['slug'] == 'btc-updown-15m-current'
        assert payload['strategy_view']['selected'] == '2'
        assert payload['strategy_view']['paper_strategy_ids'] == ['1', '2']
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_starts_binance_signal_service_when_paper_strategies_include_6(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text('STRATEGY_ID=1\nPAPER_STRATEGY_IDS=1,6\n', encoding='utf-8')
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    state = DashboardState(env_file=env_file)
    try:
        assert state._binance_signal_service is not None
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_starts_binance_signal_service_when_live_strategies_include_7(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text(
        'TRADE_MODE=live\nLIVE_TRADING_ENABLED=true\nSTRATEGY_ID=1\nPAPER_STRATEGY_IDS=1\nLIVE_STRATEGY_IDS=7\n',
        encoding='utf-8',
    )
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    state = DashboardState(env_file=env_file)
    try:
        assert state._binance_signal_service is not None
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_paper_payloads_switch_per_strategy(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text('STRATEGY_ID=2\nPAPER_STRATEGY_IDS=1,2,3,4,5,6\n', encoding='utf-8')
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    state = DashboardState(env_file=env_file)
    try:
        logs_dir = tmp_path / 'logs'
        logs_dir.mkdir(parents=True, exist_ok=True)
        csv_lines = [
            'timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason'
        ]
        for strategy_id in range(1, 7):
            csv_lines.append(
                f'2026-04-06T08:0{strategy_id}:00+00:00,paper,{strategy_id},{strategy_id},OPEN,settled-{strategy_id},2026-04-06T08:00:00+00:00,2026-04-06T08:05:00+00:00,UP,0.50,2.0,1.0,1.0,UP,{float(strategy_id):.1f},{float(strategy_id):.1f},0.0,0,False,,,,,,False,'
            )
        (logs_dir / 'paper_trades.csv').write_text('\n'.join(csv_lines) + '\n', encoding='utf-8')

        paper_strategies: dict[str, dict[str, object]] = {}
        for strategy_id in range(1, 7):
            paper_strategies[str(strategy_id)] = {
                'round_index': strategy_id + 10,
                'cash_pnl': float(strategy_id),
                'recovery_loss': 0.0,
                'consecutive_losses': 0,
                'consecutive_max_stake_skips': 0,
                'signal_round_slug': None,
                'signal_round_open_up_price': None,
                'signal_round_locked_side': None,
                'strategy6_last_ofi_score': 0.8 if strategy_id == 6 else None,
                'stop_loss_count': 0,
                'daily_realized_pnl': float(strategy_id),
                'current_day': '2026-04-06',
                'pending_paper_trades': [
                    {
                        'round_index': strategy_id + 20,
                        'event_slug': f'pending-{strategy_id}',
                        'start_time': '2026-04-06T08:10:00+00:00',
                        'end_time': '2026-04-06T08:15:00+00:00',
                        'side': 'UP',
                        'price': 0.45,
                        'order_size': 2.0,
                        'order_cost': 0.9,
                        'expected_profit': 1.1,
                        'strategy': strategy_id,
                        'entry_timing': 'OPEN',
                        'signal_open_up_price': None,
                        'signal_current_up_price': None,
                        'signal_threshold': 0.2 if strategy_id in {5, 6} else None,
                        'signal_delta': 0.4 if strategy_id in {5, 6} else None,
                        'signal_locked': False,
                        'signal_reason': None,
                        'queued_at': f'2026-04-06T08:2{strategy_id}:05+00:00',
                    }
                ],
            }
        (logs_dir / 'session_state.json').write_text(
            json.dumps({'paper_strategies': paper_strategies}),
            encoding='utf-8',
        )

        summary_all = state.get_paper_summary_payload()

        assert summary_all['strategy'] == 'all'
        assert summary_all['latest']['trade_rows'] == 6

        for strategy_id in range(1, 7):
            summary = state.get_paper_summary_payload(strategy=strategy_id)
            recent = state.get_recent_trades_payload(limit=10, strategy=strategy_id)

            assert summary['strategy'] == str(strategy_id)
            assert summary['latest']['trade_rows'] == 1
            assert summary['latest']['total_pnl'] == float(strategy_id)
            assert recent['strategy'] == str(strategy_id)
            assert recent['count'] == 2
            assert recent['rows'][0]['event_slug'] == f'pending-{strategy_id}'
            assert all(row['strategy'] == str(strategy_id) for row in recent['rows'])
    finally:
        state.close()
        os.chdir(old_cwd)


def test_dashboard_summary_payload_includes_daily_strategy_performance(tmp_path: Path):
    env_file = tmp_path / '.env.dashboard'
    env_file.write_text('STRATEGY_ID=3\nPAPER_STRATEGY_IDS=3,7\n', encoding='utf-8')
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    state = DashboardState(env_file=env_file)
    try:
        logs_dir = tmp_path / 'logs'
        logs_dir.mkdir(parents=True, exist_ok=True)
        csv_lines = [
            'timestamp,mode,round_index,strategy,entry_timing,event_slug,start_time,end_time,side,price,order_size,order_cost,expected_profit,result,trade_pnl,cash_pnl,recovery_loss,consecutive_losses,stop_loss_triggered,skip_reason,signal_open_up_price,signal_current_up_price,signal_threshold,signal_delta,signal_locked,signal_reason',
            '2026-04-28T01:00:00+00:00,paper,1,3,OPEN,s3-win,2026-04-28T00:55:00+00:00,2026-04-28T01:00:00+00:00,UP,0.50,4.0,2.0,2.0,UP,2.0,2.0,0.0,0,False,,,,,,False,',
            '2026-04-28T01:05:00+00:00,paper,1,7,OPEN,s7-loss,2026-04-28T01:00:00+00:00,2026-04-28T01:05:00+00:00,DOWN,0.50,2.0,1.0,1.0,UP,-1.0,-1.0,1.0,1,False,,,,,,False,',
            '2026-04-28T01:10:00+00:00,paper,2,7,OPEN,s7-skip,2026-04-28T01:05:00+00:00,2026-04-28T01:10:00+00:00,SKIP,,0.0,0.0,0.0,,0.0,-1.0,1.0,1,False,strategy7_ofi_unavailable,,,,,False,strategy7_ofi_unavailable',
            '2026-04-29T01:00:00+00:00,paper,2,3,OPEN,s3-loss,2026-04-29T00:55:00+00:00,2026-04-29T01:00:00+00:00,DOWN,0.50,1.0,0.5,1.0,UP,-0.5,1.5,0.5,1,False,,,,,,False,',
            '2026-04-29T01:05:00+00:00,paper,3,7,OPEN,s7-win,2026-04-29T01:00:00+00:00,2026-04-29T01:05:00+00:00,UP,0.50,6.0,3.0,3.0,UP,3.0,2.0,0.0,0,False,,,,,,False,',
        ]
        (logs_dir / 'paper_trades.csv').write_text('\n'.join(csv_lines) + '\n', encoding='utf-8')

        payload = state.get_paper_summary_payload()

        assert 'strategy_days' in payload
        assert [
            (row['date'], row['strategy'], row['trade_rows'], row['skip_rows'], row['total_pnl'], row['cumulative_pnl'])
            for row in payload['strategy_days']
        ] == [
            ('2026-04-28', '3', 1, 0, 2.0, 2.0),
            ('2026-04-28', '7', 1, 1, -1.0, -1.0),
            ('2026-04-29', '3', 1, 0, -0.5, 1.5),
            ('2026-04-29', '7', 1, 0, 3.0, 2.0),
        ]

        strategy_seven = state.get_paper_summary_payload(strategy=7)

        assert [
            (row['date'], row['strategy'], row['trade_rows'], row['skip_rows'])
            for row in strategy_seven['strategy_days']
        ] == [
            ('2026-04-28', '7', 1, 1),
            ('2026-04-29', '7', 1, 0),
        ]
    finally:
        state.close()
        os.chdir(old_cwd)
