from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest
import requests

from config import AppConfig
from models import MarketQuote, MarketWindow, SessionState, TradePlan, TradeRecord
from runtime_control import RuntimeControl
from trader import (
    SideDecision,
    _resolve_side_from_strategy,
    _update_max_stake_skip_streak,
    append_trade_log,
    load_session_state,
    place_live_order,
    run_live_trading,
    run_paper_trading,
    _create_live_clob_client,
    validate_live_runtime_config,
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
    assert captured['creds'] == {
        'api_key': 'builder-key',
        'secret': 'builder-secret',
        'passphrase': 'builder-passphrase',
    }
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

def test_validate_live_runtime_config_requires_private_key_and_funder():
    cfg = AppConfig(trade_mode='live', live_trading_enabled=True)

    with pytest.raises(RuntimeError, match='private key'):
        validate_live_runtime_config(cfg)


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
    assert side_first.side == "UP"
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



