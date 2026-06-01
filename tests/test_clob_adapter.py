from __future__ import annotations

import pytest

import trader
from clob_adapter import (
    build_live_market_order_args,
    effective_price_cap_to_raw_price_cap,
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


class _ShallowOrderBookClient(_InjectedOrderClient):
    def __init__(self, book):
        super().__init__()
        self.book = book

    def get_order_book(self, token_id):
        token_id = getattr(token_id, "token_id", token_id)
        assert token_id == "up-token"
        return self.book


class _MappedOrderBookClient(_InjectedOrderClient):
    def __init__(self, books_by_token):
        super().__init__()
        self.books_by_token = books_by_token
        self.order_book_calls = []

    def get_order_book(self, token_id):
        token_id = getattr(token_id, "token_id", token_id)
        self.order_book_calls.append(token_id)
        return self.books_by_token.get(token_id)


class _FokThenFakOrderClient(_InjectedOrderClient):
    def post_order(self, signed_order, order_type):
        self.posted_orders.append((signed_order, order_type))
        if order_type == "FOK":
            raise RuntimeError(
                "[py_clob_client_v2] request error status=400 "
                "url=https://clob.polymarket.com/order "
                'body={"error":"order couldn\'t be fully filled. FOK orders are fully filled or killed."}'
            )
        return {"success": True, "orderID": "oid-fak-fallback"}


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
        if params is not None:
            return []
        assert only_first_page is True
        return [
            {"order_id": "oid-sdk-fill", "size": "1.0", "price": "0.50"},
            {"orderID": "oid-sdk-fill", "size": "2.0", "price": "0.55"},
        ]


class _SdkTakerOrderTradeLookupClient:
    def __init__(self) -> None:
        self.calls = []

    def get_order(self, order_id):
        assert order_id == "oid-sdk-taker"
        return {"status": "matched", "price": "0.60"}

    def get_trades(self, params=None, only_first_page=False):
        self.calls.append((params, only_first_page))
        if isinstance(params, dict):
            raise AttributeError("'dict' object has no attribute 'market'")
        if params is not None:
            return []
        assert only_first_page is True
        return [
            {"taker_order_id": "oid-sdk-taker", "size": "1.923075", "price": "0.52", "status": "CONFIRMED"}
        ]


class _OfficialTakerTradeLookupClient:
    def get_order(self, order_id):
        assert order_id == "oid-official-taker"
        return {
            "status": "matched",
            "size_matched": "3.0",
            "price": "0.60",
        }

    def get_trades(self, params=None, only_first_page=False):
        if params is not None:
            raise AttributeError("'dict' object has no attribute 'market'")
        assert only_first_page is True
        return [
            {"taker_order_id": "oid-official-taker", "size": "1.0", "price": "0.49"},
            {"takerOrderId": "oid-official-taker", "size": "2.0", "price": "0.52"},
            {"taker_order_id": "other-order", "size": "9.0", "price": "0.99"},
        ]


class _OfficialMakerTradeLookupClient:
    def get_order(self, order_id):
        assert order_id == "oid-official-maker"
        return {
            "status": "matched",
            "size_matched": "4.0",
            "price": "0.60",
        }

    def get_trades(self, params=None, only_first_page=False):
        if params is not None:
            raise AttributeError("'dict' object has no attribute 'market'")
        assert only_first_page is True
        return [
            {
                "size": "4.0",
                "price": "0.51",
                "maker_orders": [
                    {"order_id": "oid-official-maker"},
                    {"order_id": "other-order"},
                ],
            },
        ]


class _AssociatedTradesLookupClient:
    def get_order(self, order_id):
        assert order_id == "oid-associated"
        return {
            "status": "matched",
            "size_matched": "3.0",
            "price": "0.60",
            "associate_trades": ["trade-a", "trade-b"],
        }

    def get_trades(self, params=None, only_first_page=False):
        if isinstance(params, dict):
            raise AttributeError("'dict' object has no attribute 'market'")
        trade_id = getattr(params, "id", None)
        if trade_id == "oid-associated":
            return []
        if trade_id == "trade-a":
            return [{"id": "trade-a", "taker_order_id": "oid-associated", "size": "1.0", "price": "0.48"}]
        if trade_id == "trade-b":
            return [{"id": "trade-b", "taker_order_id": "oid-associated", "size": "2.0", "price": "0.51"}]
        assert params is None
        assert only_first_page is True
        return []


class _PendingOfficialTradeLookupClient:
    def get_order(self, order_id):
        assert order_id == "oid-pending-chain"
        return {
            "status": "matched",
            "size_matched": "2.0",
            "price": "0.50",
            "associate_trades": ["trade-pending"],
        }

    def get_trades(self, params=None, only_first_page=False):
        if isinstance(params, dict):
            raise AttributeError("'dict' object has no attribute 'market'")
        trade_id = getattr(params, "id", None)
        if trade_id == "oid-pending-chain":
            return []
        if trade_id == "trade-pending":
            return [
                {
                    "id": "trade-pending",
                    "taker_order_id": "oid-pending-chain",
                    "size": "2.0",
                    "price": "0.50",
                    "status": "MINED",
                }
            ]
        assert params is None
        assert only_first_page is True
        return []


class _ConfirmedOfficialTradeLookupClient:
    def get_order(self, order_id):
        assert order_id == "oid-confirmed-chain"
        return {
            "status": "matched",
            "size_matched": "2.0",
            "price": "0.50",
            "associate_trades": ["trade-confirmed"],
        }

    def get_trades(self, params=None, only_first_page=False):
        if isinstance(params, dict):
            raise AttributeError("'dict' object has no attribute 'market'")
        trade_id = getattr(params, "id", None)
        if trade_id == "oid-confirmed-chain":
            return []
        if trade_id == "trade-confirmed":
            return [
                {
                    "id": "trade-confirmed",
                    "taker_order_id": "oid-confirmed-chain",
                    "size": "2.0",
                    "price": "0.50",
                    "status": "CONFIRMED",
                }
            ]
        assert params is None
        assert only_first_page is True
        return []


def test_clob_adapter_reads_available_balance_from_nested_payload():
    balance = read_available_live_balance(
        cfg=AppConfig(live_private_key="pk", live_funder="0xfunder"),
        clob_client=_BalanceClient(),
    )

    assert balance == pytest.approx(12.5)


def test_clob_adapter_submits_injected_market_order_with_strategy_price_cap():
    client = _InjectedOrderClient()
    plan = TradePlan(
        True,
        side="UP",
        price=0.54,
        order_size=2.0,
        order_cost=1.08,
        expected_profit=0.92,
        max_entry_price=0.55,
    )

    order_id, response = submit_live_strategy_order(
        cfg=AppConfig(strategy_id=7),
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


def test_clob_adapter_converts_effective_price_cap_to_official_raw_limit():
    assert effective_price_cap_to_raw_price_cap(0.54, fee_rate=0.07) == pytest.approx(0.52)


def test_clob_adapter_submits_market_order_with_raw_price_cap_from_max_entry_price():
    client = _InjectedOrderClient()
    plan = TradePlan(
        True,
        side="UP",
        price=0.54,
        order_size=2.0,
        order_cost=1.08,
        expected_profit=0.92,
        max_entry_price=0.54,
    )

    submit_live_strategy_order(
        cfg=AppConfig(strategy_id=10),
        clob_client=client,
        token_id="up-token",
        plan=plan,
    )

    assert client.created_orders[0].price == pytest.approx(0.54)


def test_clob_adapter_skips_when_order_book_depth_cannot_fill_fok_market_order():
    client = _ShallowOrderBookClient({"asks": [{"price": "0.52", "size": "1.0"}]})
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
        cfg=AppConfig(strategy_id=10, live_order_type="FOK"),
        clob_client=client,
        strategy_id=10,
        slug="btc-updown-5m-test",
        token_id="up-token",
        plan=plan,
        remaining_budget=10.0,
    )

    assert execution.status == "skipped"
    assert execution.skip_reason == "live_order_book_depth_insufficient"
    assert client.created_orders == []
    assert client.posted_orders == []


def test_clob_adapter_skips_when_best_ask_is_too_far_below_decision_price():
    client = _ShallowOrderBookClient({"asks": [{"price": "0.18", "size": "10.0"}]})
    plan = TradePlan(
        True,
        side="UP",
        price=0.505,
        order_size=2.0,
        order_cost=1.01,
        expected_profit=0.99,
        max_entry_price=0.54,
    )

    execution = trader._execute_order_plan(
        mode="live",
        cfg=AppConfig(
            strategy_id=10,
            live_order_type="FOK",
            live_max_price_improvement=0.05,
        ),
        clob_client=client,
        strategy_id=10,
        slug="btc-updown-5m-test",
        token_id="up-token",
        plan=plan,
        remaining_budget=10.0,
    )

    assert execution.status == "skipped"
    assert execution.skip_reason == "live_order_book_price_improved_too_much"
    assert client.created_orders == []
    assert client.posted_orders == []


def test_clob_adapter_skips_when_best_ask_is_below_min_entry_price():
    client = _ShallowOrderBookClient({"asks": [{"price": "0.49", "size": "10.0"}]})
    plan = TradePlan(
        True,
        side="UP",
        price=0.505,
        order_size=2.0,
        order_cost=1.01,
        expected_profit=0.99,
        max_entry_price=0.54,
    )

    execution = trader._execute_order_plan(
        mode="live",
        cfg=AppConfig(
            strategy_id=10,
            live_order_type="FOK",
            min_entry_price=0.50,
            live_max_price_improvement=0.05,
        ),
        clob_client=client,
        strategy_id=10,
        slug="btc-updown-5m-test",
        token_id="up-token",
        plan=plan,
        remaining_budget=10.0,
    )

    assert execution.status == "skipped"
    assert execution.skip_reason == "live_order_book_price_below_min_entry"
    assert client.created_orders == []
    assert client.posted_orders == []


def test_clob_adapter_skips_fak_when_best_ask_is_below_min_entry_price():
    client = _ShallowOrderBookClient({"asks": [{"price": "0.48", "size": "10.0"}]})
    plan = TradePlan(
        True,
        side="UP",
        price=0.505,
        order_size=2.0,
        order_cost=1.01,
        expected_profit=0.99,
        max_entry_price=0.54,
    )

    execution = trader._execute_order_plan(
        mode="live",
        cfg=AppConfig(
            strategy_id=7,
            live_order_type="FAK",
            min_entry_price=0.50,
            live_max_price_improvement=0.05,
        ),
        clob_client=client,
        strategy_id=7,
        slug="btc-updown-5m-test",
        token_id="up-token",
        plan=plan,
        remaining_budget=10.0,
    )

    assert execution.status == "skipped"
    assert execution.skip_reason == "live_order_book_price_below_min_entry"
    assert client.created_orders == []
    assert client.posted_orders == []


def test_clob_adapter_skips_fak_when_complementary_bid_implies_price_below_min_entry():
    client = _MappedOrderBookClient(
        {
            "down-token": {"asks": [{"price": "0.54", "size": "10.0"}]},
            "up-token": {"bids": [{"price": "0.54", "size": "10.0"}]},
        },
    )
    plan = TradePlan(
        True,
        side="DOWN",
        price=0.535,
        order_size=2.0,
        order_cost=1.07,
        expected_profit=0.93,
        max_entry_price=0.54,
    )

    execution = trader._execute_order_plan(
        mode="live",
        cfg=AppConfig(
            strategy_id=10,
            live_order_type="FAK",
            min_entry_price=0.52,
            live_max_price_improvement=0.05,
        ),
        clob_client=client,
        strategy_id=10,
        slug="btc-updown-5m-test",
        token_id="down-token",
        opposite_token_id="up-token",
        plan=plan,
        remaining_budget=10.0,
    )

    assert execution.status == "skipped"
    assert execution.skip_reason == "live_order_book_price_below_min_entry"
    assert client.order_book_calls == ["down-token", "up-token"]
    assert client.created_orders == []
    assert client.posted_orders == []


def test_clob_adapter_ignores_same_token_bid_when_opposite_token_is_unknown():
    client = _ShallowOrderBookClient(
        {
            "asks": [{"price": "0.54", "size": "10.0"}],
            "bids": [{"price": "0.54", "size": "10.0"}],
        }
    )
    plan = TradePlan(
        True,
        side="UP",
        price=0.535,
        order_size=2.0,
        order_cost=1.07,
        expected_profit=0.93,
        max_entry_price=0.54,
    )

    execution = trader._execute_order_plan(
        mode="live",
        cfg=AppConfig(
            strategy_id=10,
            live_order_type="FAK",
            min_entry_price=0.52,
            live_max_price_improvement=0.05,
        ),
        clob_client=client,
        strategy_id=10,
        slug="btc-updown-5m-test",
        token_id="up-token",
        plan=plan,
        remaining_budget=10.0,
    )

    assert execution.status == "submitted"
    assert len(client.created_orders) == 1
    assert client.posted_orders


def test_clob_adapter_falls_back_to_fak_when_fok_market_order_is_not_filled():
    client = _FokThenFakOrderClient()
    plan = TradePlan(
        True,
        side="UP",
        price=0.54,
        order_size=2.0,
        order_cost=1.08,
        expected_profit=0.92,
        max_entry_price=0.54,
    )

    order_id, response = submit_live_strategy_order(
        cfg=AppConfig(strategy_id=10, live_order_type="FOK"),
        clob_client=client,
        token_id="up-token",
        plan=plan,
    )

    assert order_id == "oid-fak-fallback"
    assert response["success"] is True
    assert [posted[1] for posted in client.posted_orders] == ["FOK", "FAK"]
    assert len(client.created_orders) == 2
    assert client.created_orders[0].order_type == "FOK"
    assert client.created_orders[1].order_type == "FAK"
    assert client.created_orders[0].price == pytest.approx(0.54)
    assert client.created_orders[1].price == pytest.approx(0.54)


def test_clob_adapter_can_disable_fok_to_fak_fallback():
    client = _FokThenFakOrderClient()
    plan = TradePlan(
        True,
        side="UP",
        price=0.54,
        order_size=2.0,
        order_cost=1.08,
        expected_profit=0.92,
        max_entry_price=0.54,
    )

    with pytest.raises(RuntimeError, match="fully filled"):
        submit_live_strategy_order(
            cfg=AppConfig(strategy_id=10, live_order_type="FOK", live_fok_fallback_to_fak=False),
            clob_client=client,
            token_id="up-token",
            plan=plan,
        )

    assert [posted[1] for posted in client.posted_orders] == ["FOK"]


def test_clob_adapter_passes_user_balance_to_market_buy_for_fee_adjustment():
    plan = TradePlan(True, side="UP", price=0.54, order_size=10.0, order_cost=5.4, expected_profit=4.6)

    order_args = build_live_market_order_args(
        token_id="up-token",
        plan=plan,
        order_type="FOK",
        market_order_price=0.55,
        use_sdk_types=False,
        user_usdc_balance=5.5,
    )

    assert order_args.amount == pytest.approx(5.4)
    assert order_args.price == pytest.approx(0.55)
    assert order_args.user_usdc_balance == pytest.approx(5.5)


def test_clob_adapter_prefers_plan_dynamic_price_cap():
    client = _InjectedOrderClient()
    plan = TradePlan(
        True,
        side="UP",
        price=0.525,
        max_entry_price=0.53,
        order_size=2.0,
        order_cost=1.05,
        expected_profit=0.95,
    )

    order_id, response = submit_live_strategy_order(
        cfg=AppConfig(strategy_id=9, max_entry_price=0.56),
        clob_client=client,
        token_id="up-token",
        plan=plan,
    )

    assert order_id == "oid-adapter"
    assert response["success"] is True
    assert client.created_orders[0].price == pytest.approx(0.53)


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
        pending_live_tracks_recovery_loss=False,
    )

    plan = build_verified_pending_live_trade_plan(state, clob_client=_OrderLookupClient())

    assert plan is not None
    assert plan.side == "UP"
    assert plan.price == pytest.approx(0.5)
    assert plan.order_size == pytest.approx(2.0)
    assert plan.order_cost == pytest.approx(1.0)
    assert plan.expected_profit == pytest.approx(1.0)
    assert plan.tracks_recovery_loss is False


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


def test_clob_adapter_fallback_matches_sdk_taker_order_id_trade_fills():
    client = _SdkTakerOrderTradeLookupClient()
    state = LiveStrategyState(
        pending_live_side="UP",
        pending_live_order_id="oid-sdk-taker",
    )

    plan = build_verified_pending_live_trade_plan(state, clob_client=client)

    assert plan is not None
    assert plan.order_size == pytest.approx(1.923075)
    assert plan.order_cost == pytest.approx(0.999999)
    assert plan.price == pytest.approx(0.52)


def test_clob_adapter_accounts_for_crypto_taker_fee_on_official_fills():
    client = _SdkTakerOrderTradeLookupClient()
    state = LiveStrategyState(
        pending_live_side="UP",
        pending_live_order_id="oid-sdk-taker",
    )

    plan = build_verified_pending_live_trade_plan(state, clob_client=client, fee_rate=0.07)

    assert plan is not None
    assert plan.raw_price == pytest.approx(0.52)
    assert plan.price == pytest.approx(0.537472)
    assert plan.order_size == pytest.approx(1.923075)
    assert plan.raw_order_cost == pytest.approx(0.999999)
    assert plan.fee == pytest.approx(0.0335999664)
    assert plan.order_cost == pytest.approx(1.0335989664)
    assert plan.expected_profit == pytest.approx(0.8894760336)


def test_clob_adapter_matches_official_taker_order_id_trade_fills():
    state = LiveStrategyState(
        pending_live_side="UP",
        pending_live_order_id="oid-official-taker",
    )

    plan = build_verified_pending_live_trade_plan(state, clob_client=_OfficialTakerTradeLookupClient())

    assert plan is not None
    assert plan.order_size == pytest.approx(3.0)
    assert plan.order_cost == pytest.approx(1.53)
    assert plan.price == pytest.approx(1.53 / 3.0)


def test_clob_adapter_matches_official_maker_order_trade_fills():
    state = LiveStrategyState(
        pending_live_side="UP",
        pending_live_order_id="oid-official-maker",
    )

    plan = build_verified_pending_live_trade_plan(state, clob_client=_OfficialMakerTradeLookupClient())

    assert plan is not None
    assert plan.order_size == pytest.approx(4.0)
    assert plan.order_cost == pytest.approx(2.04)
    assert plan.price == pytest.approx(0.51)


def test_clob_adapter_fetches_associated_trade_ids_before_using_order_limit_price():
    state = LiveStrategyState(
        pending_live_side="UP",
        pending_live_order_id="oid-associated",
    )

    plan = build_verified_pending_live_trade_plan(state, clob_client=_AssociatedTradesLookupClient())

    assert plan is not None
    assert plan.order_size == pytest.approx(3.0)
    assert plan.order_cost == pytest.approx(1.5)
    assert plan.price == pytest.approx(0.5)


def test_clob_adapter_requires_confirmed_official_trade_before_settlement_plan():
    state = LiveStrategyState(
        pending_live_side="UP",
        pending_live_order_id="oid-pending-chain",
    )

    plan = build_verified_pending_live_trade_plan(
        state,
        clob_client=_PendingOfficialTradeLookupClient(),
        require_confirmed_trades=True,
    )

    assert plan is None


def test_clob_adapter_accepts_confirmed_official_trade_for_settlement_plan():
    state = LiveStrategyState(
        pending_live_side="UP",
        pending_live_order_id="oid-confirmed-chain",
    )

    plan = build_verified_pending_live_trade_plan(
        state,
        clob_client=_ConfirmedOfficialTradeLookupClient(),
        require_confirmed_trades=True,
    )

    assert plan is not None
    assert plan.order_size == pytest.approx(2.0)
    assert plan.order_cost == pytest.approx(1.0)
    assert plan.price == pytest.approx(0.5)


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
