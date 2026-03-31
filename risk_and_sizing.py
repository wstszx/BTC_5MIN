from __future__ import annotations

from dataclasses import replace

from models import SessionState, TradePlan


def validate_price(price: float | None) -> bool:
    return price is not None and 0 < price < 1


def compute_order_size(recovery_loss: float, target_profit: float, price: float) -> float:
    base = target_profit if recovery_loss <= 0 else recovery_loss + target_profit
    return base / (1 - price)


def compute_order_cost(order_size: float, price: float) -> float:
    return order_size * price


def should_stop_for_daily_loss(daily_realized_pnl: float, daily_loss_cap: float) -> bool:
    return daily_realized_pnl <= -abs(daily_loss_cap)


def should_reset_after_max_losses(consecutive_losses: int, max_consecutive_losses: int) -> bool:
    return consecutive_losses >= max_consecutive_losses


def reset_after_stop_loss(state: SessionState) -> SessionState:
    updated = replace(state)
    updated.recovery_loss = 0.0
    updated.consecutive_losses = 0
    updated.stop_loss_count += 1
    return updated


def build_trade_plan(
    *,
    state: SessionState,
    side: str,
    price: float | None,
    target_profit: float,
    max_price_threshold: float,
    max_stake: float,
    daily_loss_cap: float,
    max_consecutive_losses: int,
    bet_sizing_mode: str = "TARGET_PROFIT",
    base_order_cost: float = 1.0,
) -> TradePlan:
    if side not in {"UP", "DOWN"}:
        raise ValueError(f"Unsupported side: {side}")

    if should_stop_for_daily_loss(state.daily_realized_pnl, daily_loss_cap):
        return TradePlan(False, side=side, price=price, skip_reason="daily_loss_cap_reached")

    if should_reset_after_max_losses(state.consecutive_losses, max_consecutive_losses):
        return TradePlan(
            False,
            side=side,
            price=price,
            skip_reason="max_consecutive_losses_reached",
            stop_loss_triggered=True,
        )

    if not validate_price(price):
        return TradePlan(False, side=side, price=price, skip_reason="invalid_price")

    if price > max_price_threshold:
        return TradePlan(False, side=side, price=price, skip_reason="price_above_threshold")
    mode = bet_sizing_mode.upper()
    if mode == "FIXED_BASE_COST":
        if base_order_cost <= 0:
            return TradePlan(False, side=side, price=price, skip_reason="invalid_base_order_cost")
        if state.recovery_loss <= 0:
            order_cost = base_order_cost
            order_size = order_cost / price
            expected_profit = order_size * (1 - price)
        else:
            expected_profit = state.recovery_loss + base_order_cost
            order_size = expected_profit / (1 - price)
            order_cost = compute_order_cost(order_size, price)
    elif mode == "TARGET_PROFIT":
        order_size = compute_order_size(state.recovery_loss, target_profit, price)
        order_cost = compute_order_cost(order_size, price)
        expected_profit = order_size * (1 - price)
    else:
        return TradePlan(False, side=side, price=price, skip_reason="invalid_bet_sizing_mode")

    if order_cost > max_stake:
        return TradePlan(False, side=side, price=price, skip_reason="order_cost_above_max_stake")

    return TradePlan(
        True,
        side=side,
        price=price,
        order_size=order_size,
        order_cost=order_cost,
        expected_profit=expected_profit,
    )


def apply_round_outcome(state: SessionState, plan: TradePlan, *, won: bool) -> SessionState:
    if not plan.should_trade:
        return replace(state)

    updated = replace(state)

    if won:
        trade_pnl = plan.order_size * (1 - (plan.price or 0.0))
        updated.cash_pnl += trade_pnl
        updated.daily_realized_pnl += trade_pnl
        updated.recovery_loss = 0.0
        updated.consecutive_losses = 0
        return updated

    trade_loss = plan.order_cost
    updated.cash_pnl -= trade_loss
    updated.daily_realized_pnl -= trade_loss
    updated.recovery_loss += trade_loss
    updated.consecutive_losses += 1
    return updated
