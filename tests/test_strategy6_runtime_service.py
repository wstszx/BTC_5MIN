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


def test_apply_strategy6_signal_to_quote_is_noop_for_other_strategies():
    service = BinanceDepth5SignalService(ws_url='wss://stream.binance.com:9443/ws', stream='btcusdt@depth5')
    service.push_payload({'b': [['100000', '5']], 'a': [['100001', '1']]})
    cfg = AppConfig(strategy_id=2)
    quote = MarketQuote(slug='s1')

    _apply_strategy6_signal_to_quote(cfg=cfg, quote=quote, binance_signal_service=service)

    assert quote.strategy6_ofi_score is None
    assert quote.strategy6_signal_at is None
