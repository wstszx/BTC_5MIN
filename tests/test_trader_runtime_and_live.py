from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest
import requests

from binance_signal import BinanceDepth5SignalService
from config import AppConfig
from models import MarketQuote, MarketWindow, PaperStrategyState, SessionState, TradePlan, TradeRecord
from runtime_control import RuntimeControl
from trader import (
    SideDecision,
    _list_redeemable_live_positions,
    load_live_redeem_state,
    save_live_redeem_state,
    execute_live_redeem,
    attempt_live_redeem,
    run_live_redeem_worker,
    _resolve_side_from_strategy,
    _update_max_stake_skip_streak,
    append_trade_log,
    load_session_state,
    place_live_order,
    run_live_trading,
    run_paper_trading,
    _create_live_clob_client,
    _candidate_cfg_with_params,
    validate_live_runtime_config,
    _paper_experiment_id,
)


class _TransientPaperClient:
    def __init__(self):
        self.calls = 0

    def find_current_and_next_rounds(self, *, now):
        self.calls += 1
        if self.calls == 1:
            raise requests.exceptions.SSLError("temporary ssl failure")
        raise KeyboardInterrupt


class _NoMarketClient:
    def find_current_and_next_rounds(self, *, now):
        return None, None


class _LiveMarketClient:
    def find_current_and_next_rounds(self, *, now):
        window = MarketWindow(
            event_id="evt-1",
            market_id="mkt-1",
            slug="btc-updown-5m-test",
            title="BTC 5m Test",
            start_time=now - timedelta(minutes=1),
            end_time=now + timedelta(minutes=4),
            up_token_id="up-token",
            down_token_id="down-token",
        )
        return window, None

    def get_market_by_slug(self, slug: str):
        return {
            "slug": slug,
            "outcomes": '["Up", "Down"]',
            "outcomePrices": '["0.55", "0.45"]',
            "clobTokenIds": '["up-token", "down-token"]',
            "bestBid": "0.54",
            "bestAsk": "0.56",
            "acceptingOrders": True,
        }

    def quote_from_market(self, _market):
        return MarketQuote(
            slug="btc-updown-5m-test",
            up_price=0.55,
            down_price=0.45,
            up_best_ask=0.56,
            fetched_at=datetime.now(timezone.utc),
        )


class _NoTradeLiveMarketClient(_LiveMarketClient):
    def find_current_and_next_rounds(self, *, now):
        return None, None


def test_create_live_clob_client_prefers_explicit_api_credentials(monkeypatch):
    captured: dict[str, object] = {}

    from py_clob_client.clob_types import ApiCreds

    class _FakeClobClient:
        def __init__(self, host, chain_id, key, signature_type, funder):
            captured['host'] = host
            captured['chain_id'] = chain_id
            captured['key'] = key
            captured['signature_type'] = signature_type
            captured['funder'] = funder

        def create_or_derive_api_creds(self):
            captured['derived'] = True
            return {'api_key': 'derived'}

        def set_api_creds(self, creds):
            captured['creds'] = creds

    import sys
    import types

    fake_module = types.ModuleType('py_clob_client.client')
    fake_module.ClobClient = _FakeClobClient
    monkeypatch.setitem(sys.modules, 'py_clob_client.client', fake_module)

    client = _create_live_clob_client(
        AppConfig(
            trade_mode='live',
            live_trading_enabled=True,
            live_private_key='pk-live',
            live_funder='0xfunder',
            live_api_key='builder-key',
            live_api_secret='builder-secret',
            live_api_passphrase='builder-passphrase',
        )
    )

    assert client is not None
    assert captured['key'] == 'pk-live'
    assert captured['creds'] == ApiCreds(
        api_key='builder-key',
        api_secret='builder-secret',
        api_passphrase='builder-passphrase',
    )
    assert 'derived' not in captured


def test_create_live_clob_client_derives_api_credentials_when_explicit_values_missing(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeClobClient:
        def __init__(self, host, chain_id, key, signature_type, funder):
            captured['key'] = key
            captured['funder'] = funder

        def create_or_derive_api_creds(self):
            captured['derived'] = True
            return {'api_key': 'derived-key'}

        def set_api_creds(self, creds):
            captured['creds'] = creds

    import sys
    import types

    fake_module = types.ModuleType('py_clob_client.client')
    fake_module.ClobClient = _FakeClobClient
    monkeypatch.setitem(sys.modules, 'py_clob_client.client', fake_module)

    client = _create_live_clob_client(
        AppConfig(
            trade_mode='live',
            live_trading_enabled=True,
            live_private_key='pk-live',
            live_funder='0xfunder',
        )
    )

    assert client is not None
    assert captured['derived'] is True
    assert captured['creds'] == {'api_key': 'derived-key'}


def test_live_redeem_auth_mode_prefers_builder_then_relayer():
    builder_cfg = AppConfig(
        live_redeem_builder_api_key='builder-key',
        live_redeem_builder_secret='builder-secret',
        live_redeem_builder_passphrase='builder-passphrase',
    )
    relayer_cfg = AppConfig(
        live_redeem_relayer_api_key='relayer-key',
        live_redeem_relayer_api_key_address='0xrelayer',
    )
    empty_cfg = AppConfig()

    assert builder_cfg.live_redeem_auth_mode == 'builder'
    assert relayer_cfg.live_redeem_auth_mode == 'relayer'
    assert empty_cfg.live_redeem_auth_mode == 'unconfigured'


def test_create_live_clob_client_falls_back_when_explicit_api_credentials_are_invalid(monkeypatch):
    captured: dict[str, object] = {"creds": []}

    from py_clob_client.clob_types import ApiCreds

    class _FakeClobClient:
        def __init__(self, host, chain_id, key, signature_type, funder):
            captured['key'] = key
            captured['funder'] = funder

        def set_api_creds(self, creds):
            captured['creds'].append(creds)

        def get_api_keys(self):
            if len(captured['creds']) == 1:
                raise Exception("PolyApiException[status_code=401, error_message={'error': 'Unauthorized/Invalid api key'}]")
            captured['validated_after_fallback'] = True
            return {'apiKeys': ['derived-key']}

        def create_or_derive_api_creds(self):
            captured['derived'] = True
            return {'api_key': 'derived-key'}

    import sys
    import types

    fake_module = types.ModuleType('py_clob_client.client')
    fake_module.ClobClient = _FakeClobClient
    monkeypatch.setitem(sys.modules, 'py_clob_client.client', fake_module)

    client = _create_live_clob_client(
        AppConfig(
            trade_mode='live',
            live_trading_enabled=True,
            live_private_key='pk-live',
            live_funder='0xfunder',
            live_api_key='builder-key',
            live_api_secret='builder-secret',
            live_api_passphrase='builder-passphrase',
        )
    )

    assert client is not None
    assert captured['derived'] is True
    assert captured['validated_after_fallback'] is True
    assert captured['creds'] == [
        ApiCreds(
            api_key='builder-key',
            api_secret='builder-secret',
            api_passphrase='builder-passphrase',
        ),
        {'api_key': 'derived-key'},
    ]

def test_redeemable_positions_filters_live_user_and_required_fields():
    class _RedeemablePositionsClient:
        def __init__(self):
            self.users: list[str] = []

        def get_current_positions(self, *, user: str, redeemable: bool | None = None):
            self.users.append(user)
            assert redeemable is True
            return [
                {
                    'proxyWallet': '0xfunder',
                    'conditionId': 'cond-yes',
                    'eventSlug': 'btc-updown-5m-1',
                    'outcome': 'Yes',
                    'size': '12.5',
                    'redeemable': True,
                },
                {
                    'proxyWallet': '0xfunder',
                    'conditionId': 'cond-zero',
                    'eventSlug': 'btc-updown-5m-2',
                    'outcome': 'No',
                    'size': '0',
                    'redeemable': True,
                },
                {
                    'proxyWallet': '0xfunder',
                    'conditionId': None,
                    'eventSlug': 'btc-updown-5m-3',
                    'outcome': 'Yes',
                    'size': '3',
                    'redeemable': True,
                },
                {
                    'proxyWallet': '0xother',
                    'conditionId': 'cond-other',
                    'eventSlug': 'btc-updown-5m-4',
                    'outcome': 'No',
                    'size': '4',
                    'redeemable': True,
                },
                {
                    'proxyWallet': '0xfunder',
                    'conditionId': 'cond-false',
                    'eventSlug': 'btc-updown-5m-5',
                    'outcome': 'Yes',
                    'size': '4',
                    'redeemable': False,
                },
            ]

    cfg = AppConfig(trade_mode='live', live_trading_enabled=True, live_private_key='pk', live_funder='0xFunder')

    positions = _list_redeemable_live_positions(cfg, client=_RedeemablePositionsClient())

    assert positions == [
        {
            'condition_id': 'cond-yes',
            'event_slug': 'btc-updown-5m-1',
            'outcome': 'Yes',
            'size': 12.5,
            'redeemable': True,
            'user': '0xfunder',
        }
    ]


def test_redeem_state_defaults_when_file_missing(tmp_path):
    state = load_live_redeem_state(tmp_path / "live_redeem_state.json")

    assert state == {
        "conditions": {},
        "runtime": {
            "enabled": False,
            "last_poll_at": None,
            "last_attempt_at": None,
            "last_result": None,
            "last_tx_hash": None,
            "last_submission_id": None,
            "last_submission_status": None,
            "pending_redeem_count": 0,
        },
    }


def test_redeem_state_roundtrip_preserves_retry_fields(tmp_path):
    state_path = tmp_path / "live_redeem_state.json"
    original = {
        "conditions": {
            "cond-1": {
                "status": "retry_wait",
                "attempt_count": 3,
                "last_attempt_at": "2026-04-12T08:00:00+00:00",
                "next_attempt_at": "2026-04-12T08:05:00+00:00",
                "last_tx_hash": "0xabc",
                "last_submission_id": None,
                "last_submission_status": None,
                "event_slug": "btc-updown-5m-1",
                "outcome": "Yes",
                "size": 12.5,
                "redeemable": True,
                "user": "0xfunder",
                "last_error": "rpc timeout",
                "completed_at": None,
            }
        },
        "runtime": {
            "enabled": True,
            "last_poll_at": "2026-04-12T08:00:30+00:00",
            "last_attempt_at": "2026-04-12T08:00:00+00:00",
            "last_result": "retry_wait",
            "last_tx_hash": "0xabc",
            "last_submission_id": None,
            "last_submission_status": None,
            "pending_redeem_count": 1,
        },
    }

    save_live_redeem_state(state_path, original)
    loaded = load_live_redeem_state(state_path)

    assert loaded == original


def test_redeem_state_roundtrip_preserves_submission_metadata(tmp_path):
    state_path = tmp_path / "live_redeem_state.json"
    original = {
        "conditions": {
            "cond-1": {
                "status": "submitted",
                "attempt_count": 1,
                "last_submission_id": "sub-1",
                "last_submission_status": "pending",
                "last_tx_hash": None,
                "redeemable": True,
            }
        },
        "runtime": {
            "enabled": True,
            "last_result": "submitted",
            "last_submission_id": "sub-1",
            "last_submission_status": "pending",
            "pending_redeem_count": 1,
        },
    }

    save_live_redeem_state(state_path, original)
    loaded = load_live_redeem_state(state_path)

    assert loaded["conditions"]["cond-1"]["last_submission_id"] == "sub-1"
    assert loaded["conditions"]["cond-1"]["last_submission_status"] == "pending"
    assert loaded["runtime"]["last_submission_id"] == "sub-1"
    assert loaded["runtime"]["last_submission_status"] == "pending"


def test_validate_live_runtime_config_requires_private_key_and_funder():
    cfg = AppConfig(trade_mode='live', live_trading_enabled=True)

    with pytest.raises(RuntimeError, match='private key'):
        validate_live_runtime_config(cfg)


def test_validate_live_runtime_config_requires_redeem_auth_when_auto_redeem_enabled():
    cfg = AppConfig(
        trade_mode='live',
        live_trading_enabled=True,
        live_private_key='pk',
        live_funder='0xfunder',
        live_auto_redeem_enabled=True,
    )

    with pytest.raises(RuntimeError, match='redeem'):
        validate_live_runtime_config(cfg)


def test_redeem_executor_uses_binary_index_sets_and_supports_dry_run():
    calls: list[dict[str, object]] = []

    def fake_executor(*, cfg, condition_id, event_slug, index_sets, dry_run):
        calls.append({
            "auth_mode": cfg.live_redeem_auth_mode,
            "condition_id": condition_id,
            "event_slug": event_slug,
            "index_sets": list(index_sets),
            "dry_run": dry_run,
        })
        return "0xtxhash"

    cfg = AppConfig(trade_mode="live", live_trading_enabled=True, live_private_key="pk", live_funder="0xfunder")

    dry_run_result = execute_live_redeem(
        cfg,
        condition_id="cond-1",
        event_slug="btc-updown-5m-1",
        dry_run=True,
        executor=fake_executor,
    )
    live_result = execute_live_redeem(
        cfg,
        condition_id="cond-1",
        event_slug="btc-updown-5m-1",
        executor=fake_executor,
    )

    assert dry_run_result == "dry-run:cond-1"
    assert live_result == "0xtxhash"
    assert calls == [
        {
            "auth_mode": "unconfigured",
            "condition_id": "cond-1",
            "event_slug": "btc-updown-5m-1",
            "index_sets": [1, 2],
            "dry_run": False,
        }
    ]

def test_execute_live_redeem_uses_builder_credentials_when_available(monkeypatch):
    captured: dict[str, object] = {}

    def fake_relayer_execute(cfg, *, condition_id, event_slug, index_sets):
        captured['auth_mode'] = cfg.live_redeem_auth_mode
        captured['condition_id'] = condition_id
        captured['event_slug'] = event_slug
        captured['index_sets'] = list(index_sets)
        return {'submission_id': 'sub-1', 'tx_hash': None}

    import trader as trader_module
    monkeypatch.setattr(trader_module, '_execute_live_redeem_via_relayer', fake_relayer_execute)

    cfg = AppConfig(
        live_redeem_builder_api_key='builder-key',
        live_redeem_builder_secret='builder-secret',
        live_redeem_builder_passphrase='builder-passphrase',
    )

    result = execute_live_redeem(
        cfg,
        condition_id='0x' + ('34' * 32),
        event_slug='btc-updown-5m-live',
    )

    assert result == 'sub-1'
    assert captured == {
        'auth_mode': 'builder',
        'condition_id': '0x' + ('34' * 32),
        'event_slug': 'btc-updown-5m-live',
        'index_sets': [1, 2],
    }


def test_execute_live_redeem_uses_relayer_key_when_builder_missing(monkeypatch):
    captured: dict[str, object] = {}

    def fake_relayer_execute(cfg, *, condition_id, event_slug, index_sets):
        captured['auth_mode'] = cfg.live_redeem_auth_mode
        return {'submission_id': 'sub-relayer', 'tx_hash': '0xabc'}

    import trader as trader_module
    monkeypatch.setattr(trader_module, '_execute_live_redeem_via_relayer', fake_relayer_execute)

    cfg = AppConfig(
        live_redeem_relayer_api_key='relayer-key',
        live_redeem_relayer_api_key_address='0xrelayer',
    )

    result = execute_live_redeem(
        cfg,
        condition_id='0x' + ('56' * 32),
        event_slug='btc-updown-5m-live',
    )

    assert result == 'sub-relayer'
    assert captured['auth_mode'] == 'relayer'

def test_redeem_retry_marks_terminal_errors_without_reschedule():
    cfg = AppConfig(
        trade_mode="live",
        live_trading_enabled=True,
        live_private_key="pk",
        live_funder="0xfunder",
        live_auto_redeem_enabled=True,
    )
    state = load_live_redeem_state(Path("missing.json"))
    position = {
        "condition_id": "cond-terminal",
        "event_slug": "btc-updown-5m-terminal",
        "outcome": "Yes",
        "size": 2.0,
        "redeemable": True,
        "user": "0xfunder",
    }

    def terminal_executor(**kwargs):
        raise RuntimeError("already redeemed")

    result = attempt_live_redeem(
        cfg,
        state,
        position,
        now=datetime(2026, 4, 12, 8, 0, tzinfo=timezone.utc),
        executor=terminal_executor,
    )

    entry = state["conditions"]["cond-terminal"]
    assert result["status"] == "terminal_error"
    assert entry["status"] == "terminal_error"
    assert entry["attempt_count"] == 1
    assert entry["next_attempt_at"] is None


def test_redeem_retry_schedules_backoff_for_transient_failures():
    cfg = AppConfig(
        trade_mode="live",
        live_trading_enabled=True,
        live_private_key="pk",
        live_funder="0xfunder",
        live_auto_redeem_enabled=True,
        live_auto_redeem_initial_backoff_seconds=30,
        live_auto_redeem_max_backoff_seconds=300,
    )
    state = load_live_redeem_state(Path("missing.json"))
    position = {
        "condition_id": "cond-retry",
        "event_slug": "btc-updown-5m-retry",
        "outcome": "No",
        "size": 3.0,
        "redeemable": True,
        "user": "0xfunder",
    }
    now = datetime(2026, 4, 12, 8, 0, tzinfo=timezone.utc)

    def transient_executor(**kwargs):
        raise RuntimeError("rpc timeout")

    first = attempt_live_redeem(cfg, state, position, now=now, executor=transient_executor)
    second = attempt_live_redeem(
        cfg,
        state,
        position,
        now=datetime(2026, 4, 12, 8, 0, 31, tzinfo=timezone.utc),
        executor=transient_executor,
    )

    entry = state["conditions"]["cond-retry"]
    assert first["status"] == "retry_wait"
    assert second["status"] == "retry_wait"
    assert entry["attempt_count"] == 2
    assert entry["next_attempt_at"] == "2026-04-12T08:01:31+00:00"


def test_live_redeem_worker_skips_when_disabled_and_processes_due_positions(tmp_path, monkeypatch):
    disabled_stop = threading.Event()
    disabled_calls = {"positions": 0}

    class DisabledClient:
        def get_current_positions(self, *, user: str, redeemable: bool | None = None):
            disabled_calls["positions"] += 1
            return []

    def stop_immediately(_seconds):
        disabled_stop.set()

    monkeypatch.setattr("trader.time.sleep", stop_immediately)

    disabled_result = run_live_redeem_worker(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            live_auto_redeem_enabled=False,
            live_auto_redeem_poll_seconds=1,
        ),
        market_client=DisabledClient(),
        state_path=tmp_path / "disabled_redeem_state.json",
        stop_event=disabled_stop,
    )

    assert disabled_result["status"] == "stopped"
    assert disabled_calls["positions"] == 0

    active_stop = threading.Event()
    submitted: list[dict[str, object]] = []

    class ActiveClient:
        def get_current_positions(self, *, user: str, redeemable: bool | None = None):
            return [
                {
                    "proxyWallet": "0xfunder",
                    "conditionId": "cond-live",
                    "eventSlug": "btc-updown-5m-live",
                    "outcome": "Yes",
                    "size": "5",
                    "redeemable": True,
                }
            ]

    def fake_executor(**kwargs):
        submitted.append(dict(kwargs))
        return "0xlive"

    def stop_after_first_sleep(_seconds):
        active_stop.set()

    monkeypatch.setattr("trader.time.sleep", stop_after_first_sleep)
    state_path = tmp_path / "active_redeem_state.json"
    active_result = run_live_redeem_worker(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            live_auto_redeem_enabled=True,
            live_auto_redeem_poll_seconds=1,
            live_redeem_relayer_api_key="relayer-key",
            live_redeem_relayer_api_key_address="0xrelayer",
        ),
        market_client=ActiveClient(),
        state_path=state_path,
        stop_event=active_stop,
        executor=fake_executor,
    )

    saved = load_live_redeem_state(state_path)
    assert active_result["status"] == "stopped"
    assert len(submitted) == 1
    assert submitted[0]["condition_id"] == "cond-live"
    assert submitted[0]["index_sets"] == [1, 2]
    assert saved["conditions"]["cond-live"]["status"] == "submitted"
    assert saved["runtime"]["pending_redeem_count"] == 1


def test_run_live_trading_stops_when_stop_event_is_set(tmp_path, monkeypatch):
    stop_event = threading.Event()

    def fake_sleep(_seconds):
        stop_event.set()

    monkeypatch.setattr('trader.time.sleep', fake_sleep)

    result = run_live_trading(
        AppConfig(
            trade_mode='live',
            live_trading_enabled=True,
            live_private_key='pk',
            live_funder='0xfunder',
            poll_interval_seconds=1,
        ),
        market_client=_NoTradeLiveMarketClient(),
        clob_client=_StubClobClient(),
        state_path=tmp_path / 'live_state.json',
        log_path=tmp_path / 'live_orders.csv',
        stop_event=stop_event,
    )

    assert result['status'] == 'stopped'


def test_run_live_trading_reports_pending_live_order_blocks_switch(tmp_path):
    control = RuntimeControl(initial_mode='live')
    state_path = tmp_path / 'live_state.json'
    state_path.write_text(
        json.dumps(
            {
                'round_index': 0,
                'cash_pnl': 0.0,
                'recovery_loss': 0.0,
                'consecutive_losses': 0,
                'consecutive_max_stake_skips': 0,
                'signal_round_slug': None,
                'signal_round_open_up_price': None,
                'signal_round_locked_side': None,
                'stop_loss_count': 0,
                'daily_realized_pnl': 0.0,
                'current_day': '2026-04-01',
                'pending_live_slug': 'btc-updown-5m-prev',
                'pending_live_side': 'UP',
                'pending_live_price': 0.5,
                'pending_live_order_size': 2.0,
                'pending_live_order_cost': 1.0,
                'pending_live_expected_profit': 1.0,
                'pending_live_end_time': '2099-01-01T00:00:00+00:00',
                'pending_live_order_id': 'oid-prev',
            }
        ),
        encoding='utf-8',
    )

    result = run_live_trading(
        AppConfig(
            trade_mode='live',
            live_trading_enabled=True,
            live_private_key='pk',
            live_funder='0xfunder',
        ),
        market_client=_NoTradeLiveMarketClient(),
        clob_client=_StubClobClient(),
        state_path=state_path,
        log_path=tmp_path / 'live_orders.csv',
        runtime_control=control,
        stop_when_safe=lambda: True,
    )

    snapshot = control.snapshot()
    assert snapshot.pending_live_order is True
    assert snapshot.safe_to_switch is False
    assert snapshot.switch_reason is None
    assert result['status'] == 'pending_settlement'


class _RoundEndMarketClient(_LiveMarketClient):
    def find_current_and_next_rounds(self, *, now):
        window = MarketWindow(
            event_id="evt-2",
            market_id="mkt-2",
            slug="btc-updown-5m-round-end",
            title="BTC 5m Round End",
            start_time=now - timedelta(minutes=1),
            end_time=now + timedelta(minutes=1),
            up_token_id="up-token",
            down_token_id="down-token",
        )
        return window, None


class _StubClobClient:
    def __init__(self, *, post_response=None, order_payloads=None):
        self.created_orders = []
        self.posted_orders = []
        self.post_response = post_response if post_response is not None else {"success": True, "orderID": "oid-123"}
        self.order_payloads = order_payloads or {}

    def create_market_order(self, order_args):
        self.created_orders.append(order_args)
        return {"signed": True, "payload": order_args}

    def post_order(self, order, order_type):
        self.posted_orders.append((order, order_type))
        return self.post_response

    def get_order(self, order_id):
        return self.order_payloads.get(order_id, {})


class _StrictStubClobClient(_StubClobClient):
    def create_market_order(self, order_args):
        assert hasattr(order_args, "price")
        assert hasattr(order_args, "fee_rate_bps")
        return super().create_market_order(order_args)


class _SettlingLiveClient(_LiveMarketClient):
    def get_event_by_slug(self, slug: str):
        if slug != "btc-updown-5m-prev":
            raise AssertionError(f"Unexpected slug {slug}")
        return {"eventMetadata": {"priceToBeat": 100.0, "finalPrice": 90.0}}


class _UnresolvedSettlingLiveClient(_LiveMarketClient):
    def get_event_by_slug(self, slug: str):
        if slug != "btc-updown-5m-prev":
            raise AssertionError(f"Unexpected slug {slug}")
        return {"eventMetadata": {"priceToBeat": None, "finalPrice": None}}


def test_session_state_roundtrip_preserves_pending_paper_trades(tmp_path):
    state_path = tmp_path / "session_state.json"
    state_path.write_text(
        json.dumps(
            {
                "round_index": 7,
                "cash_pnl": 12.5,
                "recovery_loss": 1.0,
                "consecutive_losses": 1,
                "consecutive_max_stake_skips": 0,
                "signal_round_slug": None,
                "signal_round_open_up_price": None,
                "signal_round_locked_side": None,
                "stop_loss_count": 0,
                "daily_realized_pnl": 3.0,
                "current_day": "2026-04-06",
                "pending_live_slug": None,
                "pending_live_side": None,
                "pending_live_price": None,
                "pending_live_order_size": None,
                "pending_live_order_cost": None,
                "pending_live_expected_profit": None,
                "pending_live_order_id": None,
                "pending_live_end_time": None,
                "pending_paper_trades": [
                    {
                        "round_index": 7,
                        "event_slug": "btc-updown-5m-queued",
                        "start_time": "2026-04-06T06:00:00+00:00",
                        "end_time": "2026-04-06T06:05:00+00:00",
                        "side": "UP",
                        "price": 0.44,
                        "order_size": 2.0,
                        "order_cost": 0.88,
                        "expected_profit": 1.12,
                        "strategy": 2,
                        "entry_timing": "OPEN",
                        "signal_open_up_price": 0.5,
                        "signal_current_up_price": 0.49,
                        "signal_threshold": 0.01,
                        "signal_delta": -0.01,
                        "signal_locked": False,
                        "signal_reason": None,
                        "queued_at": "2026-04-06T06:00:05+00:00"
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    state = load_session_state(state_path)

    assert len(state.pending_paper_trades) == 1
    queued = state.pending_paper_trades[0]
    assert queued.event_slug == "btc-updown-5m-queued"
    assert queued.order_cost == 0.88
    assert queued.expected_profit == 1.12


def test_load_session_state_wraps_legacy_paper_state_for_effective_strategy(tmp_path):
    state_path = tmp_path / bytes([115, 101, 115, 115, 105, 111, 110, 95, 115, 116, 97, 116, 101, 46, 106, 115, 111, 110]).decode()
    state_path.write_text(
        json.dumps(
            dict(
                round_index=3,
                cash_pnl=1.5,
                recovery_loss=0.5,
                consecutive_losses=1,
                consecutive_max_stake_skips=0,
                signal_round_slug=None,
                signal_round_open_up_price=None,
                signal_round_locked_side=None,
                strategy6_last_ofi_score=None,
                stop_loss_count=0,
                daily_realized_pnl=1.5,
                current_day=bytes([50, 48, 50, 54, 45, 48, 52, 45, 49, 49]).decode(),
                pending_live_slug=None,
                pending_live_side=None,
                pending_live_price=None,
                pending_live_order_size=None,
                pending_live_order_cost=None,
                pending_live_expected_profit=None,
                pending_live_order_id=None,
                pending_live_end_time=None,
                pending_paper_trades=[],
            )
        ),
        encoding=bytes([117, 116, 102, 45, 56]).decode(),
    )

    state = load_session_state(state_path, effective_paper_strategy_ids=[6])

    assert sorted(state.paper_strategies.keys()) == [6]
    assert state.paper_strategies[6].round_index == 3
    assert state.paper_strategies[6].cash_pnl == 1.5


def test_load_session_state_preserves_multi_strategy_payload(tmp_path):
    state_path = tmp_path / bytes([115, 101, 115, 115, 105, 111, 110, 95, 115, 116, 97, 116, 101, 46, 106, 115, 111, 110]).decode()
    state_path.write_text(
        json.dumps(
            dict(
                paper_strategies={
                    1: dict(
                        round_index=2,
                        cash_pnl=0.5,
                        recovery_loss=0.0,
                        consecutive_losses=0,
                        consecutive_max_stake_skips=0,
                        signal_round_slug=None,
                        signal_round_open_up_price=None,
                        signal_round_locked_side=None,
                        strategy6_last_ofi_score=None,
                        stop_loss_count=0,
                        daily_realized_pnl=0.5,
                        current_day=bytes([50, 48, 50, 54, 45, 48, 52, 45, 49, 49]).decode(),
                        pending_paper_trades=[],
                    ),
                    6: dict(
                        round_index=4,
                        cash_pnl=3.0,
                        recovery_loss=1.0,
                        consecutive_losses=2,
                        consecutive_max_stake_skips=0,
                        signal_round_slug=None,
                        signal_round_open_up_price=None,
                        signal_round_locked_side=None,
                        strategy6_last_ofi_score=0.75,
                        stop_loss_count=1,
                        daily_realized_pnl=3.0,
                        current_day=bytes([50, 48, 50, 54, 45, 48, 52, 45, 49, 49]).decode(),
                        pending_paper_trades=[],
                    ),
                },
            )
        ),
        encoding=bytes([117, 116, 102, 45, 56]).decode(),
    )

    state = load_session_state(state_path, effective_paper_strategy_ids=[1, 6])

    assert state.paper_strategies[1].round_index == 2
    assert state.paper_strategies[6].round_index == 4
    assert state.paper_strategies[6].strategy6_last_ofi_score == 0.75


def test_load_session_state_preserves_paper_experiment_ids(tmp_path):
    state_path = tmp_path / "session_state.json"
    state_path.write_text(
        json.dumps(
            {
                "paper_strategies": {
                    "5": {
                        "round_index": 10,
                        "cash_pnl": 2.0,
                        "recovery_loss": 0.0,
                        "consecutive_losses": 0,
                        "consecutive_max_stake_skips": 0,
                        "signal_round_slug": None,
                        "signal_round_open_up_price": None,
                        "signal_round_locked_side": None,
                        "strategy6_last_ofi_score": None,
                        "stop_loss_count": 0,
                        "daily_realized_pnl": 2.0,
                        "current_day": "2026-04-16",
                        "pending_paper_trades": [],
                        "experiment_id": "challenger-s5-a",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    state = load_session_state(state_path, effective_paper_strategy_ids=[5])

    assert state.paper_strategies[5].experiment_id == "challenger-s5-a"





class _ImmediatePaperRoundClient(_LiveMarketClient):
    def find_current_and_next_rounds(self, *, now):
        window = MarketWindow(
            event_id="evt-paper-1",
            market_id="mkt-paper-1",
            slug="btc-updown-5m-paper-now",
            title="BTC 5m Paper Now",
            start_time=now - timedelta(seconds=10),
            end_time=now + timedelta(minutes=4, seconds=50),
            up_token_id="up-token",
            down_token_id="down-token",
        )
        return window, None


def test_run_paper_trading_waits_for_pending_settlement_before_next_round(tmp_path, monkeypatch):
    stop_event = threading.Event()

    def fake_sleep(_seconds):
        stop_event.set()

    monkeypatch.setattr('trader.time.sleep', fake_sleep)

    state_path = tmp_path / 'state.json'
    state_path.write_text(
        json.dumps(
            {
                'round_index': 1,
                'cash_pnl': 0.0,
                'recovery_loss': 0.0,
                'consecutive_losses': 0,
                'consecutive_max_stake_skips': 0,
                'signal_round_slug': None,
                'signal_round_open_up_price': None,
                'signal_round_locked_side': None,
                'stop_loss_count': 0,
                'daily_realized_pnl': 0.0,
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
                        'round_index': 0,
                        'event_slug': 'btc-updown-5m-paper-prev',
                        'start_time': '2026-04-06T06:00:00+00:00',
                        'end_time': '2099-04-06T06:05:00+00:00',
                        'side': 'UP',
                        'price': 0.5,
                        'order_size': 2.0,
                        'order_cost': 1.0,
                        'expected_profit': 1.0,
                        'strategy': 2,
                        'entry_timing': 'OPEN',
                        'signal_open_up_price': None,
                        'signal_current_up_price': None,
                        'signal_threshold': None,
                        'signal_delta': None,
                        'signal_locked': False,
                        'signal_reason': None,
                        'queued_at': '2026-04-06T06:00:05+00:00',
                    }
                ],
            }
        ),
        encoding='utf-8',
    )

    class _UnresolvedPaperClient(_ImmediatePaperRoundClient):
        def get_event_by_slug(self, slug: str):
            assert slug == 'btc-updown-5m-paper-prev'
            return {'eventMetadata': {'priceToBeat': None, 'finalPrice': None}}

    result = run_paper_trading(
        AppConfig(strategy_id=2, poll_interval_seconds=1),
        client=_UnresolvedPaperClient(),
        state_path=state_path,
        log_path=tmp_path / 'paper.csv',
        stop_event=stop_event,
    )

    state = load_session_state(state_path)
    assert result['status'] == 'stopped'
    assert state.round_index == 1
    assert len(state.pending_paper_trades) == 1
    assert state.pending_paper_trades[0].event_slug == 'btc-updown-5m-paper-prev'
    assert not (tmp_path / 'paper.csv').exists()


def test_run_paper_trading_continues_after_transient_exception(tmp_path, monkeypatch):
    monkeypatch.setattr("trader.time.sleep", lambda _seconds: None)
    cfg = AppConfig(poll_interval_seconds=1)
    client = _TransientPaperClient()

    with pytest.raises(KeyboardInterrupt):
        run_paper_trading(
            cfg,
            client=client,
            state_path=tmp_path / "state.json",
            log_path=tmp_path / "paper.csv",
        )

    assert client.calls == 2


def test_place_live_order_dry_run_returns_order_plan(tmp_path):
    result = place_live_order(
        cfg=AppConfig(),
        market_client=_LiveMarketClient(),
        state_path=tmp_path / "state.json",
        dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert result["side"] == "UP"
    assert result["token_id"] == "up-token"
    assert result["should_trade"] is True
    assert result["order_cost"] > 0


def test_place_live_order_waits_until_entry_window_before_submitting(tmp_path):
    cfg = AppConfig(live_trading_enabled=True)
    stub_clob = _StubClobClient()

    class _FutureRoundClient(_LiveMarketClient):
        def find_current_and_next_rounds(self, *, now):
            window = MarketWindow(
                event_id="evt-1",
                market_id="mkt-1",
                slug="btc-updown-5m-next",
                title="BTC 5m Next",
                start_time=now + timedelta(minutes=1),
                end_time=now + timedelta(minutes=6),
                up_token_id="up-token",
                down_token_id="down-token",
            )
            return None, window

    result = place_live_order(
        cfg=cfg,
        market_client=_FutureRoundClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "live.csv",
    )

    assert result["status"] == "waiting_for_entry"
    assert stub_clob.created_orders == []
    assert stub_clob.posted_orders == []


def test_place_live_order_dry_run_does_not_persist_state_on_signal_skip(tmp_path):
    cfg = AppConfig(
        strategy_id=5,
        signal_momentum_threshold=0.05,
        signal_weak_signal_mode="SKIP",
    )
    state_path = tmp_path / "state.json"
    original = {
        "round_index": 4,
        "cash_pnl": 0.0,
        "recovery_loss": 0.0,
        "consecutive_losses": 0,
        "consecutive_max_stake_skips": 0,
        "signal_round_slug": None,
        "signal_round_open_up_price": None,
        "signal_round_locked_side": None,
        "stop_loss_count": 0,
        "daily_realized_pnl": -1.0,
        "current_day": "1900-01-01",
    }
    state_path.write_text(json.dumps(original), encoding="utf-8")

    result = place_live_order(
        cfg=cfg,
        market_client=_LiveMarketClient(),
        state_path=state_path,
        dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert result["should_trade"] is False
    assert result["skip_reason"] == "signal_too_weak_skip"
    assert json.loads(state_path.read_text(encoding="utf-8")) == original


def test_place_live_order_submits_market_order_with_injected_clob(tmp_path):
    cfg = AppConfig(live_trading_enabled=True)
    stub_clob = _StubClobClient()

    result = place_live_order(
        cfg=cfg,
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "state.json",
    )

    assert result["status"] == "submitted"
    assert result["side"] == "UP"
    assert result["token_id"] == "up-token"
    assert result["order_id"] == "oid-123"
    assert len(stub_clob.created_orders) == 1
    assert stub_clob.created_orders[0].side == "BUY"
    assert len(stub_clob.posted_orders) == 1


def test_place_live_order_rejects_submission_response_without_acceptance(tmp_path):
    cfg = AppConfig(live_trading_enabled=True)
    stub_clob = _StubClobClient(post_response={"success": False, "errorMsg": "rejected"})
    state_path = tmp_path / "state.json"

    with pytest.raises(RuntimeError, match="not accepted"):
        place_live_order(
            cfg=cfg,
            market_client=_LiveMarketClient(),
            clob_client=stub_clob,
            state_path=state_path,
            log_path=tmp_path / "live.csv",
        )

    state = load_session_state(state_path)
    assert state.pending_live_slug is None
    assert state.round_index == 0


def test_place_live_order_with_injected_client_provides_full_market_order_args(tmp_path):
    cfg = AppConfig(live_trading_enabled=True)
    stub_clob = _StrictStubClobClient()

    result = place_live_order(
        cfg=cfg,
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "live.csv",
    )

    assert result["status"] == "submitted"
    assert len(stub_clob.created_orders) == 1


def test_place_live_order_strategy7_sets_market_order_price_cap(tmp_path, monkeypatch):
    cfg = AppConfig(
        strategy_id=7,
        live_trading_enabled=True,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_max_entry_price=0.55,
        strategy7_min_signal_gap=0.01,
        strategy7_confirm_before_entry_seconds=0,
    )
    stub_clob = _StrictStubClobClient()

    class _Strategy7LiveClient(_LiveMarketClient):
        def get_nearest_history_point(self, token_id, *, target_ts, start_ts, end_ts, fidelity, max_offset_seconds):
            return {"price": 0.50}

        def quote_from_market(self, _market):
            return MarketQuote(
                slug="btc-updown-5m-test",
                up_price=0.54,
                down_price=0.46,
                up_best_ask=0.54,
                strategy6_ofi_score=0.8,
                strategy6_signal_at=datetime.now(timezone.utc),
                fetched_at=datetime.now(timezone.utc),
            )

    monkeypatch.setattr("trader._entry_time_for_round", lambda cfg, window: datetime.now(timezone.utc))

    result = place_live_order(
        cfg=cfg,
        market_client=_Strategy7LiveClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "live.csv",
    )

    assert result["status"] == "submitted"
    assert len(stub_clob.created_orders) == 1
    assert stub_clob.created_orders[0].price == pytest.approx(0.55)


def test_place_live_order_resets_state_after_repeated_max_stake_skips(tmp_path):
    cfg = AppConfig(
        live_trading_enabled=True,
        max_stake=1.0,
        max_consecutive_losses=3,
    )
    stub_clob = _StubClobClient()
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "round_index": 3,
                "cash_pnl": -5.0,
                "recovery_loss": 5.0,
                "consecutive_losses": 2,
                "consecutive_max_stake_skips": 2,
                "signal_round_slug": None,
                "signal_round_open_up_price": None,
                "signal_round_locked_side": None,
                "stop_loss_count": 0,
                "daily_realized_pnl": -5.0,
                "current_day": "2026-04-07",
                "pending_live_slug": None,
                "pending_live_side": None,
                "pending_live_price": None,
                "pending_live_order_size": None,
                "pending_live_order_cost": None,
                "pending_live_expected_profit": None,
                "pending_live_order_id": None,
                "pending_live_end_time": None,
                "pending_paper_trades": [],
            }
        ),
        encoding="utf-8",
    )

    result = place_live_order(
        cfg=cfg,
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=state_path,
        log_path=tmp_path / "live.csv",
    )

    state = load_session_state(state_path)
    assert result["status"] == "skipped"
    assert result["skip_reason"] == "order_cost_above_max_stake"
    assert stub_clob.created_orders == []
    assert state.round_index == 4
    assert state.recovery_loss == 0.0
    assert state.consecutive_losses == 0
    assert state.consecutive_max_stake_skips == 0
    assert state.stop_loss_count == 1


def test_place_live_order_settles_previous_pending_trade_before_new_submission(tmp_path):
    cfg = AppConfig(live_trading_enabled=True, max_stake=25.0)
    stub_clob = _StubClobClient(
        order_payloads={
            "oid-prev": {
                "status": "filled",
                "filled_order_size": 2.0,
                "filled_order_cost": 1.0,
                "avg_price": 0.5,
            }
        }
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "round_index": 1,
                "cash_pnl": 0.0,
                "recovery_loss": 0.0,
                "consecutive_losses": 0,
                "consecutive_max_stake_skips": 0,
                "signal_round_slug": None,
                "signal_round_open_up_price": None,
                "signal_round_locked_side": None,
                "stop_loss_count": 0,
                "daily_realized_pnl": 0.0,
                "current_day": "2026-04-02",
                "pending_live_slug": "btc-updown-5m-prev",
                "pending_live_side": "UP",
                "pending_live_price": 0.5,
                "pending_live_order_size": 2.0,
                "pending_live_order_cost": 1.0,
                "pending_live_expected_profit": 1.0,
                "pending_live_end_time": "2026-04-02T00:00:00+00:00",
                "pending_live_order_id": "oid-prev",
            }
        ),
        encoding="utf-8",
    )

    result = place_live_order(
        cfg=cfg,
        market_client=_SettlingLiveClient(),
        clob_client=stub_clob,
        state_path=state_path,
        log_path=tmp_path / "live.csv",
    )

    assert result["status"] == "submitted"
    assert stub_clob.created_orders[0].amount > 1.0

    state = load_session_state(state_path)
    assert state.recovery_loss == pytest.approx(1.0)
    assert state.consecutive_losses == 1
    assert state.round_index == 2


def test_place_live_order_waits_for_previous_pending_trade_settlement(tmp_path):
    cfg = AppConfig(live_trading_enabled=True, max_stake=25.0)
    stub_clob = _StubClobClient(order_payloads={"oid-prev": {"status": "filled"}})
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "round_index": 1,
                "cash_pnl": 0.0,
                "recovery_loss": 0.0,
                "consecutive_losses": 0,
                "consecutive_max_stake_skips": 0,
                "signal_round_slug": None,
                "signal_round_open_up_price": None,
                "signal_round_locked_side": None,
                "stop_loss_count": 0,
                "daily_realized_pnl": 0.0,
                "current_day": "2026-04-02",
                "pending_live_slug": "btc-updown-5m-prev",
                "pending_live_side": "UP",
                "pending_live_price": 0.5,
                "pending_live_order_size": 2.0,
                "pending_live_order_cost": 1.0,
                "pending_live_expected_profit": 1.0,
                "pending_live_end_time": "2026-04-02T00:00:00+00:00",
                "pending_live_order_id": "oid-prev",
            }
        ),
        encoding="utf-8",
    )

    result = place_live_order(
        cfg=cfg,
        market_client=_UnresolvedSettlingLiveClient(),
        clob_client=stub_clob,
        state_path=state_path,
        log_path=tmp_path / "live.csv",
    )

    assert result["status"] == "pending_settlement"
    assert result["skip_reason"] == "awaiting_fill_confirmation"
    assert stub_clob.created_orders == []


def test_place_live_order_requires_private_key_without_injected_client(tmp_path):
    cfg = AppConfig(live_trading_enabled=True)

    with pytest.raises(RuntimeError, match="PRIVATE_KEY"):
        place_live_order(
            cfg=cfg,
            market_client=_LiveMarketClient(),
            state_path=tmp_path / "state.json",
            dry_run=False,
        )


def test_update_max_stake_skip_streak_alerts_once_per_streak():
    state = SessionState()

    assert _update_max_stake_skip_streak(state, skip_reason="order_cost_above_max_stake", threshold=3) is False
    assert _update_max_stake_skip_streak(state, skip_reason="order_cost_above_max_stake", threshold=3) is False
    assert _update_max_stake_skip_streak(state, skip_reason="order_cost_above_max_stake", threshold=3) is True
    assert _update_max_stake_skip_streak(state, skip_reason="order_cost_above_max_stake", threshold=3) is False
    assert state.consecutive_max_stake_skips == 4

    assert _update_max_stake_skip_streak(state, skip_reason="invalid_price", threshold=3) is False
    assert state.consecutive_max_stake_skips == 0


def test_resolve_side_from_strategy_uses_quote_momentum_for_strategy_5():
    cfg = AppConfig(
        strategy_id=5,
        signal_momentum_threshold=0.02,
        signal_fallback_strategy_id=2,
        signal_weak_signal_mode="FALLBACK",
    )
    state = SessionState(round_index=0)

    first_quote = MarketQuote(slug="s1", up_best_ask=0.56, up_price=0.55)
    side_first = _resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=first_quote)
    assert side_first.side is None
    assert side_first.reason == "signal_too_weak_skip"
    assert state.signal_round_open_up_price == 0.55

    lower_quote = MarketQuote(slug="s1", up_best_ask=0.52, up_price=0.52)
    side_second = _resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=lower_quote)
    assert side_second.side == "DOWN"


def test_resolve_side_from_strategy_prefers_up_last_price_over_best_ask_for_signal():
    cfg = AppConfig(
        strategy_id=5,
        signal_momentum_threshold=0.02,
        signal_weak_signal_mode="SKIP",
    )
    state = SessionState(round_index=0)

    # Open anchor should come from up_price, not best_ask.
    first_quote = MarketQuote(slug="s1", up_best_ask=0.90, up_price=0.50)
    first = _resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=first_quote)
    assert first.side is None
    assert state.signal_round_open_up_price == 0.50

    # Even with extreme best_ask spike, side should be based on up_price momentum.
    second_quote = MarketQuote(slug="s1", up_best_ask=0.99, up_price=0.54)
    second = _resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=second_quote)
    assert second.side == "UP"
    assert second.signal_delta is not None
    assert second.signal_delta == pytest.approx(0.04)


def test_resolve_side_from_strategy_skips_weak_signal_when_mode_is_skip():
    cfg = AppConfig(
        strategy_id=5,
        signal_momentum_threshold=0.02,
        signal_weak_signal_mode="SKIP",
    )
    state = SessionState(round_index=0)

    quote = MarketQuote(slug="s1", up_best_ask=0.56, up_price=0.55)
    decision = _resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote)

    assert decision.side is None
    assert decision.reason == "signal_too_weak_skip"


def test_resolve_side_from_strategy_locks_side_near_entry():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=5,
        signal_momentum_threshold=0.01,
        signal_weak_signal_mode="SKIP",
        signal_lock_before_entry_seconds=20,
    )
    state = SessionState(round_index=0, signal_round_slug="s1", signal_round_open_up_price=0.50)

    up_quote = MarketQuote(slug="s1", up_best_ask=0.53, up_price=0.53)
    first = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=up_quote,
        now=now,
        entry_time=now + timedelta(seconds=5),
    )
    assert first.side == "UP"
    assert state.signal_round_locked_side == "UP"

    down_quote = MarketQuote(slug="s1", up_best_ask=0.45, up_price=0.45)
    second = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=down_quote,
        now=now + timedelta(seconds=2),
        entry_time=now + timedelta(seconds=5),
    )
    assert second.side == "UP"
    assert second.signal_locked is True


def test_strategy7_returns_up_when_ofi_and_momentum_agree():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_max_entry_price=0.56,
        strategy7_min_signal_gap=0.01,
        strategy7_confirm_before_entry_seconds=12,
        binance_signal_stale_seconds=2.0,
    )
    state = SessionState(round_index=0, signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.54,
        up_best_ask=0.54,
        strategy6_ofi_score=0.8,
        fetched_at=now,
        strategy6_signal_at=now,
    )

    decision = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        now=now,
        entry_time=now + timedelta(seconds=20),
    )

    assert decision.side == "UP"
    assert decision.reason is None
    assert decision.signal_delta == pytest.approx(0.04)


def test_strategy7_locks_confirmed_side_for_round():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_max_entry_price=0.56,
        strategy7_min_signal_gap=0.01,
        strategy7_confirm_before_entry_seconds=3,
        signal_lock_before_entry_seconds=20,
        binance_signal_stale_seconds=2.0,
    )
    state = SessionState(round_index=0, signal_round_slug="s1", signal_round_open_up_price=0.50)

    first = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=MarketQuote(
            slug="s1",
            up_price=0.54,
            up_best_ask=0.54,
            strategy6_ofi_score=0.8,
            fetched_at=now,
            strategy6_signal_at=now,
        ),
        now=now,
        entry_time=now + timedelta(seconds=10),
    )

    assert first.side == "UP"
    assert state.signal_round_locked_side == "UP"

    second = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=MarketQuote(
            slug="s1",
            up_price=0.45,
            up_best_ask=0.45,
            strategy6_ofi_score=-0.9,
            fetched_at=now + timedelta(seconds=2),
            strategy6_signal_at=now + timedelta(seconds=2),
        ),
        now=now + timedelta(seconds=2),
        entry_time=now + timedelta(seconds=10),
    )

    assert second.side == "UP"
    assert second.signal_locked is True


def test_strategy7_rechecks_max_entry_price_after_lock():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_max_entry_price=0.55,
        strategy7_min_signal_gap=0.01,
        strategy7_confirm_before_entry_seconds=3,
        signal_lock_before_entry_seconds=20,
        binance_signal_stale_seconds=2.0,
    )
    state = SessionState(round_index=0, signal_round_slug="s1", signal_round_open_up_price=0.50)

    first = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=MarketQuote(
            slug="s1",
            up_price=0.54,
            up_best_ask=0.54,
            strategy6_ofi_score=0.8,
            fetched_at=now,
            strategy6_signal_at=now,
        ),
        now=now,
        entry_time=now + timedelta(seconds=10),
    )

    assert first.side == "UP"
    assert state.signal_round_locked_side == "UP"

    second = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=MarketQuote(
            slug="s1",
            up_price=0.58,
            up_best_ask=0.58,
            strategy6_ofi_score=0.8,
            fetched_at=now + timedelta(seconds=1),
            strategy6_signal_at=now + timedelta(seconds=1),
        ),
        now=now + timedelta(seconds=1),
        entry_time=now + timedelta(seconds=10),
    )

    assert second.side is None
    assert second.reason == "strategy7_price_too_high"
    assert second.signal_locked is True
    assert state.signal_round_locked_side == "UP"


def test_strategy7_skips_when_signals_conflict():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
    )
    state = SessionState(round_index=0, signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.47,
        up_best_ask=0.47,
        strategy6_ofi_score=0.8,
        fetched_at=now,
        strategy6_signal_at=now,
    )

    decision = _resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)

    assert decision.side is None
    assert decision.reason == "strategy7_signal_conflict"


def test_strategy7_skips_when_confirmation_is_too_late():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_confirm_before_entry_seconds=15,
    )
    state = SessionState(round_index=0, signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.54,
        up_best_ask=0.54,
        strategy6_ofi_score=0.8,
        fetched_at=now,
        strategy6_signal_at=now,
    )

    decision = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        now=now,
        entry_time=now + timedelta(seconds=10),
    )

    assert decision.side is None
    assert decision.reason == "strategy7_entry_too_late"


def test_append_trade_log_rotates_legacy_schema_file(tmp_path):
    log_path = tmp_path / "paper_trades.csv"
    log_path.write_text("timestamp,mode\n2026-03-31T00:00:00+00:00,paper\n", encoding="utf-8")

    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=datetime.now(timezone.utc),
            mode="paper",
            round_index=1,
            strategy=5,
            entry_timing="OPEN",
            event_slug="s1",
            start_time=datetime.now(timezone.utc) - timedelta(minutes=5),
            end_time=datetime.now(timezone.utc),
            side="UP",
            price=0.5,
            order_size=2.0,
            order_cost=1.0,
            expected_profit=1.0,
            result="UP",
            trade_pnl=1.0,
            cash_pnl=1.0,
            recovery_loss=0.0,
            consecutive_losses=0,
        ),
    )

    rotated = list(tmp_path.glob("paper_trades_legacy_*.csv"))
    assert len(rotated) == 1
    header = log_path.read_text(encoding="utf-8").splitlines()[0]
    assert "signal_reason" in header


def test_append_trade_log_writes_experiment_id_column(tmp_path):
    log_path = tmp_path / "paper_trades.csv"

    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=datetime.now(timezone.utc),
            mode="paper",
            round_index=1,
            strategy=5,
            entry_timing="OPEN",
            event_slug="s1",
            start_time=datetime.now(timezone.utc) - timedelta(minutes=5),
            end_time=datetime.now(timezone.utc),
            side="UP",
            price=0.5,
            order_size=2.0,
            order_cost=1.0,
            expected_profit=1.0,
            result="UP",
            trade_pnl=1.0,
            cash_pnl=1.0,
            recovery_loss=0.0,
            consecutive_losses=0,
            experiment_id="challenger-s5-a",
        ),
    )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert "experiment_id" in lines[0]
    assert "challenger-s5-a" in lines[1]


def test_paper_experiment_id_defaults_to_strategy_prefix():
    state = PaperStrategyState()

    experiment_id = _paper_experiment_id(5, state)

    assert experiment_id == "strategy-5"
    assert state.experiment_id == "strategy-5"


def test_place_live_order_skips_when_ws_stale_guard_triggered(tmp_path):
    cfg = AppConfig(ws_trade_guard_stale_seconds=0.0)

    class _StaleLiveClient(_LiveMarketClient):
        def get_ws_runtime_stats(self):
            return {
                "ws_enabled": True,
                "ws_available": True,
                "ws_last_message_age_seconds": 10.0,
            }

    result = place_live_order(
        cfg=cfg,
        market_client=_StaleLiveClient(),
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "live.csv",
        dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert result["should_trade"] is False
    assert result["skip_reason"] == "ws_stale"


def test_run_paper_trading_dry_run_skips_when_ws_stale_guard_triggered(tmp_path):
    cfg = AppConfig(
        strategy_id=2,
        ws_trade_guard_stale_seconds=0.0,
    )

    class _StalePaperClient(_LiveMarketClient):
        def get_ws_runtime_stats(self):
            return {
                "ws_enabled": True,
                "ws_available": True,
                "ws_last_message_age_seconds": 10.0,
            }

    result = run_paper_trading(
        cfg,
        client=_StalePaperClient(),
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "paper.csv",
        dry_run_once=True,
    )

    assert result["status"] == "dry_run"
    assert result["should_trade"] is False
    assert result["skip_reason"] == "ws_stale"


def test_run_paper_trading_dry_run_skips_when_entry_window_missed(tmp_path):
    cfg = AppConfig(strategy_id=2)

    result = run_paper_trading(
        cfg,
        client=_LiveMarketClient(),
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "paper.csv",
        dry_run_once=True,
    )

    assert result["status"] == "dry_run"
    assert result["should_trade"] is False
    assert result["skip_reason"] == "entry_window_missed"


def test_run_paper_trading_dry_run_allows_trade_within_entry_grace_window(tmp_path):
    cfg = AppConfig(strategy_id=2)

    class _GraceWindowPaperClient(_LiveMarketClient):
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

    result = run_paper_trading(
        cfg,
        client=_GraceWindowPaperClient(),
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "paper.csv",
        dry_run_once=True,
    )

    assert result["status"] == "dry_run"
    assert result["should_trade"] is True
    assert result["skip_reason"] is None


def test_run_paper_trading_dry_run_prefers_next_round_when_current_entry_window_missed(tmp_path):
    cfg = AppConfig(strategy_id=2)

    class _CurrentMissedNextAvailableClient(_LiveMarketClient):
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

    result = run_paper_trading(
        cfg,
        client=_CurrentMissedNextAvailableClient(),
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "paper.csv",
        dry_run_once=True,
    )

    assert result["status"] == "dry_run"
    assert result["slug"] == "btc-updown-5m-next"
    assert result["should_trade"] is True
    assert result["skip_reason"] is None


def test_run_paper_trading_dry_run_allows_trade_after_day_rollover(tmp_path):
    cfg = AppConfig(strategy_id=2)
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "round_index": 0,
                "cash_pnl": -60.0,
                "recovery_loss": 0.0,
                "consecutive_losses": 0,
                "consecutive_max_stake_skips": 0,
                "signal_round_slug": None,
                "signal_round_open_up_price": None,
                "signal_round_locked_side": None,
                "stop_loss_count": 0,
                "daily_realized_pnl": -60.0,
                "current_day": "1900-01-01",
            }
        ),
        encoding="utf-8",
    )

    class _GraceWindowPaperClient(_LiveMarketClient):
        def find_current_and_next_rounds(self, *, now):
            window = MarketWindow(
                event_id="evt-reset",
                market_id="mkt-reset",
                slug="btc-updown-5m-reset",
                title="BTC 5m Reset",
                start_time=now - timedelta(seconds=7),
                end_time=now + timedelta(minutes=4, seconds=53),
                up_token_id="up-token",
                down_token_id="down-token",
            )
            return window, None

    result = run_paper_trading(
        cfg,
        client=_GraceWindowPaperClient(),
        state_path=state_path,
        log_path=tmp_path / "paper.csv",
        dry_run_once=True,
    )

    assert result["status"] == "dry_run"
    assert result["should_trade"] is True


def test_run_paper_trading_resets_state_after_repeated_max_stake_skips(tmp_path, monkeypatch):
    monkeypatch.setattr("trader._sleep_until_round_end", lambda cfg, window, stop_event=None: False)
    cfg = AppConfig(
        strategy_id=2,
        max_stake=1.0,
        max_consecutive_losses=3,
        poll_interval_seconds=1,
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "round_index": 3,
                "cash_pnl": -5.0,
                "recovery_loss": 5.0,
                "consecutive_losses": 2,
                "consecutive_max_stake_skips": 2,
                "signal_round_slug": None,
                "signal_round_open_up_price": None,
                "signal_round_locked_side": None,
                "stop_loss_count": 0,
                "daily_realized_pnl": -5.0,
                "current_day": "2026-04-07",
                "pending_live_slug": None,
                "pending_live_side": None,
                "pending_live_price": None,
                "pending_live_order_size": None,
                "pending_live_order_cost": None,
                "pending_live_expected_profit": None,
                "pending_live_order_id": None,
                "pending_live_end_time": None,
                "pending_paper_trades": [],
            }
        ),
        encoding="utf-8",
    )

    class _NearEntryPaperClient(_LiveMarketClient):
        def find_current_and_next_rounds(self, *, now):
            window = MarketWindow(
                event_id="evt-paper-reset",
                market_id="mkt-paper-reset",
                slug="btc-updown-5m-paper-reset",
                title="BTC 5m Paper Reset",
                start_time=now - timedelta(seconds=7),
                end_time=now + timedelta(minutes=4, seconds=53),
                up_token_id="up-token",
                down_token_id="down-token",
            )
            return window, None

    result = run_paper_trading(
        cfg,
        client=_NearEntryPaperClient(),
        state_path=state_path,
        log_path=tmp_path / "paper.csv",
    )

    state = load_session_state(state_path)
    assert result["status"] == "stopped"
    assert state.round_index == 4
    assert state.recovery_loss == 0.0
    assert state.consecutive_losses == 0
    assert state.consecutive_max_stake_skips == 0
    assert state.stop_loss_count == 1


def test_run_paper_trading_stops_when_stop_event_is_set(tmp_path, monkeypatch):
    stop_event = threading.Event()
    sleep_calls = {"count": 0}

    def fake_sleep(_seconds):
        sleep_calls["count"] += 1
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)

    result = run_paper_trading(
        AppConfig(poll_interval_seconds=1),
        client=_NoMarketClient(),
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "paper.csv",
        stop_event=stop_event,
    )

    assert result["status"] == "stopped"
    assert sleep_calls["count"] == 1


def test_run_paper_trading_reports_safe_to_switch_when_idle(tmp_path):
    control = RuntimeControl(initial_mode='paper')

    result = run_paper_trading(
        AppConfig(poll_interval_seconds=1),
        client=_NoMarketClient(),
        state_path=tmp_path / 'state.json',
        log_path=tmp_path / 'paper.csv',
        runtime_control=control,
        stop_when_safe=lambda: True,
    )

    snapshot = control.snapshot()
    assert result['status'] == 'stopped'
    assert snapshot.active_mode == 'paper'
    assert snapshot.current_round_slug is None
    assert snapshot.round_in_progress is False
    assert snapshot.safe_to_switch is True
    assert snapshot.pending_live_order is False


def test_run_paper_trading_refreshes_config_provider_between_iterations(tmp_path, monkeypatch):
    stop_event = threading.Event()
    sleep_calls: list[float] = []
    config_sequence = [1.0, 5.0]
    config_calls: list[float] = []
    initial_state_data = {
        "round_index": 1,
        "cash_pnl": 10.0,
        "recovery_loss": 2.0,
        "consecutive_losses": 0,
        "consecutive_max_stake_skips": 0,
        "signal_round_slug": None,
        "signal_round_open_up_price": None,
        "signal_round_locked_side": None,
        "stop_loss_count": 0,
        "daily_realized_pnl": 5.0,
        "current_day": "2026-04-01",
    }
    state = SessionState(**initial_state_data)
    initial_state_snapshot = asdict(state)
    monkeypatch.setattr("trader.load_session_state", lambda path: state)
    monkeypatch.setattr("trader.save_session_state", lambda path, payload: None)
    monkeypatch.setattr("trader._refresh_daily_session_state", lambda state_arg, now: False)

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) == 2:
            stop_event.set()

    def config_provider():
        value = config_sequence.pop(0) if config_sequence else 5.0
        config_calls.append(value)
        return AppConfig(poll_interval_seconds=value)

    monkeypatch.setattr("trader.time.sleep", fake_sleep)

    result = run_paper_trading(
        AppConfig(poll_interval_seconds=1),
        client=_NoMarketClient(),
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "paper.csv",
        stop_event=stop_event,
        config_provider=config_provider,
    )

    assert result["status"] == "stopped"
    assert sleep_calls == [1.0, 5.0]
    assert config_calls == [1.0, 5.0]
    assert asdict(state) == initial_state_snapshot


def test_run_paper_trading_config_provider_refreshes_default_client(tmp_path, monkeypatch):
    client_instances: list["RecordingClient"] = []

    class RecordingClient:
        def __init__(self, cfg: AppConfig):
            self.config = cfg
            client_instances.append(self)

        def find_current_and_next_rounds(self, *, now):
            return None, None

    stop_event = threading.Event()
    sleep_calls: list[float] = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        stop_event.set()

    def config_provider():
        return AppConfig(poll_interval_seconds=99)

    monkeypatch.setattr("trader.PolymarketClient", RecordingClient)
    monkeypatch.setattr("trader.time.sleep", fake_sleep)

    result = run_paper_trading(
        AppConfig(poll_interval_seconds=1),
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "paper.csv",
        stop_event=stop_event,
        config_provider=config_provider,
    )

    assert result["status"] == "stopped"
    assert sleep_calls == [99]
    assert len(client_instances) == 1
    assert client_instances[0].config.poll_interval_seconds == 99


def test_run_paper_trading_starts_binance_service_when_strategy6_is_enabled_by_config_reload(tmp_path, monkeypatch):
    class RecordingBinanceSignalService:
        instances: list["RecordingBinanceSignalService"] = []

        def __init__(self, *, ws_url: str, stream: str):
            self.ws_url = ws_url.rstrip("/") + "/" + stream.lstrip("/")
            self.started = 0
            self.closed = 0
            RecordingBinanceSignalService.instances.append(self)

        def start(self):
            self.started += 1

        def close(self):
            self.closed += 1

        def latest(self):
            return None

    stop_event = threading.Event()
    sleep_calls: list[float] = []
    configs = [
        AppConfig(strategy_id=2, paper_strategy_ids=[2], poll_interval_seconds=1),
        AppConfig(strategy_id=2, paper_strategy_ids=[2, 6], poll_interval_seconds=1),
    ]

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            stop_event.set()

    def config_provider():
        if configs:
            return configs.pop(0)
        return AppConfig(strategy_id=2, paper_strategy_ids=[2, 6], poll_interval_seconds=1)

    monkeypatch.setattr("trader.BinanceDepth5SignalService", RecordingBinanceSignalService)
    monkeypatch.setattr("trader.time.sleep", fake_sleep)

    result = run_paper_trading(
        AppConfig(strategy_id=2, paper_strategy_ids=[2], poll_interval_seconds=1),
        client=_NoMarketClient(),
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "paper.csv",
        stop_event=stop_event,
        config_provider=config_provider,
    )

    assert result["status"] == "stopped"
    assert sleep_calls == [1, 1]
    assert len(RecordingBinanceSignalService.instances) == 1
    assert RecordingBinanceSignalService.instances[0].started == 1


def test_run_paper_trading_processes_all_selected_strategies(tmp_path, monkeypatch):
    monkeypatch.setattr("trader._sleep_until_round_end", lambda cfg, window, stop_event=None: False)

    class _AllStrategiesPaperClient(_LiveMarketClient):
        def find_current_and_next_rounds(self, *, now):
            window = MarketWindow(
                event_id="evt-all",
                market_id="mkt-all",
                slug="btc-updown-5m-all",
                title="BTC 5m All Strategies",
                start_time=now - timedelta(seconds=7),
                end_time=now + timedelta(minutes=4, seconds=53),
                up_token_id="up-token",
                down_token_id="down-token",
            )
            return window, None

        def get_nearest_history_point(self, token_id, *, target_ts, start_ts, end_ts, fidelity, max_offset_seconds):
            return {"price": 0.50}

    signal_service = BinanceDepth5SignalService(
        ws_url="wss://stream.binance.com:9443/ws",
        stream="btcusdt@depth5",
    )
    signal = signal_service.push_payload(
        {"b": [["100000", "5"]], "a": [["100001", "1"]]},
        now=datetime.now(timezone.utc),
    )
    strategy_ids = [1, 2, 3, 4, 5, 6]

    result = run_paper_trading(
        AppConfig(
            strategy_id=2,
            paper_strategy_ids=strategy_ids,
            poll_interval_seconds=1,
            signal_momentum_threshold=0.02,
            ofi_threshold=0.65,
        ),
        client=_AllStrategiesPaperClient(),
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "paper.csv",
        binance_signal_service=signal_service,
    )

    state = load_session_state(tmp_path / "state.json", effective_paper_strategy_ids=strategy_ids)

    assert result["status"] == "stopped"
    assert sorted(state.paper_strategies.keys()) == strategy_ids
    for strategy_id in strategy_ids:
        pending = state.paper_strategies[strategy_id].pending_paper_trades
        assert len(pending) == 1
        assert pending[0].strategy == strategy_id
        assert pending[0].event_slug == "btc-updown-5m-all"
    assert state.paper_strategies[5].pending_paper_trades[0].signal_delta == pytest.approx(0.05)
    assert state.paper_strategies[6].strategy6_last_ofi_score == pytest.approx(signal.ofi_score)


def test_settled_pending_paper_trade_writes_single_final_csv_row(tmp_path):
    state_path = tmp_path / 'state.json'
    log_path = tmp_path / 'paper.csv'
    state_path.write_text(
        json.dumps(
            {
                'round_index': 1,
                'cash_pnl': 0.0,
                'recovery_loss': 0.0,
                'consecutive_losses': 0,
                'consecutive_max_stake_skips': 0,
                'signal_round_slug': None,
                'signal_round_open_up_price': None,
                'signal_round_locked_side': None,
                'stop_loss_count': 0,
                'daily_realized_pnl': 0.0,
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
                        'round_index': 0,
                        'event_slug': 'btc-updown-5m-paper-prev',
                        'start_time': '2026-04-06T06:00:00+00:00',
                        'end_time': '2026-04-06T06:05:00+00:00',
                        'side': 'UP',
                        'price': 0.5,
                        'order_size': 2.0,
                        'order_cost': 1.0,
                        'expected_profit': 1.0,
                        'strategy': 2,
                        'entry_timing': 'OPEN',
                        'signal_open_up_price': 0.51,
                        'signal_current_up_price': 0.5,
                        'signal_threshold': 0.01,
                        'signal_delta': -0.01,
                        'signal_locked': False,
                        'signal_reason': None,
                        'queued_at': '2026-04-06T06:00:05+00:00',
                        'experiment_id': 'challenger-s5-a',
                    }
                ],
            }
        ),
        encoding='utf-8',
    )

    class _SettlingPaperClient(_NoMarketClient):
        def get_event_by_slug(self, slug: str):
            assert slug == 'btc-updown-5m-paper-prev'
            return {'eventMetadata': {'priceToBeat': 100.0, 'finalPrice': 120.0}}

    result = run_paper_trading(
        AppConfig(poll_interval_seconds=1),
        client=_SettlingPaperClient(),
        state_path=state_path,
        log_path=log_path,
        stop_when_safe=lambda: True,
    )

    rows = log_path.read_text(encoding='utf-8').splitlines()
    assert result['status'] == 'stopped'
    assert len(rows) == 2
    assert 'btc-updown-5m-paper-prev' in rows[1]
    assert 'challenger-s5-a' in rows[1]
    assert ',UP,0.5,2.0,1.0,1.0,UP,1.0,1.0,0.0,0,' in rows[1]


def test_run_paper_trading_settles_pending_paper_trade_before_processing_new_round(tmp_path):
    state_path = tmp_path / 'state.json'
    state_path.write_text(
        json.dumps(
            {
                'round_index': 1,
                'cash_pnl': 0.0,
                'recovery_loss': 0.0,
                'consecutive_losses': 0,
                'consecutive_max_stake_skips': 0,
                'signal_round_slug': None,
                'signal_round_open_up_price': None,
                'signal_round_locked_side': None,
                'stop_loss_count': 0,
                'daily_realized_pnl': 0.0,
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
                        'round_index': 0,
                        'event_slug': 'btc-updown-5m-paper-prev',
                        'start_time': '2026-04-06T06:00:00+00:00',
                        'end_time': '2026-04-06T06:05:00+00:00',
                        'side': 'UP',
                        'price': 0.5,
                        'order_size': 2.0,
                        'order_cost': 1.0,
                        'expected_profit': 1.0,
                        'strategy': 2,
                        'entry_timing': 'OPEN',
                        'signal_open_up_price': None,
                        'signal_current_up_price': None,
                        'signal_threshold': None,
                        'signal_delta': None,
                        'signal_locked': False,
                        'signal_reason': None,
                        'queued_at': '2026-04-06T06:00:05+00:00',
                    }
                ],
            }
        ),
        encoding='utf-8',
    )

    class _SettlingPaperClient(_NoMarketClient):
        def get_event_by_slug(self, slug: str):
            assert slug == 'btc-updown-5m-paper-prev'
            return {'eventMetadata': {'priceToBeat': 100.0, 'finalPrice': 120.0}}

    result = run_paper_trading(
        AppConfig(poll_interval_seconds=1),
        client=_SettlingPaperClient(),
        state_path=state_path,
        log_path=tmp_path / 'paper.csv',
        stop_when_safe=lambda: True,
    )

    state = load_session_state(state_path)
    assert result['status'] == 'stopped'
    assert state.cash_pnl == 1.0
    assert state.daily_realized_pnl == 1.0
    assert state.recovery_loss == 0.0
    assert state.consecutive_losses == 0
    assert state.pending_paper_trades == []


def test_run_paper_trading_stop_event_during_settlement_wait_prevents_settlement(tmp_path, monkeypatch):
    stop_event = threading.Event()
    sleep_calls: list[float] = []
    settle_calls: list[str] = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        stop_event.set()

    def fail_settle(*args, **kwargs):
        settle_calls.append("called")
        raise RuntimeError("Round is not resolved yet")

    monkeypatch.setattr("trader.time.sleep", fake_sleep)
    monkeypatch.setattr("trader._settle_paper_trade", fail_settle)
    monkeypatch.setattr("trader._sleep_until_round_end", lambda cfg, window, stop_event=None: True)
    monkeypatch.setattr(
        "trader._resolve_side_from_strategy",
        lambda **kwargs: SideDecision(side="UP"),
    )
    monkeypatch.setattr(
        "trader.build_trade_plan",
        lambda *args, **kwargs: TradePlan(
            True,
            "UP",
            price=0.5,
            order_size=1.0,
            order_cost=0.5,
            expected_profit=0.5,
        ),
    )
    monkeypatch.setattr("trader._entry_time_for_round", lambda cfg, window: datetime.now(timezone.utc))

    result = run_paper_trading(
        AppConfig(poll_interval_seconds=1),
        client=_RoundEndMarketClient(),
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "paper.csv",
        stop_event=stop_event,
    )

    assert result["status"] == "stopped"
    assert settle_calls == []
    state = load_session_state(tmp_path / "state.json")
    assert len(state.pending_paper_trades) == 1


def test_run_paper_trading_stop_event_stops_during_round_end_wait(tmp_path, monkeypatch):
    stop_event = threading.Event()
    sleep_calls: list[float] = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)
    monkeypatch.setattr(
        "trader._resolve_side_from_strategy",
        lambda **kwargs: SideDecision(side="UP"),
    )
    monkeypatch.setattr(
        "trader.build_trade_plan",
        lambda *args, **kwargs: TradePlan(
            True,
            "UP",
            price=0.5,
            order_size=1.0,
            order_cost=0.5,
            expected_profit=0.5,
        ),
    )
    def fail_settle(*args, **kwargs):
        raise AssertionError("Settlement should not run when stop_event triggers earlier")
    monkeypatch.setattr("trader._settle_paper_trade", fail_settle)

    result = run_paper_trading(
        AppConfig(poll_interval_seconds=1),
        client=_RoundEndMarketClient(),
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "paper.csv",
        stop_event=stop_event,
    )

    assert result["status"] == "stopped"
    assert sleep_calls == [1.0]




def test_run_paper_trading_multi_strategy_settles_ready_strategy_without_blocking_others(tmp_path, monkeypatch):
    stop_event = threading.Event()

    def fake_sleep(_seconds):
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)

    state_path = tmp_path / "state.json"
    log_path = tmp_path / "paper.csv"
    state_path.write_text(
        json.dumps(
            {
                "paper_strategies": {
                    "1": {
                        "round_index": 1,
                        "cash_pnl": 0.0,
                        "recovery_loss": 0.0,
                        "consecutive_losses": 0,
                        "consecutive_max_stake_skips": 0,
                        "signal_round_slug": None,
                        "signal_round_open_up_price": None,
                        "signal_round_locked_side": None,
                        "strategy6_last_ofi_score": None,
                        "stop_loss_count": 0,
                        "daily_realized_pnl": 0.0,
                        "current_day": "2026-04-11",
                        "pending_paper_trades": [
                            {
                                "round_index": 1,
                                "event_slug": "btc-updown-5m-paper-s1",
                                "start_time": "2026-04-11T06:00:00+00:00",
                                "end_time": "2099-04-11T06:05:00+00:00",
                                "side": "UP",
                                "price": 0.5,
                                "order_size": 2.0,
                                "order_cost": 1.0,
                                "expected_profit": 1.0,
                                "strategy": 1,
                                "entry_timing": "OPEN",
                                "signal_open_up_price": None,
                                "signal_current_up_price": None,
                                "signal_threshold": None,
                                "signal_delta": None,
                                "signal_locked": False,
                                "signal_reason": None,
                                "queued_at": "2026-04-11T06:00:05+00:00"
                            }
                        ]
                    },
                    "6": {
                        "round_index": 4,
                        "cash_pnl": 0.0,
                        "recovery_loss": 0.0,
                        "consecutive_losses": 0,
                        "consecutive_max_stake_skips": 0,
                        "signal_round_slug": None,
                        "signal_round_open_up_price": None,
                        "signal_round_locked_side": None,
                        "strategy6_last_ofi_score": None,
                        "stop_loss_count": 0,
                        "daily_realized_pnl": 0.0,
                        "current_day": "2026-04-11",
                        "pending_paper_trades": [
                            {
                                "round_index": 4,
                                "event_slug": "btc-updown-5m-paper-s6",
                                "start_time": "2026-04-11T06:05:00+00:00",
                                "end_time": "2026-04-11T06:10:00+00:00",
                                "side": "DOWN",
                                "price": 0.4,
                                "order_size": 2.5,
                                "order_cost": 1.0,
                                "expected_profit": 1.5,
                                "strategy": 6,
                                "entry_timing": "OPEN",
                                "signal_open_up_price": None,
                                "signal_current_up_price": None,
                                "signal_threshold": 0.2,
                                "signal_delta": -0.4,
                                "signal_locked": False,
                                "signal_reason": None,
                                "queued_at": "2026-04-11T06:05:05+00:00"
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    class _MultiStrategySettlementClient(_NoMarketClient):
        def get_event_by_slug(self, slug: str):
            if slug == "btc-updown-5m-paper-s1":
                return {"eventMetadata": {"priceToBeat": None, "finalPrice": None}}
            if slug == "btc-updown-5m-paper-s6":
                return {"eventMetadata": {"priceToBeat": 100.0, "finalPrice": 80.0}}
            raise AssertionError(slug)

    result = run_paper_trading(
        AppConfig(strategy_id=2, paper_strategy_ids=[1, 6], poll_interval_seconds=1),
        client=_MultiStrategySettlementClient(),
        state_path=state_path,
        log_path=log_path,
        stop_event=stop_event,
    )

    state = load_session_state(state_path, effective_paper_strategy_ids=[1, 6])
    assert result["status"] == "stopped"
    assert len(state.paper_strategies[1].pending_paper_trades) == 1
    assert state.paper_strategies[6].pending_paper_trades == []
    rows = log_path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2
    assert "btc-updown-5m-paper-s6" in rows[1]
    assert ",6,OPEN,btc-updown-5m-paper-s6," in rows[1]


def test_run_paper_trading_persists_active_challenger_pending_state(tmp_path, monkeypatch):
    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    stop_event = threading.Event()

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "optimizer_state.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "last_run_at": "2026-04-16T10:00:00+00:00",
                "champion_id": "strategy-2",
                "active_challengers": [
                    {
                        "candidate_id": "challenger-s2-a",
                        "base_strategy_id": 2,
                        "params": {
                            "TARGET_PROFIT": 1.0,
                            "MAX_PRICE_THRESHOLD": 0.65,
                        },
                        "validation_score": 0.9,
                    }
                ],
                "promotable_candidates": [],
            }
        ),
        encoding="utf-8",
    )

    def fake_sleep(_seconds):
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)

    try:
        result = run_paper_trading(
            AppConfig(strategy_id=2, paper_strategy_ids=[2], poll_interval_seconds=1),
            client=_ImmediatePaperRoundClient(),
            state_path=tmp_path / "state.json",
            log_path=tmp_path / "paper.csv",
            stop_event=stop_event,
        )

        optimizer_state = json.loads((logs_dir / "optimizer_state.json").read_text(encoding="utf-8"))
        challenger = optimizer_state["active_challengers"][0]

        assert result["status"] == "stopped"
        assert challenger["paper_state"]["experiment_id"] == "challenger-s2-a"
        assert len(challenger["paper_state"]["pending_paper_trades"]) == 1
        assert challenger["paper_state"]["pending_paper_trades"][0]["experiment_id"] == "challenger-s2-a"
    finally:
        os.chdir(old_cwd)


def test_candidate_cfg_with_params_applies_strategy7_optimizer_values():
    base_cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_max_entry_price=0.55,
    )

    candidate_cfg = _candidate_cfg_with_params(
        base_cfg,
        7,
        {
            "TARGET_PROFIT": 1.2,
            "STRATEGY7_OFI_THRESHOLD": 0.75,
            "STRATEGY7_MOMENTUM_THRESHOLD": 0.03,
            "STRATEGY7_MAX_ENTRY_PRICE": 0.53,
        },
    )

    assert candidate_cfg.strategy_id == 7
    assert candidate_cfg.target_profit == pytest.approx(1.2)
    assert candidate_cfg.strategy7_ofi_threshold == pytest.approx(0.75)
    assert candidate_cfg.strategy7_momentum_threshold == pytest.approx(0.03)
    assert candidate_cfg.strategy7_max_entry_price == pytest.approx(0.53)
