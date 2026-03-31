from __future__ import annotations


def _is_valid_price(price: float | None) -> bool:
    return price is not None and 0 < price < 1


def get_group_size(strategy_id: int) -> int:
    if strategy_id not in (1, 2, 3, 4):
        raise ValueError(f"Unsupported strategy_id: {strategy_id}")
    return strategy_id


def _pattern_side_for_round(strategy_id: int, round_index: int) -> str:
    if round_index < 0:
        raise ValueError("round_index must be non-negative")
    group_size = get_group_size(strategy_id)
    block_index = round_index // group_size
    return "UP" if block_index % 2 == 0 else "DOWN"


def get_side_for_round(
    strategy_id: int,
    round_index: int,
    *,
    signal_open_up_price: float | None = None,
    signal_current_up_price: float | None = None,
    signal_threshold: float = 0.015,
    signal_fallback_strategy_id: int = 2,
) -> str:
    if strategy_id in (1, 2, 3, 4):
        return _pattern_side_for_round(strategy_id, round_index)

    if strategy_id != 5:
        raise ValueError(f"Unsupported strategy_id: {strategy_id}")

    threshold = max(0.0, signal_threshold)
    if _is_valid_price(signal_open_up_price) and _is_valid_price(signal_current_up_price):
        delta = signal_current_up_price - signal_open_up_price
        if delta >= threshold:
            return "UP"
        if delta <= -threshold:
            return "DOWN"

    fallback = signal_fallback_strategy_id
    if fallback == 5:
        fallback = 2
    return _pattern_side_for_round(fallback, round_index)
