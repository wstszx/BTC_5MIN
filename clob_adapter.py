from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

from config import AppConfig
from models import LiveStrategyState, TradePlan


@dataclass(slots=True)
class OrderExecutionResult:
    status: str
    remaining_budget: float | None
    order_id: str | None = None
    response: Any | None = None
    skip_reason: str | None = None
    balance_error: str | None = None


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


def build_trade_plan_from_official_trades(
    strategy_state: LiveStrategyState,
    *,
    clob_client: Any,
    order_payload: dict[str, Any] | None = None,
) -> TradePlan | None:
    order_id = str(strategy_state.pending_live_order_id or "").strip()
    if not order_id:
        return None

    total_size = 0.0
    total_cost = 0.0
    for trade in _read_official_order_trades(clob_client, order_id, order_payload=order_payload):
        size, price = _trade_size_and_price(trade)
        if size is None or price is None or not 0 < price < 1:
            continue
        total_size += size
        total_cost += size * price

    if total_size <= 0 or total_cost <= 0:
        return None
    fill_price = total_cost / total_size
    if not 0 < fill_price < 1:
        return None
    return TradePlan(
        True,
        side=strategy_state.pending_live_side,
        price=fill_price,
        order_size=total_size,
        order_cost=total_cost,
        expected_profit=total_size * (1 - fill_price),
    )


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
    )
    if official_trade_plan is not None:
        return official_trade_plan

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

    return TradePlan(
        True,
        side=strategy_state.pending_live_side,
        price=fill_price,
        order_size=order_size,
        order_cost=order_cost,
        expected_profit=order_size * (1 - fill_price),
    )


def build_live_market_order_args(
    *,
    token_id: str,
    plan: TradePlan,
    order_type: Any,
    market_order_price: float | None,
    use_sdk_types: bool,
):
    if use_sdk_types:
        from py_clob_client_v2 import MarketOrderArgs, Side

        return MarketOrderArgs(
            token_id=token_id,
            amount=plan.order_cost,
            side=Side.BUY,
            price=market_order_price or 0,
            order_type=order_type,
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
        },
    )()


def post_live_market_order(live_client: Any, order_args: Any, order_type: Any) -> Any:
    create_and_post = getattr(live_client, "create_and_post_market_order", None)
    if callable(create_and_post):
        return create_and_post(order_args=order_args, order_type=order_type)

    signed_order = live_client.create_market_order(order_args)
    return live_client.post_order(signed_order, order_type)


def submit_live_strategy_order(
    *,
    cfg: AppConfig,
    clob_client: Any | None,
    token_id: str,
    plan: TradePlan,
    client_factory: Callable[[AppConfig], Any] | None = None,
) -> tuple[str, Any]:
    live_client = clob_client or (client_factory or create_live_clob_client)(cfg)
    use_sdk = type(live_client).__name__ == "ClobClient"
    order_type = (
        resolve_live_order_type(cfg.live_order_type)
        if use_sdk
        else (cfg.live_order_type or "FOK").upper()
    )
    market_order_price = getattr(cfg, "max_entry_price", None)
    order_args = build_live_market_order_args(
        token_id=token_id,
        plan=plan,
        order_type=order_type,
        market_order_price=market_order_price,
        use_sdk_types=use_sdk,
    )
    response = post_live_market_order(live_client, order_args, order_type)
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
    try:
        order_id, response = submit_live_strategy_order(
            cfg=cfg,
            clob_client=clob_client,
            token_id=token_id,
            plan=plan,
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
