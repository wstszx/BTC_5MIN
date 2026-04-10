from datetime import datetime, timezone

from binance_signal import BinanceDepth5SignalService, parse_depth5_message


def test_parse_depth5_message_builds_positive_ofi_signal_from_array_levels():
    signal = parse_depth5_message(
        {
            'b': [['100000', '10']],
            'a': [['100001', '1']],
        },
        now=datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc),
    )

    assert signal.ofi_score is not None
    assert signal.ofi_score > 0.8
    assert signal.bid_price == 100000.0
    assert signal.ask_qty == 1.0


def test_parse_depth5_message_accepts_object_levels():
    signal = parse_depth5_message(
        {
            'bids': [{'price': '100000', 'quantity': '2'}],
            'asks': [{'price': '100001', 'quantity': '3'}],
        }
    )

    assert signal.bid_qty == 2.0
    assert signal.ask_price == 100001.0


def test_signal_service_stores_latest_signal_from_payload():
    service = BinanceDepth5SignalService(ws_url='wss://stream.binance.com:9443/ws', stream='btcusdt@depth5')

    service.push_payload({'b': [['100000', '5']], 'a': [['100001', '1']]})
    latest = service.latest()

    assert latest is not None
    assert latest.ofi_score is not None
    assert latest.ofi_score > 0
