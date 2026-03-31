from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import AppConfig
from risk_and_sizing import compute_order_cost, compute_order_size
from strategy import get_side_for_round


@dataclass(slots=True)
class LossStreakThreshold:
    threshold_round: int
    streak_group_count: int
    occurrence_per_round: float


@dataclass(slots=True)
class StreakRiskAnalysis:
    analyzed_round_count: int
    strategy_id: int
    hit_rate: float
    max_loss_streak: int
    max_affordable_round: int
    recommended_reset_round: int
    target_occurrence: float
    thresholds: list[LossStreakThreshold]


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_result(row: dict[str, str]) -> str | None:
    result = (row.get("result") or "").strip().upper()
    if result in {"UP", "DOWN"}:
        return result

    price_to_beat = _optional_float(row.get("price_to_beat"))
    final_price = _optional_float(row.get("final_price"))
    if price_to_beat is None or final_price is None:
        return None
    return "UP" if final_price >= price_to_beat else "DOWN"


def _load_ordered_results(csv_path: Path) -> list[str]:
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    rows.sort(key=lambda row: row.get("start_time") or "")
    results: list[str] = []
    for row in rows:
        resolved = _resolve_result(row)
        if resolved in {"UP", "DOWN"}:
            results.append(resolved)
    return results


def compute_max_affordable_round(
    *,
    target_profit: float,
    max_stake: float,
    worst_case_price: float,
    search_max_round: int = 30,
) -> int:
    if search_max_round < 1:
        return 0
    if worst_case_price <= 0 or worst_case_price >= 1:
        return 0

    recovery_loss = 0.0
    affordable = 0
    for round_index in range(1, search_max_round + 1):
        order_size = compute_order_size(recovery_loss, target_profit, worst_case_price)
        order_cost = compute_order_cost(order_size, worst_case_price)
        if order_cost > max_stake:
            break
        affordable = round_index
        recovery_loss += order_cost
    return affordable


def analyze_streak_risk(
    csv_path: Path,
    cfg: AppConfig,
    *,
    strategy_id: int | None = None,
    target_occurrence: float = 0.01,
    min_round: int = 2,
    max_round: int = 10,
    worst_case_price: float | None = None,
) -> StreakRiskAnalysis:
    if min_round < 1:
        raise ValueError("min_round must be >= 1")
    if max_round < min_round:
        raise ValueError("max_round must be >= min_round")
    if not (0 < target_occurrence < 1):
        raise ValueError("target_occurrence must be between 0 and 1")

    strategy_id = strategy_id or cfg.strategy_id
    if strategy_id == 5:
        raise ValueError("strategy_id=5 (price momentum) is not supported by analyze-streak; use backtest/research-strategy.")
    results = _load_ordered_results(csv_path)
    if not results:
        raise ValueError(f"No resolved rounds found in {csv_path}")

    hits = 0
    loss_streaks: list[int] = []
    current_loss_streak = 0

    for round_index, result in enumerate(results):
        side = get_side_for_round(strategy_id, round_index)
        if side == result:
            hits += 1
            if current_loss_streak > 0:
                loss_streaks.append(current_loss_streak)
                current_loss_streak = 0
        else:
            current_loss_streak += 1
    if current_loss_streak > 0:
        loss_streaks.append(current_loss_streak)

    threshold_rows: list[LossStreakThreshold] = []
    total_rounds = len(results)
    for threshold in range(min_round, max_round + 1):
        group_count = sum(1 for streak_length in loss_streaks if streak_length >= threshold)
        threshold_rows.append(
            LossStreakThreshold(
                threshold_round=threshold,
                streak_group_count=group_count,
                occurrence_per_round=group_count / total_rounds,
            )
        )

    price_for_cap = worst_case_price if worst_case_price is not None else cfg.max_price_threshold
    max_affordable_round = compute_max_affordable_round(
        target_profit=cfg.target_profit,
        max_stake=cfg.max_stake,
        worst_case_price=price_for_cap,
        search_max_round=max(max_round, min_round),
    )
    capital_limit_round = min(max_round, max_affordable_round)
    if capital_limit_round < 1:
        recommended = 1
    else:
        candidates = [
            stat.threshold_round
            for stat in threshold_rows
            if stat.threshold_round <= capital_limit_round and stat.occurrence_per_round <= target_occurrence
        ]
        recommended = min(candidates) if candidates else capital_limit_round

    return StreakRiskAnalysis(
        analyzed_round_count=total_rounds,
        strategy_id=strategy_id,
        hit_rate=hits / total_rounds,
        max_loss_streak=max(loss_streaks, default=0),
        max_affordable_round=max_affordable_round,
        recommended_reset_round=recommended,
        target_occurrence=target_occurrence,
        thresholds=threshold_rows,
    )
