from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import dashboard
from dashboard import (
    ConfigValidationError,
    DashboardState,
    _dashboard_html,
    _dashboard_js,
    create_dashboard_runtime,
)
from models import MarketQuote, MarketWindow
from runtime_control import RuntimeControl


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

    assert "\u5148\u770b\u54ea\u91cc" in js
    assert "\u600e\u4e48\u5b89\u5168\u6539\u53c2\u6570" in js
    assert "\u600e\u4e48\u5224\u65ad\u5f53\u524d\u80fd\u4e0d\u80fd\u8dd1" in js
    assert "\u51fa\u95ee\u9898\u5148\u770b\u54ea\u91cc" in js
    assert "\u9875\u9762\u5143\u7d20\u8bf4\u660e" in js


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

    assert "\u5f53\u524d\u72b6\u6001\u6682\u672a\u8bc6\u522b" in js
    assert "\u8bf7\u5237\u65b0\u9875\u9762" in js
    assert "\u8054\u7cfb\u7ef4\u62a4\u8005" in js



def test_dashboard_assets_mark_pending_recent_trades_clearly():
    js = _dashboard_js()

    assert "const isPending = row.pending_status === 'pending_settlement';" in js
    assert "const resultText = isPending ? '待结算' : (row.result || '--');" in js
    assert "const rowClass = isPending ? 'recent-pending' : '';" in js
    assert "setChip('recentStatus', pendingCount > 0 ? (rows.length + ' 行 · ' + pendingCount + ' 待结算') : (rows.length + ' 行'), pendingCount > 0 ? 'warn' : 'ok');" in js

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
        assert payload['env_values']['TRADE_MODE'] == 'live'
        assert payload['env_values']['LIVE_TRADING_ENABLED'] == 'true'
        assert payload['env_values']['POLYMARKET_PRIVATE_KEY'] != 'super-secret-private-key'
        assert payload['env_values']['POLYMARKET_PRIVATE_KEY']
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
        assert payload['env_values']['POLYMARKET_API_KEY'] == 'builder-key'
        assert payload['env_values']['POLYMARKET_API_SECRET'] != 'builder-secret'
        assert payload['env_values']['POLYMARKET_API_PASSPHRASE'] != 'builder-passphrase'
        assert payload['env_values']['POLYMARKET_API_SECRET']
        assert payload['env_values']['POLYMARKET_API_PASSPHRASE']
    finally:
        state.close()

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

    assert 'id="runtimeModeCard"' in html
    assert 'id="runtimeSavedMode"' in html
    assert 'id="runtimeRunningMode"' in html
    assert 'id="runtimeRestartRequired"' in html
    assert 'id="runtimeLiveReady"' in html
    assert 'id="runtimeLiveError"' in html
    assert 'function renderRuntimeStatus(' in js
    assert 'payload.runtime_status || {}' in js



def test_dashboard_assets_switch_recent_endpoint_by_running_mode():
    js = _dashboard_js()

    assert "const runningMode = String((((state.config || {}).runtime_status || {}).active_mode || (((state.config || {}).runtime_status || {}).running_mode) || 'paper')).toLowerCase();" in js
    assert "const strategy = encodeURIComponent(String(state.paperStrategyFilter || 'all'));" in js
    assert "const recentEndpoint = runningMode === 'live' ? '/api/live/recent?limit=80' : '/api/paper/recent?limit=80&strategy=' + strategy;" in js



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

def test_dashboard_assets_format_recent_trade_round_slug_as_datetime():
    js = _dashboard_js()

    assert 'function formatRoundSlug(' in js
    assert "const match = raw.match(/-(\\d{10})(?:$|\\D)/);" in js
    assert "return dt.toLocaleString('zh-CN', { hour12: false });" in js
    assert "'<td title=\"' + esc(row.event_slug || '--') + '\">' + esc(formatRoundSlug(row.event_slug)) + '</td>' +" in js

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


def test_dashboard_assets_include_recent_result_validation_column():
    html = _dashboard_html()
    js = _dashboard_js()

    assert '<th>校验</th>' in html
    assert 'function resultCheckText(' in js
    assert "row.result_check_status" in js
    assert "resolved_price_to_beat" in js
    assert "resolved_final_price" in js


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
        assert payload['select_options']['PAPER_STRATEGY_IDS'] == ['1', '2', '3', '4', '5', '6']
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
