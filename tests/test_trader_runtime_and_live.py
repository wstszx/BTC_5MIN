from __future__ import annotations

import csv
import json
import os
import threading
from pathlib import Path
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest
import requests
import trader
import utils

from binance_signal import BinanceDepth5SignalService
from clob_adapter import create_live_clob_client, read_available_live_balance
from config import AppConfig
from models import LiveStrategyState, MarketQuote, MarketWindow, PaperStrategyState, PendingPaperTrade, SessionState, TradePlan, TradeRecord
from paper_report import summarize_paper_trades
from runtime_control import RuntimeControl
from utils import _sleep_if_not_stopped
from runtime_config import validate_live_runtime_config
from trader import (
    SideDecision,
    _resolve_side_from_strategy,
    _sync_live_state_ledger_from_trade_log,
    _update_max_stake_skip_streak,
    append_trade_log,
    load_session_state,
    save_session_state,
    place_live_order,
    run_live_trading,
    run_paper_trading,
    _candidate_cfg_with_params,
    _paper_experiment_id,
)


@dataclass(frozen=True)
class _FakeApiCreds:
    api_key: str
    api_secret: str
    api_passphrase: str


def _install_fake_clob_v2(monkeypatch, fake_client_cls):
    import sys
    import types

    fake_pkg = types.ModuleType('py_clob_client_v2')
    fake_pkg.ClobClient = fake_client_cls
    fake_pkg.ApiCreds = _FakeApiCreds

    fake_client_module = types.ModuleType('py_clob_client_v2.client')
    fake_client_module.ClobClient = fake_client_cls

    fake_types_module = types.ModuleType('py_clob_client_v2.clob_types')
    fake_types_module.ApiCreds = _FakeApiCreds

    monkeypatch.setitem(sys.modules, 'py_clob_client_v2', fake_pkg)
    monkeypatch.setitem(sys.modules, 'py_clob_client_v2.client', fake_client_module)
    monkeypatch.setitem(sys.modules, 'py_clob_client_v2.clob_types', fake_types_module)


def test_requirements_use_py_clob_client_v2_dependency():
    requirements = Path('requirements.txt').read_text(encoding='utf-8').splitlines()

    assert any(line.startswith('py-clob-client-v2') for line in requirements)
    assert 'py-clob-client' not in requirements


def test_sync_live_state_ledger_from_trade_log_uses_reconciled_loss(tmp_path: Path):
    live_csv = tmp_path / "live_orders.csv"
    with live_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "strategy",
                "end_time",
                "side",
                "order_cost",
                "expected_profit",
                "result",
                "skip_reason",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "strategy": "7",
                "end_time": "2026-05-02T10:05:00+00:00",
                "side": "UP",
                "order_cost": "1.0",
                "expected_profit": "2.0",
                "result": "UP",
                "skip_reason": "",
            }
        )
        writer.writerow(
            {
                "strategy": "7",
                "end_time": "2026-05-03T00:35:00+00:00",
                "side": "DOWN",
                "order_cost": "1.1999994",
                "expected_profit": "1.1529406",
                "result": "UP",
                "skip_reason": "",
            }
        )
        writer.writerow(
            {
                "strategy": "7",
                "end_time": "2026-05-03T00:45:00+00:00",
                "side": "SKIP",
                "order_cost": "0.0",
                "expected_profit": "0.0",
                "result": "",
                "skip_reason": "strategy7_price_too_high",
            }
        )

    state = SessionState(
        cash_pnl=10.75171118,
        daily_realized_pnl=1.1529406,
        recovery_loss=0.0,
        consecutive_losses=0,
        live_strategies={
            7: LiveStrategyState(
                cash_pnl=10.75171118,
                daily_realized_pnl=1.1529406,
                recovery_loss=0.0,
                consecutive_losses=0,
                pending_live_slug="btc-updown-5m-current",
            )
        },
    )

    _sync_live_state_ledger_from_trade_log(state, live_csv=live_csv, active_strategy_ids=[7])

    live_state = state.live_strategies[7]
    assert live_state.cash_pnl == pytest.approx(0.8000006)
    assert live_state.daily_realized_pnl == pytest.approx(-1.1999994)
    assert live_state.recovery_loss == 0.0
    assert live_state.consecutive_losses == 1
    assert live_state.pending_live_slug == "btc-updown-5m-current"
    assert state.cash_pnl == pytest.approx(0.8000006)


def test_sync_live_state_ledger_from_trade_log_does_not_restore_legacy_flat_sizing_state(tmp_path: Path):
    live_csv = tmp_path / "live_orders.csv"
    with live_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "strategy",
                "end_time",
                "side",
                "order_cost",
                "expected_profit",
                "result",
                "skip_reason",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "strategy": "7",
                "end_time": "2026-05-03T00:35:00+00:00",
                "side": "DOWN",
                "order_cost": "1.1999994",
                "expected_profit": "1.1529406",
                "result": "UP",
                "skip_reason": "",
            }
        )

    cfg = AppConfig(strategy_id=7, live_strategy_ids=[7])
    state = SessionState(
        cash_pnl=0.0,
        daily_realized_pnl=0.0,
        recovery_loss=0.0,
        consecutive_losses=0,
        live_strategies={
            7: LiveStrategyState(
                cash_pnl=0.0,
                daily_realized_pnl=0.0,
                recovery_loss=0.0,
                consecutive_losses=0,
            )
        },
    )

    _sync_live_state_ledger_from_trade_log(state, live_csv=live_csv, active_strategy_ids=[7], cfg=cfg)

    live_state = state.live_strategies[7]
    assert live_state.cash_pnl == pytest.approx(-1.1999994)
    assert live_state.daily_realized_pnl == pytest.approx(-1.1999994)
    assert live_state.recovery_loss == 0.0
    assert live_state.consecutive_losses == 1
    assert state.recovery_loss == 0.0
    assert state.consecutive_losses == 1


def test_sync_live_state_ledger_from_trade_log_respects_stop_loss_reset(tmp_path: Path):
    live_csv = tmp_path / "live_orders.csv"
    with live_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "strategy",
                "end_time",
                "side",
                "order_cost",
                "expected_profit",
                "result",
                "skip_reason",
                "stop_loss_triggered",
            ],
        )
        writer.writeheader()
        for order_cost in ("1.2", "2.8"):
            writer.writerow(
                {
                    "strategy": "7",
                    "end_time": "2026-05-03T04:00:00+00:00",
                    "side": "UP",
                    "order_cost": order_cost,
                    "expected_profit": "1.0",
                    "result": "DOWN",
                    "skip_reason": "",
                    "stop_loss_triggered": "False",
                }
            )
        writer.writerow(
            {
                "strategy": "7",
                "end_time": "2026-05-03T04:05:00+00:00",
                "side": "UP",
                "order_cost": "0.0",
                "expected_profit": "0.0",
                "result": "",
                "skip_reason": "max_consecutive_losses_reached",
                "stop_loss_triggered": "True",
            }
        )

    state = SessionState(
        recovery_loss=4.0,
        consecutive_losses=2,
        live_strategies={
            7: LiveStrategyState(
                recovery_loss=4.0,
                consecutive_losses=2,
            )
        },
    )

    _sync_live_state_ledger_from_trade_log(state, live_csv=live_csv, active_strategy_ids=[7])

    live_state = state.live_strategies[7]
    assert live_state.recovery_loss == 0.0
    assert live_state.consecutive_losses == 0
    assert state.recovery_loss == 0.0
    assert state.consecutive_losses == 0


def test_requirements_pin_runtime_dependencies():
    requirements = [
        line.strip()
        for line in Path('requirements.txt').read_text(encoding='utf-8').splitlines()
        if line.strip() and not line.strip().startswith('#')
    ]

    for package_name in (
        'requests',
        'pandas',
        'python-dateutil',
        'pytest',
        'websocket-client',
        'tenacity',
        'py-clob-client-v2',
    ):
        matching = [line for line in requirements if line.startswith(package_name)]
        assert matching, f'missing {package_name}'
        assert any(any(operator in line for operator in ('>=', '==', '~=')) for line in matching)
        assert any('<' in line for line in matching)


def test_describe_ws_runtime_reports_current_error_not_recovered_last_error():
    class _RecoveredWsClient:
        def get_ws_runtime_stats(self):
            return {
                "ws_enabled": True,
                "ws_available": True,
                "ws_connected": True,
                "ws_reconnect_count": 5,
                "ws_invalid_operation_count": 0,
                "ws_connect_attempts": 6,
                "ws_subscribed_asset_count": 2,
                "ws_cached_asset_count": 88,
                "ws_last_message_age_seconds": 1.2,
                "ws_current_error": None,
                "ws_last_error": "Connection to remote host was lost.",
            }

    text = trader._describe_ws_runtime(_RecoveredWsClient())

    assert "current_error=None" in text
    assert "last_error=Connection to remote host was lost." not in text


def test_sleep_if_not_stopped_waits_on_stop_event(monkeypatch):
    class FakeStopEvent:
        def __init__(self) -> None:
            self.wait_seconds: float | None = None
            self._stopped = False

        def is_set(self) -> bool:
            return self._stopped

        def wait(self, seconds: float) -> bool:
            self.wait_seconds = seconds
            self._stopped = True
            return True

    def fail_sleep(seconds):
        if seconds != 0:
            raise AssertionError(f'long time.sleep should not be used with stop_event; got {seconds}')

    stop_event = FakeStopEvent()
    monkeypatch.setattr(trader.time, 'sleep', fail_sleep)

    assert _sleep_if_not_stopped(stop_event, 30.0) is False
    assert stop_event.wait_seconds == 30.0


def test_save_session_state_uses_atomic_replace(monkeypatch, tmp_path: Path):
    state_path = tmp_path / 'session_state.json'
    replace_calls: list[tuple[Path, Path]] = []
    original_replace = Path.replace

    def spy_replace(self: Path, target: Path):
        replace_calls.append((self, Path(target)))
        return original_replace(self, target)

    monkeypatch.setattr(Path, 'replace', spy_replace)

    save_session_state(state_path, SessionState(cash_pnl=1.25))

    assert replace_calls
    assert replace_calls[-1][1] == state_path
    assert json.loads(state_path.read_text(encoding='utf-8'))['cash_pnl'] == 1.25


def test_save_session_state_retries_transient_windows_replace_denial(monkeypatch, tmp_path: Path):
    state_path = tmp_path / 'session_state.json'
    attempts = 0
    original_replace = Path.replace

    def flaky_replace(self: Path, target: Path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(5, 'Access is denied')
        return original_replace(self, target)

    monkeypatch.setattr(Path, 'replace', flaky_replace)

    save_session_state(state_path, SessionState(cash_pnl=2.5))

    assert attempts == 2
    assert json.loads(state_path.read_text(encoding='utf-8'))['cash_pnl'] == 2.5


def test_save_session_state_replaces_readonly_target_on_windows(tmp_path: Path):
    state_path = tmp_path / 'session_state.json'
    state_path.write_text('{"cash_pnl": 0.0}', encoding='utf-8')
    state_path.chmod(0o444)

    try:
        save_session_state(state_path, SessionState(cash_pnl=3.75))
        assert json.loads(state_path.read_text(encoding='utf-8'))['cash_pnl'] == 3.75
    finally:
        state_path.chmod(0o666)


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
            start_time=now - timedelta(seconds=9),
            end_time=now + timedelta(minutes=4, seconds=51),
            up_token_id="up-token",
            down_token_id="down-token",
        )
        return window, None

    def get_market_by_slug(self, slug: str):
        return {
            "slug": slug,
            "outcomes": '["Up", "Down"]',
            "outcomePrices": '["0.54", "0.46"]',
            "clobTokenIds": '["up-token", "down-token"]',
            "bestBid": "0.50",
            "bestAsk": "0.52",
            "acceptingOrders": True,
        }

    def quote_from_market(self, _market):
        return MarketQuote(
            slug="btc-updown-5m-test",
            up_price=0.54,
            down_price=0.49,
            up_best_ask=0.52,
            fetched_at=datetime.now(timezone.utc),
        )


class _NoTradeLiveMarketClient(_LiveMarketClient):
    def find_current_and_next_rounds(self, *, now):
        return None, None


class _TransientLiveMarketDiscoveryClient(_LiveMarketClient):
    def __init__(self):
        self.calls = 0

    def find_current_and_next_rounds(self, *, now):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError(
                "Unable to fetch https://gamma-api.polymarket.com/events after 4 attempts: "
                "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"
            )
        return None, None


class _TransientLiveMarketQuoteClient(_LiveMarketClient):
    def __init__(self):
        self.market_calls = 0

    def get_market_by_slug(self, slug: str):
        self.market_calls += 1
        if self.market_calls == 1:
            raise RuntimeError(
                "Unable to fetch https://gamma-api.polymarket.com/markets/slug/btc-updown-5m-test after 4 attempts: "
                "('Connection aborted.', ConnectionResetError(10054, '远程主机强迫关闭了一个现有的连接。', None, 10054, None))"
            )
        return super().get_market_by_slug(slug)


class _MissedEntryNoNextLiveClient(_LiveMarketClient):
    def find_current_and_next_rounds(self, *, now):
        window = MarketWindow(
            event_id="evt-missed",
            market_id="mkt-missed",
            slug="btc-updown-5m-missed",
            title="BTC 5m Missed",
            start_time=now - timedelta(seconds=30),
            end_time=now + timedelta(minutes=4, seconds=30),
            up_token_id="up-token",
            down_token_id="down-token",
        )
        return window, None


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

    _install_fake_clob_v2(monkeypatch, _FakeClobClient)

    client = create_live_clob_client(
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
    assert captured['creds'] == _FakeApiCreds(
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

        def create_or_derive_api_key(self):
            captured['derived'] = True
            return {'api_key': 'derived-key'}

        def set_api_creds(self, creds):
            captured['creds'] = creds

    _install_fake_clob_v2(monkeypatch, _FakeClobClient)

    client = create_live_clob_client(
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


def test_create_live_clob_client_falls_back_when_explicit_api_credentials_are_invalid(monkeypatch):
    captured: dict[str, object] = {"creds": []}

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

        def create_or_derive_api_key(self):
            captured['derived'] = True
            return {'api_key': 'derived-key'}

    _install_fake_clob_v2(monkeypatch, _FakeClobClient)

    client = create_live_clob_client(
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
        _FakeApiCreds(
            api_key='builder-key',
            api_secret='builder-secret',
            api_passphrase='builder-passphrase',
        ),
        {'api_key': 'derived-key'},
    ]


def test_create_live_clob_client_prefers_deriving_api_credentials_after_explicit_rejection(monkeypatch):
    captured: dict[str, object] = {"creds": [], "calls": []}

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

        def derive_api_key(self):
            captured['calls'].append('derive')
            return {'api_key': 'derived-key'}

        def create_api_key(self):
            captured['calls'].append('create')
            raise Exception("Could not create api key")

    _install_fake_clob_v2(monkeypatch, _FakeClobClient)

    client = create_live_clob_client(
        AppConfig(
            trade_mode='live',
            live_trading_enabled=True,
            live_private_key='pk-live',
            live_funder='0xfunder',
            live_api_key='stale-key',
            live_api_secret='stale-secret',
            live_api_passphrase='stale-passphrase',
        )
    )

    assert client is not None
    assert captured['validated_after_fallback'] is True
    assert captured['calls'] == ['derive']
    assert captured['creds'] == [
        _FakeApiCreds(
            api_key='stale-key',
            api_secret='stale-secret',
            api_passphrase='stale-passphrase',
        ),
        {'api_key': 'derived-key'},
    ]


def test_submit_live_strategy_order_prefers_v2_create_and_post_market_order():
    captured: dict[str, object] = {}

    class _CreateAndPostClient:
        def create_and_post_market_order(self, *, order_args, order_type):
            captured['order_args'] = order_args
            captured['order_type'] = order_type
            return {'success': True, 'orderID': 'oid-v2'}

        def create_market_order(self, _order_args):
            raise AssertionError('legacy create/post path should not be used when v2 helper exists')

        def post_order(self, _order, _order_type):
            raise AssertionError('legacy create/post path should not be used when v2 helper exists')

    order_id, response = trader._submit_live_strategy_order(
        cfg=AppConfig(live_trading_enabled=True),
        clob_client=_CreateAndPostClient(),
        token_id='up-token',
        plan=TradePlan(True, 'UP', price=0.55, order_size=2.0, order_cost=1.1, expected_profit=0.9),
    )

    assert order_id == 'oid-v2'
    assert response == {'success': True, 'orderID': 'oid-v2'}
    assert captured['order_type'] == 'FOK'
    assert captured['order_args'].token_id == 'up-token'
    assert not hasattr(captured['order_args'], 'fee_rate_bps')


def test_settle_pending_paper_trade_does_not_use_cached_ws_market_resolution():
    class _WsResolvedClient:
        def get_event_by_slug(self, slug: str):
            return {
                'slug': slug,
                'eventMetadata': {},
                'markets': [
                    {
                        'outcomes': '["Up", "Down"]',
                        'outcomePrices': '["0.5", "0.5"]',
                        'clobTokenIds': '["up-token", "down-token"]',
                        'closed': False,
                    }
                ],
            }

        def get_ws_market_resolution(self, market):
            return {'winning_outcome': 'Down', 'winning_asset_id': 'down-token'}

    state = SessionState()
    item = PendingPaperTrade(
        round_index=0,
        event_slug='btc-updown-5m-ws-resolved',
        start_time='2026-04-26T00:00:00+00:00',
        end_time='2026-04-26T00:05:00+00:00',
        side='DOWN',
        price=0.45,
        order_size=2.0,
        order_cost=0.9,
        expected_profit=1.1,
        strategy=2,
        entry_timing='OPEN',
    )

    with pytest.raises(RuntimeError, match="is not resolved yet"):
        trader._settle_pending_paper_trade(
            client=_WsResolvedClient(),
            state=state,
            item=item,
        )

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


def test_run_live_trading_stops_cleanly_when_config_provider_disables_live(tmp_path):
    runtime_control = RuntimeControl(initial_mode="live")

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
        ),
        market_client=_NoTradeLiveMarketClient(),
        clob_client=_StubClobClient(),
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
        config_provider=lambda: AppConfig(trade_mode="live", live_trading_enabled=False),
        runtime_control=runtime_control,
    )

    snapshot = runtime_control.snapshot()
    assert result == {"status": "stopped", "skip_reason": "live_trading_disabled"}
    assert snapshot.safe_to_switch is True
    assert snapshot.pending_live_order is False


def test_run_live_trading_retries_transient_clob_client_timeout(tmp_path, monkeypatch):
    attempts = {"create_client": 0, "sleeps": []}

    def fake_create_client(_cfg):
        attempts["create_client"] += 1
        if attempts["create_client"] == 1:
            raise Exception(
                "[py_clob_client_v2] request error: The read operation timed out "
                "PolyApiException[status_code=None, error_message=Request exception!]"
            )
        return _StubClobClient()

    def fake_sleep_if_not_stopped(_stop_event, seconds):
        attempts["sleeps"].append(seconds)
        return len(attempts["sleeps"]) == 1

    monkeypatch.setattr("trader._create_live_clob_client", fake_create_client)
    monkeypatch.setattr("trader._sleep_if_not_stopped", fake_sleep_if_not_stopped)

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            poll_interval_seconds=1,
            runtime_error_backoff_base_seconds=1,
        ),
        market_client=_NoTradeLiveMarketClient(),
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
    )

    assert result["status"] == "stopped"
    assert attempts["create_client"] == 2
    assert attempts["sleeps"] == [1, 1]


def test_run_live_trading_retries_transient_market_discovery_ssl_error(tmp_path, monkeypatch):
    attempts = {"sleeps": []}
    market_client = _TransientLiveMarketDiscoveryClient()

    def fake_sleep_if_not_stopped(_stop_event, seconds):
        attempts["sleeps"].append(seconds)
        return len(attempts["sleeps"]) == 1

    monkeypatch.setattr("trader._sleep_if_not_stopped", fake_sleep_if_not_stopped)

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            poll_interval_seconds=1,
            runtime_error_backoff_base_seconds=1,
        ),
        market_client=market_client,
        clob_client=_StubClobClient(),
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
    )

    assert result["status"] == "stopped"
    assert market_client.calls == 2
    assert attempts["sleeps"] == [1, 1]


def test_run_live_trading_retries_transient_market_quote_reset_error(tmp_path, monkeypatch):
    attempts = {"sleeps": []}
    market_client = _TransientLiveMarketQuoteClient()

    def fake_sleep_if_not_stopped(_stop_event, seconds):
        attempts["sleeps"].append(seconds)
        return len(attempts["sleeps"]) == 1

    monkeypatch.setattr("trader._sleep_if_not_stopped", fake_sleep_if_not_stopped)

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            poll_interval_seconds=1,
            runtime_error_backoff_base_seconds=1,
        ),
        market_client=market_client,
        clob_client=_StubClobClient(),
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
    )

    assert result["status"] == "stopped"
    assert market_client.market_calls == 2
    assert attempts["sleeps"] == [1, 1]


def test_run_live_trading_reuses_created_clob_client_across_poll_loops(tmp_path, monkeypatch):
    attempts = {"create_client": 0, "sleeps": 0}

    def fake_create_client(_cfg):
        attempts["create_client"] += 1
        return _StubClobClient()

    def fake_sleep_if_not_stopped(_stop_event, _seconds):
        attempts["sleeps"] += 1
        return attempts["sleeps"] == 1

    monkeypatch.setattr("trader._create_live_clob_client", fake_create_client)
    monkeypatch.setattr("trader._sleep_if_not_stopped", fake_sleep_if_not_stopped)

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            poll_interval_seconds=1,
        ),
        market_client=_NoTradeLiveMarketClient(),
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
    )

    assert result["status"] == "stopped"
    assert attempts["create_client"] == 1


def test_run_live_trading_treats_transient_balance_ssl_error_as_unavailable_budget(tmp_path, monkeypatch):
    stop_event = threading.Event()

    class _BalanceSslErrorClobClient(_StubClobClient):
        def get_balance(self):
            raise Exception(
                "[py_clob_client_v2] request error: [SSL: UNEXPECTED_EOF_WHILE_READING] "
                "EOF occurred in violation of protocol (_ssl.c:1081) "
                "PolyApiException[status_code=None, error_message=Request exception!]"
            )

    def fake_sleep(_seconds):
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
            price=0.55,
            order_size=2.0,
            order_cost=1.0,
            expected_profit=1.0,
        ),
    )

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            poll_interval_seconds=1,
        ),
        market_client=_LiveMarketClient(),
        clob_client=_BalanceSslErrorClobClient(),
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
        stop_event=stop_event,
    )

    rows = list(csv.DictReader((tmp_path / "live_orders.csv").open(newline="", encoding="utf-8")))
    assert result["status"] == "stopped"
    assert len(rows) == 1
    assert rows[0]["skip_reason"] == "live_wallet_balance_unavailable"
    assert "UNEXPECTED_EOF_WHILE_READING" in rows[0]["balance_error"]


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


def test_run_live_trading_books_provisional_loss_when_final_price_is_missing_even_with_consensus(tmp_path, monkeypatch):
    state_path = tmp_path / "live_state.json"
    state_path.write_text(
        json.dumps(
            {
                "live_strategies": {
                    "7": {
                        "round_index": 4,
                        "cash_pnl": 0.0,
                        "recovery_loss": 0.0,
                        "consecutive_losses": 0,
                        "pending_live_slug": "btc-updown-5m-prev",
                        "pending_live_side": "UP",
                        "pending_live_price": 0.5,
                        "pending_live_order_size": 2.0,
                        "pending_live_order_cost": 1.0,
                        "pending_live_expected_profit": 1.0,
                        "pending_live_order_id": "oid-prev",
                        "pending_live_end_time": "2026-05-05T00:05:00+00:00",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("trader._resolve_side_from_strategy", lambda **kwargs: SideDecision(side=None))
    monkeypatch.setattr("trader._sleep_if_not_stopped", lambda _stop_event, _seconds: False)

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=7,
            live_strategy_ids=[7],
        ),
        market_client=_NoFinalPriceConsensusLiveClient(),
        clob_client=_StubClobClient(
            order_payloads={
                "oid-prev": {
                    "status": "matched",
                    "size_matched": "2.0",
                    "price": "0.50",
                    "associate_trades": ["trade-prev"],
                }
            },
            trade_payloads={
                "trade-prev": [
                    {
                        "id": "trade-prev",
                        "taker_order_id": "oid-prev",
                        "size": "2.0",
                        "price": "0.50",
                        "status": "CONFIRMED",
                    }
                ]
            },
        ),
        state_path=state_path,
        log_path=tmp_path / "live_orders.csv",
        stop_event=threading.Event(),
        stop_when_safe=lambda: True,
    )

    reloaded = load_session_state(state_path, effective_live_strategy_ids=[7])
    rows = list(csv.DictReader((tmp_path / "live_orders.csv").open(newline="", encoding="utf-8")))
    assert result["status"] == "stopped"
    assert reloaded.live_strategies[7].pending_live_slug is None
    assert reloaded.live_strategies[7].cash_pnl == pytest.approx(-1.035)
    assert reloaded.live_strategies[7].recovery_loss == 0.0
    assert reloaded.live_strategies[7].consecutive_losses == 1
    assert rows[0]["result"] == "PROVISIONAL_LOSS"
    assert rows[0]["trade_pnl"] == "-1.035"


def test_run_live_trading_books_provisional_loss_when_final_price_is_missing(tmp_path, monkeypatch):
    state_path = tmp_path / "live_state.json"
    state_path.write_text(
        json.dumps(
            {
                "live_strategies": {
                    "7": {
                        "round_index": 4,
                        "cash_pnl": 0.0,
                        "recovery_loss": 0.0,
                        "consecutive_losses": 0,
                        "pending_live_slug": "btc-updown-5m-prev",
                        "pending_live_side": "UP",
                        "pending_live_price": 0.5,
                        "pending_live_order_size": 2.0,
                        "pending_live_order_cost": 1.0,
                        "pending_live_expected_profit": 1.0,
                        "pending_live_order_id": "oid-prev",
                        "pending_live_end_time": "2026-05-05T00:05:00+00:00",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    captured = {"recovery_loss": None, "consecutive_losses": None}
    stop_event = threading.Event()

    def capture_plan_state(*args, **kwargs):
        plan_state = kwargs["state"]
        captured["recovery_loss"] = plan_state.recovery_loss
        captured["consecutive_losses"] = plan_state.consecutive_losses
        stop_event.set()
        return TradePlan(False, kwargs["side"], price=kwargs["price"], skip_reason="captured")

    monkeypatch.setattr("trader._resolve_side_from_strategy", lambda **kwargs: SideDecision(side="UP"))
    monkeypatch.setattr("trader.build_trade_plan", capture_plan_state)

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=7,
            live_strategy_ids=[7],
        ),
        market_client=_NoFinalPriceConsensusLiveClient(),
        clob_client=_StubClobClient(
            order_payloads={
                "oid-prev": {
                    "status": "matched",
                    "size_matched": "2.0",
                    "price": "0.50",
                    "associate_trades": ["trade-prev"],
                }
            },
            trade_payloads={
                "trade-prev": [
                    {
                        "id": "trade-prev",
                        "taker_order_id": "oid-prev",
                        "size": "2.0",
                        "price": "0.50",
                        "status": "CONFIRMED",
                    }
                ]
            },
        ),
        state_path=state_path,
        log_path=tmp_path / "live_orders.csv",
        stop_event=stop_event,
    )

    reloaded = load_session_state(state_path, effective_live_strategy_ids=[7])
    rows = list(csv.DictReader((tmp_path / "live_orders.csv").open(newline="", encoding="utf-8")))
    assert result["status"] == "stopped"
    assert captured == {"recovery_loss": 0.0, "consecutive_losses": 1}
    assert reloaded.live_strategies[7].pending_live_slug is None
    assert reloaded.live_strategies[7].cash_pnl == pytest.approx(-1.035)
    assert reloaded.live_strategies[7].recovery_loss == 0.0
    assert reloaded.live_strategies[7].consecutive_losses == 1
    assert rows[0]["event_slug"] == "btc-updown-5m-prev"
    assert rows[0]["result"] == "PROVISIONAL_LOSS"
    assert rows[0]["trade_pnl"] == "-1.035"
    assert rows[1]["event_slug"] == "btc-updown-5m-test"
    assert rows[1]["skip_reason"] == "captured"


def test_run_live_trading_does_not_evaluate_new_orders_while_any_live_order_is_pending(tmp_path, monkeypatch):
    state_path = tmp_path / "live_state.json"
    state_path.write_text(
        json.dumps(
            {
                "live_strategies": {
                    "7": {
                        "round_index": 4,
                        "cash_pnl": 0.0,
                        "recovery_loss": 0.0,
                        "consecutive_losses": 0,
                        "pending_live_slug": "btc-updown-5m-prev",
                        "pending_live_side": "UP",
                        "pending_live_price": 0.5,
                        "pending_live_order_size": 2.0,
                        "pending_live_order_cost": 1.0,
                        "pending_live_expected_profit": 1.0,
                        "pending_live_order_id": "oid-prev",
                        "pending_live_end_time": "2099-01-01T00:00:00+00:00",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    evaluation_calls = {"side": 0, "plan": 0}

    def fail_if_strategy_evaluated(**kwargs):
        evaluation_calls["side"] += 1
        return SideDecision(side="UP")

    def fail_if_plan_built(*args, **kwargs):
        evaluation_calls["plan"] += 1
        return TradePlan(
            True,
            "UP",
            price=0.55,
            order_size=2.0,
            order_cost=1.0,
            expected_profit=1.0,
        )

    monkeypatch.setattr("trader._resolve_side_from_strategy", fail_if_strategy_evaluated)
    monkeypatch.setattr("trader.build_trade_plan", fail_if_plan_built)

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=3,
            live_strategy_ids=[3, 7],
        ),
        market_client=_LiveMarketClient(),
        clob_client=_StubClobClient(),
        state_path=state_path,
        log_path=tmp_path / "live_orders.csv",
        stop_when_safe=lambda: True,
    )

    assert result["status"] == "pending_settlement"
    assert evaluation_calls == {"side": 0, "plan": 0}


def test_run_live_strategy7_flat_continues_with_unresolved_pending_trade(tmp_path, monkeypatch):
    stop_event = threading.Event()
    stub_clob = _StubClobClient(balance_payload={"available": 3.0})

    def fake_sleep(_seconds):
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)
    monkeypatch.setattr("trader._resolve_side_from_strategy", lambda **kwargs: SideDecision(side="UP"))
    monkeypatch.setattr(
        "trader.build_trade_plan",
        lambda *args, **kwargs: TradePlan(
            True,
            "UP",
            price=0.50,
            order_size=2.0,
            order_cost=1.0,
            expected_profit=1.0,
            tracks_recovery_loss=False,
        ),
    )

    state_path = tmp_path / "live_state.json"
    state_path.write_text(
        json.dumps(
            {
                "live_strategies": {
                    "7": {
                        "round_index": 4,
                        "cash_pnl": 0.0,
                        "recovery_loss": 0.0,
                        "consecutive_losses": 0,
                        "pending_live_slug": "btc-updown-5m-prev",
                        "pending_live_side": "UP",
                        "pending_live_price": 0.5,
                        "pending_live_order_size": 2.0,
                        "pending_live_order_cost": 1.0,
                        "pending_live_expected_profit": 1.0,
                        "pending_live_order_id": "oid-prev",
                        "pending_live_end_time": "2099-01-01T00:00:00+00:00",
                        "pending_live_tracks_recovery_loss": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=7,
            live_strategy_ids=[7],
            poll_interval_seconds=1,
        ),
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=state_path,
        log_path=tmp_path / "live_orders.csv",
        stop_event=stop_event,
    )

    reloaded = load_session_state(state_path, effective_live_strategy_ids=[7])
    strategy_state = reloaded.live_strategies[7]
    assert result["status"] == "stopped"
    assert len(stub_clob.created_orders) == 1
    assert strategy_state.pending_live_slug == "btc-updown-5m-test"
    assert [item.event_slug for item in strategy_state.pending_live_trades] == [
        "btc-updown-5m-prev",
        "btc-updown-5m-test",
    ]
    assert strategy_state.pending_live_trades[0].order_id == "oid-prev"
    assert strategy_state.pending_live_trades[1].tracks_recovery_loss is False


def test_run_live_trading_keeps_pending_and_blocks_new_orders_when_settlement_errors(tmp_path, monkeypatch):
    state_path = tmp_path / "live_state.json"
    state_path.write_text(
        json.dumps(
            {
                "live_strategies": {
                    "7": {
                        "round_index": 4,
                        "cash_pnl": 0.0,
                        "recovery_loss": 0.0,
                        "consecutive_losses": 0,
                        "pending_live_slug": "btc-updown-5m-prev",
                        "pending_live_side": "UP",
                        "pending_live_price": 0.5,
                        "pending_live_order_size": 2.0,
                        "pending_live_order_cost": 1.0,
                        "pending_live_expected_profit": 1.0,
                        "pending_live_order_id": "oid-prev",
                        "pending_live_end_time": "2026-04-02T00:00:00+00:00",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    evaluation_calls = {"side": 0, "plan": 0}

    class _SettlementErrorLiveClient(_LiveMarketClient):
        def get_event_by_slug(self, slug: str):
            raise ValueError(f"broken settlement payload for {slug}")

    def fail_if_strategy_evaluated(**kwargs):
        evaluation_calls["side"] += 1
        return SideDecision(side="UP")

    def fail_if_plan_built(*args, **kwargs):
        evaluation_calls["plan"] += 1
        return TradePlan(
            True,
            "UP",
            price=0.55,
            order_size=2.0,
            order_cost=1.0,
            expected_profit=1.0,
        )

    monkeypatch.setattr("trader._resolve_side_from_strategy", fail_if_strategy_evaluated)
    monkeypatch.setattr("trader.build_trade_plan", fail_if_plan_built)

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=3,
            live_strategy_ids=[3, 7],
        ),
        market_client=_SettlementErrorLiveClient(),
        clob_client=_StubClobClient(),
        state_path=state_path,
        log_path=tmp_path / "live_orders.csv",
        stop_when_safe=lambda: True,
    )

    reloaded = load_session_state(state_path, effective_live_strategy_ids=[3, 7])
    strategy_result = next(item for item in result["strategies"] if item["strategy_id"] == 7)

    assert result["status"] == "pending_settlement"
    assert strategy_result["status"] == "pending_settlement"
    assert strategy_result["skip_reason"] == "settlement_error"
    assert "broken settlement payload" in strategy_result["error"]
    assert evaluation_calls == {"side": 0, "plan": 0}
    assert reloaded.live_strategies[7].pending_live_slug == "btc-updown-5m-prev"
    assert reloaded.live_strategies[7].pending_live_order_id == "oid-prev"


def test_run_live_trading_waits_for_confirmed_clob_trade_before_new_orders(tmp_path, monkeypatch):
    state_path = tmp_path / "live_state.json"
    state_path.write_text(
        json.dumps(
            {
                "live_strategies": {
                    "7": {
                        "round_index": 4,
                        "cash_pnl": 0.0,
                        "recovery_loss": 0.0,
                        "consecutive_losses": 0,
                        "pending_live_slug": "btc-updown-5m-prev",
                        "pending_live_side": "UP",
                        "pending_live_price": 0.5,
                        "pending_live_order_size": 2.0,
                        "pending_live_order_cost": 1.0,
                        "pending_live_expected_profit": 1.0,
                        "pending_live_order_id": "oid-prev",
                        "pending_live_end_time": "2026-04-02T00:00:00+00:00",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    evaluation_calls = {"side": 0, "plan": 0}

    class _PendingChainSettlementClobClient(_StubClobClient):
        def __init__(self):
            super().__init__(
                order_payloads={
                    "oid-prev": {
                        "status": "matched",
                        "size_matched": "2.0",
                        "price": "0.50",
                        "associate_trades": ["trade-pending"],
                    }
                }
            )

        def get_trades(self, params=None, only_first_page=False):
            if isinstance(params, dict):
                raise AttributeError("'dict' object has no attribute 'market'")
            trade_id = getattr(params, "id", None)
            if trade_id == "oid-prev":
                return []
            if trade_id == "trade-pending":
                return [
                    {
                        "id": "trade-pending",
                        "taker_order_id": "oid-prev",
                        "size": "2.0",
                        "price": "0.50",
                        "status": "MINED",
                    }
                ]
            assert params is None
            assert only_first_page is True
            return []

    def fail_if_strategy_evaluated(**kwargs):
        evaluation_calls["side"] += 1
        return SideDecision(side="UP")

    def fail_if_plan_built(*args, **kwargs):
        evaluation_calls["plan"] += 1
        return TradePlan(
            True,
            "UP",
            price=0.55,
            order_size=2.0,
            order_cost=1.0,
            expected_profit=1.0,
        )

    monkeypatch.setattr("trader._resolve_side_from_strategy", fail_if_strategy_evaluated)
    monkeypatch.setattr("trader.build_trade_plan", fail_if_plan_built)

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=3,
            live_strategy_ids=[3, 7],
        ),
        market_client=_SettlingLiveClient(),
        clob_client=_PendingChainSettlementClobClient(),
        state_path=state_path,
        log_path=tmp_path / "live_orders.csv",
        stop_when_safe=lambda: True,
    )

    reloaded = load_session_state(state_path, effective_live_strategy_ids=[3, 7])
    strategy_result = next(item for item in result["strategies"] if item["strategy_id"] == 7)

    assert result["status"] == "pending_settlement"
    assert strategy_result["status"] == "pending_settlement"
    assert strategy_result["skip_reason"] == "awaiting_fill_confirmation"
    assert strategy_result["order_id"] == "oid-prev"
    assert evaluation_calls == {"side": 0, "plan": 0}
    assert reloaded.live_strategies[7].pending_live_slug == "btc-updown-5m-prev"
    assert reloaded.live_strategies[7].pending_live_order_id == "oid-prev"


def test_run_live_trading_keeps_removed_pending_live_strategy_managed(tmp_path):
    control = RuntimeControl(initial_mode='live')
    state_path = tmp_path / 'live_state.json'
    state_path.write_text(
        json.dumps(
            {
                'live_strategies': {
                    '6': {
                        'round_index': 4,
                        'cash_pnl': 0.0,
                        'recovery_loss': 0.0,
                        'consecutive_losses': 0,
                        'consecutive_max_stake_skips': 0,
                        'signal_round_slug': None,
                        'signal_round_open_up_price': None,
                        'signal_round_locked_side': None,
                        'strategy6_last_ofi_score': 0.75,
                        'stop_loss_count': 0,
                        'daily_realized_pnl': 0.0,
                        'current_day': '2026-04-23',
                        'pending_live_slug': 'btc-updown-5m-orphaned',
                        'pending_live_side': 'UP',
                        'pending_live_price': 0.51,
                        'pending_live_order_size': 2.0,
                        'pending_live_order_cost': 1.0,
                        'pending_live_expected_profit': 1.0,
                        'pending_live_order_id': 'oid-orphaned',
                        'pending_live_end_time': '2099-01-01T00:00:00+00:00',
                    }
                }
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
            strategy_id=3,
            live_strategy_ids=[3],
        ),
        market_client=_NoTradeLiveMarketClient(),
        clob_client=_StubClobClient(),
        state_path=state_path,
        log_path=tmp_path / 'live_orders.csv',
        runtime_control=control,
        stop_when_safe=lambda: True,
    )

    snapshot = control.snapshot()
    state = load_session_state(state_path, effective_live_strategy_ids=[3])

    assert result['status'] == 'pending_settlement'
    assert any(item['strategy_id'] == 6 and item['status'] == 'pending_settlement' for item in result['strategies'])
    assert snapshot.pending_live_order is True
    assert snapshot.safe_to_switch is False
    assert snapshot.current_round_slug == 'btc-updown-5m-orphaned'
    assert state.live_strategies[6].pending_live_order_id == 'oid-orphaned'
    assert state.live_strategies[6].pending_live_slug == 'btc-updown-5m-orphaned'


def test_run_live_trading_keeps_retryable_settlement_clob_timeout_pending(tmp_path):
    state_path = tmp_path / "live_state.json"
    state_path.write_text(
        json.dumps(
            {
                "live_strategies": {
                    "4": {
                        "round_index": 4,
                        "cash_pnl": 0.0,
                        "recovery_loss": 0.0,
                        "consecutive_losses": 0,
                        "pending_live_slug": "btc-updown-5m-prev",
                        "pending_live_side": "UP",
                        "pending_live_price": 0.5,
                        "pending_live_order_size": 2.0,
                        "pending_live_order_cost": 1.0,
                        "pending_live_expected_profit": 1.0,
                        "pending_live_order_id": "oid-timeout",
                        "pending_live_end_time": "2026-04-02T00:00:00+00:00",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=4,
            live_strategy_ids=[4],
            poll_interval_seconds=1,
        ),
        market_client=_SettlingLiveClient(),
        clob_client=_SettlementTimeoutClobClient(),
        state_path=state_path,
        log_path=tmp_path / "live_orders.csv",
        stop_when_safe=lambda: True,
    )

    state = load_session_state(state_path, effective_live_strategy_ids=[4])
    strategy_result = next(item for item in result["strategies"] if item["strategy_id"] == 4)

    assert result["status"] == "pending_settlement"
    assert strategy_result["status"] == "pending_settlement"
    assert strategy_result["phase"] == "settlement"
    assert strategy_result["skip_reason"] == "settlement_retryable_clob_error"
    assert "Request exception" in strategy_result["error"]
    assert not any(item.get("status") == "error" for item in result["strategies"])
    assert state.live_strategies[4].pending_live_slug == "btc-updown-5m-prev"
    assert state.live_strategies[4].pending_live_order_id == "oid-timeout"


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
    def __init__(self, *, post_response=None, order_payloads=None, balance_payload=None, trade_payloads=None):
        self.created_orders = []
        self.posted_orders = []
        self.post_response = post_response if post_response is not None else {"success": True, "orderID": "oid-123"}
        self.order_payloads = order_payloads or {}
        self.balance_payload = (
            balance_payload if balance_payload is not None else {"available": 100.0, "balance": 100.0}
        )
        self.trade_payloads = trade_payloads or {}

    def create_market_order(self, order_args):
        self.created_orders.append(order_args)
        return {"signed": True, "payload": order_args}

    def post_order(self, order, order_type):
        self.posted_orders.append((order, order_type))
        return self.post_response

    def get_order(self, order_id):
        return self.order_payloads.get(order_id, {})

    def get_trades(self, params=None, only_first_page=False):
        if isinstance(params, dict):
            order_id = str(params.get("order_id") or params.get("orderID") or params.get("orderId") or "")
            return self.trade_payloads.get(order_id, [])
        trade_id = str(getattr(params, "id", "") or "")
        if trade_id:
            return self.trade_payloads.get(trade_id, [])
        return self.trade_payloads.get(None, [])

    def get_balance(self):
        return self.balance_payload


class _StrictStubClobClient(_StubClobClient):
    def create_market_order(self, order_args):
        assert hasattr(order_args, "price")
        assert not hasattr(order_args, "fee_rate_bps")
        assert not hasattr(order_args, "nonce")
        assert not hasattr(order_args, "taker")
        return super().create_market_order(order_args)


class _SettlementTimeoutClobClient(_StubClobClient):
    def get_trades(self, params=None):
        raise RuntimeError(
            "[py_clob_client_v2] request error: The read operation timed out "
            "PolyApiException[status_code=None, error_message=Request exception!]"
        )


def _confirmed_trade(order_id: str, *, size: float = 2.0, price: float = 0.5):
    return {"taker_order_id": order_id, "size": size, "price": price, "status": "CONFIRMED"}


class _FokNotFilledClobClient(_StubClobClient):
    def post_order(self, order, order_type):
        self.posted_orders.append((order, order_type))
        raise RuntimeError(
            "[py_clob_client_v2] request error status=400 "
            "url=https://clob.polymarket.com/order "
            'body={"error":"order couldn\'t be fully filled. FOK orders are fully filled or killed.",'
            '"orderID":"0xdeadbeef"}'
        )


class _TransientSubmitClobClient(_StubClobClient):
    def post_order(self, order, order_type):
        self.posted_orders.append((order, order_type))
        raise RuntimeError(
            "[py_clob_client_v2] request error: [SSL: UNEXPECTED_EOF_WHILE_READING] "
            "EOF occurred in violation of protocol (_ssl.c:1000) "
            "PolyApiException[status_code=None, error_message=Request exception!]"
        )


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


class _TerminalPricesSettlingLiveClient(_LiveMarketClient):
    def get_event_by_slug(self, slug: str):
        if slug != "btc-updown-5m-prev":
            raise AssertionError(f"Unexpected slug {slug}")
        return {
            "closed": True,
            "eventMetadata": {},
            "markets": [
                {
                    "conditionId": "cond-prev",
                    "closed": True,
                    "outcomes": '["Up","Down"]',
                    "outcomePrices": '["0","1"]',
                }
            ],
        }

    def get_clob_market_by_condition_id(self, condition_id: str):
        assert condition_id == "cond-prev"
        return {
            "closed": True,
            "tokens": [
                {"outcome": "Up", "winner": False},
                {"outcome": "Down", "winner": True},
            ],
        }


class _NoFinalPriceConsensusLiveClient(_LiveMarketClient):
    def get_event_by_slug(self, slug: str):
        if slug != "btc-updown-5m-prev":
            raise AssertionError(f"Unexpected slug {slug}")
        return {
            "slug": slug,
            "closed": True,
            "eventMetadata": {"priceToBeat": 100.0},
            "markets": [
                {
                    "conditionId": "cond-prev",
                    "closed": True,
                    "outcomes": '["Up","Down"]',
                    "outcomePrices": '["0","1"]',
                }
            ],
        }

    def get_market_by_slug(self, slug: str):
        if slug != "btc-updown-5m-prev":
            return super().get_market_by_slug(slug)
        return {
            "conditionId": "cond-prev",
            "closed": True,
            "events": [{"eventMetadata": {"priceToBeat": 100.0}}],
            "outcomes": '["Up","Down"]',
            "outcomePrices": '["0","1"]',
        }

    def get_clob_market_by_condition_id(self, condition_id: str):
        assert condition_id == "cond-prev"
        return {
            "closed": True,
            "tokens": [
                {"outcome": "Up", "winner": False},
                {"outcome": "Down", "winner": True},
            ],
        }


class _ResolvedPositionSettlingLiveClient(_LiveMarketClient):
    def get_event_by_slug(self, slug: str):
        if slug != "btc-updown-5m-prev":
            raise AssertionError(f"Unexpected slug {slug}")
        return {
            "closed": True,
            "eventMetadata": {},
            "markets": [
                {
                    "closed": True,
                    "outcomes": '["Up","Down"]',
                    "outcomePrices": '["0.49","0.51"]',
                }
            ],
        }

    def get_current_positions(self, *, user: str, redeemable: bool | None = None):
        assert user == "0xfunder"
        assert redeemable is True
        return [
            {
                "proxyWallet": "0xfunder",
                "eventSlug": "btc-updown-5m-prev",
                "outcome": "Up",
                "size": "2.0",
                "redeemable": True,
                "conditionId": "0x" + "11" * 32,
            }
        ]


class _ConflictingPositionAndOfficialResultClient(_ResolvedPositionSettlingLiveClient):
    def get_event_by_slug(self, slug: str):
        if slug != "btc-updown-5m-prev":
            raise AssertionError(f"Unexpected slug {slug}")
        return {
            "closed": True,
            "eventMetadata": {},
            "markets": [
                {
                    "conditionId": "cond-prev",
                    "closed": True,
                    "outcomes": '["Up","Down"]',
                    "outcomePrices": '["0","1"]',
                }
            ],
        }

    def get_clob_market_by_condition_id(self, condition_id: str):
        assert condition_id == "cond-prev"
        return {
            "closed": True,
            "tokens": [
                {"outcome": "Up", "winner": False},
                {"outcome": "Down", "winner": True},
            ],
        }


class _OfficialTerminalResultNoPositionClient(_LiveMarketClient):
    def get_event_by_slug(self, slug: str):
        if slug != "btc-updown-5m-prev":
            raise AssertionError(f"Unexpected slug {slug}")
        return {
            "closed": True,
            "eventMetadata": {},
            "markets": [
                {
                    "conditionId": "cond-prev",
                    "closed": True,
                    "outcomes": '["Up","Down"]',
                    "outcomePrices": '["0","1"]',
                }
            ],
        }

    def get_clob_market_by_condition_id(self, condition_id: str):
        assert condition_id == "cond-prev"
        return {
            "closed": True,
            "tokens": [
                {"outcome": "Up", "winner": False},
                {"outcome": "Down", "winner": True},
            ],
        }

    def get_current_positions(self, *, user: str, redeemable: bool | None = None):
        assert user == "0xfunder"
        assert redeemable is True
        return []


class _OfficialMarketEndpointFallbackClient(_LiveMarketClient):
    def get_event_by_slug(self, slug: str):
        if slug != "btc-updown-5m-prev":
            raise AssertionError(f"Unexpected slug {slug}")
        return {
            "closed": False,
            "eventMetadata": {"priceToBeat": 100.0},
            "markets": [
                {
                    "slug": slug,
                    "closed": False,
                    "outcomes": '["Up","Down"]',
                    "outcomePrices": '["0.49","0.51"]',
                }
            ],
        }

    def get_market_by_slug(self, slug: str):
        if slug != "btc-updown-5m-prev":
            raise AssertionError(f"Unexpected market slug {slug}")
        return {
            "slug": slug,
            "closed": True,
            "eventMetadata": {
                "priceToBeat": 100.0,
                "finalPrice": 90.0,
            },
            "outcomes": '["Up","Down"]',
            "outcomePrices": '["0","1"]',
        }

    def get_current_positions(self, *, user: str, redeemable: bool | None = None):
        assert user == "0xfunder"
        assert redeemable is True
        return []


class _TerminalPricesWithoutFinalPriceClient(_LiveMarketClient):
    def get_event_by_slug(self, slug: str):
        if slug != "btc-updown-5m-prev":
            raise AssertionError(f"Unexpected slug {slug}")
        return {
            "closed": False,
            "eventMetadata": {"priceToBeat": 100.0},
            "markets": [
                {
                    "slug": slug,
                    "conditionId": "cond-prev",
                    "closed": False,
                    "outcomes": '["Up","Down"]',
                    "outcomePrices": '["0.49","0.51"]',
                }
            ],
        }

    def get_market_by_slug(self, slug: str):
        if slug != "btc-updown-5m-prev":
            raise AssertionError(f"Unexpected market slug {slug}")
        return {
            "slug": slug,
            "conditionId": "cond-prev",
            "closed": True,
            "eventMetadata": {"priceToBeat": 100.0},
            "outcomes": '["Up","Down"]',
            "outcomePrices": '["0","1"]',
        }

    def get_clob_market_by_condition_id(self, condition_id: str):
        assert condition_id == "cond-prev"
        return {
            "closed": True,
            "tokens": [
                {"outcome": "Up", "winner": False},
                {"outcome": "Down", "winner": True},
            ],
        }

    def get_current_positions(self, *, user: str, redeemable: bool | None = None):
        assert user == "0xfunder"
        assert redeemable is True
        return []


class _WsOnlyResolutionLiveClient(_LiveMarketClient):
    def get_event_by_slug(self, slug: str):
        if slug != "btc-updown-5m-prev":
            raise AssertionError(f"Unexpected slug {slug}")
        return {
            "closed": False,
            "eventMetadata": {"priceToBeat": 100.0},
            "markets": [
                {
                    "closed": False,
                    "outcomes": '["Up","Down"]',
                    "outcomePrices": '["0.49","0.51"]',
                }
            ],
        }

    def get_current_positions(self, *, user: str, redeemable: bool | None = None):
        assert user == "0xfunder"
        assert redeemable is True
        return []

    def get_ws_market_resolution(self, market):
        return {"winning_outcome": "Down"}


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
    state_path = tmp_path / "session_state.json"
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
    state_path = tmp_path / "session_state.json"
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


def test_load_session_state_live_strategies_wraps_legacy_live_state_for_effective_strategy(tmp_path):
    state_path = tmp_path / "session_state.json"
    state_path.write_text(
        json.dumps(
            {
                "round_index": 5,
                "cash_pnl": 2.5,
                "recovery_loss": 0.75,
                "consecutive_losses": 2,
                "consecutive_max_stake_skips": 1,
                "signal_round_slug": "btc-updown-5m-live-prev",
                "signal_round_open_up_price": 0.51,
                "signal_round_locked_side": "UP",
                "strategy6_last_ofi_score": 0.82,
                "stop_loss_count": 3,
                "daily_realized_pnl": -1.25,
                "current_day": "2026-04-21",
                "pending_live_slug": "btc-updown-5m-live-pending",
                "pending_live_side": "DOWN",
                "pending_live_price": 0.43,
                "pending_live_order_size": 12.0,
                "pending_live_order_cost": 5.16,
                "pending_live_expected_profit": 6.84,
                "pending_live_order_id": "oid-live-7",
                "pending_live_end_time": "2026-04-21T12:05:00+00:00",
                "pending_paper_trades": [],
            }
        ),
        encoding="utf-8",
    )

    state = load_session_state(state_path, effective_live_strategy_ids=[7])

    assert sorted(state.live_strategies.keys()) == [7]
    live_state = state.live_strategies[7]
    assert live_state.round_index == 5
    assert live_state.cash_pnl == 2.5
    assert live_state.recovery_loss == 0.0
    assert live_state.consecutive_losses == 2
    assert live_state.consecutive_max_stake_skips == 1
    assert live_state.signal_round_slug == "btc-updown-5m-live-prev"
    assert live_state.signal_round_open_up_price == 0.51
    assert live_state.signal_round_locked_side == "UP"
    assert live_state.strategy6_last_ofi_score == 0.82
    assert live_state.stop_loss_count == 3
    assert live_state.daily_realized_pnl == -1.25
    assert live_state.current_day == "2026-04-21"
    assert live_state.pending_live_slug == "btc-updown-5m-live-pending"
    assert live_state.pending_live_side == "DOWN"
    assert live_state.pending_live_price == 0.43
    assert live_state.pending_live_order_size == 12.0
    assert live_state.pending_live_order_cost == 5.16
    assert live_state.pending_live_expected_profit == 6.84
    assert live_state.pending_live_order_id == "oid-live-7"
    assert live_state.pending_live_end_time == "2026-04-21T12:05:00+00:00"


def test_load_session_state_live_strategies_preserves_legacy_pending_state_during_multi_strategy_migration(tmp_path):
    state_path = tmp_path / "session_state.json"
    state_path.write_text(
        json.dumps(
            {
                "round_index": 5,
                "cash_pnl": 2.5,
                "recovery_loss": 0.75,
                "consecutive_losses": 2,
                "consecutive_max_stake_skips": 1,
                "signal_round_slug": "btc-updown-5m-live-prev",
                "signal_round_open_up_price": 0.51,
                "signal_round_locked_side": "UP",
                "strategy6_last_ofi_score": 0.82,
                "stop_loss_count": 3,
                "daily_realized_pnl": -1.25,
                "current_day": "2026-04-21",
                "pending_live_slug": "btc-updown-5m-live-pending",
                "pending_live_side": "DOWN",
                "pending_live_price": 0.43,
                "pending_live_order_size": 12.0,
                "pending_live_order_cost": 5.16,
                "pending_live_expected_profit": 6.84,
                "pending_live_order_id": "oid-live-7",
                "pending_live_end_time": "2026-04-21T12:05:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    state = load_session_state(state_path, effective_live_strategy_ids=[7, 3])

    assert list(state.live_strategies.keys()) == [7, 3]
    preserved = state.live_strategies[7]
    empty = state.live_strategies[3]
    assert preserved.pending_live_order_id == "oid-live-7"
    assert preserved.pending_live_slug == "btc-updown-5m-live-pending"
    assert preserved.round_index == 5
    assert empty.pending_live_order_id is None
    assert empty.pending_live_slug is None


def test_load_session_state_live_strategies_uses_trusted_legacy_strategy_identity_when_present(tmp_path):
    state_path = tmp_path / "session_state.json"
    state_path.write_text(
        json.dumps(
            {
                "strategy_id": 7,
                "round_index": 5,
                "cash_pnl": 2.5,
                "recovery_loss": 0.75,
                "consecutive_losses": 2,
                "consecutive_max_stake_skips": 1,
                "signal_round_slug": "btc-updown-5m-live-prev",
                "signal_round_open_up_price": 0.51,
                "signal_round_locked_side": "UP",
                "strategy6_last_ofi_score": 0.82,
                "stop_loss_count": 3,
                "daily_realized_pnl": -1.25,
                "current_day": "2026-04-21",
                "pending_live_slug": "btc-updown-5m-live-pending",
                "pending_live_side": "DOWN",
                "pending_live_price": 0.43,
                "pending_live_order_size": 12.0,
                "pending_live_order_cost": 5.16,
                "pending_live_expected_profit": 6.84,
                "pending_live_order_id": "oid-live-7",
                "pending_live_end_time": "2026-04-21T12:05:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    state = load_session_state(state_path, effective_live_strategy_ids=[3, 7])

    assert sorted(state.live_strategies.keys()) == [3, 7]
    assert state.live_strategies[7].pending_live_order_id == "oid-live-7"
    assert state.live_strategies[7].pending_live_slug == "btc-updown-5m-live-pending"
    assert state.live_strategies[3].pending_live_order_id is None
    assert state.live_strategies[3].pending_live_slug is None


def test_load_session_state_live_strategies_preserves_multiple_live_strategy_entries(tmp_path):
    state_path = tmp_path / "session_state.json"
    state_path.write_text(
        json.dumps(
            {
                "live_strategies": {
                    "3": {
                        "round_index": 2,
                        "cash_pnl": 1.2,
                        "recovery_loss": 0.0,
                        "consecutive_losses": 0,
                        "consecutive_max_stake_skips": 0,
                        "signal_round_slug": "btc-updown-5m-live-3",
                        "signal_round_open_up_price": 0.5,
                        "signal_round_locked_side": None,
                        "strategy6_last_ofi_score": None,
                        "stop_loss_count": 0,
                        "daily_realized_pnl": 1.2,
                        "current_day": "2026-04-22",
                        "pending_live_slug": None,
                        "pending_live_side": None,
                        "pending_live_price": None,
                        "pending_live_order_size": None,
                        "pending_live_order_cost": None,
                        "pending_live_expected_profit": None,
                        "pending_live_order_id": None,
                        "pending_live_end_time": None,
                    },
                    "7": {
                        "round_index": 9,
                        "cash_pnl": -0.4,
                        "recovery_loss": 1.1,
                        "consecutive_losses": 1,
                        "consecutive_max_stake_skips": 2,
                        "signal_round_slug": "btc-updown-5m-live-7",
                        "signal_round_open_up_price": 0.47,
                        "signal_round_locked_side": "DOWN",
                        "strategy6_last_ofi_score": 0.76,
                        "stop_loss_count": 2,
                        "daily_realized_pnl": -0.4,
                        "current_day": "2026-04-22",
                        "pending_live_slug": "btc-updown-5m-live-7-pending",
                        "pending_live_side": "UP",
                        "pending_live_price": 0.48,
                        "pending_live_order_size": 8.0,
                        "pending_live_order_cost": 3.84,
                        "pending_live_expected_profit": 4.16,
                        "pending_live_order_id": "oid-live-7-existing",
                        "pending_live_end_time": "2026-04-22T09:35:00+00:00",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    state = load_session_state(state_path, effective_live_strategy_ids=[3, 7])

    assert sorted(state.live_strategies.keys()) == [3, 7]
    assert state.live_strategies[3].round_index == 2
    assert state.live_strategies[3].cash_pnl == 1.2
    assert state.live_strategies[7].round_index == 9
    assert state.live_strategies[7].strategy6_last_ofi_score == 0.76
    assert state.live_strategies[7].pending_live_order_id == "oid-live-7-existing"


def test_load_session_state_live_strategies_roundtrip_preserves_legacy_top_level_fields_when_map_exists(tmp_path):
    state_path = tmp_path / "session_state.json"
    original_payload = {
        "round_index": 99,
        "cash_pnl": 9.9,
        "recovery_loss": 0.4,
        "consecutive_losses": 4,
        "consecutive_max_stake_skips": 3,
        "signal_round_slug": "legacy-live-top-level",
        "signal_round_open_up_price": 0.61,
        "signal_round_locked_side": "UP",
        "strategy6_last_ofi_score": 0.91,
        "stop_loss_count": 5,
        "daily_realized_pnl": 9.9,
        "current_day": "2026-04-22",
        "pending_live_slug": "legacy-pending-slug",
        "pending_live_side": "DOWN",
        "pending_live_price": 0.42,
        "pending_live_order_size": 10.0,
        "pending_live_order_cost": 4.2,
        "pending_live_expected_profit": 5.8,
        "pending_live_order_id": "legacy-order-id",
        "pending_live_end_time": "2026-04-22T09:40:00+00:00",
        "live_strategies": {
            "3": {
                "round_index": 2,
                "cash_pnl": 1.2,
                "recovery_loss": 0.0,
                "consecutive_losses": 0,
                "consecutive_max_stake_skips": 0,
                "signal_round_slug": "btc-updown-5m-live-3",
                "signal_round_open_up_price": 0.5,
                "signal_round_locked_side": None,
                "strategy6_last_ofi_score": None,
                "stop_loss_count": 0,
                "daily_realized_pnl": 1.2,
                "current_day": "2026-04-22",
                "pending_live_slug": None,
                "pending_live_side": None,
                "pending_live_price": None,
                "pending_live_order_size": None,
                "pending_live_order_cost": None,
                "pending_live_expected_profit": None,
                "pending_live_order_id": None,
                "pending_live_end_time": None,
            },
            "7": {
                "round_index": 9,
                "cash_pnl": -0.4,
                "recovery_loss": 1.1,
                "consecutive_losses": 1,
                "consecutive_max_stake_skips": 2,
                "signal_round_slug": "btc-updown-5m-live-7",
                "signal_round_open_up_price": 0.47,
                "signal_round_locked_side": "DOWN",
                "strategy6_last_ofi_score": 0.76,
                "stop_loss_count": 2,
                "daily_realized_pnl": -0.4,
                "current_day": "2026-04-22",
                "pending_live_slug": "btc-updown-5m-live-7-pending",
                "pending_live_side": "UP",
                "pending_live_price": 0.48,
                "pending_live_order_size": 8.0,
                "pending_live_order_cost": 3.84,
                "pending_live_expected_profit": 4.16,
                "pending_live_order_id": "oid-live-7-existing",
                "pending_live_end_time": "2026-04-22T09:35:00+00:00",
            },
        },
    }
    state_path.write_text(json.dumps(original_payload), encoding="utf-8")

    state = load_session_state(state_path, effective_live_strategy_ids=[7, 3])
    save_session_state(state_path, state)
    reloaded_payload = json.loads(state_path.read_text(encoding="utf-8"))

    assert reloaded_payload["pending_live_order_id"] == "legacy-order-id"
    assert reloaded_payload["pending_live_slug"] == "legacy-pending-slug"
    assert reloaded_payload["round_index"] == 99
    assert reloaded_payload["live_strategies"]["7"]["pending_live_order_id"] == "oid-live-7-existing"
    assert reloaded_payload["live_strategies"]["3"]["round_index"] == 2





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


def test_run_paper_strategy7_flat_continues_with_unresolved_pending_trade(tmp_path, monkeypatch):
    stop_event = threading.Event()

    def fake_sleep(_seconds):
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)
    monkeypatch.setattr("trader._resolve_side_from_strategy", lambda **kwargs: SideDecision(side="UP"))
    monkeypatch.setattr(
        "trader.build_trade_plan",
        lambda *args, **kwargs: TradePlan(
            True,
            "UP",
            price=0.50,
            order_size=2.0,
            order_cost=1.0,
            expected_profit=1.0,
            tracks_recovery_loss=False,
        ),
    )

    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "paper_strategies": {
                    "7": {
                        "round_index": 1,
                        "cash_pnl": 0.0,
                        "recovery_loss": 0.0,
                        "consecutive_losses": 0,
                        "pending_paper_trades": [
                            {
                                "round_index": 0,
                                "event_slug": "btc-updown-5m-paper-prev",
                                "start_time": "2026-04-06T06:00:00+00:00",
                                "end_time": "2099-04-06T06:05:00+00:00",
                                "side": "UP",
                                "price": 0.5,
                                "order_size": 2.0,
                                "order_cost": 1.0,
                                "expected_profit": 1.0,
                                "strategy": 7,
                                "entry_timing": "OPEN",
                                "queued_at": "2026-04-06T06:00:05+00:00",
                                "tracks_recovery_loss": False,
                            }
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    class _UnresolvedStrategy7PaperClient(_ImmediatePaperRoundClient):
        def get_event_by_slug(self, slug: str):
            assert slug == "btc-updown-5m-paper-prev"
            return {"eventMetadata": {"priceToBeat": None, "finalPrice": None}}

    result = run_paper_trading(
        AppConfig(
            strategy_id=7,
            paper_strategy_ids=[7],
            poll_interval_seconds=1,
        ),
        client=_UnresolvedStrategy7PaperClient(),
        state_path=state_path,
        log_path=tmp_path / "paper.csv",
        stop_event=stop_event,
    )

    state = load_session_state(state_path, effective_paper_strategy_ids=[7])
    pending = state.paper_strategies[7].pending_paper_trades
    assert result["status"] == "stopped"
    assert [item.event_slug for item in pending] == [
        "btc-updown-5m-paper-prev",
        "btc-updown-5m-paper-now",
    ]
    assert pending[1].tracks_recovery_loss is False


def test_run_paper_trading_continues_with_unresolved_pending_settlement(tmp_path, monkeypatch):
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
    assert state.round_index == 2
    assert len(state.pending_paper_trades) == 2
    assert state.pending_paper_trades[0].event_slug == 'btc-updown-5m-paper-prev'
    assert state.pending_paper_trades[1].event_slug == 'btc-updown-5m-paper-now'
    assert not (tmp_path / 'paper.csv').exists()


def test_run_paper_trading_keeps_pending_when_settlement_errors(tmp_path, monkeypatch):
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

    class _BrokenSettlementPaperClient(_ImmediatePaperRoundClient):
        def get_event_by_slug(self, slug: str):
            raise ValueError(f"broken settlement payload for {slug}")

    result = run_paper_trading(
        AppConfig(strategy_id=2, poll_interval_seconds=1),
        client=_BrokenSettlementPaperClient(),
        state_path=state_path,
        log_path=tmp_path / 'paper.csv',
        stop_when_safe=lambda: True,
        stop_event=stop_event,
    )

    state = load_session_state(state_path)
    assert result['status'] == 'stopped'
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


def test_live_and_paper_dry_runs_expose_matching_trade_plan_for_same_config(tmp_path):
    cfg = AppConfig(
        strategy_id=4,
        paper_strategy_ids=[4],
        live_strategy_ids=[4],
        base_order_cost=1.25,
        max_stake=10.0,
    )
    market_client = _LiveMarketClient()

    live_result = place_live_order(
        cfg=cfg,
        market_client=market_client,
        state_path=tmp_path / "live_state.json",
        dry_run=True,
    )
    paper_result = run_paper_trading(
        cfg,
        client=market_client,
        state_path=tmp_path / "paper_state.json",
        log_path=tmp_path / "paper.csv",
        dry_run_once=True,
    )

    assert paper_result["status"] == "dry_run"
    assert live_result["status"] == "dry_run"
    for key in ("side", "price", "should_trade", "skip_reason", "order_size", "order_cost", "expected_profit"):
        if isinstance(live_result[key], float):
            assert paper_result[key] == pytest.approx(live_result[key])
        else:
            assert paper_result[key] == live_result[key]


def test_place_live_order_skips_when_entry_window_missed_without_next_round(tmp_path):
    cfg = AppConfig(
        live_trading_enabled=True,
        open_delay_seconds=0,
        entry_grace_seconds=1,
    )
    stub_clob = _StubClobClient()

    result = place_live_order(
        cfg=cfg,
        market_client=_MissedEntryNoNextLiveClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "live.csv",
    )

    state = load_session_state(tmp_path / "state.json")
    assert result["status"] == "skipped"
    assert result["should_trade"] is False
    assert result["skip_reason"] == "entry_window_missed"
    assert stub_clob.created_orders == []
    assert state.pending_live_slug is None


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
        log_path=tmp_path / "live.csv",
    )

    assert result["status"] == "submitted"
    assert result["side"] == "UP"
    assert result["token_id"] == "up-token"
    assert result["order_id"] == "oid-123"
    assert len(stub_clob.created_orders) == 1
    assert stub_clob.created_orders[0].side == "BUY"
    assert len(stub_clob.posted_orders) == 1


def test_place_live_order_passes_available_balance_for_fee_adjustment(tmp_path):
    cfg = AppConfig(live_trading_enabled=True, max_entry_price=0.58)
    stub_clob = _StubClobClient(balance_payload={"available": 5.5, "balance": 5.5})

    result = place_live_order(
        cfg=cfg,
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "live.csv",
    )

    assert result["status"] == "submitted"
    assert stub_clob.created_orders[0].user_usdc_balance == pytest.approx(5.5)


def test_place_live_order_logs_official_fill_price_after_submission(tmp_path):
    cfg = AppConfig(live_trading_enabled=True)
    stub_clob = _StubClobClient(
        order_payloads={
            "oid-123": {
                "status": "filled",
                "filled_order_size": "2.0",
                "filled_order_cost": "1.06",
                "avg_price": "0.53",
            }
        }
    )
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "live.csv"

    result = place_live_order(
        cfg=cfg,
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=state_path,
        log_path=log_path,
    )

    state = load_session_state(state_path)
    rows = list(csv.DictReader(log_path.open(newline="", encoding="utf-8")))

    assert result["status"] == "submitted"
    assert result["price"] == pytest.approx(0.547437)
    assert result["order_size"] == pytest.approx(2.0)
    assert result["order_cost"] == pytest.approx(1.094874)
    assert state.pending_live_price == pytest.approx(0.547437)
    assert state.pending_live_order_size == pytest.approx(2.0)
    assert state.pending_live_order_cost == pytest.approx(1.094874)
    assert rows[-1]["price"] == "0.547437"
    assert rows[-1]["order_size"] == "2.0"
    assert rows[-1]["order_cost"] == "1.094874"


def test_place_live_order_logs_fee_adjusted_effective_fill_after_submission(tmp_path):
    cfg = AppConfig(live_trading_enabled=True)
    stub_clob = _StubClobClient(
        order_payloads={"oid-123": {"status": "filled"}},
        trade_payloads={
            "oid-123": [
                _confirmed_trade("oid-123", size=1.923075, price=0.52),
            ],
        },
    )
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "live.csv"

    result = place_live_order(
        cfg=cfg,
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=state_path,
        log_path=log_path,
    )

    state = load_session_state(state_path)
    rows = list(csv.DictReader(log_path.open(newline="", encoding="utf-8")))

    assert result["status"] == "submitted"
    assert result["price"] == pytest.approx(0.537472)
    assert result["raw_price"] == pytest.approx(0.52)
    assert result["fee"] == pytest.approx(0.0335999664)
    assert result["raw_order_cost"] == pytest.approx(0.999999)
    assert result["order_cost"] == pytest.approx(1.0335989664)
    assert result["expected_profit"] == pytest.approx(0.8894760336)
    assert state.pending_live_price == pytest.approx(0.537472)
    assert state.pending_live_order_cost == pytest.approx(1.0335989664)
    assert state.pending_live_raw_price == pytest.approx(0.52)
    assert state.pending_live_fee == pytest.approx(0.0335999664)
    assert rows[-1]["price"] == "0.537472"
    assert rows[-1]["raw_price"] == "0.52"
    assert rows[-1]["fee"] == "0.0335999664"
    assert rows[-1]["raw_order_cost"] == "0.999999"
    assert rows[-1]["order_cost"] == "1.0335989664"


def test_place_live_order_audits_official_fill_above_max_entry_price(tmp_path, monkeypatch):
    class _InsideCapLiveMarketClient(_LiveMarketClient):
        def quote_from_market(self, _market):
            return MarketQuote(
                slug="btc-updown-5m-test",
                up_price=0.52,
                down_price=0.48,
                up_best_ask=0.52,
                fetched_at=datetime.now(timezone.utc),
            )

    messages: list[str] = []
    monkeypatch.setattr("trader._runtime_log", messages.append)
    cfg = AppConfig(live_trading_enabled=True, max_entry_price=0.54)
    stub_clob = _StubClobClient(
        order_payloads={
            "oid-123": {
                "status": "filled",
                "filled_order_size": "2.0",
                "filled_order_cost": "1.12",
                "avg_price": "0.56",
            }
        }
    )
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "live.csv"

    result = place_live_order(
        cfg=cfg,
        market_client=_InsideCapLiveMarketClient(),
        clob_client=stub_clob,
        state_path=state_path,
        log_path=log_path,
    )

    rows = list(csv.DictReader(log_path.open(newline="", encoding="utf-8")))

    assert result["status"] == "submitted"
    assert result["price"] == pytest.approx(0.577248)
    assert rows[-1]["price"] == "0.577248"
    assert any("official_fill_price_above_max_entry_price" in message for message in messages)
    assert any("max_entry_price=0.54" in message for message in messages)


def test_place_live_order_logs_official_market_order_price_cap(tmp_path):
    cfg = AppConfig(live_trading_enabled=True, strategy_id=10, max_entry_price=0.54)
    stub_clob = _StubClobClient(
        order_payloads={
            "oid-123": {
                "status": "filled",
                "filled_order_size": "2.0",
                "filled_order_cost": "1.04",
                "avg_price": "0.52",
            }
        }
    )
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "live.csv"

    plan = TradePlan(
        True,
        side="UP",
        price=0.52,
        order_size=2.0,
        order_cost=1.04,
        expected_profit=0.96,
        max_entry_price=0.54,
    )
    execution = trader._execute_order_plan(
        mode="live",
        cfg=cfg,
        clob_client=stub_clob,
        strategy_id=10,
        slug="btc-updown-5m-test",
        token_id="up-token",
        plan=plan,
        remaining_budget=100.0,
    )
    executed_plan, fill_source = trader._plan_with_verified_live_fill(
        plan=plan,
        side="UP",
        order_id=execution.order_id,
        clob_client=stub_clob,
        max_entry_price=plan.max_entry_price,
        strategy_id=10,
        slug="btc-updown-5m-test",
    )
    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=datetime.now(timezone.utc),
            mode="live",
            round_index=0,
            strategy=10,
            entry_timing=cfg.entry_timing,
            event_slug="btc-updown-5m-test",
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc) + timedelta(minutes=5),
            side="UP",
            price=executed_plan.price,
            order_size=executed_plan.order_size,
            order_cost=executed_plan.order_cost,
            expected_profit=executed_plan.expected_profit,
            order_id=execution.order_id,
            fill_source=fill_source,
            raw_price=executed_plan.raw_price,
            raw_order_cost=executed_plan.raw_order_cost,
            fee=executed_plan.fee,
            live_price_cap=execution.live_price_cap,
        ),
    )

    rows = list(csv.DictReader(log_path.open(newline="", encoding="utf-8")))

    assert execution.status == "submitted"
    assert execution.live_price_cap == pytest.approx(0.52)
    assert rows[-1]["live_price_cap"] == "0.52"


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


def test_place_live_order_skips_fok_not_filled_without_pending_state(tmp_path):
    cfg = AppConfig(live_trading_enabled=True)
    stub_clob = _FokNotFilledClobClient()
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "live.csv"

    result = place_live_order(
        cfg=cfg,
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=state_path,
        log_path=log_path,
    )

    state = load_session_state(state_path)
    rows = list(csv.DictReader(log_path.open(newline="", encoding="utf-8")))

    assert result["status"] == "skipped"
    assert result["skip_reason"] == "live_fok_not_filled"
    assert result["side"] == "UP"
    assert result["token_id"] == "up-token"
    assert len(stub_clob.created_orders) == 1
    assert len(stub_clob.posted_orders) == 1
    assert state.pending_live_slug is None
    assert state.pending_live_order_id is None
    assert rows[-1]["skip_reason"] == "live_fok_not_filled"
    assert rows[-1]["order_size"] == "0.0"
    assert rows[-1]["order_cost"] == "0.0"
    assert rows[-1]["expected_profit"] == "0.0"


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
        strategy7_max_entry_price=0.56,
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

    monkeypatch.setattr(
        "trader._entry_time_for_round",
        lambda cfg, window: datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    monkeypatch.setattr(
        "trader._resolve_side_from_strategy",
        lambda **kwargs: SideDecision(side="UP"),
    )

    result = place_live_order(
        cfg=replace(cfg, max_entry_price=0.56),
        market_client=_Strategy7LiveClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "live.csv",
    )

    assert result["status"] == "submitted"
    assert len(stub_clob.created_orders) == 1
    assert stub_clob.created_orders[0].price == pytest.approx(0.54)


def test_place_live_order_ignores_legacy_recovery_loss_for_max_stake_check(tmp_path):
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
    assert result["status"] == "submitted"
    assert "skip_reason" not in result
    assert len(stub_clob.created_orders) == 1
    assert state.round_index == 4
    assert state.recovery_loss == 0.0
    assert state.consecutive_losses == 2
    assert state.consecutive_max_stake_skips == 0
    assert state.stop_loss_count == 0


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
        },
        trade_payloads={"oid-prev": [_confirmed_trade("oid-prev")]},
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
    assert stub_clob.created_orders[0].amount == pytest.approx(1.0)

    state = load_session_state(state_path)
    assert state.recovery_loss == 0.0
    assert state.consecutive_losses == 1
    assert state.round_index == 2
    rows = list(csv.DictReader((tmp_path / "live.csv").open(newline="", encoding="utf-8")))
    settled_rows = [row for row in rows if row["event_slug"] == "btc-updown-5m-prev" and row["result"]]
    assert len(settled_rows) == 1
    assert settled_rows[0]["result"] == "DOWN"
    assert float(settled_rows[0]["trade_pnl"]) == pytest.approx(-1.035)
    assert float(settled_rows[0]["cash_pnl"]) == pytest.approx(-1.035)
    summary = summarize_paper_trades(tmp_path / "live.csv", tz_offset="+00:00")[-1]
    assert summary.trade_rows == 1
    assert summary.hit_rate == 0.0
    assert summary.total_pnl == pytest.approx(-1.035)


def test_place_live_order_settles_previous_pending_trade_from_terminal_outcome_prices_when_metadata_missing(tmp_path):
    cfg = AppConfig(live_trading_enabled=True, max_stake=25.0)
    stub_clob = _StubClobClient(
        order_payloads={
            "oid-prev": {
                "status": "filled",
                "filled_order_size": 2.0,
                "filled_order_cost": 1.0,
                "avg_price": 0.5,
            }
        },
        trade_payloads={"oid-prev": [_confirmed_trade("oid-prev")]},
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
        market_client=_TerminalPricesSettlingLiveClient(),
        clob_client=stub_clob,
        state_path=state_path,
        log_path=tmp_path / "live.csv",
    )

    assert result["status"] == "submitted"

    state = load_session_state(state_path)
    assert state.recovery_loss == 0.0
    assert state.consecutive_losses == 1
    assert state.round_index == 2
    assert state.pending_live_order_id == "oid-123"
    assert state.pending_live_slug == "btc-updown-5m-test"


def test_place_live_order_syncs_live_strategy_map_before_persisting(tmp_path):
    cfg = AppConfig(strategy_id=1, live_trading_enabled=True, max_stake=25.0)
    stub_clob = _StubClobClient(
        order_payloads={
            "oid-prev": {
                "status": "filled",
                "filled_order_size": 2.0,
                "filled_order_cost": 1.0,
                "avg_price": 0.5,
            }
        },
        trade_payloads={"oid-prev": [_confirmed_trade("oid-prev")]},
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
                "live_strategies": {
                    "3": {
                        "round_index": 8,
                        "cash_pnl": 2.5,
                        "recovery_loss": 0.0,
                        "consecutive_losses": 0,
                        "consecutive_max_stake_skips": 0,
                        "signal_round_slug": None,
                        "signal_round_open_up_price": None,
                        "signal_round_locked_side": None,
                        "strategy6_last_ofi_score": None,
                        "stop_loss_count": 0,
                        "daily_realized_pnl": 2.5,
                        "current_day": "2026-04-02",
                        "pending_live_slug": "btc-updown-5m-other",
                        "pending_live_side": "DOWN",
                        "pending_live_price": 0.4,
                        "pending_live_order_size": 5.0,
                        "pending_live_order_cost": 2.0,
                        "pending_live_expected_profit": 3.0,
                        "pending_live_order_id": "oid-other",
                        "pending_live_end_time": "2026-04-02T00:05:00+00:00",
                    },
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
                        "current_day": "2026-04-02",
                        "pending_live_slug": "btc-updown-5m-prev",
                        "pending_live_side": "UP",
                        "pending_live_price": 0.5,
                        "pending_live_order_size": 2.0,
                        "pending_live_order_cost": 1.0,
                        "pending_live_expected_profit": 1.0,
                        "pending_live_order_id": "oid-prev",
                        "pending_live_end_time": "2026-04-02T00:00:00+00:00",
                    },
                },
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

    reloaded_payload = json.loads(state_path.read_text(encoding="utf-8"))

    assert result["status"] == "submitted"
    assert reloaded_payload["pending_live_order_id"] == "oid-123"
    assert reloaded_payload["pending_live_slug"] == "btc-updown-5m-test"
    assert reloaded_payload["round_index"] == 2
    assert reloaded_payload["live_strategies"]["1"]["pending_live_order_id"] == "oid-123"
    assert reloaded_payload["live_strategies"]["1"]["pending_live_slug"] == "btc-updown-5m-test"
    assert reloaded_payload["live_strategies"]["1"]["round_index"] == 2
    assert reloaded_payload["live_strategies"]["3"]["pending_live_order_id"] == "oid-other"
    assert reloaded_payload["live_strategies"]["3"]["pending_live_slug"] == "btc-updown-5m-other"


def test_place_live_order_continues_when_previous_pending_trade_is_unresolved(tmp_path):
    cfg = AppConfig(live_trading_enabled=True, max_stake=25.0)
    stub_clob = _StubClobClient(
        order_payloads={"oid-prev": {"status": "filled"}},
        trade_payloads={"oid-prev": [_confirmed_trade("oid-prev", size=1.9607843137254901, price=0.51)]},
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
        market_client=_UnresolvedSettlingLiveClient(),
        clob_client=stub_clob,
        state_path=state_path,
        log_path=tmp_path / "live.csv",
    )

    state = load_session_state(state_path)
    assert result["status"] == "submitted"
    assert len(stub_clob.created_orders) == 1
    assert [item.event_slug for item in state.pending_live_trades] == [
        "btc-updown-5m-prev",
        "btc-updown-5m-test",
    ]


def test_run_live_trading_submits_multiple_strategies_in_same_round_when_wallet_budget_allows(tmp_path, monkeypatch):
    stop_event = threading.Event()
    stub_clob = _StubClobClient(balance_payload={"available": 3.0})

    def fake_sleep(_seconds):
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
            price=0.55,
            order_size=2.0,
            order_cost=1.5,
            expected_profit=0.5,
        ),
    )

    cfg = AppConfig(
        trade_mode="live",
        live_trading_enabled=True,
        live_private_key="pk",
        live_funder="0xfunder",
        strategy_id=1,
        live_strategy_ids=[1, 3],
        base_order_cost=1.5,
        poll_interval_seconds=1,
    )

    result = run_live_trading(
        cfg,
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
        stop_event=stop_event,
    )

    state = load_session_state(tmp_path / "live_state.json", effective_live_strategy_ids=[1, 3])

    assert result["status"] == "stopped"
    assert len(stub_clob.created_orders) == 2
    assert state.live_strategies[1].pending_live_slug == "btc-updown-5m-test"
    assert state.live_strategies[3].pending_live_slug == "btc-updown-5m-test"
    rows = (tmp_path / "live_orders.csv").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3
    assert ",1,OPEN,btc-updown-5m-test," in rows[1]
    assert ",3,OPEN,btc-updown-5m-test," in rows[2]


def test_run_live_trading_executes_inside_paper_entry_threshold(tmp_path, monkeypatch):
    stop_event = threading.Event()
    stub_clob = _StubClobClient(balance_payload={"available": 3.0})

    def fake_sleep(_seconds):
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)
    monkeypatch.setattr(
        "trader._entry_time_for_round",
        lambda cfg, window: datetime.now(timezone.utc) + timedelta(seconds=0.5),
    )
    monkeypatch.setattr(
        "trader._resolve_side_from_strategy",
        lambda **kwargs: SideDecision(side="UP"),
    )
    monkeypatch.setattr(
        "trader.build_trade_plan",
        lambda *args, **kwargs: TradePlan(
            True,
            "UP",
            price=0.55,
            order_size=2.0,
            order_cost=1.5,
            expected_profit=0.5,
        ),
    )

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=1,
            live_strategy_ids=[1],
            base_order_cost=1.5,
            poll_interval_seconds=1,
        ),
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
        stop_event=stop_event,
    )

    state = load_session_state(tmp_path / "live_state.json", effective_live_strategy_ids=[1])

    assert result["status"] == "stopped"
    assert len(stub_clob.created_orders) == 1
    assert state.live_strategies[1].pending_live_slug == "btc-updown-5m-test"


def test_run_live_trading_mirrors_same_decision_to_paper_state_and_log(tmp_path, monkeypatch):
    stop_event = threading.Event()
    stub_clob = _StubClobClient(balance_payload={"available": 3.0})

    def fake_sleep(_seconds):
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)
    monkeypatch.setattr(
        "trader._resolve_side_from_strategy",
        lambda **kwargs: SideDecision(
            side="UP",
            signal_open_up_price=0.50,
            signal_current_up_price=0.55,
            signal_delta=0.05,
            signal_edge=0.08,
            signal_probability=0.62,
            max_entry_price=0.56,
        ),
    )
    monkeypatch.setattr(
        "trader.build_trade_plan",
        lambda *args, **kwargs: TradePlan(
            True,
            "UP",
            price=0.55,
            order_size=2.0,
            order_cost=1.0,
            expected_profit=0.8181818181818181,
        ),
    )

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=10,
            live_strategy_ids=[10],
            paper_strategy_ids=[10],
            base_order_cost=1.0,
            poll_interval_seconds=1,
        ),
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
        mirror_paper_state_path=tmp_path / "paper_state.json",
        mirror_paper_log_path=tmp_path / "paper_trades.csv",
        stop_event=stop_event,
    )

    paper_state = load_session_state(tmp_path / "paper_state.json", effective_paper_strategy_ids=[10])
    live_rows = list(csv.DictReader((tmp_path / "live_orders.csv").open(newline="", encoding="utf-8")))

    assert result["status"] == "stopped"
    assert len(stub_clob.created_orders) == 1
    assert paper_state.paper_strategies[10].last_processed_paper_event_slug == "btc-updown-5m-test"
    assert len(paper_state.paper_strategies[10].pending_paper_trades) == 1
    pending = paper_state.paper_strategies[10].pending_paper_trades[0]
    assert pending.event_slug == "btc-updown-5m-test"
    assert pending.side == "UP"
    assert pending.price == pytest.approx(0.55)
    assert pending.order_cost == pytest.approx(1.0)
    assert live_rows[-1]["event_slug"] == "btc-updown-5m-test"
    assert live_rows[-1]["side"] == "UP"
    assert not (tmp_path / "paper_trades.csv").exists()


def test_run_live_trading_settles_mirrored_pending_paper_trade(tmp_path, monkeypatch):
    stop_event = threading.Event()

    def fake_sleep(_seconds):
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)

    paper_state_path = tmp_path / "paper_state.json"
    paper_log_path = tmp_path / "paper_trades.csv"
    paper_state = SessionState(
        paper_strategies={
            10: PaperStrategyState(
                round_index=1,
                pending_paper_trades=[
                    PendingPaperTrade(
                        round_index=0,
                        event_slug="btc-updown-5m-test",
                        start_time="2026-05-22T02:35:00+00:00",
                        end_time="2026-05-22T02:40:00+00:00",
                        side="UP",
                        price=0.55,
                        order_size=2.0,
                        order_cost=1.0,
                        expected_profit=0.8181818181818181,
                        strategy=10,
                        entry_timing="OPEN",
                    )
                ],
                last_processed_paper_event_slug="btc-updown-5m-test",
            )
        }
    )
    save_session_state(paper_state_path, paper_state)

    class _ResolvedMirrorClient(_LiveMarketClient):
        def get_event_by_slug(self, slug: str):
            if slug != "btc-updown-5m-test":
                raise AssertionError(f"Unexpected slug {slug}")
            return {"eventMetadata": {"priceToBeat": 100.0, "finalPrice": 101.0}}

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=10,
            live_strategy_ids=[10],
            paper_strategy_ids=[10],
            poll_interval_seconds=1,
        ),
        market_client=_ResolvedMirrorClient(),
        clob_client=_StubClobClient(),
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
        mirror_paper_state_path=paper_state_path,
        mirror_paper_log_path=paper_log_path,
        stop_event=stop_event,
    )

    reloaded = load_session_state(paper_state_path, effective_paper_strategy_ids=[10])
    rows = list(csv.DictReader(paper_log_path.open(newline="", encoding="utf-8")))

    assert result["status"] == "stopped"
    assert reloaded.paper_strategies[10].pending_paper_trades == []
    assert rows[-1]["mode"] == "paper"
    assert rows[-1]["event_slug"] == "btc-updown-5m-test"
    assert rows[-1]["result"] == "UP"
    assert float(rows[-1]["trade_pnl"]) == pytest.approx(0.9)


def test_run_live_trading_skips_fok_not_filled_without_runtime_error(tmp_path, monkeypatch):
    stop_event = threading.Event()
    stub_clob = _FokNotFilledClobClient(balance_payload={"available": 3.0})

    def fake_sleep(_seconds):
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
            price=0.55,
            order_size=2.0,
            order_cost=1.5,
            expected_profit=0.5,
        ),
    )

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=1,
            live_strategy_ids=[1],
            base_order_cost=1.5,
            poll_interval_seconds=1,
        ),
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
        stop_event=stop_event,
    )

    state = load_session_state(tmp_path / "live_state.json", effective_live_strategy_ids=[1])
    rows = list(csv.DictReader((tmp_path / "live_orders.csv").open(newline="", encoding="utf-8")))

    assert result["status"] == "stopped"
    assert len(stub_clob.created_orders) == 1
    assert state.live_strategies[1].pending_live_slug is None
    assert state.live_strategies[1].pending_live_order_id is None
    assert rows[-1]["strategy"] == "1"
    assert rows[-1]["skip_reason"] == "live_fok_not_filled"
    assert rows[-1]["order_size"] == "0.0"
    assert rows[-1]["order_cost"] == "0.0"
    assert rows[-1]["expected_profit"] == "0.0"


def test_run_live_trading_skips_transient_clob_submit_error_without_strategy_error(tmp_path, monkeypatch):
    stop_event = threading.Event()
    stub_clob = _TransientSubmitClobClient(balance_payload={"available": 3.0})

    def fake_sleep(_seconds):
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
            price=0.55,
            order_size=2.0,
            order_cost=1.5,
            expected_profit=0.5,
        ),
    )

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=7,
            live_strategy_ids=[7],
            base_order_cost=1.5,
            poll_interval_seconds=1,
        ),
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
        stop_event=stop_event,
    )

    state = load_session_state(tmp_path / "live_state.json", effective_live_strategy_ids=[7])
    rows = list(csv.DictReader((tmp_path / "live_orders.csv").open(newline="", encoding="utf-8")))

    assert result["status"] == "stopped"
    assert len(stub_clob.posted_orders) == 1
    assert state.live_strategies[7].pending_live_slug is None
    assert state.live_strategies[7].pending_live_order_id is None
    assert state.live_strategies[7].last_processed_live_event_slug == "btc-updown-5m-test"
    assert rows[-1]["strategy"] == "7"
    assert rows[-1]["skip_reason"] == "live_retryable_clob_error"


def test_run_live_trading_skips_missed_entry_window_without_submitting(tmp_path, monkeypatch):
    stop_event = threading.Event()
    stub_clob = _StubClobClient(balance_payload={"available": 3.0})

    def fake_sleep(_seconds):
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=4,
            live_strategy_ids=[4],
            open_delay_seconds=0,
            entry_grace_seconds=1,
            poll_interval_seconds=1,
        ),
        market_client=_MissedEntryNoNextLiveClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
        stop_event=stop_event,
    )

    state = load_session_state(tmp_path / "live_state.json", effective_live_strategy_ids=[4])
    rows = (tmp_path / "live_orders.csv").read_text(encoding="utf-8").splitlines()
    assert result["status"] == "stopped"
    assert stub_clob.created_orders == []
    assert state.live_strategies[4].pending_live_slug is None
    assert len(rows) == 2
    assert "entry_window_missed" in rows[1]


def test_run_live_trading_does_not_advance_same_missed_live_round_twice(tmp_path, monkeypatch):
    state_path = tmp_path / "live_state.json"
    log_path = tmp_path / "live_orders.csv"
    stub_clob = _StubClobClient(balance_payload={"available": 3.0})

    def fake_sleep(_seconds):
        active_stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)

    cfg = AppConfig(
        trade_mode="live",
        live_trading_enabled=True,
        live_private_key="pk",
        live_funder="0xfunder",
        strategy_id=4,
        live_strategy_ids=[4],
        open_delay_seconds=0,
        entry_grace_seconds=1,
        poll_interval_seconds=1,
    )

    for _ in range(2):
        active_stop_event = threading.Event()
        result = run_live_trading(
            cfg,
            market_client=_MissedEntryNoNextLiveClient(),
            clob_client=stub_clob,
            state_path=state_path,
            log_path=log_path,
            stop_event=active_stop_event,
        )
        assert result["status"] == "stopped"

    state = load_session_state(state_path, effective_live_strategy_ids=[4])
    rows = list(csv.DictReader(log_path.open(newline="", encoding="utf-8")))

    assert state.live_strategies[4].round_index == 1
    assert len(rows) == 1
    assert rows[0]["event_slug"] == "btc-updown-5m-missed"
    assert rows[0]["skip_reason"] == "entry_window_missed"


def test_run_live_trading_marks_signal_skip_round_processed_like_paper(tmp_path, monkeypatch):
    state_path = tmp_path / "live_state.json"
    log_path = tmp_path / "live_orders.csv"
    stub_clob = _StubClobClient(balance_payload={"available": 3.0})
    side_calls = 0

    def fake_sleep(_seconds):
        active_stop_event.set()

    def fake_side(**kwargs):
        nonlocal side_calls
        side_calls += 1
        return SideDecision(side=None, reason="signal_unavailable")

    monkeypatch.setattr("trader.time.sleep", fake_sleep)
    monkeypatch.setattr("trader._resolve_side_from_strategy", fake_side)

    cfg = AppConfig(
        trade_mode="live",
        live_trading_enabled=True,
        live_private_key="pk",
        live_funder="0xfunder",
        strategy_id=4,
        live_strategy_ids=[4],
        open_delay_seconds=0,
        poll_interval_seconds=1,
    )

    for _ in range(2):
        active_stop_event = threading.Event()
        result = run_live_trading(
            cfg,
            market_client=_LiveMarketClient(),
            clob_client=stub_clob,
            state_path=state_path,
            log_path=log_path,
            stop_event=active_stop_event,
        )
        assert result["status"] == "stopped"

    state = load_session_state(state_path, effective_live_strategy_ids=[4])
    rows = list(csv.DictReader(log_path.open(newline="", encoding="utf-8")))

    assert side_calls == 1
    assert state.live_strategies[4].round_index == 1
    assert state.live_strategies[4].last_processed_live_event_slug == "btc-updown-5m-test"
    assert len(rows) == 1
    assert rows[0]["event_slug"] == "btc-updown-5m-test"
    assert rows[0]["skip_reason"] == "signal_unavailable"


def test_run_live_trading_logs_candidate_price_for_signal_price_skip(tmp_path, monkeypatch):
    state_path = tmp_path / "live_state.json"
    log_path = tmp_path / "live_orders.csv"
    stub_clob = _StubClobClient(balance_payload={"available": 3.0})

    def fake_sleep(_seconds):
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)
    monkeypatch.setattr(
        "trader._resolve_side_from_strategy",
        lambda **kwargs: SideDecision(
            side=None,
            reason="strategy7_price_too_high",
            candidate_side="DOWN",
            candidate_price=0.57,
        ),
    )

    stop_event = threading.Event()
    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=7,
            live_strategy_ids=[7],
            open_delay_seconds=0,
            poll_interval_seconds=1,
        ),
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=state_path,
        log_path=log_path,
        stop_event=stop_event,
    )

    rows = list(csv.DictReader(log_path.open(newline="", encoding="utf-8")))
    assert result["status"] == "stopped"
    assert stub_clob.created_orders == []
    assert rows[0]["side"] == "SKIP"
    assert rows[0]["price"] == "0.57"
    assert rows[0]["skip_reason"] == "strategy7_price_too_high"


def test_run_live_trading_logs_observed_round_before_entry_without_submitting(tmp_path, monkeypatch):
    stop_event = threading.Event()
    stub_clob = _StubClobClient(balance_payload={"available": 3.0})

    def fake_sleep(_seconds):
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=4,
            live_strategy_ids=[4],
            open_delay_seconds=120,
            poll_interval_seconds=1,
        ),
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
        stop_event=stop_event,
    )

    assert result["status"] == "stopped"
    assert stub_clob.created_orders == []
    with (tmp_path / "live_orders.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["event_slug"] == "btc-updown-5m-test"
    assert rows[0]["strategy"] == "4"
    assert rows[0]["order_cost"] == "0.0"
    assert rows[0]["result"] == ""
    assert rows[0]["skip_reason"] == "observed_waiting_for_entry"


def test_run_live_trading_logs_current_missed_round_before_advancing_to_next(tmp_path, monkeypatch):
    stop_event = threading.Event()
    stub_clob = _StubClobClient(balance_payload={"available": 3.0})

    class _CurrentMissedNextAvailableLiveClient(_LiveMarketClient):
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
            market = super().get_market_by_slug(slug)
            market["slug"] = slug
            return market

    def fake_sleep(_seconds):
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=4,
            live_strategy_ids=[4],
            open_delay_seconds=12,
            entry_grace_seconds=5,
            poll_interval_seconds=1,
        ),
        market_client=_CurrentMissedNextAvailableLiveClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
        stop_event=stop_event,
    )

    assert result["status"] == "stopped"
    assert stub_clob.created_orders == []
    with (tmp_path / "live_orders.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["event_slug"] == "btc-updown-5m-current"
    assert rows[0]["skip_reason"] == "entry_window_missed"


def test_run_live_trading_isolates_strategy_exceptions_and_continues(tmp_path, monkeypatch):
    stop_event = threading.Event()
    stub_clob = _StubClobClient(balance_payload={"available": 3.0})

    def fake_sleep(_seconds):
        stop_event.set()

    def fake_side(**kwargs):
        strategy_id = kwargs["cfg"].strategy_id
        if strategy_id == 1:
            raise RuntimeError("strategy 1 blew up")
        return SideDecision(side="UP")

    monkeypatch.setattr("trader.time.sleep", fake_sleep)
    monkeypatch.setattr("trader._resolve_side_from_strategy", fake_side)
    monkeypatch.setattr(
        "trader.build_trade_plan",
        lambda *args, **kwargs: TradePlan(
            True,
            "UP",
            price=0.55,
            order_size=2.0,
            order_cost=1.5,
            expected_profit=0.5,
        ),
    )

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=1,
            live_strategy_ids=[1, 3],
            base_order_cost=1.5,
            open_delay_seconds=5,
            poll_interval_seconds=1,
        ),
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
        stop_event=stop_event,
    )

    state = load_session_state(tmp_path / "live_state.json", effective_live_strategy_ids=[1, 3])

    assert result["status"] == "stopped"
    assert len(stub_clob.created_orders) == 1
    assert state.live_strategies[1].pending_live_slug is None
    assert state.live_strategies[3].pending_live_slug == "btc-updown-5m-test"
    with (tmp_path / "live_orders.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert rows[0]["strategy"] == "1"
    assert rows[0]["side"] == "SKIP"
    assert rows[0]["skip_reason"] == "strategy_evaluation_error: strategy 1 blew up"
    assert rows[1]["strategy"] == "3"
    assert rows[1]["event_slug"] == "btc-updown-5m-test"


def test_run_live_trading_keeps_running_and_alerts_on_geoblock(tmp_path, monkeypatch):
    stop_event = threading.Event()
    control = RuntimeControl(initial_mode="live")

    class _GeoblockedClobClient(_StubClobClient):
        def post_order(self, order, order_type):
            raise RuntimeError(
                "[py_clob_client_v2] request error status=403 "
                "url=https://clob.polymarket.com/order "
                'body={"error":"Trading restricted in your region, please refer to available regions - '
                'https://docs.polymarket.com/developers/CLOB/geoblock"}'
            )

    def fake_sleep(_seconds):
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
            price=0.55,
            order_size=2.0,
            order_cost=1.5,
            expected_profit=0.5,
        ),
    )

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            strategy_id=3,
            live_strategy_ids=[3],
            base_order_cost=1.5,
            poll_interval_seconds=1,
        ),
        market_client=_LiveMarketClient(),
        clob_client=_GeoblockedClobClient(balance_payload={"available": 3.0}),
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
        stop_event=stop_event,
        runtime_control=control,
    )

    snapshot = control.snapshot()
    state = load_session_state(tmp_path / "live_state.json", effective_live_strategy_ids=[3])

    assert result["status"] == "stopped"
    assert state.live_strategies[3].pending_live_slug is None
    assert snapshot.safe_to_switch is True
    assert snapshot.pending_live_order is False
    assert snapshot.runtime_alert_code == "trading_restricted"
    assert snapshot.runtime_alert_level == "error"
    assert snapshot.runtime_alert_message is not None
    assert "Trading restricted" in snapshot.runtime_alert_message


def test_run_live_trading_wallet_budget_skips_later_strategy_after_earlier_submission(tmp_path, monkeypatch):
    stop_event = threading.Event()
    stub_clob = _StubClobClient(balance_payload={"available": 2.0})

    def fake_sleep(_seconds):
        stop_event.set()

    def fake_plan(*args, **kwargs):
        order_cost = kwargs["base_order_cost"]
        return TradePlan(
            True,
            "UP",
            price=0.55,
            order_size=order_cost / 0.55,
            order_cost=order_cost,
            expected_profit=0.5,
        )

    monkeypatch.setattr("trader.time.sleep", fake_sleep)
    monkeypatch.setattr(
        "trader._resolve_side_from_strategy",
        lambda **kwargs: SideDecision(side="UP"),
    )
    monkeypatch.setattr("trader.build_trade_plan", fake_plan)

    cfg = AppConfig(
        trade_mode="live",
        live_trading_enabled=True,
        live_private_key="pk",
        live_funder="0xfunder",
        strategy_id=1,
        live_strategy_ids=[1, 3],
        base_order_cost=1.5,
        poll_interval_seconds=1,
    )
    cfg.live_profiles[3].base_order_cost = 0.75

    result = run_live_trading(
        cfg,
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
        stop_event=stop_event,
    )

    state = load_session_state(tmp_path / "live_state.json", effective_live_strategy_ids=[1, 3])

    assert result["status"] == "stopped"
    assert len(stub_clob.created_orders) == 1
    assert state.live_strategies[1].pending_live_slug == "btc-updown-5m-test"
    assert state.live_strategies[3].pending_live_slug is None
    rows = (tmp_path / "live_orders.csv").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3
    assert ",1,OPEN,btc-updown-5m-test," in rows[1]
    assert ",3,OPEN,btc-updown-5m-test," in rows[2]
    assert ",UP,0.52,0.0,0.0,0.0," in rows[2]
    assert "insufficient_live_wallet_balance" in rows[2]


def test_read_available_live_balance_rejects_untrusted_or_missing_payloads():
    class _BalanceOnlyClient:
        def __init__(self, payload):
            self.payload = payload

        def get_balance(self):
            return self.payload

    bad_payloads = [
        {"balance": "12.5"},
        {"available": "n/a"},
        {},
    ]

    for payload in bad_payloads:
        with pytest.raises(RuntimeError, match="trustworthy live wallet balance"):
            read_available_live_balance(
                cfg=AppConfig(
                    trade_mode="live",
                    live_trading_enabled=True,
                    live_private_key="pk",
                    live_funder="0xfunder",
                ),
                clob_client=_BalanceOnlyClient(payload),
            )


def test_read_available_live_balance_passes_collateral_asset_type_to_v2_balance_allowance():
    class _V2BalanceClient:
        def __init__(self):
            self.params = None

        def get_balance_allowance(self, params):
            self.params = params
            if getattr(params, "asset_type", None) != "COLLATERAL":
                raise AssertionError("expected COLLATERAL asset_type")
            return {"available": 12.5}

    client = _V2BalanceClient()

    balance = read_available_live_balance(
        cfg=AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
        ),
        clob_client=client,
    )

    assert balance == 12.5
    assert client.params is not None


def test_read_available_live_balance_handles_v2_balance_allowance_payload():
    class _V2BalanceClient:
        def get_balance_allowance(self, _params):
            return {
                "balance": "12500000",
                "allowances": {
                    "0xexchange": "8000000",
                    "0xexchangeV2": "9250000",
                },
            }

    balance = read_available_live_balance(
        cfg=AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
        ),
        clob_client=_V2BalanceClient(),
    )

    assert balance == 9.25


def test_read_available_live_balance_accepts_zero_v2_balance_allowance_payload():
    class _V2BalanceClient:
        def get_balance_allowance(self, _params):
            return {
                "balance": "0",
                "allowances": {
                    "0xexchange": "0",
                    "0xexchangeV2": "0",
                },
            }

    balance = read_available_live_balance(
        cfg=AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
        ),
        clob_client=_V2BalanceClient(),
    )

    assert balance == 0.0


def test_settle_pending_live_trade_operates_on_single_strategy_state():
    pending_strategy = LiveStrategyState(
        round_index=1,
        cash_pnl=0.0,
        recovery_loss=0.0,
        consecutive_losses=0,
        pending_live_slug="btc-updown-5m-prev",
        pending_live_side="UP",
        pending_live_price=0.5,
        pending_live_order_size=2.0,
        pending_live_order_cost=1.0,
        pending_live_expected_profit=1.0,
        pending_live_order_id="oid-prev",
        pending_live_end_time="2026-04-02T00:00:00+00:00",
    )
    untouched_strategy = LiveStrategyState(
        pending_live_slug="btc-updown-5m-other",
        pending_live_order_id="oid-other",
    )
    stub_clob = _StubClobClient(
        order_payloads={
            "oid-prev": {
                "status": "filled",
                "filled_order_size": 2.0,
                "filled_order_cost": 1.0,
                "avg_price": 0.5,
            }
        },
        trade_payloads={"oid-prev": [_confirmed_trade("oid-prev")]},
    )

    updated_strategy, status, settled = trader._settle_pending_live_trade_if_needed(
        market_client=_SettlingLiveClient(),
        clob_client=stub_clob,
        strategy_state=pending_strategy,
        now=datetime(2026, 4, 2, 0, 6, tzinfo=timezone.utc),
    )

    assert settled is True
    assert status is not None
    assert status["status"] == "settled"
    assert status["slug"] == "btc-updown-5m-prev"
    assert status["side"] == "UP"
    assert status["price"] == pytest.approx(0.5175)
    assert status["order_size"] == pytest.approx(2.0)
    assert status["order_cost"] == pytest.approx(1.035)
    assert status["expected_profit"] == pytest.approx(0.965)
    assert status["result"] == "DOWN"
    assert status["trade_pnl"] == pytest.approx(-1.035)
    assert status["raw_price"] == pytest.approx(0.5)
    assert status["raw_order_cost"] == pytest.approx(1.0)
    assert status["fee"] == pytest.approx(0.035)
    assert updated_strategy.round_index == 1
    assert updated_strategy.cash_pnl == pytest.approx(-1.035)
    assert updated_strategy.recovery_loss == 0.0
    assert updated_strategy.consecutive_losses == 1
    assert updated_strategy.pending_live_slug is None
    assert updated_strategy.pending_live_order_id is None
    assert untouched_strategy.pending_live_slug == "btc-updown-5m-other"
    assert untouched_strategy.pending_live_order_id == "oid-other"


def test_settle_pending_live_trade_uses_official_redeemable_position_without_waiting_for_terminal_prices():
    pending_strategy = LiveStrategyState(
        round_index=1,
        cash_pnl=0.0,
        recovery_loss=0.0,
        consecutive_losses=0,
        pending_live_slug="btc-updown-5m-prev",
        pending_live_side="UP",
        pending_live_price=0.5,
        pending_live_order_size=2.0,
        pending_live_order_cost=1.0,
        pending_live_expected_profit=1.0,
        pending_live_order_id="oid-prev",
        pending_live_end_time="2026-04-02T00:00:00+00:00",
    )
    stub_clob = _StubClobClient(
        order_payloads={
            "oid-prev": {
                "status": "filled",
                "filled_order_size": 2.0,
                "filled_order_cost": 1.0,
                "avg_price": 0.5,
            }
        },
        trade_payloads={"oid-prev": [_confirmed_trade("oid-prev")]},
    )

    updated_strategy, status, settled = trader._settle_pending_live_trade_if_needed(
        market_client=_ResolvedPositionSettlingLiveClient(),
        clob_client=stub_clob,
        strategy_state=pending_strategy,
        now=datetime(2026, 4, 2, 0, 6, tzinfo=timezone.utc),
        funder="0xfunder",
    )

    assert settled is True
    assert status["status"] == "settled"
    assert status["result"] == "UP"
    assert status["trade_pnl"] == pytest.approx(0.965)
    assert updated_strategy.cash_pnl == pytest.approx(0.965)
    assert updated_strategy.pending_live_slug is None
    assert updated_strategy.pending_live_order_id is None


def test_settle_pending_live_trade_ignores_string_false_redeemable_position():
    class _StringFalseRedeemableClient(_LiveMarketClient):
        def get_event_by_slug(self, slug: str):
            if slug != "btc-updown-5m-prev":
                raise AssertionError(f"Unexpected slug {slug}")
            return {
                "closed": True,
                "eventMetadata": {"priceToBeat": 100.0},
                "markets": [
                    {
                        "closed": True,
                        "outcomes": '["Up","Down"]',
                        "outcomePrices": '["0.49","0.51"]',
                    }
                ],
            }

        def get_current_positions(self, *, user: str, redeemable: bool | None = None):
            assert user == "0xfunder"
            assert redeemable is True
            return [
                {
                    "proxyWallet": "0xfunder",
                    "eventSlug": "btc-updown-5m-prev",
                    "outcome": "Up",
                    "size": "2.0",
                    "redeemable": "false",
                }
            ]

    pending_strategy = LiveStrategyState(
        round_index=1,
        cash_pnl=0.0,
        recovery_loss=0.0,
        consecutive_losses=0,
        pending_live_slug="btc-updown-5m-prev",
        pending_live_side="UP",
        pending_live_price=0.5,
        pending_live_order_size=2.0,
        pending_live_order_cost=1.0,
        pending_live_expected_profit=1.0,
        pending_live_order_id="oid-prev",
        pending_live_end_time="2026-04-02T00:00:00+00:00",
    )
    stub_clob = _StubClobClient(
        order_payloads={
            "oid-prev": {
                "status": "filled",
                "filled_order_size": 2.0,
                "filled_order_cost": 1.0,
                "avg_price": 0.5,
            }
        },
        trade_payloads={"oid-prev": [_confirmed_trade("oid-prev")]},
    )

    updated_strategy, status, settled = trader._settle_pending_live_trade_if_needed(
        market_client=_StringFalseRedeemableClient(),
        clob_client=stub_clob,
        strategy_state=pending_strategy,
        now=datetime(2026, 4, 2, 0, 6, tzinfo=timezone.utc),
        funder="0xfunder",
    )

    assert settled is True
    assert status["status"] == "provisional_loss"
    assert status["result"] == "PROVISIONAL_LOSS"
    assert status["trade_pnl"] == pytest.approx(-1.035)
    assert updated_strategy.cash_pnl == pytest.approx(-1.035)
    assert updated_strategy.recovery_loss == 0.0
    assert updated_strategy.consecutive_losses == 1
    assert updated_strategy.pending_live_slug is None


def test_settle_pending_live_trade_uses_official_terminal_result_when_position_is_not_redeemable():
    pending_strategy = LiveStrategyState(
        round_index=1,
        cash_pnl=0.0,
        recovery_loss=0.0,
        consecutive_losses=0,
        pending_live_slug="btc-updown-5m-prev",
        pending_live_side="UP",
        pending_live_price=0.5,
        pending_live_order_size=2.0,
        pending_live_order_cost=1.0,
        pending_live_expected_profit=1.0,
        pending_live_order_id="oid-prev",
        pending_live_end_time="2026-04-02T00:00:00+00:00",
    )
    stub_clob = _StubClobClient(
        order_payloads={
            "oid-prev": {
                "status": "filled",
                "filled_order_size": 2.0,
                "filled_order_cost": 1.0,
                "avg_price": 0.5,
            }
        },
        trade_payloads={"oid-prev": [_confirmed_trade("oid-prev")]},
    )

    updated_strategy, status, settled = trader._settle_pending_live_trade_if_needed(
        market_client=_OfficialTerminalResultNoPositionClient(),
        clob_client=stub_clob,
        strategy_state=pending_strategy,
        now=datetime(2026, 4, 2, 0, 6, tzinfo=timezone.utc),
        funder="0xfunder",
    )

    assert settled is True
    assert status["status"] == "settled"
    assert status["result"] == "DOWN"
    assert status["trade_pnl"] == pytest.approx(-1.035)
    assert updated_strategy.cash_pnl == pytest.approx(-1.035)
    assert updated_strategy.pending_live_slug is None
    assert updated_strategy.pending_live_order_id is None


def test_settle_pending_live_trade_uses_frozen_pending_plan_when_official_result_is_ready_before_fill_lookup():
    pending_strategy = LiveStrategyState(
        round_index=4,
        cash_pnl=0.0,
        recovery_loss=0.0,
        consecutive_losses=0,
        pending_live_slug="btc-updown-5m-prev",
        pending_live_side="UP",
        pending_live_price=0.51,
        pending_live_order_size=1.9607843137254901,
        pending_live_order_cost=1.0,
        pending_live_expected_profit=0.9607843137254901,
        pending_live_order_id="oid-prev",
        pending_live_end_time="2026-04-02T00:00:00+00:00",
    )
    stub_clob = _StubClobClient(
        order_payloads={"oid-prev": {"status": "filled"}},
        trade_payloads={"oid-prev": [_confirmed_trade("oid-prev", size=1.9607843137254901, price=0.51)]},
    )

    updated_strategy, status, settled = trader._settle_pending_live_trade_if_needed(
        market_client=_OfficialTerminalResultNoPositionClient(),
        clob_client=stub_clob,
        strategy_state=pending_strategy,
        now=datetime(2026, 4, 2, 0, 6, tzinfo=timezone.utc),
        funder="0xfunder",
    )

    assert settled is True
    assert status["status"] == "settled"
    assert status["result"] == "DOWN"
    assert status["trade_pnl"] == pytest.approx(-1.0343)
    assert status["order_cost"] == pytest.approx(1.0343)
    assert updated_strategy.cash_pnl == pytest.approx(-1.0343)
    assert updated_strategy.recovery_loss == 0.0
    assert updated_strategy.consecutive_losses == 1
    assert updated_strategy.pending_live_slug is None
    assert updated_strategy.pending_live_order_id is None


def test_settle_pending_live_trade_uses_official_market_endpoint_when_event_endpoint_lags():
    pending_strategy = LiveStrategyState(
        round_index=4,
        cash_pnl=0.0,
        recovery_loss=0.0,
        consecutive_losses=0,
        pending_live_slug="btc-updown-5m-prev",
        pending_live_side="UP",
        pending_live_price=0.5,
        pending_live_order_size=2.0,
        pending_live_order_cost=1.0,
        pending_live_expected_profit=1.0,
        pending_live_order_id="oid-prev",
        pending_live_end_time="2026-04-02T00:00:00+00:00",
    )
    stub_clob = _StubClobClient(
        order_payloads={
            "oid-prev": {
                "status": "filled",
                "filled_order_size": 2.0,
                "filled_order_cost": 1.0,
                "avg_price": 0.5,
            }
        },
        trade_payloads={"oid-prev": [_confirmed_trade("oid-prev")]},
    )

    updated_strategy, status, settled = trader._settle_pending_live_trade_if_needed(
        market_client=_OfficialMarketEndpointFallbackClient(),
        clob_client=stub_clob,
        strategy_state=pending_strategy,
        now=datetime(2026, 4, 2, 0, 6, tzinfo=timezone.utc),
        funder="0xfunder",
    )

    assert settled is True
    assert status["status"] == "settled"
    assert status["result"] == "DOWN"
    assert status["trade_pnl"] == pytest.approx(-1.035)
    assert updated_strategy.cash_pnl == pytest.approx(-1.035)
    assert updated_strategy.pending_live_slug is None
    assert updated_strategy.pending_live_order_id is None


def test_settle_pending_live_trade_waits_for_final_price_when_price_to_beat_exists():
    pending_strategy = LiveStrategyState(
        round_index=4,
        cash_pnl=0.0,
        recovery_loss=0.0,
        consecutive_losses=0,
        pending_live_slug="btc-updown-5m-prev",
        pending_live_side="UP",
        pending_live_price=0.5,
        pending_live_order_size=2.0,
        pending_live_order_cost=1.0,
        pending_live_expected_profit=1.0,
        pending_live_order_id="oid-prev",
        pending_live_end_time="2026-04-02T00:00:00+00:00",
    )
    stub_clob = _StubClobClient(
        order_payloads={
            "oid-prev": {
                "status": "filled",
                "filled_order_size": 2.0,
                "filled_order_cost": 1.0,
                "avg_price": 0.5,
            }
        },
        trade_payloads={"oid-prev": [_confirmed_trade("oid-prev")]},
    )

    updated_strategy, status, settled = trader._settle_pending_live_trade_if_needed(
        market_client=_TerminalPricesWithoutFinalPriceClient(),
        clob_client=stub_clob,
        strategy_state=pending_strategy,
        now=datetime(2026, 4, 2, 0, 6, tzinfo=timezone.utc),
        funder="0xfunder",
    )

    assert settled is True
    assert status["status"] == "provisional_loss"
    assert status["result"] == "PROVISIONAL_LOSS"
    assert status["trade_pnl"] == pytest.approx(-1.035)
    assert updated_strategy.cash_pnl == pytest.approx(-1.035)
    assert updated_strategy.recovery_loss == 0.0
    assert updated_strategy.consecutive_losses == 1
    assert updated_strategy.pending_live_slug is None


def test_settle_pending_live_trade_waits_for_final_price_before_provisional_deadline():
    pending_strategy = LiveStrategyState(
        round_index=4,
        cash_pnl=0.0,
        recovery_loss=0.0,
        consecutive_losses=0,
        pending_live_slug="btc-updown-5m-prev",
        pending_live_side="UP",
        pending_live_price=0.5,
        pending_live_order_size=2.0,
        pending_live_order_cost=1.0,
        pending_live_expected_profit=1.0,
        pending_live_order_id="oid-prev",
        pending_live_end_time="2026-04-02T00:00:00+00:00",
    )
    stub_clob = _StubClobClient(
        order_payloads={
            "oid-prev": {
                "status": "filled",
                "filled_order_size": 2.0,
                "filled_order_cost": 1.0,
                "avg_price": 0.5,
            }
        },
        trade_payloads={"oid-prev": [_confirmed_trade("oid-prev")]},
    )

    updated_strategy, status, settled = trader._settle_pending_live_trade_if_needed(
        market_client=_TerminalPricesWithoutFinalPriceClient(),
        clob_client=stub_clob,
        strategy_state=pending_strategy,
        now=datetime(2026, 4, 2, 0, 0, 3, tzinfo=timezone.utc),
        funder="0xfunder",
        final_price_wait_seconds=4.0,
    )

    assert settled is False
    assert status["status"] == "pending_settlement"
    assert status["skip_reason"] == "awaiting_final_price"
    assert status["final_price_deadline"] == "2026-04-02T00:00:04+00:00"
    assert updated_strategy.pending_live_slug == "btc-updown-5m-prev"
    assert updated_strategy.cash_pnl == pytest.approx(0.0)


def test_settle_pending_live_trade_does_not_use_ws_only_resolution_for_live_settlement():
    pending_strategy = LiveStrategyState(
        round_index=4,
        cash_pnl=0.0,
        recovery_loss=0.0,
        consecutive_losses=0,
        pending_live_slug="btc-updown-5m-prev",
        pending_live_side="DOWN",
        pending_live_price=0.38,
        pending_live_order_size=6.315788,
        pending_live_order_cost=2.39999944,
        pending_live_expected_profit=3.91578856,
        pending_live_order_id="oid-prev",
        pending_live_end_time="2026-04-02T00:00:00+00:00",
    )
    stub_clob = _StubClobClient(
        order_payloads={
            "oid-prev": {
                "status": "filled",
                "filled_order_size": 6.315788,
                "filled_order_cost": 2.39999944,
                "avg_price": 0.38,
            }
        },
        trade_payloads={"oid-prev": [_confirmed_trade("oid-prev", size=6.315788, price=0.38)]},
    )

    updated_strategy, status, settled = trader._settle_pending_live_trade_if_needed(
        market_client=_WsOnlyResolutionLiveClient(),
        clob_client=stub_clob,
        strategy_state=pending_strategy,
        now=datetime(2026, 4, 2, 0, 6, tzinfo=timezone.utc),
        funder="0xfunder",
    )

    assert settled is False
    assert status["status"] == "pending_settlement"
    assert status["skip_reason"] == "round_unresolved"
    assert updated_strategy.pending_live_slug == "btc-updown-5m-prev"
    assert updated_strategy.cash_pnl == pytest.approx(0.0)


def test_settle_pending_live_trade_prefers_official_market_result_over_position_snapshot():
    pending_strategy = LiveStrategyState(
        round_index=1,
        cash_pnl=0.0,
        recovery_loss=0.0,
        consecutive_losses=0,
        pending_live_slug="btc-updown-5m-prev",
        pending_live_side="UP",
        pending_live_price=0.5,
        pending_live_order_size=2.0,
        pending_live_order_cost=1.0,
        pending_live_expected_profit=1.0,
        pending_live_order_id="oid-prev",
        pending_live_end_time="2026-04-02T00:00:00+00:00",
    )
    stub_clob = _StubClobClient(
        order_payloads={
            "oid-prev": {
                "status": "filled",
                "filled_order_size": 2.0,
                "filled_order_cost": 1.0,
                "avg_price": 0.5,
            }
        },
        trade_payloads={"oid-prev": [_confirmed_trade("oid-prev")]},
    )

    updated_strategy, status, settled = trader._settle_pending_live_trade_if_needed(
        market_client=_ConflictingPositionAndOfficialResultClient(),
        clob_client=stub_clob,
        strategy_state=pending_strategy,
        now=datetime(2026, 4, 2, 0, 6, tzinfo=timezone.utc),
        funder="0xfunder",
    )

    assert settled is True
    assert status["result"] == "DOWN"
    assert status["trade_pnl"] == pytest.approx(-1.035)
    assert updated_strategy.cash_pnl == pytest.approx(-1.035)
    assert updated_strategy.pending_live_slug is None


def test_place_live_order_requires_private_key_without_injected_client(tmp_path):
    cfg = AppConfig(live_trading_enabled=True)

    with pytest.raises(RuntimeError, match="PRIVATE_KEY"):
        place_live_order(
            cfg=cfg,
            market_client=_LiveMarketClient(),
            state_path=tmp_path / "state.json",
            log_path=tmp_path / "live.csv",
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


def test_strategy7_skips_without_error_when_history_anchor_lookup_fails():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        binance_signal_stale_seconds=2.0,
    )
    state = SessionState(round_index=0)
    window = MarketWindow(
        event_id="evt-1",
        market_id="mkt-1",
        slug="s1",
        title="BTC Up or Down",
        start_time=now - timedelta(seconds=45),
        end_time=now + timedelta(minutes=4),
        up_token_id="up-token",
        down_token_id="down-token",
    )
    quote = MarketQuote(
        slug="s1",
        up_price=0.54,
        up_best_ask=0.54,
        strategy6_ofi_score=0.8,
        fetched_at=now,
        strategy6_signal_at=now,
    )

    class FailingHistoryClient:
        def get_nearest_history_point(self, *args, **kwargs):
            raise RuntimeError("prices-history ssl eof")

        def get_price_history(self, *args, **kwargs):
            return {"history": []}

    decision = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        market_client=FailingHistoryClient(),
        window=window,
        now=now,
    )

    assert decision.side is None
    assert decision.reason == "strategy7_momentum_too_weak"
    assert state.signal_round_open_up_price == pytest.approx(0.54)


def test_strategy7_uses_base_threshold_when_dynamic_history_lookup_fails():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_min_signal_gap=0.01,
        strategy7_confirm_before_entry_seconds=0,
        binance_signal_stale_seconds=2.0,
    )
    state = SessionState(round_index=0, signal_round_slug="s1", signal_round_open_up_price=0.50)
    window = MarketWindow(
        event_id="evt-1",
        market_id="mkt-1",
        slug="s1",
        title="BTC Up or Down",
        start_time=now - timedelta(seconds=45),
        end_time=now + timedelta(minutes=4),
        up_token_id="up-token",
        down_token_id="down-token",
    )
    quote = MarketQuote(
        slug="s1",
        up_price=0.54,
        up_best_ask=0.54,
        strategy6_ofi_score=0.8,
        fetched_at=now,
        strategy6_signal_at=now,
    )

    class FailingHistoryClient:
        def get_price_history(self, *args, **kwargs):
            raise RuntimeError("prices-history ssl eof")

    decision = _resolve_side_from_strategy(
        cfg=cfg,
        state=state,
        slug="s1",
        quote=quote,
        market_client=FailingHistoryClient(),
        window=window,
        now=now,
        entry_time=now + timedelta(seconds=10),
    )

    assert decision.side == "UP"
    assert decision.signal_threshold == pytest.approx(0.02)
    assert decision.signal_delta == pytest.approx(0.04)


def test_strategy7_keeps_locked_side_when_revalidation_conflicts():
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

    assert second.side is None
    assert second.reason == "strategy7_signal_conflict"
    assert second.signal_delta == pytest.approx(-0.05)
    assert second.signal_locked is True
    assert state.signal_round_locked_side == "UP"


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
    assert second.candidate_side == "UP"
    assert second.candidate_price == pytest.approx(0.58)
    assert second.signal_locked is True
    assert state.signal_round_locked_side == "UP"


def test_strategy7_skips_when_signals_conflict():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_min_signal_gap=0.01,
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
    assert state.signal_round_locked_side is None


def test_strategy7_skips_when_confirmation_is_too_late():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_min_signal_gap=0.01,
        strategy7_confirm_before_entry_seconds=15,
    )
    state = SessionState(round_index=0, signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.56,
        up_best_ask=0.56,
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


def test_strategy7_strong_signal_can_pass_with_late_confirm_relaxation():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_min_signal_gap=0.01,
        strategy7_max_entry_price=0.56,
        strategy7_confirm_before_entry_seconds=15,
        strategy7_late_confirm_strong_signal_gap=0.01,
        strategy7_late_confirm_relax_seconds=6,
    )
    state = SessionState(round_index=0, signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.54,
        up_best_ask=0.54,
        strategy6_ofi_score=0.80,
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

    assert decision.side == "UP"
    assert decision.reason is None


def test_strategy7_strong_signal_still_skips_when_relaxation_is_zero():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_min_signal_gap=0.01,
        strategy7_max_entry_price=0.56,
        strategy7_confirm_before_entry_seconds=15,
        strategy7_late_confirm_strong_signal_gap=0.01,
        strategy7_late_confirm_relax_seconds=0,
    )
    state = SessionState(round_index=0, signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.54,
        up_best_ask=0.54,
        strategy6_ofi_score=0.80,
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


def test_strategy7_late_confirm_relaxation_requires_current_signal_quality():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_min_signal_gap=0.03,
        strategy7_max_entry_price=0.56,
        strategy7_confirm_before_entry_seconds=15,
        strategy7_late_confirm_strong_signal_gap=0.01,
        strategy7_late_confirm_relax_seconds=6,
    )
    state = SessionState(round_index=0, signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.54,
        up_best_ask=0.54,
        strategy6_ofi_score=0.67,
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
    assert decision.reason == "strategy7_confidence_too_low"


def test_strategy7_clamps_confirmation_window_to_available_open_entry_window():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=7,
        open_delay_seconds=5,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_max_entry_price=0.56,
        strategy7_min_signal_gap=0.01,
        strategy7_confirm_before_entry_seconds=12,
        binance_signal_stale_seconds=2.0,
    )
    window = MarketWindow(
        event_id="evt-s1",
        market_id="mkt-s1",
        slug="s1",
        title="Strategy 7 Open Window Clamp",
        start_time=now,
        end_time=now + timedelta(minutes=15),
        up_token_id="up",
        down_token_id="down",
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
        window=window,
        now=now,
        entry_time=now + timedelta(seconds=cfg.open_delay_seconds),
    )

    assert decision.side == "UP"


def test_strategy8_returns_trend_side_when_ofi_and_momentum_agree():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=8,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_min_signal_gap=0.01,
        strategy7_max_entry_price=0.56,
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


def test_strategy8_returns_reversal_side_when_ofi_and_momentum_conflict_strongly():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=8,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_min_signal_gap=0.01,
        strategy7_max_entry_price=0.56,
        strategy7_confirm_before_entry_seconds=12,
        binance_signal_stale_seconds=2.0,
    )
    state = SessionState(round_index=0, signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.46,
        down_price=0.54,
        down_best_ask=0.54,
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
    assert decision.reason == "strategy8_conflict_reversal"


def test_strategy8_skips_when_market_state_is_weak():
    now = datetime.now(timezone.utc)
    cfg = AppConfig(
        strategy_id=8,
        strategy7_ofi_threshold=0.65,
        strategy7_momentum_threshold=0.02,
        strategy7_min_signal_gap=0.01,
    )
    state = SessionState(round_index=0, signal_round_slug="s1", signal_round_open_up_price=0.50)
    quote = MarketQuote(
        slug="s1",
        up_price=0.51,
        up_best_ask=0.51,
        strategy6_ofi_score=0.66,
        fetched_at=now,
        strategy6_signal_at=now,
    )

    decision = _resolve_side_from_strategy(cfg=cfg, state=state, slug="s1", quote=quote, now=now)

    assert decision.side is None
    assert decision.reason == "strategy8_market_state_weak"


def test_poll_interval_uses_base_when_no_target_round():
    cfg = AppConfig()
    now = datetime.now(timezone.utc)

    assert trader._poll_interval_for_target_round(
        cfg=cfg,
        now=now,
        target_round=None,
    ) == pytest.approx(cfg.poll_interval_seconds)


def test_poll_interval_switches_to_fast_window_near_entry_target_round():
    now = datetime(2026, 4, 22, 1, 29, 18, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="evt-near-entry",
        market_id="mkt-near-entry",
        slug="btc-updown-15m-near-entry",
        title="Near Entry Window",
        start_time=datetime(2026, 4, 22, 1, 29, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 22, 1, 44, 0, tzinfo=timezone.utc),
        up_token_id="up",
        down_token_id="down",
    )
    cfg = AppConfig(
        open_delay_seconds=25,
        near_entry_poll_window_seconds=10,
        fast_poll_interval_seconds=1,
    )

    assert trader._poll_interval_for_target_round(
        cfg=cfg,
        now=now,
        target_round=window,
    ) == pytest.approx(1.0)


def test_poll_interval_stays_base_after_entry_window_is_missed_target_round():
    now = datetime(2026, 4, 22, 1, 29, 31, tzinfo=timezone.utc)
    window = MarketWindow(
        event_id="evt-missed-entry",
        market_id="mkt-missed-entry",
        slug="btc-updown-15m-missed-entry",
        title="Missed Entry Window",
        start_time=datetime(2026, 4, 22, 1, 29, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 4, 22, 1, 44, 0, tzinfo=timezone.utc),
        up_token_id="up",
        down_token_id="down",
    )
    cfg = AppConfig(
        open_delay_seconds=25,
        entry_grace_seconds=5,
        near_entry_poll_window_seconds=10,
        fast_poll_interval_seconds=1,
    )

    assert trader._poll_interval_for_target_round(
        cfg=cfg,
        now=now,
        target_round=window,
    ) == pytest.approx(cfg.poll_interval_seconds)


def test_live_pending_settlement_uses_fast_poll_interval():
    cfg = AppConfig(poll_interval_seconds=5, fast_poll_interval_seconds=1)

    assert trader._poll_interval_for_live_result(
        cfg=cfg,
        result={"status": "pending_settlement"},
    ) == pytest.approx(1)
    assert trader._poll_interval_for_live_result(
        cfg=cfg,
        result={"status": "waiting_for_entry"},
    ) == pytest.approx(5)


def test_live_pending_final_price_uses_final_price_poll_interval():
    cfg = AppConfig(
        poll_interval_seconds=5,
        fast_poll_interval_seconds=1,
        final_price_poll_interval_seconds=0.75,
    )

    assert trader._poll_interval_for_live_result(
        cfg=cfg,
        result={
            "status": "pending_settlement",
            "skip_reason": "awaiting_final_price",
        },
    ) == pytest.approx(0.75)


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


def test_append_trade_log_updates_existing_live_trade_when_result_arrives(tmp_path):
    log_path = tmp_path / "live_orders.csv"
    start = datetime(2026, 4, 28, 5, 30, tzinfo=timezone.utc)
    end = start + timedelta(minutes=15)

    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=start + timedelta(seconds=30),
            mode="live",
            round_index=20,
            strategy=7,
            entry_timing="OPEN",
            event_slug="btc-updown-15m-1777354200",
            start_time=start,
            end_time=end,
            side="UP",
            price=0.38,
            order_size=4.222010080645161,
            order_cost=1.604363830645161,
            expected_profit=2.61764625,
        ),
    )
    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=end + timedelta(minutes=3),
            mode="live",
            round_index=21,
            strategy=7,
            entry_timing="OPEN",
            event_slug="btc-updown-15m-1777354200",
            start_time=start,
            end_time=end,
            side="UP",
            price=0.55,
            order_size=4.210525,
            order_cost=2.31578875,
            expected_profit=1.89473625,
            result="DOWN",
            trade_pnl=-2.31578875,
            cash_pnl=-2.90516175,
            recovery_loss=3.933435,
            consecutive_losses=2,
        ),
    )

    rows = list(csv.DictReader(log_path.open(newline="", encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["timestamp"] == "2026-04-28T05:30:30+00:00"
    assert rows[0]["round_index"] == "20"
    assert rows[0]["result"] == "DOWN"
    assert float(rows[0]["trade_pnl"]) == pytest.approx(-2.31578875)


def test_append_trade_log_updates_live_observed_placeholder(tmp_path):
    log_path = tmp_path / "live_orders.csv"
    start = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=5)

    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=start,
            mode="live",
            round_index=1,
            strategy=4,
            entry_timing="OPEN",
            event_slug="btc-updown-5m-test",
            start_time=start,
            end_time=end,
            side="SKIP",
            price=None,
            order_size=0.0,
            order_cost=0.0,
            expected_profit=0.0,
            result=None,
            trade_pnl=0.0,
            cash_pnl=-1.0,
            recovery_loss=1.0,
            consecutive_losses=1,
            skip_reason="observed_waiting_for_entry",
        ),
    )
    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=start + timedelta(seconds=12),
            mode="live",
            round_index=1,
            strategy=4,
            entry_timing="OPEN",
            event_slug="btc-updown-5m-test",
            start_time=start,
            end_time=end,
            side="UP",
            price=0.49,
            order_size=2.0,
            order_cost=1.0,
            expected_profit=1.0,
            result=None,
            trade_pnl=0.0,
            cash_pnl=-1.0,
            recovery_loss=1.0,
            consecutive_losses=1,
        ),
    )

    rows = list(csv.DictReader(log_path.open(newline="", encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["timestamp"] == start.isoformat()
    assert rows[0]["side"] == "UP"
    assert rows[0]["price"] == "0.49"
    assert rows[0]["order_cost"] == "1.0"
    assert rows[0]["skip_reason"] == ""


def test_append_trade_log_keeps_live_observed_placeholder_stable_during_waiting_updates(tmp_path):
    log_path = tmp_path / "live_orders.csv"
    start = datetime(2026, 5, 1, 8, 45, tzinfo=timezone.utc)
    end = start + timedelta(minutes=5)

    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=start,
            mode="live",
            round_index=1,
            strategy=4,
            entry_timing="OPEN",
            event_slug="btc-updown-5m-waiting",
            start_time=start,
            end_time=end,
            side="SKIP",
            price=None,
            order_size=0.0,
            order_cost=0.0,
            expected_profit=0.0,
            result=None,
            trade_pnl=0.0,
            cash_pnl=-5.1824,
            recovery_loss=2.0,
            consecutive_losses=1,
            skip_reason="observed_waiting_for_entry",
        ),
    )
    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=start + timedelta(seconds=3),
            mode="live",
            round_index=1,
            strategy=4,
            entry_timing="OPEN",
            event_slug="btc-updown-5m-waiting",
            start_time=start,
            end_time=end,
            side="DOWN",
            price=0.54,
            order_size=0.0,
            order_cost=0.0,
            expected_profit=0.0,
            result=None,
            trade_pnl=0.0,
            cash_pnl=-5.1824,
            recovery_loss=2.0,
            consecutive_losses=1,
            skip_reason="observed_waiting_for_entry",
        ),
    )

    rows = list(csv.DictReader(log_path.open(newline="", encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["timestamp"] == start.isoformat()
    assert rows[0]["side"] == "SKIP"
    assert rows[0]["price"] == ""
    assert rows[0]["order_cost"] == "0.0"
    assert rows[0]["skip_reason"] == "observed_waiting_for_entry"


def test_append_trade_log_replaces_live_observed_placeholder_with_final_skip(tmp_path):
    log_path = tmp_path / "live_orders.csv"
    start = datetime(2026, 5, 1, 8, 45, tzinfo=timezone.utc)
    end = start + timedelta(minutes=5)

    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=start,
            mode="live",
            round_index=1,
            strategy=4,
            entry_timing="OPEN",
            event_slug="btc-updown-5m-skip",
            start_time=start,
            end_time=end,
            side="SKIP",
            price=None,
            order_size=0.0,
            order_cost=0.0,
            expected_profit=0.0,
            result=None,
            trade_pnl=0.0,
            cash_pnl=-5.1824,
            recovery_loss=2.0,
            consecutive_losses=1,
            skip_reason="observed_waiting_for_entry",
        ),
    )
    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=start + timedelta(seconds=12),
            mode="live",
            round_index=1,
            strategy=4,
            entry_timing="OPEN",
            event_slug="btc-updown-5m-skip",
            start_time=start,
            end_time=end,
            side="DOWN",
            price=0.36,
            order_size=0.0,
            order_cost=0.0,
            expected_profit=0.0,
            result=None,
            trade_pnl=0.0,
            cash_pnl=-5.1824,
            recovery_loss=2.0,
            consecutive_losses=1,
            skip_reason="price_below_threshold",
        ),
    )

    rows = list(csv.DictReader(log_path.open(newline="", encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["side"] == "DOWN"
    assert rows[0]["price"] == "0.36"
    assert rows[0]["order_cost"] == "0.0"
    assert rows[0]["skip_reason"] == "price_below_threshold"


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
        client=_MissedEntryNoNextLiveClient(),
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


def test_run_paper_trading_logs_current_round_skip_before_advancing_to_next_round(tmp_path, monkeypatch):
    cfg = AppConfig(strategy_id=2, paper_strategy_ids=[2])
    monkeypatch.setattr("trader._sleep_until_round_end", lambda cfg, window, stop_event=None: False)

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

        def get_market_by_slug(self, slug: str):
            market = super().get_market_by_slug(slug)
            market["slug"] = slug
            return market

    log_path = tmp_path / "paper.csv"
    state_path = tmp_path / "state.json"

    result = run_paper_trading(
        cfg,
        client=_CurrentMissedNextAvailableClient(),
        state_path=state_path,
        log_path=log_path,
    )

    assert result["status"] == "stopped"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "btc-updown-5m-current" in lines[1]
    assert "entry_window_missed" in lines[1]
    state = load_session_state(state_path, effective_paper_strategy_ids=[2])
    assert state.paper_strategies[2].round_index == 1


def test_run_paper_trading_does_not_duplicate_missed_current_round_skip_after_restart(tmp_path, monkeypatch):
    cfg = AppConfig(strategy_id=2, paper_strategy_ids=[2])
    monkeypatch.setattr("trader._sleep_until_round_end", lambda cfg, window, stop_event=None: False)

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

        def get_market_by_slug(self, slug: str):
            market = super().get_market_by_slug(slug)
            market["slug"] = slug
            return market

    log_path = tmp_path / "paper.csv"
    state_path = tmp_path / "state.json"

    first = run_paper_trading(
        cfg,
        client=_CurrentMissedNextAvailableClient(),
        state_path=state_path,
        log_path=log_path,
    )
    second = run_paper_trading(
        cfg,
        client=_CurrentMissedNextAvailableClient(),
        state_path=state_path,
        log_path=log_path,
    )

    assert first["status"] == "stopped"
    assert second["status"] == "stopped"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "btc-updown-5m-current" in lines[1]
    assert "entry_window_missed" in lines[1]


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


def test_run_paper_trading_ignores_legacy_recovery_loss_for_max_stake_check(tmp_path, monkeypatch):
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
    assert state.consecutive_losses == 2
    assert state.consecutive_max_stake_skips == 0
    assert state.stop_loss_count == 0
    assert len(state.pending_paper_trades) == 1
    assert state.pending_paper_trades[0].event_slug == "btc-updown-5m-paper-reset"


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

    def fake_sleep_if_not_stopped(_stop_event, seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) == 2:
            stop_event.set()
        return not stop_event.is_set()

    def config_provider():
        value = config_sequence.pop(0) if config_sequence else 5.0
        config_calls.append(value)
        return AppConfig(poll_interval_seconds=value)

    monkeypatch.setattr("trader._sleep_if_not_stopped", fake_sleep_if_not_stopped)

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

    def fake_sleep_if_not_stopped(_stop_event, seconds):
        sleep_calls.append(seconds)
        stop_event.set()
        return False

    def config_provider():
        return AppConfig(poll_interval_seconds=99)

    monkeypatch.setattr("trader.PolymarketClient", RecordingClient)
    monkeypatch.setattr("trader._sleep_if_not_stopped", fake_sleep_if_not_stopped)

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

    def fake_sleep_if_not_stopped(_stop_event, seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            stop_event.set()
        return not stop_event.is_set()

    def config_provider():
        if configs:
            return configs.pop(0)
        return AppConfig(strategy_id=2, paper_strategy_ids=[2, 6], poll_interval_seconds=1)

    monkeypatch.setattr("trader.BinanceDepth5SignalService", RecordingBinanceSignalService)
    monkeypatch.setattr("trader._sleep_if_not_stopped", fake_sleep_if_not_stopped)

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
    assert state.paper_strategies[5].pending_paper_trades[0].signal_delta == pytest.approx(0.04)
    assert state.paper_strategies[6].strategy6_last_ofi_score == pytest.approx(signal.ofi_score)


def test_market_order_min_stake_uses_configured_minimum_only_for_market_buys():
    assert trader._market_min_order_size({"orderMinSize": "5"}) == pytest.approx(5.0)
    assert trader._market_min_order_size({"minimum_order_size": 7}) == pytest.approx(7.0)
    assert trader._effective_min_order_cost(AppConfig(min_stake=2.5), {"orderMinSize": "5"}) == pytest.approx(2.5)
    assert trader._effective_min_order_cost(AppConfig(min_stake=8.0), {"orderMinSize": "5"}) == pytest.approx(8.0)


def test_run_paper_trading_allows_market_buy_when_order_min_size_exceeds_plan_cost(tmp_path, monkeypatch):
    monkeypatch.setattr("trader._sleep_until_round_end", lambda cfg, window, stop_event=None: False)
    monkeypatch.setattr("trader._resolve_side_from_strategy", lambda **kwargs: SideDecision(side="UP"))

    class _MarketMinClient(_LiveMarketClient):
        def get_market_by_slug(self, slug: str):
            market = super().get_market_by_slug(slug)
            market["orderMinSize"] = "5"
            return market

    result = run_paper_trading(
        AppConfig(
            trade_mode="paper",
            paper_strategy_ids=[2],
            base_order_cost=1.2,
            min_stake=1.05,
            poll_interval_seconds=1,
        ),
        client=_MarketMinClient(),
        state_path=tmp_path / "paper_state.json",
        log_path=tmp_path / "paper.csv",
    )

    assert result["status"] == "stopped"
    state = load_session_state(tmp_path / "paper_state.json", effective_paper_strategy_ids=[2])
    pending = state.paper_strategies[2].pending_paper_trades
    assert len(pending) == 1
    assert pending[0].order_cost == pytest.approx(1.2)
    assert pending[0].order_size == pytest.approx(1.2 / 0.52)
    assert not (tmp_path / "paper.csv").exists()


def test_run_live_trading_allows_market_buy_when_order_min_size_exceeds_plan_cost(tmp_path, monkeypatch):
    stop_event = threading.Event()
    stub_clob = _StubClobClient(balance_payload={"available": 10.0})

    def fake_sleep(_seconds):
        stop_event.set()

    class _MarketMinClient(_LiveMarketClient):
        def get_market_by_slug(self, slug: str):
            market = super().get_market_by_slug(slug)
            market["orderMinSize"] = "5"
            return market

    monkeypatch.setattr("trader.time.sleep", fake_sleep)
    monkeypatch.setattr("trader._resolve_side_from_strategy", lambda **kwargs: SideDecision(side="UP"))

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            live_strategy_ids=[2],
            base_order_cost=1.2,
            min_stake=1.05,
            poll_interval_seconds=1,
        ),
        market_client=_MarketMinClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live.csv",
        stop_event=stop_event,
    )

    assert result["status"] == "stopped"
    assert len(stub_clob.created_orders) == 1
    assert stub_clob.created_orders[0].amount == pytest.approx(1.2)


def test_run_live_trading_logs_order_id_and_submitted_plan_fill_source(tmp_path, monkeypatch):
    stop_event = threading.Event()
    stub_clob = _StubClobClient(balance_payload={"available": 10.0})

    def fake_sleep(_seconds):
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)
    monkeypatch.setattr("trader._resolve_side_from_strategy", lambda **kwargs: SideDecision(side="UP"))
    monkeypatch.setattr(
        "trader.build_trade_plan",
        lambda *args, **kwargs: TradePlan(
            True,
            "UP",
            price=0.52,
            order_size=1.923075,
            order_cost=0.999999,
            expected_profit=0.923076,
            max_entry_price=0.54,
        ),
    )

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            live_strategy_ids=[10],
            strategy_id=10,
            min_stake=0.01,
            max_stake=25.0,
            poll_interval_seconds=1,
        ),
        market_client=_LiveMarketClient(),
        clob_client=stub_clob,
        state_path=tmp_path / "live_state.json",
        log_path=tmp_path / "live_orders.csv",
        stop_event=stop_event,
    )

    rows = list(csv.DictReader((tmp_path / "live_orders.csv").open(newline="", encoding="utf-8")))
    submitted = next(row for row in rows if row["strategy"] == "10" and row["side"] == "UP")

    assert result["status"] == "stopped"
    assert submitted["order_id"] == "oid-123"
    assert submitted["fill_source"] == "submitted_plan"


def test_run_live_trading_settlement_uses_fee_adjusted_confirmed_fill_for_pnl(tmp_path, monkeypatch):
    stop_event = threading.Event()
    stub_clob = _StubClobClient(
        order_payloads={"oid-prev": {"status": "filled"}},
        trade_payloads={"oid-prev": [_confirmed_trade("oid-prev", size=1.923075, price=0.52)]},
        balance_payload={"available": 10.0},
    )
    state_path = tmp_path / "live_state.json"
    log_path = tmp_path / "live_orders.csv"
    state_path.write_text(
        json.dumps(
            {
                "live_strategies": {
                    "10": {
                        "round_index": 9,
                        "cash_pnl": 0.0,
                        "recovery_loss": 0.0,
                        "consecutive_losses": 0,
                        "consecutive_max_stake_skips": 0,
                        "signal_round_slug": None,
                        "signal_round_open_up_price": None,
                        "signal_round_locked_side": None,
                        "stop_loss_count": 0,
                        "daily_realized_pnl": 0.0,
                        "current_day": "2026-05-22",
                        "pending_live_slug": "btc-updown-5m-prev",
                        "pending_live_side": "UP",
                        "pending_live_price": 0.52,
                        "pending_live_order_size": 1.923075,
                        "pending_live_order_cost": 0.999999,
                        "pending_live_expected_profit": 0.923076,
                        "pending_live_end_time": "2026-04-02T00:00:00+00:00",
                        "pending_live_order_id": "oid-prev",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    def fake_sleep(_seconds):
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)
    monkeypatch.setattr("trader._resolve_side_from_strategy", lambda **kwargs: SideDecision(side=None, reason="signal_unavailable"))

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            live_strategy_ids=[10],
            strategy_id=10,
            max_stake=25.0,
            poll_interval_seconds=1,
        ),
        market_client=_SettlingLiveClient(),
        clob_client=stub_clob,
        state_path=state_path,
        log_path=log_path,
        stop_event=stop_event,
    )

    rows = list(csv.DictReader(log_path.open(newline="", encoding="utf-8")))
    settled = next(row for row in rows if row["event_slug"] == "btc-updown-5m-prev")

    assert result["status"] == "stopped"
    assert settled["price"] == "0.537472"
    assert settled["raw_price"] == "0.52"
    assert settled["fee"] == "0.0335999664"
    assert settled["raw_order_cost"] == "0.999999"
    assert settled["order_cost"] == "1.0335989664"
    assert settled["expected_profit"] == "0.8894760336"
    assert settled["result"] == "DOWN"
    assert settled["trade_pnl"] == "-1.0335989664"
    assert settled["cash_pnl"] == "-1.0335989664"
    assert settled["fill_source"] == "official_confirmed_trade"


def test_run_live_trading_settlement_replaces_submitted_plan_with_confirmed_fill(tmp_path, monkeypatch):
    stop_event = threading.Event()
    stub_clob = _StubClobClient(
        order_payloads={"oid-prev": {"status": "filled"}},
        trade_payloads={"oid-prev": [_confirmed_trade("oid-prev", size=1.8518518518518516, price=0.54)]},
        balance_payload={"available": 10.0},
    )
    state_path = tmp_path / "live_state.json"
    log_path = tmp_path / "live_orders.csv"
    state_path.write_text(
        json.dumps(
            {
                "live_strategies": {
                    "10": {
                        "round_index": 9,
                        "cash_pnl": 0.0,
                        "recovery_loss": 0.0,
                        "consecutive_losses": 0,
                        "consecutive_max_stake_skips": 0,
                        "signal_round_slug": None,
                        "signal_round_open_up_price": None,
                        "signal_round_locked_side": None,
                        "stop_loss_count": 0,
                        "daily_realized_pnl": 0.0,
                        "current_day": "2026-05-22",
                        "pending_live_slug": "btc-updown-5m-prev",
                        "pending_live_side": "UP",
                        "pending_live_price": 0.52,
                        "pending_live_order_size": 1.923075,
                        "pending_live_order_cost": 0.999999,
                        "pending_live_expected_profit": 0.923076,
                        "pending_live_end_time": "2026-04-02T00:00:00+00:00",
                        "pending_live_order_id": "oid-prev",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    start = datetime(2026, 4, 1, 23, 55, tzinfo=timezone.utc)
    append_trade_log(
        log_path,
        TradeRecord(
            timestamp=start,
            mode="live",
            round_index=8,
            strategy=10,
            entry_timing="OPEN",
            event_slug="btc-updown-5m-prev",
            start_time=start,
            end_time=start + timedelta(minutes=5),
            side="UP",
            price=0.52,
            order_size=1.923075,
            order_cost=0.999999,
            expected_profit=0.923076,
            order_id="oid-prev",
            fill_source="submitted_plan",
        ),
    )

    def fake_sleep(_seconds):
        stop_event.set()

    monkeypatch.setattr("trader.time.sleep", fake_sleep)
    monkeypatch.setattr("trader._resolve_side_from_strategy", lambda **kwargs: SideDecision(side=None, reason="signal_unavailable"))

    result = run_live_trading(
        AppConfig(
            trade_mode="live",
            live_trading_enabled=True,
            live_private_key="pk",
            live_funder="0xfunder",
            live_strategy_ids=[10],
            strategy_id=10,
            max_stake=25.0,
            poll_interval_seconds=1,
        ),
        market_client=_SettlingLiveClient(),
        clob_client=stub_clob,
        state_path=state_path,
        log_path=log_path,
        stop_event=stop_event,
    )

    rows = list(csv.DictReader(log_path.open(newline="", encoding="utf-8")))
    settled = next(row for row in rows if row["event_slug"] == "btc-updown-5m-prev")

    assert result["status"] == "stopped"
    assert settled["price"] == "0.557388"
    assert settled["order_size"] == "1.8518518518518516"
    assert settled["order_cost"] == "1.0321999999999998"
    assert settled["result"] == "DOWN"
    assert settled["trade_pnl"] == "-1.0321999999999998"
    assert settled["order_id"] == "oid-prev"
    assert settled["fill_source"] == "official_confirmed_trade"


def test_run_paper_trading_uses_simulated_budget_like_live_execution(tmp_path, monkeypatch):
    stop_event = threading.Event()

    def fake_sleep(_seconds):
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
            price=0.55,
            order_size=2.0,
            order_cost=1.5,
            expected_profit=0.5,
        ),
    )

    result = run_paper_trading(
        AppConfig(
            trade_mode="paper",
            paper_strategy_ids=[1, 3],
            base_order_cost=1.5,
            paper_simulated_wallet_balance=2.0,
            poll_interval_seconds=1,
        ),
        client=_LiveMarketClient(),
        state_path=tmp_path / "paper_state.json",
        log_path=tmp_path / "paper_trades.csv",
        stop_event=stop_event,
    )

    state = load_session_state(tmp_path / "paper_state.json", effective_paper_strategy_ids=[1, 3])
    rows = (tmp_path / "paper_trades.csv").read_text(encoding="utf-8").splitlines()

    assert result["status"] == "stopped"
    assert len(state.paper_strategies[1].pending_paper_trades) == 1
    assert state.paper_strategies[3].pending_paper_trades == []
    assert len(rows) == 2
    assert ",3,OPEN,btc-updown-5m-test," in rows[1]
    assert "insufficient_live_wallet_balance" in rows[1]


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


def test_run_paper_trading_settles_pending_paper_trade_from_terminal_outcome_prices_when_metadata_missing(tmp_path):
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
                        'event_slug': 'btc-updown-15m-paper-prev',
                        'start_time': '2026-04-06T06:00:00+00:00',
                        'end_time': '2026-04-06T06:15:00+00:00',
                        'side': 'DOWN',
                        'price': 0.54,
                        'order_size': 1.8518518518518516,
                        'order_cost': 1.0,
                        'expected_profit': 0.8518518518518516,
                        'strategy': 1,
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

    class _TerminalPricesClient(_NoMarketClient):
        def get_event_by_slug(self, slug: str):
            assert slug == 'btc-updown-15m-paper-prev'
            return {
                'closed': True,
                'eventMetadata': {},
                'markets': [
                    {
                        'closed': True,
                        'outcomes': '["Up","Down"]',
                        'outcomePrices': '["0","1"]',
                    }
                ],
            }

    result = run_paper_trading(
        AppConfig(market_timeframe='15m', poll_interval_seconds=1),
        client=_TerminalPricesClient(),
        state_path=state_path,
        log_path=tmp_path / 'paper.csv',
        stop_when_safe=lambda: True,
    )

    state = load_session_state(state_path)
    assert result['status'] == 'stopped'
    assert state.pending_paper_trades == []
    assert state.cash_pnl == pytest.approx(0.8518518518518516)
    assert state.daily_realized_pnl == pytest.approx(0.8518518518518516)


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
    def fake_sleep_until_round_end(cfg, window, stop_event=None):
        if stop_event is not None:
            stop_event.set()
        return False

    monkeypatch.setattr("trader._sleep_until_round_end", fake_sleep_until_round_end)
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

    def fake_sleep_if_not_stopped(_stop_event, seconds):
        sleep_calls.append(seconds)
        stop_event.set()
        return False

    monkeypatch.setattr("trader._sleep_if_not_stopped", fake_sleep_if_not_stopped)
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


def test_run_paper_trading_near_entry_fast_poll_uses_shorter_sleep(tmp_path, monkeypatch):
    stop_event = threading.Event()
    sleep_calls: list[float] = []

    class _NearEntryFastPollClient(_LiveMarketClient):
        def find_current_and_next_rounds(self, *, now):
            window = MarketWindow(
                event_id="evt-near-entry-fast-poll",
                market_id="mkt-near-entry-fast-poll",
                slug="btc-updown-15m-near-entry-fast-poll",
                title="Near Entry Fast Poll",
                start_time=now - timedelta(seconds=16),
                end_time=now + timedelta(minutes=14, seconds=44),
                up_token_id="up-token",
                down_token_id="down-token",
            )
            return window, None

    def fake_sleep_if_not_stopped(_stop_event, seconds):
        sleep_calls.append(seconds)
        stop_event.set()
        return False

    monkeypatch.setattr("trader._sleep_if_not_stopped", fake_sleep_if_not_stopped)
    monkeypatch.setattr(
        "trader._resolve_side_from_strategy",
        lambda **kwargs: SideDecision(side=None, reason="signal_unavailable"),
    )

    result = run_paper_trading(
        AppConfig(
            strategy_id=7,
            paper_strategy_ids=[7],
            poll_interval_seconds=5,
            near_entry_poll_window_seconds=10,
            fast_poll_interval_seconds=1,
            open_delay_seconds=25,
        ),
        client=_NearEntryFastPollClient(),
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "paper.csv",
        stop_event=stop_event,
    )

    assert result["status"] == "stopped"
    assert sleep_calls == [1.0]




def test_run_paper_trading_multi_strategy_pending_settlement_allows_new_round(tmp_path, monkeypatch):
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

    class _MultiStrategySettlementClient(_LiveMarketClient):
        def get_event_by_slug(self, slug: str):
            if slug == "btc-updown-5m-paper-s1":
                return {"eventMetadata": {"priceToBeat": None, "finalPrice": None}}
            if slug == "btc-updown-5m-paper-s6":
                return {"eventMetadata": {"priceToBeat": 100.0, "finalPrice": 80.0}}
            raise AssertionError(slug)

    cfg = AppConfig(strategy_id=2, paper_strategy_ids=[1, 6], poll_interval_seconds=1)

    class _NoopBinanceSignalService:
        ws_url = cfg.binance_ws_url.rstrip("/") + "/" + cfg.binance_depth_stream.lstrip("/")

        def latest(self):
            return None

        def refresh_from_rest(self, *, now):
            return None

        def close(self):
            return None

    result = run_paper_trading(
        cfg,
        client=_MultiStrategySettlementClient(),
        state_path=state_path,
        log_path=log_path,
        stop_event=stop_event,
        binance_signal_service=_NoopBinanceSignalService(),
    )

    state = load_session_state(state_path, effective_paper_strategy_ids=[1, 6])
    assert result["status"] == "stopped"
    assert len(state.paper_strategies[1].pending_paper_trades) == 2
    assert state.paper_strategies[1].pending_paper_trades[1].event_slug == "btc-updown-5m-test"
    assert state.paper_strategies[6].pending_paper_trades == []
    assert state.paper_strategies[6].last_processed_paper_event_slug == "btc-updown-5m-test"
    rows = log_path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3
    assert "btc-updown-5m-paper-s6" in rows[1]
    assert "btc-updown-5m-test" in log_path.read_text(encoding="utf-8")


def test_run_paper_trading_does_not_use_ws_only_resolution_for_pending_settlement(tmp_path):
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
                        'side': 'DOWN',
                        'price': 0.4,
                        'order_size': 2.5,
                        'order_cost': 1.0,
                        'expected_profit': 1.5,
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

    class _WsOnlyPaperClient(_NoMarketClient):
        def get_event_by_slug(self, slug: str):
            assert slug == 'btc-updown-5m-paper-prev'
            return {
                'closed': False,
                'eventMetadata': {},
                'markets': [
                    {
                        'closed': False,
                        'outcomes': '["Up","Down"]',
                        'outcomePrices': '["0.2","0.8"]',
                    }
                ],
            }

        def get_ws_market_resolution(self, _market):
            return {'winning_outcome': 'DOWN'}

    result = run_paper_trading(
        AppConfig(poll_interval_seconds=1),
        client=_WsOnlyPaperClient(),
        state_path=state_path,
        log_path=tmp_path / 'paper.csv',
        stop_when_safe=lambda: True,
    )

    state = load_session_state(state_path)
    assert result['status'] == 'stopped'
    assert len(state.pending_paper_trades) == 1
    assert state.pending_paper_trades[0].event_slug == 'btc-updown-5m-paper-prev'
    assert not (tmp_path / 'paper.csv').exists()


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
                            "BASE_ORDER_COST": 1.0,
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
            "BASE_ORDER_COST": 1.2,
            "STRATEGY7_OFI_THRESHOLD": 0.75,
            "STRATEGY7_MOMENTUM_THRESHOLD": 0.03,
            "STRATEGY7_MAX_ENTRY_PRICE": 0.53,
        },
    )

    assert candidate_cfg.strategy_id == 7
    assert candidate_cfg.base_order_cost == pytest.approx(1.2)
    assert candidate_cfg.strategy7_ofi_threshold == pytest.approx(0.75)
    assert candidate_cfg.strategy7_momentum_threshold == pytest.approx(0.03)
    assert candidate_cfg.strategy7_max_entry_price == pytest.approx(0.53)
