from datetime import datetime, timedelta, timezone

from binance_signal import BinanceDepth5SignalService
from config import AppConfig
from models import MarketQuote
from trader import _apply_strategy6_signal_to_quote, _sync_paper_binance_signal_service


def test_apply_strategy6_signal_to_quote_copies_latest_binance_signal():
    service = BinanceDepth5SignalService(ws_url='wss://stream.binance.com:9443/ws', stream='btcusdt@depth5')
    signal = service.push_payload(
        {'b': [['100000', '5']], 'a': [['100001', '1']]},
        now=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    cfg = AppConfig(strategy_id=6)
    quote = MarketQuote(slug='s1')

    _apply_strategy6_signal_to_quote(cfg=cfg, quote=quote, binance_signal_service=service)

    assert quote.strategy6_ofi_score == signal.ofi_score
    assert quote.strategy6_signal_at == signal.signal_at


def test_apply_strategy6_signal_to_quote_copies_latest_binance_signal_for_strategy9():
    service = BinanceDepth5SignalService(ws_url='wss://stream.binance.com:9443/ws', stream='btcusdt@depth5')
    signal = service.push_payload(
        {'b': [['100000', '5']], 'a': [['100001', '1']]},
        now=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    cfg = AppConfig(strategy_id=9)
    quote = MarketQuote(slug='s1')

    _apply_strategy6_signal_to_quote(cfg=cfg, quote=quote, binance_signal_service=service)

    assert quote.strategy6_ofi_score == signal.ofi_score
    assert quote.strategy6_signal_at == signal.signal_at


def test_apply_strategy6_signal_to_quote_copies_latest_binance_signal_for_strategy10():
    service = BinanceDepth5SignalService(ws_url='wss://stream.binance.com:9443/ws', stream='btcusdt@depth5')
    signal = service.push_payload(
        {'b': [['100000', '5']], 'a': [['100001', '1']]},
        now=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    cfg = AppConfig(strategy_id=10)
    quote = MarketQuote(slug='s1')

    _apply_strategy6_signal_to_quote(cfg=cfg, quote=quote, binance_signal_service=service)

    assert quote.strategy6_ofi_score == signal.ofi_score
    assert quote.strategy6_signal_at == signal.signal_at


def test_apply_strategy6_signal_to_quote_copies_binance_mid_price_for_strategy11():
    service = BinanceDepth5SignalService(ws_url='wss://stream.binance.com:9443/ws', stream='btcusdt@depth5')
    signal = service.push_payload(
        {'b': [['100000', '5']], 'a': [['100020', '1']]},
        now=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    cfg = AppConfig(strategy_id=11)
    quote = MarketQuote(slug='s1')

    _apply_strategy6_signal_to_quote(cfg=cfg, quote=quote, binance_signal_service=service)

    assert quote.strategy6_ofi_score == signal.ofi_score
    assert quote.strategy6_signal_at == signal.signal_at
    assert quote.binance_mid_price == 100010.0
    assert quote.binance_signal_at == signal.signal_at


def test_apply_strategy6_signal_to_quote_copies_binance_mid_price_for_strategy13():
    service = BinanceDepth5SignalService(ws_url='wss://stream.binance.com:9443/ws', stream='btcusdt@depth5')
    signal = service.push_payload(
        {'b': [['100000', '5']], 'a': [['100020', '1']]},
        now=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    cfg = AppConfig(strategy_id=13)
    quote = MarketQuote(slug='s1')

    _apply_strategy6_signal_to_quote(cfg=cfg, quote=quote, binance_signal_service=service)

    assert quote.strategy6_ofi_score == signal.ofi_score
    assert quote.strategy6_signal_at == signal.signal_at
    assert quote.binance_mid_price == 100010.0
    assert quote.binance_signal_at == signal.signal_at


def test_apply_strategy6_signal_to_quote_preserves_fresh_strategy13_quote_binance_price():
    class EmptyBinanceSignalService:
        def __init__(self):
            self.refresh_calls = 0

        def latest(self):
            return None

        def refresh_from_rest(self, *, now):
            self.refresh_calls += 1
            return None

    signal_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    cfg = AppConfig(strategy_id=13, binance_signal_stale_seconds=10.0)
    quote = MarketQuote(
        slug='s1',
        binance_mid_price=100140.0,
        binance_signal_at=signal_at,
    )
    service = EmptyBinanceSignalService()

    _apply_strategy6_signal_to_quote(
        cfg=cfg,
        quote=quote,
        binance_signal_service=service,
        now=datetime.now(timezone.utc),
    )

    assert service.refresh_calls == 0
    assert quote.binance_mid_price == 100140.0
    assert quote.binance_signal_at == signal_at


def test_apply_strategy6_signal_to_quote_is_noop_for_other_strategies():
    service = BinanceDepth5SignalService(ws_url='wss://stream.binance.com:9443/ws', stream='btcusdt@depth5')
    service.push_payload({'b': [['100000', '5']], 'a': [['100001', '1']]})
    cfg = AppConfig(strategy_id=2)
    quote = MarketQuote(slug='s1')

    _apply_strategy6_signal_to_quote(cfg=cfg, quote=quote, binance_signal_service=service)

    assert quote.strategy6_ofi_score is None
    assert quote.strategy6_signal_at is None


def test_apply_strategy6_signal_to_quote_falls_back_to_rest_when_latest_signal_missing():
    service = BinanceDepth5SignalService(
        ws_url='wss://stream.binance.com:9443/ws',
        stream='btcusdt@depth5',
        rest_url='https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=5',
        rest_timeout_seconds=3.0,
    )
    expected_time = datetime.now(timezone.utc) - timedelta(seconds=1)

    class StubSession:
        def get(self, url, *, timeout):
            assert url == 'https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=5'
            assert timeout == 3.0

            class StubResponse:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {'b': [['100000', '5']], 'a': [['100001', '1']]}

            return StubResponse()

    service._rest_session = StubSession()
    cfg = AppConfig(strategy_id=6)
    quote = MarketQuote(slug='s1')

    _apply_strategy6_signal_to_quote(cfg=cfg, quote=quote, binance_signal_service=service, now=expected_time)

    assert quote.strategy6_ofi_score is not None
    assert quote.strategy6_ofi_score > 0
    assert quote.strategy6_signal_at == expected_time


def test_sync_paper_binance_signal_service_starts_for_strategy11(monkeypatch):
    instances = []

    class RecordingBinanceSignalService:
        def __init__(self, *, ws_url, stream):
            self.ws_url = ws_url.rstrip("/") + "/" + stream.lstrip("/")
            self.started = 0
            self.closed = 0
            instances.append(self)

        def start(self):
            self.started += 1

        def close(self):
            self.closed += 1

    monkeypatch.setattr("trader.BinanceDepth5SignalService", RecordingBinanceSignalService)

    service = _sync_paper_binance_signal_service(
        cfg=AppConfig(strategy_id=2),
        strategy_ids=[2, 11],
        service=None,
    )

    assert service is instances[0]
    assert instances[0].started == 1


def test_sync_paper_binance_signal_service_starts_for_strategy13(monkeypatch):
    instances = []

    class RecordingBinanceSignalService:
        def __init__(self, *, ws_url, stream):
            self.ws_url = ws_url.rstrip("/") + "/" + stream.lstrip("/")
            self.started = 0
            self.closed = 0
            instances.append(self)

        def start(self):
            self.started += 1

        def close(self):
            self.closed += 1

    monkeypatch.setattr("trader.BinanceDepth5SignalService", RecordingBinanceSignalService)

    service = _sync_paper_binance_signal_service(
        cfg=AppConfig(strategy_id=2),
        strategy_ids=[2, 13],
        service=None,
    )

    assert service is instances[0]
    assert instances[0].started == 1


def test_sync_paper_binance_signal_service_restarts_matching_service():
    class ExistingBinanceSignalService:
        ws_url = 'wss://stream.binance.com:9443/ws/btcusdt@depth5'

        def __init__(self):
            self.started = 0
            self.closed = 0

        def start(self):
            self.started += 1

        def close(self):
            self.closed += 1

    service = ExistingBinanceSignalService()

    result = _sync_paper_binance_signal_service(
        cfg=AppConfig(strategy_id=11),
        strategy_ids=[11],
        service=service,
    )

    assert result is service
    assert service.started == 1


def test_sync_paper_binance_signal_service_reuses_matching_service_without_start_method():
    class _NoopService:
        ws_url = 'wss://stream.binance.com:9443/ws/btcusdt@depth5'

        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    service = _NoopService()

    result = _sync_paper_binance_signal_service(
        cfg=AppConfig(strategy_id=6, paper_strategy_ids=[6]),
        strategy_ids=[6],
        service=service,
    )

    assert result is service
    assert service.closed == 0
