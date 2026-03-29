from __future__ import annotations


def get_group_size(strategy_id: int) -> int:
    if strategy_id not in (1, 2, 3, 4):
        raise ValueError(f"Unsupported strategy_id: {strategy_id}")
    return strategy_id


def get_side_for_round(strategy_id: int, round_index: int) -> str:
    if round_index < 0:
        raise ValueError("round_index must be non-negative")
    group_size = get_group_size(strategy_id)
    block_index = round_index // group_size
    return "UP" if block_index % 2 == 0 else "DOWN"
