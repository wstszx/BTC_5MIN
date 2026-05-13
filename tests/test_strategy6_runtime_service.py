from datetime import datetime, timedelta, timezone

from binance_signal import BinanceDepth5SignalService
from config import AppConfig
from models import MarketQuote
from trader import _apply_strategy6_signal_to_quote


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
