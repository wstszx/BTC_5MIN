from __future__ import annotations

import json

import pytest

from config import AppConfig
from polymarket_api import PolymarketClient


def test_quote_from_market_uses_best_levels_and_midpoint_from_ws_book_snapshot(monkeypatch):
    client = PolymarketClient(AppConfig(ws_enabled=True))
    monkeypatch.setattr(client, "_ws_subscribe_assets", lambda asset_ids: None)

    try:
        client._handle_ws_message(
            json.dumps(
                [
                    {
                        "event_type": "book",
                        "asset_id": "up-token",
                        "bids": [
                            {"price": "0.01", "size": "100"},
                            {"price": "0.46", "size": "50"},
                        ],
                        "asks": [
                            {"price": "0.99", "size": "100"},
                            {"price": "0.52", "size": "60"},
                        ],
                    },
                    {
                        "event_type": "book",
                        "asset_id": "down-token",
                        "bids": [
                            {"price": "0.01", "size": "100"},
                            {"price": "0.48", "size": "80"},
                        ],
                        "asks": [
                            {"price": "0.99", "size": "100"},
                            {"price": "0.54", "size": "70"},
                        ],
                    },
                ]
            )
        )

        quote = client.quote_from_market(
            {
                "slug": "btc-updown-5m-test",
                "outcomes": '["Up", "Down"]',
                "outcomePrices": '["0.515", "0.485"]',
                "clobTokenIds": '["up-token", "down-token"]',
                "bestBid": "0.50",
                "bestAsk": "0.53",
                "acceptingOrders": True,
            }
        )
    finally:
        client.close()

    assert quote.source == "websocket"
    assert quote.up_best_bid == pytest.approx(0.46)
    assert quote.up_best_ask == pytest.approx(0.52)
    assert quote.down_best_bid == pytest.approx(0.48)
    assert quote.down_best_ask == pytest.approx(0.54)
    assert quote.up_price == pytest.approx(0.49)
    assert quote.down_price == pytest.approx(0.51)
