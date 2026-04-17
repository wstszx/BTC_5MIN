from __future__ import annotations


def _is_valid_price(price: float | None) -> bool:
    return price is not None and 0 < price < 1


def compute_ofi_score(
    bid_price: float | None,
    bid_qty: float | None,
    ask_price: float | None,
    ask_qty: float | None,
) -> float | None:
    try:
        bid_power = float(bid_price) * float(bid_qty)
        ask_power = float(ask_price) * float(ask_qty)
    except (TypeError, ValueError):
        return None

    if bid_power <= 0 or ask_power <= 0:
        return None

    total_power = bid_power + ask_power
    if total_power <= 0:
        return None

    return (bid_power - ask_power) / total_power


def get_group_size(strategy_id: int) -> int:
    if strategy_id not in (1, 2, 3, 4):
        raise ValueError(f'Unsupported strategy_id: {strategy_id}')
    return strategy_id


def _pattern_side_for_round(strategy_id: int, round_index: int) -> str:
    if round_index < 0:
        raise ValueError('round_index must be non-negative')
    group_size = get_group_size(strategy_id)
    block_index = round_index // group_size
    return 'UP' if block_index % 2 == 0 else 'DOWN'


def get_side_for_round(
    strategy_id: int,
    round_index: int,
    *,
    signal_open_up_price: float | None = None,
    signal_current_up_price: float | None = None,
    signal_threshold: float = 0.015,
    signal_fallback_strategy_id: int = 2,
    ofi_score: float | None = None,
    ofi_threshold: float = 0.65,
    signal_min_gap: float = 0.0,
) -> str:
    if strategy_id in (1, 2, 3, 4):
        return _pattern_side_for_round(strategy_id, round_index)

    if strategy_id == 5:
        threshold = max(0.0, signal_threshold)
        if _is_valid_price(signal_open_up_price) and _is_valid_price(signal_current_up_price):
            delta = signal_current_up_price - signal_open_up_price
            if delta >= threshold:
                return 'UP'
            if delta <= -threshold:
                return 'DOWN'
        raise ValueError('strategy_id=5 requires strong momentum signal; weak momentum should skip')

    if strategy_id == 6:
        if ofi_score is None:
            raise ValueError('strategy_id=6 requires ofi_score')
        threshold = max(0.0, ofi_threshold)
        if ofi_score >= threshold:
            return 'UP'
        if ofi_score <= -threshold:
            return 'DOWN'
        raise ValueError('strategy_id=6 requires strong ofi_score')

    if strategy_id == 7:
        if ofi_score is None:
            raise ValueError('strategy_id=7 requires ofi_score')
        if not (_is_valid_price(signal_open_up_price) and _is_valid_price(signal_current_up_price)):
            raise ValueError('strategy_id=7 requires momentum prices')

        threshold = max(0.0, ofi_threshold)
        momentum_threshold = max(0.0, signal_threshold)
        min_gap = max(0.0, signal_min_gap)
        momentum_delta = signal_current_up_price - signal_open_up_price

        if abs(ofi_score) < threshold:
            raise ValueError('strategy_id=7 requires strong ofi_score')
        if abs(momentum_delta) < momentum_threshold:
            raise ValueError('strategy_id=7 requires strong momentum signal')
        if ofi_score * momentum_delta <= 0:
            raise ValueError('strategy_id=7 requires agreeing OFI and momentum signals')
        if abs(ofi_score) < threshold + min_gap or abs(momentum_delta) < momentum_threshold + min_gap:
            raise ValueError('strategy_id=7 requires signal gap above threshold')
        return 'UP' if momentum_delta > 0 else 'DOWN'

    raise ValueError(f'Unsupported strategy_id: {strategy_id}')
