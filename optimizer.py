from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
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


def build_optimizer_state(
    *,
    ranked_candidates: list[dict[str, Any]],
    champion_id: str | None,
    top_n: int,
    last_run_at: str,
) -> dict[str, Any]:
    active_challengers = [
        {
            "candidate_id": item.get("candidate_id"),
            "base_strategy_id": item.get("base_strategy_id"),
            "params": item.get("params") or {},
            "validation_score": item.get("validation_score"),
        }
        for item in ranked_candidates[: max(0, top_n)]
    ]
    promotable_candidates = [
        {
            "candidate_id": item.get("candidate_id"),
            "base_strategy_id": item.get("base_strategy_id"),
            "params": item.get("params") or {},
            "validation_score": item.get("validation_score"),
        }
        for item in ranked_candidates
        if bool(item.get("promotable"))
    ]
    return {
        "enabled": True,
        "last_run_at": last_run_at,
        "champion_id": champion_id,
        "active_challengers": active_challengers,
        "promotable_candidates": promotable_candidates,
    }


def save_optimizer_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_optimizer_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "enabled": False,
            "last_run_at": None,
            "champion_id": None,
            "active_challengers": [],
            "promotable_candidates": [],
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid optimizer state payload in {path}")
    return payload


def run_optimizer_cycle(
    *,
    ranked_candidates: list[dict[str, Any]],
    champion_id: str | None,
    output_path: Path,
    top_n: int,
    last_run_at: str,
) -> dict[str, Any]:
    payload = build_optimizer_state(
        ranked_candidates=ranked_candidates,
        champion_id=champion_id,
        top_n=top_n,
        last_run_at=last_run_at,
    )
    save_optimizer_state(output_path, payload)
    return payload
