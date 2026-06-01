from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from config import AppConfig
from models import LiveStrategyState, TradePlan


POLYMARKET_CRYPTO_TAKER_FEE_RATE = 0.07


@dataclass(slots=True)
class OrderExecutionResult:
    status: str
    remaining_budget: float | None
    order_id: str | None = None
    response: Any | None = None
    skip_reason: str | None = None
    balance_error: str | None = None
    live_price_cap: float | None = None


def is_retryable_live_clob_error(exc: Exception) -> bool:
    return is_retryable_live_io_error(exc)


def is_live_fok_not_filled_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "order couldn't be fully filled" in message
        and ("fok" in message or "fully filled or killed" in message)
    )


def is_live_trading_restricted_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "trading restricted" in message
        or "geoblock" in message
        or ("status=403" in message and "restricted" in message)
        or ("status_code=403" in message and "restricted" in message)
    )


def is_retryable_live_io_error(exc: Exception) -> bool:
    message = str(exc).lower()
    if any(marker in message for marker in ("status_code=401", "status_code=403", "unauthorized", "forbidden")):
        return False
    retryable_markers = (
        "timeout",
        "timed out",
        "request exception",
        "status_code=none",
        "status_code=5",
        "connection",
        "unable to fetch",
        "temporar",
        "ssl",
        "eof occurred",
        "unexpected_eof_while_reading",
    )
    return any(marker in message for marker in retryable_markers)


def resolve_live_order_type(raw_order_type: str):
    from py_clob_client_v2 import OrderType

    normalized = (raw_order_type or "FOK").upper()
    return getattr(OrderType, normalized, OrderType.FOK)


def coerce_positive_float(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def _field_value(item: Any, *names: str) -> Any:
    for name in names:
        if isinstance(item, dict) and name in item:
            return item.get(name)
        if hasattr(item, name):
            return getattr(item, name)
    return None


def _book_levels(book: Any, side: str) -> list[Any]:
    candidates = [side]
    if side == "asks":
        candidates.extend(["asksList", "ask_levels", "sell"])
    elif side == "bids":
        candidates.extend(["bidsList", "bid_levels", "buy"])
    for name in candidates:
        levels = _field_value(book, name)
        if isinstance(levels, list):
            return levels
    return []


def _level_price(level: Any) -> float | None:
    return coerce_positive_float(_field_value(level, "price", "p"))


def _level_size(level: Any) -> float | None:
    return coerce_positive_float(_field_value(level, "size", "quantity", "qty", "q"))


def available_order_book_ask_size_at_or_below(book: Any, price_cap: float | None) -> float | None:
    if book is None or price_cap is None or price_cap <= 0:
        return None
    total_size = 0.0
    seen_level = False
    for level in _book_levels(book, "asks"):
        price = _level_price(level)
        size = _level_size(level)
        if price is None or size is None:
            continue
        seen_level = True
        if price <= price_cap:
            total_size += size
    return total_size if seen_level else None


def best_order_book_ask_price(book: Any) -> float | None:
    best_price: float | None = None
    for level in _book_levels(book, "asks"):
        price = _level_price(level)
        if price is None:
            continue
        best_price = price if best_price is None else min(best_price, price)
    return best_price


def best_order_book_bid_price(book: Any) -> float | None:
    best_price: float | None = None
    for level in _book_levels(book, "bids"):
        price = _level_price(level)
        if price is None:
            continue
        best_price = price if best_price is None else max(best_price, price)
    return best_price


def best_possible_binary_buy_price(book: Any, opposite_book: Any | None = None) -> float | None:
    candidates: list[float] = []
    ask_price = best_order_book_ask_price(book)
    if ask_price is not None:
        candidates.append(ask_price)
    if opposite_book is not None:
        opposite_bid_price = best_order_book_bid_price(opposite_book)
        if opposite_bid_price is not None and 0 < opposite_bid_price < 1:
            candidates.append(1.0 - opposite_bid_price)
    return min(candidates) if candidates else None


def price_improvement_floor(price: float | None, max_improvement: float | None) -> float | None:
    if price is None or max_improvement is None or max_improvement <= 0:
        return None
    return max(0.0, float(price) - float(max_improvement))


def read_live_order_book(live_client: Any, token_id: str) -> Any | None:
    get_order_book = getattr(live_client, "get_order_book", None)
    if not callable(get_order_book):
        return None
    try:
        from py_clob_client_v2 import BookParams

        try:
            return get_order_book(BookParams(token_id=token_id))
        except (TypeError, AttributeError):
            pass
    except Exception:
        pass
    try:
        return get_order_book(token_id)
    except TypeError:
        try:
            return get_order_book(token_id=token_id)
        except Exception:
            return None
    except Exception:
        return None


def calculate_crypto_taker_fee(shares: float, price: float, fee_rate: float = POLYMARKET_CRYPTO_TAKER_FEE_RATE) -> float:
    if shares <= 0 or not 0 < price < 1 or fee_rate <= 0:
        return 0.0
    d_shares = Decimal(str(shares))
    d_price = Decimal(str(price))
    d_fee_rate = Decimal(str(fee_rate))
    return float(d_shares * d_fee_rate * d_price * (Decimal("1") - d_price))


def effective_price_after_fee(price: float, fee_rate: float = POLYMARKET_CRYPTO_TAKER_FEE_RATE) -> float:
    if not 0 < price < 1 or fee_rate <= 0:
        return price
    return float(Decimal(str(price)) + Decimal(str(fee_rate)) * Decimal(str(price)) * (Decimal("1") - Decimal(str(price))))


def effective_price_cap_to_raw_price_cap(
    effective_price_cap: float | None,
    *,
    fee_rate: float = POLYMARKET_CRYPTO_TAKER_FEE_RATE,
    price_decimals: int = 2,
) -> float | None:
    if effective_price_cap is None or not 0 < effective_price_cap < 1:
        return effective_price_cap
    if fee_rate <= 0:
        return effective_price_cap
    low = 0.0
    high = min(effective_price_cap, 1.0)
    for _ in range(48):
        mid = (low + high) / 2
        effective_mid = mid + fee_rate * mid * (1 - mid)
        if effective_mid <= effective_price_cap:
            low = mid
        else:
            high = mid
    scale = 10**max(0, int(price_decimals))
    return math.floor(low * scale) / scale


def apply_fee_to_trade_plan(plan: TradePlan, *, fee_rate: float = POLYMARKET_CRYPTO_TAKER_FEE_RATE) -> TradePlan:
    if not plan.should_trade or plan.order_size <= 0 or plan.price is None or not 0 < plan.price < 1:
        return plan
    raw_price = plan.raw_price if plan.raw_price is not None else plan.price
    raw_order_cost = plan.raw_order_cost if plan.raw_order_cost is not None else plan.order_size * raw_price
    fee = calculate_crypto_taker_fee(plan.order_size, raw_price, fee_rate)
    if fee <= 0:
        return plan
    effective_cost = float(Decimal(str(raw_order_cost)) + Decimal(str(fee)))
    effective_price = float(Decimal(str(effective_cost)) / Decimal(str(plan.order_size)))
    return TradePlan(
        True,
        side=plan.side,
        price=effective_price,
        order_size=plan.order_size,
        order_cost=effective_cost,
        expected_profit=plan.order_size - effective_cost,
        max_entry_price=plan.max_entry_price,
        order_cost_multiplier=plan.order_cost_multiplier,
        raw_price=raw_price,
        raw_order_cost=raw_order_cost,
        fee=fee,
        skip_reason=plan.skip_reason,
        stop_loss_triggered=plan.stop_loss_triggered,
        tracks_recovery_loss=plan.tracks_recovery_loss,
    )


def _payload_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "trades", "value", "results"):
            items = payload.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []


def _trade_matches_order(trade: dict[str, Any], order_id: str) -> bool:
    candidate = (
        trade.get("order_id")
        or trade.get("orderID")
        or trade.get("orderId")
        or trade.get("taker_order_id")
        or trade.get("takerOrderId")
        or trade.get("taker_orderID")
        or trade.get("takerOrderID")
        or trade.get("id")
    )
    if candidate is not None and str(candidate).strip() == order_id:
        return True
    for key in ("maker_orders", "makerOrders", "orders"):
        orders = trade.get(key)
        if not isinstance(orders, list):
            continue
        for order in orders:
            if not isinstance(order, dict):
                continue
            nested_id = order.get("order_id") or order.get("orderID") or order.get("orderId") or order.get("id")
            if nested_id is not None and str(nested_id).strip() == order_id:
                return True
    return False


def _official_order_associated_trade_ids(order_payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(order_payload, dict):
        return []
    raw_values = (
        order_payload.get("associate_trades")
        or order_payload.get("associateTrades")
        or order_payload.get("associated_trades")
        or order_payload.get("associatedTrades")
        or []
    )
    if isinstance(raw_values, (str, int)):
        raw_values = [raw_values]
    if not isinstance(raw_values, list):
        return []
    trade_ids: list[str] = []
    for raw_value in raw_values:
        trade_id = str(raw_value or "").strip()
        if trade_id:
            trade_ids.append(trade_id)
    return trade_ids


def _read_official_order_trades(
    clob_client: Any,
    order_id: str,
    *,
    order_payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    for method_name in ("get_trades", "getTrades"):
        get_trades = getattr(clob_client, method_name, None)
        if not callable(get_trades):
            continue
        for params in ({"order_id": order_id}, {"orderID": order_id}, {"orderId": order_id}):
            try:
                payload = get_trades(params)
            except (AttributeError, TypeError):
                continue
            items = [
                trade
                for trade in _payload_items(payload)
                if _trade_matches_order(trade, order_id)
            ]
            if items:
                return items
        try:
            from py_clob_client_v2 import TradeParams

            payload = get_trades(TradeParams(id=order_id), only_first_page=True)
        except (ImportError, AttributeError, TypeError):
            pass
        else:
            items = [
                trade
                for trade in _payload_items(payload)
                if _trade_matches_order(trade, order_id)
            ]
            if items:
                return items
        associated_items: list[dict[str, Any]] = []
        for trade_id in _official_order_associated_trade_ids(order_payload):
            try:
                payload = get_trades(TradeParams(id=trade_id), only_first_page=True)
            except (NameError, AttributeError, TypeError):
                break
            associated_items.extend(
                trade
                for trade in _payload_items(payload)
                if _trade_matches_order(trade, order_id)
            )
        if associated_items:
            return associated_items
        try:
            payload = get_trades(None, only_first_page=True)
        except TypeError:
            try:
                payload = get_trades()
            except (AttributeError, TypeError):
                continue
        except AttributeError:
            continue
        items = [
            trade
            for trade in _payload_items(payload)
            if _trade_matches_order(trade, order_id)
        ]
        if items:
            return items
    return []


def _trade_size_and_price(trade: dict[str, Any]) -> tuple[float | None, float | None]:
    size = coerce_positive_float(
        trade.get("size")
        or trade.get("filled_size")
        or trade.get("filledSize")
        or trade.get("matched_size")
        or trade.get("matchedSize")
    )
    price = coerce_positive_float(
        trade.get("price")
        or trade.get("fill_price")
        or trade.get("fillPrice")
        or trade.get("avg_price")
        or trade.get("avgPrice")
    )
    return size, price


def _trade_status(trade: dict[str, Any]) -> str:
    return str(trade.get("status") or trade.get("trade_status") or trade.get("tradeStatus") or "").strip().upper()


def _is_confirmed_trade(trade: dict[str, Any]) -> bool:
    return _trade_status(trade) == "CONFIRMED"


def build_trade_plan_from_official_trades(
    strategy_state: LiveStrategyState,
    *,
    clob_client: Any,
    order_payload: dict[str, Any] | None = None,
    require_confirmed_trades: bool = False,
    fee_rate: float = 0.0,
) -> TradePlan | None:
    order_id = str(strategy_state.pending_live_order_id or "").strip()
    if not order_id:
        return None

    total_size = 0.0
    total_cost = 0.0
    for trade in _read_official_order_trades(clob_client, order_id, order_payload=order_payload):
        if require_confirmed_trades and not _is_confirmed_trade(trade):
            continue
        size, price = _trade_size_and_price(trade)
        if size is None or price is None or not 0 < price < 1:
            continue
        total_size = float(Decimal(str(total_size)) + Decimal(str(size)))
        total_cost = float(Decimal(str(total_cost)) + (Decimal(str(size)) * Decimal(str(price))))

    if total_size <= 0 or total_cost <= 0:
        return None
    fill_price = float(Decimal(str(total_cost)) / Decimal(str(total_size)))
    if not 0 < fill_price < 1:
        return None
    plan = TradePlan(
        True,
        side=strategy_state.pending_live_side,
        price=fill_price,
        order_size=total_size,
        order_cost=total_cost,
        expected_profit=total_size * (1 - fill_price),
        raw_price=fill_price,
        raw_order_cost=total_cost,
        tracks_recovery_loss=strategy_state.pending_live_tracks_recovery_loss,
    )
    return apply_fee_to_trade_plan(plan, fee_rate=fee_rate) if fee_rate > 0 else plan


def extract_live_order_id(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    for key in ("orderID", "orderId", "id"):
        raw = response.get(key)
        if raw is None:
            continue
        order_id = str(raw).strip()
        if order_id:
            return order_id
    return None


def validate_live_submission_response(response: Any) -> str:
    if not isinstance(response, dict):
        raise RuntimeError("Live order not accepted: invalid submission response.")

    if response.get("success") is False:
        reason = response.get("errorMsg") or response.get("error") or response.get("message") or "submission rejected"
        raise RuntimeError(f"Live order not accepted: {reason}")

    order_id = extract_live_order_id(response)
    if order_id is None:
        raise RuntimeError("Live order not accepted: missing order id in submission response.")
    return order_id


def parse_live_balance_value(payload: Any) -> float | None:
    if isinstance(payload, bool):
        return None
    if isinstance(payload, (int, float)):
        value = float(payload)
        if math.isfinite(value) and value >= 0:
            return value
        return None
    if isinstance(payload, str):
        raw = payload.strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        if math.isfinite(value) and value >= 0:
            return value
        return None
    return None


def parse_live_collateral_token_units(payload: Any) -> float | None:
    value = parse_live_balance_value(payload)
    if value is None:
        return None
    if isinstance(payload, int) or (isinstance(payload, str) and payload.strip().isdigit()):
        return value / 1_000_000
    return value


def find_live_balance_value(payload: Any, candidate_keys: tuple[str, ...]) -> float | None:
    direct_value = parse_live_balance_value(payload)
    if direct_value is not None:
        return direct_value
    if isinstance(payload, dict):
        lowered = {str(key).strip().lower(): value for key, value in payload.items()}
        for key in candidate_keys:
            if key in lowered:
                nested_value = find_live_balance_value(lowered[key], candidate_keys)
                if nested_value is not None:
                    return nested_value
        for container_key in ("result", "data", "balances", "allowances", "collateral", "funds"):
            if container_key not in lowered:
                continue
            nested_value = find_live_balance_value(lowered[container_key], candidate_keys)
            if nested_value is not None:
                return nested_value
    if isinstance(payload, (list, tuple)):
        for item in payload:
            nested_value = find_live_balance_value(item, candidate_keys)
            if nested_value is not None:
                return nested_value
    return None


def find_live_balance_allowance_value(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None

    lowered = {str(key).strip().lower(): value for key, value in payload.items()}
    balance = parse_live_collateral_token_units(lowered.get("balance"))
    if balance is None:
        return None
    if balance <= 0:
        return 0.0

    allowances = lowered.get("allowances")
    allowance_values: list[float] = []
    if isinstance(allowances, dict):
        allowance_values = [
            value
            for value in (parse_live_collateral_token_units(item) for item in allowances.values())
            if value is not None
        ]
    else:
        allowance_value = parse_live_collateral_token_units(allowances)
        if allowance_value is not None:
            allowance_values.append(allowance_value)

    if not allowance_values:
        return None
    return min(balance, max(allowance_values))


def read_available_live_balance(*, cfg: AppConfig, clob_client: Any | None) -> float:
    live_client = clob_client or create_live_clob_client(cfg)
    available_keys = (
        "available",
        "available_balance",
        "availablebalance",
        "available_usdc",
        "free",
        "allowance",
        "available_allowance",
        "spendable",
        "spendable_balance",
    )
    get_balance_allowance = getattr(live_client, "get_balance_allowance", None)
    if callable(get_balance_allowance):
        try:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams

            payload = get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        except TypeError:
            payload = get_balance_allowance()
        available_balance = find_live_balance_value(payload, available_keys)
        if available_balance is not None:
            return available_balance
        balance_allowance_value = find_live_balance_allowance_value(payload)
        if balance_allowance_value is not None:
            return balance_allowance_value

    candidate_methods = (
        "get_balance",
        "get_collateral_balance",
        "get_usdc_balance",
    )
    for method_name in candidate_methods:
        read_balance = getattr(live_client, method_name, None)
        if not callable(read_balance):
            continue
        payload = read_balance()
        available_balance = find_live_balance_value(payload, available_keys)
        if available_balance is not None:
            return available_balance
    raise RuntimeError("Unable to determine a trustworthy live wallet balance.")


def build_verified_pending_live_trade_plan(
    strategy_state: LiveStrategyState,
    *,
    clob_client: Any | None,
    require_confirmed_trades: bool = False,
    fee_rate: float = 0.0,
) -> TradePlan | None:
    if strategy_state.pending_live_side not in {"UP", "DOWN"}:
        raise RuntimeError("Pending live trade is missing a valid side.")
    if not strategy_state.pending_live_order_id:
        return None
    if clob_client is None:
        return None

    get_order = getattr(clob_client, "get_order", None)
    if not callable(get_order):
        return None

    order_payload = get_order(strategy_state.pending_live_order_id)
    if not isinstance(order_payload, dict):
        return None

    official_trade_plan = build_trade_plan_from_official_trades(
        strategy_state,
        clob_client=clob_client,
        order_payload=order_payload,
        require_confirmed_trades=require_confirmed_trades,
        fee_rate=fee_rate,
    )
    if official_trade_plan is not None:
        return official_trade_plan

    if require_confirmed_trades:
        return None

    status = str(order_payload.get("status") or "").strip().lower()
    has_fill_markers = any(
        order_payload.get(key) is not None
        for key in (
            "filled_order_size",
            "filledOrderSize",
            "filled_order_cost",
            "filledOrderCost",
            "avg_price",
            "avgPrice",
        )
    )
    if status not in {"filled", "matched"} and not has_fill_markers:
        return None

    order_size = coerce_positive_float(
        order_payload.get("filled_order_size")
        or order_payload.get("filledOrderSize")
        or order_payload.get("size_matched")
        or order_payload.get("matched_size")
    )
    order_cost = coerce_positive_float(
        order_payload.get("filled_order_cost")
        or order_payload.get("filledOrderCost")
        or order_payload.get("filled_value")
        or order_payload.get("filledValue")
        or order_payload.get("cost")
    )
    fill_price = coerce_positive_float(
        order_payload.get("avg_price")
        or order_payload.get("avgPrice")
        or order_payload.get("price")
    )

    if order_size is None and order_cost is not None and fill_price is not None:
        order_size = order_cost / fill_price
    if order_cost is None and order_size is not None and fill_price is not None:
        order_cost = order_size * fill_price
    if fill_price is None and order_size is not None and order_cost is not None:
        fill_price = order_cost / order_size

    if order_size is None or order_cost is None or fill_price is None or not 0 < fill_price < 1:
        return None

    plan = TradePlan(
        True,
        side=strategy_state.pending_live_side,
        price=fill_price,
        order_size=order_size,
        order_cost=order_cost,
        expected_profit=order_size * (1 - fill_price),
        raw_price=fill_price,
        raw_order_cost=order_cost,
        tracks_recovery_loss=strategy_state.pending_live_tracks_recovery_loss,
    )
    return apply_fee_to_trade_plan(plan, fee_rate=fee_rate) if fee_rate > 0 else plan


def build_live_market_order_args(
    *,
    token_id: str,
    plan: TradePlan,
    order_type: Any,
    market_order_price: float | None,
    use_sdk_types: bool,
    user_usdc_balance: float | None = None,
):
    if use_sdk_types:
        from py_clob_client_v2 import MarketOrderArgs, Side

        return MarketOrderArgs(
            token_id=token_id,
            amount=plan.order_cost,
            side=Side.BUY,
            price=market_order_price or 0,
            order_type=order_type,
            user_usdc_balance=user_usdc_balance or 0,
        )

    return type(
        "InjectedMarketOrderArgs",
        (),
        {
            "token_id": token_id,
            "amount": plan.order_cost,
            "side": "BUY",
            "order_type": order_type,
            "price": market_order_price,
            "user_usdc_balance": user_usdc_balance or 0,
        },
    )()


def post_live_market_order(live_client: Any, order_args: Any, order_type: Any) -> Any:
    create_and_post = getattr(live_client, "create_and_post_market_order", None)
    if callable(create_and_post):
        return create_and_post(order_args=order_args, order_type=order_type)

    signed_order = live_client.create_market_order(order_args)
    return live_client.post_order(signed_order, order_type)


def _order_type_text(order_type: Any) -> str:
    return str(getattr(order_type, "name", order_type) or "").upper()


def _fallback_fak_order_type(order_type: Any) -> Any:
    if _order_type_text(order_type) != "FOK":
        return None
    try:
        from py_clob_client_v2 import OrderType

        return getattr(OrderType, "FAK", "FAK")
    except Exception:
        return "FAK"


def _build_live_market_order_args_for_type(
    *,
    token_id: str,
    plan: TradePlan,
    order_type: Any,
    market_order_price: float | None,
    use_sdk_types: bool,
    user_usdc_balance: float | None,
) -> Any:
    return build_live_market_order_args(
        token_id=token_id,
        plan=plan,
        order_type=order_type,
        market_order_price=market_order_price,
        use_sdk_types=use_sdk_types,
        user_usdc_balance=user_usdc_balance,
    )


def submit_live_strategy_order(
    *,
    cfg: AppConfig,
    clob_client: Any | None,
    token_id: str,
    plan: TradePlan,
    user_usdc_balance: float | None = None,
    client_factory: Callable[[AppConfig], Any] | None = None,
) -> tuple[str, Any]:
    live_client = clob_client or (client_factory or create_live_clob_client)(cfg)
    use_sdk = type(live_client).__name__ == "ClobClient"
    order_type = (
        resolve_live_order_type(cfg.live_order_type)
        if use_sdk
        else (cfg.live_order_type or "FOK").upper()
    )
    market_order_price = plan.max_entry_price if plan.max_entry_price is not None else getattr(cfg, "max_entry_price", None)
    order_args = _build_live_market_order_args_for_type(
        token_id=token_id,
        plan=plan,
        order_type=order_type,
        market_order_price=market_order_price,
        use_sdk_types=use_sdk,
        user_usdc_balance=user_usdc_balance,
    )
    try:
        response = post_live_market_order(live_client, order_args, order_type)
    except Exception as exc:
        fallback_order_type = _fallback_fak_order_type(order_type)
        if (
            not getattr(cfg, "live_fok_fallback_to_fak", True)
            or fallback_order_type is None
            or not is_live_fok_not_filled_error(exc)
        ):
            raise
        fallback_order_args = _build_live_market_order_args_for_type(
            token_id=token_id,
            plan=plan,
            order_type=fallback_order_type,
            market_order_price=market_order_price,
            use_sdk_types=use_sdk,
            user_usdc_balance=user_usdc_balance,
        )
        response = post_live_market_order(live_client, fallback_order_args, fallback_order_type)
    return validate_live_submission_response(response), response


def execute_order_plan(
    *,
    mode: str,
    cfg: AppConfig,
    clob_client: Any | None,
    strategy_id: int,
    slug: str,
    token_id: str | None,
    plan: TradePlan,
    remaining_budget: float | None,
    opposite_token_id: str | None = None,
    balance_error: str | None = None,
    client_factory: Callable[[AppConfig], Any] | None = None,
) -> OrderExecutionResult:
    if token_id is None:
        raise RuntimeError(f"Missing token id for side={plan.side} on market={slug}")
    if remaining_budget is None:
        return OrderExecutionResult(
            status="skipped",
            remaining_budget=None,
            skip_reason="live_wallet_balance_unavailable",
            balance_error=balance_error,
        )
    if plan.order_cost > remaining_budget:
        return OrderExecutionResult(
            status="skipped",
            remaining_budget=remaining_budget,
            skip_reason="insufficient_live_wallet_balance",
        )
    if mode == "paper":
        order_id = f"paper-{strategy_id}-{slug}"
        return OrderExecutionResult(
            status="submitted",
            remaining_budget=max(0.0, remaining_budget - plan.order_cost),
            order_id=order_id,
            response={"success": True, "orderID": order_id, "simulated": True},
        )
    market_order_price = plan.max_entry_price if plan.max_entry_price is not None else getattr(cfg, "max_entry_price", None)
    should_precheck_order_book_price = (
        getattr(cfg, "live_precheck_order_book_depth", True)
        and _order_type_text(getattr(cfg, "live_order_type", "FOK")) in {"FOK", "FAK"}
        and market_order_price is not None
    )
    if should_precheck_order_book_price:
        live_client_for_depth = clob_client or (client_factory or create_live_clob_client)(cfg)
        book = read_live_order_book(live_client_for_depth, token_id)
        opposite_book = (
            read_live_order_book(live_client_for_depth, opposite_token_id)
            if opposite_token_id and opposite_token_id != token_id
            else None
        )
        best_buy_price = best_possible_binary_buy_price(book, opposite_book)
        min_entry_price = getattr(cfg, "min_entry_price", None)
        if (
            best_buy_price is not None
            and min_entry_price is not None
            and best_buy_price + 1e-9 < min_entry_price
        ):
            return OrderExecutionResult(
                status="skipped",
                remaining_budget=remaining_budget,
                skip_reason="live_order_book_price_below_min_entry",
            )
        improvement_floor = price_improvement_floor(
            plan.raw_price if plan.raw_price is not None else plan.price,
            getattr(cfg, "live_max_price_improvement", None),
        )
        if (
            best_buy_price is not None
            and improvement_floor is not None
            and best_buy_price + 1e-9 < improvement_floor
        ):
            return OrderExecutionResult(
                status="skipped",
                remaining_budget=remaining_budget,
                skip_reason="live_order_book_price_improved_too_much",
            )
        should_require_full_depth = _order_type_text(getattr(cfg, "live_order_type", "FOK")) == "FOK"
        available_size = available_order_book_ask_size_at_or_below(book, market_order_price)
        if should_require_full_depth and available_size is not None and available_size + 1e-9 < plan.order_size:
            return OrderExecutionResult(
                status="skipped",
                remaining_budget=remaining_budget,
                skip_reason="live_order_book_depth_insufficient",
            )
        clob_client = live_client_for_depth
    try:
        order_id, response = submit_live_strategy_order(
            cfg=cfg,
            clob_client=clob_client,
            token_id=token_id,
            plan=plan,
            user_usdc_balance=remaining_budget,
            client_factory=client_factory,
        )
    except Exception as exc:
        if is_live_fok_not_filled_error(exc):
            return OrderExecutionResult(
                status="skipped",
                remaining_budget=remaining_budget,
                skip_reason="live_fok_not_filled",
            )
        if is_retryable_live_clob_error(exc):
            return OrderExecutionResult(
                status="skipped",
                remaining_budget=remaining_budget,
                skip_reason="live_retryable_clob_error",
                balance_error=str(exc),
            )
        raise
    return OrderExecutionResult(
        status="submitted",
        remaining_budget=max(0.0, remaining_budget - plan.order_cost),
        order_id=order_id,
        response=response,
        live_price_cap=plan.max_entry_price if plan.max_entry_price is not None else getattr(cfg, "max_entry_price", None),
    )


def create_live_clob_client(cfg: AppConfig):
    if not cfg.live_private_key:
        raise RuntimeError("Missing PRIVATE_KEY/POLYMARKET_PRIVATE_KEY for live trading.")

    from py_clob_client_v2 import ApiCreds, ClobClient

    def _apply_derived_api_creds(client: Any):
        derive = getattr(client, "derive_api_key", None)
        if not callable(derive):
            derive = getattr(client, "derive_api_creds", None)
        if not callable(derive):
            derive = getattr(client, "create_or_derive_api_key", None)
        if not callable(derive):
            derive = getattr(client, "create_or_derive_api_creds", None)
        if not callable(derive):
            raise RuntimeError("CLOB V2 client does not expose API credential derivation.")
        client.set_api_creds(derive())

    def _is_invalid_api_key_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "invalid api key" in message or "unauthorized" in message or "status_code=401" in message

    clob_client = ClobClient(
        host=cfg.clob_api_base,
        chain_id=cfg.live_chain_id,
        key=cfg.live_private_key,
        signature_type=cfg.live_signature_type,
        funder=cfg.live_funder,
    )
    if cfg.live_api_key and cfg.live_api_secret and cfg.live_api_passphrase:
        clob_client.set_api_creds(
            ApiCreds(
                api_key=cfg.live_api_key,
                api_secret=cfg.live_api_secret,
                api_passphrase=cfg.live_api_passphrase,
            )
        )
        get_api_keys = getattr(clob_client, "get_api_keys", None)
        if callable(get_api_keys):
            try:
                get_api_keys()
            except Exception as exc:
                if not _is_invalid_api_key_error(exc):
                    raise
                print("[live] explicit API credentials rejected; falling back to derived credentials.", flush=True)
                _apply_derived_api_creds(clob_client)
                get_api_keys()
        return clob_client
    _apply_derived_api_creds(clob_client)
    return clob_client


def live_clob_client_config_key(cfg: AppConfig) -> tuple[Any, ...]:
    return (
        cfg.clob_api_base,
        cfg.live_chain_id,
        cfg.live_private_key,
        cfg.live_signature_type,
        cfg.live_funder,
        cfg.live_api_key,
        cfg.live_api_secret,
        cfg.live_api_passphrase,
    )
