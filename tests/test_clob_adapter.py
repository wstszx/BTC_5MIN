from __future__ import annotations

import pytest

import trader
from clob_adapter import (
    build_verified_pending_live_trade_plan,
    create_live_clob_client,
    is_live_fok_not_filled_error,
    read_available_live_balance,
    submit_live_strategy_order,
)
from config import AppConfig
from models import LiveStrategyState, TradePlan


class _BalanceClient:
    def get_balance(self):
        return {"result": {"available": "12.5"}}


class _InjectedOrderClient:
    def __init__(self) -> None:
        self.created_orders = []
        self.posted_orders = []

    def create_market_order(self, order_args):
        self.created_orders.append(order_args)
        return {"signed": True, "order": order_args}

    def post_order(self, signed_order, order_type):
        self.posted_orders.append((signed_order, order_type))
        return {"success": True, "orderID": "oid-adapter"}


def test_clob_adapter_identifies_fok_not_filled_error():
    exc = RuntimeError(
        "[py_clob_client_v2] request error status=400 "
        "url=https://clob.polymarket.com/order "
        'body={"error":"order couldn\'t be fully filled. FOK orders are fully filled or killed."}'
    )

    assert is_live_fok_not_filled_error(exc) is True


@pytest.mark.parametrize(
    "message",
    [
        "[py_clob_client_v2] request error status=400 body={'error':'invalid price'}",
        "PolyApiException[status_code=401, error_message={'error': 'Unauthorized'}]",
        "Trading restricted in your region, please refer to available regions",
        "The read operation timed out PolyApiException[status_code=None, error_message=Request exception!]",
    ],
)
def test_clob_adapter_does_not_misclassify_other_live_errors(message):
    assert is_live_fok_not_filled_error(RuntimeError(message)) is False


class _OrderLookupClient:
    def get_order(self, order_id):
        assert order_id == "oid-filled"
        return {
            "status": "filled",
            "filled_order_size": "2.0",
            "filled_order_cost": "1.0",
            "avg_price": "0.5",
        }


class _TradeLookupClient:
    def get_order(self, order_id):
        assert order_id == "oid-split-fill"
        return {
            "status": "matched",
            "size_matched": "3.0",
            "price": "0.60",
        }

    def get_trades(self, params=None):
        assert params == {"order_id": "oid-split-fill"}
        return [
            {"order_id": "oid-split-fill", "size": "1.0", "price": "0.50"},
            {"orderID": "oid-split-fill", "size": "2.0", "price": "0.55"},
            {"order_id": "other-order", "size": "9.0", "price": "0.99"},
        ]


class _SdkTradeLookupClient:
    def __init__(self) -> None:
        self.calls = []

    def get_order(self, order_id):
        assert order_id == "oid-sdk-fill"
        return {
            "status": "matched",
            "size_matched": "3.0",
            "price": "0.60",
        }

    def get_trades(self, params=None, only_first_page=False):
        self.calls.append((params, only_first_page))
        if isinstance(params, dict):
            raise AttributeError("'dict' object has no attribute 'market'")
        assert params is None
        assert only_first_page is True
        return [
            {"order_id": "oid-sdk-fill", "size": "1.0", "price": "0.50"},
            {"orderID": "oid-sdk-fill", "size": "2.0", "price": "0.55"},
        ]


def test_clob_adapter_reads_available_balance_from_nested_payload():
    balance = read_available_live_balance(
        cfg=AppConfig(live_private_key="pk", live_funder="0xfunder"),
        clob_client=_BalanceClient(),
    )

    assert balance == pytest.approx(12.5)


def test_clob_adapter_submits_injected_market_order_with_strategy_price_cap():
    client = _InjectedOrderClient()
    plan = TradePlan(True, side="UP", price=0.54, order_size=2.0, order_cost=1.08, expected_profit=0.92)

    order_id, response = submit_live_strategy_order(
        cfg=AppConfig(strategy_id=7, strategy7_max_entry_price=0.55),
        clob_client=client,
        token_id="up-token",
        plan=plan,
    )

    assert order_id == "oid-adapter"
    assert response["success"] is True
    assert client.created_orders[0].token_id == "up-token"
    assert client.created_orders[0].amount == pytest.approx(1.08)
    assert client.created_orders[0].side == "BUY"
    assert client.created_orders[0].price == pytest.approx(0.55)
    assert client.posted_orders[0][1] == "FOK"


def test_clob_adapter_applies_price_cap_to_strategy4_live_market_order():
    client = _InjectedOrderClient()
    plan = TradePlan(True, side="DOWN", price=0.55, order_size=4.4, order_cost=2.44, expected_profit=1.96)

    order_id, response = submit_live_strategy_order(
        cfg=AppConfig(strategy_id=4, max_entry_price=0.55),
        clob_client=client,
        token_id="down-token",
        plan=plan,
    )

    assert order_id == "oid-adapter"
    assert response["success"] is True
    assert client.created_orders[0].token_id == "down-token"
    assert client.created_orders[0].amount == pytest.approx(2.44)
    assert client.created_orders[0].price == pytest.approx(0.55)
    assert client.posted_orders[0][1] == "FOK"


def test_clob_adapter_builds_verified_pending_live_trade_plan_from_fill_payload():
    state = LiveStrategyState(
        pending_live_side="UP",
        pending_live_order_id="oid-filled",
    )

    plan = build_verified_pending_live_trade_plan(state, clob_client=_OrderLookupClient())

    assert plan is not None
    assert plan.side == "UP"
    assert plan.price == pytest.approx(0.5)
    assert plan.order_size == pytest.approx(2.0)
    assert plan.order_cost == pytest.approx(1.0)
    assert plan.expected_profit == pytest.approx(1.0)


def test_clob_adapter_prefers_official_trade_fills_over_order_limit_price():
    state = LiveStrategyState(
        pending_live_side="UP",
        pending_live_order_id="oid-split-fill",
    )

    plan = build_verified_pending_live_trade_plan(state, clob_client=_TradeLookupClient())

    assert plan is not None
    assert plan.side == "UP"
    assert plan.order_size == pytest.approx(3.0)
    assert plan.order_cost == pytest.approx(1.6)
    assert plan.price == pytest.approx(1.6 / 3.0)
    assert plan.expected_profit == pytest.approx(1.4)


def test_clob_adapter_falls_back_to_sdk_trade_lookup_when_dict_params_are_not_supported():
    client = _SdkTradeLookupClient()
    state = LiveStrategyState(
        pending_live_side="UP",
        pending_live_order_id="oid-sdk-fill",
    )

    plan = build_verified_pending_live_trade_plan(state, clob_client=client)

    assert plan is not None
    assert plan.order_size == pytest.approx(3.0)
    assert plan.order_cost == pytest.approx(1.6)
    assert plan.price == pytest.approx(1.6 / 3.0)
    assert client.calls[0][0] == {"order_id": "oid-sdk-fill"}
    assert client.calls[-1] == (None, True)


def test_trader_reexports_clob_adapter_helpers(monkeypatch):
    assert trader._create_live_clob_client is create_live_clob_client

    client = _InjectedOrderClient()
    order_id, response = trader._submit_live_strategy_order(
        cfg=AppConfig(),
        clob_client=client,
        token_id="up-token",
        plan=TradePlan(True, side="UP", price=0.54, order_size=2.0, order_cost=1.08, expected_profit=0.92),
    )

    assert order_id == "oid-adapter"
    assert response["success"] is True
    assert client.created_orders[0].token_id == "up-token"

    monkeypatch.setattr("trader._create_live_clob_client", lambda _cfg: _BalanceClient())
    assert trader._read_available_live_balance(cfg=AppConfig(), clob_client=None) == pytest.approx(12.5)
