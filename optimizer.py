from __future__ import annotations

import json
import csv
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from itertools import product
from statistics import mean
from typing import Any, Callable, Iterable, Sequence

from config import AppConfig
from backtest import run_backtest
from walk_forward import WalkForwardWindow, build_walk_forward_windows


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


def evaluate_candidates_with_walk_forward(
    candidates: Sequence[dict[str, Any] | OptimizerCandidate],
    *,
    rows: Sequence[dict[str, Any]],
    windows: Sequence[WalkForwardWindow],
    scorer: Callable[[dict[str, Any], Sequence[dict[str, Any]], Sequence[dict[str, Any]]], dict[str, float]],
) -> list[dict[str, Any]]:
    evaluated: list[dict[str, Any]] = []
    for raw_candidate in candidates:
        candidate = raw_candidate if isinstance(raw_candidate, dict) else {
            "candidate_id": raw_candidate.candidate_id,
            "base_strategy_id": raw_candidate.base_strategy_id,
            "params": dict(raw_candidate.params),
        }
        scores: list[dict[str, float]] = []
        for window in windows:
            train_rows = rows[window.train_start:window.train_end]
            validation_rows = rows[window.validation_start:window.validation_end]
            scores.append(scorer(candidate, train_rows, validation_rows))
        if scores:
            evaluated.append(
                {
                    **candidate,
                    "window_count": len(scores),
                    "total_pnl": mean(item.get("total_pnl", 0.0) for item in scores),
                    "max_drawdown": mean(item.get("max_drawdown", 0.0) for item in scores),
                    "validation_score": mean(item.get("validation_score", 0.0) for item in scores),
                }
            )
        else:
            evaluated.append(
                {
                    **candidate,
                    "window_count": 0,
                    "total_pnl": 0.0,
                    "max_drawdown": 0.0,
                    "validation_score": 0.0,
                }
            )
    return rank_optimizer_candidates(evaluated)


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


_CANDIDATE_PARAM_ATTR_MAP: dict[str, str] = {
    "TARGET_PROFIT": "target_profit",
    "MAX_PRICE_THRESHOLD": "max_price_threshold",
    "SIGNAL_MOMENTUM_THRESHOLD": "signal_momentum_threshold",
    "OFI_THRESHOLD": "ofi_threshold",
    "MAX_ENTRY_PRICE": "max_entry_price",
}


def _candidate_to_config(base_cfg: AppConfig, candidate: dict[str, Any]) -> AppConfig:
    kwargs: dict[str, Any] = {"strategy_id": int(candidate["base_strategy_id"])}
    for key, value in (candidate.get("params") or {}).items():
        attr = _CANDIDATE_PARAM_ATTR_MAP.get(str(key))
        if attr:
            kwargs[attr] = value
    return replace(base_cfg, **kwargs)


def score_candidate_with_backtest_rows(
    candidate: dict[str, Any],
    *,
    rows: Sequence[dict[str, Any]],
    base_cfg: AppConfig,
) -> dict[str, float]:
    if not rows:
        return {"total_pnl": 0.0, "max_drawdown": 0.0, "validation_score": 0.0, "trade_count": 0.0}

    cfg = _candidate_to_config(base_cfg, candidate)
    with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", suffix=".csv", delete=False) as handle:
        fieldnames = list(rows[0].keys())
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        temp_path = Path(handle.name)
    try:
        result = run_backtest(temp_path, cfg)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return {
        "total_pnl": float(result.total_pnl),
        "max_drawdown": float(result.max_drawdown),
        "validation_score": float(result.total_pnl - result.max_drawdown),
        "trade_count": float(result.trade_count),
    }


def run_optimizer_from_history_csv(
    *,
    csv_path: Path,
    base_cfg: AppConfig,
    output_path: Path,
    strategy_ids: Iterable[int],
    target_profits: Iterable[float],
    max_price_thresholds: Iterable[float],
    strategy5_thresholds: Iterable[float],
    train_size: int,
    validation_size: int,
    step_size: int,
    top_n: int,
    champion_id: str | None,
    last_run_at: str,
) -> dict[str, Any]:
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    windows = build_walk_forward_windows(
        rows,
        train_size=train_size,
        validation_size=validation_size,
        step_size=step_size,
    )
    candidates = build_candidate_configs(
        base_cfg,
        strategy_ids=strategy_ids,
        target_profits=target_profits,
        max_price_thresholds=max_price_thresholds,
        strategy5_thresholds=strategy5_thresholds,
    )
    ranked_candidates = evaluate_candidates_with_walk_forward(
        candidates,
        rows=rows,
        windows=windows,
        scorer=lambda candidate, _train_rows, validation_rows: score_candidate_with_backtest_rows(
            candidate,
            rows=validation_rows,
            base_cfg=base_cfg,
        ),
    )
    return run_optimizer_cycle(
        ranked_candidates=ranked_candidates,
        champion_id=champion_id,
        output_path=output_path,
        top_n=top_n,
        last_run_at=last_run_at,
    )
