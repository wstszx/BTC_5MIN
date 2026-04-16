from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any, Iterable

from config import AppConfig


@dataclass(frozen=True, slots=True)
class OptimizerCandidate:
    candidate_id: str
    base_strategy_id: int
    params: dict[str, float | int | str]


def build_candidate_configs(
    cfg: AppConfig,
    *,
    strategy_ids: Iterable[int],
    target_profits: Iterable[float],
    max_price_thresholds: Iterable[float],
    strategy5_thresholds: Iterable[float],
) -> list[OptimizerCandidate]:
    candidates: list[OptimizerCandidate] = []
    for strategy_id, target_profit, max_price_threshold in product(strategy_ids, target_profits, max_price_thresholds):
        if strategy_id == 5:
            for momentum_threshold in strategy5_thresholds:
                params = {
                    "TARGET_PROFIT": float(target_profit),
                    "MAX_PRICE_THRESHOLD": float(max_price_threshold),
                    "SIGNAL_MOMENTUM_THRESHOLD": float(momentum_threshold),
                }
                candidates.append(
                    OptimizerCandidate(
                        candidate_id=f"s{strategy_id}-tp{target_profit}-mp{max_price_threshold}-sm{momentum_threshold}",
                        base_strategy_id=strategy_id,
                        params=params,
                    )
                )
        else:
            params = {
                "TARGET_PROFIT": float(target_profit),
                "MAX_PRICE_THRESHOLD": float(max_price_threshold),
            }
            candidates.append(
                OptimizerCandidate(
                    candidate_id=f"s{strategy_id}-tp{target_profit}-mp{max_price_threshold}",
                    base_strategy_id=strategy_id,
                    params=params,
                )
            )
    return candidates


def rank_optimizer_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda item: (
            float(item.get("validation_score", 0.0)),
            -float(item.get("max_drawdown", 0.0)),
            float(item.get("total_pnl", 0.0)),
        ),
        reverse=True,
    )
