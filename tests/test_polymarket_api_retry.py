import requests
from datetime import datetime, timezone

from config import AppConfig
from polymarket_api import PolymarketClient


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FlakySession:
    def __init__(self):
        self.headers = {}
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls < 3:
            raise requests.exceptions.SSLError("transient ssl")
        return _StubResponse({"ok": True})


class _AlwaysFailSession:
    def __init__(self):
        self.headers = {}
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        raise requests.exceptions.ConnectionError("network down")


def test_get_json_retries_with_backoff_and_recovers(monkeypatch):
    sleeps = []
    monkeypatch.setattr("polymarket_api.time.sleep", lambda seconds: sleeps.append(seconds))

    client = PolymarketClient(AppConfig(), session=_FlakySession())
    payload = client._get_json("/ping", base_url="https://example.com", retries=3)

    assert payload == {"ok": True}
    assert client.session.calls == 3
    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0]


def test_get_json_raises_runtime_error_after_retries(monkeypatch):
    monkeypatch.setattr("polymarket_api.time.sleep", lambda _seconds: None)
    client = PolymarketClient(AppConfig(), session=_AlwaysFailSession())

    try:
        client._get_json("/events", base_url="https://example.com", retries=2)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected RuntimeError")

    assert "Unable to fetch https://example.com/events" in message
    assert "after 2 attempts" in message
    assert client.session.calls == 2


def test_ws_message_handler_ingests_book_and_price_change():
    client = PolymarketClient(AppConfig(ws_enabled=False))

    book_msg = '[{"asset_id":"up-token","bids":[{"price":"0.51"}],"asks":[{"price":"0.52"}]}]'
    client._handle_ws_message(book_msg)
    snapshot = client._ws_quotes_by_asset.get("up-token")
    assert snapshot is not None
    assert snapshot["best_bid"] == 0.51
    assert snapshot["best_ask"] == 0.52

    change_msg = '{"event_type":"price_change","price_changes":[{"asset_id":"up-token","price":"0.53","best_bid":"0.52","best_ask":"0.54"}]}'
    client._handle_ws_message(change_msg)
    updated = client._ws_quotes_by_asset.get("up-token")
    assert updated is not None
    assert updated["last_price"] == 0.53
    assert updated["best_bid"] == 0.52
    assert updated["best_ask"] == 0.54


def test_quote_from_market_falls_back_to_http_when_ws_empty():
    cfg = AppConfig(ws_enabled=True)
    client = PolymarketClient(cfg)

    # Avoid opening a real socket in unit test; force empty WS snapshot.
    client._ws_quote_for_assets = lambda _asset_ids: {}  # type: ignore[method-assign]

    market = {
        "slug": "btc-updown-5m-test",
        "outcomes": '["Up", "Down"]',
        "outcomePrices": '["0.55", "0.45"]',
        "clobTokenIds": '["up-token", "down-token"]',
        "bestBid": "0.54",
        "bestAsk": "0.56",
        "acceptingOrders": True,
    }

    quote = client.quote_from_market(market)
    assert quote.source == "http"
    assert quote.up_price == 0.55
    assert quote.up_best_ask == 0.56


def test_quote_from_market_prefers_ws_snapshot_when_available():
    cfg = AppConfig(ws_enabled=True)
    client = PolymarketClient(cfg)

    now = datetime.now(timezone.utc)

    def fake_ws(_asset_ids):
        return {
            "up-token": {
                "last_price": 0.58,
                "best_bid": 0.57,
                "best_ask": 0.59,
                "updated_at": now,
            },
            "down-token": {
                "last_price": 0.42,
                "best_bid": 0.41,
                "best_ask": 0.43,
                "updated_at": now,
            },
        }

    client._ws_quote_for_assets = fake_ws  # type: ignore[method-assign]

    market = {
        "slug": "btc-updown-5m-test",
        "outcomes": '["Up", "Down"]',
        "outcomePrices": '["0.55", "0.45"]',
        "clobTokenIds": '["up-token", "down-token"]',
        "bestBid": "0.54",
        "bestAsk": "0.56",
        "acceptingOrders": True,
    }

    quote = client.quote_from_market(market)
    assert quote.source == "websocket"
    assert quote.up_price == 0.58
    assert quote.down_price == 0.42
    assert quote.up_best_ask == 0.59


def test_get_ws_runtime_stats_reports_core_fields():
    client = PolymarketClient(AppConfig(ws_enabled=True))

    stats = client.get_ws_runtime_stats()
    assert stats["ws_enabled"] is True
    assert stats["ws_connected"] is False
    assert stats["ws_connect_attempts"] == 0
    assert stats["ws_reconnect_count"] == 0
    assert stats["ws_subscribed_asset_count"] == 0
    assert stats["ws_cached_asset_count"] == 0
    assert stats["ws_last_message_age_seconds"] is None
